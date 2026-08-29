"""
main.py
────────
Entry point.  Starts:
  1. All saved Telethon userbot clients (reconnect from DB).
  2. The python-telegram-bot admin dashboard bot (polling).

Run:  python main.py
"""

import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import config
from bot.handlers.dashboard import callback_handler, message_handler
from bot.handlers.start     import start_handler
from bot.utils              import db
from userbot.manager        import start_all_saved_userbots

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Silence noisy libs
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ── Startup / Shutdown hooks ──────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    logger.info("Reconnecting saved userbot sessions…")
    await start_all_saved_userbots()
    logger.info("All saved userbots reconnected. Dashboard bot starting…")


async def post_shutdown(application: Application) -> None:
    from userbot.manager import all_userbots
    for sid, client in all_userbots().items():
        try:
            await client.disconnect()
            logger.info("Disconnected userbot %s", sid)
        except Exception as exc:
            logger.warning("Error disconnecting %s: %s", sid, exc)


# ── Build PTB application ─────────────────────────────────────────────────────

def build_app() -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_handler))

    # Callback queries (button presses)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Text & photo messages (conversation state)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            message_handler,
        )
    )

    return app


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Ensure DB is initialised
    db.load()

    logger.info("Starting Admin Dashboard Bot…")
    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
