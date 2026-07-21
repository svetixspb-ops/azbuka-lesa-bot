"""Мозг текстового ИИ-консультанта «Бука».

Переиспользует от Веры: llm.extract_query (разбор позиций), catalog.search /
compute_total (поиск + детерминированный расчёт), data_loader (обновление фида).
Своё: текстовая персона, сборка контекста ДАННЫЕ, протокол заявки <<LEAD>>.

Контракт: build_reply(session_id, text) -> {"reply", "chips", "lead"}.
Цифры в ответе берутся из ДАННЫХ (код), не из модели.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Жёсткие таймауты, чтобы один медленный ответ LLM не подвешивал «печатает…».
_EXTRACT_TIMEOUT = 8.0    # разбор позиции — если не успел, просто ищем без него
_CHAT_TIMEOUT = 16.0      # основной ответ — дольше нельзя, иначе клиент ждёт «вечно»

import os

import catalog
import delivery
import llm
import tyos_handoff
import tyos_lead
import tyos_order
from tyos_prompts import build_system_prompt

# Ссылка на MAX-бот Азбуки (появится после создания Артёмом). Пока пусто —
# кнопка «продолжить в MAX» показывает, что расчёт сохранён, и ждёт бота.
MAX_BOT_LINK = (os.environ.get("TYOS_MAX_BOT_LINK") or "").strip()

log = logging.getLogger("tyos.brain")

# Журнал полных диалогов — основа для еженедельного аудита. Одна строка = одна
# реплика (клиент или Бука) с session_id и временем.
_DIALOGS_PATH = Path(__file__).resolve().parent / "dialogs.jsonl"
_MSK = timezone(timedelta(hours=3))


def _log_turn(session_id: str, role: str, content: str) -> None:
    try:
        with open(_DIALOGS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(_MSK).isoformat(timespec="seconds"),
                "session_id": session_id,
                "role": role,
                "content": content,
            }, ensure_ascii=False) + "\n")
    except Exception as e:  # журнал не должен ронять ответ
        log.warning("dialog log failed: %s", e)

_SYSTEM = build_system_prompt()
_HISTORY_MAX = 16  # реплик (user+assistant) храним в контексте
_LEAD_RE = re.compile(r"<<LEAD>>\s*(\{[^{}]*\})", re.DOTALL)

# Сопутствующие товары — ищем в каталоге, если клиент упомянул их в реплике
# (базовые формы, search_loose сам стеммит под падежи).
_SUPPLEMENTARY_KW = ("саморез", "крепёж", "крепеж", "гвозд", "уголок", "пропитк",
                     "антисепт", "грунт", "краск", "лак", "масло")

# Предложение обработки (детерминированно): для пиломатериалов, если её ещё не
# обсуждали. Промт это держит ненадёжно — поэтому решаем кодом.
_PILOMAT_KW = ("доск", "брус", "вагонк", "имитац", "планкен", "рейк", "блок-хаус",
               "блокхаус", "балк")
_OBRAB_KW = ("обработ", "торцов", "строг", "пропитк", "распил", "фрезеров",
             "склей", "антисепт", "огнезащит", "биозащит", "покрас",
             "как есть", "без обработ")
_OBRAB_QUESTION = ("Перед тем как сохранить расчёт — нужна обработка под ваш размер: "
                   "торцовка, строжка или пропитка? Или берёте как есть?")


# Намерение клиента оформить заказ — сигнал показать развилку MAX/телефон.
# «оставить телефон» исключаем: это уже выбор телефонной ветки, не повод для кнопок.
_ORDER_INTENT_KW = ("оформ", "беру", "заказыва", "хочу заказать", "готов заказать",
                    "передайте менеджер", "передать менеджер", "давайте оформ", "оформляем")
_PHONE_RE = re.compile(r"\d[\d\-\s()]{6,}\d")


# Детерминированный захват услуги-обработки из реплики клиента (страховка к set_service).
_SERVICE_MAP = (("пропитк", "пропитка"), ("антисепт", "антисептик"), ("огнезащит", "огнезащита"),
                ("биозащит", "биозащита"), ("торцов", "торцовка"), ("строж", "строжка"),
                ("распил", "распил"), ("фрезеров", "фрезеровка"), ("склей", "склейка"),
                ("покрас", "покраска"))


# Жёсткий отказ — уважаем сразу, не дожимаем (запрет из продающих навыков).
_HARD_REFUSAL_KW = ("нет, спасибо", "нет спасибо", "спасибо, не надо", "не надо пока",
                    "пока не готов", "не интересно", "отказываюсь")
# Мягкое сомнение — повод для ОДНОГО хода ценностью (рычаг №2), не для дропа.
_SOFT_OBJECTION_KW = ("дорого", "дорогова", "дороговато", "много выходит", "многовато",
                      "накладно", "это много", "дороже", "подумаю", "надо подумать",
                      "ещё подумаю", "не сейчас", "пока думаю", "позже", "пока подожду")


def _is_hard_refusal(text: str) -> bool:
    low = text.lower().strip()
    return any(k in low for k in _HARD_REFUSAL_KW)


def _is_soft_objection(text: str) -> bool:
    low = text.lower().strip()
    return any(k in low for k in _SOFT_OBJECTION_KW)


def _is_refusal(text: str) -> bool:
    # обратная совместимость: «жёсткий отказ» = старое поведение дропа
    return _is_hard_refusal(text)


_DELIVERY_PLACE_RE = re.compile(
    r"(?:доставк\w*|привез\w*|довез\w*|везти)\s+(?:в|во|на)\s+(.+?)"
    r"(?:[,.;]|\s+к\s|\s+до\s|\s+на\s+след|\s+к\s+концу|$)", re.IGNORECASE)


def _detect_delivery(text: str) -> tuple[str | None, str | None]:
    """Детерминированно поймать способ получения из реплики (страховка к set_delivery)."""
    low = text.lower()
    if "самовывоз" in low or "заберу сам" in low or "сам заберу" in low or "заберём сам" in low:
        return ("самовывоз", None)
    if "доставк" in low or "привез" in low or "довез" in low:
        m = _DELIVERY_PLACE_RE.search(text)
        place = m.group(1).strip(" .,") if m else None
        return ("доставка", place)
    return (None, None)


def _detected_service(text: str) -> str | None:
    low = text.lower()
    if "не нужн" in low or "без обработ" in low or "как есть" in low or "не надо" in low:
        return None
    for kw, name in _SERVICE_MAP:
        if kw in low:
            return name
    return None


def _order_intent(text: str) -> bool:
    low = text.lower()
    if "оставить телефон" in low:
        return False
    return any(k in low for k in _ORDER_INTENT_KW)


# До фиксации спрашиваем способ получения + срок (вариант 2 Светы), один раз.
_DELIVERY_QUESTION = ("И последнее перед расчётом: заберёте сами со склада (Красное Село, "
                      "ул. Свободы 44А) или нужна доставка — и если доставка, в какой населённый "
                      "пункт? К какому сроку нужно?")


def _needs_delivery(session: dict[str, Any]) -> bool:
    if session.get("delivery_asked"):
        return False
    order = session["order"]
    return bool(order["items"]) and not order.get("delivery")


def _has_phone(text: str) -> bool:
    return bool(_PHONE_RE.search(text))


def _needs_obrabotka(session: dict[str, Any], history: list[dict[str, Any]]) -> bool:
    """Спросить обработку, если: ещё не спрашивали, в заказе есть пиломатериал,
    услуги ещё нет, и клиент сам про обработку не говорил."""
    if session.get("obrab_asked"):
        return False
    order = session["order"]
    if not tyos_order.has_pilomat(order) or order["services"]:
        return False
    user_text = " ".join(m["content"] for m in history if m.get("role") == "user").lower()
    if any(k in user_text for k in _OBRAB_KW):
        return False
    return True

# Память сессий в процессе. session_id -> {"history": [...], "order": {...}}.
_SESSIONS: dict[str, dict[str, Any]] = {}


def _session(session_id: str) -> dict[str, Any]:
    s = _SESSIONS.get(session_id)
    if s is None:
        # Добавляем GREETING в историю как первое сообщение ассистента.
        # Фронтенд показывает его сам — мозг должен «помнить», что уже поздоровался,
        # иначе при первом user-сообщении модель представляется повторно.
        from tyos_prompts import GREETING
        s = {
            "history": [{"role": "assistant", "content": GREETING}],
            "items": [],
            "order": tyos_order.new_order(),
        }
        _SESSIONS[session_id] = s
    return s


def reset(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)


def _remember_item(session: dict[str, Any], product: dict[str, Any],
                   calc: dict[str, Any] | None) -> None:
    """Сложить позицию в заявку, не дублируя по имени (обновляем расчёт)."""
    name = product.get("name")
    for it in session["items"]:
        if (it.get("product") or {}).get("name") == name:
            if calc:
                it["calc"] = calc
            return
    session["items"].append({"product": product, "calc": calc})


async def _build_data_block(text: str, history: list[dict[str, Any]],
                            session: dict[str, Any]) -> str:
    """Найти товары по последней реплике и собрать блок ДАННЫЕ для модели."""
    try:
        extracted = await asyncio.wait_for(llm.extract_query(text, history),
                                           timeout=_EXTRACT_TIMEOUT)
    except (Exception, asyncio.TimeoutError) as e:
        log.warning("extract_query failed/timeout: %s", e)
        extracted = {"items": []}

    items = extracted.get("items") or []
    lines: list[str] = []
    for it in items:
        if not (it.get("text") or it.get("thickness_mm") or it.get("width_mm")
                or it.get("diameter_mm")):
            continue
        results = catalog.search(
            text=it.get("text"),
            thickness_mm=it.get("thickness_mm"),
            width_mm=it.get("width_mm"),
            length_mm=it.get("length_mm"),
            diameter_mm=it.get("diameter_mm"),
            species=it.get("species"),
        )
        raw = it.get("raw") or it.get("text") or "позиция"
        if not results:
            lines.append(f"— По запросу «{raw}»: точного совпадения в каталоге нет "
                         f"(предложи аналог или изготовление под размер, мягко).")
            continue
        best = results[0]
        line = "— " + catalog.format_product_line(best)
        calc = catalog.compute_total(best, it)
        if calc:
            line += f" | расчёт: {calc['how']}"
        if best.get("url"):
            line += f" | страница товара: {best['url']}"
        lines.append(line)
        _remember_item(session, best, calc)
        # ещё 1–2 альтернативы для подбора
        for alt in results[1:3]:
            lines.append("   альтернатива: " + catalog.format_product_line(alt))

    # Сопутствующие товары (крепёж, пропитка, краска) клиент часто спрашивает
    # отдельным вопросом — extract_query их не всегда вытягивает. Ищем по ключевым
    # словам напрямую, чтобы цены/упаковки были РЕАЛЬНЫЕ из каталога, не из памяти.
    low = text.lower()
    for kw in _SUPPLEMENTARY_KW:
        if kw not in low:
            continue
        for p in catalog.search_loose(kw, limit=4):
            sline = "— " + catalog.format_product_line(p)
            if sline not in lines:
                lines.append(sline)

    if not lines:
        return ("ДАННЫЕ: по последней реплике конкретных товарных позиций не распознано. "
                "Если клиент назвал товар — уточни размеры/сорт; цифры не придумывай.")
    date = catalog.get_yml_date()
    head = (f"ДАННЫЕ (актуальные цены и наличие из каталога на {date}; "
            f"используй ТОЛЬКО это для любых цифр):")
    return head + "\n" + "\n".join(lines)


def _strip_markdown(text: str) -> str:
    """Убрать markdown-разметку — виджет показывает текст как есть, звёздочки видны буквально.

    Снимаем **жирный**/*курсив*/__/#, маркеры списков «* »/«- » приводим к «• ».
    """
    text = text.replace("**", "").replace("__", "")
    out_lines = []
    for ln in text.split("\n"):
        s = ln.lstrip()
        indent = ln[:len(ln) - len(s)]
        # заголовки markdown
        while s.startswith("#"):
            s = s[1:].lstrip()
        # маркеры списка → «• »
        if s[:2] in ("- ", "* "):
            s = "• " + s[2:]
        out_lines.append(indent + s)
    text = "\n".join(out_lines)
    # одиночные * вокруг слов (*курсив*) — убрать оставшиеся звёздочки
    text = re.sub(r"\*(\S[^*]*?\S|\S)\*", r"\1", text)
    return text


# Самопредставление и приветствие Буки уже показаны в приветствии (клиентская часть
# виджета). Из ЛЮБОГО ответа сервера вырезаем ведущие предложения-вступления — модель
# норовит повторять их посреди диалога в разных формулировках.
_GREET_KW = ("здравствуйте", "добрый день", "доброе утро", "добрый вечер",
             "приветствую", "доброго времени")


def _is_intro_sentence(s: str) -> bool:
    low = s.lower()
    if any(g in low for g in _GREET_KW):
        return True
    if ("тёс" in low or "тес" in low) and "консультант" in low:
        return True
    if "обратились" in low and ("азбук" in low or "лес" in low):
        return True
    return False


def _strip_reintro(text: str) -> str:
    """Срезать ведущие предложения-вступления (приветствие / «я Бука, ИИ-консультант» /
    «вы обратились в Азбуку Леса») в любых формулировках."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    i = 0
    while i < len(parts) and _is_intro_sentence(parts[i]):
        i += 1
    result = " ".join(parts[i:]).strip()
    return result if result else text


def _handoff_close_text(session: dict[str, Any], session_id: str) -> str:
    """Резюме заказа + предварительный итог (сумма считается кодом) перед выбором.

    Реализует правило «закрывай собранный заказ»: позиции, итог, пометка
    «точную подтвердит менеджер», затем приглашение к выбору MAX/телефон.
    """
    packet = tyos_lead.build_lead(contact={}, items=session["items"], session_id=session_id)
    # В резюме — только позиции с посчитанной суммой (материалы), без «голых» имён
    # (например, банка антисептика, попавшая из услуги-обработки).
    priced = [p for p in packet.get("positions", []) if p.get("how")]
    lines = [f'{p.get("name")}: {p["how"]}' for p in priced]
    if not lines:
        return "Готов сохранить ваш расчёт. Как удобнее продолжить?"
    summary = "Фиксирую заказ: " + "; ".join(lines) + "."
    if packet.get("total"):
        summary += f" Итого ориентировочно {packet['total']:,} ₽".replace(",", " ")
        summary += " — точную сумму и стоимость обработки подтвердит менеджер."
    summary += " Как удобнее продолжить?"
    return summary


def _parse_lead(reply: str) -> tuple[str, dict[str, Any] | None]:
    """Вырезать служебную строку <<LEAD>>{...}. Вернуть (видимый_текст, contact|None)."""
    m = _LEAD_RE.search(reply)
    if not m:
        return reply.strip(), None
    visible = (reply[:m.start()] + reply[m.end():]).strip()
    try:
        contact = json.loads(m.group(1))
        if not isinstance(contact, dict):
            contact = None
    except Exception as e:
        log.warning("lead json parse failed: %s", e)
        contact = None
    return visible, contact


async def _run_tools(messages: list[dict[str, Any]], order: dict[str, Any],
                     force_first_tool: str | None = None) -> str:
    """Цикл function-calling: модель правит заказ через функции, код считает.
    Возвращает финальный текст ответа (после всех вызовов функций).
    force_first_tool — имя функции, которую модель ОБЯЗАНА вызвать на первом раунде
    (страховка: расчёт был, но позиция не занеслась в заказ)."""
    for i in range(5):  # максимум раундов вызовов
        choice: Any = "auto"
        if i == 0 and force_first_tool:
            choice = {"type": "function", "function": {"name": force_first_tool}}
        msg = await llm.chat_tools(messages, tyos_order.TOOLS, tool_choice=choice)
        tcs = msg.get("tool_calls")
        if not tcs:
            return (msg.get("content") or "").strip()
        messages.append(msg)
        for tc in tcs:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = tyos_order.execute(order, fn.get("name", ""), args)
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
        # если задана доставка — посчитать её по тарифу ДО показа снимка,
        # чтобы модель озвучила сумму, а не «уточнит менеджер»
        if order.get("delivery"):
            try:
                await asyncio.wait_for(_resolve_delivery_cost(order), timeout=9)
            except (Exception, asyncio.TimeoutError):
                pass
        # после правок показываем модели актуальный заказ, чтобы озвучила точные числа
        messages.append({"role": "system", "content": tyos_order.render_state(order)})
    return ""  # упёрлись в лимит раундов — отдадим пусто, обработается фолбэком


async def _resolve_delivery_cost(order: dict[str, Any]) -> None:
    """Посчитать стоимость доставки по зоне (как у Веры). Если зона известна и тариф
    есть — примерная сумма; иначе «уточнит менеджер». Геокодинг для незнакомых пунктов."""
    d = order.get("delivery")
    if not d or d.get("method") != "доставка" or not d.get("address") or "est_cost" in d:
        return
    try:
        total_vol = sum((it.get("volume_m3_each") or 0) * it["qty"] for it in order["items"].values())
        lens = [it.get("length_m") for it in order["items"].values() if it.get("length_m")]
        max_len = max(lens) if lens else None
        zone = delivery.zone_from_place(d["address"])
        km = None
        if zone is None:
            zone, km = await delivery.geocode_zone(d["address"])
        amount, _note = delivery.compute(max_len, total_vol, zone, manip=False, km=km)
        d["est_cost"] = amount
        if amount:
            amt = f"{amount:,}".replace(",", " ")
            d["note"] = f"≈ {amt} ₽, точную подтвердит менеджер"
        else:
            d["note"] = "стоимость уточнит менеджер"
    except Exception as e:  # расчёт доставки не должен ронять ответ
        log.warning("delivery cost failed: %s", e)
        d["est_cost"] = None
        d["note"] = "стоимость уточнит менеджер"


_CALC_KW = ("посчита", "рассчита", "расчита", "сколько буд", "сколько выйд", "сколько сто",
            "упаков", "штук", " шт", "куб", "м3", "м³", "погонаж")


def _calc_intent(text: str) -> bool:
    """Реплика похожа на запрос расчёта позиции (есть число + признак товара/количества).
    Используется как условие страховки-форса add_or_update_item при пустом заказе."""
    low = text.lower()
    has_digit = any(c.isdigit() for c in low)
    if not has_digit:
        return False
    if any(k in low for k in _CALC_KW):
        return True
    # размер вида 100x150 / 50х150х6000
    return bool(re.search(r"\d+\s*[xх]\s*\d+", low))


async def build_reply(session_id: str, text: str) -> dict[str, Any]:
    session = _session(session_id)
    history: list[dict[str, Any]] = session["history"]
    order: dict[str, Any] = session["order"]

    _log_turn(session_id, "user", text)
    data_block = await _build_data_block(text, history, session)

    history.append({"role": "user", "content": text})
    extra_ctx: list[dict[str, Any]] = []
    if session.get("delivery_asked") and not order.get("delivery"):
        extra_ctx = [{"role": "system", "content":
                      "⚠️ ЖДЁМ АДРЕС ДОСТАВКИ: клиент отвечает на вопрос про получение. "
                      "Прочти его реплику выше и вызови set_delivery(method=..., address=...) "
                      "прямо сейчас — это обязательный первый вызов функции в этом ответе."}]
    messages = (
        [{"role": "system", "content": _SYSTEM}]
        + history[-_HISTORY_MAX:]
        + [{"role": "system", "content": data_block}]
        + [{"role": "system", "content": tyos_order.render_state(order)}]
        + extra_ctx
    )
    try:
        raw = await asyncio.wait_for(_run_tools(messages, order), timeout=_CHAT_TIMEOUT + 8)
        # Страховка позиций: был явный расчёт (размеры/количество), но модель не занесла
        # позицию в заказ → форсируем вызов add_or_update_item, чтобы заказ не остался
        # пустым (иначе бронь/заявку нечем закрыть). Текст первого ответа сохраняем —
        # он уже содержит верные числа из ДАННЫХ; форс делаем на копии сообщений.
        if not order["items"] and _calc_intent(text):
            forced = await asyncio.wait_for(
                _run_tools(list(messages), order, force_first_tool="add_or_update_item"),
                timeout=_CHAT_TIMEOUT + 8)
            if not raw:
                raw = forced
    except (Exception, asyncio.TimeoutError) as e:
        log.warning("run_tools failed/timeout: %s", e)
        history.pop()  # не сохраняем повисшую реплику
        return {"reply": "Извините, ответ задержался. Повторите, пожалуйста, вопрос — "
                         "или оставьте телефон, и менеджер свяжется.",
                "chips": [], "lead": None, "actions": []}
    if not raw:
        raw = "Секунду, собираю заказ. Уточните, пожалуйста, последнюю позицию."

    # Страховка: если клиент назвал услугу-обработку, а модель не вызвала set_service —
    # фиксируем её в заказе детерминированно, чтобы не потерялась.
    svc = _detected_service(text)
    if svc and not any(s["type"].lower() == svc for s in order["services"]):
        tyos_order.set_service(order, svc)

    # Страховка: поймать способ получения из реплики, если модель не вызвала set_delivery.
    if not order.get("delivery"):
        method, place = _detect_delivery(text)
        if method:
            tyos_order.set_delivery(order, method, place)
        elif session.get("delivery_asked") and not _calc_intent(text):
            # Бот уже спрашивал про доставку, модель не вызвала set_delivery —
            # берём весь текст как адрес (клиент написал просто «Гатчина», «ул. Свободы 5»).
            stripped = text.strip(" .,!?")
            if stripped and 2 < len(stripped) < 120:
                tyos_order.set_delivery(order, "доставка", stripped)

    # Если доставка задана — посчитать её по зоне (как у Веры).
    try:
        await asyncio.wait_for(_resolve_delivery_cost(order), timeout=9)
    except (Exception, asyncio.TimeoutError):
        pass

    visible, contact = _parse_lead(raw)

    # Развилка выдачи итога: бот предлагает MAX или телефон (кнопками).
    actions: list[dict[str, Any]] = []
    handoff = "<<HANDOFF>>" in visible
    visible = visible.replace("<<HANDOFF>>", "").strip()
    low_v = visible.lower()
    if not handoff and ("как удобнее продолжить" in low_v
                        or ("сохранить" in low_v and "расчёт" in low_v and "?" in visible)
                        or ("забронир" in low_v and "?" in visible)):
        handoff = True
    if not handoff and _order_intent(text) and not _has_phone(text) and order["items"]:
        handoff = True
    # Рычаг №2 — работа с сомнением. Мягкое «дорого/подумаю» в ПЕРВЫЙ раз: НЕ дропаем,
    # даём модели сделать ход ценностью (правило 16б в промте). Жёсткое «нет» ИЛИ
    # повторное сомнение после уже сделанного хода — уважаем, дропаем без дожима.
    soft = _is_soft_objection(text) and order["items"]
    hard = _is_hard_refusal(text)
    if soft and not hard and not session.get("objection_handled"):
        session["objection_handled"] = True
        handoff = False  # кнопки не показываем — сейчас ход ценностью, не закрытие
    elif hard or soft:
        handoff = False
        lv = visible.lower()
        if "как удобнее продолжить" in lv or ("сохран" in lv and "расчёт" in lv) or "забронир" in lv:
            visible = ("Хорошо, не тороплю. Расчёт будет у меня — обращайтесь, "
                       "когда будете готовы, помогу с заказом и заявкой.")
    # Страховка (правило 24): не предлагаем «сохранить расчёт», если заказ пуст —
    # значит позиция не зафиксировалась. Просим уточнить, кнопки не показываем.
    if handoff and not order["items"]:
        handoff = False
        visible = ("Чтобы сохранить расчёт, уточните, пожалуйста, что берём — "
                   "назовите позицию и количество, и я соберу заказ.")
    if handoff:
        if _needs_obrabotka(session, history):
            session["obrab_asked"] = True
            visible = _OBRAB_QUESTION
        elif _needs_delivery(session):
            session["delivery_asked"] = True
            visible = _DELIVERY_QUESTION
        else:
            # Полный снимок заказа (состав + услуги + доставка + срок + итог), всё кодом.
            visible = tyos_order.render_summary(order)
            packet = tyos_order.to_packet(order, session_id=session_id)
            token = tyos_handoff.save(packet)
            max_url = (MAX_BOT_LINK + ("&" if "?" in MAX_BOT_LINK else "?")
                       + "start=" + token) if MAX_BOT_LINK else ""
            actions = [
                {"type": "max", "label": "💾 Забронировать и продолжить в MAX",
                 "url": max_url, "token": token},
                {"type": "phone", "label": "📞 Забронировать — оставить телефон"},
            ]

    # Markdown больше НЕ вырезаем — виджет рендерит **жирный** (формат-карточки).
    # Чистим только лишние # и одиночные * на всякий случай.
    visible = visible.replace("#", "").strip()
    visible = _strip_reintro(visible)
    lead_status = None
    if contact and (contact.get("contact") or contact.get("name")):
        lead = tyos_order.to_packet(order, contact=contact, session_id=session_id)
        lead_status = await tyos_lead.deliver(lead)
        log.info("lead delivered: %s", lead_status)

    history.append({"role": "assistant", "content": visible})
    _log_turn(session_id, "assistant", visible)
    return {"reply": visible, "chips": [], "lead": lead_status, "actions": actions}
