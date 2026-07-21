#!/usr/bin/env bash
# Ежедневный аудит диалогов Буки в режиме «предлагает → Света утверждает» (было еженедельно, с 21.07 — ежедневно).
# Пишет ПРЕДЛОЖЕНИЯ уроков в tyos_lessons_pending.md. Живую копилку (tyos_lessons.md)
# и сервис НЕ трогает — применяются только после ревью Светы (перенести одобренные
# строки в tyos_lessons.md + systemctl restart tyos-bot.service).
set -e
cd /root/workspace/azbuka-lesa-bot
venv/bin/python tyos_audit.py 60 >> tyos_audit.log 2>&1
echo "$(date -Is) audit (propose-only) done -> tyos_lessons_pending.md" >> tyos_audit.log
