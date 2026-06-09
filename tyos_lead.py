"""Сборка и доставка готовой заявки от Тёса.

Каналы доставки (sinks):
  1. ВСЕГДА — журнал leads.jsonl + лог. Надёжный путь, работает без настройки.
  2. MAX (если задан MAX_BOT_TOKEN + MAX_LEAD_CHAT_ID) — отправка менеджеру/Свете
     через нашу же библиотеку maxapi. На тест получатель — MAX Светланы.
  3. Дубль (почта/Битрикс/таблица) — ждём канал от Артёма, заложен как заглушка.

Заявка собирается из контакта (от клиента) + позиций и расчётов, которые Тёс
накопил за диалог (их кладёт brain в session). Цифры — из catalog.compute_total,
не из модели.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("tyos.lead")

ROOT = Path(__file__).resolve().parent
LEADS_PATH = ROOT / "leads.jsonl"

MSK = timezone(timedelta(hours=3))


def _now_msk_iso() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M МСК")


def build_lead(*, contact: dict[str, Any], items: list[dict[str, Any]],
               session_id: str) -> dict[str, Any]:
    """Собрать пакет заявки. contact = {name, contact, preferred_time, note}.

    items — накопленные позиции вида {"product": <row>, "calc": <compute_total|None>}.
    """
    positions: list[dict[str, Any]] = []
    grand_total = 0
    for it in items:
        p = it.get("product") or {}
        calc = it.get("calc")
        pos = {
            "name": p.get("name"),
            "price": int(p["price"]) if p.get("price") is not None else None,
        }
        if calc:
            pos["qty"] = f'{calc["n"]} {calc["unit"]}'
            pos["sum"] = calc["total"]
            pos["how"] = calc["how"]
            grand_total += int(calc["total"])
        positions.append(pos)

    return {
        "ts": _now_msk_iso(),
        "session_id": session_id,
        "name": contact.get("name"),
        "contact": contact.get("contact"),
        "preferred_time": contact.get("preferred_time"),
        "delivery": contact.get("delivery"),
        "note": contact.get("note"),
        "positions": positions,
        "total": grand_total or None,
    }


def render_for_manager(lead: dict[str, Any]) -> str:
    """Человекочитаемая заявка для менеджера (MAX/лог)."""
    lines = ["🪵 Новая заявка с сайта — ИИ-консультант Тёс", ""]
    if lead.get("name"):
        lines.append(f"Имя: {lead['name']}")
    lines.append(f"Контакт: {lead.get('contact') or '—'}")
    if lead.get("preferred_time"):
        lines.append(f"Удобное время: {lead['preferred_time']}")
    if lead.get("delivery"):
        lines.append(f"Доставка: {lead['delivery']}")
    if lead.get("deadline"):
        lines.append(f"Срок: {lead['deadline']}")
    if lead.get("services"):
        lines.append("Обработка: " + ", ".join(lead["services"]) + " (стоимость рассчитать)")
    if lead.get("note"):
        lines.append(f"Запрос: {lead['note']}")
    if lead.get("positions"):
        lines.append("")
        lines.append("Позиции:")
        for p in lead["positions"]:
            row = f"• {p.get('name') or '—'}"
            if p.get("how"):
                row += f" — {p['how']}"
            elif p.get("price"):
                row += f" — {p['price']} ₽"
            lines.append(row)
    if lead.get("total"):
        lines.append("")
        lines.append(f"Итого ≈ {lead['total']:,} ₽".replace(",", " "))
    lines.append("")
    lines.append(f"({lead.get('ts')} · сессия {lead.get('session_id')})")
    return "\n".join(lines)


def _persist(lead: dict[str, Any]) -> None:
    """Надёжный sink: дописать заявку в leads.jsonl."""
    try:
        with open(LEADS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead, ensure_ascii=False) + "\n")
    except Exception as e:  # журнал не должен ронять ответ клиенту
        log.exception("lead persist failed: %s", e)


async def _send_max(text: str) -> bool:
    """MAX-sink. Включается, только если заданы MAX_BOT_TOKEN и получатель.

    Получатель: MAX_LEAD_USER_ID (id человека — на тест = Светлана, потом менеджер)
    или MAX_LEAD_CHAT_ID (id группового чата). Использует maxapi (как наши MAX-боты).
    """
    token = (os.environ.get("MAX_BOT_TOKEN") or "").strip()
    user_id = (os.environ.get("MAX_LEAD_USER_ID") or "").strip()
    chat_id = (os.environ.get("MAX_LEAD_CHAT_ID") or "").strip()
    if not (token and (user_id or chat_id)):
        return False
    try:
        from maxapi import Bot  # импорт здесь — чтобы каркас работал и без maxapi
        bot = Bot(token)
        if user_id:
            await bot.send_message(user_id=int(user_id), text=text)
        else:
            await bot.send_message(chat_id=int(chat_id), text=text)
        sess = getattr(bot, "session", None)
        if sess and hasattr(sess, "close"):
            await sess.close()
        return True
    except Exception as e:
        log.exception("MAX send failed: %s", e)
        return False


async def deliver(lead: dict[str, Any]) -> dict[str, Any]:
    """Доставить заявку во все доступные каналы. Возвращает статус по каналам."""
    text = render_for_manager(lead)
    _persist(lead)
    log.info("LEAD:\n%s", text)
    max_ok = await _send_max(text)
    return {"persisted": True, "max": max_ok, "logged": True}
