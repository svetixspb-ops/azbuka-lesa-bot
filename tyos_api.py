"""HTTP-сервер ИИ-консультанта «Тёс»: отдаёт тестовую страницу с виджетом + чат-API.

Запуск:
    cd /root/workspace/azbuka-lesa-bot && venv/bin/python tyos_api.py
    (порт по умолчанию 8091, env TYOS_PORT)

Эндпоинты:
  GET  /                          → тестовая страница с виджетом (web/index.html)
  GET  /static/*                  → статика виджета
  GET  /health                    → состояние + дата каталога
  POST /chat   {session_id, text} → {reply, chips, lead}
  POST /reset  {session_id}       → {ok: true}
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
load_dotenv(ROOT / ".env")

import catalog  # noqa: E402
import tyos_brain  # noqa: E402
import tyos_handoff  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("tyos-api")

TYOS_PORT = int(os.environ.get("TYOS_PORT", "8091"))


async def handle_index(request: web.Request) -> web.Response:
    f = WEB / "index.html"
    if not f.exists():
        raise web.HTTPNotFound(text="no index")
    return web.Response(body=f.read_bytes(), content_type="text/html", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    try:
        avail = sum(1 for _ in catalog._connect().execute("SELECT 1 FROM products WHERE count>0"))
    except Exception:
        avail = None
    return web.json_response({
        "status": "ok",
        "catalog_date": catalog.get_yml_date(),
        "products_available": avail,
        "max_lead": bool((os.environ.get("MAX_BOT_TOKEN") or "").strip()
                         and ((os.environ.get("MAX_LEAD_USER_ID") or "").strip()
                              or (os.environ.get("MAX_LEAD_CHAT_ID") or "").strip())),
    })


async def handle_chat(request: web.Request) -> web.Response:
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
        out = await tyos_brain.build_reply(session_id, text)
    except Exception as e:
        log.exception("build_reply failed: %s", e)
        return web.json_response({"error": "brain_failed", "detail": str(e)}, status=502)
    return web.json_response(out)


async def handle_reset(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        data = {}
    session_id = str(data.get("session_id") or "").strip()
    if session_id:
        tyos_brain.reset(session_id)
    return web.json_response({"ok": True})


async def handle_handoff(request: web.Request) -> web.Response:
    """GET /handoff/{token} → сохранённый расчёт. Для MAX-бота: поднять контекст
    клиента, перешедшего с сайта (?start=<token>)."""
    token = request.match_info.get("token", "")
    packet = tyos_handoff.get(token)
    if not packet:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(packet)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/chat", handle_chat)
    app.router.add_post("/reset", handle_reset)
    app.router.add_get("/handoff/{token}", handle_handoff)
    if WEB.exists():
        app.router.add_static("/static/", WEB, show_index=False)
    return app


if __name__ == "__main__":
    log.info("Tyos API on :%s, catalog=%s", TYOS_PORT, catalog.get_yml_date())
    web.run_app(build_app(), host="0.0.0.0", port=TYOS_PORT, access_log=None)
