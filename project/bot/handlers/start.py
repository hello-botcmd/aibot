"""
bot/handlers/start.py
──────────────────────
/start command → landing screen with dashboard image.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import config
from bot.utils.helpers import is_admin
from bot.utils.keyboards import start_keyboard

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "✨ *Welcome to the Control Panel* ✨\n\n"
    "This bot lets authorised admins manage multiple Telegram userbot accounts "
    "powered by AI chat.\n\n"
    "Use the buttons below to navigate."
)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user    = update.effective_user

    if message is None:
        return
    if not user or not is_admin(user.id, config.ADMIN_IDS):
        await message.reply_text("⛔ You are not authorised to use this bot.")
        return

    context.user_data.clear()
    kb = start_keyboard(config.CONTACT_USERNAME)

    try:
        await message.reply_photo(
            photo=config.DASHBOARD_IMAGE_URL,
            caption=WELCOME_TEXT,
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Could not send photo, falling back to text: %s", exc)
        await message.reply_text(
            WELCOME_TEXT,
            parse_mode="Markdown",
            reply_markup=kb,
        )
