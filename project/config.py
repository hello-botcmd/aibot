# ============================================================
#                 C O N F I G . P Y
# ------------------------------------------------------------
#  ⚠️  NO SECRETS LIVE IN THIS FILE ANYMORE.
#
#  Credentials are read from the environment / `project/.env`
#  (copy `.env.example` → `.env`).  Everything else — trigger
#  words, limits, captions — is edited right here.
#
#  This file is now safe to commit.  Note that the values it
#  *used* to contain are still in git history, so they must be
#  rotated: @BotFather /revoke, new OpenRouter key, new API_HASH.
# ============================================================
"""
config.py
─────────
Single place for every tunable.  Secrets come from env/.env,
behaviour knobs are plain literals below so you can edit them
without touching a shell.
"""

import os
import re
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent / ".env"

try:
    from dotenv import load_dotenv
except ImportError:                                  # real env vars still work,
    if _ENV_FILE.exists():                           # but a silently-ignored .env
                                                   # is a support nightmare
        raise SystemExit(
            "✖ project/.env exists but python-dotenv is not installed, so it would be\n"
            "  ignored. Run:  pip install -r requirements.txt\n"
            "  (or export the variables in your shell / systemd unit instead)\n"
        )

    def load_dotenv(*_a, **_k):
        return False

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PAID_DIR = DATA_DIR / "paid"          # paid-photo bytes (never commit)
LOG_FILE = DATA_DIR / "bot.log"
STORE_FILE = DATA_DIR / "store.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PAID_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(_ENV_FILE, override=False)


# ── env helpers ───────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int = 0) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int_list(name: str) -> list[int]:
    raw = _env(name).replace("\n", ",").replace(" ", ",")
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


# ── Admin Dashboard Bot ───────────────────────────────────────────────────────
BOT_TOKEN = _env("BOT_TOKEN")                                  # from @BotFather
ADMIN_IDS = _env_int_list("ADMIN_IDS")                          # e.g. "123,456"
DASHBOARD_IMAGE_URL = "https://i.ibb.co/nhQQLxK/894e3a6da2af.jpg"
CONTACT_USERNAME = "@sexyiwowu"

# ── Telegram API credentials (from https://my.telegram.org) ───────────────────
TELEGRAM_API_ID = _env_int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = _env("TELEGRAM_API_HASH")

# ── OpenRouter AI ─────────────────────────────────────────────────────────────
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = "openai/gpt-4o"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/auth/key"
OPENROUTER_TIMEOUT = 45          # hard cap per HTTP attempt (×2 attempts worst case)
OPENROUTER_MAX_TOKENS = 200
OPENROUTER_TEMPERATURE = 0.9
# Optional: attribution headers OpenRouter uses for rankings.  Leave "" to omit
# (a fake/placeholder URL is worse than nothing — it pollutes their stats).
OPENROUTER_SITE_URL = ""
OPENROUTER_APP_TITLE = ""

DEFAULT_PERSONA = (
    "You are a friendly assistant. Reply in Hinglish (Hindi + English mix) "
    "in short, natural messages like a close friend would. "
    "Never mention that you are an AI, a bot, or an assistant."
)

# ── Userbot behaviour ─────────────────────────────────────────────────────────
MAX_HISTORY_TURNS = 6            # past exchanges kept per peer
MAX_HISTORY_PEERS = 200          # hard cap on stored peers per account
PEER_TTL_DAYS = 14               # drop peer data idle longer than this
COOLDOWN_SECONDS = 5             # min gap between AI replies per peer
MAX_INPUT_CHARS = 1000           # inbound text truncated to this
REPLY_MAX_AGE_SECONDS = 120      # ignore DMs older than this (restart replay guard)
IGNORE_BOTS = True               # never auto-reply to other bots
AI_DEFAULT_ENABLED = False       # state for a brand-new account

# ── 💎 Paid photo (monetisation) ──────────────────────────────────────────────
# Any ONE of these words/phrases fires the paid photo.
# Matching is case-insensitive and WHOLE-WORD, so a trigger fires both
# when it is the entire message and when it is buried in a sentence:
#     "send"            ✅        "hey can you send it? 🙏"   ✅
#     "send the pic"    ✅        "sender", "sending", "recommend" ❌ (correct)
# Add as many as you want — multi-word phrases are fine too.
PAID_TRIGGER_WORDS = [
    "send",
    "pic",
    "photo",
    "content",
    "bhejo",
]

# Optional escape hatch: your own regex, OR-ed with the words above.
# e.g. r"(send|nude|leak)s?\b"     — "" disables it.
PAID_TRIGGER_EXTRA_REGEX = ""

PAID_DEFAULT_STARS = 15          # pre-suggested price in the dashboard prompt
PAID_MIN_STARS = 0               # 0 = send for free
PAID_MAX_STARS = 5000            # Telegram's own ceiling is far above this
PAID_MAX_PHOTO_MB = 10           # Bot-API download limit is 20 MB; keep it sane
PAID_NO_FORWARDS = True          # recipients can't save/forward the paid media
PAID_FALLBACK_FREE = False       # if Stars are unavailable for this account:
                                 #   False → do NOT send, notify admins (default)
                                 #   True  → send the photo for free instead
PAID_LOCKED_CAPTION = "🔒 Unlock for {stars} ⭐ — tap the Pay button above."
PAID_FREE_CAPTION = ""           # caption used when stars == 0
PAID_BUSY_TEXT = "⏳ Something is off at my end right now — try me again in a bit 🙏"
PAID_NOTIFY_ADMIN = True         # DM the admins when a paid send fails

# ── Ops ───────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DROP_INVALID_SESSIONS_ON_BOOT = True   # delete accounts whose session no longer works
CRITICAL_BACKOFF_SECONDS = 3600        # pause AI on billing/auth errors (402/401)


# ── Startup validation ────────────────────────────────────────────────────────
def validate(strict: bool = True) -> tuple[list[str], list[str]]:
    """
    Split config problems into ``fatal`` (the dashboard cannot run) and
    ``warnings`` (a feature is degraded but usable).

    An empty OpenRouter key or API_HASH must NOT stop the bot: the admin still
    needs the panel to fix it. Only a missing bot token / admin list is fatal.
    """
    fatal: list[str] = []
    warn: list[str] = []

    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        fatal.append("BOT_TOKEN is missing or malformed — set it in project/.env")
    if not ADMIN_IDS:
        fatal.append("ADMIN_IDS is empty — e.g. ADMIN_IDS=8580367479 in project/.env")
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        warn.append("TELEGRAM_API_ID / TELEGRAM_API_HASH missing — userbots cannot connect")
    if not OPENROUTER_API_KEY:
        warn.append("OPENROUTER_API_KEY missing — AI auto-reply is disabled")
    if not PAID_TRIGGER_WORDS and not PAID_TRIGGER_EXTRA_REGEX:
        warn.append("no paid-photo triggers configured — the 💎 flow will never fire")

    if PAID_TRIGGER_EXTRA_REGEX:
        try:
            re.compile(PAID_TRIGGER_EXTRA_REGEX, re.IGNORECASE)
        except re.error as exc:
            fatal.append(f"PAID_TRIGGER_EXTRA_REGEX is not a valid regex: {exc}")

    if strict and fatal:
        raise SystemExit("\n✖ Cannot start:\n  - " + "\n  - ".join(fatal) +
                         "\n\nCopy project/.env.example → project/.env, fill it in, then re-run.\n")
    return fatal, warn
