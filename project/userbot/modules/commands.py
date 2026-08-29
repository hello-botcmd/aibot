"""
userbot/modules/commands.py
────────────────────────────
Userbot self-commands (only you, the account owner, can trigger these).
Prefix: .  (dot commands in any chat / saved messages)

.ping   — latency check
.help   — list all dot commands
"""

import time
import logging

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
`.setstatus`   — Show current AI status & persona
`.resethistory <userid>` — Clear conversation history with a user
`.setpersona <text>` — Set custom AI persona for this account
""".strip()


def register(client: TelegramClient, session_id: str) -> None:

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
        db.update_account(session_id, ai_enabled=True)
        await event.edit("✅ AI auto-reply **enabled** for this account.")

    # ── .aichatoff ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.aichatoff$"))
    async def aichatoff_handler(event):
        db.update_account(session_id, ai_enabled=False)
        await event.edit("❌ AI auto-reply **disabled** for this account.")

    # ── .setstatus ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.setstatus$"))
    async def setstatus_handler(event):
        acc    = db.get_account(session_id, config.DEFAULT_PERSONA)
        status = "🟢 ON" if (acc and acc.get("ai_enabled")) else "🔴 OFF"
        store  = db.load()
        glob   = "🟢 ON" if store.get("global_ai_on") else "🔴 OFF"
        persona = (acc or {}).get("persona", config.DEFAULT_PERSONA)
        await event.edit(
            f"**AI Status (this account):** {status}\n"
            f"**Global Master Toggle:** {glob}\n"
            f"**Persona:** `{persona[:100]}`",
            parse_mode="md",
        )

    # ── .resethistory <userid> ───────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.resethistory\s+(\d+)$"))
    async def resethistory_handler(event):
        uid  = event.pattern_match.group(1)
        acc  = db.get_account(session_id, config.DEFAULT_PERSONA) or {}
        hist = acc.get("history", {})
        if uid in hist:
            del hist[uid]
            db.update_account(session_id, history=hist)
            await event.edit(f"🧹 History cleared for user `{uid}`.", parse_mode="md")
        else:
            await event.edit(f"ℹ️ No history found for user `{uid}`.", parse_mode="md")

    # ── .setpersona <text> ───────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.setpersona\s+(.+)$"))
    async def setpersona_handler(event):
        persona = event.pattern_match.group(1).strip()
        db.update_account(session_id, persona=persona)
        await event.edit(f"✅ Persona updated:\n`{persona}`", parse_mode="md")

    logger.debug("[%s] Command handlers registered.", session_id)
