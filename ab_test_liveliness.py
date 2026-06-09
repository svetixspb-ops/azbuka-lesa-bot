"""A/B-тест ЖИВОСТИ РЕЧИ Веры: DeepSeek vs Qwen.

В отличие от ab_test_qwen.py (точность извлечения), здесь сравниваем генерацию
ответных реплик Веры — тепло, разговорность, краткость, соблюдение правил.
Воспроизводит реальную сборку из brain.build_reply:
  [system SYSTEM_PROMPT] + история + (реплика клиента + блок каталога),
  temperature=0.3, max_tokens=110.

«Живость» субъективна — поэтому скрипт:
  • печатает ответы обоих движков бок о бок (финальная оценка — за ухом Светы)
  • считает объективное: число слов (для телефона важна краткость) и авто-флаги
    нарушений правил (цена названа? markdown? повторное «здравствуйте»?).

Запуск:  QWEN_API_KEY=... venv/bin/python ab_test_liveliness.py
Результат также пишется в liveliness_result.txt для отправки в Telegram.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx


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
from prompts import SYSTEM_PROMPT  # noqa: E402

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "key": os.environ.get("DEEPSEEK_API_KEY", ""),
    },
    "qwen": {
        "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        "model": os.environ.get("QWEN_MODEL", "qwen-plus"),
        "key": os.environ.get("QWEN_API_KEY", ""),
    },
}


async def _chat(prov: dict[str, str], messages: list[dict[str, Any]]) -> str:
    async with httpx.AsyncClient(timeout=90) as cx:
        r = await cx.post(
            f"{prov['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {prov['key']}"},
            json={"model": prov["model"], "messages": messages, "temperature": 0.3, "max_tokens": 110},
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()


# ── Сценарии: каждый мирроринг реального вызова build_reply ───────────────────
# history — предыдущие реплики; user — текущая реплика клиента; ctx — блок каталога,
# который система подставляет (то, что Вера «видит» о наличии).
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "Клиент представился (тёплое вступление о приёме заявки)",
        "history": [],
        "user": "Здравствуйте, меня зовут Сергей.",
        "ctx": "[Найденных позиций нет — клиент ещё не назвал товар]",
    },
    {
        "name": "Общий запрос без размеров (один тёплый уточняющий вопрос)",
        "history": [{"role": "user", "content": "Меня зовут Сергей."},
                    {"role": "assistant", "content": "Очень приятно, Сергей! Сейчас сотрудники не могут ответить, я приму заявку и передам в сметный отдел. Что вам нужно?"}],
        "user": "Нужна вагонка.",
        "ctx": "[Клиент назвал только общий тип «вагонка» без размеров. Не перечисляй виды — задай ОДИН уточняющий вопрос про назначение или размеры.]",
    },
    {
        "name": "Позиция в наличии (подтвердить + спросить количество, БЕЗ цены)",
        "history": [{"role": "user", "content": "Нужна имитация бруса лиственница."}],
        "user": "Толщина двадцать, ширина сто сорок шесть, длина шесть метров.",
        "ctx": "Найдено по запросу «имитация бруса лиственница 20×146×6000»:\n- Имитация бруса, лиственница, 20×146×6000, сорт АВ — В НАЛИЧИИ (цену назовёт система)\n[Позиция есть. Подтверди коротко и спроси количество. Цену НЕ называй.]",
    },
    {
        "name": "Жалоба (признать неудобство + сразу к делу)",
        "history": [],
        "user": "Я вам три раза звонил, никто трубку не берёт! Безобразие.",
        "ctx": "[Клиент жалуется. Коротко признай неудобство и сразу предложи помочь.]",
    },
    {
        "name": "«Вы бот?» (честно, без ярлыка «бот»)",
        "history": [{"role": "user", "content": "Нужна доска."},
                    {"role": "assistant", "content": "Поняла. Подскажите, какая толщина и длина нужны?"}],
        "user": "А вы вообще живой человек или робот?",
        "ctx": "[Вопрос про природу собеседника. Ответь честно как цифровой сотрудник, без слова «бот», и верни к делу.]",
    },
    {
        "name": "Точного размера нет (тёплая альтернатива, не футболить в смету)",
        "history": [{"role": "user", "content": "Нужен планкен двадцать на сто пятьдесят."}],
        "user": "Двадцать на сто пятьдесят, шесть метров.",
        "ctx": "Найдено по запросу «планкен 20×150×6000»:\n- Точного 20×150 НЕТ.\n- Есть близкое: планкен скошенный, 20×145×6000 — В НАЛИЧИИ.\n[Точного размера нет. Предложи близкий вариант конкретно, БЕЗ цены. Не передавай в смету.]",
    },
    {
        "name": "Закрытие — клиент сказал «это всё» (ровно ОДИН шаг: доставка)",
        "history": [{"role": "user", "content": "Брус сто на сто, три метра, двадцать штук."},
                    {"role": "assistant", "content": "Поняла, брус сто на сто, три метра, двадцать штук. Что-то ещё?"}],
        "user": "Нет, это всё.",
        "ctx": "[Заказ собран. Вопрос доставки ещё не решён. Сделай РОВНО один шаг — спроси самовывоз или доставка со склада Красное Село. Прощание и сводку НЕ добавляй в эту же реплику.]",
    },
]

PRICE_RE = re.compile(r"\b\d[\d\s]*\s*(?:руб|₽|рубл)", re.IGNORECASE)
MARKDOWN_RE = re.compile(r"[*#`]|^\s*[-•]\s", re.MULTILINE)
GREET_RE = re.compile(r"\bздравствуйте\b|\bдобрый день\b|меня зовут вера", re.IGNORECASE)
GOODBYE_RE = re.compile(r"хорошего дня|всего доброго|до свидания|всего хорошего", re.IGNORECASE)


def flags(scn: dict[str, Any], text: str) -> list[str]:
    out: list[str] = []
    words = len(re.findall(r"\S+", text))
    if PRICE_RE.search(text):
        out.append("⚠ назвал ЦЕНУ (нельзя — это зона системы)")
    if MARKDOWN_RE.search(text):
        out.append("⚠ markdown/список (текст идёт в озвучку)")
    if GREET_RE.search(text):
        out.append("⚠ повторное приветствие/представление")
    # Для сценария закрытия: прощание в той же реплике, что и вопрос доставки — склейка шагов
    if scn["name"].startswith("Закрытие") and GOODBYE_RE.search(text):
        out.append("⚠ склеил прощание с шагом доставки")
    if words > 45:
        out.append(f"⚠ длинно для телефона ({words} слов)")
    return out


async def gen(name: str, scn: dict[str, Any]) -> tuple[str, float]:
    prov = PROVIDERS[name]
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += scn["history"]
    msgs.append({"role": "user", "content": scn["user"] + "\n\n" + scn["ctx"]})
    t0 = time.monotonic()
    try:
        txt = await _chat(prov, msgs)
    except Exception as e:
        return f"[ОШИБКА {type(e).__name__}: {e}]", 0.0
    return txt, time.monotonic() - t0


async def main() -> None:
    have = [n for n in PROVIDERS if PROVIDERS[n]["key"]]
    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit(f"Тест живости речи Веры — провайдеры: {have}")
    lat: dict[str, list[float]] = {n: [] for n in have}
    wc: dict[str, list[int]] = {n: [] for n in have}
    flagcount: dict[str, int] = {n: 0 for n in have}

    for scn in SCENARIOS:
        emit("\n" + "═" * 64)
        emit(f"📞 {scn['name']}")
        emit(f"   Клиент: «{scn['user']}»")
        results = await asyncio.gather(*(gen(n, scn) for n in have))
        for n, (txt, dt) in zip(have, results):
            words = len(re.findall(r"\S+", txt))
            fl = flags(scn, txt)
            lat[n].append(dt)
            wc[n].append(words)
            flagcount[n] += len(fl)
            emit(f"\n  ── {n.upper()} ({words} слов, {dt:.1f}с):")
            emit(f"     {txt}")
            for f in fl:
                emit(f"       {f}")

    emit("\n" + "═" * 64)
    emit("ИТОГ:")
    for n in have:
        avg_w = sum(wc[n]) / len(wc[n]) if wc[n] else 0
        avg_l = sum(lat[n]) / len(lat[n]) if lat[n] else 0
        emit(f"  {n:<10} ср.слов/реплику={avg_w:.0f}  ср.латентность={avg_l:.1f}с  авто-флагов={flagcount[n]}")

    with open(os.path.join(os.path.dirname(__file__), "liveliness_result.txt"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
