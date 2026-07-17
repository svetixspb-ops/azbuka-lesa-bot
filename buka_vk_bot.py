"""
VK-бот Буки — ИИ-консультант Азбуки Леса, зеркало MAX-бота (buka_max_bot.py).

Сценарий 1 (первый заказ через сайт):
  Клиент считает на сайте → «Забронировать в ВК» → vk.me-ссылка с ?ref=токен
  → бот загружает расчёт → собирает имя+телефон → шлёт менеджеру
  → переходит в полный чат-режим

Сценарий 2 (прямой вход / повторный заказ):
  Клиент пишет сообществу «нужна вагонка 50м²»
  → полный диалог: каталог, расчёт, сборка заказа, заявка менеджеру
  (тот же мозг tyos_brain, что в виджете на сайте и в MAX)

Транспорт: VK Bots Long Poll API (сообщения сообщества), без внешних SDK.
Нужно в .env: VK_GROUP_TOKEN (ключ доступа сообщества с правом «сообщения»).
Опционально: VK_LEAD_USER_ID (менеджер; должен сам написать сообществу хоть раз).
"""
import os
import re
import json
import time
import random
import asyncio
import logging
import tempfile
from pathlib import Path

# load_dotenv MUST run before tyos_brain/llm imports — they read env vars at module level
from dotenv import load_dotenv
_BASE_EARLY = Path(__file__).parent
load_dotenv(_BASE_EARLY / ".env")

import aiohttp

import speechkit
import tyos_brain
from tyos_prompts import GREETING

BASE = _BASE_EARLY

VK_API = "https://api.vk.com/method/"
VK_API_VERSION = "5.199"
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN", "").strip()
REMINDER_DELAY = int(os.environ.get("TYOS_REMINDER_DELAY", "3600"))  # секунд до напоминания
REMINDER_TEXT = "Что решили по заказу? Ещё актуально? Если нужно — продолжим расчёт 🌿"
HANDOFF_STORE = BASE / "handoffs.json"
LEADS_PATH = BASE / "leads.jsonl"
STATE_PATH = BASE / "buka_vk_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("buka-vk")

if not VK_GROUP_TOKEN:
    raise SystemExit("VK_GROUP_TOKEN не задан в .env")

# --- Тексты хэндофф-сценария (сайт → ВК) ---
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


# --- VK API (минимальный клиент) ---

class VKError(RuntimeError):
    pass


async def vk_call(http: aiohttp.ClientSession, method: str, **params) -> dict:
    params.setdefault("access_token", VK_GROUP_TOKEN)
    params.setdefault("v", VK_API_VERSION)
    async with http.post(VK_API + method, data=params,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
        data = await r.json()
    if "error" in data:
        err = data["error"]
        raise VKError(f"{method}: [{err.get('error_code')}] {err.get('error_msg')}")
    return data.get("response")


async def vk_send(http: aiohttp.ClientSession, peer_id: int, text: str) -> None:
    # VK режет сообщения ~4096 символов — шлём кусками по абзацам
    chunks = _split_message(text)
    for chunk in chunks:
        await vk_call(
            http, "messages.send",
            peer_id=peer_id, message=chunk,
            random_id=random.randint(1, 2**31 - 1),
        )


def _split_message(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > limit:
            if current:
                chunks.append(current.rstrip())
            current = ""
            while len(para) > limit:  # сверхдлинный абзац — режем жёстко
                chunks.append(para[:limit])
                para = para[limit:]
        current += para + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


async def _notify_manager(http: aiohttp.ClientSession, text: str) -> None:
    lead_uid = os.environ.get("VK_LEAD_USER_ID", "").strip()
    if not lead_uid:
        return
    try:
        await vk_send(http, int(lead_uid), text)
    except Exception as e:
        log.warning("notify manager failed: %s", e)


# --- Голосовой ввод: аудио из ВК → Yandex STT ---

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


async def _stt_from_url(http: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            raw = await r.read()
        ogg = await _to_oggopus(raw)
        text = (await speechkit.stt(ogg)).strip()
        return text or None
    except Exception as e:
        log.warning("VK voice STT failed: %s", e)
        return None


def _extract_voice(message: dict) -> str | None:
    """URL голосового сообщения (VK уже отдаёт ogg) или готовая транскрипция."""
    for a in message.get("attachments") or []:
        if a.get("type") == "audio_message":
            am = a.get("audio_message") or {}
            # VK иногда транскрибирует сам
            tr = (am.get("transcript") or "").strip()
            if tr and am.get("transcript_state") == "done":
                return "TR:" + tr
            url = am.get("link_ogg") or am.get("link_mp3")
            if url:
                return url
    return None


# --- Адаптация ответа для ВК (без markdown, без упоминаний MAX) ---

_VK_STRIP_PHRASES = [
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
    for old, new in _VK_STRIP_PHRASES:
        text = text.replace(old, new)
    # Чистим двойные пробелы и точки после замен
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\. \.', '.', text)
    return text.strip()


# --- Детект «контакт через ВК» (пользователь уже в ВК, телефон не нужен) ---

_VK_CONTACT_SIGNALS = (
    "в вк", "в vk", "во вконтакте", "вконтакте", "по этому номеру",
    "напишите сюда", "напишите мне здесь", "свяжитесь здесь",
    "свяжитесь в вк", "свяжитесь в vk", "пусть свяжется",
    "напишите в вк", "напишите в vk", "связаться здесь", "связаться в вк",
)


def _is_vk_contact_signal(text: str) -> bool:
    low = text.lower()
    return any(sig in low for sig in _VK_CONTACT_SIGNALS)


async def _deliver_vk_lead(uid: int, session_id: str) -> bool:
    """Доставить заявку с ВК-контактом (без телефона — пользователь в ВК)."""
    import tyos_order as _ord
    import tyos_lead as _lead
    session = tyos_brain._session(session_id)
    order = session.get("order", {})
    if not order.get("items"):
        return False
    packet = _ord.to_packet(
        order,
        contact={"contact": f"ВК-аккаунт (vk.com/id{uid})", "name": None,
                 "preferred_time": None, "delivery": None, "note": None},
        session_id=session_id,
    )
    status = await _lead.deliver(packet)
    log.info("VK-contact lead: user=%s status=%s", uid, status)
    return True


async def _notify_hot_order(http: aiohttp.ClientSession, uid: int, session_id: str) -> None:
    """Уведомить менеджера о горячем расчёте без контакта (VK id известен)."""
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
        f"ВК: vk.com/id{uid}\n\n"
        f"{summary}\n\n"
        f"Можно написать клиенту в ВК напрямую."
    )
    await _notify_manager(http, text)


# --- Обработка входящего сообщения ---

async def on_message(http: aiohttp.ClientSession, message: dict) -> None:
    uid = message.get("from_id")
    peer = message.get("peer_id") or uid
    if not uid or uid < 0:  # сообщения от сообществ игнорируем
        return
    text = (message.get("text") or "").strip()

    # Кнопка «Начать» присылает payload {"command": "start"}
    payload_raw = message.get("payload") or ""
    is_start = False
    try:
        is_start = json.loads(payload_raw).get("command") == "start"
    except Exception:
        pass

    # Хэндофф с сайта: vk.me/club...?ref=токен → ref приходит в первом сообщении
    ref = (message.get("ref") or "").strip()
    if ref:
        packet = _load_handoff(ref)
        if packet:
            order_text = _render_order(packet)
            _set_user_state(uid, {"step": "name", "packet": packet, "token": ref})
            await vk_send(http, peer, GREETING_WITH_ORDER + order_text + "\n\n" + ASK_NAME)
            return
        await vk_send(
            http, peer,
            "Расчёт не найден или устарел. Пожалуйста, повторите расчёт на сайте alyansles.ru"
        )
        return

    if is_start and not text:
        session_id = f"vk-{uid}"
        tyos_brain.reset(session_id)
        _set_user_state(uid, {"step": "chat", "last_activity": time.time()})
        await vk_send(http, peer, _strip_md(GREETING))
        return

    # Голосовое сообщение
    if not text:
        voice = _extract_voice(message)
        if voice:
            if voice.startswith("TR:"):
                text = voice[3:]
            else:
                text = await _stt_from_url(http, voice) or ""
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
        await vk_send(http, peer, ASK_PHONE)
        return

    # Хэндофф-поток: сбор телефона → заявка → переход в чат
    if step == "phone":
        user_state["phone"] = text
        user_state["step"] = "chat"  # сразу в чат-режим
        _set_user_state(uid, user_state)

        packet = user_state.get("packet", {})
        name = user_state.get("name", "—")
        phone = text

        lead = {**packet, "name": name, "contact": phone, "source": "vk-handoff"}
        _persist_lead(lead)

        from tyos_lead import render_for_manager
        notify_text = (
            f"Новая заявка — бронь через ВК\n\n"
            f"Имя: {name}\nТелефон: {phone}\n\n"
            f"{render_for_manager(lead)}"
        )
        await _notify_manager(http, notify_text)
        await vk_send(http, peer, DONE_TEXT)
        log.info("VK handoff lead: user=%s", uid)
        return

    # Полный чат-режим (прямой вход или после хэндоффа)
    if not step:
        session_id = f"vk-{uid}"
        tyos_brain.reset(session_id)
        _set_user_state(uid, {"step": "chat", "last_activity": time.time()})

    session_id = f"vk-{uid}"
    try:
        result = await asyncio.wait_for(
            tyos_brain.build_reply(session_id, text),
            timeout=35
        )
    except asyncio.TimeoutError:
        await vk_send(http, peer, "Секунду, задержка — повторите, пожалуйста, запрос.")
        return
    except Exception as e:
        log.error("build_reply error: %s", e)
        await vk_send(
            http, peer,
            "Произошла ошибка. Попробуйте ещё раз или позвоните: 8 (812) 426-17-61"
        )
        return

    reply = _strip_md(result["reply"])

    # Если пользователь сигнализировал «свяжитесь в ВК» — доставить лид и закрыть
    if _is_vk_contact_signal(text) and not result.get("lead"):
        delivered = await _deliver_vk_lead(uid, session_id)
        if delivered:
            await vk_send(
                http, peer,
                "Передал менеджеру — напишет вам здесь в ВК в ближайшее время. 🌿"
            )
            return

    await vk_send(http, peer, reply)

    # Горячий расчёт: бот дошёл до финального снимка заказа (actions есть),
    # но клиент ещё не оставил контакт → уведомить менеджера с VK id
    if result.get("actions") and not result.get("lead"):
        await _notify_hot_order(http, uid, session_id)

    if result.get("lead"):
        log.info("lead delivered via VK chat: user=%s status=%s", uid, result["lead"])


async def reminder_loop(http: aiohttp.ClientSession):
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
                session_id = f"vk-{uid_str}"
                session = tyos_brain._session(session_id)
                has_content = bool(session.get("order", {}).get("items") or session.get("history"))
                if not has_content:
                    continue
                uid = int(uid_str)
                await vk_send(http, uid, REMINDER_TEXT)
                user_state["reminder_sent"] = True
                state[uid_str] = user_state
                _save_state(state)
                log.info("reminder sent to user=%s", uid)
        except Exception as e:
            log.warning("reminder_loop error: %s", e)
        await asyncio.sleep(300)  # проверяем каждые 5 минут


# --- Long Poll ---

async def _group_id(http: aiohttp.ClientSession) -> int:
    resp = await vk_call(http, "groups.getById")
    groups = resp.get("groups") if isinstance(resp, dict) else resp
    return groups[0]["id"]


async def longpoll_loop(http: aiohttp.ClientSession, group_id: int):
    server = key = ts = None
    while True:
        try:
            if not server:
                lp = await vk_call(http, "groups.getLongPollServer", group_id=group_id)
                server, key, ts = lp["server"], lp["key"], lp["ts"]
                log.info("Long Poll server obtained")
            async with http.get(
                server, params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                timeout=aiohttp.ClientTimeout(total=40),
            ) as r:
                data = await r.json(content_type=None)
            failed = data.get("failed")
            if failed == 1:
                ts = data["ts"]
                continue
            if failed in (2, 3):
                server = None  # переполучить key/ts
                continue
            ts = data["ts"]
            for upd in data.get("updates", []):
                if upd.get("type") != "message_new":
                    continue
                message = (upd.get("object") or {}).get("message") or {}
                asyncio.create_task(_safe_on_message(http, message))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("longpoll error: %s — retry in 5s", e)
            server = None
            await asyncio.sleep(5)


async def _safe_on_message(http: aiohttp.ClientSession, message: dict):
    try:
        await on_message(http, message)
    except Exception as e:
        log.error("on_message error: %s", e)


async def main():
    async with aiohttp.ClientSession() as http:
        gid = await _group_id(http)
        log.info("Buka VK bot starting (full mode), group_id=%s ...", gid)
        asyncio.create_task(reminder_loop(http))
        await longpoll_loop(http, gid)


if __name__ == "__main__":
    asyncio.run(main())
