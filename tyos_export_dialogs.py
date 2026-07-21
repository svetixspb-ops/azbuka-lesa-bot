"""Экспорт диалогов Буки в читаемый текстовый файл — для передачи клиенту (Артёму).

Запуск:  python3 tyos_export_dialogs.py [YYYY-MM-DD] [YYYY-MM-DD]
  Один аргумент — экспорт за этот день.
  Два аргумента — диапазон [от, до] включительно.
  Без аргументов — весь журнал целиком.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import OrderedDict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DIALOGS = os.path.join(HERE, "dialogs.jsonl")
MSK = ZoneInfo("Europe/Moscow")


def load(date_from: datetime.date | None, date_to: datetime.date | None):
    sessions: "OrderedDict[str, list]" = OrderedDict()
    with open(DIALOGS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("ts", "")
            try:
                dt = datetime.datetime.fromisoformat(ts).astimezone(MSK)
            except ValueError:
                continue
            if date_from and dt.date() < date_from:
                continue
            if date_to and dt.date() > date_to:
                continue
            sid = str(d.get("session_id", "?"))
            if sid.startswith("navi-test-"):  # тестовые диалоги Нави — не путать с реальными клиентами
                continue
            sessions.setdefault(sid, []).append((dt, d.get("role"), d.get("content", "")))
    return sessions


def render(sessions) -> str:
    out = []
    for sid, turns in sessions.items():
        first_ts = turns[0][0].strftime("%d.%m.%Y %H:%M")
        out.append(f"===== Диалог {sid} ({first_ts} МСК) =====\n")
        for dt, role, content in turns:
            who = "Клиент" if role == "user" else ("Бука" if role == "assistant" else str(role))
            out.append(f"[{dt.strftime('%H:%M:%S')}] {who}: {content}")
        out.append("")
    return "\n".join(out)


def main():
    date_from = date_to = None
    if len(sys.argv) == 2:
        date_from = date_to = datetime.date.fromisoformat(sys.argv[1])
    elif len(sys.argv) >= 3:
        date_from = datetime.date.fromisoformat(sys.argv[1])
        date_to = datetime.date.fromisoformat(sys.argv[2])

    sessions = load(date_from, date_to)
    text = render(sessions)
    label = "all"
    if date_from and date_to and date_from == date_to:
        label = date_from.isoformat()
    elif date_from or date_to:
        label = f"{date_from or '..'}_{date_to or '..'}"
    out_path = os.path.join(HERE, f"dialogs_export_{label}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Диалоги Буки ({label}) — всего {len(sessions)}\n\n" + text)
    print(f"Экспортировано {len(sessions)} диалогов -> {out_path}")


if __name__ == "__main__":
    main()
