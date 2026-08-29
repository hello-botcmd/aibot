"""
userbot/manager.py
──────────────────
Manages the lifecycle of Telethon userbot clients.
Each session string maps to one running TelegramClient instance.

Public API
----------
add_userbot(session_string, default_persona) -> dict
remove_userbot(session_id)                  -> None
get_userbot(session_id)                     -> TelegramClient | None
all_userbots()                              -> dict[str, TelegramClient]
start_all_saved_userbots()                  -> None   (called on bot startup)
"""

import asyncio
import hashlib
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from bot.utils import db

logger = logging.getLogger(__name__)

# Loaded from environment / config — you must set these in config.py
# Telethon needs API_ID / API_HASH (get from https://my.telegram.org)
API_ID   = getattr(config, "TELEGRAM_API_ID",   0)
API_HASH = getattr(config, "TELEGRAM_API_HASH", "")

# In-memory registry:  session_id  →  TelegramClient
_clients: dict[str, TelegramClient] = {}


def _make_session_id(session_string: str) -> str:
    """Deterministic 12-char ID from the session string."""
    return hashlib.sha256(session_string.encode()).hexdigest()[:12]


async def _build_client(session_string: str) -> TelegramClient:
    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
    )
    await client.connect()
    return client


async def add_userbot(session_string: str, default_persona: str) -> dict:
    """
    Connect a new userbot.
    Returns:
      {"ok": True, "session_id": ..., "name": ..., "phone": ...}
      {"ok": False, "error": "..."}
    """
    if not API_ID or not API_HASH:
        return {
            "ok": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH not set in config.py",
        }

    session_id = _make_session_id(session_string)

    if session_id in _clients:
        return {"ok": False, "error": "This account is already connected."}

    try:
        client = await _build_client(session_string)

        if not await client.is_user_authorized():
            await client.disconnect()
            return {"ok": False, "error": "Session string is invalid or expired."}

        me    = await client.get_me()
        name  = f"{me.first_name or ''} {me.last_name or ''}".strip() or str(me.id)
        phone = me.phone or ""

        _clients[session_id] = client

        # Persist to DB
        db.add_account(session_id, session_string, default_persona)
        db.update_account(session_id, name=name, phone=phone)

        # Register event handlers for this client
        _register_handlers(client, session_id)

        logger.info("Userbot connected: %s (%s)", name, session_id)
        return {"ok": True, "session_id": session_id, "name": name, "phone": phone}

    except Exception as exc:
        logger.error("Failed to connect userbot: %s", exc)
        return {"ok": False, "error": str(exc)}


async def remove_userbot(session_id: str) -> None:
    client = _clients.pop(session_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("Error disconnecting %s: %s", session_id, exc)
    logger.info("Userbot removed: %s", session_id)


def get_userbot(session_id: str):
    return _clients.get(session_id)


def all_userbots() -> dict:
    return dict(_clients)


async def start_all_saved_userbots() -> None:
    """Called once on bot startup to reconnect all previously saved sessions."""
    accounts = db.get_accounts()
    for sid, acc in accounts.items():
        ss = acc.get("session_string", "")
        if not ss:
            continue
        if sid in _clients:
            continue
        try:
            client = await _build_client(ss)
            if not await client.is_user_authorized():
                logger.warning("Saved session %s is no longer valid, skipping.", sid)
                await client.disconnect()
                continue
            me    = await client.get_me()
            name  = f"{me.first_name or ''} {me.last_name or ''}".strip() or str(me.id)
            phone = me.phone or ""
            db.update_account(sid, name=name, phone=phone)
            _clients[sid] = client
            _register_handlers(client, sid)
            logger.info("Reconnected saved userbot: %s (%s)", name, sid)
        except Exception as exc:
            logger.error("Could not reconnect %s: %s", sid, exc)


# ── Event handler registration ────────────────────────────────────────────────

def _register_handlers(client: TelegramClient, session_id: str) -> None:
    """Attach all userbot event handlers to the given client."""
    from userbot.modules.aichat    import register as reg_ai
    from userbot.modules.commands  import register as reg_cmd

    reg_ai(client, session_id)
    reg_cmd(client, session_id)
    logger.debug("Handlers registered for %s", session_id)
