"""A/B-тест извлечения товарных позиций: DeepSeek vs Qwen vs YandexGPT.

Прогоняет один и тот же EXTRACT_QUERY_PROMPT на наборе реалистичных реплик
с эталонными разметками (gold) и считает:
  - точность по полям (толщина/ширина/длина/диаметр/сорт/порода/кол-во)
  - совпадение числа позиций
  - латентность

Бот НЕ трогается — это отдельный скрипт. Ключи берутся из .env / окружения.
Запуск:  venv/bin/python ab_test_qwen.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

# Грузим .env вручную (без зависимости от python-dotenv)
def _load_env() -> None:
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

from prompts import EXTRACT_QUERY_PROMPT  # noqa: E402

# ── Провайдеры ───────────────────────────────────────────────────────────────
# Каждый — OpenAI-совместимый /chat/completions, кроме Yandex.

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "key": os.environ.get("DEEPSEEK_API_KEY", ""),
    },
    "qwen": {
        # International endpoint Alibaba Model Studio (DashScope), OpenAI-режим
        "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        "model": os.environ.get("QWEN_MODEL", "qwen-plus"),
        "key": os.environ.get("QWEN_API_KEY", ""),
    },
}


async def _openai_chat(prov: dict[str, str], messages: list[dict[str, Any]]) -> str:
    async with httpx.AsyncClient(timeout=90) as cx:
        r = await cx.post(
            f"{prov['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {prov['key']}"},
            json={"model": prov["model"], "messages": messages, "temperature": 0.0, "max_tokens": 400},
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _parse_items(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def _coerce_num(v: Any) -> float | int | None:
    import re
    if v is None or isinstance(v, (int, float)):
        return v
    m = re.search(r"\d+(?:[.,]\d+)?", str(v))
    if not m:
        return None
    f = float(m.group().replace(",", "."))
    return int(f) if f.is_integer() else f


# ── Gold-набор: реалистичные реплики лесоторговли ────────────────────────────
# expected: список позиций. None в поле = «должно быть null».
# text_kw — подстроки, любая из которых должна встретиться в найденном text (lower).
NUM_FIELDS = ("thickness_mm", "width_mm", "length_mm", "diameter_mm", "target_m3", "quantity_pieces", "packs")

GOLD: list[dict[str, Any]] = [
    {
        "utterance": "Нужна доска обрезная сорок на сто пятьдесят, длина шесть метров, кубов пять",
        "items": [{"text_kw": ["доск"], "thickness_mm": 40, "width_mm": 150, "length_mm": 6000, "target_m3": 5}],
    },
    {
        "utterance": "Брус сто на сто, три метра, штук двадцать",
        "items": [{"text_kw": ["брус"], "thickness_mm": 100, "width_mm": 100, "length_mm": 3000, "quantity_pieces": 20}],
    },
    {
        "utterance": "Вагонка осина сорт Б, четыре упаковки",
        "items": [{"text_kw": ["вагонк"], "species": "осина", "grade": "Б", "packs": 4}],
    },
    {
        "utterance": "Дайте брус сто на сто пятьдесят шесть метров и ещё вагонку осина сорт Б четыре упаковки",
        "items": [
            {"text_kw": ["брус"], "thickness_mm": 100, "width_mm": 150, "length_mm": 6000},
            {"text_kw": ["вагонк"], "species": "осина", "grade": "Б", "packs": 4},
        ],
    },
    {
        "utterance": "Евровагонка, толщина двенадцать с половиной",
        "items": [{"text_kw": ["евровагонк", "вагонк"], "thickness_mm": 12.5, "width_mm": 96}],
    },
    {
        "utterance": "Столб диаметром сто, длина три метра, восемь штук",
        "items": [{"text_kw": ["столб"], "diameter_mm": 100, "length_mm": 3000, "quantity_pieces": 8}],
    },
    {
        "utterance": "Имитация бруса лиственница, двадцать на сто сорок, длина шесть",
        "items": [{"text_kw": ["имитац"], "species": "лиственница", "thickness_mm": 20, "width_mm": 140, "length_mm": 6000}],
    },
    {
        "utterance": "Здравствуйте, а что у вас вообще есть из досок?",
        "items": [],
    },
    {
        "utterance": "Да, давайте, пришлите в телеграм",
        "items": [],
    },
    {
        "utterance": "Террасная доска сорок пять на сто сорок, длина шесть мм",  # «мм» — ошибка STT, это метры
        "items": [{"text_kw": ["террас"], "thickness_mm": 45, "width_mm": 140, "length_mm": 6000}],
    },
    {
        "utterance": "Половая доска сосна, толщина тридцать шесть, ширина сто тридцать пять, два с половиной куба",
        "items": [{"text_kw": ["половая", "доск"], "species": "сосна", "thickness_mm": 36, "width_mm": 135, "target_m3": 2.5}],
    },
    {
        "utterance": "Планкен скошенный, двадцать на девяносто, шесть метров, тридцать штук",
        "items": [{"text_kw": ["планкен"], "thickness_mm": 20, "width_mm": 90, "length_mm": 6000, "quantity_pieces": 30}],
    },
    # Склейка из контекста: тип назван раньше, размеры — сейчас
    {
        "history": [
            {"role": "user", "content": "Мне нужна имитация бруса"},
            {"role": "assistant", "content": "Хорошо, какие размеры?"},
        ],
        "utterance": "Толщина двадцать, ширина сто восемьдесят, шесть метров",
        "items": [{"text_kw": ["имитац"], "thickness_mm": 20, "width_mm": 180, "length_mm": 6000}],
    },
    {
        "utterance": "Брусок пятьдесят на пятьдесят, три метра",
        "items": [{"text_kw": ["брусок"], "thickness_mm": 50, "width_mm": 50, "length_mm": 3000}],
    },
    {
        "utterance": "Нет, это всё, спасибо",
        "items": [],
    },
    {
        "utterance": "Доска сухая строганая сосна двадцать на девяносто шесть метров и блок-хаус тридцать шесть на сто сорок",
        "items": [
            {"text_kw": ["доск"], "species": "сосна", "thickness_mm": 20, "width_mm": 90, "length_mm": 6000},
            {"text_kw": ["блок", "хаус"], "thickness_mm": 36, "width_mm": 140},
        ],
    },
]


def score_items(expected: list[dict], got: list[dict]) -> dict[str, Any]:
    """Сравнить эталон и ответ. Возвращает счётчики верных/всего по полям + флаг совпадения числа позиций."""
    # нормализуем числовые поля в ответе
    got_norm = []
    for it in got:
        if not isinstance(it, dict):
            continue
        e = dict(it)
        for k in NUM_FIELDS:
            e[k] = _coerce_num(e.get(k))
        got_norm.append(e)

    count_ok = len(expected) == len(got_norm)
    correct = total = 0
    notes: list[str] = []

    for i, exp in enumerate(expected):
        g = got_norm[i] if i < len(got_norm) else {}
        # text по ключевым словам
        total += 1
        gt = str(g.get("text", "")).lower()
        if any(kw in gt for kw in exp["text_kw"]):
            correct += 1
        else:
            notes.append(f"#{i} text='{gt}' ∌ {exp['text_kw']}")
        # числовые + сорт/порода
        for k in (*NUM_FIELDS, "species", "grade"):
            if k not in exp:
                continue
            total += 1
            want = exp[k]
            have = g.get(k)
            if isinstance(want, str):
                ok = isinstance(have, str) and want.lower() == have.lower()
            else:
                ok = want == have
            if ok:
                correct += 1
            else:
                notes.append(f"#{i} {k}: want={want} got={have!r}")
    return {"count_ok": count_ok, "correct": correct, "total": total, "notes": notes}


async def run_provider(name: str) -> dict[str, Any]:
    prov = PROVIDERS[name]
    if not prov["key"]:
        return {"name": name, "skipped": "нет API-ключа"}

    field_correct = field_total = 0
    count_ok = 0
    lat: list[float] = []
    fails: list[str] = []

    for case in GOLD:
        context = ""
        if case.get("history"):
            lines = [("Клиент" if m["role"] == "user" else "Вера") + ": " + m["content"] for m in case["history"]]
            context = "\nНедавний диалог:\n" + "\n".join(lines) + "\n"
        prompt = EXTRACT_QUERY_PROMPT.format(utterance=case["utterance"], context=context)
        messages = [
            {"role": "system", "content": "Отвечай строго одним JSON-объектом, без markdown, без комментариев."},
            {"role": "user", "content": prompt},
        ]
        t0 = time.monotonic()
        try:
            raw = await _openai_chat(prov, messages)
        except Exception as e:
            fails.append(f"«{case['utterance'][:40]}…»: {type(e).__name__} {e}")
            continue
        lat.append(time.monotonic() - t0)
        got = _parse_items(raw)
        sc = score_items(case["items"], got)
        field_correct += sc["correct"]
        field_total += sc["total"]
        count_ok += 1 if sc["count_ok"] else 0
        for n in sc["notes"]:
            fails.append(f"«{case['utterance'][:40]}…» {n}")

    return {
        "name": name,
        "model": prov["model"],
        "field_acc": field_correct / field_total if field_total else 0,
        "field_correct": field_correct,
        "field_total": field_total,
        "count_ok": count_ok,
        "count_total": len(GOLD),
        "lat_avg": sum(lat) / len(lat) if lat else 0,
        "lat_max": max(lat) if lat else 0,
        "fails": fails,
    }


async def main() -> None:
    names = [n for n in PROVIDERS if PROVIDERS[n]["key"]]
    print(f"Провайдеры с ключами: {names or '—'}")
    results = await asyncio.gather(*(run_provider(n) for n in PROVIDERS))

    print("\n" + "=" * 60)
    print(f"{'Провайдер':<12}{'Модель':<16}{'Поля':>10}{'Позиции':>10}{'Лат.ср':>9}{'Лат.макс':>10}")
    print("-" * 60)
    for r in results:
        if r.get("skipped"):
            print(f"{r['name']:<12}{'— пропущен (' + r['skipped'] + ')'}")
            continue
        acc = f"{r['field_acc']*100:.0f}% ({r['field_correct']}/{r['field_total']})"
        cnt = f"{r['count_ok']}/{r['count_total']}"
        print(f"{r['name']:<12}{r['model']:<16}{acc:>10}{cnt:>10}{r['lat_avg']:>8.1f}s{r['lat_max']:>9.1f}s")
    print("=" * 60)

    for r in results:
        if r.get("skipped") or not r.get("fails"):
            continue
        print(f"\n── Расхождения {r['name']} ({len(r['fails'])}):")
        for f in r["fails"][:40]:
            print("  ·", f)


if __name__ == "__main__":
    asyncio.run(main())
