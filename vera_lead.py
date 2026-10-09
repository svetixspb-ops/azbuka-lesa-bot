"""Заявка по итогам телефонного звонка Веры — менеджеру в Telegram.

Просьба Артёма 09.10.2026: «надо отладить вопрос сборки лида. Должно приходить
Имя, телефон, с которого человек звонил, и кратко о чём разговор».

Собирается в момент завершения звонка (POST /reset из моста, до очистки контекста):
  имя           — brain.NAME, вычленено из первой реплики после приветствия
  телефон       — brain.CALLER, Caller ID из Asterisk (GET /callmeta в dialplan)
  позиции+сумма — brain.ORDER, посчитаны КОДОМ по каталогу, не моделью
  суть          — 1-2 предложения от модели по полной расшифровке разговора

Доставка: всегда журнал vera_leads.jsonl, плюс Telegram через @azbukalesa_bot
(VERA_LEAD_CHAT_IDS → TG_LEAD_CHAT_ID → ADMIN_IDS). Пустые звонки (молчание,
ошиблись номером) не отправляем — см. _worth_sending.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import brain
import llm

log = logging.getLogger("vera-lead")

ROOT = Path(__file__).resolve().parent
LEADS_PATH = ROOT / "vera_leads.jsonl"
MSK = timezone(timedelta(hours=3))

SUMMARY_PROMPT = """Ниже — расшифровка телефонного разговора клиента с роботом-приёмщиком
заявок склада пиломатериалов. Расшифровка машинная, в ней бывают ошибки распознавания.

Опиши СУТЬ разговора в 1-2 предложениях для менеджера, который будет перезванивать:
что человеку нужно и что осталось невыяснено. Без вступлений, без оценок работы робота,
без выдумывания деталей, которых в разговоре нет. Если разговор ни о чём — ответь одним
словом: нет.

РАЗГОВОР:
{dialog}
"""


def _rubles(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽"


def _positions(sid: str) -> tuple[list[str], int]:
    rows = brain.ORDER.get(sid) or {}
    lines, grand = [], 0
    for r in rows.values():
        grand += int(r.get("total") or 0)
        n, unit = r.get("n"), r.get("unit") or "шт"
        qty = f" — {int(n)} {unit}" if n else ""
        total = f", {_rubles(r['total'])}" if r.get("total") else ""
        lines.append(f"{r.get('ref') or 'позиция'}{qty}{total}")
    return lines, grand


def _worth_sending(sid: str, dialog: list[tuple[str, str]]) -> bool:
    """Звонок стоит заявки, если что-то записано ИЛИ клиент реально говорил.

    Один ход — это «алло» и сброс: менеджеру такое слать незачем.
    """
    if brain.ORDER.get(sid):
        return True
    return sum(1 for role, _ in dialog if role == "user") >= 2


def _render(lead: dict[str, Any]) -> str:
    parts = [f"📞 Заявка со звонка — {lead['ts']}"]
    parts.append(f"Имя: {lead['name'] or 'не назвал'}")
    parts.append(f"Телефон: {lead['phone'] or 'номер не определился'}")
    if lead["positions"]:
        parts.append("")
        parts.append("Что записано:")
        parts += [f"• {p}" for p in lead["positions"]]
        if lead["total"]:
            parts.append(f"Итого предварительно: {_rubles(lead['total'])}")
    else:
        parts.append("")
        parts.append("Позиции не записаны — робот до расчёта не дошёл.")
    if lead["summary"]:
        parts.append("")
        parts.append(f"О чём говорили: {lead['summary']}")
    return "\n".join(parts)


def _recipients() -> list[str]:
    raw = (os.environ.get("VERA_LEAD_CHAT_IDS")
           or os.environ.get("TG_LEAD_CHAT_ID")
           or os.environ.get("ADMIN_IDS") or "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]


async def _send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = _recipients()
    if not (token and chat_ids):
        log.warning("заявка не отправлена: нет TELEGRAM_BOT_TOKEN или получателей")
        return False
    ok_all = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for chat_id in chat_ids:
                async with s.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                if not data.get("ok"):
                    log.error("заявка не ушла в %s: %s", chat_id, data)
                    ok_all = False
    except Exception as e:
        log.error("отправка заявки упала: %s", e)
        return False
    return ok_all


async def _summarize(dialog: list[tuple[str, str]]) -> str:
    text = "\n".join(f"{'Клиент' if r == 'user' else 'Вера'}: {c}" for r, c in dialog)
    try:
        out = (await llm.chat([{"role": "user", "content": SUMMARY_PROMPT.format(dialog=text[-8000:])}],
                              temperature=0.2, max_tokens=160)).strip()
    except Exception as e:
        log.warning("сводка не собралась: %s", e)
        return ""
    return "" if out.lower().strip(" .") == "нет" else out


def snapshot(session_id: str) -> dict[str, Any] | None:
    """Снять всё нужное для заявки ИЗ ПАМЯТИ, синхронно. None — заявки не будет.

    Отдельно от отправки нарочно: контекст звонка чистится сразу (brain.reset), а
    сводку у модели и доставку в Telegram доделываем фоном уже по этому снимку.
    """
    sid = str(session_id)
    dialog = brain.session_transcript(sid)
    if not _worth_sending(sid, dialog):
        log.info("[%s] заявку не собираю: разговора по сути не было", sid)
        return None
    positions, total = _positions(sid)
    return {
        "ts": datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК"),
        "session_id": sid,
        "name": brain.NAME.get(sid, ""),
        "phone": brain.CALLER.get(sid, ""),
        "positions": positions,
        "total": total,
        "_dialog": dialog,
    }


async def deliver(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """Дособрать заявку по снимку (сводка у модели) и доставить менеджеру."""
    if not snap:
        return None
    lead = {k: v for k, v in snap.items() if k != "_dialog"}
    lead["summary"] = await _summarize(snap["_dialog"])
    try:                                   # журнал пишем ВСЕГДА, до отправки
        with open(LEADS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("[%s] журнал заявок недоступен: %s", lead["session_id"], e)
    lead["sent"] = await _send_telegram(_render(lead))
    log.info("[%s] заявка: %s / %s / позиций %d / отправлена=%s",
             lead["session_id"], lead["name"] or "—", lead["phone"] or "—",
             len(lead["positions"]), lead["sent"])
    return lead


async def finalize(session_id: str) -> dict[str, Any] | None:
    """Снять и доставить одним вызовом — для ручного прогона и тестов."""
    return await deliver(snapshot(session_id))
