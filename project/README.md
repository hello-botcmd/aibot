# 🤖 Telegram Userbot Dashboard

A premium multi-account Telegram userbot manager with an AI-powered auto-reply system, controlled entirely through a private admin dashboard bot.

---

## 📁 Project Structure

```
project/
├── main.py                      ← Entry point (run this)
├── config.py                    ← ALL your settings live here
├── requirements.txt
├── .env.example
├── .gitignore
│
├── bot/                         ← Admin dashboard (python-telegram-bot)
│   ├── handlers/
│   │   ├── start.py             ← /start command
│   │   └── dashboard.py        ← All button/callback logic
│   └── utils/
│       ├── db.py                ← JSON database (in-memory + disk)
│       ├── ai.py                ← OpenRouter API wrapper (async-safe)
│       ├── keyboards.py         ← All InlineKeyboardMarkup builders
│       └── helpers.py           ← Formatting utilities
│
├── userbot/                     ← Telethon userbot clients
│   ├── manager.py               ← Client lifecycle (add/remove/reconnect)
│   └── modules/
│       ├── aichat.py            ← AI auto-reply + paid photo logic
│       └── commands.py          ← Dot-commands (.ping, .help, etc.)
│
└── data/                        ← Auto-created at runtime
    ├── store.json               ← All persistent data
    └── bot.log                  ← Log file
```

---

## ⚙️ Setup

### 1. Clone & install dependencies

```bash
git clone <your-repo>
cd project
pip install -r requirements.txt
```

### 2. Get credentials

| Credential | Where to get |
|------------|-------------|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `ADMIN_IDS` | Your Telegram numeric user ID (get from [@userinfobot](https://t.me/userinfobot)) |

### 3. Edit `config.py`

```python
BOT_TOKEN          = "123456:ABC-DEF..."
ADMIN_IDS          = [your_user_id]
DASHBOARD_IMAGE_URL= "https://i.imgur.com/yourimage.jpg"
CONTACT_USERNAME   = "@yourusername"
OPENROUTER_API_KEY = "sk-or-v1-..."
OPENROUTER_MODEL   = "openai/gpt-4o"
TELEGRAM_API_ID    = 12345678
TELEGRAM_API_HASH  = "abcdef..."
```

### 4. Generate a Telethon session string

```bash
python -c "
from telethon.sync import TelegramClient
c = TelegramClient('tmp', API_ID, API_HASH)
c.start()
print(c.session.save())
c.disconnect()
"
```

Copy the printed string — you'll paste it into the bot dashboard.

### 5. Run

```bash
python main.py
```

---

## 🗂 Admin Dashboard — Button Guide

```
/start
  ├── 🗂 Dashboard
  │     ├── [➕ Add Account]    [🗃 Manage Accounts]
  │     ├── [⚡ Toggle AI]      [📊 Stats]
  │     ├── [💎 Set Paid Photo] [🔙 Back]
  │
  └── 📞 Contact  →  opens t.me/yourusername
```

| Button | What it does |
|--------|-------------|
| **Add Account** | Prompts for Telethon session string → connects & saves |
| **Manage Accounts** | Lists all accounts (🟢/🔴 AI status) → tap to terminate |
| **Toggle AI** | Master switch — turns AI ON/OFF for **all** accounts simultaneously |
| **Stats** | Connected accounts, AI-on count, uptime, total API calls, OpenRouter credit balance |
| **Set Paid Photo** | Choose account → send photo → send star count → saved; auto-sends when trigger word received |
| **Contact** | Opens Telegram chat with your configured username |
| **Back** | Returns to previous screen |

---

## 🤖 Userbot Dot-Commands

Type these in **any chat** from your userbot account:

| Command | Description |
|---------|-------------|
| `.ping` | Latency test |
| `.help` | Show all commands |
| `.aichaton` | Enable AI for this account |
| `.aichatoff` | Disable AI for this account |
| `.setstatus` | Show current AI status + persona |
| `.setpersona <text>` | Set custom AI persona for this account |
| `.resethistory <userid>` | Clear chat history with a specific user |

---

## 💎 Paid Photo Feature

1. Go to **Dashboard → Set Paid Photo**
2. Select the account
3. Send a photo
4. Send the number of Telegram Stars to charge (or `0` for free)

When the opposite user sends the **trigger word** (default: `send`, configurable in `config.py`), the userbot automatically sends that photo to the chat.

---

## 🧠 AI Behaviour

- Each account has its **own persona, history, and toggle state**
- The **global toggle** in the dashboard overrides all accounts at once
- Per-account toggles via `.aichaton` / `.aichatoff` work independently
- History is trimmed to last `MAX_HISTORY_TURNS` exchanges (default: 6)
- Rate limits are handled silently (no error messages exposed to users)
- Typing indicator loops continuously while AI generates a response

---

## 🔐 Security Notes

- Only `ADMIN_IDS` listed in `config.py` can operate the dashboard bot
- Session strings are stored locally in `data/store.json` — **never commit this file**
- API key is in `config.py` — **never commit this file** (it's in `.gitignore`)
- The bot never exposes your session strings or API keys through Telegram

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `python-telegram-bot 21.x` | Admin dashboard bot |
| `telethon 1.36.x` | Userbot clients |
| `requests` | OpenRouter API calls |

---

## 🛠 Troubleshooting

| Problem | Fix |
|---------|-----|
| `TELEGRAM_API_ID not set` | Add `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` to `config.py` |
| Session string invalid | Regenerate — sessions expire if password changed or logged out elsewhere |
| AI not replying | Check global toggle in dashboard Stats; check `OPENROUTER_API_KEY` |
| Photo not sending | Make sure you set the paid photo via dashboard first |
| Bot not responding | Check `data/bot.log` for errors |
