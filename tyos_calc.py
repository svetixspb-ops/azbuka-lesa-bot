"""Детерминированный расчёт количества материала — считает КОД, не нейросеть.

Разбор диалогов 17–18.10.2026: на запрос «пристройка 5×4, брус 150×180» Бука написала
«по каждой стене: 5 м — один брус, 4 м — один брус, итого 2 бруса в ряду» — учла две
стены из четырёх и выдала 30 штук вместо примерно 60. Клиент заказал бы половину
нужного. В расчёте обшивки бани — 49 × 456 = 22 344 ₽, а в ответе «около 20 000».

Поэтому геометрия и арифметика уезжают в код, как это уже сделано для цен: модель
получает готовые числа в блоке РАСЧЁТ и только пересказывает их словами.

Ничего не угадываем молча: все допущения (высота стен, запас на подрезку) выписаны
в блок открытым текстом, чтобы Бука переспросила, а не выдала их за данные клиента.
"""
from __future__ import annotations

import math
import re
from typing import Any

# Что строим — по этим словам понимаем, что числа в реплике это помещение, а не сечение.
_WALLS_WORDS = ("дом", "баня", "бани", "баню", "пристро", "сруб", "беседк",
                "гараж", "сарай", "хозблок", "бытовк", "каркас дома")
_CLAD_WORDS = ("обшив", "вагонк", "обшить", "отделк", "имитац", "блок-хаус", "блок хаус",
               "планкен", "стены и потолок")
_DECK_WORDS = ("настил", "пол ", "полы", "полов", "террас", "площадк", "помост", "подиум",
               "перекрыт", "лаги")
_CEILING_WORDS = ("потол", "и потолок", "потолок тоже")

_DEFAULT_WALL_H = 2.7      # высота стен, если не названа — обязательно проговаривается
_DEFAULT_WASTE = 0.10      # запас на подрезку для погонажа/обшивки
_BEAM_WASTE = 0.05
_EPS = 1e-9   # 2,7 / 0,18 = 15.000000000000002 → без допуска ceil даёт лишний ряд

_NUM = r"\d+(?:[.,]\d+)?"
# «5 на 4», «9×12», «220/270», «2,2 x 2,7»
_PAIR_RE = re.compile(rf"({_NUM})\s*(?:на|[xх*×/])\s*({_NUM})", re.IGNORECASE)
_HEIGHT_RE = re.compile(
    rf"(?:высот\w*|потол\w*\s+(?:на\s+)?)\D{{0,12}}?({_NUM})\s*(м|метр\w*|см|мм)?\b",
    re.IGNORECASE)
_LAYERS_RE = re.compile(r"в\s+(два|три|четыре|2|3|4)\s+(слоя|слой|ряда)", re.IGNORECASE)
_WORD_NUM = {"два": 2, "три": 3, "четыре": 4, "2": 2, "3": 3, "4": 4}


def _f(s: str) -> float:
    return float(str(s).replace(",", "."))


def _to_m(v: float) -> float:
    """Размер помещения к метрам: 5 → 5 м, 220 → 2,2 м (см), 5000 → 5 м (мм)."""
    if v >= 1000:
        return v / 1000
    if v >= 100:
        return v / 100
    return v


def _to_mm(v: float) -> float:
    """Размер сечения к миллиметрам: 150 → 150, 15 → 150 (см)."""
    return v * 10 if v < 100 else v


def _rub(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ") + " ₽"


def _m(n: float) -> str:
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _room_pair(text: str) -> tuple[float, float] | None:
    """Первая пара чисел в реплике = габариты помещения."""
    m = _PAIR_RE.search(text)
    if not m:
        return None
    a, b = _to_m(_f(m.group(1))), _to_m(_f(m.group(2)))
    if not (0.8 <= a <= 60 and 0.8 <= b <= 60):
        return None
    return a, b


def _height(text: str) -> tuple[float, bool]:
    """Высота стен и признак «названа клиентом» (иначе допущение)."""
    m = _HEIGHT_RE.search(text)
    if m:
        v, unit = _f(m.group(1)), (m.group(2) or "").lower()
        if unit.startswith("см"):
            v /= 100
        elif unit.startswith("мм"):
            v /= 1000
        elif v >= 100:
            v = _to_m(v)
        if 1.8 <= v <= 6:
            return v, True
    return _DEFAULT_WALL_H, False


def _layers(text: str) -> int:
    m = _LAYERS_RE.search(text)
    return _WORD_NUM.get((m.group(1) if m else "").lower(), 1)


def _dims_mm(item: dict[str, Any] | None, product: dict[str, Any] | None,
             text: str) -> tuple[float | None, float | None, float | None]:
    """Сечение и длина материала (мм): сперва из разобранной позиции, потом из каталога."""
    th = w = ln = None
    for src in (item or {}, product or {}):
        th = th or src.get("thickness_mm")
        w = w or src.get("width_mm")
        ln = ln or src.get("length_mm")
    if not (th and w):
        pairs = _PAIR_RE.findall(text)
        if len(pairs) >= 2:                      # вторая пара — сечение
            th, w = _to_mm(_f(pairs[1][0])), _to_mm(_f(pairs[1][1]))
    return (float(th) if th else None, float(w) if w else None,
            float(ln) if ln else None)


def _task(text: str) -> str | None:
    low = text.lower()
    if any(k in low for k in _CLAD_WORDS):
        return "clad"
    if any(k in low for k in _DECK_WORDS):
        return "deck"
    if any(k in low for k in _WALLS_WORDS):
        return "walls"
    return None


def build(text: str, *, item: dict[str, Any] | None = None,
          product: dict[str, Any] | None = None, price: float | None = None) -> str | None:
    """Блок РАСЧЁТ для контекста модели. None — задача не распознана, считать нечего.

    `text` — склейка последней реплики и предыдущих (объект часто назван раньше размеров).
    """
    task = _task(text)
    room = _room_pair(text)
    if not task or not room:
        return None
    a, b = room
    th, w, ln = _dims_mm(item, product, text)
    notes: list[str] = []
    lines: list[str] = []

    if task == "walls":
        if not (th and w):
            return None
        h, said = _height(text)
        if not said:
            notes.append(f"высота стен клиентом НЕ названа, взята {_m(h)} м — переспроси")
        per = (a + b) * 2
        rows = math.ceil(h / (w / 1000) - _EPS)         # ширина бруса = высота ряда в стене
        beam_len = (ln or 6000) / 1000
        per_row = math.ceil(per / beam_len - _EPS)      # ВСЕ четыре стены, не две
        pcs = rows * per_row
        pcs_waste = math.ceil(pcs * (1 + _BEAM_WASTE) - _EPS)
        vol = pcs_waste * (th / 1000) * (w / 1000) * beam_len
        lines += [
            f"периметр стен (все четыре): ({_m(a)} + {_m(b)}) × 2 = {_m(per)} м",
            f"рядов по высоте: {rows}",
            f"брусьев в одном ряду: {per_row} шт",
            f"ИТОГО К ЗАКАЗУ: {pcs_waste} шт — запас 5% на подрезку УЖЕ включён, "
            f"второй раз не добавляй",
            f"объём по этому количеству: {_m(vol)} м³",
        ]
        qty = pcs_waste
    elif task == "clad":
        h, said = _height(text)
        if not said:
            notes.append(f"высота стен клиентом НЕ названа, взята {_m(h)} м — переспроси")
        per = (a + b) * 2
        wall_area = per * h
        line = f"площадь стен: {_m(per)} м × {_m(h)} м = {_m(wall_area)} м²"
        area = wall_area
        if any(k in text.lower() for k in _CEILING_WORDS):
            ceil_area = a * b
            area += ceil_area
            line += f"; потолок: {_m(a)} × {_m(b)} = {_m(ceil_area)} м²"
        lines.append(line)
        area_waste = area * (1 + _DEFAULT_WASTE)
        lines.append(f"площадь к обшивке с запасом 10%: {_m(area_waste)} м²")
        if not (w and ln):
            lines.append("количество штук посчитаю, когда выберем профиль (нужна полезная "
                         "ширина и длина)")
            qty = None
        else:
            one = (w / 1000) * (ln / 1000)
            qty = math.ceil(area_waste / one - _EPS)
            lines.append(f"одна доска {_m(w)} мм × {_m(ln / 1000)} м = {_m(one)} м²")
            lines.append(f"ИТОГО К ЗАКАЗУ: {qty} шт — запас 10% УЖЕ включён, "
                         f"второй раз не добавляй")
    else:  # deck
        layers = _layers(text)
        area = a * b * layers
        lines.append(f"площадь: {_m(a)} × {_m(b)}"
                     + (f" × {layers} слоя" if layers > 1 else "")
                     + f" = {_m(area)} м²")
        if not (w and ln):
            lines.append("количество штук посчитаю, когда определимся с сечением и длиной")
            qty = None
        else:
            one = (w / 1000) * (ln / 1000)
            area_waste = area * (1 + _DEFAULT_WASTE / 2)
            qty = math.ceil(area_waste / one - _EPS)
            lines.append(f"одна доска {_m(w)} мм × {_m(ln / 1000)} м = {_m(one)} м²")
            lines.append(f"ИТОГО К ЗАКАЗУ: {qty} шт — запас 5% УЖЕ включён, "
                         f"второй раз не добавляй")

    if qty and price:
        lines.append(f"стоимость: {qty} × {_rub(price)} = {_rub(qty * price)}")
    out = ["РАСЧЁТ (посчитан кодом по каталогу — числа бери ТОЛЬКО отсюда, сам не пересчитывай):"]
    out += [f"  {ln_}" for ln_ in lines]
    if notes:
        out += [f"  ДОПУЩЕНИЕ: {n}" for n in notes]
    return "\n".join(out)
