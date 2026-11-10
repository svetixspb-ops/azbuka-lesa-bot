#!/usr/bin/env bash
# Сторож голосового робота Веры (azbuka-lesa-bot) — раз в 5 минут проверяет всю
# цепочку звонка и алертит Свете в Telegram, если какое-то звено отвалилось.
#
# Зачем. До 10.11.2026 у Веры не было ни алертов, ни сторожа: она могла молчать
# сутки, и узнали бы об этом от клиента. Авария Yandex SpeechKit 11.10.2026
# вскрылась случайно — Нави полез в логи после рестарта.
#
# Цепочка звонка, и что здесь проверяется:
#   Mango --SIP--> Asterisk --AudioSocket:9092--> мост --HTTP:8090--> мозг --> речь
#   1) регистрация mango-reg в Asterisk (нет её — Манго физически не доведёт звонок)
#   2) сервисы asterisk / vera-bot / vera-sip-bridge живы
#   3) порт моста 9092 слушает (сервис может быть «active» и при зависшем сокете)
#   4) мозг отвечает на /health
#   5) синтез речи работает — РАЗ В ЧАС (а не каждый прогон): на запасном провайдере
#      ElevenLabs символы платные (лимит 30 000/мес), и сторож не должен их съедать.
#
# Алерты: один раз при падении, затем повтор раз в час, пока лежит, и сообщение
# при восстановлении. Тексты без ругани — что сломалось и что это значит.
set -u

APP_DIR=/root/workspace/azbuka-lesa-bot
API_BASE=http://127.0.0.1:8090
BRIDGE_PORT=9092
STATE_DIR=/var/lib/navi
STATE_FILE="$STATE_DIR/vera-health-state.json"
LOG=/var/log/vera-healthcheck.log
ENV_FILE="/root/.claude/channels/telegram/.env"
REALERT_SECONDS=3600
TTS_PROBE_SECONDS=3600
TTS_PROBE_TEXT="проверка"

mkdir -p "$STATE_DIR" 2>/dev/null
exec >>"$LOG" 2>&1
ts="[$(date -u +%FT%TZ)]"
now=$(date +%s)

_send_alert() {
    local TOKEN CHAT
    # Токен берём у бота Нави, а не у @azbukalesa_bot: сторож должен работать и
    # тогда, когда у Веры сломано ровно всё, включая её собственный .env.
    TOKEN=$(grep -oP '(?<=TELEGRAM_BOT_TOKEN=).*' "$ENV_FILE" 2>/dev/null)
    CHAT=$(grep -oP '(?<=^VERA_ALERT_CHAT_IDS=).*' "$APP_DIR/.env" 2>/dev/null)
    [ -z "${CHAT:-}" ] && CHAT="1726068652"
    [ -z "${TOKEN:-}" ] && { echo "$ts no TELEGRAM_BOT_TOKEN, skip alert"; return; }
    local id
    for id in ${CHAT//,/ }; do
        curl -s --max-time 10 "https://api.telegram.org/bot$TOKEN/sendMessage" \
            -d chat_id="$id" -d text="$1" >/dev/null
    done
}

problems=()

# 1) регистрация в телефонии Манго
reg=$(asterisk -rx "pjsip show registrations" 2>/dev/null)
if ! echo "$reg" | grep -q "Registered"; then
    state=$(echo "$reg" | awk '/mango-reg/ {print $3}')
    problems+=("• нет регистрации в Манго (${state:-ответа от Asterisk нет}) — звонки до Веры не доходят")
fi

# 2) сервисы
for unit in asterisk vera-bot vera-sip-bridge; do
    if [ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ]; then
        problems+=("• сервис $unit не запущен")
    fi
done

# 3) порт моста
if ! ss -ltn 2>/dev/null | grep -q ":$BRIDGE_PORT "; then
    problems+=("• SIP-мост не слушает порт $BRIDGE_PORT — Asterisk не сможет отдать ему звук")
fi

# 4) мозг
health=$(curl -s --max-time 15 -w '\n%{http_code}' "$API_BASE/health" 2>&1)
hcode=$(echo "$health" | tail -1)
hbody=$(echo "$health" | sed '$d')
if [ "$hcode" != "200" ] || [ "$(echo "$hbody" | jq -r '.status // empty' 2>/dev/null)" != "ok" ]; then
    problems+=("• мозг Веры не отвечает на /health (http=$hcode)")
fi

# состояние предыдущего прогона
prev_status="ok"; fail_since=0; last_alert=0; last_tts=0
if [ -f "$STATE_FILE" ]; then
    prev_status=$(jq -r '.status // "ok"' "$STATE_FILE" 2>/dev/null || echo ok)
    fail_since=$(jq -r '.fail_since // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    last_alert=$(jq -r '.last_alert // 0' "$STATE_FILE" 2>/dev/null || echo 0)
    last_tts=$(jq -r '.last_tts // 0' "$STATE_FILE" 2>/dev/null || echo 0)
fi

# 5) синтез речи — раз в час и только если мозг жив (иначе проверяем сломанное через сломанное)
if [ ${#problems[@]} -eq 0 ] && [ $((now - last_tts)) -ge "$TTS_PROBE_SECONDS" ]; then
    key=$(grep -oP '(?<=^VOX_API_KEY=).*' "$APP_DIR/.env" 2>/dev/null)
    tcode=$(curl -s --max-time 30 -o /tmp/vera-tts-probe.wav -w '%{http_code}' \
        --get "$API_BASE/tts" --data-urlencode "text=$TTS_PROBE_TEXT" --data-urlencode "key=$key" 2>&1)
    tsize=$(stat -c%s /tmp/vera-tts-probe.wav 2>/dev/null || echo 0)
    if [ "$tcode" = "200" ] && [ "$tsize" -gt 1000 ]; then
        echo "$ts TTS probe ok ($tsize bytes)"
    else
        provider=$(grep -oP '(?<=^SPEECH_PROVIDER=).*' "$APP_DIR/.env" 2>/dev/null)
        problems+=("• синтез речи не работает (http=$tcode, ${tsize}Б, провайдер ${provider:-?}) — Вера возьмёт трубку и будет молчать")
    fi
    rm -f /tmp/vera-tts-probe.wav
    last_tts=$now
fi

if [ ${#problems[@]} -eq 0 ]; then
    status="ok"
    echo "$ts OK"
else
    status="fail"
    echo "$ts FAIL: ${problems[*]}"
fi

if [ "$status" = "fail" ]; then
    body=$(printf '%s\n' "${problems[@]}")
    if [ "$prev_status" = "ok" ]; then
        fail_since=$now
        _send_alert "⚠️ Вера: сломано звено в цепочке звонка.
$body

Клиенты сейчас до робота не дойдут. Проверяю каждые 5 минут, напишу, когда поднимется."
        last_alert=$now
    elif [ $((now - last_alert)) -ge "$REALERT_SECONDS" ]; then
        down_min=$(( (now - fail_since) / 60 ))
        _send_alert "⚠️ Вера всё ещё лежит — уже ${down_min} мин.
$body"
        last_alert=$now
    fi
else
    if [ "$prev_status" = "fail" ]; then
        down_min=$(( (now - fail_since) / 60 ))
        _send_alert "✅ Вера снова в порядке (лежала ~${down_min} мин): регистрация в Манго, мост, мозг и синтез отвечают."
    fi
    fail_since=0
fi

jq -n --arg status "$status" --argjson fail_since "$fail_since" \
      --argjson last_alert "$last_alert" --argjson last_tts "$last_tts" \
    '{status:$status, fail_since:$fail_since, last_alert:$last_alert, last_tts:$last_tts}' > "$STATE_FILE"

tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
