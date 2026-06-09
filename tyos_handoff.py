"""Передача расчёта с сайта в MAX-бот через токен.

Когда клиент на сайте выбирает «продолжить в MAX», его собранный расчёт
сохраняется под коротким токеном. MAX-бот по этому токену (?start=<token>)
поднимает расчёт и продолжает диалог с тем же контекстом + кнопкой
«Передать заказ менеджеру».

Хранилище — простой JSON-файл (низкая нагрузка теста). Когда появится MAX-бот
Азбуки, он читает расчёт через get(token) или HTTP /handoff/<token>.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("tyos.handoff")

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "handoffs.json"
MSK = timezone(timedelta(hours=3))


def _load() -> dict[str, Any]:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(d: dict[str, Any]) -> None:
    try:
        STORE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("handoff store write failed: %s", e)


def save(packet: dict[str, Any]) -> str:
    """Сохранить расчёт, вернуть короткий токен для deep-link."""
    token = uuid.uuid4().hex[:10]
    d = _load()
    d[token] = {"ts": datetime.now(MSK).isoformat(timespec="seconds"), **packet}
    _save_all(d)
    return token


def get(token: str) -> dict[str, Any] | None:
    return _load().get(token)
