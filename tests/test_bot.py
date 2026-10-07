import json

import httpx
import pytest

from app.bot import FALLBACK_MESSAGE, HANDOFF_REPLY, parse_booking, wants_human
from app.llm import AnthropicProvider, GeminiProvider, LLMError, normalize_history
from tests.conftest import ANTHROPIC_URL, CUSTOMER, OWNER, gemini_reply, text_payload


# ---------- memory window ----------

def test_memory_keeps_last_15(h):
    h.gemini.respond(200, json=gemini_reply("ok"))
    for i in range(10):  # 10 user + 10 assistant = 20 messages
        h.post(text_payload(f"message {i}", msg_id=f"wamid.m{i}"))
    history = h.db.get_history(CUSTOMER, 100)
    assert len(history) == 15
    assert history[-1] == {"role": "assistant", "content": "ok"}
    assert history[-2] == {"role": "user", "content": "message 9"}
    # Last LLM call saw at most 15 messages and the newest user message last.
    last = h.llm_requests()[-1]
    assert len(last["contents"]) <= 15
    assert last["contents"][-1]["parts"][0]["text"] == "message 9"
    assert last["contents"][0]["role"] == "user"  # normalized to start with the customer


def test_memory_is_per_customer(h):
    h.gemini.respond(200, json=gemini_reply("ok"))
    h.post(text_payload("from A", msg_id="a1", sender="919800000001"))
    h.post(text_payload("from B", msg_id="b1", sender="919800000002"))
    assert [m["content"] for m in h.db.get_history("919800000001", 15)] == ["from A", "ok"]
    assert [m["content"] for m in h.db.get_history("919800000002", 15)] == ["from B", "ok"]


# ---------- language passthrough ----------

@pytest.mark.parametrize("customer_text,ai_text", [
    ("कमरे का किराया कितना है?", "डीलक्स AC रूम ₹1,800 प्रति रात है 🙏"),
    ("bhaiya room ka rate kya hai?", "Deluxe AC room Rs 1,800 per night hai ji"),
    ("What is the room price?", "Deluxe AC room is Rs 1,800 per night."),
])
def test_language_passthrough(h, customer_text, ai_text):
    h.gemini.respond(200, json=gemini_reply(ai_text))
    h.post(text_payload(customer_text))
    req = h.llm_requests()[0]
    assert req["contents"][-1]["parts"][0]["text"] == customer_text  # sent to AI unchanged
    system = req["systemInstruction"]["parts"][0]["text"]
    assert "SAME language" in system
    assert "Ganga View Guest House" in system  # business file included
    assert "NEVER invent prices" in system
    assert h.sent_texts(CUSTOMER) == [ai_text]  # reply sent back unchanged (UTF-8 safe)
    assert req["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_gemini_request_uses_key_header_and_model(h):
    h.gemini.respond(200, json=gemini_reply("ok"))
    h.post(text_payload("hi"))
    call = h.gemini.calls[0]
    assert call.request.headers["x-goog-api-key"] == "test-gemini-key"
    assert "gemini-2.5-flash:generateContent" in str(call.request.url)


# ---------- booking ----------

BOOKING_REPLY = (
    "Dhanyavaad Ravi ji! Aapki booking request note kar li hai.\n"
    '[BOOKING]{"name": "Ravi Kumar", "date": "2026-10-20", "time": "", "guests": "2", '
    '"service": "Deluxe AC room, 2 nights", "notes": "late arrival"}[/BOOKING]'
)


def test_booking_parse_save_confirm_and_owner_alert(h):
    h.gemini.respond(200, json=gemini_reply(BOOKING_REPLY))
    h.post(text_payload("haan confirm hai"))

    bookings = h.db.list_bookings(CUSTOMER)
    assert len(bookings) == 1
    b = bookings[0]
    assert (b["name"], b["date"], b["guests"], b["business"]) == ("Ravi Kumar", "2026-10-20", "2", "guest-house")

    customer_msgs = h.sent_texts(CUSTOMER)
    assert len(customer_msgs) == 1
    assert "[BOOKING]" not in customer_msgs[0]
    assert "Dhanyavaad Ravi ji" in customer_msgs[0]
    assert f"#{b['id']}" in customer_msgs[0]

    owner_msgs = h.sent_texts(OWNER)
    assert len(owner_msgs) == 1
    assert "Ravi Kumar" in owner_msgs[0]
    assert "2026-10-20" in owner_msgs[0]
    assert "Guests: 2" in owner_msgs[0]
    assert CUSTOMER in owner_msgs[0]

    # memory stores the clean reply, not the raw JSON block
    assert "[BOOKING]" not in h.db.get_history(CUSTOMER, 15)[-1]["content"]


def test_booking_repeat_not_saved_twice(h):
    h.gemini.respond(200, json=gemini_reply(BOOKING_REPLY))
    h.post(text_payload("confirm", msg_id="x1"))
    h.post(text_payload("confirm again", msg_id="x2"))
    assert len(h.db.list_bookings(CUSTOMER)) == 1
    assert len(h.sent_texts(OWNER)) == 1


def test_parse_booking_incomplete_or_bad_json():
    b, clean = parse_booking('ok [BOOKING]{"name": "A", "date": ""}[/BOOKING]')
    assert b is None and clean == "ok"
    b, clean = parse_booking("ok [BOOKING]{not json}[/BOOKING]")
    assert b is None and clean == "ok"
    b, clean = parse_booking("no marker here")
    assert b is None and clean == "no marker here"
    b, _ = parse_booking('[BOOKING]```json\n{"name":"A","date":"1 Nov","service":"cleaning"}\n```[/BOOKING]')
    assert b == {"name": "A", "date": "1 Nov", "service": "cleaning"}


# ---------- human handoff ----------

@pytest.mark.parametrize("text", ["I want to talk to a human", "owner se baat karni hai",
                                  "kisi insaan se baat karao", "मुझे मालिक से बात करनी है"])
def test_wants_human_detection(text):
    assert wants_human(text)


def test_wants_human_negative():
    assert not wants_human("room ka rate kya hai")
    assert not wants_human("humanity is great")


def test_handoff_keyword_pauses_and_resume(h):
    h.gemini.respond(200, json=gemini_reply("AI answer"))

    h.post(text_payload("owner se baat karni hai", msg_id="h1"))
    assert h.db.is_paused(CUSTOMER)
    assert h.sent_texts(CUSTOMER) == [HANDOFF_REPLY]
    owner_alert = h.sent_texts(OWNER)
    assert len(owner_alert) == 1 and f"#resume {CUSTOMER}" in owner_alert[0]
    assert not h.gemini.called

    # While paused: no AI, no reply, but message is remembered
    h.post(text_payload("hello?", msg_id="h2"))
    assert not h.gemini.called
    assert h.sent_texts(CUSTOMER) == [HANDOFF_REPLY]
    assert h.db.get_history(CUSTOMER, 15)[-1]["content"] == "hello?"

    # Owner resumes (10-digit number without country code also works)
    h.post(text_payload(f"#resume {CUSTOMER[2:]}", msg_id="h3", sender=OWNER))
    assert not h.db.is_paused(CUSTOMER)
    assert "resumed" in h.sent_texts(OWNER)[-1].lower()

    # Bot answers again
    h.post(text_payload("room rate?", msg_id="h4"))
    assert h.gemini.called
    assert h.sent_texts(CUSTOMER)[-1] == "AI answer"


def test_ai_escalation_tag_pauses_chat(h):
    h.gemini.respond(200, json=gemini_reply("Main owner se check karke batata hoon. [HUMAN_ESCALATION]"))
    h.post(text_payload("kya aap wedding party ke liye poora hotel de sakte ho?"))
    assert h.db.is_paused(CUSTOMER)
    assert h.sent_texts(CUSTOMER) == ["Main owner se check karke batata hoon."]
    assert "needs a human" in h.sent_texts(OWNER)[0]


def test_resume_only_from_owner_number(h):
    h.db.pause_chat(CUSTOMER, "test")
    h.gemini.respond(200, json=gemini_reply("hi"))
    h.post(text_payload(f"#resume {CUSTOMER}", msg_id="r1", sender="919700000000"))
    assert h.db.is_paused(CUSTOMER)


def test_owner_pause_command_and_help(h):
    h.post(text_payload(f"#pause {CUSTOMER}", msg_id="p1", sender=OWNER))
    assert h.db.is_paused(CUSTOMER)
    h.post(text_payload("#help", msg_id="p2", sender=OWNER))
    assert "#resume" in h.sent_texts(OWNER)[-1]


def test_paused_chat_auto_resumes_after_pause_hours(h, monkeypatch):
    import app.db as dbmod
    h.db.pause_chat(CUSTOMER, "test")
    real = dbmod.time.time()
    monkeypatch.setattr(dbmod.time, "time", lambda: real + 13 * 3600)
    assert not h.db.is_paused(CUSTOMER, pause_hours=12)


# ---------- LLM failure fallback ----------

def test_llm_failure_sends_fallback_after_one_retry(h):
    h.gemini.respond(500, json={"error": "boom"})
    r = h.post(text_payload("hello"))
    assert r.status_code == 200
    assert h.gemini.call_count == 2  # one retry
    assert h.sent_texts(CUSTOMER) == [FALLBACK_MESSAGE]


def test_llm_timeout_sends_fallback(h):
    h.gemini.side_effect = httpx.ReadTimeout("slow")
    h.post(text_payload("hello"))
    assert h.gemini.call_count == 2
    assert h.sent_texts(CUSTOMER) == [FALLBACK_MESSAGE]


def test_llm_retry_then_success(h):
    h.gemini.side_effect = [httpx.Response(503, json={}), httpx.Response(200, json=gemini_reply("ok now"))]
    h.post(text_payload("hello"))
    assert h.sent_texts(CUSTOMER) == ["ok now"]


def test_llm_bad_key_no_retry(h):
    h.gemini.respond(400, json={"error": {"message": "API key not valid"}})
    h.post(text_payload("hello"))
    assert h.gemini.call_count == 1
    assert h.sent_texts(CUSTOMER) == [FALLBACK_MESSAGE]


def test_llm_empty_response_fallback(h):
    h.gemini.respond(200, json={"candidates": [{"finishReason": "SAFETY"}]})
    h.post(text_payload("hello"))
    assert h.sent_texts(CUSTOMER) == [FALLBACK_MESSAGE]


def test_missing_key_fallback(harness_factory):
    h = harness_factory(gemini_api_key="")
    h.post(text_payload("hello"))
    assert not h.gemini.called
    assert h.sent_texts(CUSTOMER) == [FALLBACK_MESSAGE]


# ---------- Anthropic provider ----------

def test_anthropic_provider_selected(harness_factory):
    h = harness_factory(llm_provider="anthropic")
    route = h.router.post(ANTHROPIC_URL).respond(
        200, json={"content": [{"type": "text", "text": "Hello from Claude"}]})
    h.post(text_payload("hello"))
    assert route.called
    req = json.loads(route.calls[0].request.content)
    assert req["messages"][-1] == {"role": "user", "content": "hello"}
    assert "BUSINESS INFO" in req["system"]
    assert route.calls[0].request.headers["x-api-key"] == "test-anthropic-key"
    assert h.sent_texts(CUSTOMER) == ["Hello from Claude"]
    assert h.client.get("/health").json()["llm_provider"] == "anthropic"


async def test_providers_raise_llmerror_without_key():
    with pytest.raises(LLMError):
        await GeminiProvider("").generate("sys", [{"role": "user", "content": "hi"}])
    with pytest.raises(LLMError):
        await AnthropicProvider("").generate("sys", [{"role": "user", "content": "hi"}])


def test_normalize_history():
    hist = [{"role": "assistant", "content": "x"}, {"role": "user", "content": "a"},
            {"role": "user", "content": "b"}, {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "c"}]
    assert normalize_history(hist) == [{"role": "user", "content": "a\nb"},
                                       {"role": "assistant", "content": "c"}]


def test_business_switch(harness_factory):
    h = harness_factory(active_business="dental-clinic")
    h.gemini.respond(200, json=gemini_reply("ok"))
    h.post(text_payload("hi"))
    assert "Smile Care Dental Clinic" in h.llm_requests()[0]["systemInstruction"]["parts"][0]["text"]
