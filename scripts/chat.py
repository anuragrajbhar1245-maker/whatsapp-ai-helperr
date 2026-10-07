"""Talk to the bot in your terminal, without WhatsApp.

Usage (from the project folder):
    python scripts/chat.py
    python scripts/chat.py --business dental-clinic

It uses the real AI if GEMINI_API_KEY (or ANTHROPIC_API_KEY with LLM_PROVIDER=anthropic,
or OPENAI_BASE_URL with LLM_PROVIDER=omniroute)
is set in your .env file. Without a key it uses a tiny offline demo brain.

Commands inside the chat:
    /owner <text>   send a message as the OWNER (for example: /owner #resume 919000000001)
    /bookings       show saved bookings
    /reset          clear this chat's memory and un-pause it
    /quit           exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # Windows terminals: print Hindi / emoji safely
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

from app.bot import Bot  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.knowledge import list_businesses  # noqa: E402
from app.llm import LLMProvider, get_provider  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.whatsapp import ConsoleMessenger  # noqa: E402

CUSTOMER = "919000000001"
DEMO_OWNER = "919000000099"


class OfflineDemoLLM:
    """Used when no API key is set, so you can still test the flow."""

    name = "offline-demo"

    async def generate(self, system: str, history: list[dict[str, str]]) -> str:
        last = history[-1]["content"] if history else ""
        return ("(Offline demo - no API key set. Set LLM_PROVIDER + key in .env for real answers.)\n"
                f"You said: {last}")


def has_key(settings: Settings) -> bool:
    if settings.llm_provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if settings.llm_provider in ("openai", "omniroute"):
        return bool(settings.openai_base_url)
    return bool(settings.gemini_api_key)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the WhatsApp AI helper in the terminal")
    parser.add_argument("--business", help="business file name, e.g. guest-house")
    parser.add_argument("--debug", action="store_true", help="show logs")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.business:
        settings.active_business = args.business
    settings.owner_whatsapp = settings.owner_whatsapp or DEMO_OWNER
    settings.database_path = str(Path(tempfile.gettempdir()) / f"wa-chat-{uuid.uuid4().hex[:8]}.db")
    setup_logging("INFO" if args.debug else "ERROR")
    if not args.debug:
        logging.getLogger().setLevel(logging.ERROR)

    if settings.active_business not in list_businesses(settings.business_dir):
        print(f"Business '{settings.active_business}' not found. Available: {list_businesses(settings.business_dir)}")
        return

    llm: LLMProvider = get_provider(settings) if has_key(settings) else OfflineDemoLLM()
    messenger = ConsoleMessenger(owner=settings.owner_whatsapp)
    db = Database(settings.database_path)
    bot = Bot(settings=settings, db=db, messenger=messenger, llm=llm)

    print(f"Business: {settings.active_business} | AI: {llm.name} | you are customer +{CUSTOMER}")
    print("Type a message. Commands: /owner <text>, /bookings, /reset, /quit\n")
    owner = bot.owner
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/bookings":
            for b in db.list_bookings():
                print(f"  #{b['id']} {b['name']} | {b['date']} | guests={b['guests']} | service={b['service']}")
            if not db.list_bookings():
                print("  (no bookings yet)")
            continue
        if text == "/reset":
            db.clear_history(CUSTOMER)
            db.resume_chat(CUSTOMER)
            print("  (memory cleared)")
            continue
        if text.startswith("/owner "):
            await bot.handle_message(owner, "text", text[len("/owner "):])
            continue
        await bot.handle_message(CUSTOMER, "text", text)
        if db.is_paused(CUSTOMER):
            print(f"  (bot is paused for this chat - type: /owner #resume {CUSTOMER})")
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
