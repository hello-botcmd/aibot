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

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from bot.utils import db

logger = logging.getLogger(__name__)

# In-memory registry:  session_id  →  TelegramClient
_clients: dict[str, TelegramClient] = {}


def _creds() -> tuple[int, str]:
    """
    Read API credentials at call time (not import time) so a late
    ``load_dotenv``/config reload is still honoured.
    """
    return int(getattr(config, "TELEGRAM_API_ID", 0) or 0), \
           str(getattr(config, "TELEGRAM_API_HASH", "") or "")


def _make_session_id(session_string: str) -> str:
    """Deterministic 12-char ID from the session string."""
    return hashlib.sha256(session_string.encode()).hexdigest()[:12]


async def _build_client(session_string: str) -> TelegramClient:
    api_id, api_hash = _creds()
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    return client


def _describe_me(me) -> tuple[str, str]:
    name  = f"{me.first_name or ''} {me.last_name or ''}".strip() or str(me.id)
    phone = me.phone or ""
    return name, phone


async def add_userbot(session_string: str, default_persona: str = "") -> dict:
    """
    Connect a new userbot.
    Returns:
      {"ok": True, "session_id": ..., "name": ..., "phone": ...}
      {"ok": False, "error": "..."}
    """
    api_id, api_hash = _creds()
    if not api_id or not api_hash:
        return {
            "ok": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH not set in config.py",
        }

    session_id = _make_session_id(session_string)

    if session_id in _clients:
        return {"ok": False, "error": "This account is already connected."}

    client: TelegramClient | None = None
    try:
        client = await _build_client(session_string)

        if not await client.is_user_authorized():
            await client.disconnect()
            return {"ok": False, "error": "Session string is invalid or expired."}

        me = await client.get_me()
        name, phone = _describe_me(me)

        _clients[session_id] = client

        # Persist to DB — but do NOT reset settings if this session was
        # already known (e.g. it failed to reconnect at boot): overwriting
        # would silently wipe the persona, history and paid config.
        persona = default_persona or getattr(config, "DEFAULT_PERSONA", "")
        if not db.account_exists(session_id):
            db.add_account(session_id, session_string, persona)
        db.update_account(session_id, name=name, phone=phone,
                          session_string=session_string)

        # Register event handlers for this client
        _register_handlers(client, session_id)

        logger.info("Userbot connected: %s (%s)", name, session_id)
        return {"ok": True, "session_id": session_id, "name": name, "phone": phone}

    except Exception as exc:                                    # noqa: BLE001
        logger.exception("Failed to connect userbot: %s", exc)
        # Drop the half-built client so a retry is not blocked by the
        # "already connected" guard above.
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        _clients.pop(session_id, None)
        return {"ok": False, "error": str(exc)}


async def remove_userbot(session_id: str) -> None:
    """Disconnect and forget a client (does not touch the DB)."""
    from userbot.modules.aichat import forget_session

    client = _clients.pop(session_id, None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()
    forget_session(session_id)                  # drop per-peer locks/cooldowns
    logger.info("Userbot removed: %s", session_id)


async def terminate_userbot(session_id: str) -> bool:
    """Full teardown: disconnect, forget, delete from the DB and its media."""
    await remove_userbot(session_id)
    return db.remove_account(session_id, drop_files=True)


def get_userbot(session_id: str) -> TelegramClient | None:
    return _clients.get(session_id)


def all_userbots() -> dict[str, TelegramClient]:
    return dict(_clients)


async def start_all_saved_userbots() -> None:
    """Called once on bot startup to reconnect all previously saved sessions."""
    drop_invalid = bool(getattr(config, "DROP_INVALID_SESSIONS_ON_BOOT", True))
    persona = getattr(config, "DEFAULT_PERSONA", "")
    accounts = db.get_accounts(persona)

    for sid, acc in list(accounts.items()):
        ss = (acc or {}).get("session_string", "")
        if not ss:
            continue
        if sid in _clients:
            continue
        try:
            client = await _build_client(ss)
            if not await client.is_user_authorized():
                await client.disconnect()
                if drop_invalid:
                    logger.warning("Saved session %s is no longer valid — removing it.", sid)
                    db.remove_account(sid, drop_files=True)
                else:
                    logger.warning("Saved session %s is no longer valid, skipping.", sid)
                continue

            me = await client.get_me()
            name, phone = _describe_me(me)
            db.update_account(sid, name=name, phone=phone)
            _clients[sid] = client
            _register_handlers(client, sid)
            logger.info("Reconnected saved userbot: %s (%s)", name, sid)
        except Exception as exc:                                # noqa: BLE001
            # Deliberately NOT deleted: a network blip or Telegram 500 is not
            # proof the session is dead, and dropping it would destroy the
            # user's account for good. Only `is_user_authorized() == False`
            # (above) is treated as a genuinely invalid session.
            logger.error("Could not reconnect %s: %s (session kept)", sid, exc)


# ── Event handler registration ────────────────────────────────────────────────

def _register_handlers(client: TelegramClient, session_id: str) -> None:
    """Attach all userbot event handlers to the given client (idempotent)."""
    from userbot.modules.aichat   import register as reg_ai
    from userbot.modules.commands import register as reg_cmd

    reg_ai(client, session_id)
    reg_cmd(client, session_id)
    logger.debug("Handlers registered for %s", session_id)


async def disconnect_all() -> None:
    """Disconnect every live client (shutdown path)."""
    for sid, client in list(_clients.items()):
        with contextlib.suppress(Exception):
            await client.disconnect()
            logger.info("Disconnected userbot %s", sid)
    _clients.clear()
