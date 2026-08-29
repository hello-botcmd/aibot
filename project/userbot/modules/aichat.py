"""
userbot/modules/aichat.py
──────────────────────────
Core AI auto-reply logic for each Telethon userbot account.
- Replies to private DMs with AI (OpenRouter).
- Sends paid photo when trigger word is detected.
- Per-account settings (persona, ai_enabled, history, cooldown).
- Thread-safe via per-user asyncio.Lock.
"""

import asyncio
import logging
import time

from telethon import TelegramClient, events

import config
from bot.utils import db
from bot.utils.ai import generate_reply
from bot.utils.helpers import trim_history

logger = logging.getLogger(__name__)

# Per-account, per-peer locks:  session_id → { peer_id → Lock }
_locks: dict[str, dict[str, asyncio.Lock]] = {}


def _get_lock(session_id: str, peer_id: str) -> asyncio.Lock:
    _locks.setdefault(session_id, {})
    if peer_id not in _locks[session_id]:
        _locks[session_id][peer_id] = asyncio.Lock()
    return _locks[session_id][peer_id]


async def _keep_typing(client: TelegramClient, peer, stop_event: asyncio.Event):
    """Continuously sends typing action until stop_event is set."""
    try:
        while not stop_event.is_set():
            await client.action(peer, "typing").__aenter__()
            await asyncio.sleep(4)
    except Exception:
        pass


def register(client: TelegramClient, session_id: str) -> None:
    """Register DM event handler on the given Telethon client."""

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_private_message(event):
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        peer_id  = str(sender.id)
        lock     = _get_lock(session_id, peer_id)

        async with lock:
            acc = db.get_account(session_id, config.DEFAULT_PERSONA)
            if acc is None:
                return

            # ── Global + per-account toggle ──────────────────────────────
            store = db.load()
            if not store.get("global_ai_on") and not acc.get("ai_enabled"):
                return

            # ── Must have text ───────────────────────────────────────────
            msg_text = event.message.text or ""
            if not msg_text.strip():
                return

            # ── Paid-photo trigger ───────────────────────────────────────
            if config.PAID_TRIGGER_WORD.lower() in msg_text.lower():
                paid_id    = acc.get("paid_photo_id")
                paid_stars = acc.get("paid_stars", 0)
                if paid_id:
                    try:
                        await client.send_file(
                            event.chat_id,
                            paid_id,
                        )
                        logger.info("[%s] Paid photo sent to %s", session_id, peer_id)
                    except Exception as exc:
                        logger.error("[%s] Paid photo send failed: %s", session_id, exc)
                    return  # don't also fire AI reply for trigger word

            # ── Cooldown ─────────────────────────────────────────────────
            now        = time.time()
            last_times = acc.setdefault("last_msg_time", {})
            if now - last_times.get(peer_id, 0) < config.COOLDOWN_SECONDS:
                return

            # ── Rate-limit gate ──────────────────────────────────────────
            if now < acc.get("rate_limited_until", 0):
                return

            # ── Length guard ─────────────────────────────────────────────
            if len(msg_text) > 1000:
                msg_text = msg_text[:1000]

            # Update cooldown timestamp immediately
            acc["last_msg_time"][peer_id] = now
            db.update_account(session_id, last_msg_time=acc["last_msg_time"])

            # ── Typing indicator ─────────────────────────────────────────
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(
                _keep_typing(client, event.chat_id, stop_typing)
            )

            try:
                history = acc.get("history", {}).get(peer_id, [])
                reply_text, retry_after = await generate_reply(
                    acc["persona"], history, msg_text
                )
            finally:
                stop_typing.set()
                typing_task.cancel()

            # ── Rate-limit handling ──────────────────────────────────────
            if retry_after:
                db.update_account(
                    session_id,
                    rate_limited_until=time.time() + retry_after,
                )
                # Don't reply to user on rate limit — silent fail
                return

            # ── Send reply ───────────────────────────────────────────────
            try:
                await event.reply(reply_text)
            except Exception as exc:
                logger.error("[%s] Reply send error: %s", session_id, exc)
                return

            db.increment_api_calls()

            # ── Update history ───────────────────────────────────────────
            history.append({"role": "user",      "text": msg_text})
            history.append({"role": "assistant", "text": reply_text})
            history = trim_history(history, config.MAX_HISTORY_TURNS)

            acc_history = acc.get("history", {})
            acc_history[peer_id] = history
            db.update_account(session_id, history=acc_history)

    logger.debug("[%s] AI chat handler registered.", session_id)
