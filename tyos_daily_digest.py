"""Ежедневный дайджест по работе Буки — отчёт Свете в Telegram.

Берёт диалоги ЗА СЕГОДНЯ (МСК), просит DeepSeek собрать короткий отчёт
(сколько диалогов, слабые места, что сработало, топ-1 правка) и шлёт его
через @azbukalesa_bot (TELEGRAM_BOT_TOKEN) на ADMIN_IDS. Ничего не меняет
в боте и в копилке уроков — это отдельный информационный канал.

Из cron (ежедневно):  0 5 * * *  cd <bot> && venv/bin/python tyos_daily_digest.py >> tyos_audit.log 2>&1
(05:00 UTC = 08:00 МСК — сводка за вчерашний день целиком)

Запуск вручную:  python3 tyos_daily_digest.py [YYYY-MM-DD]  (по умолчанию — вчера по МСК)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import llm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DIALOGS = os.path.join(HERE, "dialogs.jsonl")
MSK = ZoneInfo("Europe/Moscow")

DIGEST_PROMPT = """Ты — аналитик качества ИИ-консультанта «Бука» (магазин пиломатериалов «Азбука Леса»).
Ниже — реальные диалоги Буки с клиентами за {date}.

Собери короткий ежедневный отчёт для владелицы, СТРОГО в этом формате (без лишних заголовков):

🔴 СЛАБЫЕ МЕСТА
(1-3 пункта: где Бука терял клиента, ошибался в цифрах/тоне/сценарии, не дожимал до заявки. Если проблем не было — напиши "Не найдено".)

🟢 ЧТО СРАБОТАЛО
(1-2 пункта: удачные ходы, которые стоит закрепить. Если день был без диалогов или без явных удач — напиши "Недостаточно данных".)

🎯 ТОП-1 ПРАВКА
(Одна САМАЯ важная и конкретная правка на завтра — в промпт, сценарий или процесс. Одно предложение.)

Пиши по-русски, конкретно, без воды, без выдумывания проблем, которых нет в диалогах.

ДИАЛОГИ:
{dialogs}
"""


def read_sessions_for_date(target_date: datetime.date):
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
                ts = d.get("ts", "")
                try:
                    dt = datetime.datetime.fromisoformat(ts).astimezone(MSK)
                except ValueError:
                    continue
                if dt.date() != target_date:
                    continue
                sid = str(d.get("session_id", "?"))
                sessions.setdefault(sid, []).append((d.get("role"), d.get("content", "")))
    except FileNotFoundError:
        return []
    return list(sessions.items())


def render(sessions) -> str:
    out = []
    for sid, turns in sessions:
        out.append(f"--- Диалог {sid} ---")
        for role, content in turns:
            who = "Клиент" if role == "user" else ("Бука" if role == "assistant" else str(role))
            out.append(f"{who}: {content}")
    return "\n".join(out)


async def send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    admin_ids = (os.environ.get("ADMIN_IDS") or "").strip()
    chat_id = admin_ids.split(",")[0].strip() if admin_ids else ""
    if not (token and chat_id):
        print("TELEGRAM_BOT_TOKEN/ADMIN_IDS не заданы — дайджест не отправлен.")
        return False
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
        if not data.get("ok"):
            print("TG send failed:", data)
            return False
        return True
    except Exception as e:
        print("TG send failed:", e)
        return False


async def main() -> None:
    if len(sys.argv) > 1:
        target_date = datetime.date.fromisoformat(sys.argv[1])
    else:
        target_date = (datetime.datetime.now(MSK) - datetime.timedelta(days=1)).date()

    sessions = read_sessions_for_date(target_date)
    date_label = target_date.strftime("%d.%m.%Y")
    header = f"📊 Дайджест Буки за {date_label}\nДиалогов: {len(sessions)}\n"

    if not sessions:
        text = header + "\nЗа этот день диалогов не было."
        print(text)
        await send_telegram(text)
        return

    dialogs = render(sessions)[-20000:]
    prompt = DIGEST_PROMPT.format(date=date_label, dialogs=dialogs)
    body = (await llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=500)).strip()

    text = header + "\n" + body
    print(text)
    ok = await send_telegram(text)
    print("Отправлено в Telegram:" if ok else "НЕ отправлено в Telegram (см. выше)", ok)


if __name__ == "__main__":
    asyncio.run(main())
