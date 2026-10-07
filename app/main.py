"""FastAPI app: WhatsApp webhook + health check."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .bot import Bot
from .config import Settings
from .db import Database
from .knowledge import BusinessNotFound, load_business
from .llm import LLMProvider, get_provider
from .logging_setup import setup_logging
from .whatsapp import Messenger, WhatsAppClient

log = logging.getLogger("app")


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def extract_messages(payload: dict[str, Any], phone_number_id: str = "") -> list[dict[str, Any]]:
    """Pull incoming customer messages out of a webhook payload. Status updates are ignored."""
    found: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            meta_id = (value.get("metadata") or {}).get("phone_number_id")
            if phone_number_id and meta_id and meta_id != phone_number_id:
                continue  # message for another number on the same app
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                     for c in value.get("contacts", []) or []}
            for msg in value.get("messages", []) or []:
                sender = msg.get("from", "")
                found.append({
                    "id": msg.get("id", ""),
                    "from": sender,
                    "type": msg.get("type", "unknown"),
                    "text": (msg.get("text") or {}).get("body", "") if msg.get("type") == "text" else "",
                    "name": names.get(sender, ""),
                })
    return found


def create_app(settings: Settings | None = None, messenger: Messenger | None = None,
               llm: LLMProvider | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_level)

    db = Database(settings.database_path)
    messenger = messenger or WhatsAppClient(settings.access_token, settings.phone_number_id,
                                            settings.graph_api_version)
    bot = Bot(settings=settings, db=db, messenger=messenger, llm=llm or get_provider(settings))

    if not settings.app_secret:
        log.warning("APP_SECRET is empty: webhook signatures are NOT checked. Set it in production!")
    try:
        load_business(settings.business_dir, settings.active_business)
    except BusinessNotFound as exc:
        log.error("business_file_missing", extra={"error": str(exc)})

    app = FastAPI(title="WhatsApp AI Helper", version="1.0.0")
    app.state.settings = settings
    app.state.db = db
    app.state.bot = bot

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "whatsapp-ai-helper", "status": "ok"}

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            load_business(settings.business_dir, settings.active_business)
            business_ok = True
        except BusinessNotFound:
            business_ok = False
        return JSONResponse({
            "status": "ok",
            "business": settings.active_business,
            "business_file_ok": business_ok,
            "llm_provider": bot.llm.name,
            "whatsapp_configured": bool(settings.access_token and settings.phone_number_id),
            "signature_check": bool(settings.app_secret),
        })

    @app.get("/webhook")
    async def verify_webhook(
        mode: str | None = Query(None, alias="hub.mode"),
        token: str | None = Query(None, alias="hub.verify_token"),
        challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        if mode == "subscribe" and settings.verify_token and token == settings.verify_token:
            log.info("webhook_verified")
            return PlainTextResponse(challenge or "")
        log.warning("webhook_verify_failed", extra={"mode": mode})
        raise HTTPException(status_code=403, detail="Verification failed")

    @app.post("/webhook")
    async def receive_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        body = await request.body()
        if settings.app_secret:
            if not verify_signature(settings.app_secret, body, request.headers.get("X-Hub-Signature-256")):
                log.warning("webhook_bad_signature")
                raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        queued = 0
        for msg in extract_messages(payload, settings.phone_number_id):
            if not msg["id"] or not msg["from"]:
                continue
            if not db.mark_processed(msg["id"]):
                log.info("duplicate_message_skipped", extra={"message_id": msg["id"]})
                continue
            background.add_task(bot.handle_message, msg["from"], msg["type"], msg["text"],
                                msg["id"], msg["name"])
            queued += 1
        return JSONResponse({"status": "ok", "queued": queued})

    return app


app = create_app()
