"""AI providers behind one small interface. Plain httpx REST calls, no SDKs."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger("llm")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class LLMError(Exception):
    """Raised when the AI provider fails after retrying."""


class LLMProvider(Protocol):
    name: str

    async def generate(self, system: str, history: list[dict[str, str]]) -> str: ...


def normalize_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Make history safe for strict APIs: start with a user turn, alternate roles
    (merge consecutive same-role messages), drop empty messages."""
    out: list[dict[str, str]] = []
    for msg in history:
        role = "assistant" if msg.get("role") == "assistant" else "user"
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if not out and role == "assistant":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


async def _post_with_retry(url: str, *, headers: dict, payload: dict, timeout: float,
                           provider: str) -> dict:
    """POST with one retry on timeouts, network errors, 429 and 5xx."""
    last_error = "unknown error"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            log.warning("llm_http_error",
                        extra={"provider": provider, "status": resp.status_code, "attempt": attempt})
            if resp.status_code < 500 and resp.status_code != 429:
                break  # bad key / bad request: retry will not help
        except (httpx.HTTPError, ValueError) as exc:
            last_error = repr(exc)
            log.warning("llm_request_failed",
                        extra={"provider": provider, "error": last_error, "attempt": attempt})
    raise LLMError(f"{provider} failed: {last_error}")


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout: float = 25.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def generate(self, system: str, history: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in normalize_history(history)
        ]
        gen_config: dict = {"temperature": 0.4, "maxOutputTokens": 1024}
        if "flash" in self.model:
            gen_config["thinkingConfig"] = {"thinkingBudget": 0}  # fast + cheap for chat
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": gen_config,
        }
        data = await _post_with_retry(
            GEMINI_URL.format(model=self.model),
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            payload=payload, timeout=self.timeout, provider=self.name,
        )
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"gemini returned an unexpected response: {str(data)[:300]}") from exc
        if not text:
            raise LLMError("gemini returned an empty reply")
        return text


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5", timeout: float = 25.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def generate(self, system: str, history: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": 0.4,
            "system": system,
            "messages": normalize_history(history),
        }
        data = await _post_with_retry(
            ANTHROPIC_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=payload, timeout=self.timeout, provider=self.name,
        )
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip()
        except (KeyError, TypeError) as exc:
            raise LLMError(f"anthropic returned an unexpected response: {str(data)[:300]}") from exc
        if not text:
            raise LLMError("anthropic returned an empty reply")
        return text


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model,
                                 settings.llm_timeout_seconds)
    if settings.llm_provider not in ("gemini", ""):
        log.warning("unknown_llm_provider_using_gemini", extra={"provider": settings.llm_provider})
    return GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings.llm_timeout_seconds)
