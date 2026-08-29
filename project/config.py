# ============================================================
#                    C O N F I G . P Y
# ============================================================

# ── Admin Dashboard Bot ──────────────────────────────────────
BOT_TOKEN = "8607223226:AAFDLuUGKeofa8pTV9qiSPPqDhz1nCVUngI"

# ── Admin IDs (only these users can control the dashboard) ───
ADMIN_IDS = [8580367479]

# ── Dashboard banner image ───────────────────────────────────
DASHBOARD_IMAGE_URL = "https://i.ibb.co/nhQQLxK/894e3a6da2af.jpg"

# ── Contact username shown on Contact button ─────────────────
CONTACT_USERNAME = "@sexyiwowu"

# ── Telegram API credentials (from https://my.telegram.org) ──
API_ID   = 36134104
API_HASH = "7e85000983efb86b5d4739b6680016b2"

# ── OpenRouter AI ────────────────────────────────────────────
OPENROUTER_API_KEY     = "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"
OPENROUTER_MODEL       = "openai/gpt-4o"
OPENROUTER_URL         = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/auth/key"

# ── Default AI persona ────────────────────────────────────────
DEFAULT_PERSONA = (
    "You are a friendly assistant. Reply in Hinglish (Hindi + English mix) "
    "in short, natural messages like a close friend would."
)

# ── Userbot behaviour ─────────────────────────────────────────
MAX_HISTORY_TURNS = 6    # past exchanges to remember per user
COOLDOWN_SECONDS  = 5    # min gap between AI replies per peer

# ── Paid-photo trigger words ──────────────────────────────────
# Add as many words as you want — any one of them will trigger the paid photo
PAID_TRIGGER_WORDS = ["send", "photo", "pic", "content"]
