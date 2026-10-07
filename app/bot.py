"""The bot brain: memory, AI reply, bookings, human handoff, owner commands."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database
from .knowledge import build_system_prompt, load_business
from .llm import LLMError, LLMProvider
from .logging_setup import mask_phone
from .whatsapp import Messenger

log = logging.getLogger("bot")

FALLBACK_MESSAGE = (
    "Sorry, abhi thodi technical problem hai 🙏 Please thodi der baad message karein, "
    "ya owner aapko jaldi reply karenge.\n"
    "(Sorry, we have a small technical problem. Please try again in a few minutes.)"
)
MEDIA_REPLY = (
    "Please type your message 🙏 Main abhi sirf text message samajh sakta hoon.\n"
    "(I can only read text messages right now.)"
)
HANDOFF_REPLY = (
    "Theek hai 🙏 Maine owner ko bata diya hai, woh aapse jaldi baat karenge.\n"
    "(Okay, I have informed the owner. They will talk to you soon.)"
)
BOOKING_CONFIRM_SUFFIX = "✅ Booking request #{id} saved. Owner will confirm soon."

ESCALATION_TAG = "[HUMAN_ESCALATION]"
BOOKING_RE = re.compile(r"\[BOOKING\](.*?)\[/BOOKING\]", re.DOTALL | re.IGNORECASE)
# Customer asks for a real person (English / Hinglish / Hindi).
HANDOFF_RE = re.compile(
    r"\b(human|owner|insaan|insan|manager|real person)\b"
    r"|baat\s+karni\s+hai|baat\s+karna\s+hai|baat\s+karwa"
    r"|इंसान|मालिक|बात\s*करनी\s*है|बात\s*करना\s*है",
    re.IGNORECASE,
)

MEDIA_TYPES = {"image", "audio", "video", "document", "sticker", "location", "contacts", "unsupported"}


def normalize_number(number: str, default_cc: str = "91") -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10 and default_cc:
        digits = default_cc + digits
    return digits


def wants_human(text: str) -> bool:
    return bool(HANDOFF_RE.search(text or ""))


def parse_booking(reply: str) -> tuple[dict[str, Any] | None, str]:
    """Find a [BOOKING]{json}[/BOOKING] block. Returns (booking or None, reply without the block)."""
    match = BOOKING_RE.search(reply or "")
    if not match:
        return None, reply
    clean = BOOKING_RE.sub("", reply).strip()
    raw = match.group(1).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("booking_json_invalid", extra={"raw": raw[:300]})
        return None, clean
    if not isinstance(data, dict):
        return None, clean
    booking = {k: str(v).strip() for k, v in data.items() if v is not None and str(v).strip()}
    if not booking.get("name") or not booking.get("date") or not (booking.get("guests") or booking.get("service")):
        log.warning("booking_incomplete", extra={"fields": sorted(booking)})
        return None, clean
    return booking, clean


def format_owner_booking(booking: dict[str, Any], booking_id: int, phone: str, business: str) -> str:
    lines = [f"📅 New booking request #{booking_id} ({business})",
             f"Name: {booking['name']}",
             f"Date: {booking['date']}" + (f" {booking['time']}" if booking.get("time") else "")]
    if booking.get("guests"):
        lines.append(f"Guests: {booking['guests']}")
    if booking.get("service"):
        lines.append(f"Service: {booking['service']}")
    if booking.get("notes"):
        lines.append(f"Notes: {booking['notes']}")
    lines.append(f"Customer WhatsApp: +{phone}")
    return "\n".join(lines)


@dataclass
class Bot:
    settings: Settings
    db: Database
    messenger: Messenger
    llm: LLMProvider

    @property
    def owner(self) -> str:
        return normalize_number(self.settings.owner_whatsapp, self.settings.default_country_code)

    def system_prompt(self) -> str:
        info = load_business(self.settings.business_dir, self.settings.active_business)
        return build_system_prompt(info)

    async def notify_owner(self, text: str) -> None:
        if not self.owner:
            log.warning("owner_whatsapp_not_set", extra={"note": text[:80]})
            return
        await self.messenger.send_text(self.owner, text)

    # ---------- entry point ----------
    async def handle_message(self, phone: str, msg_type: str, text: str = "",
                             message_id: str | None = None, profile_name: str = "") -> None:
        phone = normalize_number(phone, "")
        log.info("message_in", extra={"from": mask_phone(phone), "type": msg_type, "chars": len(text or "")})
        if message_id:
            await self.messenger.mark_read(message_id)

        if msg_type == "text" and phone == self.owner and text.strip().startswith("#"):
            await self.handle_owner_command(text.strip())
            return

        paused = self.db.is_paused(phone, self.settings.pause_hours)

        if msg_type != "text":
            if msg_type in MEDIA_TYPES and not paused:
                await self.messenger.send_text(phone, MEDIA_REPLY)
            return

        text = (text or "").strip()
        if not text:
            return

        if paused:
            # Owner is handling this chat. Remember the message, do not reply.
            self.db.add_message(phone, "user", text, self.settings.memory_limit)
            log.info("chat_paused_skip", extra={"from": mask_phone(phone)})
            return

        if wants_human(text):
            self.db.add_message(phone, "user", text, self.settings.memory_limit)
            await self.escalate(phone, text, profile_name, reason="customer_asked")
            self.db.add_message(phone, "assistant", HANDOFF_REPLY, self.settings.memory_limit)
            await self.messenger.send_text(phone, HANDOFF_REPLY)
            return

        await self.ai_reply(phone, text, profile_name)

    async def ai_reply(self, phone: str, text: str, profile_name: str = "") -> None:
        limit = self.settings.memory_limit
        self.db.add_message(phone, "user", text, limit)
        history = self.db.get_history(phone, limit)
        try:
            reply = await self.llm.generate(self.system_prompt(), history)
        except LLMError as exc:
            log.error("llm_failed_fallback", extra={"error": str(exc), "from": mask_phone(phone)})
            await self.messenger.send_text(phone, FALLBACK_MESSAGE)
            return
        except Exception:  # noqa: BLE001 - never crash the background task
            log.exception("bot_unexpected_error")
            await self.messenger.send_text(phone, FALLBACK_MESSAGE)
            return

        escalate = ESCALATION_TAG in reply
        reply = reply.replace(ESCALATION_TAG, "").strip()

        booking, reply = parse_booking(reply)
        memory_note = ""
        if booking:
            booking_id, created = self.db.save_booking(phone, self.settings.active_business, booking)
            confirm = BOOKING_CONFIRM_SUFFIX.format(id=booking_id)
            reply = f"{reply}\n\n{confirm}" if reply else confirm
            memory_note = f" (booking #{booking_id} saved)"
            if created:
                log.info("booking_saved", extra={"booking_id": booking_id, "from": mask_phone(phone)})
                await self.notify_owner(
                    format_owner_booking(booking, booking_id, phone, self.settings.active_business))

        if escalate:
            await self.escalate(phone, text, profile_name, reason="ai_unsure")
            reply = reply or HANDOFF_REPLY

        if not reply:
            reply = FALLBACK_MESSAGE
        self.db.add_message(phone, "assistant", reply + memory_note, limit)
        await self.messenger.send_text(phone, reply)

    async def escalate(self, phone: str, last_text: str, profile_name: str, reason: str) -> None:
        self.db.pause_chat(phone, reason)
        log.info("chat_escalated", extra={"from": mask_phone(phone), "reason": reason})
        who = f"{profile_name} (+{phone})" if profile_name else f"+{phone}"
        await self.notify_owner(
            f"🙋 Customer needs a human: {who}\n"
            f"Last message: {last_text[:500]}\n"
            f"Bot is paused for this chat. Reply to them from your WhatsApp Business app.\n"
            f"When done, send: #resume {phone}"
        )

    # ---------- owner commands ----------
    async def handle_owner_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower()
        arg = normalize_number(parts[1], self.settings.default_country_code) if len(parts) > 1 else ""
        if cmd == "#resume" and arg:
            was_paused = self.db.resume_chat(arg)
            msg = f"✅ Bot resumed for +{arg}" if was_paused else f"ℹ️ +{arg} was not paused. Bot is active."
        elif cmd == "#pause" and arg:
            self.db.pause_chat(arg, "owner")
            msg = f"⏸️ Bot paused for +{arg}. Send #resume {arg} to turn it back on."
        elif cmd == "#paused":
            paused = self.db.list_paused()
            msg = "Paused chats:\n" + "\n".join(f"+{p}" for p in paused) if paused else "No paused chats."
        else:
            msg = ("Owner commands:\n#resume <number> - turn bot back on for a customer\n"
                   "#pause <number> - stop bot for a customer\n#paused - list paused chats")
        log.info("owner_command", extra={"cmd": cmd, "target": mask_phone(arg)})
        await self.notify_owner(msg)
