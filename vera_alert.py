"""Мгновенные алерты по голосовому роботу Вере — Свете в Telegram.

Зачем. До 10.11.2026 у Веры не было мгновенных алертов ВООБЩЕ. Если отваливалось
распознавание, синтез или мозг, об этом узнавали только утром из дайджеста — а звонки
за ночь просто пропадали. Авария Yandex SpeechKit 11.10.2026 вскрылась случайно: я
полез в логи после рестарта. У Буки такой алерт есть (`tyos_lead.alert_failure`),
у Веры — не было; этот модуль закрывает дыру.

Чем это НЕ является. Это не заявки. Заявки по звонкам с 10.11.2026 в чаты Светы не
идут совсем (журнал + дайджест + недельный разбор) — здесь только сигнал «сломалось».

Правила, в которые уже вложены грабли:

  * кулдаун 20 минут на КАЖДЫЙ вид сбоя отдельно. Общий кулдаун съедался первым же
    видом: падение синтеза глушило алерт про упавший мозг (урок из tyos_lead).
  * наши прогоны (session_id не `sip-…`) помечаются 🧪 и живут со своим кулдауном —
    тестовый сбой не должен выдавать окно настоящему клиентскому.
  * алерт никогда не рушит звонок: любая ошибка отправки только пишется в лог.
  * получатели — VERA_ALERT_CHAT_IDS (пусто и ЗАДАНО = никому), иначе ADMIN_IDS.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("vera-alert")

MSK = timezone(timedelta(hours=3))
COOLDOWN = timedelta(minutes=20)

# Кулдаун считается по ключу «вид сбоя + живой/тестовый».
_last_at: dict[str, datetime] = {}

# Человеческие заголовки видов сбоя: Света должна понять из первой строки, что именно
# сломалось и что слышит клиент.
TITLES = {
    "tts": "Вера не смогла озвучить ответ — клиент слышит тишину",
    "stt": "Вера не смогла распознать речь клиента",
    "brain": "Мозг Веры не ответил во время звонка",
    "api": "API Веры вернул ошибку во время звонка",
}


def _recipients() -> list[str]:
    raw = os.environ.get("VERA_ALERT_CHAT_IDS")
    if raw is None:
        raw = os.environ.get("ADMIN_IDS") or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _is_probe(session_id: str) -> bool:
    """Настоящий звонок — только сессия телефонии (`sip-…`), как в vera_lead._is_test."""
    return not str(session_id or "").startswith("sip-")


async def _post(text: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = _recipients()
    if not (token and chat_ids):
        log.info("алерт не отправлен: нет токена или получателей")
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            for chat_id in chat_ids:
                r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": text})
                if r.status_code != 200:
                    log.error("алерт не ушёл в %s: %s %s", chat_id, r.status_code, r.text[:200])
    except Exception as e:
        log.error("отправка алерта упала: %s", e)


def describe(e: BaseException) -> str:
    """Читаемая причина: у asyncio.TimeoutError пустой str — без имени класса
    в алерте было бы просто «Ошибка:» и ничего больше."""
    detail = str(e).strip()
    name = type(e).__name__
    return f"{name}: {detail}" if detail else name


async def fire(kind: str, detail: str, session_id: str = "", extra: str = "") -> None:
    """Отправить алерт о сбое вида `kind`. Молча выходит, если кулдаун не истёк."""
    probe = _is_probe(session_id)
    key = f"{kind}:{'probe' if probe else 'live'}"
    now = datetime.now(MSK)
    last = _last_at.get(key)
    if last is not None and now - last < COOLDOWN:
        return
    _last_at[key] = now

    head = TITLES.get(kind, f"Сбой Веры ({kind})")
    lines = [("🧪 ТЕСТОВЫЙ ПРОГОН, не живой звонок" if probe else "⚠️ Вера, живой звонок"), head]
    if session_id:
        lines.append(f"Сессия: {session_id}")
    if detail:
        lines.append(f"Причина: {detail}")
    if extra:
        lines.append(extra)
    lines.append(f"({now.strftime('%d.%m.%Y %H:%M МСК')})")
    lines.append("Повтор того же вида сбоя — не раньше чем через 20 минут.")
    await _post("\n".join(lines))
