"""
main.py
────────
Entry point.  Starts:
  1. All saved Telethon userbot clients (reconnect from DB).
  2. The python-telegram-bot admin dashboard bot (polling).

Run:  python main.py   (from the project/ directory)
"""

from __future__ import annotations

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
from bot.handlers.dashboard import callback_handler, error_handler, message_handler
from bot.handlers.start     import start_handler
from bot.utils              import db
from userbot.manager        import disconnect_all, start_all_saved_userbots

logger = logging.getLogger("aibot")


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Console + file logging, using the paths/level from config.py."""
    level = getattr(logging, str(getattr(config, "LOG_LEVEL", "INFO")).upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    except OSError as exc:                       # read-only FS, bad path, …
        print(f"⚠️  File logging disabled ({exc})", file=sys.stderr)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
        handlers=handlers,
        force=True,
    )

    # Silence noisy libs
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Startup / Shutdown hooks ──────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    logger.info("Reconnecting saved userbot sessions…")
    await start_all_saved_userbots()
    logger.info("All saved userbots reconnected. Dashboard bot starting…")


async def post_shutdown(application: Application) -> None:
    await disconnect_all()
    try:
        db.flush_pending()
        logger.info("Store flushed to %s", config.STORE_FILE)
    except Exception as exc:                     # noqa: BLE001
        logger.error("Final flush failed: %s", exc)


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

    # Never let a handler exception die silently
    app.add_error_handler(error_handler)

    return app


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()

    # Validate config before touching the DB or the network.  Fatal problems
    # raise SystemExit with an actionable message; warnings are just logged.
    _fatal, warnings = config.validate(strict=True)
    for warning in warnings:
        logger.warning("⚠️  %s", warning)

    db.load()
    logger.info("Starting Admin Dashboard Bot… (log level %s)",
                getattr(config, "LOG_LEVEL", "INFO"))

    app = build_app()
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
