"""Еженедельный аудит диалогов Буки → новые уроки в копилку.

Читает dialogs.jsonl, берёт последние N сессий, просит DeepSeek найти НОВЫЕ уроки
(чего ещё нет в tyos_lessons.md), дописывает их под датой. Бот подхватит уроки
при следующем рестарте (build_system_prompt → load_lessons).

Запуск вручную:   python3 tyos_audit.py [N_сессий]   (по умолчанию 30)
Из cron (раз в неделю):  0 6 * * 1  cd <bot> && venv/bin/python tyos_audit.py >> tyos_audit.log 2>&1
После прогона — перезапустить сервис, чтобы новые уроки попали в промпт.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")  # ключи DeepSeek ДО импорта llm

import llm  # noqa: E402
from tyos_prompts import load_lessons, LESSONS_PATH  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DIALOGS = os.path.join(HERE, "dialogs.jsonl")

AUDIT_PROMPT = """Ты — аналитик качества ИИ-консультанта «Бука» (магазин пиломатериалов «Азбука Леса»).
Ниже — реальные диалоги Буки с клиентами и текущая копилка уроков.

Найди от 0 до 5 НОВЫХ уроков — того, чего ещё НЕТ в копилке:
— где Бука терял или не дожимал клиента, ошибался в цифрах, тоне, сценарии;
— какие ходы, наоборот, сработали и их стоит закрепить;
— что улучшить, чтобы чаще доводить до заявки.

Формат КАЖДОГО урока строго: «- **Тема:** короткое правило в повелительном наклонении».
Только НОВОЕ; не повторяй и не перефразируй существующие уроки. Не выдумывай проблем, которых в диалогах нет.
Если новых уроков нет — верни ровно: НЕТ НОВЫХ

ТЕКУЩАЯ КОПИЛКА УРОКОВ:
{lessons}

ДИАЛОГИ:
{dialogs}
"""


def read_sessions(n_sessions: int):
    sessions: "OrderedDict[str, list]" = OrderedDict()
    try:
        with open(DIALOGS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = str(d.get("session_id", "?"))
                if sid.startswith("navi-test-"):  # тестовые диалоги Нави — не считаем как реальных клиентов
                    continue
                sessions.setdefault(sid, []).append((d.get("role"), d.get("content", "")))
    except FileNotFoundError:
        return []
    return list(sessions.items())[-n_sessions:]


def render(sessions) -> str:
    out = []
    for sid, turns in sessions:
        out.append(f"--- Диалог {sid} ---")
        for role, content in turns:
            who = "Клиент" if role == "user" else ("Бука" if role == "assistant" else str(role))
            out.append(f"{who}: {content}")
    return "\n".join(out)


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    sessions = read_sessions(n)
    if not sessions:
        print("Нет диалогов для аудита.")
        return
    dialogs = render(sessions)[-20000:]  # ограничиваем контекст
    lessons = load_lessons() or "(пусто)"
    prompt = AUDIT_PROMPT.format(lessons=lessons, dialogs=dialogs)
    out = (await llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=700)).strip()

    new_lines = [ln.rstrip() for ln in out.splitlines() if ln.lstrip().startswith("- ")]
    if not new_lines or "НЕТ НОВЫХ" in out.upper():
        print("Новых уроков не найдено.")
        return

    stamp = datetime.date.today().isoformat()
    # РЕЖИМ «ПРЕДЛАГАЕТ → СВЕТА УТВЕРЖДАЕТ»: пишем в pending, живую копилку и бота НЕ трогаем.
    pending = os.path.join(HERE, "tyos_lessons_pending.md")
    block = f"\n## Предложено аудитом {stamp} (НА РЕВЬЮ — не применено)\n" + "\n".join(new_lines) + "\n"
    with open(pending, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"Предложено уроков на ревью: {len(new_lines)} → {os.path.basename(pending)}")
    print("\n".join(new_lines))
    print("\nЭто ПРЕДЛОЖЕНИЯ. В бота попадут только после ОК Светы: перенести нужные строки "
          "в tyos_lessons.md и перезапустить tyos-bot.service.")


if __name__ == "__main__":
    asyncio.run(main())
