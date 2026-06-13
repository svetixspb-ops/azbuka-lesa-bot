"""Серверный объект заказа Тёса — единый источник правды по составу и суммам.

Реализует спецификацию «целостность заказа» (Клод): состав и арифметику держит
КОД, модель только наполняет заказ через функции (tool calls) и озвучивает готовые
числа. Это убирает два бага мультизаказов: потерю позиций и ошибки сумм.

Состав:
  order = {
    "items":   { key: {name, unit_price, qty, line_total, volume_m3_each, stock} },
    "services":[ {type, scope, note} ],
    "delivery":{ method, address, note } | None,
    "deadline": str | None,
    "subtotal_materials": int, "total_estimate": int,
  }
Позиции находятся по каталогу (как и в остальном боте) — модель передаёт описание
товара, код резолвит его в реальную позицию из наличия.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Any

import catalog

_MSK = timezone(timedelta(hours=3))


def new_order() -> dict[str, Any]:
    return {"items": {}, "services": [], "delivery": None, "deadline": None,
            "subtotal_materials": 0, "total_estimate": 0}


def _recompute(order: dict[str, Any]) -> None:
    # line_total считается pack-aware в add_or_update_item (через catalog.compute_total),
    # здесь только суммируем — НЕ пересчитываем unit_price×qty (это давало ×pack для упаковок).
    sub = 0
    for it in order["items"].values():
        sub += int(it["line_total"])
    order["subtotal_materials"] = sub
    # услуги пока без известных цен → в сумму не входят, идут строкой с пометкой
    order["total_estimate"] = sub


def _line_calc(it: dict[str, Any]) -> str:
    """Человекочитаемая выкладка по позиции (pack-aware). Берём готовую из compute_total."""
    if it.get("how"):
        return it["how"]
    return f"{it['qty']} шт × {it['unit_price']} ₽ = {it['line_total']:,} ₽".replace(",", " ")


def _find_product(query: str, thickness=None, width=None, length=None,
                  species=None) -> dict[str, Any] | None:
    res = catalog.search(text=query, thickness_mm=thickness, width_mm=width,
                         length_mm=length, species=species, limit=1)
    if res:
        return res[0]
    res = catalog.search_loose(query, limit=1)
    return res[0] if res else None


def add_or_update_item(order: dict[str, Any], query: str, qty: float | None = None,
                       target_m3: float | None = None, packs: float | None = None) -> str:
    """Добавить/обновить позицию. Количество: qty — штук, packs — упаковок,
    target_m3 — нужный объём. Для товаров с упаковкой (pack_count) цена в каталоге
    указана ЗА УПАКОВКУ — расчёт делает catalog.compute_total (pack-aware),
    поэтому неважно, в штуках или упаковках задал клиент, ×pack не задвоится."""
    p = _find_product(query)
    if not p:
        return f"NOT_FOUND: «{query}» — в каталоге не нашёл, предложи аналог или изготовление."
    v1 = catalog.piece_volume_m3(p.get("thickness_mm"), p.get("width_mm"),
                                 p.get("length_mm"), p.get("diameter_mm"))
    # единый детерминированный расчёт (учитывает упаковки и цену за упаковку)
    res = catalog.compute_total(p, {"quantity_pieces": qty, "packs": packs, "target_m3": target_m3})
    if res is None:
        return f"NEED_QTY: для «{p['name']}» уточни количество (штук, упаковок или объём)."
    key = p["name"]
    order["items"][key] = {
        "name": p["name"], "unit_price": int(res["price"]),
        "n": int(res["n"]), "unit": res["unit"], "pack": res["pack"],
        "pieces": int(res["pieces"]), "qty": int(res["pieces"]),
        "line_total": int(res["total"]), "how": res["how"],
        "volume_m3_each": round(v1, 4) if v1 else None, "stock": p.get("count"),
        "length_m": (p.get("length_mm") or 0) / 1000 or None,
    }
    _recompute(order)
    short = "по объёму" if (target_m3 and not qty and not packs) else ""
    return f"OK: {p['name']} — {res['how']} {short}".strip()


def remove_item(order: dict[str, Any], query: str) -> str:
    ql = query.lower()
    for key in list(order["items"]):
        if ql in key.lower() or all(w in key.lower() for w in ql.split() if len(w) > 3):
            order["items"].pop(key)
            _recompute(order)
            return f"REMOVED: {key}"
    return f"NOT_IN_ORDER: «{query}» не было в заказе."


def set_service(order: dict[str, Any], type_: str, scope: str | None = None) -> str:
    for s in order["services"]:
        if s["type"].lower() == type_.lower():
            s["scope"] = scope or s.get("scope")
            return f"OK_SERVICE: {type_}"
    order["services"].append({"type": type_, "scope": scope,
                              "note": "стоимость рассчитает менеджер"})
    return f"OK_SERVICE: {type_}"


def set_delivery(order: dict[str, Any], method: str, address: str | None = None) -> str:
    order["delivery"] = {"method": method, "address": address,
                         "note": "стоимость уточнит менеджер" if method != "самовывоз" else None}
    return f"OK_DELIVERY: {method}"


def set_deadline(order: dict[str, Any], text: str) -> str:
    order["deadline"] = text
    return f"OK_DEADLINE: {text}"


def render_state(order: dict[str, Any]) -> str:
    """Краткий снимок заказа для контекста модели (она его озвучивает)."""
    if not order["items"] and not order["services"]:
        return "ЗАКАЗ ПУСТ."
    lines = ["ТЕКУЩИЙ ЗАКАЗ (числа точные, считает код — озвучивай их, не пересчитывай):"]
    for it in order["items"].values():
        s = "— " + it["name"] + ": " + _line_calc(it)
        if it.get("stock") is not None:
            s += f" (в наличии {it['stock']})"
        lines.append(s)
    for sv in order["services"]:
        lines.append(f"— услуга: {sv['type']}{' (' + sv['scope'] + ')' if sv.get('scope') else ''} — стоимость рассчитает менеджер")
    if order.get("delivery"):
        d = order["delivery"]
        lines.append(f"— получение: {d['method']}{', ' + d['address'] if d.get('address') else ''}"
                     + (f" — {d['note']}" if d.get("note") else ""))
    if order.get("deadline"):
        lines.append(f"— срок: {order['deadline']}")
    lines.append(f"ИТОГО предварительно: {order['total_estimate']:,} ₽ (точную подтвердит менеджер)".replace(",", " "))
    return "\n".join(lines)


def render_summary(order: dict[str, Any]) -> str:
    """Полный снимок-резюме для фиксации заказа (правило 11)."""
    if not order["items"]:
        return "Готов сохранить ваш расчёт. Как удобнее продолжить?"
    lines = ["Фиксирую заказ:"]
    for it in order["items"].values():
        lines.append(f"• {it['name']} — " + _line_calc(it))
    for sv in order["services"]:
        lines.append(f"• обработка: {sv['type']}{' (' + sv['scope'] + ')' if sv.get('scope') else ''} — стоимость рассчитает менеджер")
    if order.get("delivery"):
        d = order["delivery"]
        lines.append(f"• получение: {d['method']}{', ' + d['address'] if d.get('address') else ''}"
                     + (f" — {d['note']}" if d.get("note") else ""))
    if order.get("deadline"):
        lines.append(f"• срок: {order['deadline']}")
    lines.append("")
    lines.append(f"Итого предварительно: {order['total_estimate']:,} ₽".replace(",", " ")
                 + " — точную сумму и стоимость обработки подтвердит менеджер.")
    lines.append("")
    lines.append("Забронирую материал за вами на 2–3 дня по текущему наличию — "
                 "менеджер подтвердит бронь и поможет с доставкой. "
                 "Оставите телефон или продолжим в MAX?")
    return "\n".join(lines)


def has_pilomat(order: dict[str, Any]) -> bool:
    kw = ("доск", "брус", "вагонк", "имитац", "планкен", "рейк", "блок", "балк")
    names = " ".join(order["items"]).lower()
    return any(k in names for k in kw)


def to_packet(order: dict[str, Any], contact: dict[str, Any] | None = None,
              session_id: str | None = None) -> dict[str, Any]:
    """Собрать пакет заявки из заказа (для менеджера / хэндоффа в MAX)."""
    positions = []
    for it in order["items"].values():
        qty_str = (f"{it['n']} уп ({it['pieces']} шт)" if it.get("pack") else f"{it['qty']} шт")
        positions.append({
            "name": it["name"], "price": it["unit_price"], "qty": qty_str,
            "sum": it["line_total"],
            "how": _line_calc(it),
        })
    d = order.get("delivery") or {}
    delivery_str = None
    if d:
        delivery_str = d.get("method", "")
        if d.get("address"):
            delivery_str += f", {d['address']}"
    contact = contact or {}
    return {
        "ts": datetime.now(_MSK).strftime("%Y-%m-%d %H:%M МСК"),
        "session_id": session_id,
        "name": contact.get("name"),
        "contact": contact.get("contact"),
        "preferred_time": contact.get("preferred_time"),
        "delivery": delivery_str,
        "deadline": order.get("deadline"),
        "services": [f"{s['type']}{' (' + s['scope'] + ')' if s.get('scope') else ''}" for s in order["services"]],
        "note": contact.get("note"),
        "positions": positions,
        "total": order["total_estimate"] or None,
    }


# Схемы функций для модели (OpenAI-совместимый формат tools).
TOOLS = [
    {"type": "function", "function": {
        "name": "add_or_update_item",
        "description": "Добавить позицию в заказ или изменить её количество. Передай описание товара (тип+размеры) и количество ОДНИМ из способов: qty — штук, packs — упаковок, target_m3 — нужный объём в м³. ВАЖНО: НЕ перемножай сам — сумму считает код. Для товаров в упаковках цена в каталоге за упаковку; если клиент сказал «N упаковок» — передай packs=N (а не qty), если «N штук» — qty=N, код сам разберётся. Если клиент задал количество оптом для нескольких ранее названных позиций («по 100», «тоже по 100») — вызови функцию для КАЖДОЙ позиции отдельно.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "товар с размерами, напр. «брус сухой строганный 20х45х3000» или «террасная доска лиственница 28х145х6000 палубная»"},
            "qty": {"type": "integer", "description": "количество штук"},
            "packs": {"type": "integer", "description": "количество упаковок (для товаров, продающихся упаковками)"},
            "target_m3": {"type": "number", "description": "нужный объём в м³ (если задан кубами)"},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "remove_item",
        "description": "Убрать позицию из заказа (клиент отказался от неё).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "какую позицию убрать (название/размеры)"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "set_service",
        "description": "Добавить услугу обработки к заказу (пропитка/антисептик/огнезащита/торцовка/строжка/распил/покраска).",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "description": "вид обработки"},
            "scope": {"type": "string", "description": "объём обработки, напр. «весь объём»"}}, "required": ["type"]}}},
    {"type": "function", "function": {
        "name": "set_delivery",
        "description": "Указать способ получения: самовывоз или доставка + адрес/населённый пункт.",
        "parameters": {"type": "object", "properties": {
            "method": {"type": "string", "enum": ["самовывоз", "доставка"]},
            "address": {"type": "string"}}, "required": ["method"]}}},
    {"type": "function", "function": {
        "name": "set_deadline",
        "description": "Зафиксировать срок, к которому клиенту нужен заказ.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
]


def execute(order: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    """Выполнить вызов функции от модели, вернуть короткий результат-строку."""
    try:
        if name == "add_or_update_item":
            return add_or_update_item(order, args.get("query", ""), args.get("qty"),
                                      args.get("target_m3"), args.get("packs"))
        if name == "remove_item":
            return remove_item(order, args.get("query", ""))
        if name == "set_service":
            return set_service(order, args.get("type", ""), args.get("scope"))
        if name == "set_delivery":
            return set_delivery(order, args.get("method", ""), args.get("address"))
        if name == "set_deadline":
            return set_deadline(order, args.get("text", ""))
    except Exception as e:  # noqa
        return f"ERROR: {e}"
    return f"UNKNOWN_TOOL: {name}"
