"""HTTP-API «мозга» Веры для сценария Voximplant (телефония).

Тот же мозг, что и в Telegram-боте (brain.py) — расчёт товара/доставки
детерминированный, LLM = YandexGPT (по .env LLM_PROVIDER).

Запуск:
    cd /root/workspace/azbuka-lesa-bot && venv/bin/python api.py
    (порт по умолчанию 8090, переопределяется env API_PORT)

Защита: заголовок `X-API-Key: <VOX_API_KEY из .env>`. Если VOX_API_KEY в .env
не задан — проверка отключена (тестовый режим).

Эндпоинты:
  GET  /health                         → состояние + дата каталога
  POST /chat   {session_id, text}      → {reply}                (текст→текст, основной для Voximplant)
  POST /reset  {session_id}            → {ok: true}             (сбросить контекст звонка)
  POST /voice  (audio/* в теле, ?session_id=, ?tts=1)
                                        → {transcript, reply, audio_base64:[...]}  (голос→голос, опционально)

Контракт диалога: на каждую реплику клиента слать /chat с одним и тем же
session_id (id звонка) — Вера помнит контекст внутри звонка. В начале нового
звонка можно (не обязательно) дёрнуть /reset.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import brain  # noqa: E402
import catalog  # noqa: E402
import speechkit  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("vera-api")

API_KEY = (os.environ.get("VOX_API_KEY") or "").strip()
API_PORT = int(os.environ.get("API_PORT", "8090"))


def _check_key(request: web.Request) -> None:
    """Бросает HTTPUnauthorized, если ключ задан в .env и не совпал."""
    if not API_KEY:
        return  # тестовый режим — без ключа
    if request.headers.get("X-API-Key", "") != API_KEY:
        raise web.HTTPUnauthorized(text='{"error":"bad api key"}', content_type="application/json")


async def handle_health(request: web.Request) -> web.Response:
    try:
        avail = sum(1 for _ in catalog._connect().execute("SELECT 1 FROM products WHERE count>0"))
    except Exception:
        avail = None
    return web.json_response({
        "status": "ok",
        "catalog_date": catalog.get_yml_date(),
        "products_available": avail,
        "llm_provider": os.environ.get("LLM_PROVIDER", "?"),
    })


async def handle_chat(request: web.Request) -> web.Response:
    _check_key(request)
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"invalid json"}', content_type="application/json")
    session_id = str(data.get("session_id") or "").strip()
    text = (data.get("text") or "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text='{"error":"session_id required"}', content_type="application/json")
    if not text:
        raise web.HTTPBadRequest(text='{"error":"text required"}', content_type="application/json")
    try:
        reply = await brain.build_reply(session_id, text)
    except Exception as e:
        log.exception("build_reply failed: %s", e)
        return web.json_response({"error": "brain_failed", "detail": str(e)}, status=502)
    # Отделяем служебный маркер завершения — телефония по end=true кладёт трубку.
    reply, end = brain.split_end(reply)
    return web.json_response({"reply": reply, "end": end})


async def handle_reset(request: web.Request) -> web.Response:
    _check_key(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text='{"error":"session_id required"}', content_type="application/json")
    brain.reset(session_id)
    return web.json_response({"ok": True})


_TTS_CACHE: dict[str, bytes] = {}        # text → WAV; маленький LRU для повторов (заполнитель)
_TTS_CACHE_MAX = 64


async def handle_tts(request: web.Request) -> web.Response:
    """GET /tts?text=...&key=... → WAV (alena/neutral/+8%).

    Для Voximplant createURLPlayer (он не умеет слать заголовки → ключ в query).
    Повторяющиеся фразы (заполнитель «Секунду, смотрю») кэшируются в памяти.
    """
    if API_KEY:
        key = request.headers.get("X-API-Key") or request.query.get("key", "")
        if key != API_KEY:
            raise web.HTTPUnauthorized(text='{"error":"bad api key"}', content_type="application/json")
    text = (request.query.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(text='{"error":"text query required"}', content_type="application/json")
    wav = _TTS_CACHE.get(text)
    if wav is None:
        try:
            wav = await speechkit.tts_wav(text)
        except Exception as e:
            log.exception("tts_wav failed: %s", e)
            return web.json_response({"error": "tts_failed", "detail": str(e)}, status=502)
        if len(_TTS_CACHE) >= _TTS_CACHE_MAX:
            _TTS_CACHE.pop(next(iter(_TTS_CACHE)))
        _TTS_CACHE[text] = wav
    return web.Response(body=wav, content_type="audio/x-wav",
                        headers={"Cache-Control": "public, max-age=3600"})


_AMBIANCE_PATH = ROOT / "ambiance.wav"
try:
    _AMBIANCE_BYTES = _AMBIANCE_PATH.read_bytes()
except Exception:
    _AMBIANCE_BYTES = b""


async def handle_ambiance(request: web.Request) -> web.Response:
    """GET /ambiance.wav — тихий фоновый луп (офис/клавиатура) для подмешивания в звонок."""
    if not _AMBIANCE_BYTES:
        raise web.HTTPNotFound(text='{"error":"no ambiance file"}', content_type="application/json")
    return web.Response(body=_AMBIANCE_BYTES, content_type="audio/x-wav",
                        headers={"Cache-Control": "public, max-age=86400"})


async def handle_voice(request: web.Request) -> web.Response:
    """Голос→голос: audio/* в теле запроса. STT → мозг → (опц.) TTS.

    Query: session_id (обяз.), tts=1 чтобы вернуть синтез голоса (по умолчанию да).
    """
    _check_key(request)
    session_id = str(request.query.get("session_id") or "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text='{"error":"session_id query param required"}', content_type="application/json")
    want_tts = request.query.get("tts", "1") not in ("0", "false", "no")
    audio = await request.read()
    if not audio:
        raise web.HTTPBadRequest(text='{"error":"empty audio body"}', content_type="application/json")
    try:
        transcript = await speechkit.stt(audio)
    except Exception as e:
        log.exception("STT failed: %s", e)
        return web.json_response({"error": "stt_failed", "detail": str(e)}, status=502)
    if not transcript.strip():
        return web.json_response({"transcript": "", "reply": "", "note": "пустая расшифровка"})
    try:
        reply = await brain.build_reply(session_id, transcript)
    except Exception as e:
        log.exception("build_reply failed: %s", e)
        return web.json_response({"transcript": transcript, "error": "brain_failed", "detail": str(e)}, status=502)
    out: dict = {"transcript": transcript, "reply": reply}
    if want_tts:
        try:
            segments = await speechkit.tts(reply)
            out["audio_base64"] = [base64.b64encode(s).decode("ascii") for s in segments]
            out["audio_format"] = "oggopus"
        except Exception as e:
            log.exception("TTS failed: %s", e)
            out["tts_error"] = str(e)
    return web.json_response(out)


def build_app() -> web.Application:
    app = web.Application(client_max_size=20 * 1024 * 1024)  # до 20 МБ аудио
    app.router.add_get("/health", handle_health)
    app.router.add_post("/chat", handle_chat)
    app.router.add_get("/tts", handle_tts)
    app.router.add_get("/ambiance.wav", handle_ambiance)
    app.router.add_post("/reset", handle_reset)
    app.router.add_post("/voice", handle_voice)
    return app


if __name__ == "__main__":
    log.info("Vera API starting on :%s, catalog=%s, key=%s",
             API_PORT, catalog.get_yml_date(), "set" if API_KEY else "OFF(test)")
    web.run_app(build_app(), host="0.0.0.0", port=API_PORT, access_log=log)
