"""Хранилище реальных характеристик товаров, собранных со страниц сайта
(alyansles.ru/catalog/items/...) — фид (YML_URL) даёт только name/description/
price/count, без структурированных полей (влажность, тех.обработка,
поверхность, порода, сорт). Карточка товара их даёт (таблица «Характеристики»).

Наполняется скриптом scrape_product_attrs.py (см. его докстринг — как часто
запускать). Читается catalog.py при сборке строки товара для LLM.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "catalog.db"

# Поля таблицы «Характеристики» на карточке, которые стоит показывать модели —
# ключевые для «не выдумывай обработку/влажность» (баг Артёма 2026-07-24).
# Остальные (бренд, страна, вес) не нужны в диалоге с клиентом.
_RELEVANT_KEYS = {
    "Влажность", "Тех обработка", "Поверхность", "Порода", "Сорт", "Цвет",
}


def ensure_table() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS product_attrs ("
            " id TEXT PRIMARY KEY,"
            " url TEXT,"
            " attrs_json TEXT,"
            " scraped_at TEXT"
            ")"
        )


def save_many(rows: list[dict[str, Any]]) -> None:
    """rows: [{'id':..., 'url':..., 'attrs': {...}, 'scraped_at':...}]"""
    ensure_table()
    with sqlite3.connect(DB_PATH) as con:
        con.executemany(
            "INSERT INTO product_attrs (id, url, attrs_json, scraped_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET url=excluded.url, attrs_json=excluded.attrs_json,"
            " scraped_at=excluded.scraped_at",
            [(r["id"], r.get("url"), json.dumps(r.get("attrs") or {}, ensure_ascii=False), r.get("scraped_at"))
             for r in rows],
        )


def get(product_id: str) -> dict[str, Any] | None:
    ensure_table()
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM product_attrs WHERE id=?", (product_id,)).fetchone()
    if not row:
        return None
    return {"url": row["url"], "attrs": json.loads(row["attrs_json"] or "{}"), "scraped_at": row["scraped_at"]}


def relevant_line(attrs: dict[str, str]) -> str:
    """Короткая строка для контекста LLM из значимых полей характеристик."""
    parts = [f"{k}: {attrs[k]}" for k in _RELEVANT_KEYS if attrs.get(k)]
    return ", ".join(parts)


def count() -> int:
    ensure_table()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT COUNT(*) FROM product_attrs").fetchone()[0]
