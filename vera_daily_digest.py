"""Ежедневный дайджест по звонкам Веры — отчёт в Telegram.

Берёт звонки ЗА ДЕНЬ из vera_dialogs.jsonl (журнал пишет brain._log_turn),
считает детерминированную статистику (звонки, промахи поиска, поправки клиента),
просит DeepSeek/YandexGPT собрать короткий разбор и шлёт его через
@azbukalesa_bot (TELEGRAM_BOT_TOKEN) + прикладывает полные расшифровки файлом.

Сделано по образцу tyos_daily_digest.py (Бука), просьба Артёма 09.10.2026:
«хорошо бы иметь доступ к прослушиванию диалогов, чтобы выискивать косяки».

Получатели: VERA_DIGEST_CHAT_IDS, если задан, иначе ADMIN_IDS.

Из systemd-таймера (ежедневно 08:00 МСК) — vera-daily-digest.{service,timer}.
Запуск вручную:  python3 vera_daily_digest.py [YYYY-MM-DD]   (по умолчанию — вчера по МСК)
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
DIALOGS = os.path.join(HERE, "vera_dialogs.jsonl")
MSK = ZoneInfo("Europe/Moscow")

# Клиент поправляет Веру — самый дешёвый признак, что она услышала/подобрала не то.
_CORRECTION_MARKERS = ("не прямой", "не тот", "не то", "я же", "я просил", "я говорю",
                       "нет, не", "не нужен", "не нужна", "не это", "послушайте",
                       "ещё раз", "еще раз", "повторяю", "посмотрите внимательно")

DIGEST_PROMPT = """Ты — аналитик качества голосового робота «Вера» (приём заявок по телефону,
магазин пиломатериалов «Азбука Леса»). Ниже — реальные телефонные разговоры Веры за {date}.

Расшифровки машинные: в них бывают ошибки распознавания («панкен» вместо «планкен»).
Строки «[поиск]» показывают, что Вера поняла из реплики и что нашла в каталоге — по ним
видно, промахнулась она в распознавании, в понимании или в поиске по каталогу.

Собери короткий отчёт СТРОГО в этом формате (без лишних заголовков):

🔴 КОСЯКИ
(1-3 пункта. Для каждого: что просил клиент → что сделала Вера → на каком шаге сломалось
— расслышала, поняла, нашла в каталоге или сформулировала ответ. Если косяков нет — «Не найдено».)

🟢 ЧТО СРАБОТАЛО
(1-2 пункта. Если данных мало — «Недостаточно данных».)

🎯 ТОП-1 ПРАВКА
(Одна самая важная конкретная правка на завтра. Одно предложение.)

Пиши по-русски, конкретно, без воды и без выдуманных проблем.

РАЗГОВОРЫ:
{dialogs}
"""


def read_sessions_for_date(target_date: datetime.date):
    """Звонки за дату: {session_id: [(role, content, extra), ...]} в порядке появления."""
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
                try:
                    dt = datetime.datetime.fromisoformat(d.get("ts", "")).astimezone(MSK)
                except ValueError:
                    continue
                if dt.date() != target_date:
                    continue
                sid = str(d.get("session_id", "?"))
                if sid.startswith("healthcheck"):   # технические пинги мониторинга — не звонки
                    continue
                sessions.setdefault(sid, []).append((d.get("role"), d.get("content", ""), d))
    except FileNotFoundError:
        return []
    return list(sessions.items())


def render(sessions) -> str:
    out = []
    for sid, turns in sessions:
        out.append(f"--- Звонок {sid} ---")
        for role, content, d in turns:
            if role == "search":
                for it in d.get("items") or []:
                    found = ", ".join(it.get("найдено") or []) or "ничего"
                    line = f"[поиск] «{it.get('запрос')}» → понято {it.get('понято')} → найдено: {found}"
                    if it.get("неточно"):
                        line += f"  (ТОЧНОГО совпадения нет, отброшено: {', '.join(it['неточно'])})"
                    out.append(line)
                continue
            who = {"user": "Клиент", "assistant": "Вера", "error": "ОШИБКА"}.get(role, str(role))
            out.append(f"{who}: {content}")
        out.append("")
    return "\n".join(out)


def hard_stats(sessions) -> list[str]:
    """Детерминированные сигналы — считаются по журналу, не моделью."""
    misses: list[str] = []      # поиск вернул пусто или только неточное совпадение
    corrections: list[str] = []  # клиент поправлял Веру
    errors = 0
    for sid, turns in sessions:
        for role, content, d in turns:
            if role == "search":
                for it in d.get("items") or []:
                    if not it.get("найдено"):
                        misses.append(f"«{it.get('запрос')}» — не найдено ничего")
                    elif it.get("неточно"):
                        misses.append(f"«{it.get('запрос')}» — точного нет, "
                                      f"отброшено: {', '.join(it['неточно'])}")
            elif role == "user":
                low = (content or "").lower()
                if any(m in low for m in _CORRECTION_MARKERS):
                    corrections.append(f"«{content[:90]}»")
            elif role == "error":
                errors += 1
    lines = []
    if misses:
        lines.append(f"Промахи поиска: {len(misses)}")
        lines += [f"  • {m}" for m in misses[:5]]
    if corrections:
        lines.append(f"Клиент поправлял Веру: {len(corrections)}")
        lines += [f"  • {c}" for c in corrections[:5]]
    if errors:
        lines.append(f"Сбоев (нейросеть не ответила): {errors}")
    return lines


def _recipients() -> list[str]:
    raw = (os.environ.get("VERA_DIGEST_CHAT_IDS") or os.environ.get("ADMIN_IDS") or "")
    return [cid.strip() for cid in raw.split(",") if cid.strip()]


async def send_telegram(text: str) -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = _recipients()
    if not (token and chat_ids):
        print("TELEGRAM_BOT_TOKEN/получатели не заданы — дайджест не отправлен.")
        return False
    ok_all = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for chat_id in chat_ids:
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
    chat_ids = _recipients()
    if not (token and chat_ids):
        return False
    ok_all = True
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            for chat_id in chat_ids:
                form = aiohttp.FormData()
                form.add_field("chat_id", chat_id)
                if caption:
                    form.add_field("caption", caption)
                form.add_field("document", content.encode("utf-8"), filename=filename,
                               content_type="text/plain")
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
    header = f"📞 Дайджест Веры за {date_label}\nЗвонков: {len(sessions)}\n"

    if not sessions:
        text = header + "\nЗа этот день звонков не было."
        print(text)
        await send_telegram(text)
        return

    stats = hard_stats(sessions)
    stats_block = ("\n" + "\n".join(stats) + "\n") if stats else ""

    dialogs_full = render(sessions)
    prompt = DIGEST_PROMPT.format(date=date_label, dialogs=dialogs_full[-20000:])
    body = (await llm.chat([{"role": "user", "content": prompt}],
                           temperature=0.3, max_tokens=500)).strip()

    text = header + stats_block + "\n" + body
    print(text)
    ok = await send_telegram(text)
    print("Отправлено в Telegram:" if ok else "НЕ отправлено в Telegram (см. выше)", ok)

    ok_doc = await send_telegram_document(
        f"zvonki_vera_{target_date.isoformat()}.txt", dialogs_full,
        caption=f"Расшифровки звонков Веры за {date_label}")
    print("Файл с расшифровками отправлен:" if ok_doc else "Файл НЕ отправлен", ok_doc)


if __name__ == "__main__":
    asyncio.run(main())
