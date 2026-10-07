from tests.conftest import gemini_reply


def test_demo_page_served(h):
    r = h.client.get("/demo")
    assert r.status_code == 200
    assert "Ganga View Guest House" in r.text
    assert "SAMPLE" not in r.text.split("<title>")[1].split("</title>")[0]


def test_demo_chat_returns_ai_reply_without_whatsapp(h):
    h.gemini.respond(200, json=gemini_reply("Namaste! Deluxe room Rs 1,800 hai."))
    session = h.client.get("/demo/new").json()["session"]
    r = h.client.post("/demo/chat", json={"session": session, "text": "room price?"})
    assert r.status_code == 200
    assert r.json()["replies"] == ["Namaste! Deluxe room Rs 1,800 hai."]
    assert h.sent_texts() == []  # nothing goes out on real WhatsApp


def test_demo_rejects_bad_session(h):
    r = h.client.post("/demo/chat", json={"session": "abc", "text": "hi"})
    assert r.status_code == 400


def test_demo_can_be_turned_off(harness_factory):
    h = harness_factory(demo_page=False)
    assert h.client.get("/demo").status_code == 404
