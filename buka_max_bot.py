"""
MAX-бот Буки — ИИ-консультант Азбуки Леса с полным функционалом.

Сценарий 1 (первый заказ через сайт):
  Клиент считает на сайте → «Забронировать в MAX» → deep-link с токеном
  → бот загружает расчёт → собирает имя+телефон → шлёт менеджеру
  → переходит в полный чат-режим

Сценарий 2 (прямой вход / повторный заказ):
  Клиент пишет в MAX «нужна вагонка 50м²»
  → полный диалог: каталог, расчёт, сборка заказа, заявка менеджеру
  (тот же мозг, что в виджете на сайте)
"""
import os
import re
import json
import asyncio
import logging
from pathlib import Path

# load_dotenv MUST run before tyos_brain/llm imports — they read env vars at module level
from dotenv import load_dotenv
_BASE_EARLY = Path(__file__).parent
load_dotenv(_BASE_EARLY / ".env")

from maxapi import Bot, Dispatcher
from maxapi.types import MessageCreated, BotStarted

import tyos_brain
from tyos_prompts import GREETING

BASE = _BASE_EARLY

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
HANDOFF_STORE = BASE / "handoffs.json"
LEADS_PATH = BASE / "leads.jsonl"
STATE_PATH = BASE / "buka_max_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buka-max")

if not MAX_BOT_TOKEN:
    raise SystemExit("MAX_BOT_TOKEN не задан в .env")

# --- Тексты хэндофф-сценария (сайт → MAX) ---
GREETING_WITH_ORDER = "Привет! Я Бука 🌿 — получил ваш расчёт с сайта Азбуки Леса.\n\n"
ASK_NAME = "Как вас зовут? (для подтверждения брони)"
ASK_PHONE = "Укажите номер телефона — менеджер свяжется для подтверждения."
DONE_TEXT = (
    "Спасибо! Заявка принята ✅\n\n"
    "Менеджер позвонит вам в ближайшее время. "
    "Материал зарезервирован на 2–3 дня.\n\n"
    "Если понадобится ещё что-то — пишите прямо сюда, я помогу."
)

# --- Состояние пользователей ---

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    except Exception as e:
        log.warning("state save failed: %s", e)


def _get_user_state(uid: int) -> dict:
    return _load_state().get(str(uid), {})


def _set_user_state(uid: int, data: dict) -> None:
    state = _load_state()
    state[str(uid)] = data
    _save_state(state)


# --- Хэндофф-хранилище (токены с сайта) ---

def _load_handoff(token: str) -> dict | None:
    try:
        store = json.loads(HANDOFF_STORE.read_text("utf-8"))
        return store.get(token)
    except Exception:
        return None


def _render_order(packet: dict) -> str:
    lines = ["Ваш расчёт:"]
    for p in (packet.get("positions") or []):
        row = f"• {p.get('name', '—')}"
        how = p.get("how") or ""
        s = p.get("sum")
        if how:
            row += f" — {how}"
        elif s:
            row += f" — {s:,} ₽".replace(",", " ")
        lines.append(row)
    if packet.get("delivery"):
        lines.append(f"\nДоставка: {packet['delivery']}")
    if packet.get("services"):
        lines.append("Обработка: " + ", ".join(packet["services"]))
    if packet.get("total"):
        lines.append(f"\nИтого ≈ {packet['total']:,} ₽".replace(",", " "))
    return "\n".join(lines)


def _persist_lead(lead: dict) -> None:
    try:
        with open(LEADS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("lead persist failed: %s", e)


async def _notify_manager(text: str) -> None:
    lead_uid = os.environ.get("MAX_LEAD_USER_ID", "").strip()
    if not lead_uid:
        return
    try:
        nb = Bot(MAX_BOT_TOKEN)
        await nb.send_message(user_id=int(lead_uid), text=text)
        await nb.close_session()
    except Exception as e:
        log.warning("notify manager failed: %s", e)


# --- Адаптация ответа для MAX (без markdown) ---

def _strip_md(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#+\s+', '', text)
    # Заменить «или продолжим в MAX?» — в MAX уже находимся
    text = text.replace("Оставите телефон или продолжим в MAX?",
                        "Напишите ваш телефон — забронирую.")
    text = text.replace("или продолжим в MAX?", "")
    return text.strip()


# --- Бот ---

bot = Bot(MAX_BOT_TOKEN)
dp = Dispatcher()


@dp.bot_started()
async def on_start(event: BotStarted):
    uid = event.from_user.user_id
    payload = (event.payload or "").strip()

    if payload:
        # Хэндофф с сайта: загрузить расчёт, собрать контакт
        packet = _load_handoff(payload)
        if not packet:
            await bot.send_message(
                user_id=uid,
                text="Расчёт не найден или устарел. Пожалуйста, повторите расчёт на сайте alyansles.ru"
            )
            return
        order_text = _render_order(packet)
        _set_user_state(uid, {"step": "name", "packet": packet, "token": payload})
        await bot.send_message(
            user_id=uid,
            text=GREETING_WITH_ORDER + order_text + "\n\n" + ASK_NAME
        )
    else:
        # Прямой вход → полный чат-режим
        session_id = f"max-{uid}"
        tyos_brain.reset(session_id)
        _set_user_state(uid, {"step": "chat"})
        await bot.send_message(user_id=uid, text=_strip_md(GREETING))


@dp.message_created()
async def on_message(event: MessageCreated):
    uid = event.from_user.user_id
    text = (event.message.body.text or "").strip()
    if not text:
        return

    user_state = _get_user_state(uid)
    step = user_state.get("step")

    # Хэндофф-поток: сбор имени
    if step == "name":
        user_state["name"] = text
        user_state["step"] = "phone"
        _set_user_state(uid, user_state)
        await bot.send_message(user_id=uid, text=ASK_PHONE)
        return

    # Хэндофф-поток: сбор телефона → заявка → переход в чат
    if step == "phone":
        user_state["phone"] = text
        user_state["step"] = "chat"  # сразу в чат-режим
        _set_user_state(uid, user_state)

        packet = user_state.get("packet", {})
        name = user_state.get("name", "—")
        phone = text

        lead = {**packet, "name": name, "contact": phone, "source": "max-handoff"}
        _persist_lead(lead)

        from tyos_lead import render_for_manager
        notify_text = (
            f"Новая заявка — бронь через MAX\n\n"
            f"Имя: {name}\nТелефон: {phone}\n\n"
            f"{render_for_manager(lead)}"
        )
        await _notify_manager(notify_text)
        await bot.send_message(user_id=uid, text=DONE_TEXT)
        log.info("MAX handoff lead: user=%s name=%s phone=%s", uid, name, phone)
        return

    # Полный чат-режим (прямой вход или после хэндоффа)
    if not step:
        # Первое сообщение без /start — инициализируем чат
        session_id = f"max-{uid}"
        tyos_brain.reset(session_id)
        _set_user_state(uid, {"step": "chat"})

    session_id = f"max-{uid}"
    try:
        result = await asyncio.wait_for(
            tyos_brain.build_reply(session_id, text),
            timeout=35
        )
    except asyncio.TimeoutError:
        await bot.send_message(
            user_id=uid,
            text="Секунду, задержка — повторите, пожалуйста, запрос."
        )
        return
    except Exception as e:
        log.error("build_reply error: %s", e)
        await bot.send_message(
            user_id=uid,
            text="Произошла ошибка. Попробуйте ещё раз или позвоните: 8 (812) 426-17-61"
        )
        return

    reply = _strip_md(result["reply"])
    await bot.send_message(user_id=uid, text=reply)

    if result.get("lead"):
        log.info("lead delivered via MAX chat: user=%s status=%s", uid, result["lead"])


async def main():
    await bot.delete_webhook()
    log.info("Buka MAX bot starting (full mode)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
