"""Общий «мозг» Веры — расшифровка-агностичная логика диалога.

Один и тот же код используют:
- bot.py    — Telegram-обёртка (тест голос-в-голос)
- api.py    — HTTP-эндпоинт для сценария Voximplant (телефония)

Контракт: дать текст реплики клиента + идентификатор сессии (звонка) → получить
текст ответа Веры. История диалога хранится по session_id в памяти процесса.

extract → поиск по каждой позиции → контекст для LLM → ответ LLM → детерминированная
подстановка суммы доставки (тег [[DELIVERY ...]] → точная сумма из delivery.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import catalog
import delivery
import llm
import prompts

log = logging.getLogger("vera-brain")

_MSK = timezone(timedelta(hours=3))

# Журнал звонков: каждая реплика клиента, ответ Веры и что она нашла в каталоге.
# Нужен, чтобы разбирать косяки постфактум — раньше это жило только в systemd-журнале,
# который чистится (просьба Артёма 09.10.2026 «иметь доступ к прослушиванию диалогов»).
_DIALOGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vera_dialogs.jsonl")


def _log_turn(session_id: str, role: str, content: str, **extra: Any) -> None:
    """Дописать строку в журнал звонков. Журнал НИКОГДА не роняет ответ клиенту."""
    try:
        rec = {
            "ts": datetime.now(_MSK).isoformat(timespec="seconds"),
            "session_id": str(session_id),
            "role": role,
            "content": content,
        }
        rec.update(extra)
        with open(_DIALOGS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("dialog log failed: %s", e)


def _log_found(session_id: str, per_item: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
    """Что Вера поняла из реплики и что нашла по этому в каталоге — для разбора поиска."""
    rows = []
    for it, products in per_item:
        if not (it.get("text") or "").strip():
            continue
        rows.append({
            "запрос": it.get("raw") or it.get("text"),
            "понято": {k: it.get(k) for k in ("text", "thickness_mm", "width_mm", "length_mm",
                                              "species", "grade", "quantity_pieces", "packs")
                       if it.get(k) is not None},
            "найдено": [p.get("name") for p in products[:5]],
            "неточно": next((p["loose_dropped"] for p in products if p.get("loose_dropped")), None),
        })
    if rows:
        _log_turn(session_id, "search", "", items=rows)
_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
_WEEKDAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def _resolve_relative_date(text: str) -> str | None:
    """«послезавтра»/«завтра»/«в пятницу» и т.п. -> «23 сентября». None, если не распознано —
    тогда в ответе клиенту остаётся только его собственная формулировка, без выдумки даты."""
    low = text.strip().lower()
    now = datetime.now(_MSK)
    offset = None
    if "послезавтра" in low:
        offset = 2
    elif "сегодня" in low:
        offset = 0
    elif "завтра" in low:
        offset = 1
    else:
        for i, wd in enumerate(_WEEKDAYS_RU):
            if wd[:5] in low:
                delta = (i - now.weekday()) % 7
                offset = delta if delta != 0 else 7  # «в понедельник» в сам понедельник = через неделю
                break
    if offset is None:
        return None
    target = now + timedelta(days=offset)
    return f"{target.day} {_MONTHS_RU[target.month - 1]}"

HISTORY_MAX = 8  # 4 user + 4 assistant turns
# session_id — строка (Telegram uid как str, либо call-id Voximplant)
HISTORY: dict[str, list[dict[str, Any]]] = defaultdict(list)
# Накопленный заказ по сессии: product_name -> {pieces, unit_vol, length_m, total, how}.
# Нужен для ДЕТЕРМИНИРОВАННОГО расчёта доставки (объём+длина) и для сводки.
ORDER: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
# State-machine доставки по сессии: {stage, place, zone, amount, address, date}.
# stage: await_place → await_address → await_date → done.
DELIVERY: dict[str, dict[str, Any]] = defaultdict(dict)
# Товар, который Вера СЕЙЧАС обсуждает по сессии (предложила как близкое / назвала «есть … сколько?»).
# Чтобы оффер и финальный расчёт шли по ОДНОЙ позиции (один размер = несколько SKU: сухая/сырая/сорт),
# и клиент не услышал одну цену в предложении, а другую — в итоге.
LOCKED: dict[str, dict[str, Any]] = {}
# Запрошенное количество/пачки к предложенной замене, ожидающее подтверждения «да».
# Чтобы на голое «да, давайте» (без повтора числа) озвучить ИТОГ, а не молча уйти к доставке.
PENDING: dict[str, dict[str, Any]] = {}

# Имя клиента и номер, с которого он звонит — для заявки менеджеру (просьба Артёма
# 09.10.2026: «должно приходить имя, телефон, с которого человек звонил, и кратко
# о чём разговор»). Номер кладёт телефония через GET /callmeta ещё до первого слова.
NAME: dict[str, str] = {}
CALLER: dict[str, str] = {}
# Сессии, где уже прозвучало пояснение про сметный отдел. С 09.10.2026 оно говорится
# не в приветствии, а в момент ПЕРВОЙ записанной позиции: клиент звонит с готовым
# запросом, держать его полминуты монологом до того, как он сказал хоть слово, —
# плохой размен (решение Светы и Артёма).
INTRO_SAID: set[str] = set()


def _intro_once(sid: str) -> str:
    """Пояснение про приём заявки — ровно один раз за звонок, иначе пустая строка."""
    if sid in INTRO_SAID:
        return ""
    INTRO_SAID.add(sid)
    return " Сейчас сотрудники не могут ответить, поэтому заявку принимаю я — расчёт подтвердит сметный отдел."


def set_caller(session_id: str, number: str) -> None:
    """Запомнить номер звонящего (Caller ID из Asterisk) для этой сессии."""
    num = (number or "").strip()
    if num and num.lower() not in ("unknown", "anonymous", "<unknown>"):
        CALLER[str(session_id)] = num


def session_transcript(session_id: str) -> list[tuple[str, str]]:
    """Весь разговор этой сессии из журнала звонков — [(роль, текст), ...].

    Берём из файла, а не из HISTORY: история обрезана до последних ходов, а для
    сводки менеджеру нужен разговор целиком.
    """
    sid = str(session_id)
    out: list[tuple[str, str]] = []
    try:
        with open(_DIALOGS_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(d.get("session_id")) != sid:
                    continue
                role = d.get("role")
                if role in ("user", "assistant"):
                    out.append((role, d.get("content") or ""))
    except FileNotFoundError:
        pass
    return out

# Маркер завершения разговора: мозг ставит его в прощальную реплику, телефония
# по нему кладёт трубку (иначе на «Спасибо» клиента бот заново спрашивает «что интересует?»).
END_TAG = "[[END]]"


# Фразы прощания — по ним завершаем звонок ДЕТЕРМИНИРОВАННО (надёжнее, чем ждать [[END]] от модели).
_FAREWELL = ("хорошего дня", "хорошего вечера", "доброго дня", "доброго вечера",
             "всего доброго", "всего хорошего", "до свидания", "всего наилучшего")


def split_end(text: str) -> tuple[str, bool]:
    """Отделить маркер завершения: вернуть (чистый текст для озвучки, нужно_ли_завершить).

    end=True если модель поставила END_TAG ИЛИ реплика содержит фразу прощания.
    """
    clean = text.replace(END_TAG, "").strip()
    low = clean.lower()
    end = (END_TAG in text) or any(f in low for f in _FAREWELL)
    return clean, end


def reset(session_id: str) -> None:
    """Сбросить контекст диалога одной сессии."""
    sid = str(session_id)
    HISTORY.pop(sid, None)
    ORDER.pop(sid, None)
    DELIVERY.pop(sid, None)
    LOCKED.pop(sid, None)
    PENDING.pop(sid, None)
    NAME.pop(sid, None)
    CALLER.pop(sid, None)
    INTRO_SAID.discard(sid)


def _record_order_item(sid: str, it: dict[str, Any], products: list[dict[str, Any]]) -> None:
    """Если позиция посчитана (есть товар + количество) — запомнить её в заказе сессии.

    Хранит число штук, объём 1 шт и длину — для расчёта доставки и сводки.
    """
    import math
    if not products:
        return
    p = _pick_product(products, it)   # тот же вариант, что показываем клиенту
    tot = catalog.compute_total(p, it)
    if not tot:
        return
    unit_vol = catalog.piece_volume_m3(p.get("thickness_mm"), p.get("width_mm"),
                                       p.get("length_mm"), p.get("diameter_mm"))
    pack = p.get("pack_count")
    qty = catalog._to_num(it.get("quantity_pieces"))
    packs = catalog._to_num(it.get("packs"))
    target = catalog._to_num(it.get("target_m3"))
    if pack:
        n_packs = packs if packs else (math.ceil(qty / pack) if qty else
                  (math.ceil(math.ceil(target / unit_vol) / pack) if (target and unit_vol) else None))
        pieces = int(n_packs * pack) if n_packs else None
    else:
        pieces = int(qty) if qty else (math.ceil(target / unit_vol) if (target and unit_vol) else None)
    # Поправка клиента («не прямой, а скошенный») не должна ОСТАВЛЯТЬ в заявке
    # обе позиции: записанный ранее товар с конфликтующим профилем вытесняем
    # (звонок Артёма 09.10.2026).
    def _size_tag(nm: str) -> str:
        m = re.search(r"\d+\s*[хx]\s*\d+(?:\s*[хx]\s*\d+)?", nm.lower())
        return re.sub(r"\s+", "", m.group()) if m else ""

    new_size = _size_tag(p["name"])
    for old_name in [k for k in ORDER[sid]
                     if k != p["name"]
                     and _size_tag(k) == new_size          # тот же размер, иначе это другая позиция заказа
                     and _profile_conflict(p["name"].lower(), k.lower())]:
        ORDER[sid].pop(old_name, None)
    ORDER[sid][p["name"]] = {
        "pieces": pieces,
        "unit_vol": unit_vol,
        "length_m": (p.get("length_mm") or 0) / 1000 or None,
        "total": tot["total"],
        "how": tot["how"],
        "ref": _full_ref(p),   # полное наименование для сводки заказа
        "n": tot["n"],
        "unit": tot["unit"],   # "шт" | "уп"
    }


def _order_summary(sid: str) -> str | None:
    """Сводка всего заказа с ОБЩЕЙ суммой — для повтора перед закрытием (просьба Артёма 2026-06-01)."""
    rows = ORDER.get(sid) or {}
    if not rows:
        return None
    parts = []
    grand = 0
    for r in rows.values():
        grand += int(r.get("total") or 0)
        n, unit = r.get("n"), r.get("unit")
        if unit == "уп" and n:
            qp = f"{int(n)} {_plural(n, 'упаковка', 'упаковки', 'упаковок')}"
        elif n:
            qp = _shtuk(n)
        else:
            qp = ""
        ref = r.get("ref") or "позиция"
        parts.append(f"{ref} — {qp}" if qp else ref)
    body = "; ".join(parts)
    return (f"Давайте повторим ваш заказ. {body}. Итого по заказу {_rubles(grand)} — "
            f"это предварительная стоимость, точную озвучит менеджер.")


def _order_volume_length(sid: str) -> tuple[float | None, float | None]:
    """Суммарный объём (м³) и максимальная длина (м) накопленного заказа."""
    vol = 0.0
    maxlen = 0.0
    have_vol = False
    for r in ORDER[sid].values():
        if r.get("pieces") and r.get("unit_vol"):
            vol += r["pieces"] * r["unit_vol"]
            have_vol = True
        if r.get("length_m"):
            maxlen = max(maxlen, r["length_m"])
    return (vol if have_vol else None), (maxlen or None)


# Круглые товары: extract часто кладёт диаметр в thickness_mm (клиент говорит «столб 100»
# без слова «диаметр»). Для них толщину без ширины трактуем как диаметр.
_CYL_WORDS = ("столб", "бревн", "брёвн", "опор", "свая", "сва́", "кругл")


def _pick_product(products: list[dict[str, Any]], it: dict[str, Any]) -> dict[str, Any] | None:
    """Выбрать лучший вариант среди совпадений одного размера.

    Сначала уважаем явно названный клиентом сорт (_grade_filter) — иначе, при нескольких
    сортах одного размера, дефолт «первый по остатку» может подсунуть самый залежавшийся
    (обычно ВС) вместо запрошенного (баг Артёма 23.09 — рейка/доска игнорировали сорт АВ).
    Если клиент назвал количество и есть варианты с ДОСТАТОЧНЫМ остатком — берём из них
    самый дешёвый (не гоним в «нехватку» из-за другого варианта с малым остатком).
    Иначе — первый по умолчанию (сортировка поиска = по убыванию остатка).
    """
    if not products:
        return None
    products = _grade_filter(it, products)
    req = catalog._to_num(it.get("quantity_pieces"))
    if req:
        enough = [p for p in products if not p.get("pack_count") and (p.get("count") or 0) >= req]
        if enough:
            return min(enough, key=lambda p: p.get("price") or 0)
    return products[0]


def _search_item(it: dict[str, Any]) -> list[dict[str, Any]]:
    """Поиск по одной товарной позиции с ослабленным fallback."""
    text = (it.get("text") or "").lower()
    thickness = it.get("thickness_mm")
    width = it.get("width_mm")
    diameter = it.get("diameter_mm")
    if any(w in text for w in _CYL_WORDS) and not diameter and thickness and not width:
        diameter, thickness = thickness, None   # «столб 100» → диаметр 100, не толщина

    products = catalog.search(
        text=it.get("text") or None,
        thickness_mm=thickness,
        width_mm=width,
        length_mm=it.get("length_mm"),
        diameter_mm=diameter,
        species=it.get("species"),
    )
    if not products and it.get("text"):
        products = catalog.search_loose(it["text"], limit=8)
    return products


def _build_context_block(per_item: list[tuple[dict[str, Any], list[dict[str, Any]]]],
                          asked_avail: bool = False, asked_price: bool = False) -> str:
    """Контекст для LLM, сгруппированный по позициям запроса клиента.

    asked_avail/asked_price — клиент спросил про наличие/цену НАПРЯМУЮ в этой реплике (разворот
    Артёма 22.09.2026). По умолчанию (оба False) Вера не сверяется с остатком вслух и не называет
    цену — просто фиксирует заявку; цену итога клиент услышит в конце разговора (см. _order_summary)."""
    if not per_item:
        return ("В этой реплике товара нет — это приветствие, уточнение, подтверждение (да/нет/спасибо) "
                "или завершение разговора. Продолжай по контексту истории диалога: если заказ уже собран "
                "и клиент подтверждает/завершает — двигайся к закрытию (доставка → СМС-сводка → прощание), "
                "НЕ начинай разговор заново и НЕ спрашивай «что вас интересует». Если разговор только начался "
                "и клиент молчит/поздоровался — мягко уточни, что интересует.")
    blocks = ["Позиции из запроса клиента и найденные по ним товары "
              "(используй ТОЛЬКО эти товары, не выдумывай и не бери цены по памяти):"]
    for i, (it, products) in enumerate(per_item, 1):
        label = it.get("raw") or it.get("text") or "позиция"
        hints = []
        if it.get("grade"):
            hints.append(f"сорт {it['grade']}")
        if it.get("diameter_mm"):
            hints.append(f"диаметр {it['diameter_mm']} мм")
        if it.get("target_m3"):
            hints.append(f"нужно {it['target_m3']} м³")
        if it.get("quantity_pieces"):
            hints.append(f"нужно {it['quantity_pieces']} шт")
        if it.get("packs"):
            hints.append(f"нужно {it['packs']} уп")
        hint_s = f" — {', '.join(hints)}" if hints else ""
        blocks.append(f"\nПозиция {i}: «{label}»{hint_s}")
        # Товар найден только после выбрасывания слов из запроса — значит точного
        # совпадения в каталоге нет, это лишь похожая позиция. Говорим об этом прямо,
        # иначе Вера фиксирует её как заказанную (звонок Артёма 09.10.2026: клиент
        # просил планкен СКОШЕННЫЙ, записан был планкен ПРЯМОЙ).
        dropped = next((p["loose_dropped"] for p in products if p.get("loose_dropped")), None)
        if dropped:
            miss = ", ".join(f"«{w}»" for w in dropped)
            blocks.append(f"  — ВНИМАНИЕ: точного совпадения по {miss} в каталоге НЕ найдено. "
                          f"Товары ниже — ПОХОЖИЕ, а не то, что просил клиент. НЕ записывай их "
                          f"в заявку как заказанное и НЕ говори «записала». Скажи честно: точно "
                          f"такого в каталоге не вижу, предложи похожее как альтернативу "
                          f"(«есть вот такое — подойдёт?») либо передай позицию в сметный отдел.")
        # Слишком много совпадений И вообще без параметров → НЕ зачитываем список
        # (для голоса длинный перечень = плохо), просим уточнить ОДИН параметр.
        # Если вариантов и так МАЛО (≤3) — уточнять нечего, выбирать не из чего: показываем
        # их сразу, даже если клиент не назвал ни одного параметра (баг Артёма 21.09 — «ДВП»:
        # в наличии всего 1 позиция, а Вера зациклилась в «назовите длину», «назовите толщину»,
        # хотя выбирать было решительно не из чего). Если ни один из показанных не подойдёт
        # клиенту — обычный сценарий сметного отдела (см. ветку "иначе не подошло" в промпте).
        no_params = not any(it.get(k) for k in ("thickness_mm", "width_mm", "length_mm", "diameter_mm", "grade", "target_m3", "quantity_pieces", "packs"))
        # Есть ли вообще сорт у найденных позиций — чтобы Вера не спрашивала про сорт там, где его нет.
        # (было: смотрели только в поле sort — для рейки/доски оно пустое, сорт зашит в название,
        # бот считал что сорта нет вообще и не только не спрашивал, а активно игнорировал названный клиентом)
        has_grade = any(_product_grade(p) for p in products) if products else False
        has_qty = any(it.get(k) for k in ("quantity_pieces", "packs", "target_m3"))
        if products and len(products) > 3 and no_params:
            narrow_by = "размер и влажность" if not has_grade else "размер, влажность или сорт"
            blocks.append(f"  — в наличии {len(products)} вариантов. "
                          f"НЕ перечисляй их клиенту списком — задай ОДИН короткий уточняющий вопрос "
                          f"про {narrow_by}, чтобы сузить до 1-2 вариантов.")
            if not has_grade:
                blocks.append("  — ВНИМАНИЕ: сорт у этих позиций НЕ указан — про сорт НЕ спрашивай и сорта НЕ выдумывай.")
        elif products:
            req_qty = catalog._to_num(it.get("quantity_pieces"))
            primary = _pick_product(products, it)
            # Если подобран вариант с достаточным остатком — показываем ТОЛЬКО его
            # (не сыплем альтернативами с малым остатком и ложной «нехваткой»).
            enough_ok = bool(req_qty) and not primary.get("pack_count") and (primary.get("count") or 0) >= req_qty
            show = [primary] if enough_ok else products[:5]
            for p in show:
                line = "  — " + catalog.format_product_line(p)
                tot = catalog.compute_total(p, it)
                if tot:
                    price_note = "ИТОГ (точно, не пересчитывай)" if (asked_price or asked_avail) else \
                        "ИТОГ для справки — НЕ озвучивай сумму сейчас, только если клиент прямо спросит цену; иначе прозвучит в конце разговора"
                    line += f"  | {price_note}: {tot['how']}"
                blocks.append(line)
                # Запрошено больше, чем в наличии.
                cnt = p.get("count") or 0
                if req_qty and not p.get("pack_count") and req_qty > cnt:
                    if asked_avail:
                        blocks.append(f"    ВНИМАНИЕ: клиент просит {int(req_qty)} шт, а в наличии только {cnt}. "
                                      f"Скажи, что в наличии {cnt} шт: предложи оформить {cnt} сейчас, "
                                      f"а недостающее передать в сметный отдел под заказ. НЕ оформляй {int(req_qty)} молча.")
                    else:
                        blocks.append(f"    Наличие клиент не спрашивал — про нехватку ({cnt} из {int(req_qty)}) "
                                      f"вслух НЕ говори. Просто запиши {int(req_qty)} шт как есть, менеджер уточнит.")
            if not has_grade:
                blocks.append("  — сорт у этой позиции НЕ указан — про сорт НЕ спрашивай.")
            if len(products) <= 2 and not has_qty:
                blocks.append("  — количество клиент пока не назвал: спроси, сколько штук нужно "
                              "(для столбов/штучного товара — именно в ШТУКАХ, не в кубах). "
                              "Про наличие говори, только если клиент сам спросил.")
        else:
            blocks.append("  — этого нет в переданном каталоге. НЕ говори «недоступно»/«не продаём»/«нет в продаже»/"
                          "«пока недоступно к заказу». Запиши позицию в заявку и скажи, что передашь её в сметный отдел — "
                          "менеджер подберёт и перезвонит. Если уместно, спроси нужное количество.")
    return "\n".join(blocks)


# Сигналы, что клиент сразу перешёл к заказу (а не назвал имя) — тогда обычный ход с LLM.
_ORDER_SIGNALS = (
    "доск", "брус", "вагонк", "пиломат", "террас", "имитац", "блок", "хаус", "рейк",
    "штакет", "забор", "сорт", "лиственниц", "сосн", "хво", "куб", "метр", "мм",
    "размер", "цен", "стоимост", "наличи", "достав", "профиль", "планкен",
)
_NAME_LEADINS = ("меня зовут ", "моё имя ", "мое имя ", "это ", "зовут ", "можно ", "я ")
# Не имена: приветствия/служебные слова — на них отвечаем без подстановки имени.
_NOT_NAMES = {
    "здравствуйте", "здравствуй", "привет", "приветствую", "добрый", "доброе", "добрая",
    "день", "утро", "вечер", "ночи", "алло", "ало", "да", "слушаю", "говорите", "хорошо",
    "ага", "угу", "нет", "извините", "простите", "девушка",
    # Имена самой Веры: «Привет, Вера» — это обращение К НЕЙ, а не имя клиента
    # (то же правило жёстко прописано в prompts.py).
    "вера", "верочка", "елена",
}


def _maybe_name(text: str) -> str | None:
    """Первый ход после приветствия: вычленить имя.

    None  → это не имя (клиент сразу про заказ) → обычный LLM-ход.
    ""    → похоже на имя, но чисто не вычленили → поприветствуем без имени.
    "Имя" → вычлененное имя.
    """
    t = text.strip().strip(".!,?")
    low = t.lower()
    if any(s in low for s in _ORDER_SIGNALS) or any(c.isdigit() for c in t):
        return None
    # Срезаем приветствие ПЕРЕД разбором: «Здравствуйте, меня зовут Сергей» — это имя
    # Сергей, а не служебное слово. Раньше «здравствуйте» ловилось в _NOT_NAMES, и имя
    # терялось целиком (найдено на тестовом прогоне нового сценария 09.10.2026).
    t = re.sub(r"^\s*(здравствуй(те)?|добрый\s+(день|вечер)|доброе\s+утро|приветствую|привет|алло|ало)\b[\s,.!-]*",
               "", t, flags=re.IGNORECASE).strip()
    low = t.lower()
    for p in _NAME_LEADINS:
        if low.startswith(p):
            t = t[len(p):].strip()
            break
    words = [w for w in t.split() if w]
    if any(w.lower() in _NOT_NAMES for w in words):
        return ""  # приветствие/служебное слово — поздороваемся без имени
    if 1 <= len(words) <= 2 and all(w[:1].isalpha() for w in words):
        return " ".join(w.capitalize() for w in words)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# ДЕТЕРМИНИРОВАННАЯ товарная реплика: цену и итог считает и ОЗВУЧИВАЕТ код, не LLM.
# Нейросеть в длинном диалоге путает цены между позициями и выдумывает несуществующие
# размеры (баг на звонке 2026-05-31). Поэтому для одиночной определённой позиции фразу
# с ценой/итогом строит код по каталогу. Мультизаказ и уточнения — остаются за LLM,
# но LLM запрещено называть цифры цен (правило в prompts.py).
# ─────────────────────────────────────────────────────────────────────────────
_LEN_WORD = {1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять", 6: "шесть"}
_DESC_WORDS = ("профилирован", "антисептир", "биозащит", "огнезащит", "окрашен", "камерн", "сух",
               "завальцован", "строган")
_NATURAL_MOISTURE_MARK = ("естествен",)
_CHEAP_WORDS = ("дешевл", "подешевл", "недорог", "эконом", "бюджет", "попроще")
# Прямой вопрос про наличие/цену (разворот Артёма 22.09.2026): по умолчанию Вера в это НЕ лезет,
# отвечает предметно только когда клиент спросил явно.
_AVAIL_Q_WORDS = ("наличи", "есть ли", "сколько есть", "сколько у вас", "остал", "в продаже", "сейчас есть")
_PRICE_Q_WORDS = ("цен", "стоит", "стоимост", "почём", "почем", "сколько буд", "сколько выйдет")
# Обработка-пропитка (биозащита/огнезащита): если нет готового — делаем за 1-2 дня (просьба Артёма 2026-06-01).
_TREATMENT_WORDS = ("антисептир", "биозащит", "огнезащит", "антипир", "пропит")
# Покраска — отдельная услуга: без поиска цены/количества, заявка менеджеру (просьба Артёма 2026-06-01).
_PAINT_WORDS = ("покрас", "покраш", "окрас", "окраш", "крашен", "колер", "тонир")


def _wants_natural_moisture(raw: str) -> bool:
    """Клиент прямо просит НЕ сухую доску («естественной влажности» / «не сухая» / «не сухой»).

    Баг 2026-09: подстрока «сух» есть и в «не сухая» — без этой проверки код читал отрицание
    как запрос сухой доски и зацикливался, переспрашивая/подтверждая сухую снова и снова."""
    if any(w in raw for w in _NATURAL_MOISTURE_MARK):
        return True
    return re.search(r"\bне\s+\S*сух", raw) is not None


def _wants_unplaned(raw: str) -> bool:
    """Клиент прямо просит НЕстроганную доску («нестроганная» / «не строганная»).

    Симметрично _wants_natural_moisture: подстрока «строган» есть и в «нестроганная»,
    без этой проверки отрицание читалось бы как запрос строганной."""
    if "нестроган" in raw:
        return True
    return re.search(r"\bне\s+\S*строган", raw) is not None


def _desc_words_in(raw: str) -> list[str]:
    """Слова-исполнения из _DESC_WORDS, которые клиент ПОЛОЖИТЕЛЬНО запросил (без отрицания)."""
    natural = _wants_natural_moisture(raw)
    unplaned = _wants_unplaned(raw)
    words = []
    for w in _DESC_WORDS:
        if w not in raw:
            continue
        if w == "сух" and natural:
            continue
        if w == "строган" and unplaned:
            continue
        words.append(w)
    return words


def _asks_availability(text: str) -> bool:
    """Клиент ПРЯМО спросил про наличие/остатки — только тогда Вера в них лезет (Артём 22.09.2026)."""
    t = text.lower()
    return any(w in t for w in _AVAIL_Q_WORDS)


def _asks_price(text: str) -> bool:
    """Клиент ПРЯМО спросил про цену — только тогда Вера называет её сразу, а не в конце разговора."""
    t = text.lower()
    return any(w in t for w in _PRICE_Q_WORDS)


def _len_phrase(length_mm: float | None) -> str:
    if not length_mm:
        return ""
    m = length_mm / 1000
    if m == int(m):
        mi = int(m)
        unit = _plural(mi, "метр", "метра", "метров")
        return f"{_LEN_WORD.get(mi, mi)} {unit}"
    return f"{m:g} м"


def _type_word(p: dict[str, Any]) -> str:
    return (p.get("name") or "").split()[0] if p.get("name") else "позиция"


def _fmt_dim(x: Any) -> str:
    """Размер словами для озвучки: целое → «20», дробное → «12,5» (не теряем половинку)."""
    f = float(x)
    return str(int(f)) if f.is_integer() else f"{f:g}".replace(".", ",")


def _spoken_ref(p: dict[str, Any]) -> str:
    """Короткая разговорная ссылка на товар: «рейка 20 на 95, три метра»."""
    th, w, d, l = p.get("thickness_mm"), p.get("width_mm"), p.get("diameter_mm"), p.get("length_mm")
    parts = [_type_word(p).lower()]
    if d:
        parts.append(f"диаметр {_fmt_dim(d)}")
    elif th and w:
        parts.append(f"{_fmt_dim(th)} на {_fmt_dim(w)}")
    lp = _len_phrase(l)
    ref = " ".join(parts)
    return f"{ref}, {lp}" if lp else ref


_PAREN_DIGIT_RE = re.compile(r"\([^)]*\d[^)]*\)")  # «(до 25 см)» — убрать целиком
# Числовой сорт («3-4 сорт», «сорт 1») — убираем вместе со словом «сорт» (буквенный «сорт АВ» оставляем).
_SORT_NUM_RE = re.compile(r"\b\d[\d\-–]*\s*сорт[а-яё]*|\bсорт[а-яё]*\s*\d[\d\-–]*", re.IGNORECASE)
# Метки размеров/единиц в названии — не часть исполнения, в озвучку не идут.
_DIM_LABELS = {"д", "l", "d", "л", "мм", "м", "=", "мм.", "м.", "д.", "х"}


def _name_desc(p: dict[str, Any]) -> str:
    """Описательная часть наименования БЕЗ размерного кода: «Палубная доска сухая завальцованная».

    Нужно, чтобы клиент слышал ПОЛНОЕ наименование с исполнением (сухая строганная/завальцованная,
    антисептированная, профилированная), а не только тип. Выкидываем токены с цифрами (размер,
    D=…/L=…, «мм») и метки длины/диаметра (Д/L/=) — их озвучим отдельно словами. Породу/сорт/
    обработку оставляем. Скобки с цифрами («(до 25 см)») режем целиком, без цифр («(хвоя)») — оставляем.
    """
    name = (p.get("name") or "").replace("×", "х")
    name = _PAREN_DIGIT_RE.sub(" ", name)
    name = _SORT_NUM_RE.sub(" ", name)
    name = name.replace("(", " ").replace(")", " ")
    words = []
    for w in name.split():
        if any(ch.isdigit() for ch in w):
            continue  # размерный код (28х145х6000, D=100мм, L=3м)
        cw = w.strip(".,").lower()
        if not cw or cw in _DIM_LABELS:
            continue
        words.append(w.strip(".,"))
    return " ".join(words)


def _full_ref(p: dict[str, Any]) -> str:
    """Полное разговорное наименование: исполнение + размеры словами.

    «Палубная доска сухая завальцованная, 28 на 145, шесть метров». По просьбе Артёма
    (2026-06-01): когда Вера находит позицию — называет ПОЛНОЕ наименование, чтобы клиент
    точно понимал, что заказывает (сухой строганный / сухой завальцованный / антисептированный)."""
    th, w, d, l = p.get("thickness_mm"), p.get("width_mm"), p.get("diameter_mm"), p.get("length_mm")
    dims = []
    if d:
        dims.append(f"диаметр {_fmt_dim(d)}")
    elif th and w:
        dims.append(f"{_fmt_dim(th)} на {_fmt_dim(w)}")
    lp = _len_phrase(l)
    if lp:
        dims.append(lp)
    base = _name_desc(p) or _type_word(p)
    return f"{base}, {', '.join(dims)}" if dims else base


_GRADE_TOKENS = {"А", "Б", "АБ", "АВ", "АВС", "В", "ВС", "С", "Б/Ц", "БС", "БСК", "Э"}
_NAME_GRADE_RE = re.compile(r"СОРТ\s+([А-Я/]+)")


def _product_grade(p: dict[str, Any]) -> str:
    """Сорт товара: сперва поле `sort` (вагонка и т.п.), а если пусто — токен «сорт X» из
    названия (рейка/доска — у них сорт зашит в текст, поле sort всегда NULL, баг Артёма 23.09:
    Вера считала, что у рейки/доски сорта нет вообще, и игнорировала явно названный клиентом)."""
    s = (p.get("sort") or "").strip().upper()
    if s:
        return s
    m = _NAME_GRADE_RE.search((p.get("name") or "").upper())
    if m and m.group(1) in _GRADE_TOKENS:
        return m.group(1)
    return ""


def _grade_filter(it: dict[str, Any], products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Если клиент назвал сорт — оставляем товары с этим сортом (поле sort ИЛИ вшитый в
    название). Если таких нет — не блокируем продажу, возвращаем исходный список."""
    g = (it.get("grade") or "").strip().upper()
    if not g:
        return products
    matched = [p for p in products if _product_grade(p) == g]
    return matched or products


def _price_phrase(p: dict[str, Any]) -> str:
    """«… за упаковку» для пакованных товаров (евровагонка ×10), иначе «… за штуку»."""
    unit = "упаковку" if p.get("pack_count") else "штуку"
    return f"{_rubles(int(p['price']))} за {unit}"


def _distinct_types(products: list[dict[str, Any]]) -> set[str]:
    return {_type_word(p).lower() for p in products}


def _confident_pick(it: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Выбрать один вариант с учётом описательных слов запроса и интонации «дешевле»."""
    if not products:
        return None
    raw = (it.get("raw") or "").lower()
    cand = products
    if _wants_natural_moisture(raw):
        natural = [p for p in cand if "сух" not in (p.get("name") or "").lower()]
        if natural:
            cand = natural
    for w in _desc_words_in(raw):
        f = [p for p in cand if w[:7] in (p.get("name") or "").lower()]
        if f:
            cand = f
    cand = _grade_filter(it, cand)  # уважать названный сорт (А/В/АВ), зашитый в название
    qty = catalog._to_num(it.get("quantity_pieces"))
    if qty:
        enough = [p for p in cand if not p.get("pack_count") and (p.get("count") or 0) >= qty]
        if enough:
            return min(enough, key=lambda p: p.get("price") or 0)
    if any(w in raw for w in _CHEAP_WORDS):
        return min(cand, key=lambda p: p.get("price") or 0)
    return cand[0]  # порядок поиска = по убыванию остатка (самый ходовой)


def _has_disambig(it: dict[str, Any]) -> bool:
    """Есть ли в запросе сигнал, позволяющий уверенно выбрать вариант среди разных типов."""
    raw = (it.get("raw") or "").lower()
    return (bool(_desc_words_in(raw)) or _wants_natural_moisture(raw) or any(w in raw for w in _CHEAP_WORDS)
            or bool(catalog._to_num(it.get("quantity_pieces"))) or bool(catalog._to_num(it.get("packs"))))


def _amt_word(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    d, u = n % 100, n % 10
    if 11 <= d <= 14:
        return many
    if u == 1:
        return one
    if 2 <= u <= 4:
        return few
    return many


def _rubles(n: int) -> str:
    return f"{_amt_word(int(n))} {_plural(n, 'рубль', 'рубля', 'рублей')}"


def _shtuk(n: int) -> str:
    return f"{int(n)} {_plural(n, 'штука', 'штуки', 'штук')}"


def _req_dims(it: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None]:
    """Запрошенные размеры (мм) с поправкой на круглые товары («столб 100» → диаметр)."""
    th = catalog._to_num(it.get("thickness_mm"))
    w = catalog._to_num(it.get("width_mm"))
    l = catalog._to_num(it.get("length_mm"))
    d = catalog._to_num(it.get("diameter_mm"))
    text = (it.get("text") or "").lower()
    if any(x in text for x in _CYL_WORDS) and not d and th and not w:
        d, th = th, None
    return th, w, l, d


def _pair_close(pt: Any, pw: Any, th: float, w: float, tol: float) -> bool:
    """Сравнить пару (толщина, ширина) ПОРЯДОК-НЕЗАВИСИМО: клиент мог сказать «12 на 45»
    вместо «45 на 12». Безопасно для погонажа (плинтус/галтель), где первое число > второго."""
    if pt is None or pw is None:
        return False
    pt, pw = float(pt), float(pw)
    same = abs(pt - th) <= tol and abs(pw - w) <= tol
    swap = abs(pt - w) <= tol and abs(pw - th) <= tol
    return same or swap


def _exact_match(p: dict[str, Any], it: dict[str, Any]) -> bool:
    """Товар ТОЧНО соответствует названным размерам (по тем, что клиент указал).

    Толщина/ширина сравниваются порядок-независимо (см. _pair_close); длина и диаметр —
    строго. Защита от тихой подмены: фиксируем заказ ТОЛЬКО при точном совпадении.
    """
    th, w, l, d = _req_dims(it)
    for val, col in ((l, "length_mm"), (d, "diameter_mm")):
        if val is not None:
            pv = p.get(col)
            if pv is None or abs(float(pv) - val) > 0.5:
                return False
    if th is not None and w is not None:
        return _pair_close(p.get("thickness_mm"), p.get("width_mm"), th, w, 0.5)
    for val, col in ((th, "thickness_mm"), (w, "width_mm")):
        if val is not None:
            pv = p.get(col)
            if pv is None or abs(float(pv) - val) > 0.5:
                return False
    return True


def _dim_distance(it: dict[str, Any], p: dict[str, Any]) -> float:
    """Дистанция размеров запроса до товара (мм). Толщина/ширина — порядок-независимо,
    длину учитываем мягче. Для ранжирования «ближайшего» варианта."""
    th, w, l, d = _req_dims(it)
    s = 0.0
    pt, pw = p.get("thickness_mm"), p.get("width_mm")
    if th is not None and w is not None and pt is not None and pw is not None:
        same = abs(float(pt) - th) + abs(float(pw) - w)
        swap = abs(float(pt) - w) + abs(float(pw) - th)
        s += min(same, swap)
    else:
        for val, col in ((th, "thickness_mm"), (w, "width_mm")):
            if val is not None and p.get(col) is not None:
                s += abs(float(p[col]) - val)
    if d is not None and p.get("diameter_mm") is not None:
        s += abs(float(p["diameter_mm"]) - d)
    if l is not None and p.get("length_mm") is not None:
        s += abs(float(p["length_mm"]) - l) / 100.0
    return s


def _dim_distance_core(it: dict[str, Any], p: dict[str, Any]) -> float:
    """Дистанция только по сечению (толщина/ширина/диаметр), БЕЗ длины. Для определения
    «тот же товар, что обсуждаем» — клиент мог принять близкую по сечению позицию другой длины."""
    th, w, _l, d = _req_dims(it)
    s = 0.0
    pt, pw = p.get("thickness_mm"), p.get("width_mm")
    if th is not None and w is not None and pt is not None and pw is not None:
        s += min(abs(float(pt) - th) + abs(float(pw) - w), abs(float(pt) - w) + abs(float(pw) - th))
    else:
        for val, col in ((th, "thickness_mm"), (w, "width_mm")):
            if val is not None and p.get(col) is not None:
                s += abs(float(p[col]) - val)
    if d is not None and p.get("diameter_mm") is not None:
        s += abs(float(p["diameter_mm"]) - d)
    return s


# Типовые слова (не «исполнение», а вид товара) — для приоритета при выборе ближайшего.
_TYPE_HINTS = ("каркасн", "строган", "палубн", "террас", "полов", "шпунт", "обрезн")


def _nearest_alt(it: dict[str, Any], pool: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Из пула выбрать товар, БЛИЖАЙШИЙ по размеру к запросу. При равной дистанции —
    приоритет тем, чьё название совпадает со словами клиента (сухая/каркасная/…), потом по остатку."""
    if not pool:
        return None
    raw = (it.get("raw") or it.get("text") or "").lower()

    def key(p: dict[str, Any]) -> tuple[float, int, int]:
        name = (p.get("name") or "").lower()
        bonus = 0
        for wrd in (*_DESC_WORDS, *_TYPE_HINTS):
            ws = wrd[:5]
            if ws in raw and ws in name:
                bonus -= 1  # меньше = выше приоритет
        return (round(_dim_distance(it, p), 2), bonus, -(p.get("count") or 0))

    return sorted(pool, key=key)[0]


# Взаимоисключающие профили одного товара: назвал один — товар с другим НЕ подходит.
# Звонок Артёма 09.10.2026: клиент трижды сказал «планкен скошенный», но «скошен»/«прям»
# не были исполнениями из _DESC_WORDS, конфликта не возникало — и _continues_locked
# держался уже зафиксированного «Планкен прямой», повторяя «Записала: прямой» на каждую
# поправку. Для raw держим точные формы («прямо сейчас» ≠ «прямой планкен»).
_PROFILE_GROUPS = (
    (("скошен", r"скошен"),
     ("прям",   r"прям(?:ой|ого|ом|ым|ая|ую|ое|ые|ых)")),
)


def _profile_conflict(raw: str, name: str) -> bool:
    """Клиент назвал профиль из группы, а у товара профиль ДРУГОЙ из той же группы."""
    for group in _PROFILE_GROUPS:
        asked = [sub for sub, pat in group if re.search(pat, raw)]
        if not asked:
            continue
        others = [sub for sub, _ in group if sub not in asked]
        if any(o in name for o in others) and not any(a in name for a in asked):
            return True
    return False


def _desc_conflict(it: dict[str, Any], p: dict[str, Any]) -> bool:
    """Клиент назвал исполнение (сухая/антисептир…), которого у товара p НЕТ — нельзя его подсовывать."""
    raw = (it.get("raw") or "").lower()
    name = (p.get("name") or "").lower()
    if _wants_natural_moisture(raw) and "сух" in name:
        return True  # просил не сухую/естественной влажности — сухая p не подходит
    if _profile_conflict(raw, name):
        return True
    return any(w[:5] not in name for w in _desc_words_in(raw))


def _continues_locked(it: dict[str, Any], locked: dict[str, Any]) -> bool:
    """Клиент уточняет количество/подтверждает по товару, который мы УЖЕ обсуждаем
    (предложили/назвали). Тогда расчёт идёт по той же позиции — оффер и итог не разъезжаются."""
    it_type = (it.get("text") or "").split()
    lp_type = _type_word(locked).lower()
    if it_type and it_type[0][:4] != lp_type[:4]:
        return False  # перешёл к другому типу товара
    th, w, l, d = _req_dims(it)
    if th is None and w is None and d is None:
        # размеры не названы — чистое уточнение количества; но иная длина = другой товар
        if l is not None and locked.get("length_mm") and abs(float(locked["length_mm"]) - l) > 100:
            return False
        return True
    # размеры названы — должны точно совпасть с обсуждаемым товаром (порядок-независимо)
    if th is not None and w is not None and not _pair_close(locked.get("thickness_mm"), locked.get("width_mm"), th, w, 1.0):
        return False
    if d is not None and (locked.get("diameter_mm") is None or abs(float(locked["diameter_mm"]) - d) > 1.0):
        return False
    if l is not None and locked.get("length_mm") and abs(float(locked["length_mm"]) - l) > 100:
        return False
    return True


def _locked_or_pick(sid: str, it: dict[str, Any], exact: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Если среди точных совпадений есть товар, который мы уже обсуждали (locked) — берём ЕГО
    (чтобы цена в итоге = цене из предложения). Иначе выбираем обычным образом."""
    lp = LOCKED.get(sid)
    if lp:
        match = next((p for p in exact if p.get("id") == lp.get("id")), None)
        if match and not _desc_conflict(it, match):
            return match
    return _confident_pick(it, exact)


def _present(sid: str, it: dict[str, Any], p: dict[str, Any],
             asked_avail: bool = False, asked_price: bool = False) -> str:
    """Озвучить выбранный товар p: либо итог по количеству, либо цену + вопрос «сколько?».
    Запоминает p как обсуждаемый (LOCKED), чтобы следующая реплика считалась по нему же.

    Разворот Артёма 22.09.2026: наличие/остатки и цену Вера озвучивает ТОЛЬКО если клиент
    спросил про них прямо (asked_avail/asked_price). По умолчанию — просто фиксирует заявку,
    остаток/цену не называет; цену всего заказа клиент услышит в конце (см. _order_summary)."""
    LOCKED[sid] = p
    PENDING.pop(sid, None)  # количество подтверждено/озвучено — ожидание снято
    tot = catalog.compute_total(p, it)
    ref = _full_ref(p)   # полное наименование (с исполнением), просьба Артёма 2026-06-01
    cnt = p.get("count") or 0
    qn = catalog._to_num(it.get("quantity_pieces"))
    short = bool(qn) and not p.get("pack_count") and qn > cnt
    if short and asked_avail:
        return (f"{ref.capitalize()} — в наличии только {_shtuk(cnt)}. Оформить {cnt} сейчас, "
                f"а остальное передать в сметный отдел под заказ?")
    if tot and not short:
        _record_order_item(sid, it, [p])  # в заказ для доставки/сводки
        if tot["unit"] == "уп":
            packs_w = _plural(tot["n"], "упаковка", "упаковки", "упаковок")
            qty_phrase = f"{tot['n']} {packs_w} ({_shtuk(tot['pieces'])})"
        else:
            qty_phrase = _shtuk(tot["n"])
        price_part = f", {_rubles(tot['total'])}" if asked_price else ""
        return f"Записала: {ref} — {qty_phrase}{price_part}.{_intro_once(sid)} Что-то ещё?"
    if short:
        # По умолчанию (наличие не спросили) — не сверяемся с остатком вслух, просто фиксируем.
        return (f"Записала: {ref} — {_shtuk(int(qn))}. Передам заявку менеджеру, он уточнит "
                f"по наличию на сегодня. Что-то ещё?")
    ask = "Сколько упаковок вам нужно?" if p.get("pack_count") else "Сколько вам нужно?"
    price_part = f" — {_price_phrase(p)}" if asked_price else ""
    return f"Есть {ref}{price_part}. {ask}"


_AVAIL_UNKNOWN_PHRASE = ("Я цифровой сотрудник, у меня данные на утро — возможно, за сегодня что-то "
                         "уже произвели или привезли, но я этого не знаю. Давайте запишу ваши пожелания, "
                         "и менеджер свяжется с вами с конкретикой.")


def _product_reply(sid: str, per_item: list[tuple[dict[str, Any], list[dict[str, Any]]]],
                    asked_avail: bool = False, asked_price: bool = False) -> str | None:
    """Детерминированная реплика для ОДИНОЧНОЙ определённой позиции.

    Цену/итог считает и озвучивает КОД по каталогу. Возвращает None — тогда отвечает LLM
    (мультизаказ, позиция без размеров, разные типы одного размера без сигнала выбора).

    Разворот Артёма 22.09.2026: приоритет — принять и записать заявку, а не свериться с наличием.
    Наличие/остаток и подбор БЛИЖАЙШЕГО варианта (когда точного размера/исполнения нет) Вера
    озвучивает только если клиент спросил про наличие/цену НАПРЯМУЮ (asked_avail/asked_price) —
    иначе просто уточняет недостающее (обычно длину) и фиксирует позицию как есть, наличие
    решит менеджер. Исключение — обработка пропиткой (антисептик/огнезащита): по ней всегда
    фиксированный ответ «2-3 дня, под заказ», это не вопрос наличия.
    """
    real = [(it, pr) for (it, pr) in per_item if (it.get("text") or "").strip()]
    if len(real) != 1:
        return None  # 0 или мультизаказ → LLM (раздробит по одной, без цен)
    it, products = real[0]

    # Продолжение разговора по уже обсуждаемому товару: клиент назвал количество/подтвердил.
    # Считаем по той же позиции (locked) — цена в итоге совпадёт с озвученной в предложении.
    locked = LOCKED.get(sid)
    if locked and _continues_locked(it, locked) and not _desc_conflict(it, locked):
        return _present(sid, it, locked, asked_avail=asked_avail, asked_price=asked_price)

    has_dims = any(_req_dims(it))
    if not has_dims:
        # Товар без размеров. Если найденные позиции — аксессуары/крепёж/изоляция (у них вообще нет
        # размеров в каталоге) — авто-расчёта по ним нет (решение Sveta): передаём в сметный отдел.
        # Если же это пиломатериал без названного размера («нужна доска») — у него размеры ЕСТЬ,
        # просто клиент их не назвал → возвращаем None, LLM уточнит параметры.
        accessory = products and all(
            not (p.get("thickness_mm") or p.get("width_mm") or p.get("length_mm") or p.get("diameter_mm"))
            for p in products)
        if accessory:
            name = _type_word(products[0]).capitalize()
            return (f"{name} — это передам в сметный отдел, коллеги посчитают и перезвонят. "
                    f"Что-то ещё подобрать?")
        return None  # «нужна рейка» без размеров → LLM уточнит параметры

    # Только товары, ТОЧНО совпавшие по названным размерам (без «съезда» поиска).
    exact = [p for p in products if _exact_match(p, it)]

    # По умолчанию (клиент не попросил именно сухую) — приоритет НЕ сухой, естественной влажности
    # (п.4 разворота Артёма 22.09.2026). Если сухая всё же выбрана, но строганная/завальцованная
    # не названа явно — приоритет НЕстроганной. Срабатывает только если такой вариант реально есть.
    raw_full = (it.get("raw") or "").lower()
    if exact and "сух" not in raw_full:
        natural_pref = [p for p in exact if "сух" not in (p.get("name") or "").lower()]
        if natural_pref:
            exact = natural_pref
    if exact and not any(w in raw_full for w in ("строган", "завальцован")):
        unplaned_pref = [p for p in exact if not any(w in (p.get("name") or "").lower()
                                                       for w in ("строган", "завальцован"))]
        if unplaned_pref:
            exact = unplaned_pref

    if not exact:
        if not (asked_avail or asked_price):
            # ДЕФОЛТ: наличие не спросили — не сверяемся и не отказываем, просто уточняем
            # недостающее (обычно длину) и фиксируем позицию как есть, дальше решит менеджер.
            if not it.get("length_mm"):
                return ("Поняла, уточните, пожалуйста, длину — запишу заявку, дальше менеджер "
                        "подскажет по наличию.")
            label = it.get("raw") or it.get("text") or "эту позицию"
            return (f"Записала — {label}. Передам заявку менеджеру, он свяжется и уточнит "
                    f"по наличию. Что-то ещё?")
        # Точного размера в наличии нет — предлагаем БЛИЖАЙШИЙ по размеру того же типа
        # (раньше брали первый по остатку — мог увести с 45×145 на 50×150).
        text = (it.get("text") or "").lower()
        type_tok = text.split()[0] if text else ""
        # Широкий пул того же типа, чтобы РАНЖИРОВАТЬ по близости размера, а не по остатку
        # (limit маленький → ближайший размер мог не попасть в выборку и уводил на дальний).
        pool = list(products)
        pool += catalog.search_loose(it.get("text") or "", limit=80)
        seen: set = set()
        pool = [p for p in pool if not (p.get("id") in seen or seen.add(p.get("id")))]
        if type_tok:
            same_type = [p for p in pool if type_tok[:4] in (p.get("name") or "").lower()]
            pool = same_type or pool
        pool = _grade_filter(it, pool)  # уважать названный сорт (А/В) в подборе замены
        # Если уже обсуждаем товар того же сечения (STT повторил неточные размеры/старую длину) —
        # держимся ЕГО (клиент уже принял эту замену), а не выбираем заново другой SKU.
        if locked and _dim_distance_core(it, locked) <= 5.0 and not _desc_conflict(it, locked):
            a = locked
        else:
            a = _nearest_alt(it, pool)
        if a:
            qn = catalog._to_num(it.get("quantity_pieces"))
            pk = catalog._to_num(it.get("packs"))
            # Этот вариант уже предлагали и клиент назвал количество/пачки → сразу считаем, не переспрашиваем.
            if locked and a.get("id") == locked.get("id") and (qn or pk):
                return _present(sid, it, a, asked_avail=asked_avail, asked_price=asked_price)
            LOCKED[sid] = a
            # Запомнить запрошенное количество — чтобы на «да» озвучить итог, а не уйти молча.
            PENDING[sid] = {"quantity_pieces": qn, "packs": pk} if (qn or pk) else {}
            price_part = f", {_price_phrase(a)}" if asked_price else ""
            return f"Точно такого размера сейчас нет. Есть близкое — {_full_ref(a)}{price_part}. Подойдёт?"
        return _AVAIL_UNKNOWN_PHRASE

    # Запрошено особое исполнение (профилированный/сухой/антисептир...) — а его в наличии нет
    # среди точных совпадений: НЕ подменяем молча, предлагаем имеющийся вариант как близкое.
    raw_low = (it.get("raw") or "").lower()
    natural = _wants_natural_moisture(raw_low)
    desc_req = _desc_words_in(raw_low)
    if desc_req or natural:
        with_desc = exact
        if natural:
            with_desc = [p for p in with_desc if "сух" not in (p.get("name") or "").lower()]
        if desc_req:
            with_desc = [p for p in with_desc if all(w[:7] in (p.get("name") or "").lower() for w in desc_req)]
        if not with_desc:
            a = _confident_pick(it, exact)
            LOCKED[sid] = a
            qn = catalog._to_num(it.get("quantity_pieces"))
            pk = catalog._to_num(it.get("packs"))
            PENDING[sid] = {"quantity_pieces": qn, "packs": pk} if (qn or pk) else {}
            # Пропитка биозащитой/огнезащитой — фиксированный ответ (Артём 22.09.2026), не вопрос
            # наличия: всегда «2-3 дня, под заказ», независимо от того, спросили про наличие или нет.
            if any(t in raw_low for t in _TREATMENT_WORDS):
                return (f"Такую обработку — антисептирование или огнезащиту — мы делаем за два-три дня "
                        f"после заказа, это под заказ. Базовый материал — {_full_ref(a)} — в наличии. "
                        f"Оформить заявку с обработкой?")
            if not (asked_avail or asked_price):
                return ("Поняла, записала пожелание по этому исполнению — менеджер уточнит "
                        "по наличию и перезвонит с конкретикой. Что-то ещё?")
            price_part = f", {_price_phrase(a)}" if asked_price else ""
            if natural:
                return (f"Естественной влажности этого размера сейчас нет, только сухая. "
                        f"Есть {_full_ref(a)}{price_part}. Подойдёт?")
            return f"Именно в таком исполнении этого размера сейчас нет. Есть {_full_ref(a)}{price_part}. Подойдёт?"
        exact = with_desc

    # Разные ТИПЫ товара одного размера (доска/планкен/штакетник) без сигнала выбора → LLM уточнит назначение.
    if len(_distinct_types(exact)) > 1 and not _has_disambig(it):
        return None

    p = _locked_or_pick(sid, it, exact)
    if not p:
        return None
    return _present(sid, it, p, asked_avail=asked_avail, asked_price=asked_price)


async def build_reply(session_id: str, transcript: str) -> str:
    """Полный цикл: extract → поиск по каждой позиции → контекст → LLM-ответ.

    Обновляет историю диалога session_id. Может бросить исключение из llm.chat —
    ловит вызывающий (Telegram/HTTP-обёртка).
    """
    sid = str(session_id)

    # ── Быстрый детерминированный ход «имя» (сразу после приветствия) ──────────────
    # Приветствие уже спросило «как могу обращаться?». Первая реплика клиента — имя.
    # Отвечаем мгновенно, БЕЗ двух LLM-вызовов → нет паузы и нет филлера «Секунду».
    # Если клиент сразу про заказ (_maybe_name → None) — уходим в обычный ход.
    if not HISTORY[sid]:
        nm = _maybe_name(transcript)
        if nm is not None:
            if nm:
                NAME[sid] = nm
            greet = f"Очень приятно, {nm}! " if nm else "Очень приятно! "
            # Утверждённое вступление про приём заявки (как в SYSTEM_PROMPT, режим приёма заявок) —
            # говорится ОДИН раз сразу после имени. Раньше быстрый ход отдавал укороченную версию.
            answer = greet + "Расскажите, что вам нужно, — подберу и запишу."
            HISTORY[sid].append({"role": "user", "content": transcript})
            HISTORY[sid].append({"role": "assistant", "content": answer})
            _log_turn(sid, "user", transcript)
            _log_turn(sid, "assistant", answer)
            return answer

    # Реплику клиента журналим ДО вызовов LLM: если провайдер отвалится и ответ
    # уйдёт фолбэком из обёртки, в журнале всё равно будет видно, что клиент сказал
    # (урок 25 плейбука — «наблюдаемая тишина» не должна путаться с настоящей).
    _log_turn(sid, "user", transcript)
    try:
        q = await llm.extract_query(transcript, history=HISTORY[sid][-HISTORY_MAX:])
    except Exception as e:
        log.exception("extract_query failed: %s", e)
        q = {"items": []}
    items = q.get("items") or []
    log.info("sid=%s items=%s", sid, items)

    per_item = [(it, _search_item(it)) for it in items]
    _log_found(sid, per_item)
    low = transcript.lower()
    dstate = DELIVERY[sid]
    stage = dstate.get("stage")

    # ── ДОСТАВКА как ДЕТЕРМИНИРОВАННАЯ state-machine ──────────────────────────────
    # Реплики по доставке (сумма, адрес, дата) бот выдаёт ГОТОВЫМ текстом из кода —
    # нейросеть к цифрам доставки не подпускается (иначе озвучивала выдуманные суммы:
    # «600»/«4000» вместо посчитанных 7000). direct_reply != None → llm.chat пропускается.
    pickup = any(w in low for w in ("самовывоз", "заберу", "забер", "сам приеду", "сам заберу"))
    enter_delivery = ("достав" in low) and stage not in ("await_place", "await_date")
    direct_reply: str | None = None
    context_block = ""

    def _amt(a: int) -> str:
        return f"{a:,}".replace(",", " ") + " рублей"

    # ── Сигнал «это всё / больше ничего» → ПОВТОР заказа с ОБЩЕЙ суммой (просьба Артёма 2026-06-01) ──
    last_assistant = next((m["content"] for m in reversed(HISTORY[sid]) if m.get("role") == "assistant"), "")
    asked_more = ("что-то ещё" in last_assistant.lower()) or ("что-то еще" in last_assistant.lower())
    no_new_item_glob = not any((it.get("text") or "").strip() for it, _ in per_item)
    explicit_done = any(s in low for s in ("это всё", "это все", "больше ничего", "на этом всё",
                                           "на этом все", "ничего больше", "достаточно", "пока всё", "пока все"))
    neg_more = any(w in low for w in ("нет", "не надо", "не нужно", "всё", "все", "закончил", "хватит"))
    done_signal = (no_new_item_glob and bool(ORDER.get(sid)) and len(low) < 40
                   and stage not in ("await_place", "await_date", "await_name", "sms_offered")
                   and not pickup and not enter_delivery
                   and (explicit_done or (asked_more and neg_more)))

    def _ask_name_or_sms(prefix: str) -> str:
        """Следующий шаг закрытия: спросить имя, если ещё не знаем, иначе сразу сводку.

        Имя спрашиваем В КОНЦЕ, а не в приветствии (решение Светы и Артёма 09.10.2026):
        номер звонящего телефония отдаёт сама, а имя в финале не стоит клиенту ожидания.
        """
        if NAME.get(sid):
            DELIVERY[sid]["stage"] = "sms_offered"
            return prefix + " Прислать вам СМС со сводкой заказа — позиции, цену и ссылку на сайт?"
        DELIVERY[sid]["stage"] = "await_name"
        return prefix + " И подскажите, как вас записать в заявке?"

    if pickup and len(low) < 60 and stage != "await_date":
        DELIVERY[sid] = {"stage": "sms_offered", "mode": "pickup"}
        direct_reply = _ask_name_or_sms(
            "Самовывоз бесплатный — со склада в Красном Селе, улица Свободы, дом 44 А.")
    elif stage == "await_name":
        nm = _maybe_name(transcript)
        if nm:
            NAME[sid] = nm
        DELIVERY[sid]["stage"] = "sms_offered"
        direct_reply = ((f"Записала, {nm}. " if nm else "Поняла. ")
                        + "Прислать вам СМС со сводкой заказа — позиции, цену и ссылку на сайт?")
    elif stage == "sms_offered":
        # Клиент ответил на предложение СМС → детерминированное прощание (звонок завершается).
        DELIVERY[sid]["stage"] = "done"
        neg = any(w in low for w in ("нет", "не надо", "не нужно", "не присыл"))
        sms_part = "" if neg else " и пришлю СМС со сводкой заказа"
        direct_reply = (f"Спасибо за обращение! Передаю вашу заявку в сметный отдел на подтверждение заказа"
                        f"{sms_part}. Хорошего дня!" + END_TAG)
    elif done_signal:
        # Повторяем собранный заказ + общая сумма, затем сразу к вопросу доставки.
        summ = _order_summary(sid)
        if summ:
            direct_reply = summ + " Подскажите, нужна доставка или самовывоз со склада в Красном Селе?"
    elif any(w in low for w in _PAINT_WORDS) and stage not in ("await_place", "await_date", "await_name", "sms_offered"):
        # Покраска — отдельная услуга: без поиска цены/количества, заявка менеджеру (просьба Артёма 2026-06-01).
        direct_reply = ("Покраску материала выполняет менеджер по отдельной заявке — я обязательно её "
                        "зафиксирую и передам, он свяжется с вами и всё рассчитает. Подобрать вам сам материал?")
    elif enter_delivery:
        DELIVERY[sid] = {"stage": "await_place", "mode": "delivery"}
        direct_reply = "В какой город или населённый пункт нужно доставить заказ?"
    elif stage == "await_place" and len(low) < 60:
        # Улицу/дом НЕ спрашиваем (просьба Артёма 2026-06-01): достаточно населённого пункта,
        # цена усреднённая по нему. После пункта сразу спрашиваем день доставки.
        # Снимаем ведущий предлог («в Гатчину»→«Гатчину»), чтобы не вышло «доставка в в Гатчину».
        place = re.sub(r"^(в|во|на)\s+", "", transcript.strip(), flags=re.IGNORECASE)
        zone = delivery.zone_from_place(place)
        gkm = None
        # Незнакомый пункт (нет в выверенной таблице, не общий СПб) → геокодинг OSM → зона по расстоянию.
        if zone is None and not delivery.is_spb_generic(place):
            zone, gkm = await delivery.geocode_zone(place)
        if delivery.is_spb_generic(place) and zone is None:
            # Назвали город целиком — переспрашиваем район; со 2-го раза не зацикливаемся.
            tries = dstate.get("place_tries", 0) + 1
            dstate["place_tries"] = tries
            if tries >= 2:
                dstate.update({"place": "Санкт-Петербург", "zone": None, "stage": "await_date"})
                direct_reply = ("Санкт-Петербург большой — точную стоимость доставки рассчитает менеджер. "
                                "На какой день вам нужна доставка?")
            else:
                direct_reply = ("Санкт-Петербург большой, стоимость зависит от района. Подскажите район "
                                "или ближайший населённый пункт — например, Красное Село, Горелово или Пушкин.")
        elif zone is None:
            # Незнакомый/нерасслышанный пункт: один переспрос, потом — на менеджера, без зацикливания.
            tries = dstate.get("place_tries", 0) + 1
            dstate["place_tries"] = tries
            if tries >= 2:
                dstate.update({"place": place, "zone": None, "stage": "await_date"})
                direct_reply = ("Поняла. Точную стоимость доставки по этому пункту рассчитает менеджер. "
                                "На какой день вам нужна доставка?")
            else:
                direct_reply = "Не расслышала пункт. Повторите, пожалуйста, в какой город или посёлок доставить заказ?"
        else:
            vol, maxlen = _order_volume_length(sid)
            # km нужен для дальней зоны (50+ км, тариф за км) — берём из геокодинга, если был.
            amount, note = delivery.compute(maxlen, vol, zone, manip=False, km=gkm) if vol else (None, "no_order")
            dstate.update({"place": place, "zone": zone, "stage": "await_date"})
            if amount is not None:
                dstate["amount"] = amount
                direct_reply = (f"Доставка в {place} — {_amt(amount)}. На какой день вам нужна доставка?")
            else:
                direct_reply = ("Точную стоимость доставки по этому пункту рассчитает менеджер. "
                                "На какой день вам нужна доставка?")
    elif stage == "await_date":
        resolved_date = _resolve_relative_date(transcript)
        dstate.update({"date": transcript.strip(), "date_resolved": resolved_date})
        amt = dstate.get("amount")
        place = dstate.get("place", "")
        # Пункт зачитываем в сводке ТОЛЬКО если он распознан (zone != None). Иначе это мог быть
        # не расслышанный топоним — не выдаём его за факт.
        place_ok = dstate.get("zone") is not None and place
        loc = f"в {place}" if place_ok else "по указанному адресу"
        cost_part = f", стоимость доставки {_amt(amt)}" if amt else ", стоимость доставки рассчитает менеджер"
        # «послезавтра»/«в пятницу» и т.п. — распознаём в реальную календарную дату (МСК), а не
        # только повторяем слова клиента (иначе заявка в сметный отдел уходит без точной даты).
        said = transcript.strip()
        said = said[:1].lower() + said[1:]   # «В пятницу» → «в пятницу»: это середина фразы
        date_part = f"{said} ({resolved_date})" if resolved_date else said
        direct_reply = _ask_name_or_sms(
            f"Записала: доставка {date_part} {loc}{cost_part}.")
    else:
        # Голое подтверждение («да, давайте») к предложенной замене с уже названным количеством →
        # озвучиваем ИТОГ детерминированно (иначе LLM подтвердит без суммы — баг «итог не озвучен»).
        affirm = any(w in low for w in ("да", "давай", "подойд", "хорош", "согла", "беру", "годит", "устра", "ага", "угу")) and len(low) < 30
        no_new_item = not any((it.get("text") or "").strip() for it, _ in per_item)
        # Прямой вопрос про наличие/цену (разворот Артёма 22.09.2026) — иначе Вера в них не лезет.
        asked_avail = _asks_availability(low)
        asked_price = _asks_price(low)
        det = None
        if affirm and no_new_item and LOCKED.get(sid) and PENDING.get(sid):
            p = LOCKED[sid]
            synth = {"text": _type_word(p), "raw": "",
                     "quantity_pieces": PENDING[sid].get("quantity_pieces"),
                     "packs": PENDING[sid].get("packs")}
            if catalog.compute_total(p, synth):
                det = _present(sid, synth, p, asked_avail=asked_avail, asked_price=asked_price)
        # Иначе — обычный детерминированный ход: одиночная определённая позиция считается кодом.
        if det is None:
            det = _product_reply(sid, per_item, asked_avail=asked_avail, asked_price=asked_price)
        if det is not None:
            direct_reply = det
        else:
            # Мультизаказ / уточнение / разные типы — отвечает LLM (без цифр цен, см. промпт).
            context_block = _build_context_block(per_item, asked_avail=asked_avail, asked_price=asked_price)
            for it, products in per_item:
                _record_order_item(sid, it, products)

    if direct_reply is not None:
        # Детерминированный ответ по доставке — нейросеть не вызываем.
        answer = direct_reply
    else:
        msgs = [{"role": "system", "content": prompts.SYSTEM_PROMPT}]
        msgs += HISTORY[sid][-HISTORY_MAX:]
        msgs.append({"role": "user", "content": transcript + "\n\n" + context_block})
        answer = await llm.chat(msgs, temperature=0.3, max_tokens=110)
        answer = delivery.render_tags(answer)  # на случай, если LLM всё же выставит тег

    # В историю кладём БЕЗ служебного END_TAG, чтобы модель не копировала его в будущих репликах.
    clean_for_history, _ = split_end(answer)
    HISTORY[sid].append({"role": "user", "content": transcript})
    HISTORY[sid].append({"role": "assistant", "content": clean_for_history})
    HISTORY[sid] = HISTORY[sid][-HISTORY_MAX:]
    _log_turn(sid, "assistant", clean_for_history)
    return answer
