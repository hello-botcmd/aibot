"""
bot/handlers/dashboard.py
──────────────────────────
All CallbackQueryHandler / MessageHandler logic for the dashboard flow.

State machine (stored in context.user_data):
  "awaiting"      : None | "session_string" | "paid_photo" | "paid_stars"
  "paid_acc_sid"  : str   ← which account the paid-photo flow targets

Paid photos are stored as **bytes on disk** (``data/paid/<sid>-paid.jpg``), not
as a Bot-API ``file_id``: a file_id minted by this dashboard bot cannot be
resolved by the userbot, which is a different Telegram identity.
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

import config
from bot.utils import db
from bot.utils.ai import fetch_credits
from bot.utils.helpers import credits_text, fmt_uptime, is_admin
from bot.utils.keyboards import (
    account_actions_keyboard,
    accounts_keyboard,
    cancel_keyboard,
    dashboard_keyboard,
    paid_menu_keyboard,
    paid_photo_account_keyboard,
    start_keyboard,
    toggle_keyboard,
)
from userbot.manager import add_userbot, get_userbot, terminate_userbot

logger = logging.getLogger(__name__)

DASHBOARD_CAPTION = (
    "🗂 *Dashboard*\n\n"
    "• ➕ *Add Account* — connect a Telethon session string\n"
    "• 🗃 *Manage Accounts* — view, terminate connected accounts\n"
    "• ⚡ *Toggle AI* — turn AI auto-reply ON / OFF for all accounts\n"
    "• 📊 *Stats* — accounts, API credits, uptime\n"
    "• 💎 *Set Paid Photo* — auto-send a paid photo when triggered\n"
)

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ── helpers ──────────────────────────────────────────────────────────────────

async def _send_or_edit_photo(update: Update, caption: str, reply_markup,
                              parse_mode="Markdown") -> None:
    """Edit the existing dashboard message if possible, otherwise send a new one."""
    q = update.callback_query
    try:
        await q.edit_message_media(
            media=InputMediaPhoto(media=config.DASHBOARD_IMAGE_URL,
                                  caption=caption, parse_mode=parse_mode),
            reply_markup=reply_markup,
        )
        return
    except Exception as exc:                       # noqa: BLE001
        logger.debug("edit_message_media failed, falling back: %s", exc)

    try:
        await q.edit_message_caption(caption=caption, parse_mode=parse_mode,
                                     reply_markup=reply_markup)
        return
    except Exception as exc:                       # noqa: BLE001
        logger.debug("edit_message_caption failed, sending new: %s", exc)

    await q.message.reply_photo(photo=config.DASHBOARD_IMAGE_URL, caption=caption,
                                parse_mode=parse_mode, reply_markup=reply_markup)


def _guard(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and is_admin(user.id, config.ADMIN_IDS)


def _acc_name(acc: dict | None, sid: str) -> str:
    if not acc:
        return sid[:12]
    return acc.get("name") or acc.get("phone") or sid[:12]


def _trigger_summary() -> str:
    words = list(getattr(config, "PAID_TRIGGER_WORDS", []) or [])
    if getattr(config, "PAID_TRIGGER_EXTRA_REGEX", ""):
        words.append(f"regex:{config.PAID_TRIGGER_EXTRA_REGEX}")
    return ", ".join(f"`{w}`" for w in words) if words else "_(none configured)_"


def _valid_sid(sid: str) -> bool:
    """Session ids are 12 hex chars — refuse anything else (path traversal)."""
    return bool(sid) and bool(_SESSION_ID_RE.match(sid)) and db.account_exists(sid)


def _paid_file_path(sid: str) -> tuple[Path, str]:
    """Absolute path + store-relative path for an account's paid photo."""
    from userbot import paidmedia

    path = config.PAID_DIR / f"{sid}-paid.jpg"
    return path, paidmedia.rel_for(path)


# ── main callback router ─────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not _guard(update):
        await q.answer("⛔ Not authorised.", show_alert=True)
        return

    data = q.data or ""

    # ── Home ────────────────────────────────────────────────────────────────
    if data == "back_home":
        context.user_data.clear()
        await _send_or_edit_photo(
            update, "✨ *Main Menu*\n\nSelect an option below.",
            start_keyboard(config.CONTACT_USERNAME))

    # ── Dashboard ───────────────────────────────────────────────────────────
    elif data == "dashboard":
        context.user_data.clear()
        await _send_or_edit_photo(update, DASHBOARD_CAPTION, dashboard_keyboard())

    # ── Add Account ─────────────────────────────────────────────────────────
    elif data == "add_account":
        context.user_data.clear()
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

    # ── Manage Accounts ─────────────────────────────────────────────────────
    elif data == "manage_accounts":
        context.user_data.clear()
        accounts = db.get_accounts(config.DEFAULT_PERSONA)
        if not accounts:
            await q.edit_message_caption(
                caption="📭 *No accounts connected yet.*\n\nGo to Dashboard → Add Account.",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard("dashboard"))
            return
        rows = "\n".join(
            f"• {'🟢' if a.get('ai_enabled') else '🔴'} `{_acc_name(a, sid)}`"
            for sid, a in accounts.items())
        await q.edit_message_caption(
            caption=(f"🗃 *Connected Accounts*\n\n{rows}\n\n"
                     "Tap an account to manage it.\n🟢 = AI ON  |  🔴 = AI OFF"),
            parse_mode="Markdown",
            reply_markup=accounts_keyboard(accounts))

    # ── Single account actions ──────────────────────────────────────────────
    elif data.startswith("acc_"):
        sid = data[4:]
        acc = db.get_account(sid, config.DEFAULT_PERSONA)
        if not acc:
            await q.answer("Account not found.", show_alert=True)
            return
        context.user_data.clear()
        name   = _acc_name(acc, sid)
        status = "🟢 ON" if acc.get("ai_enabled") else "🔴 OFF"
        live   = "🟢 connected" if get_userbot(sid) else "🔌 offline"
        persona = (acc.get("persona") or "")[:60] or "_(default)_"
        paid   = acc.get("paid") or {}
        paid_txt = (f"{paid.get('stars') or 0} ⭐" if paid.get("photo_file")
                    else "not set")
        await q.edit_message_caption(
            caption=(
                f"📱 *Account:* `{name}`\n"
                f"🤖 *AI Status:* {status}\n"
                f"🔌 *Client:* {live}\n"
                f"💬 *Conversations:* `{db.peer_count(sid)}`\n"
                f"💎 *Paid photo:* {paid_txt}\n"
                f"🧠 *Persona:* `{persona}…`\n\n"
                "Choose an action:"
            ),
            parse_mode="Markdown",
            reply_markup=account_actions_keyboard(sid))

    # ── Terminate account ───────────────────────────────────────────────────
    elif data.startswith("terminate_"):
        sid = data[10:]
        acc = db.get_account(sid, config.DEFAULT_PERSONA)
        name = _acc_name(acc, sid)
        context.user_data.clear()
        await terminate_userbot(sid)
        await q.edit_message_caption(
            caption=f"🗑 Account *{name}* has been terminated and removed.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("manage_accounts"))

    # ── Toggle AI ───────────────────────────────────────────────────────────
    elif data == "toggle_ai":
        context.user_data.clear()
        state = db.is_global_ai_on()
        await q.edit_message_caption(
            caption=(
                "⚡ *Global AI Toggle*\n\n"
                f"Current state: {'🟢 ON' if state else '🔴 OFF'}\n\n"
                "This is the master kill-switch and it also updates every "
                "account's individual AI preference.\n"
                "A reply is sent only when this **and** the account's own "
                "switch are ON."
            ),
            parse_mode="Markdown",
            reply_markup=toggle_keyboard(state))

    elif data in ("do_toggle_on", "do_toggle_off"):
        new_state = data == "do_toggle_on"
        db.set_global_ai(new_state)
        icon = "🟢 ON" if new_state else "🔴 OFF"
        await q.edit_message_caption(
            caption=f"✅ Global AI has been turned *{icon}* for all accounts.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("dashboard"))

    # ── Stats ───────────────────────────────────────────────────────────────
    elif data == "stats":
        context.user_data.clear()
        store    = db.load()
        accounts = store.get("accounts", {})
        counters = db.get_counters()
        uptime   = fmt_uptime(db.get_uptime_seconds())
        ai_on    = sum(1 for a in accounts.values() if a.get("ai_enabled"))
        cred     = await fetch_credits()
        cred_str = credits_text(cred)
        await q.edit_message_caption(
            caption=(
                "📊 *Bot Statistics*\n\n"
                f"👥 *Accounts connected:* `{len(accounts)}`\n"
                f"🤖 *AI active on:* `{ai_on}` account(s)\n"
                f"🕐 *Uptime:* `{uptime}`\n"
                f"📡 *AI calls:* `{store.get('total_api_calls', 0)}` "
                f"(✅ `{counters['calls_ok']}` / ❌ `{counters['calls_failed']}`)\n"
                f"💎 *Paid photos sent:* `{counters['paid_sends']}`\n"
                f"💳 *OpenRouter Credits:*\n   `{cred_str}`\n"
            ),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard("dashboard"))

    # ── Set Paid Photo ──────────────────────────────────────────────────────
    elif data == "set_paid_photo":
        context.user_data.clear()
        accounts = db.get_accounts(config.DEFAULT_PERSONA)
        if not accounts:
            await q.edit_message_caption(
                caption="📭 No accounts connected. Add one first.",
                parse_mode="Markdown",
                reply_markup=cancel_keyboard("dashboard"))
            return
        await q.edit_message_caption(
            caption=("💎 *Set Paid Photo*\n\nSelect the account to configure:\n\n"
                     f"Triggers: {_trigger_summary()}"),
            parse_mode="Markdown",
            reply_markup=paid_photo_account_keyboard(accounts))

    elif data.startswith("paidacc_"):
        sid = data[8:]
        acc = db.get_account(sid, config.DEFAULT_PERSONA)
        if not acc:
            await q.answer("Account not found.", show_alert=True)
            return
        context.user_data.clear()
        context.user_data["paid_acc_sid"] = sid
        await _render_paid_menu(update, sid)

    elif data.startswith("paidphoto_"):
        sid = data[10:]
        if not _valid_sid(sid):
            await q.answer("Account not found.", show_alert=True)
            return
        context.user_data["awaiting"]     = "paid_photo"
        context.user_data["paid_acc_sid"] = sid
        limit = getattr(config, "PAID_MAX_PHOTO_MB", 10)
        await q.edit_message_caption(
            caption=(f"📸 *Upload the paid photo*\n\n"
                     f"Send a photo (max ~{limit} MB).\n\n"
                     f"Triggers: {_trigger_summary()}"),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(f"paidacc_{sid}"))

    elif data.startswith("paidstars_"):
        sid = data[10:]
        if not _valid_sid(sid):
            await q.answer("Account not found.", show_alert=True)
            return
        paid = db.get_paid(sid)
        if not paid.get("photo_file"):
            await q.answer("Upload a photo first.", show_alert=True)
            return
        context.user_data["awaiting"]     = "paid_stars"
        context.user_data["paid_acc_sid"] = sid
        default = getattr(config, "PAID_DEFAULT_STARS", 0)
        lo = getattr(config, "PAID_MIN_STARS", 0)
        hi = getattr(config, "PAID_MAX_STARS", 5000)
        await q.edit_message_caption(
            caption=(f"⭐ *Star price* — currently `{paid.get('stars') or 0}`\n\n"
                     f"Send a number from `{lo}` to `{hi}`.\n"
                     f"Send `0` to deliver it for free.\n\n"
                     f"Suggested: `{default}`"),
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(f"paidacc_{sid}"))

    elif data.startswith("paidremove_"):
        sid = data[11:]
        if not _valid_sid(sid):
            await q.answer("Account not found.", show_alert=True)
            return
        db.clear_paid(sid, drop_files=True)
        context.user_data.clear()
        await q.edit_message_caption(
            caption="🗑 Paid photo removed for this account.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(f"acc_{sid}"))

    else:
        # Stale keyboard from an earlier version / expired menu.
        logger.debug("unhandled callback data: %r", data)
        await q.answer("This menu has expired — go back to the Dashboard.",
                       show_alert=True)


async def _render_paid_menu(update: Update, sid: str) -> None:
    """Show the per-account paid-photo submenu."""
    q = update.callback_query
    from userbot import paidmedia

    acc  = db.get_account(sid, config.DEFAULT_PERSONA) or {}
    paid = acc.get("paid") or {}
    rel  = paid.get("photo_file")
    stars = int(paid.get("stars") or 0)

    if rel and not paidmedia.file_for(rel):
        notice = "⚠️ The stored file is missing — re-upload it.\n\n"
    else:
        notice = ""

    body = (
        f"💎 *Paid Photo — {_acc_name(acc, sid)}*\n\n"
        f"{notice}"
        f"*Photo:* {f'`{rel}`' if rel else '_not set_'}\n"
        f"*Price:* {f'`{stars} ⭐`' if stars else '_free_'}\n"
        f"*Cached handle:* {paidmedia.describe(paid.get('photo_ref')) if rel else '—'}\n"
        f"*Stars supported:* `{paidmedia.supports_stars()}`\n"
        f"*Triggers:* {_trigger_summary()}"
    )
    await q.edit_message_caption(caption=body, parse_mode="Markdown",
                                 reply_markup=paid_menu_keyboard(sid, bool(rel)))


# ── text / photo message handler (conversation state) ────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update):
        return

    message = update.effective_message
    if message is None:
        return

    awaiting = context.user_data.get("awaiting")

    # ── Session string input ─────────────────────────────────────────────────
    if awaiting == "session_string":
        session_string = (message.text or "").strip()
        if not session_string:
            await message.reply_text("❌ Please send a valid session string as text.")
            return

        await message.reply_text("⏳ Connecting to Telegram… please wait.")
        result = await add_userbot(session_string, config.DEFAULT_PERSONA)

        if result["ok"]:
            context.user_data.clear()
            await message.reply_text(
                "✅ *Account connected successfully!*\n\n"
                f"👤 Name: `{result['name']}`\n"
                f"📱 Phone: `{result['phone']}`\n"
                f"🆔 Session ID: `{result['session_id']}`\n\n"
                "Use *Manage Accounts* to view or terminate it.",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                f"❌ *Connection failed:*\n`{result['error']}`\n\n"
                "Make sure the session string is valid and try again.",
                parse_mode="Markdown",
            )

    # ── Paid photo — waiting for the photo ───────────────────────────────────
    elif awaiting == "paid_photo":
        sid = context.user_data.get("paid_acc_sid", "")
        if not _valid_sid(sid):
            context.user_data.clear()
            await message.reply_text("❌ Session expired. Please start over from Set Paid Photo.")
            return

        if not message.photo:
            await message.reply_text("📸 Please send a *photo* (not a file).", parse_mode="Markdown")
            return

        photo = message.photo[-1]
        limit_mb = getattr(config, "PAID_MAX_PHOTO_MB", 10)
        if photo.file_size and photo.file_size > limit_mb * 1024 * 1024:
            await message.reply_text(
                f"❌ That photo is too large (max {limit_mb} MB). Send a smaller one.",
                parse_mode="Markdown")
            return

        path, rel = _paid_file_path(sid)
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            data = await tg_file.download_as_bytearray()
            if not data:
                raise ValueError("downloaded file is empty")
            config.PAID_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(data))
        except Exception as exc:                               # noqa: BLE001
            logger.error("paid photo download failed for %s: %s", sid, exc)
            await message.reply_text(
                f"❌ Could not download that photo: `{exc}`\nPlease try again.",
                parse_mode="Markdown")
            return

        # Bytes changed → any cached Telethon handle is now stale.
        db.update_paid(sid, photo_file=rel, photo_ref={})
        context.user_data["awaiting"] = "paid_stars"

        default = getattr(config, "PAID_DEFAULT_STARS", 0)
        lo = getattr(config, "PAID_MIN_STARS", 0)
        hi = getattr(config, "PAID_MAX_STARS", 5000)
        await message.reply_text(
            f"⭐ Got the photo! ({len(bytes(data)) // 1024} KB)\n\n"
            f"Now send the *number of Telegram Stars* to charge "
            f"(`{lo}`–`{hi}`, e.g. `{default}`).\n"
            "Send `0` for no star charge.",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(f"paidacc_{sid}"),
        )

    # ── Paid photo — waiting for the star price ──────────────────────────────
    elif awaiting == "paid_stars":
        sid = context.user_data.get("paid_acc_sid", "")
        text = (message.text or "").strip()

        if not text.lstrip("+-").isdigit():
            await message.reply_text("❌ Please send a valid whole number (e.g. `15`).",
                                     parse_mode="Markdown")
            return

        stars = int(text)
        lo = getattr(config, "PAID_MIN_STARS", 0)
        hi = getattr(config, "PAID_MAX_STARS", 5000)
        if not (lo <= stars <= hi):
            await message.reply_text(
                f"❌ Please send a number between `{lo}` and `{hi}`.", parse_mode="Markdown")
            return

        if not _valid_sid(sid):
            context.user_data.clear()
            await message.reply_text("❌ Session expired. Please start over from Set Paid Photo.")
            return

        paid = db.get_paid(sid)
        if not paid.get("photo_file"):
            await message.reply_text("❌ Upload the photo first from the Paid Photo menu.")
            context.user_data.clear()
            return

        db.update_paid(sid, stars=stars)
        acc  = db.get_account(sid, config.DEFAULT_PERSONA)
        name = _acc_name(acc, sid)
        context.user_data.clear()

        star_text = f"{stars} ⭐" if stars else "No charge (free send)"
        await message.reply_text(
            f"✅ *Paid photo updated for {name}!*\n\n"
            f"Stars: {star_text}\n"
            f"Triggers: {_trigger_summary()}\n\n"
            "The userbot will send this photo whenever a trigger word is received.",
            parse_mode="Markdown",
        )

    else:
        # Not in a conversation — ignore quietly instead of echoing errors.
        logger.debug("ignoring stray message in state %r", awaiting)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log PTB errors instead of letting them vanish into the polling loop."""
    logger.error("Exception while handling an update: %s", context.error, exc_info=context.error)
    with contextlib.suppress(Exception):
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Check the logs for details.")
