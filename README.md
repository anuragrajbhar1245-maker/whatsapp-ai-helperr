# WhatsApp AI Helper

A WhatsApp bot for small businesses (guest houses, dental clinics, saree shops...).

Customers send a message on WhatsApp. The bot:

- answers in the **same language** as the customer (Hindi, Hinglish or English),
- uses **only the facts in the business file** (it does not make up prices),
- remembers the **last 15 messages** of each customer,
- takes **bookings / appointments** (name, date, guests or service) and sends the owner a summary on WhatsApp,
- **hands the chat to the owner** when the customer asks for a human (or the AI is not sure),
- politely asks the customer to type when they send a photo, voice note, etc.

Built with Python 3.12 + FastAPI. AI: Google Gemini (default) or Anthropic Claude.

---

## 1. What you need

- Python 3.12 (Windows: download from python.org, tick **"Add python.exe to PATH"** when installing)
- A Gemini API key (free): https://aistudio.google.com/apikey
- A Meta developer account with a WhatsApp app: https://developers.facebook.com/apps
- (For deploy) A free Render account: https://render.com

---

## 2. Run it on your Windows PC

Open **PowerShell** in the project folder and run:

```powershell
# 1. Create a virtual environment and turn it on
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
# If you see "running scripts is disabled", run this once, then try again:
# Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 2. Install packages
pip install -r requirements-dev.txt

# 3. Make your settings file
Copy-Item .env.example .env
notepad .env        # fill in GEMINI_API_KEY, VERIFY_TOKEN, etc. and save

# 4. Run the tests (all should pass)
python -m pytest -q

# 5. Start the server
uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000/health in your browser. You should see `"status": "ok"`.

### Chat with the bot without WhatsApp

You can test the bot in the terminal. It uses the real AI if `GEMINI_API_KEY` is in `.env`.

```powershell
python scripts\chat.py
python scripts\chat.py --business dental-clinic
python scripts\chat.py --business saree-shop
```

Inside the chat:

| Type this | What it does |
|---|---|
| `/owner #resume 919000000001` | act as the owner and turn the bot back on |
| `/bookings` | show saved bookings |
| `/reset` | clear memory |
| `/quit` | exit |

---

## 3. Give your PC a public link (cloudflared quick tunnel)

Meta needs an `https://` link to send messages to your PC.

```powershell
# Install once
winget install --id Cloudflare.cloudflared

# Keep the server running in one window. In a NEW PowerShell window:
cloudflared tunnel --url http://localhost:8000
```

It prints a link like `https://random-words.trycloudflare.com`. Your webhook link is:

```
https://random-words.trycloudflare.com/webhook
```

Note: the quick-tunnel link **changes every time** you restart cloudflared. Then you must update it in Meta again. For a fixed link, deploy to Render (step 5).

---

## 4. Connect WhatsApp (Meta webhook setup)

1. Go to https://developers.facebook.com/apps, open your app, add the **WhatsApp** product.
2. **WhatsApp > API Setup**: copy the **Phone number ID** and the **access token** into `.env`
   (`PHONE_NUMBER_ID`, `ACCESS_TOKEN`). Add your own phone number as a test recipient.
3. **App settings > Basic**: copy the **App secret** into `.env` (`APP_SECRET`).
4. Choose any secret word for `VERIFY_TOKEN` in `.env` (example: `ganga123secret`). Restart the server.
5. **WhatsApp > Configuration > Webhook > Edit**:
   - Callback URL: `https://YOUR-LINK/webhook`
   - Verify token: the same word as `VERIFY_TOKEN`
   - Click **Verify and save**.
6. Under **Webhook fields**, click **Subscribe** next to **messages**.
7. Send "hi" from your phone to the test number. The bot should reply. 🎉

Important things to know:

- The temporary access token **expires in 24 hours**. For real use, create a **System User** token
  (Business Settings > Users > System users) with `whatsapp_business_messaging` permission.
- WhatsApp rule: a business can send free-form messages only within **24 hours** of the person's last message.
  So the **owner should send any message (like "hi") to the bot number once a day**, otherwise the owner
  alerts (bookings, handoff) may not be delivered.
- Set `OWNER_WHATSAPP` to the owner's number with country code, digits only (example `919812345678`).

### Owner commands (send from the OWNER_WHATSAPP number to the bot number)

| Command | What it does |
|---|---|
| `#resume 9198xxxxxxxx` | turn the bot back on for that customer |
| `#pause 9198xxxxxxxx` | stop the bot for that customer |
| `#paused` | list paused chats |
| `#help` | show commands |

A paused chat turns back on by itself after `PAUSE_HOURS` (default 12). Set `PAUSE_HOURS=0` to never auto-resume.

---

## 5. Deploy free on Render

1. Push this code to your GitHub (private repo is fine).
2. On https://dashboard.render.com click **New > Blueprint**, pick this repo. Render reads `render.yaml`.
3. Fill the secret values it asks for: `VERIFY_TOKEN`, `APP_SECRET`, `ACCESS_TOKEN`, `PHONE_NUMBER_ID`,
   `GEMINI_API_KEY`, `OWNER_WHATSAPP` (leave `ANTHROPIC_API_KEY` empty if you use Gemini).
4. Wait for the deploy. Open `https://YOUR-APP.onrender.com/health` to check.
5. In Meta, change the callback URL to `https://YOUR-APP.onrender.com/webhook`.

Free plan limits (be honest with clients):

- The free service **sleeps after 15 minutes** with no traffic. The first message after that can take ~1 minute.
  Meta retries, so the message is not lost, but the reply is slow. A paid plan ($7/month) stays awake.
- The free disk is **not permanent**: the SQLite database (memory, bookings, paused chats) is **wiped on every
  deploy or restart**. Bookings are also sent to the owner on WhatsApp, so they are not lost. For a paying client,
  use a paid plan with a persistent disk and set `DATABASE_PATH` to the disk path.

You can also run it anywhere with Docker:

```powershell
docker build -t whatsapp-ai-helper .
docker run -p 8000:8000 --env-file .env whatsapp-ai-helper
```

---

## 6. Add a new client business

1. Copy a file in the `business` folder, for example:
   ```powershell
   Copy-Item business\guest-house.md business\sharma-hotel.md
   notepad business\sharma-hotel.md
   ```
2. Write the real details: name, address, timings, **prices in Rs**, rules, what is needed for a booking,
   and what the bot must NOT promise. Simple bullet points work best.
   The bot only knows what is in this file.
3. Set `ACTIVE_BUSINESS=sharma-hotel` in `.env` (or in Render's Environment tab).
4. Test it: `python scripts\chat.py --business sharma-hotel`
5. Restart / redeploy.

One running copy of the app = one business (one WhatsApp number). For a second client, deploy a second copy
with its own WhatsApp number and its own `ACTIVE_BUSINESS`.

---

## 7. Settings (.env)

See `.env.example` for every setting with a short note. Never put real keys in `.env.example`
and never commit `.env` (it is in `.gitignore`).

| Setting | Meaning |
|---|---|
| `VERIFY_TOKEN` | your secret word for Meta webhook verification |
| `APP_SECRET` | Meta app secret, used to check that messages really come from Meta |
| `ACCESS_TOKEN`, `PHONE_NUMBER_ID` | WhatsApp Cloud API details |
| `LLM_PROVIDER` | `gemini` (default) or `anthropic` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini key and model (default `gemini-2.5-flash`) |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Claude key and model (default `claude-haiku-4-5`) |
| `ACTIVE_BUSINESS` | business file name without `.md` |
| `OWNER_WHATSAPP` | owner number for alerts and commands |
| `DATABASE_PATH` | where the SQLite file is saved |
| `MEMORY_LIMIT` | messages remembered per customer (default 15) |
| `PAUSE_HOURS` | auto-resume paused chats after N hours (0 = never) |

---

## 8. How it works (short)

```
Customer -> WhatsApp -> Meta -> POST /webhook
   1. check signature (APP_SECRET)        -> 403 if fake
   2. skip status updates and repeats     (message id saved in SQLite)
   3. reply 200 OK right away
   4. in the background:
        owner command?      -> #resume / #pause
        photo/voice?        -> "please type your message"
        chat paused?        -> stay quiet (owner is talking)
        asks for a human?   -> pause chat, tell owner
        else                -> AI reply with business file + last 15 messages
             [BOOKING]{...} -> save booking, confirm, alert owner
             [HUMAN_ESCALATION] -> pause chat, tell owner
             AI error       -> safe "technical problem" message
```

Files:

```
app/main.py        web server, webhook, /health
app/bot.py         bot logic (bookings, handoff, owner commands)
app/llm.py         Gemini and Claude (plain HTTP calls, timeout, 1 retry)
app/whatsapp.py    sends WhatsApp messages
app/db.py          SQLite (memory, bookings, paused chats, seen message ids)
app/knowledge.py   business file + AI instructions
business/*.md      one file per client business (sample data)
scripts/chat.py    terminal chat simulator
tests/             automatic tests (pytest)
```
