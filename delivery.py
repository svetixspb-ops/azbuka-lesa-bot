"""Детерминированный расчёт стоимости доставки со склада Красное Село.

LLM путает тарифную МАТРИЦУ (длина × объём × зона), поэтому сумму считает НЕ он.
LLM только собирает параметры и вставляет в ответ служебный тег:
    [[DELIVERY len=<м> vol=<м³> place="<пункт|->" km=<число|-> manip=<да|нет>]]
`render_tags()` парсит тег и подставляет точную сумму из фикс-таблицы (или фразу
про менеджера для нестандартных случаев).
"""
from __future__ import annotations

import logging
import math
import os
import re
import unicodedata

import httpx

log = logging.getLogger("vera-delivery")

# Колонки тарифа по зонам: 0=до3, 1=до9, 2=10–19, 3=20–29, 4=30–49, 5=50+ км
ZONE_LABELS = ["до 3 км", "до 9 км", "10–19 км", "20–29 км", "30–49 км", "50+ км"]
MANAGER = "MANAGER"  # ячейка «дальше — менеджер»

# (len_bucket, vol_bucket, manipulator) -> 6 значений по зонам.
# Зона 50+ задаётся как ("perkm", ставка_₽_за_км, доплата_за_манипулятор).
TARIFF = {
    ("4", "2", False):  [1000, 1500, 2000, 3000, 4000, ("perkm", 80, 0)],
    ("6", "2", False):  [1500, 2000, 3000, 4000, 5000, ("perkm", 100, 0)],
    ("6", "6", False):  [4000, 5000, 6000, 7000, MANAGER, ("perkm", 140, 0)],
    ("6", "6", True):   [6000, 7000, 8000, 9000, MANAGER, ("perkm", 140, 2000)],
    ("6", "10", False): [8000, 9000, 11000, MANAGER, MANAGER, ("perkm", 220, 0)],
    ("6", "10", True):  [11000, 12000, 14000, MANAGER, MANAGER, ("perkm", 220, 3000)],
}

# Населённый пункт -> индекс зоны (от склада Красное Село, ул. Свободы 44 А).
# Расстояния ориентировочные — Артём уточнит/дополнит.
_ZONE_BY_PLACE_RAW = {
    0: ["красное село", "дудергоф", "можайский", "скачки"],
    1: ["горелово", "новогорелово", "виллози", "аннино", "лаголово", "новоселье"],
    2: ["русско-высоцкое", "русско высоцкое", "тайцы", "кипень", "ропша", "стрельна"],
    3: ["гатчина", "пушкин", "петергоф", "ломоносов"],
    4: ["волосово"],
}
ZONE_BY_PLACE: dict[str, int] = {}
for _z, _names in _ZONE_BY_PLACE_RAW.items():
    for _n in _names:
        ZONE_BY_PLACE[_n] = _z

# Центральные районы СПб — доставка только через менеджера.
_CENTRAL = ["василеостровский", "васильевский остров", "центральный",
            "петроградский", "адмиралтейский"]

# Общее название города без района: СПб спанит несколько зон (склад в Красном Селе —
# это тоже СПб), поэтому одной зоны нет — нужно переспросить район/ближайший пункт.
_SPB_GENERIC = ["санкт-петербург", "санкт петербург", "петербург", "петербурге",
                "санкт-петербурге", "спб", "питер", "питере", "питербург"]


def is_spb_generic(place: str | None) -> bool:
    """True, если назвали город целиком («Санкт-Петербург»), а не конкретный район/пункт."""
    p = _norm(place)
    if not p:
        return False
    # если уже есть конкретный распознаваемый пункт/район — это НЕ общий СПб
    if p in ZONE_BY_PLACE or any(c in p for c in _CENTRAL):
        return False
    return any(g in p for g in _SPB_GENERIC)


_VOWELS = "аеиоуыэюя"  # ё уже свёрнута в е в _norm


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFC", s or "").lower().strip()
    s = s.replace("ё", "е")
    return re.sub(r"\s+", " ", s)


def _destem(w: str) -> str:
    """Отбросить хвостовую гласную (грубый стемминг под падежи): гатчину->гатчин."""
    return w[:-1] if len(w) > 4 and w and w[-1] in _VOWELS else w


def zone_from_place(place: str | None) -> int | str | None:
    p = _norm(place)
    if not p or p in {"-", "—"}:
        return None
    for c in _CENTRAL:
        if c in p:
            return "CENTRAL"
    if p in ZONE_BY_PLACE:
        return ZONE_BY_PLACE[p]
    for key, z in ZONE_BY_PLACE.items():
        if key in p:
            return z
    # Падежная подстраховка: сравниваем основы (без хвостовой гласной) —
    # ловит «в Гатчину», «в Красном Селе», «Ропшу» и т.п., если LLM не привёл к им. падежу.
    pw = [_destem(w) for w in p.split()]
    for key, z in ZONE_BY_PLACE.items():
        ks = _destem(key.split()[0])
        if len(ks) >= 4 and any(w.startswith(ks) for w in pw):
            return z
    return None


def zone_from_km(km: float | None) -> int | None:
    if km is None:
        return None
    if km <= 3:
        return 0
    if km < 10:
        return 1
    if km < 20:
        return 2
    if km < 30:
        return 3
    if km < 50:
        return 4
    return 5


# ── Геокодинг незнакомых пунктов через OpenStreetMap Nominatim ────────────────
# Известные пункты обслуживает выверенная таблица ZONE_BY_PLACE; геокодинг — резерв
# для пунктов вне таблицы (Вырица, районы СПб и т.п.): адрес → координаты → расстояние
# от склада → зона. За env-флагом GEOCODE_ENABLED, чтобы при сбоях легко отключить.
GEOCODE_ENABLED = os.environ.get("GEOCODE_ENABLED", "1") not in ("0", "", "false", "no")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
# Прямая (haversine) дистанция короче дорожной — умножаем на коэффициент извилистости.
ROAD_FACTOR = float(os.environ.get("GEOCODE_ROAD_FACTOR", "1.4"))
# Склад Красное Село, ул. Свободы, 44 А.
WAREHOUSE_LAT = float(os.environ.get("WAREHOUSE_LAT", "59.7375"))
WAREHOUSE_LON = float(os.environ.get("WAREHOUSE_LON", "30.0857"))

_GEO_CACHE: dict[str, tuple[int | None, float | None]] = {}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


async def geocode_zone(place: str | None) -> tuple[int | None, float | None]:
    """Незнакомый пункт → (зона, км) через Nominatim. (None, None) если не нашли/выключено.

    Дорожная дистанция ≈ haversine × ROAD_FACTOR. Результат кешируется по нормализованному
    названию. Поиск ограничен Россией и контекстом «Ленинградская область / Санкт-Петербург»,
    чтобы не поймать одноимённый пункт в другом регионе.
    """
    if not GEOCODE_ENABLED:
        return (None, None)
    p = _norm(place)
    if not p or p in {"-", "—"}:
        return (None, None)
    if p in _GEO_CACHE:
        return _GEO_CACHE[p]
    # Винительный падеж («в Вырицу») геокодится хуже/в другой объект, чем именительный.
    # Частое окончание -у → -а (Вырицу→Вырица, Стрельну→Стрельна) для лучшего совпадения.
    base = place.strip()
    if len(base) > 4 and base[-1].lower() == "у":
        base = base[:-1] + "а"
    query = f"{base}, Россия"
    try:
        async with httpx.AsyncClient(timeout=8) as cx:
            r = await cx.get(
                NOMINATIM_URL,
                # bounded=1 + viewbox по Ленинградской области и СПб: покрывает и пригороды области,
                # и районы города (Купчино, Колпино), и отсекает одноимённые пункты в других регионах.
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "ru",
                        "viewbox": "27.5,61.3,35.7,58.4", "bounded": 1},
                headers={"User-Agent": "azbuka-lesa-vera-bot/1.0 (delivery geocoding)"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # сеть/таймаут/парсинг — мягкий откат на менеджера
        log.warning("geocode failed for %r: %s", place, e)
        return (None, None)
    if not data:
        _GEO_CACHE[p] = (None, None)
        return (None, None)
    try:
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, ValueError, IndexError):
        _GEO_CACHE[p] = (None, None)
        return (None, None)
    km = round(_haversine_km(WAREHOUSE_LAT, WAREHOUSE_LON, lat, lon) * ROAD_FACTOR, 1)
    zone = zone_from_km(km)
    _GEO_CACHE[p] = (zone, km)
    log.info("geocode %r -> %.1f км, зона %s", place, km, zone)
    return (zone, km)


def vol_bucket(v: float | None) -> str | None:
    if v is None:
        return None
    if v <= 2:
        return "2"
    if v <= 6:
        return "6"
    if v <= 10:
        return "10"
    return None  # >10 м³ -> менеджер


def len_bucket(length_m: float | None, vb: str) -> str | None:
    L = 6.0 if length_m is None else length_m
    if L > 6:
        return None  # негабарит -> менеджер
    if vb == "2" and L <= 4:
        return "4"
    return "6"


def compute(length_m, volume_m3, zone, manip, km=None):
    """-> (amount:int|None, note:str). amount=None => решает менеджер (см. note)."""
    if zone == "CENTRAL":
        return None, "central"
    if zone is None:
        return None, "no_zone"
    vb = vol_bucket(volume_m3)
    if vb is None:
        return None, "volume_over"   # >10 м³
    lb = len_bucket(length_m, vb)
    if lb is None:
        return None, "oversize"      # длина >6 м
    row = TARIFF.get((lb, vb, bool(manip))) or TARIFF.get((lb, vb, False))
    if row is None:
        return None, "no_row"
    cell = row[zone]
    if cell == MANAGER:
        return None, "manager_zone"
    if isinstance(cell, tuple):       # зона 50+ — за км
        _, rate, surch = cell
        if km is None:
            return None, "need_km"
        amount = rate * km + surch
        if km > 100:
            amount = amount * 0.95
        return int(round(amount)), "perkm"
    return int(cell), "ok"


_TAG_RE = re.compile(r"\[\[\s*DELIVERY\b(.*?)\]\]", re.IGNORECASE | re.DOTALL)
_LEFTOVER_RE = re.compile(r"\[\[[^\]]*\]?\]?")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _parse_kv(body: str):
    def f(key: str, pat: str) -> str | None:
        m = re.search(key + r"\s*=\s*" + pat, body, re.IGNORECASE)
        return m.group(1) if m else None

    place = f("place", r'"([^"]*)"') or f("place", r"([^\s\]]+)")
    manip_s = f("manip", r"(\S+)")
    manip = bool(manip_s) and _norm(manip_s).startswith(("да", "yes", "true", "y", "1"))
    return _num(f("len", r"([\d.,]+)")), _num(f("vol", r"([\d.,]+)")), place, _num(f("km", r"([\d.,]+)")), manip


def _amount_phrase(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " рублей"


def render_tags(text: str) -> str:
    """Заменить все теги [[DELIVERY ...]] на точную сумму (или фразу про менеджера)."""
    def repl(m: re.Match) -> str:
        length_m, volume_m3, place, km, manip = _parse_kv(m.group(1))
        zone = zone_from_place(place)
        if zone is None:
            zone = zone_from_km(km)
        amount, _note = compute(length_m, volume_m3, zone, manip, km=km)
        if amount is None:
            return "точную стоимость доставки рассчитает менеджер"
        return _amount_phrase(amount)

    text = _TAG_RE.sub(repl, text)
    # Подстраховка: вычистить любой недопарсенный остаток тега, чтобы он не утёк клиенту.
    if "[[" in text:
        text = _LEFTOVER_RE.sub("", text)
    return text
