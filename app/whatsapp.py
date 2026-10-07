"""WhatsApp Cloud API client (send text, mark as read)."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .logging_setup import mask_phone

log = logging.getLogger("whatsapp")

MAX_TEXT = 4096  # WhatsApp text message limit


class Messenger(Protocol):
    async def send_text(self, to: str, body: str) -> bool: ...

    async def mark_read(self, message_id: str) -> bool: ...


class WhatsAppClient:
    def __init__(self, access_token: str, phone_number_id: str, api_version: str = "v23.0",
                 timeout: float = 15.0) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.timeout = timeout

    async def _post(self, payload: dict) -> bool:
        if not self.access_token or not self.phone_number_id:
            log.warning("whatsapp_not_configured", extra={"payload_type": payload.get("type", "status")})
            return False
        headers = {"Authorization": f"Bearer {self.access_token}"}
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(self.url, json=payload, headers=headers)
                if resp.status_code < 300:
                    return True
                log.error("whatsapp_send_failed",
                          extra={"status": resp.status_code, "body": resp.text[:500], "attempt": attempt})
                if resp.status_code < 500 and resp.status_code != 429:
                    return False  # 4xx (bad token, outside 24h window...) -> retry will not help
            except httpx.HTTPError as exc:
                log.error("whatsapp_http_error", extra={"error": repr(exc), "attempt": attempt})
        return False

    async def send_text(self, to: str, body: str) -> bool:
        body = (body or "").strip()
        if not body:
            return False
        if len(body) > MAX_TEXT:
            body = body[: MAX_TEXT - 1] + "…"
        ok = await self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        })
        log.info("whatsapp_send", extra={"to": mask_phone(to), "ok": ok, "chars": len(body)})
        return ok

    async def mark_read(self, message_id: str) -> bool:
        return await self._post({
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        })


class ConsoleMessenger:
    """Prints messages instead of sending them (used by scripts/chat.py)."""

    def __init__(self, owner: str = "") -> None:
        self.owner = owner
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> bool:
        self.sent.append((to, body))
        who = "OWNER" if to == self.owner else "BOT"
        print(f"\n[{who} <- {to}]\n{body}\n")
        return True

    async def mark_read(self, message_id: str) -> bool:
        return True
