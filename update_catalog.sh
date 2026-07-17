#!/usr/bin/env bash
# Ежедневное обновление каталога Буки/Веры из YML-фида Битрикса (alyansles.ru).
# Скачивает фид → пересобирает catalog.db → перезапускает ботов (только при успехе).
set -euo pipefail
cd /root/workspace/azbuka-lesa-bot
LOG=/root/workspace/azbuka-lesa-bot/update_catalog.log

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') catalog update ==="
  if ./venv/bin/python data_loader.py; then
    systemctl restart tyos-bot.service vera-bot.service
    echo "OK: catalog rebuilt, bots restarted"
  else
    echo "FAIL: data_loader error — старый catalog.db оставлен, боты НЕ перезапущены"
    exit 1
  fi
} >> "$LOG" 2>&1

# лог не растим бесконечно (15GB-сервер): держим последние 500 строк
tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
