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
STT_LOG = os.path.join(HERE, "stt_log.jsonl")
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
                if not sid.startswith(("web-", "max-", "vk-")):  # только реальные клиентские каналы, не техпинги/тесты Нави
                    continue
                sessions.setdefault(sid, []).append((d.get("role"), d.get("content", "")))
    except FileNotFoundError:
        return []
    return list(sessions.items())


def count_stt_for_date(target_date: datetime.date) -> int:
    n = 0
    try:
        with open(STT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    dt = datetime.datetime.fromisoformat(d.get("ts", "")).astimezone(MSK)
                except ValueError:
                    continue
                if dt.date() == target_date:
                    n += 1
    except FileNotFoundError:
        return 0
    return n


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
    admin_ids = [cid.strip() for cid in (os.environ.get("ADMIN_IDS") or "").split(",") if cid.strip()]
    if not (token and admin_ids):
        print("TELEGRAM_BOT_TOKEN/ADMIN_IDS не заданы — дайджест не отправлен.")
        return False
    ok_all = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for chat_id in admin_ids:
                async with s.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                if not data.get("ok"):
                    print(f"TG send failed for {chat_id}:", data)
                    ok_all = False
        return ok_all
    except Exception as e:
        print("TG send failed:", e)
        return False


async def send_telegram_document(filename: str, content: str, caption: str = "") -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    admin_ids = [cid.strip() for cid in (os.environ.get("ADMIN_IDS") or "").split(",") if cid.strip()]
    if not (token and admin_ids):
        return False
    ok_all = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for chat_id in admin_ids:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                if caption:
                    form.add_field("caption", caption)
                form.add_field("document", content.encode("utf-8"), filename=filename, content_type="text/plain")
                async with s.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    data = await r.json()
                if not data.get("ok"):
                    print(f"TG document send failed for {chat_id}:", data)
                    ok_all = False
        return ok_all
    except Exception as e:
        print("TG document send failed:", e)
        return False


async def main() -> None:
    if len(sys.argv) > 1:
        target_date = datetime.date.fromisoformat(sys.argv[1])
    else:
        target_date = (datetime.datetime.now(MSK) - datetime.timedelta(days=1)).date()

    sessions = read_sessions_for_date(target_date)
    date_label = target_date.strftime("%d.%m.%Y")
    by_channel = {"сайт": 0, "MAX": 0, "ВК": 0, "?": 0}
    for sid, _ in sessions:
        if sid.startswith("web-"):
            by_channel["сайт"] += 1
        elif sid.startswith("max-"):
            by_channel["MAX"] += 1
        elif sid.startswith("vk-"):
            by_channel["ВК"] += 1
        else:
            by_channel["?"] += 1
    channel_line = ", ".join(f"{k}: {v}" for k, v in by_channel.items() if v)
    stt_count = count_stt_for_date(target_date)
    voice_line = f"\nГолосовых сообщений: {stt_count}" if stt_count else ""
    header = f"📊 Дайджест Буки за {date_label}\nДиалогов: {len(sessions)} ({channel_line}){voice_line}\n"

    if not sessions:
        text = header + "\nЗа этот день диалогов не было."
        print(text)
        await send_telegram(text)
        return

    dialogs_full = render(sessions)
    dialogs = dialogs_full[-20000:]
    prompt = DIGEST_PROMPT.format(date=date_label, dialogs=dialogs)
    body = (await llm.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=500)).strip()

    text = header + "\n" + body
    print(text)
    ok = await send_telegram(text)
    print("Отправлено в Telegram:" if ok else "НЕ отправлено в Telegram (см. выше)", ok)

    ok_doc = await send_telegram_document(f"dialogi_{target_date.isoformat()}.txt", dialogs_full, caption=f"Диалоги за {date_label}")
    print("Файл с диалогами отправлен:" if ok_doc else "Файл с диалогами НЕ отправлен", ok_doc)


if __name__ == "__main__":
    asyncio.run(main())
