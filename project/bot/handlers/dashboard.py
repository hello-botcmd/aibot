"""
bot/handlers/dashboard.py
──────────────────────────
All CallbackQueryHandler logic for the dashboard flow.

State machine (stored in context.user_data):
  "awaiting" : None | "session_string" | "paid_photo" | "paid_stars"
  "paid_acc_sid" : str   ← which account the paid-photo flow targets
"""

import logging
import time

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import config
from bot.utils import db
from bot.utils.ai import fetch_credits, generate_reply
from bot.utils.helpers import fmt_uptime, is_admin, credits_text, trim_history
from bot.utils.keyboards import (
    account_actions_keyboard,
    accounts_keyboard,
    cancel_keyboard,
    dashboard_keyboard,
    paid_photo_account_keyboard,
    start_keyboard,
    toggle_keyboard,
)
from userbot.manager import add_userbot, remove_userbot, get_userbot

logger = logging.getLogger(__name__)

DASHBOARD_CAPTION = (
    "🗂 *Dashboard*\n\n"
    "• ➕ *Add Account* — connect a Telethon session string\n"
    "• 🗃 *Manage Accounts* — view, terminate connected accounts\n"
    "• ⚡ *Toggle AI* — turn AI auto-reply ON / OFF for all accounts\n"
    "• 📊 *Stats* — accounts, API credits, uptime\n"
    "• 💎 *Set Paid Photo* — auto-send a paid photo when triggered\n"
)


# ── helpers ───────────────────────────────────────────────────────────────────

async def _send_or_edit_photo(update: Update, caption: str, reply_markup, parse_mode="Markdown"):
    """Edit existing message if possible, otherwise send new."""
    q = update.callback_query
    try:
        await q.edit_message_media(
            media=__import__("telegram").InputMediaPhoto(
                media=config.DASHBOARD_IMAGE_URL,
                caption=caption,
                parse_mode=parse_mode,
            ),
            reply_markup=reply_markup,
        )
    except Exception:
        try:
            await q.edit_message_caption(
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except Exception:
            await q.message.reply_photo(
                photo=config.DASHBOARD_IMAGE_URL,
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )


def _guard(update: Update) -> bool:
    user = update.effective_user
    return is_admin(user.id, config.ADMIN_IDS)


# ── main callback router ──────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not _guard(update):
        await q.answer("⛔ Not authorised.", show_alert=True)
        return

    data = q.data

    # ── Home ────────────────────────────────────────────────────────────────
    if data == "back_home":
        context.user_data.clear()
        await _send_or_edit_photo(
            update,
            "✨ *Main Menu*\n\nSelect an option below.",
            start_keyboard(config.CONTACT_USERNAME),
        )

    # ── Dashboard ────────────────────────────────────────────────────────────
    elif data == "dashboard":
        context.user_data.clear()
        await _send_or_edit_photo(update, DASHBOARD_CAPTION, dashboard_keyboard())

    # ── Add Account ──────────────────────────────────────────────────────────
    elif data == "add_account":
        context.user_data["awaiting"] = "session_string"
        await q.edit_message_caption(
            caption=(
                "🔐 *Add Telethon Account*\n\n"
                "Send your *Telethon session string* as a text message.\n\n"
                "You can generate one with:\n"
                "`python -c \"from telethon.sync import TelegramClient; "
                "c=TelegramClient('x',API_ID,API_HASH).start(); print(c.session.save())\"`\n\n"
                "_Session strings are stored locally and never shared._"
            ),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("dashboard"),
        )

    # ── Manage Accounts ───────────────────────────────────────────────────────
    elif data == "manage_accounts":
        context.user_data.clear()
        accounts = db.get_accounts(config.DEFAULT_PERSONA)
        if not accounts:
            await q.edit_message_caption(
                caption="📭 *No accounts connected yet.*\n\nGo to Dashboard → Add Account.",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard("dashboard"),
            )
            return
        await q.edit_message_caption(
            caption="🗃 *Connected Accounts*\n\nTap an account to manage it.\n🟢 = AI ON  |  🔴 = AI OFF",
            parse_mode="Markdown",
            reply_markup=accounts_keyboard(accounts),
        )

    # ── Single account actions ────────────────────────────────────────────────
    elif data.startswith("acc_"):
        sid = data[4:]
        acc = db.get_account(sid, config.DEFAULT_PERSONA)
        if not acc:
            await q.answer("Account not found.", show_alert=True)
            return
        name   = acc.get("name") or acc.get("phone") or sid[:12]
        status = "🟢 ON" if acc.get("ai_enabled") else "🔴 OFF"
        await q.edit_message_caption(
            caption=(
                f"📱 *Account:* `{name}`\n"
                f"🤖 *AI Status:* {status}\n"
                f"🧠 *Persona:* `{acc['persona'][:60]}…`\n\n"
                "Choose an action:"
            ),
            parse_mode="Markdown",
            reply_markup=account_actions_keyboard(sid),
        )

    # ── Terminate account ─────────────────────────────────────────────────────
    elif data.startswith("terminate_"):
        sid = data[10:]
        acc = db.get_account(sid, config.DEFAULT_PERSONA)
        name = (acc.get("name") or sid[:12]) if acc else sid[:12]
        await remove_userbot(sid)
        db.remove_account(sid)
        await q.edit_message_caption(
            caption=f"🗑 Account *{name}* has been terminated and removed.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("manage_accounts"),
        )

    # ── Toggle AI ─────────────────────────────────────────────────────────────
    elif data == "toggle_ai":
        store = db.load()
        await q.edit_message_caption(
            caption=(
                f"⚡ *Global AI Toggle*\n\n"
                f"Current state: {'🟢 ON' if store['global_ai_on'] else '🔴 OFF'}\n\n"
                "This controls AI auto-reply for *all* connected accounts at once.\n"
                "Each account's individual AI state will be mirrored."
            ),
            parse_mode="Markdown",
            reply_markup=toggle_keyboard(store["global_ai_on"]),
        )

    elif data in ("do_toggle_on", "do_toggle_off"):
        new_state = data == "do_toggle_on"
        db.set_global_ai(new_state)
        icon = "🟢 ON" if new_state else "🔴 OFF"
        await q.edit_message_caption(
            caption=f"✅ Global AI has been turned *{icon}* for all accounts.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("dashboard"),
        )

    # ── Stats ─────────────────────────────────────────────────────────────────
    elif data == "stats":
        store    = db.load()
        accounts = store.get("accounts", {})
        uptime   = fmt_uptime(db.get_uptime_seconds())
        total    = store.get("total_api_calls", 0)
        ai_on    = sum(1 for a in accounts.values() if a.get("ai_enabled"))
        cred     = await fetch_credits()
        cred_str = credits_text(cred)

        await q.edit_message_caption(
            caption=(
                "📊 *Bot Statistics*\n\n"
                f"👥 *Accounts connected:* `{len(accounts)}`\n"
                f"🤖 *AI active on:* `{ai_on}` account(s)\n"
                f"🕐 *Uptime:* `{uptime}`\n"
                f"📡 *Total AI calls made:* `{total}`\n"
                f"💳 *OpenRouter Credits:*\n   `{cred_str}`\n"
            ),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("dashboard"),
        )

    # ── Set Paid Photo ────────────────────────────────────────────────────────
    elif data == "set_paid_photo":
        accounts = db.get_accounts(config.DEFAULT_PERSONA)
        if not accounts:
            await q.edit_message_caption(
                caption="📭 No accounts connected. Add one first.",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard("dashboard"),
            )
            return
        await q.edit_message_caption(
            caption="💎 *Set Paid Photo*\n\nSelect the account to configure:",
            parse_mode="Markdown",
            reply_markup=paid_photo_account_keyboard(accounts),
        )

    elif data.startswith("paidacc_"):
        sid = data[8:]
        context.user_data["awaiting"]     = "paid_photo"
        context.user_data["paid_acc_sid"] = sid
        acc  = db.get_account(sid, config.DEFAULT_PERSONA)
        name = (acc.get("name") or sid[:12]) if acc else sid[:12]
        current = "None set" if not (acc and acc.get("paid_photo_id")) else "Photo already set ✅"
        await q.edit_message_caption(
            caption=(
                f"💎 *Paid Photo — {name}*\n\n"
                f"Current: {current}\n\n"
                "📸 Send the *photo* you want to use as the paid media."
            ),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("set_paid_photo"),
        )


# ── text / photo message handler (used by conversation state) ─────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    awaiting = context.user_data.get("awaiting")

    # ── Session string input ──────────────────────────────────────────────────
    if awaiting == "session_string":
        session_string = update.message.text.strip() if update.message.text else ""
        if not session_string:
            await update.message.reply_text("❌ Please send a valid session string as text.")
            return

        await update.message.reply_text("⏳ Connecting to Telegram… please wait.")
        result = await add_userbot(session_string, config.DEFAULT_PERSONA)

        if result["ok"]:
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *Account connected successfully!*\n\n"
                f"👤 Name: `{result['name']}`\n"
                f"📱 Phone: `{result['phone']}`\n"
                f"🆔 Session ID: `{result['session_id']}`\n\n"
                "Use *Manage Accounts* to view or terminate it.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"❌ *Connection failed:*\n`{result['error']}`\n\n"
                "Make sure the session string is valid and try again.",
                parse_mode="Markdown",
            )

    # ── Paid photo — waiting for photo ────────────────────────────────────────
    elif awaiting == "paid_photo":
        if not update.message.photo:
            await update.message.reply_text("📸 Please send a *photo* (not a file).", parse_mode="Markdown")
            return
        file_id = update.message.photo[-1].file_id
        context.user_data["paid_photo_file_id"] = file_id
        context.user_data["awaiting"]           = "paid_stars"
        await update.message.reply_text(
            "⭐ Got the photo!\n\nNow send the *number of Telegram Stars* to charge (e.g. `15`).\n"
            "Send `0` for no star charge.",
            parse_mode="Markdown",
        )

    # ── Paid photo — waiting for stars ────────────────────────────────────────
    elif awaiting == "paid_stars":
        text = update.message.text.strip() if update.message.text else ""
        if not text.isdigit():
            await update.message.reply_text("❌ Please send a valid number (e.g. `15`).", parse_mode="Markdown")
            return
        stars     = int(text)
        sid       = context.user_data.get("paid_acc_sid", "")
        file_id   = context.user_data.get("paid_photo_file_id", "")

        if not sid or not file_id:
            await update.message.reply_text("❌ Session expired. Please start over from Set Paid Photo.")
            context.user_data.clear()
            return

        db.update_account(sid, paid_photo_id=file_id, paid_stars=stars)
        acc  = db.get_account(sid, config.DEFAULT_PERSONA)
        name = (acc.get("name") or sid[:12]) if acc else sid[:12]
        context.user_data.clear()

        star_text = f"{stars} ⭐" if stars else "No charge (free send)"
        await update.message.reply_text(
            f"✅ *Paid photo updated for {name}!*\n\n"
            f"Stars: {star_text}\n"
            f"Trigger word: `{config.PAID_TRIGGER_WORD}`\n\n"
            "The userbot will send this photo whenever the trigger word is received.",
            parse_mode="Markdown",
        )
