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
import time
import asyncio
import logging
import tempfile
from pathlib import Path

# load_dotenv MUST run before tyos_brain/llm imports — they read env vars at module level
from dotenv import load_dotenv
_BASE_EARLY = Path(__file__).parent
load_dotenv(_BASE_EARLY / ".env")

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.types import MessageCreated, BotStarted
from maxapi.types.attachments import Audio as MaxAudio

import speechkit
import tyos_brain
from tyos_prompts import GREETING

BASE = _BASE_EARLY

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
REMINDER_DELAY = int(os.environ.get("TYOS_REMINDER_DELAY", "3600"))  # секунд до напоминания
REMINDER_TEXT = "Что решили по заказу? Ещё актуально? Если нужно — продолжим расчёт 🌿"
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


# --- Голосовой ввод: аудио из MAX → Yandex STT ---

async def _to_oggopus(raw: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.bin")
        dst = os.path.join(d, "out.ogg")
        with open(src, "wb") as f:
            f.write(raw)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-f", "ogg", dst,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError(f"ffmpeg: {err.decode('utf-8', 'replace')[:200]}")
        with open(dst, "rb") as f:
            return f.read()


async def _stt_from_url(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                raw = await r.read()
        ogg = await _to_oggopus(raw)
        text = (await speechkit.stt(ogg)).strip()
        return text or None
    except Exception as e:
        log.warning("MAX voice STT failed: %s", e)
        return None


def _extract_audio(event: MessageCreated) -> MaxAudio | None:
    atts = (event.message.body.attachments or [])
    for a in atts:
        if isinstance(a, MaxAudio):
            return a
    return None


# --- Адаптация ответа для MAX (без markdown) ---

_MAX_STRIP_PHRASES = [
    ("Оставите телефон или продолжим в MAX?", "Напишите ваш телефон — забронирую."),
    ("или продолжим в MAX?", ""),
    ("можем продолжить в MAX", ""),
    ("продолжим в MAX", ""),
    ("продолжить в MAX", ""),
    ("Если удобнее — можем продолжить в MAX", ""),
    (" в MAX.", "."),
    (" в MAX,", ","),
    (" в MAX ", " "),
    ("через MAX", ""),
]

def _strip_md(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#+\s+', '', text)
    for old, new in _MAX_STRIP_PHRASES:
        text = text.replace(old, new)
    # Чистим двойные пробелы и точки после замен
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\. \.', '.', text)
    return text.strip()


# --- Детект «контакт через MAX» (пользователь уже в MAX, телефон не нужен) ---

_MAX_CONTACT_SIGNALS = (
    "в мах", "в max", "по этому номеру", "напишите сюда", "напишите мне здесь",
    "свяжитесь здесь", "свяжитесь в max", "свяжитесь в мах",
    "пусть свяжется", "напишите в max", "напишите в мах",
    "связаться здесь", "связаться в max",
)


def _is_max_contact_signal(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in _MAX_CONTACT_SIGNALS)


async def _deliver_max_lead(uid: int, session_id: str) -> bool:
    """Доставить заявку с MAX-контактом (без телефона — пользователь в MAX)."""
    import tyos_order as _ord
    import tyos_lead as _lead
    session = tyos_brain._session(session_id)
    order = session.get("order", {})
    if not order.get("items"):
        return False
    packet = _ord.to_packet(
        order,
        contact={"contact": f"MAX-аккаунт (id {uid})", "name": None,
                 "preferred_time": None, "delivery": None, "note": None},
        session_id=session_id,
    )
    status = await _lead.deliver(packet)
    log.info("MAX-contact lead: user=%s status=%s", uid, status)
    return True


async def _notify_hot_order(uid: int, session_id: str) -> None:
    """Уведомить менеджера о горячем расчёте без контакта (MAX user_id известен)."""
    import tyos_order as _ord
    from tyos_lead import render_for_manager
    session = tyos_brain._session(session_id)
    order = session.get("order", {})
    if not order.get("items"):
        return
    packet = _ord.to_packet(order, contact=None, session_id=session_id)
    summary = render_for_manager(packet)
    text = (
        f"Горячий расчёт — контакт не оставил\n"
        f"MAX user_id: {uid}\n\n"
        f"{summary}\n\n"
        f"Можно написать клиенту в MAX напрямую по его id."
    )
    await _notify_manager(text)


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

    # Голосовое сообщение
    if not text:
        audio = _extract_audio(event)
        if audio:
            # Сначала проверяем, не транскрибировал ли MAX сам
            if audio.transcription:
                text = audio.transcription.strip()
            elif audio.payload and getattr(audio.payload, "url", None):
                text = await _stt_from_url(audio.payload.url) or ""
        if not text:
            return

    user_state = _get_user_state(uid)
    # Обновляем время активности и сбрасываем флаг напоминания
    user_state["last_activity"] = time.time()
    user_state["reminder_sent"] = False
    _set_user_state(uid, user_state)
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

    # Если пользователь сигнализировал «свяжитесь в MAX» — доставить лид и закрыть
    if _is_max_contact_signal(text) and not result.get("lead"):
        delivered = await _deliver_max_lead(uid, session_id)
        if delivered:
            await bot.send_message(
                user_id=uid,
                text="Передал менеджеру — напишет вам здесь в MAX в ближайшее время. 🌿"
            )
            return

    await bot.send_message(user_id=uid, text=reply)

    # Горячий расчёт: бот дошёл до финального снимка заказа (actions есть),
    # но клиент ещё не оставил контакт → уведомить менеджера с MAX user_id
    if result.get("actions") and not result.get("lead"):
        await _notify_hot_order(uid, session_id)

    if result.get("lead"):
        log.info("lead delivered via MAX chat: user=%s status=%s", uid, result["lead"])


async def reminder_loop():
    """Раз в 5 минут проверяет брошенные сессии и отправляет напоминание."""
    await asyncio.sleep(60)  # дать боту стартовать
    while True:
        try:
            state = _load_state()
            now = time.time()
            for uid_str, user_state in state.items():
                last = user_state.get("last_activity", 0)
                sent = user_state.get("reminder_sent", False)
                step = user_state.get("step")
                if sent or not last or step not in ("chat", None):
                    continue
                if now - last < REMINDER_DELAY:
                    continue
                # Проверяем, есть ли что-то в сессии
                session_id = f"max-{uid_str}"
                session = tyos_brain._session(session_id)
                has_content = bool(session.get("order", {}).get("items") or session.get("history"))
                if not has_content:
                    continue
                uid = int(uid_str)
                await bot.send_message(user_id=uid, text=REMINDER_TEXT)
                user_state["reminder_sent"] = True
                state[uid_str] = user_state
                _save_state(state)
                log.info("reminder sent to user=%s", uid)
        except Exception as e:
            log.warning("reminder_loop error: %s", e)
        await asyncio.sleep(300)  # проверяем каждые 5 минут


async def main():
    await bot.delete_webhook()
    log.info("Buka MAX bot starting (full mode)...")
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
