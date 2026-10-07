import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

# Make sure importing app.main (which builds a default app) never touches real files or keys.
_TMP = tempfile.mkdtemp(prefix="wa-test-")
os.environ.update({
    "DATABASE_PATH": str(Path(_TMP) / "import.db"),
    "APP_SECRET": "",
    "ACCESS_TOKEN": "",
    "GEMINI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "LOG_LEVEL": "WARNING",
})

import pytest  # noqa: E402
import respx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import BASE_DIR, Settings  # noqa: E402
from app.main import create_app  # noqa: E402

APP_SECRET = "test-app-secret"
PHONE_NUMBER_ID = "123456789"
OWNER = "919999900000"
CUSTOMER = "919811122233"
GRAPH_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        verify_token="my-verify-token",
        app_secret=APP_SECRET,
        access_token="test-access-token",
        phone_number_id=PHONE_NUMBER_ID,
        llm_provider="gemini",
        gemini_api_key="test-gemini-key",
        gemini_model="gemini-2.5-flash",
        anthropic_api_key="test-anthropic-key",
        active_business="guest-house",
        business_dir=BASE_DIR / "business",
        owner_whatsapp=OWNER,
        database_path=str(tmp_path / "test.db"),
        memory_limit=15,
        pause_hours=12,
        log_level="WARNING",
    )
    base.update(overrides)
    return Settings(**base)


def gemini_reply(text: str) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}


def text_payload(text: str, msg_id: str = "wamid.1", sender: str = CUSTOMER, name: str = "Ravi") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "919000000000", "phone_number_id": PHONE_NUMBER_ID},
                    "contacts": [{"profile": {"name": name}, "wa_id": sender}],
                    "messages": [{"from": sender, "id": msg_id, "timestamp": "1760000000",
                                  "type": "text", "text": {"body": text}}],
                },
            }],
        }],
    }


def media_payload(msg_type: str = "image", msg_id: str = "wamid.media") -> dict:
    p = text_payload("", msg_id)
    msg = p["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg["type"] = msg_type
    msg[msg_type] = {"id": "MEDIA_ID", "mime_type": "image/jpeg"}
    return p


def status_payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "919000000000", "phone_number_id": PHONE_NUMBER_ID},
            "statuses": [{"id": "wamid.out1", "status": "delivered", "timestamp": "1760000001",
                          "recipient_id": CUSTOMER}],
        }}]}],
    }


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Harness:
    """Test client + helpers to post signed webhooks and inspect sent WhatsApp messages."""

    def __init__(self, client: TestClient, router: respx.MockRouter, app):
        self.client = client
        self.router = router
        self.app = app
        self.graph = router.post(GRAPH_URL).respond(200, json={"messages": [{"id": "wamid.out"}]})
        self.gemini = router.post(GEMINI_URL)

    @property
    def db(self):
        return self.app.state.db

    def post(self, payload: dict, signed: bool = True):
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-Hub-Signature-256"] = sign(body)
        return self.client.post("/webhook", content=body, headers=headers)

    def sent_texts(self, to: str | None = None) -> list[str]:
        out = []
        for call in self.graph.calls:
            data = json.loads(call.request.content)
            if data.get("type") == "text" and (to is None or data["to"] == to):
                out.append(data["text"]["body"])
        return out

    def read_receipts(self) -> list[str]:
        return [json.loads(c.request.content)["message_id"] for c in self.graph.calls
                if json.loads(c.request.content).get("status") == "read"]

    def llm_requests(self) -> list[dict]:
        return [json.loads(c.request.content) for c in self.gemini.calls]


@pytest.fixture
def harness_factory(tmp_path):
    created = []

    def factory(**overrides) -> Harness:
        settings = make_settings(tmp_path, **overrides)
        router = respx.mock(assert_all_called=False)
        router.start()
        app = create_app(settings)
        client = TestClient(app)
        h = Harness(client, router, app)
        created.append((router, app))
        return h

    yield factory
    for router, app in created:
        router.stop()
        app.state.db.close()


@pytest.fixture
def h(harness_factory) -> Harness:
    return harness_factory()
