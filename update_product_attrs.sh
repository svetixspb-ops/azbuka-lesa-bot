#!/usr/bin/env bash
# Еженедельное обновление реальных характеристик товаров (карточки сайта) —
# см. scrape_product_attrs.py. В отличие от update_catalog.sh, рестарт ботов
# НЕ нужен: catalog.py читает product_attrs из SQLite при каждом запросе.
set -euo pipefail
cd /root/workspace/azbuka-lesa-bot
LOG=/root/workspace/azbuka-lesa-bot/update_product_attrs.log

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') product attrs update ==="
  ./venv/bin/python scrape_product_attrs.py || echo "FAIL: scrape_product_attrs error (см. вывод выше) — старые данные оставлены"
} >> "$LOG" 2>&1

tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
