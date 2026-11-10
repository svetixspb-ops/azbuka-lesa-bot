"""Недельный разбор голосового робота Веры — Свете в Telegram, по понедельникам.

Зачем отдельно от ежедневного дайджеста. Дайджест (vera_daily_digest.py) смотрит один
день и отвечает на вопрос «что сломалось вчера». Недельный разбор отвечает на другой:
«что ломается СИСТЕМАТИЧЕСКИ и чинится один раз». 10.11.2026 Света попросила убрать из
своих чатов поток заявок и оставить разбор раз в неделю — вот он.

Что внутри:
  * цифры считает КОД по журналам (звонки, заявки, промахи поиска, поправки клиента,
    сбои, простои сторожа) — модель их не выдумывает;
  * разбор просим у той же модели, что и в дайджесте (llm.py), но вопрос недельный:
    повторяющиеся причины, а не отдельные случаи;
  * отчёт целиком кладётся файлом в incoming/vera-weekly/ — чтобы Нави мог разобрать
    его своими глазами в ближайшем разговоре, а не только пересказать модель.

Получатели: VERA_WEEKLY_CHAT_IDS → VERA_ALERT_CHAT_IDS → ADMIN_IDS.
Запуск: systemd-таймер vera-weekly-review.{service,timer}, понедельник 09:00 МСК.
Вручную:  venv/bin/python vera_weekly_review.py [YYYY-MM-DD-последний-день]
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import llm                      # noqa: E402
import vera_daily_digest as dd  # noqa: E402

MSK = dd.MSK
LEADS = ROOT / "vera_leads.jsonl"
HEALTH_LOG = Path("/var/log/vera-healthcheck.log")
REPORT_DIR = Path("/root/workspace/agent/incoming/vera-weekly")

WEEKLY_PROMPT = """Ты — аналитик качества голосового робота «Вера» (приём заявок по телефону,
магазин пиломатериалов «Азбука Леса»). Ниже — ЦИФРЫ ЗА НЕДЕЛЮ, посчитанные по журналам,
и расшифровки звонков за тот же период.

Вопрос недельный, не дневной: что ломается ПОВТОРЯЮЩИМСЯ образом и чинится один раз.
Единичный случай упоминай только если он дорогой (потерянная заявка, клиент бросил трубку).

Формат строго такой:

🔁 ПОВТОРЯЮЩИЕСЯ ПРИЧИНЫ
(1-3 пункта. Для каждого: в чём картина за неделю → сколько раз → на каком шаге ломается:
расслышала / поняла / нашла в каталоге / сформулировала. Если не видно — «Не найдено».)

🟢 ЧТО РАБОТАЕТ УСТОЙЧИВО
(1-2 пункта, по цифрам. Мало данных — «Недостаточно данных».)

🎯 ОДНА ПРАВКА НА НЕДЕЛЮ
(Самое дорогое узкое место. Одно предложение, конкретно.)

Без воды, без похвалы, без выдуманных проблем. Если звонков за неделю почти не было —
скажи это прямо и не растягивай отчёт.

ЦИФРЫ:
{stats}

РАЗГОВОРЫ:
{dialogs}
"""


def _period(last_day: datetime.date) -> list[datetime.date]:
    return [last_day - datetime.timedelta(days=i) for i in range(6, -1, -1)]


def _leads_in(period: list[datetime.date]) -> list[dict]:
    """Заявки за период. ts в журнале — строка «ДД.ММ.ГГГГ ЧЧ:ММ МСК»."""
    first, last = period[0], period[-1]
    out = []
    try:
        lines = LEADS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", str(d.get("ts", "")))
        if not m:
            continue
        day = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if first <= day <= last and str(d.get("session_id", "")).startswith("sip-"):
            out.append(d)
    return out


def _downtime(period: list[datetime.date]) -> tuple[int, int]:
    """(сколько прогонов сторожа упало, сколько всего прогонов) за период.

    Сторож пишет по строке на прогон: «[ISO] OK» или «[ISO] FAIL: …». Доля падений —
    самая честная мера «сколько времени Вера была недоступна».
    """
    if not HEALTH_LOG.exists():
        return 0, 0
    first, last = period[0], period[-1]
    fails = total = 0
    for line in HEALTH_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})T", line)
        if not m:
            continue
        day = datetime.date.fromisoformat(m.group(1))
        if not (first <= day <= last):
            continue
        if " OK" in line:
            total += 1
        elif " FAIL" in line:
            total += 1
            fails += 1
    return fails, total


def _journal_failures() -> dict[str, int]:
    """Сбои в живых звонках за 7 дней — из журнала systemd моста.

    Считаем по тем же строкам, на которых стоят мгновенные алерты (vera_alert):
    озвучка, мозг, API. Если journalctl недоступен — просто пусто, отчёт не падает.
    """
    marks = {
        "озвучка не удалась": "сбой озвучки (клиент слышал тишину)",
        "запрос к мозгу не удался": "мозг не ответил",
        "] API ": "API вернул ошибку",
    }
    counts = {v: 0 for v in marks.values()}
    try:
        out = subprocess.run(
            ["journalctl", "-u", "vera-sip-bridge", "--since", "7 days ago", "--no-pager"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return {}
    for line in out.splitlines():
        for mark, label in marks.items():
            if mark in line:
                counts[label] += 1
    return {k: v for k, v in counts.items() if v}


def collect(last_day: datetime.date) -> tuple[str, str, dict]:
    period = _period(last_day)
    all_real, all_tests = [], []
    for day in period:
        real, tests = dd.read_sessions_for_date(day)
        all_real += real
        all_tests += tests

    leads = _leads_in(period)
    fails, total = _downtime(period)
    failures = _journal_failures()
    hard = dd.hard_stats(all_real)

    turns = sum(1 for _, t in all_real for role, _, _ in t if role == "user")
    no_positions = sum(1 for l in leads if not l.get("positions"))
    wants_mgr = sum(1 for l in leads if l.get("wants_manager"))

    lines = [
        f"Период: {period[0].strftime('%d.%m')} — {period[-1].strftime('%d.%m.%Y')}",
        f"Живых звонков: {len(all_real)} (наших прогонов, не в счёт: {len(all_tests)})",
        f"Реплик клиентов: {turns}",
        f"Заявок собрано: {len(leads)}"
        + (f", из них без позиций: {no_positions}" if leads else "")
        + (f", просили менеджера: {wants_mgr}" if wants_mgr else ""),
    ]
    if total:
        pct = 100 * fails / total
        lines.append(f"Доступность (сторож): {total - fails} из {total} проверок в порядке"
                     + (f", падений {fails} ({pct:.1f}%)" if fails else ", падений нет"))
    else:
        lines.append("Доступность: данных сторожа за период нет (поставлен 10.11.2026)")
    if failures:
        lines += [f"Сбоев в звонках — {k}: {v}" for k, v in failures.items()]
    else:
        lines.append("Сбоев в звонках по журналу: нет")
    lines += hard or ["Промахов поиска и поправок клиента по журналу не видно"]

    stats = "\n".join(lines)
    dialogs = dd.render(all_real) if all_real else "(живых звонков за период нет)"
    meta = {"period": period, "calls": len(all_real), "leads": len(leads)}
    return stats, dialogs, meta


async def analyse(stats: str, dialogs: str) -> str:
    prompt = WEEKLY_PROMPT.format(stats=stats, dialogs=dialogs[-60000:])
    try:
        return (await llm.chat([{"role": "user", "content": prompt}],
                               temperature=0.3, max_tokens=1100)).strip()
    except Exception as e:
        return f"(разбор моделью не собрался: {e})"


def clean(text: str) -> str:
    """Убрать markdown: в Telegram он не рендерится, звёздочки видны как мусор
    (правило Светы). Жирное превращаем в кавычки, дефисные пункты — в точки.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"«\1»", text, flags=re.S)
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"(?m)^\s*[-*]\s+", "• ", text)


def _chunks(text: str, limit: int = 3800) -> list[str]:
    """Telegram рубит сообщения длиннее 4096 символов. Режем по пустым строкам,
    чтобы разбор не обрывался на полуслове."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > limit and cur:
            out.append(cur.rstrip())
            cur = ""
        cur += block + "\n\n"
    if cur.strip():
        out.append(cur.rstrip())
    # Абзац длиннее лимита (расшифровка одной длинной реплики) пустыми строками не
    # делится — такой кусок рубим жёстко, иначе Telegram отбросит сообщение целиком.
    hard: list[str] = []
    for part in out:
        while len(part) > limit:
            hard.append(part[:limit])
            part = part[limit:]
        if part:
            hard.append(part)
    return hard


def _recipients() -> list[str]:
    raw = (os.environ.get("VERA_WEEKLY_CHAT_IDS")
           or os.environ.get("VERA_ALERT_CHAT_IDS")
           or os.environ.get("ADMIN_IDS") or "")
    return [c.strip() for c in raw.split(",") if c.strip()]


async def main() -> None:
    last_day = (datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
                else (datetime.datetime.now(MSK) - datetime.timedelta(days=1)).date())
    stats, dialogs, meta = collect(last_day)
    review = clean(await analyse(stats, dialogs))

    p = meta["period"]
    head = f"✨ Вера · разбор недели {p[0].strftime('%d.%m')}—{p[-1].strftime('%d.%m.%Y')}"
    message = f"{head}\n\n{stats}\n\n{review}"

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"REVIEW-{p[-1].isoformat()}.md"
    report.write_text(f"# {head}\n\n## Цифры\n\n{stats}\n\n## Разбор модели\n\n{review}\n\n"
                      f"## Расшифровки\n\n```\n{dialogs}\n```\n", encoding="utf-8")

    # Получателей берём отсюда, а не из dd._recipients: дайджест ходит по ADMIN_IDS
    # (там же Артём), а недельный разбор — просьба Светы и адресован ей.
    dd._recipients = _recipients            # type: ignore[assignment]
    ok = True
    for part in _chunks(message):
        ok = await dd.send_telegram(part) and ok
    print(f"недельный разбор: звонков {meta['calls']}, заявок {meta['leads']}, "
          f"telegram={ok}, файл {report}")


if __name__ == "__main__":
    asyncio.run(main())
