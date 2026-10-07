"""Browser demo: a WhatsApp-looking chat page that talks to the real bot brain.

Open http://localhost:8000/demo after starting the server. Nothing is sent on WhatsApp:
replies are captured and shown on the page. Turn it off with DEMO_PAGE=false.
"""

from __future__ import annotations

import html
import re
import secrets

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .bot import Bot
from .db import Database
from .knowledge import BusinessNotFound, load_business
from .llm import LLMProvider


class CaptureMessenger:
    """Collects what the bot would send on WhatsApp, so the page can show it."""

    def __init__(self) -> None:
        self.outbox: dict[str, list[str]] = {}

    async def send_text(self, to: str, body: str) -> bool:
        self.outbox.setdefault(to, []).append(body)
        return True

    async def mark_read(self, message_id: str) -> bool:
        return True

    def take(self, to: str) -> list[str]:
        return self.outbox.pop(to, [])


class DemoIn(BaseModel):
    session: str
    text: str


SESSION_RE = re.compile(r"^\d{10,15}$")


def business_title(settings) -> str:
    try:
        info = load_business(settings.business_dir, settings.active_business)
    except BusinessNotFound:
        return settings.active_business
    text = info if isinstance(info, str) else str(getattr(info, "text", "") or info)
    for line in text.splitlines():
        if line.startswith("# "):
            return re.sub(r"\s*\(.*?\)\s*$", "", line[2:]).strip() or settings.active_business
    return settings.active_business.replace("-", " ").title()


def build_demo_router(settings, db: Database, llm: LLMProvider) -> APIRouter:
    router = APIRouter()
    messenger = CaptureMessenger()
    bot = Bot(settings=settings, db=db, messenger=messenger, llm=llm)

    @router.get("/demo", response_class=HTMLResponse)
    async def demo_page() -> HTMLResponse:
        page = DEMO_HTML.replace("{{TITLE}}", html.escape(business_title(settings)))
        return HTMLResponse(page)

    @router.get("/demo/new")
    async def new_session() -> dict[str, str]:
        return {"session": "9177" + "".join(secrets.choice("0123456789") for _ in range(8))}

    @router.post("/demo/chat")
    async def demo_chat(body: DemoIn) -> dict:
        if not SESSION_RE.match(body.session) or body.session == bot.owner:
            raise HTTPException(status_code=400, detail="bad session")
        text = body.text.strip()[:1000]
        if not text:
            return {"replies": [], "owner": []}
        await bot.handle_message(body.session, "text", text)
        replies = messenger.take(body.session)
        owner_alerts = messenger.take(bot.owner) if bot.owner else []
        return {"replies": replies, "owner": owner_alerts}

    @router.post("/demo/reset")
    async def demo_reset(body: DemoIn) -> dict[str, str]:
        if SESSION_RE.match(body.session):
            db.clear_history(body.session)
            db.resume_chat(body.session)
        return {"status": "ok"}

    return router


DEMO_HTML = r"""<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}} - WhatsApp</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: #d1d7db; display: flex; justify-content: center; min-height: 100vh; }
  .phone { width: 100%; max-width: 440px; height: 100vh; display: flex; flex-direction: column;
           background: #efeae2; box-shadow: 0 0 30px rgba(0,0,0,.15); }
  @media (min-width: 600px) { .phone { height: 92vh; margin-top: 4vh; border-radius: 18px; overflow: hidden; } }
  header { background: #008069; color: #fff; padding: 10px 14px; display: flex; align-items: center; gap: 12px; }
  .avatar { width: 40px; height: 40px; border-radius: 50%; background: #25d366; display: flex;
            align-items: center; justify-content: center; font-weight: 700; font-size: 18px; }
  .who { flex: 1; min-width: 0; }
  .name { font-weight: 600; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .status { font-size: 12.5px; opacity: .85; }
  .reset { background: none; border: 1px solid rgba(255,255,255,.6); color: #fff; border-radius: 14px;
           padding: 4px 10px; font-size: 12px; cursor: pointer; }
  #chat { flex: 1; overflow-y: auto; padding: 14px 12px;
          background-color: #efeae2;
          background-image: radial-gradient(rgba(0,0,0,.035) 1px, transparent 1px);
          background-size: 18px 18px; }
  .day { text-align: center; margin: 4px 0 12px; }
  .day span { background: #fff; color: #54656f; font-size: 12px; padding: 5px 10px; border-radius: 7px;
              box-shadow: 0 1px .5px rgba(0,0,0,.13); }
  .note { background: #ffeecd; color: #54656f; font-size: 12px; text-align: center; padding: 6px 10px;
          border-radius: 7px; margin: 0 auto 12px; max-width: 90%; }
  .row { display: flex; margin: 3px 0; }
  .row.me { justify-content: flex-end; }
  .bubble { max-width: 82%; padding: 6px 9px 4px; border-radius: 8px; font-size: 14.5px; line-height: 1.38;
            box-shadow: 0 1px .5px rgba(0,0,0,.13); white-space: pre-wrap; word-wrap: break-word; position: relative; }
  .me .bubble { background: #d9fdd3; border-top-right-radius: 0; }
  .bot .bubble { background: #fff; border-top-left-radius: 0; }
  .meta { font-size: 11px; color: #667781; text-align: right; margin-top: 2px; }
  .tick { color: #53bdeb; margin-left: 3px; }
  .owner .bubble { background: #fff3c4; font-size: 12.5px; color: #5c4a00; max-width: 90%; margin: 6px auto; }
  .owner { justify-content: center; }
  .typing .bubble { color: #667781; font-style: italic; }
  footer { display: flex; gap: 8px; padding: 8px 10px; background: #f0f2f5; }
  #msg { flex: 1; border: none; border-radius: 22px; padding: 11px 16px; font-size: 15px; outline: none; }
  #send { width: 46px; height: 46px; border-radius: 50%; border: none; background: #00a884; color: #fff;
          font-size: 20px; cursor: pointer; }
  #send:disabled { background: #8fd3c5; cursor: default; }
</style>
</head>
<body>
<div class="phone">
  <header>
    <div class="avatar" id="av">G</div>
    <div class="who"><div class="name" id="title">{{TITLE}}</div><div class="status" id="st">online</div></div>
    <button class="reset" id="reset" title="Start a new chat">New chat</button>
  </header>
  <div id="chat">
    <div class="day"><span>Today</span></div>
    <div class="note">🔒 AI assistant demo. Hindi, English ya Hinglish mein message karke dekhiye.</div>
  </div>
  <footer>
    <input id="msg" placeholder="Message" autocomplete="off" autofocus>
    <button id="send" aria-label="Send">➤</button>
  </footer>
</div>
<script>
const chat = document.getElementById('chat'), input = document.getElementById('msg'),
      sendBtn = document.getElementById('send'), st = document.getElementById('st');
document.getElementById('av').textContent = (document.getElementById('title').textContent.trim()[0] || 'B').toUpperCase();
let session = localStorage.getItem('demoSession');
const intro = chat.innerHTML;

async function ensureSession() {
  if (session) return session;
  const r = await fetch('/demo/new'); session = (await r.json()).session;
  localStorage.setItem('demoSession', session); return session;
}
function now() { return new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function add(kind, text) {
  const row = document.createElement('div'); row.className = 'row ' + kind;
  const tick = kind === 'me' ? '<span class="tick">✓✓</span>' : '';
  row.innerHTML = '<div class="bubble">' + esc(text) + '<div class="meta">' + now() + tick + '</div></div>';
  chat.appendChild(row); chat.scrollTop = chat.scrollHeight; return row;
}
async function send() {
  const text = input.value.trim(); if (!text) return;
  input.value = ''; sendBtn.disabled = true; add('me', text);
  st.textContent = 'typing…';
  try {
    const r = await fetch('/demo/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: await ensureSession(), text})});
    const data = await r.json();
    for (const reply of (data.replies || [])) {
      await new Promise(res => setTimeout(res, 350)); add('bot', reply);
    }
    for (const o of (data.owner || [])) add('owner', '🔔 Owner ko alert gaya:\n' + o);
    if (!(data.replies || []).length && !(data.owner || []).length)
      add('owner', '(Bot paused: owner is handling this chat. Press New chat to restart.)');
  } catch (e) { add('owner', 'Error: server se jawab nahi aaya. Server chal raha hai?'); }
  st.textContent = 'online'; sendBtn.disabled = false; input.focus();
}
sendBtn.onclick = send;
input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
document.getElementById('reset').onclick = async () => {
  if (session) await fetch('/demo/reset', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session, text: ''})});
  localStorage.removeItem('demoSession'); session = null; chat.innerHTML = intro; input.focus();
};
</script>
</body>
</html>
"""
