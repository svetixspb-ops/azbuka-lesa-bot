"""YML → SQLite loader для каталога Азбуки Леса.

YML — стандартный фид Битрикса для Яндекс.Маркета. Содержит:
- ~1567 товаров на момент 2026-05-27
- price (за упаковку или штуку — зависит от категории)
- count (остаток)
- name, description, categoryId

Парсер выдирает из name размеры (AxBxC мм), сорт, размер упаковки —
этой структуры достаточно чтобы LLM мог сопоставить запрос «доска 50х150х6м, 5 кубов».

Запускать: `python data_loader.py` (по умолчанию из YML_URL),
            или `python data_loader.py /path/to/catalog.xml` (локальный файл).
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "catalog.db"
DEFAULT_YML_URL = "https://www.alyansles.ru/bitrix/catalog_export/export_kTn.xml"

# Размеры в названии: «16х96х1000», «50×150×6000», «150x150x6», «100х100х3100мм».
# Дефис/x/х/× как разделители. Длина может быть в мм (4 цифры) или метрах (1 цифра).
DIM_RE = re.compile(
    r"(?<![\d,.])(\d{1,4}(?:[.,]\d)?)\s*[xх×]\s*(\d{1,4}(?:[.,]\d)?)"
    r"(?:\s*[xх×]\s*(\d{1,4}(?:[.,]\d)?))?",
    re.IGNORECASE,
)
# Длина в формате «Д = 6 м», «Д=5,7 м», «Д=6м» — отдельно от размеров AxB
# (имитация, вагонка, шпунт.доска часто пишут длину так). (?!м) чтобы не цеплять «мм».
LEN_RE = re.compile(r"д\s*=?\s*(\d{1,2}(?:[.,]\d)?)\s*м(?!м)", re.IGNORECASE)
# Круглые товары (столбы, брёвна, опоры): диаметр «D=100мм», «Ø100мм», «диаметр 100 мм».
# Latin D (как в фиде Битрикса) ловится через IGNORECASE. Без AxB-размеров.
DIAM_RE = re.compile(r"(?:d\s*=\s*|ø\s*|диаметр\w*\s+)(\d{2,3})\s*мм", re.IGNORECASE)
# Длина круглых: «L=3м», «L=3.4м», «L=3,7 м» (Latin L). (?!м) чтобы не цеплять «мм».
CYL_LEN_RE = re.compile(r"l\s*=\s*(\d{1,2}(?:[.,]\d)?)\s*м(?!м)", re.IGNORECASE)
PACK_RE = re.compile(r"\((\d+)\s*шт(?:/уп)?\)", re.IGNORECASE)
# Сорт в кавычках: "А", "АБ", "1/4", «3/3» — берём то, что между кавычками
SORT_RE = re.compile(r'["«]([А-Яа-яA-Za-z0-9/\\\- ]{1,8})[»"]')
# Категория «(осина)», «(хвоя)», «(берёза)» — порода в скобках
SPECIES_RE = re.compile(r"\((осина|хвоя|сосна|ель|берёз[аы]|ольха|липа|дуб|лиственниц[аы])\)", re.IGNORECASE)
# Порода СЛОВОМ в названии (без скобок): «Террасная доска Лиственница АВ», «доска Хвоя».
# Каждый паттерн → каноническое имя породы (как в EXTRACT_QUERY_PROMPT).
SPECIES_WORD_PATTERNS = [
    ("лиственница", re.compile(r"лиственниц", re.IGNORECASE)),
    ("осина", re.compile(r"осин(?:а|ы|ов)", re.IGNORECASE)),
    ("берёза", re.compile(r"бер[её]з", re.IGNORECASE)),
    ("ольха", re.compile(r"ольх", re.IGNORECASE)),
    ("липа", re.compile(r"\bлип[аыо]", re.IGNORECASE)),
    ("дуб", re.compile(r"\bдуб\b", re.IGNORECASE)),
    ("сосна", re.compile(r"\bсосн", re.IGNORECASE)),
    ("ель", re.compile(r"\b(?:ель|елов)", re.IGNORECASE)),
    ("хвоя", re.compile(r"\bхво[яи]", re.IGNORECASE)),  # хвоя = общая (сосна/ель) — ставим последней
]


def parse_name(name: str) -> dict[str, Any]:
    """Вытащить структурированные поля из строки названия."""
    result: dict[str, Any] = {
        "thickness_mm": None,
        "width_mm": None,
        "length_mm": None,
        "diameter_mm": None,
        "pack_count": None,
        "sort": None,
        "species": None,
    }

    m = DIM_RE.search(name)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        def to_float(s: str) -> float:
            return float(s.replace(",", "."))
        # Толщина и ширина в пиломатериалах — ВСЕГДА мм (5..300мм диапазон),
        # никогда не метры. Перевод метров → мм допустим ТОЛЬКО для длины
        # (третьей размерности), где встречается «150х150х6» = 6 метров = 6000 мм.
        if a and b:
            result["thickness_mm"] = to_float(a)
            result["width_mm"] = to_float(b)
        if c:
            v = to_float(c)
            result["length_mm"] = v * 1000 if 1 <= v <= 50 else v

    # Длина не попала в AxBxC (формат «Д = 6 м») — восстановим отдельно.
    if result["length_mm"] is None:
        m_len = LEN_RE.search(name)
        if m_len:
            result["length_mm"] = float(m_len.group(1).replace(",", ".")) * 1000

    # Круглые товары (столб/бревно/опора): нет прямоугольных размеров → ищем диаметр + L=.
    # Так появляется объём цилиндра для расчёта доставки (π·r²·длина).
    if result["thickness_mm"] is None and result["width_mm"] is None:
        m_d = DIAM_RE.search(name)
        if m_d:
            result["diameter_mm"] = float(m_d.group(1))
        if result["length_mm"] is None:
            m_cl = CYL_LEN_RE.search(name)
            if m_cl:
                result["length_mm"] = float(m_cl.group(1).replace(",", ".")) * 1000

    m = PACK_RE.search(name)
    if m:
        result["pack_count"] = int(m.group(1))

    # Евровагонка продаётся упаковками по 10 шт, но «(10шт/уп)» в названии НЕТ
    # (цена в прайсе — за упаковку 10 шт; подтвердил Артём через Sveta 2026-05-28).
    if result["pack_count"] is None and "евровагонка" in name.lower():
        result["pack_count"] = 10

    m = SORT_RE.search(name)
    if m:
        result["sort"] = m.group(1).strip()

    m = SPECIES_RE.search(name)
    if m:
        result["species"] = m.group(1).lower()
    else:
        # Порода словом в названии (без скобок) — перебираем по приоритету (хвоя — общая, последней).
        for canon, pat in SPECIES_WORD_PATTERNS:
            if pat.search(name):
                result["species"] = canon
                break

    return result


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            parent_id TEXT,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            available INTEGER NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL,
            category_id TEXT,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,  -- предварительно lowercased для case-insensitive LIKE (SQLite LOWER() не работает с кириллицей без ICU)
            description TEXT,
            count INTEGER,
            thickness_mm REAL,
            width_mm REAL,
            length_mm REAL,
            diameter_mm REAL,
            pack_count INTEGER,
            sort TEXT,
            species TEXT,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );
        CREATE INDEX IF NOT EXISTS idx_products_dims ON products(thickness_mm, width_mm, length_mm);
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
        CREATE INDEX IF NOT EXISTS idx_products_species ON products(species);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def fetch_yml(url: str | None = None) -> bytes:
    target = url or os.environ.get("YML_URL", DEFAULT_YML_URL)
    with httpx.Client(timeout=30, follow_redirects=True) as cx:
        r = cx.get(target)
        r.raise_for_status()
    return r.content


def load_into_db(xml_bytes: bytes, db_path: Path = DB_PATH) -> dict[str, int]:
    root = ET.fromstring(xml_bytes)
    shop = root.find("shop")
    yml_date = root.get("date", "")

    if db_path.exists():
        db_path.unlink()  # фуллресет — каталог небольшой, итоги атомарны
    con = sqlite3.connect(db_path)
    try:
        init_db(con)
        cats = shop.find("categories") or []
        cat_rows = [(c.get("id"), c.get("parentId"), (c.text or "").strip()) for c in cats]
        con.executemany(
            "INSERT OR REPLACE INTO categories(id, parent_id, name) VALUES (?, ?, ?)",
            cat_rows,
        )

        offers = shop.find("offers") if shop.find("offers") is not None else shop
        prod_rows = []
        for o in offers.findall("offer"):
            name = (o.findtext("name") or "").strip()
            parsed = parse_name(name)
            prod_rows.append((
                o.get("id"),
                1 if o.get("available", "true").lower() == "true" else 0,
                float(o.findtext("price") or 0),
                (o.findtext("currencyId") or "RUR").strip(),
                (o.findtext("categoryId") or "").strip() or None,
                name,
                name.lower(),
                (o.findtext("description") or "").strip(),
                int(o.findtext("count") or 0),
                parsed["thickness_mm"],
                parsed["width_mm"],
                parsed["length_mm"],
                parsed["diameter_mm"],
                parsed["pack_count"],
                parsed["sort"],
                parsed["species"],
            ))
        con.executemany(
            "INSERT OR REPLACE INTO products(id, available, price, currency, "
            "category_id, name, name_lower, description, count, thickness_mm, width_mm, "
            "length_mm, diameter_mm, pack_count, sort, species) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            prod_rows,
        )
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", ("yml_date", yml_date))
        con.commit()
        return {"categories": len(cat_rows), "products": len(prod_rows)}
    finally:
        con.close()


def main() -> None:
    load_dotenv(ROOT / ".env")
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        xml_bytes = Path(sys.argv[1]).read_bytes()
        source = sys.argv[1]
    else:
        url = os.environ.get("YML_URL", DEFAULT_YML_URL)
        xml_bytes = fetch_yml(url)
        source = url
    print(f"Loaded {len(xml_bytes)} bytes from {source}")
    stats = load_into_db(xml_bytes)
    print(f"DB written: categories={stats['categories']}, products={stats['products']} → {DB_PATH}")


if __name__ == "__main__":
    main()
