# ============================================================
#                    C O N F I G . P Y
#   Edit this file to customise your bot without touching code
# ============================================================

# ── Telegram Bot (Admin Dashboard) ──────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"           # from @BotFather

# ── Admin user-ids (int) — only these users can use the bot ─
ADMIN_IDS = [123456789, 987654321]          # add as many as you need

# ── Dashboard image URL ─────────────────────────────────────
DASHBOARD_IMAGE_URL = "https://i.imgur.com/yourimage.jpg"

# ── Contact / Support handle ────────────────────────────────
CONTACT_USERNAME = "@sexyiwowu"             # shown on Contact button

# ── OpenRouter AI ───────────────────────────────────────────
OPENROUTER_API_KEY  = "sk-or-v1-XXXXXXXXXXXXXXXX"
OPENROUTER_MODEL    = "openai/gpt-4o"       # valid OpenRouter model slug
OPENROUTER_URL      = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/auth/key"

# ── AI Persona (shared default; each account can override) ──
DEFAULT_PERSONA = (
    "You are a friendly assistant. Reply in Hinglish (Hindi + English mix) "
    "in short, natural messages like a close friend would."
)

# ── Userbot behaviour ───────────────────────────────────────
MAX_HISTORY_TURNS   = 6     # conversation turns remembered per user
COOLDOWN_SECONDS    = 5     # minimum seconds between AI replies (per user)

# ── Paid-photo trigger word ─────────────────────────────────
PAID_TRIGGER_WORD   = "send"   # if other person sends this word, bot replies with paid photo
