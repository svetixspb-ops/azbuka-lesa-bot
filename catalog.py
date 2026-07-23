"""Поиск по каталогу Азбуки Леса (SQLite).

LLM передаёт сюда структурированные параметры, мы возвращаем top-N товаров.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "catalog.db"


def _to_num(v: Any) -> float | None:
    """Достать число из int/float/строки («10 штук»→10, «2,5 куба»→2.5). None если нет."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    import re
    m = re.search(r"\d+(?:[.,]\d+)?", str(v))
    return float(m.group().replace(",", ".")) if m else None


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def get_yml_date() -> str:
    with _connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key='yml_date'").fetchone()
        return row["value"] if row else ""


# Слова-«модификаторы», которые в названиях товаров часто отсутствуют
# (они в категориях). Если запрос содержит только их + общее слово — отрубаем их.
# ВАЖНО: «строганная/строганый» и «сухая/сухой» реально есть в названиях товаров
# (проверено в catalog.db) и различают товары (напр. «доска сухая строганная» vs
# «доска сухая каркасная» — разные позиции с разной ценой). Раньше они были в этом
# списке и отбрасывались как шум — из-за этого поиск подменял товар молча (2026-07-23).
_GENERIC_MODIFIERS = {"обрезная", "обрезной", "необрезная"}


def _tokens(text: str) -> list[str]:
    """Понизим регистр и разобьём по пробелам — для case-insensitive Cyrillic LIKE."""
    return [t for t in text.lower().split() if t and len(t) >= 3]


_VOWELS = "аеёиоуыэюя"


def _stem_token(t: str) -> str:
    """Грубый стемминг под падежи/формы: «планки»/«планка»→«планк» (ловит «планкен»),
    «доски»→«доск». Отбрасываем хвостовую гласную у слов длиннее 4 символов."""
    return t[:-1] if len(t) > 4 and t[-1] in _VOWELS else t


def _do_search(
    sql_parts: list[str],
    params: list[Any],
    limit: int,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM products WHERE " + " AND ".join(sql_parts) + " ORDER BY count DESC LIMIT ?"
    params = params + [limit]
    with _connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def search(
    text: str | None = None,
    thickness_mm: float | None = None,
    width_mm: float | None = None,
    length_mm: float | None = None,
    diameter_mm: float | None = None,
    species: str | None = None,
    available_only: bool = True,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Найти товары с graceful fallback: сначала строгий поиск, если 0 — ослабляем
    фильтры по очереди (порода → длина → дополнительные слова).
    """
    base: list[str] = ["1=1"]
    base_params: list[Any] = []
    if available_only:
        base.append("available=1 AND count > 0")

    tokens = _tokens(text) if text else []
    # выкидываем общие модификаторы — их в названиях обычно нет
    primary = [t for t in tokens if t not in _GENERIC_MODIFIERS]

    has_dims = thickness_mm is not None or width_mm is not None or diameter_mm is not None
    has_filter = has_dims or species or primary

    def attempt(strict_species: bool, strict_length: bool, strict_tokens: bool,
                dim_tol: float = 0.0) -> list[dict[str, Any]]:
        sql_parts = list(base)
        params: list[Any] = list(base_params)

        def add_dim(col: str, val: float) -> None:
            if dim_tol > 0:  # допуск для приблизительных размеров («около 15»)
                sql_parts.append(f"{col} BETWEEN ? AND ?")
                params.extend([val * (1 - dim_tol), val * (1 + dim_tol)])
            else:
                sql_parts.append(f"{col} = ?"); params.append(val)

        # Толщина+ширина сравниваются ПОРЯДОК-НЕЗАВИСИМО: клиент/STT мог сказать «12 на 45»
        # вместо «45 на 12». Каталог хранит размер в фикс-порядке, поэтому матчим обе ориентации.
        if thickness_mm is not None and width_mm is not None:
            t, w = float(thickness_mm), float(width_mm)
            if dim_tol > 0:
                sql_parts.append(
                    "((thickness_mm BETWEEN ? AND ? AND width_mm BETWEEN ? AND ?) "
                    "OR (thickness_mm BETWEEN ? AND ? AND width_mm BETWEEN ? AND ?))")
                params.extend([t * (1 - dim_tol), t * (1 + dim_tol), w * (1 - dim_tol), w * (1 + dim_tol),
                               w * (1 - dim_tol), w * (1 + dim_tol), t * (1 - dim_tol), t * (1 + dim_tol)])
            else:
                sql_parts.append("((thickness_mm = ? AND width_mm = ?) OR (thickness_mm = ? AND width_mm = ?))")
                params.extend([t, w, w, t])
        elif thickness_mm is not None:
            add_dim("thickness_mm", float(thickness_mm))
        elif width_mm is not None:
            add_dim("width_mm", float(width_mm))
        if diameter_mm is not None:
            add_dim("diameter_mm", float(diameter_mm))
        if length_mm is not None and strict_length:
            sql_parts.append("length_mm = ?"); params.append(float(length_mm))
        if species and strict_species:
            sql_parts.append("species = ?"); params.append(species.lower())
        if strict_tokens and primary:
            for t in primary:
                sql_parts.append("name_lower LIKE ?"); params.append(f"%{_stem_token(t)}%")
        return _do_search(sql_parts, params, limit)

    # Если вообще никаких фильтров — ничего не возвращаем (не сыпем рандом).
    if not has_filter:
        return []

    # Лесенка от строгого к мягкому. Минимум 1 фильтр всегда сохраняется,
    # чтобы не возвращать случайные товары.
    attempts: list[tuple[bool, bool, bool]] = [(True, True, True)]
    if species:
        attempts.append((False, True, True))  # снимаем породу
    if length_mm is not None and (has_dims or primary):
        attempts.append((False, False, True))  # снимаем длину
    if primary and has_dims:
        attempts.append((False, False, False))  # только размеры
    elif primary and not has_dims:
        # есть только текст — никогда не дропаем токены, иначе вернём мусор
        pass

    seen = set()
    for flags in attempts:
        if flags in seen:
            continue
        seen.add(flags)
        results = attempt(*flags)
        if results:
            return results
    # Допуск по размерам: «около 15 см» ловит 16/18 мм и т.п., когда точного совпадения нет.
    if has_dims:
        for flags in [(False, True, True), (False, False, True), (False, False, False)]:
            results = attempt(*flags, dim_tol=0.25)
            if results:
                return results
    return []


def search_loose(text: str, limit: int = 8) -> list[dict[str, Any]]:
    """Свободный поиск без размеров: токены из запроса через LIKE."""
    tokens = [t for t in _tokens(text) if t not in _GENERIC_MODIFIERS]
    if not tokens:
        return []
    sql_parts = ["available=1 AND count > 0"]
    params: list[Any] = []
    for t in tokens:
        sql_parts.append("name_lower LIKE ?")
        params.append(f"%{_stem_token(t)}%")
    return _do_search(sql_parts, params, limit)


def piece_volume_m3(thickness_mm: float | None, width_mm: float | None, length_mm: float | None,
                    diameter_mm: float | None = None) -> float | None:
    """Объём одной штуки в м³ (для расчёта кубатуры).

    Круглые товары (столб/бревно с диаметром) — объём цилиндра π·r²·длина.
    Прямоугольные (доска/брус) — толщина × ширина × длина.
    """
    import math
    if diameter_mm and length_mm:
        r = (diameter_mm / 1000) / 2
        return math.pi * r * r * (length_mm / 1000)
    if not (thickness_mm and width_mm and length_mm):
        return None
    return (thickness_mm / 1000) * (width_mm / 1000) * (length_mm / 1000)


def pieces_for_volume(thickness_mm: float, width_mm: float, length_mm: float, target_m3: float) -> int:
    """Сколько штук нужно для целевого объёма (округление вверх)."""
    v1 = piece_volume_m3(thickness_mm, width_mm, length_mm)
    if not v1:
        return 0
    import math
    return max(1, math.ceil(target_m3 / v1))


def format_product_line(p: dict[str, Any]) -> str:
    """Однострочка для контекста LLM: имя, цена, остаток, объём."""
    parts = [p["name"], f"{int(p['price'])} ₽"]
    if p.get("pack_count"):
        parts.append(f"уп. {p['pack_count']} шт")
    if p["count"]:
        parts.append(f"в наличии {p['count']}")
    v = piece_volume_m3(p.get("thickness_mm"), p.get("width_mm"), p.get("length_mm"), p.get("diameter_mm"))
    if v:
        parts.append(f"объём 1 шт = {v:.4f} м³")
    return " · ".join(parts)


def compute_total(p: dict[str, Any], it: dict[str, Any]) -> dict[str, Any] | None:
    """Детерминированный расчёт количества и итоговой суммы по одной позиции.

    LLM ошибается в округлении/умножении, поэтому считаем сами. Возвращает
    {"how": "<человекочитаемая выкладка с суммой>", "total": int} или None,
    если количество не задано (тогда LLM просто уточнит).
    """
    import math

    price = int(p["price"])
    pack = p.get("pack_count")
    v1 = piece_volume_m3(p.get("thickness_mm"), p.get("width_mm"), p.get("length_mm"), p.get("diameter_mm"))
    # LLM иногда отдаёт количество строкой («10 штук», «два куба») — вытаскиваем число.
    target = _to_num(it.get("target_m3"))
    qty = _to_num(it.get("quantity_pieces"))
    packs = _to_num(it.get("packs"))

    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    if pack:  # цена указана за упаковку из pack шт
        if packs:
            n = int(packs)
        elif qty:
            n = math.ceil(float(qty) / pack)
        elif target and v1:
            pieces = math.ceil(float(target) / v1)
            n = math.ceil(pieces / pack)
        else:
            return None
        total = n * price
        pieces = int(n * pack)
        return {"total": total, "n": int(n), "unit": "уп", "pack": int(pack), "pieces": pieces,
                "price": price, "how": f"{n} уп × {fmt(price)} = {fmt(total)} ₽ (по {pack} шт в упаковке)"}

    # цена за штуку
    if qty:
        n = int(qty)
    elif target and v1:
        n = math.ceil(float(target) / v1)
    else:
        return None
    total = n * price
    return {"total": total, "n": int(n), "unit": "шт", "pack": None, "pieces": int(n),
            "price": price, "how": f"{n} шт × {fmt(price)} = {fmt(total)} ₽"}
