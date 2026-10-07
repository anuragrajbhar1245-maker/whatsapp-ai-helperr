import json

import httpx

from tests.conftest import (CUSTOMER, gemini_reply, media_payload, sign, status_payload,
                            text_payload)


# ---------- GET /webhook verification ----------

def test_verify_ok(h):
    r = h.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "my-verify-token",
                                         "hub.challenge": "12345"})
    assert r.status_code == 200
    assert r.text == "12345"


def test_verify_wrong_token(h):
    r = h.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                         "hub.challenge": "12345"})
    assert r.status_code == 403


def test_verify_wrong_mode(h):
    r = h.client.get("/webhook", params={"hub.mode": "unsubscribe", "hub.verify_token": "my-verify-token",
                                         "hub.challenge": "1"})
    assert r.status_code == 403


def test_verify_rejected_when_verify_token_not_configured(harness_factory):
    h = harness_factory(verify_token="")
    r = h.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "",
                                         "hub.challenge": "1"})
    assert r.status_code == 403


# ---------- signature ----------

def test_signature_valid(h):
    h.gemini.respond(200, json=gemini_reply("Namaste! Kaise madad karun?"))
    r = h.post(text_payload("hello"))
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "queued": 1}
    assert h.sent_texts(CUSTOMER) == ["Namaste! Kaise madad karun?"]


def test_signature_invalid(h):
    body = json.dumps(text_payload("hello")).encode()
    r = h.client.post("/webhook", content=body,
                      headers={"X-Hub-Signature-256": sign(body, "wrong-secret")})
    assert r.status_code == 403
    assert not h.gemini.called
    assert not h.graph.called


def test_signature_missing(h):
    r = h.post(text_payload("hello"), signed=False)
    assert r.status_code == 403


def test_signature_tampered_body(h):
    body = json.dumps(text_payload("hello")).encode()
    header = sign(body)
    tampered = body.replace(b"hello", b"HACKED")
    r = h.client.post("/webhook", content=tampered, headers={"X-Hub-Signature-256": header})
    assert r.status_code == 403


def test_signature_skipped_when_app_secret_empty(harness_factory):
    h = harness_factory(app_secret="")
    h.gemini.respond(200, json=gemini_reply("Hi!"))
    r = h.post(text_payload("hello"), signed=False)
    assert r.status_code == 200
    assert h.sent_texts(CUSTOMER) == ["Hi!"]


def test_invalid_json(h):
    body = b"not json"
    r = h.client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 400


# ---------- dedupe / ignore ----------

def test_dedupe_same_message_id(h):
    h.gemini.respond(200, json=gemini_reply("Reply"))
    r1 = h.post(text_payload("hello", msg_id="wamid.SAME"))
    r2 = h.post(text_payload("hello", msg_id="wamid.SAME"))
    assert r1.json()["queued"] == 1
    assert r2.json()["queued"] == 0
    assert h.gemini.call_count == 1
    assert h.sent_texts(CUSTOMER) == ["Reply"]


def test_status_updates_ignored(h):
    r = h.post(status_payload())
    assert r.status_code == 200
    assert r.json()["queued"] == 0
    assert not h.graph.called
    assert not h.gemini.called


def test_media_gets_polite_reply(h):
    r = h.post(media_payload("image"))
    assert r.status_code == 200
    texts = h.sent_texts(CUSTOMER)
    assert len(texts) == 1
    assert "please type your message" in texts[0].lower()
    assert not h.gemini.called


def test_reaction_ignored_silently(h):
    h.post(media_payload("reaction"))
    assert h.sent_texts() == []


def test_marks_message_read(h):
    h.gemini.respond(200, json=gemini_reply("ok"))
    h.post(text_payload("hello", msg_id="wamid.READ1"))
    assert "wamid.READ1" in h.read_receipts()
    call = h.graph.calls[0]
    assert call.request.headers["Authorization"] == "Bearer test-access-token"


def test_other_phone_number_id_ignored(h):
    p = text_payload("hello")
    p["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "OTHER"
    assert h.post(p).json()["queued"] == 0


def test_health(h):
    r = h.client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["business_file_ok"] is True
    assert data["llm_provider"] == "gemini"
    assert data["signature_check"] is True


def test_whatsapp_send_retries_once_on_5xx(h):
    h.gemini.respond(200, json=gemini_reply("hello there"))
    h.graph.side_effect = [httpx.Response(200, json={}),          # mark read
                           httpx.Response(500, text="oops"),       # first send fails
                           httpx.Response(200, json={})]           # retry works
    h.post(text_payload("hi"))
    sends = [c for c in h.graph.calls if json.loads(c.request.content).get("type") == "text"]
    assert len(sends) == 2
