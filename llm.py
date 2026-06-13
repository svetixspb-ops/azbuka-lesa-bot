"""LLM-обёртка: DeepSeek (dev) или YandexGPT (prod). Провайдер выбирается через LLM_PROVIDER."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()

# Транзиентные статусы, на которых имеет смысл повторить запрос (DeepSeek 503 «Service too busy» и т.п.)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_ATTEMPTS = 3

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
YANDEX_GPT_MODEL = os.environ.get("YANDEX_GPT_MODEL", "yandexgpt")
# Быстрая модель для extract (разбор реплики в JSON) — заметно меньше латентность.
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "yandexgpt-lite" if LLM_PROVIDER == "yandexgpt" else "deepseek-chat")
YANDEX_GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


async def _deepseek_chat(messages: list[dict[str, Any]], *, temperature: float, max_tokens: int, model: str | None = None) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY не задан")
    async with httpx.AsyncClient(timeout=60) as cx:
        r = await cx.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": model or DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        r.raise_for_status()
        data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


async def _yandexgpt_chat(messages: list[dict[str, Any]], *, temperature: float, max_tokens: int, model: str | None = None) -> str:
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        raise RuntimeError("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
    ya_messages = [{"role": m["role"], "text": m["content"]} for m in messages]
    async with httpx.AsyncClient(timeout=60) as cx:
        r = await cx.post(
            YANDEX_GPT_URL,
            headers={
                "Authorization": f"Api-Key {YANDEX_API_KEY}",
                "x-folder-id": YANDEX_FOLDER_ID,
            },
            json={
                "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{model or YANDEX_GPT_MODEL}/latest",
                "completionOptions": {
                    "stream": False,
                    "temperature": temperature,
                    "maxTokens": str(max_tokens),
                },
                "messages": ya_messages,
            },
        )
        r.raise_for_status()
        data = r.json()
    return (data["result"]["alternatives"][0]["message"]["text"] or "").strip()


async def chat(messages: list[dict[str, Any]], *, temperature: float = 0.4, max_tokens: int = 600, model: str | None = None) -> str:
    """Chat-completion через выбранный провайдер (LLM_PROVIDER) с авто-ретраем на транзиентных сбоях.

    messages = [{"role", "content"}, ...]. model — переопределить модель (напр. быстрая
    «yandexgpt-lite» для extract). На 429/5xx и сетевых таймаутах повторяет
    до _RETRY_ATTEMPTS раз с короткой паузой — чтобы единичный DeepSeek 503 не ронял звонок.
    """
    fn = _yandexgpt_chat if LLM_PROVIDER == "yandexgpt" else _deepseek_chat
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await fn(messages, temperature=temperature, max_tokens=max_tokens, model=model)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _RETRY_STATUSES:
                raise
            last_exc = e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
        if attempt < _RETRY_ATTEMPTS - 1:
            await asyncio.sleep(0.8 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


_ITEM_KEYS = ("raw", "text", "thickness_mm", "width_mm", "length_mm", "diameter_mm",
              "species", "grade", "target_m3", "quantity_pieces", "packs")
# Числовые поля — приводим к числу (LLM иногда отдаёт «100 мм», «10 штук», «2,5»).
_NUM_KEYS = ("thickness_mm", "width_mm", "length_mm", "diameter_mm", "target_m3", "quantity_pieces", "packs")


def _coerce_num(v: Any) -> float | int | None:
    if v is None or isinstance(v, (int, float)):
        return v
    import re
    m = re.search(r"\d+(?:[.,]\d+)?", str(v))
    if not m:
        return None
    f = float(m.group().replace(",", "."))
    return int(f) if f.is_integer() else f


async def chat_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     *, temperature: float = 0.3, max_tokens: int = 700,
                     tool_choice: Any = "auto") -> dict[str, Any]:
    """Chat-completion с function-calling (DeepSeek, OpenAI-совместимый формат).

    Возвращает СООБЩЕНИЕ ассистента целиком: {"role", "content", "tool_calls"?}.
    Если модель решила вызвать функции — в tool_calls список вызовов; контент
    при этом может быть пустым. Ретраи на транзиентных сбоях.
    """
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY не задан")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=60) as cx:
                r = await cx.post(
                    f"{DEEPSEEK_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            return data["choices"][0]["message"]
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in _RETRY_STATUSES:
                raise
            last_exc = e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
        if attempt < _RETRY_ATTEMPTS - 1:
            await asyncio.sleep(0.8 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def extract_query(utterance: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Извлечь все товарные позиции из реплики клиента.

    history (последние реплики диалога) даётся для склейки позиции между репликами:
    тип товара назван раньше, размеры/количество — сейчас. Возвращает
    {"items": [ {ключи из _ITEM_KEYS}, ... ]}. Если товар не назван — items=[].
    """
    from prompts import EXTRACT_QUERY_PROMPT
    context = ""
    if history:
        lines = [
            ("Клиент" if m.get("role") == "user" else "Вера") + ": " + str(m.get("content", ""))
            for m in history[-4:]
        ]
        context = ("\nНедавний диалог (для контекста; не извлекай завершённые позиции повторно, "
                   "только дополни последнюю):\n" + "\n".join(lines) + "\n")
    raw = await chat(
        [
            {"role": "system", "content": "Отвечай строго одним JSON-объектом, без markdown, без комментариев."},
            {"role": "user", "content": EXTRACT_QUERY_PROMPT.format(utterance=utterance, context=context)},
        ],
        temperature=0.0,
        max_tokens=400,
        model=EXTRACT_MODEL,
    )
    # Защита от ``` обёрток если LLM решит обернуть
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except Exception:
        return {"items": []}

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {"items": []}

    norm: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        entry = {k: it.get(k) for k in _ITEM_KEYS}
        entry["text"] = entry.get("text") or ""
        for k in _NUM_KEYS:
            entry[k] = _coerce_num(entry.get(k))
        norm.append(entry)
    return {"items": norm}
