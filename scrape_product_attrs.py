"""Обход карточек товаров на alyansles.ru — забирает реальные структурированные
характеристики (Влажность / Тех обработка / Поверхность / Порода / Сорт и т.д.),
которых НЕТ в YML-фиде (там только name/description/price/count).

Почему это нужно: 2026-07-24 Бука подписал брус «строганный», хотя в фиде
этого слова не было нигде — карточка товара прямо говорит «Поверхность:
Нестроганная». Обычным текстовым парсингом name/description это не поймать
надёжно, а с карточки — можно, там структурированная таблица.

Как сопоставляем страницу с товаром из фида: на каждой карточке в JS-конфиге
галереи есть 'PRODUCT_TYPE':'1','PRODUCT':{'ID':'<id>' — это ID ровно того же
товара, что id в YML-фиде (проверено вручную на 2 карточках). На странице
бывают и другие ID (блок «с этим товаром покупают») — их размечает другой
паттерн ('SHOW_ADD_BASKET_BTN' перед 'PRODUCT') и мы их отбрасываем.

Список страниц — из https://www.alyansles.ru/sitemapiblock2.xml (единственный
sitemap с /catalog/items/, ~1250 карточек; остальные — новости/акции/статьи).

Не все id из фида получат карточку (цветовые варианты одной модели иногда
делят одну страницу, где primary — только один из них) — это ОК, для
непокрытых id характеристики просто не показываются (текущее безопасное
поведение — не выдумывать).

Запуск вручную:   venv/bin/python scrape_product_attrs.py [--limit N]
Из cron (раз в неделю, характеристики почти не меняются — в отличие от
цены/остатка, которые фид обновляет ежедневно):
  0 4 * * 1  cd <bot> && venv/bin/python scrape_product_attrs.py >> tyos_audit.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime, timezone, timedelta

import aiohttp

import product_attrs

SITEMAP_URL = "https://www.alyansles.ru/sitemapiblock2.xml"
CONCURRENCY = 5
REQUEST_DELAY = 0.3  # секунд между запросами на одного воркера — не долбим чужой сайт
TIMEOUT = 20
MSK = timezone(timedelta(hours=3))

_PRIMARY_ID_RE = re.compile(r"PRODUCT_TYPE':'1','PRODUCT':\{'ID':'(\d+)'")
_TABS3_RE = re.compile(r'id="tabs-3">(.*?)</div>\s*</div>\s*</div>', re.S)
_ROW_RE = re.compile(r'<div class="cell">(.*?)</div>\s*<div class="cell">(.*?)</div>', re.S)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scrape_attrs")


async def fetch_sitemap_urls(session: aiohttp.ClientSession) -> list[str]:
    async with session.get(SITEMAP_URL, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
        xml = await r.text()
    urls = re.findall(r"<loc>(.*?)</loc>", xml)
    return [u for u in urls if "/catalog/items/" in u]


def _parse_page(html: str, url: str) -> dict | None:
    m = _PRIMARY_ID_RE.search(html)
    if not m:
        return None
    pid = m.group(1)
    attrs: dict[str, str] = {}
    tm = _TABS3_RE.search(html)
    if tm:
        for k, v in _ROW_RE.findall(tm.group(1)):
            k, v = k.strip(), v.strip()
            if k and v:
                attrs[k] = v
    return {"id": pid, "url": url, "attrs": attrs,
            "scraped_at": datetime.now(MSK).isoformat(timespec="seconds")}


async def _worker(name: str, queue: "asyncio.Queue[str]", session: aiohttp.ClientSession,
                   results: list[dict], errors: list[str]) -> None:
    while True:
        url = await queue.get()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                if r.status != 200:
                    errors.append(f"{url} -> HTTP {r.status}")
                    continue
                html = await r.text()
            row = _parse_page(html, url)
            if row:
                results.append(row)
            else:
                errors.append(f"{url} -> no primary id found")
        except Exception as e:
            errors.append(f"{url} -> {e}")
        finally:
            await asyncio.sleep(REQUEST_DELAY)
            queue.task_done()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="только первые N страниц (для теста)")
    args = ap.parse_args()

    headers = {"User-Agent": "azbuka-lesa-bot/1.0 (product attrs sync for Buka)"}
    async with aiohttp.ClientSession(headers=headers) as session:
        urls = await fetch_sitemap_urls(session)
        if args.limit:
            urls = urls[: args.limit]
        log.info("Страниц к обходу: %d", len(urls))

        queue: "asyncio.Queue[str]" = asyncio.Queue()
        for u in urls:
            queue.put_nowait(u)

        results: list[dict] = []
        errors: list[str] = []
        workers = [asyncio.create_task(_worker(f"w{i}", queue, session, results, errors))
                   for i in range(CONCURRENCY)]
        await queue.join()
        for w in workers:
            w.cancel()

    product_attrs.save_many(results)
    log.info("Сохранено характеристик: %d из %d страниц (%d ошибок)", len(results), len(urls), len(errors))
    if errors:
        log.info("Первые ошибки:\n%s", "\n".join(errors[:15]))
    log.info("Всего в базе product_attrs: %d", product_attrs.count())


if __name__ == "__main__":
    asyncio.run(main())
