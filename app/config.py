"""App settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Real env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # WhatsApp Cloud API
    verify_token: str = ""
    app_secret: str = ""
    access_token: str = ""
    phone_number_id: str = ""
    graph_api_version: str = "v23.0"

    # AI
    llm_provider: str = "gemini"  # "gemini", "anthropic" or "openai"/"omniroute"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"
    openai_api_key: str = ""
    openai_base_url: str = "http://localhost:20128/v1"
    openai_model: str = "agy/gemini-3.8-flash-high"
    llm_timeout_seconds: float = 25.0

    # Business
    active_business: str = "guest-house"
    business_dir: Path = field(default_factory=lambda: BASE_DIR / "business")
    owner_whatsapp: str = ""
    default_country_code: str = "91"

    # Storage / behaviour
    database_path: str = str(BASE_DIR / "data" / "bot.db")
    memory_limit: int = 15
    pause_hours: int = 12  # auto-resume a paused chat after N hours (0 = never)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(BASE_DIR / ".env")
        return cls(
            verify_token=os.getenv("VERIFY_TOKEN", ""),
            app_secret=os.getenv("APP_SECRET", ""),
            access_token=os.getenv("ACCESS_TOKEN", ""),
            phone_number_id=os.getenv("PHONE_NUMBER_ID", ""),
            graph_api_version=os.getenv("GRAPH_API_VERSION", "v23.0"),
            llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:20128/v1"),
            openai_model=os.getenv("OPENAI_MODEL", "agy/gemini-3.8-flash-high"),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "25")),
            active_business=os.getenv("ACTIVE_BUSINESS", "guest-house"),
            business_dir=Path(os.getenv("BUSINESS_DIR", str(BASE_DIR / "business"))),
            owner_whatsapp=os.getenv("OWNER_WHATSAPP", ""),
            default_country_code=os.getenv("DEFAULT_COUNTRY_CODE", "91"),
            database_path=os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "bot.db")),
            memory_limit=_int("MEMORY_LIMIT", 15),
            pause_hours=_int("PAUSE_HOURS", 12),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
