"""
userbot/modules/commands.py
────────────────────────────
Userbot self-commands (only you, the account owner, can trigger these).
Prefix: .  (dot commands in any chat / saved messages)

.ping   — latency check
.help   — list all dot commands
"""

from __future__ import annotations

import logging
import time

from telethon import TelegramClient, events

import config
from bot.utils import db

logger = logging.getLogger(__name__)

HELP_TEXT = """
**🤖 Userbot Commands**

`.ping`        — Check response latency
`.help`        — Show this help message
`.aichaton`    — Enable AI auto-reply for THIS account
`.aichatoff`   — Disable AI auto-reply for THIS account
`.setstatus`   — Show current AI status, persona & stats
`.resethistory <userid>` — Clear conversation history with a user
`.setpersona <text>` — Set custom AI persona for this account
`.paidstatus`  — Show the paid-photo configuration for this account
""".strip()


def register(client: TelegramClient, session_id: str) -> None:
    """Attach the dot-commands.  Idempotent per client instance."""
    if getattr(client, "_aibot_commands_registered", False):
        return
    client._aibot_commands_registered = True

    # ── .ping ────────────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ping$"))
    async def ping_handler(event):
        t0  = time.monotonic()
        msg = await event.edit("🏓 Pong!")
        ms  = (time.monotonic() - t0) * 1000
        await msg.edit(f"🏓 Pong! `{ms:.1f} ms`")

    # ── .help ────────────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.help$"))
    async def help_handler(event):
        await event.edit(HELP_TEXT, parse_mode="md")

    # ── .aichaton ───────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.aichaton$"))
    async def aichaton_handler(event):
        db.set_account_ai(session_id, True)
        note = ""
        if not db.is_global_ai_on():
            note = ("\n\n⚠️ The **global master switch is OFF** — this account is "
                    "enabled but will stay silent until you turn it on from the "
                    "dashboard (⚡ Toggle AI).")
        await event.edit("✅ AI auto-reply **enabled** for this account." + note,
                         parse_mode="md")

    # ── .aichatoff ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.aichatoff$"))
    async def aichatoff_handler(event):
        db.set_account_ai(session_id, False)
        await event.edit("❌ AI auto-reply **disabled** for this account.", parse_mode="md")

    # ── .setstatus ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.setstatus$"))
    async def setstatus_handler(event):
        acc     = db.get_account(session_id, config.DEFAULT_PERSONA) or {}
        status  = "🟢 ON" if acc.get("ai_enabled") else "🔴 OFF"
        glob    = "🟢 ON" if db.is_global_ai_on() else "🔴 OFF"
        persona = acc.get("persona") or config.DEFAULT_PERSONA
        limited = float(acc.get("rate_limited_until") or 0) - time.time()
        backoff = f"\n**Backoff:** {int(limited)}s remaining" if limited > 0 else ""
        await event.edit(
            f"**AI Status (this account):** {status}\n"
            f"**Global Master Toggle:** {glob}\n"
            f"**Conversations tracked:** `{db.peer_count(session_id)}`\n"
            f"**Persona:** `{persona[:100]}`" + backoff,
            parse_mode="md",
        )

    # ── .resethistory <userid> ───────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.resethistory\s+(\d+)$"))
    async def resethistory_handler(event):
        uid = event.pattern_match.group(1)
        if db.reset_peer_history(session_id, uid):
            await event.edit(f"🧹 History cleared for user `{uid}`.", parse_mode="md")
        else:
            await event.edit(f"ℹ️ No history found for user `{uid}`.", parse_mode="md")

    # ── .setpersona <text> ───────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.setpersona\s+(.+)$"))
    async def setpersona_handler(event):
        persona = event.pattern_match.group(1).strip()
        if not persona:
            await event.edit("❌ Persona cannot be empty.", parse_mode="md")
            return
        db.update_account(session_id, persona=persona)
        await event.edit(f"✅ Persona updated:\n`{persona}`", parse_mode="md")

    # ── .paidstatus ─────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.paidstatus$"))
    async def paidstatus_handler(event):
        from userbot import paidmedia

        paid = db.get_paid(session_id)
        rel  = paid.get("photo_file")
        if not rel:
            await event.edit("💎 No paid photo configured for this account.", parse_mode="md")
            return
        stars = int(paid.get("stars") or 0)
        await event.edit(
            f"💎 **Paid photo**\n"
            f"**File:** `{rel}`\n"
            f"**Stars:** `{stars or 'free'}`\n"
            f"**Cached handle:** {paidmedia.describe(paid.get('photo_ref'))}\n"
            f"**Stars supported:** `{paidmedia.supports_stars()}`",
            parse_mode="md",
        )

    logger.debug("[%s] Command handlers registered.", session_id[:8])
