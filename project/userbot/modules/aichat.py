"""
userbot/modules/aichat.py
─────────────────────────
Per-account DM pipeline for every Telethon client:

    trigger word?  → deliver the PAID photo   (independent of the AI switch)
    else           → AI auto-reply             (global switch AND account switch)

Design notes
────────────
* 💎 paid-photo check runs **before** the AI gate — turning AI off for the
  evening no longer silently kills paywall delivery.
* 💎 Stars are actually charged (see :mod:`userbot.paidmedia`), and the media
  reference belongs to the userbot, not to the dashboard bot.
* trigger matching is whole-word, so "sender"/"sending" no longer buy anything
  (and no longer swallow the AI reply).
* ``.aichatoff`` really disables AI for one account: the gate is
  ``global_ai_on AND account.ai_enabled``, and the dashboard's
  "Turn ON/OFF All" is an explicit bulk set (see ``db.set_global_ai``).
* AI error filler is never written into the conversation history — the third
  element of ``generate_reply()`` says whether the text is a real answer.
* peer history/cooldowns are TTL- and size-capped; the per-peer lock table is
  bounded instead of growing forever.
* restart replay guard: messages older than REPLY_MAX_AGE_SECONDS are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict

from telethon import TelegramClient, errors, events

import config
from bot.utils import db
from bot.utils.ai import generate_reply
from bot.utils.helpers import match_trigger
from userbot import paidmedia

logger = logging.getLogger(__name__)

# Per-account, per-peer locks:  session_id → OrderedDict[peer_id → Lock]
# Bounded LRU — the old dict grew for every peer the account ever met.
_LOCKS_PER_ACCOUNT = 2048
_locks: dict[str, OrderedDict[str, asyncio.Lock]] = {}

_admin_notify_at: dict[str, float] = {}


def _get_lock(session_id: str, peer_id: str) -> asyncio.Lock:
    bucket = _locks.setdefault(session_id, OrderedDict())
    lock = bucket.get(peer_id)
    if lock is None:
        lock = asyncio.Lock()
        bucket[peer_id] = lock
        if len(bucket) > _LOCKS_PER_ACCOUNT:        # evict oldest *unheld* locks only
            for victim, held in list(bucket.items()):
                if victim == peer_id or held.locked():
                    continue
                bucket.pop(victim, None)
                if len(bucket) <= _LOCKS_PER_ACCOUNT:
                    break
    else:
        bucket.move_to_end(peer_id)
    return lock


def forget_session(session_id: str) -> None:
    """Drop all in-memory state for a terminated account."""
    _locks.pop(session_id, None)
    _admin_notify_at.pop(session_id, None)


def _trigger_matches(text: str) -> str | None:
    """Return the matched trigger word/phrase (or None)."""
    return match_trigger(text, getattr(config, "PAID_TRIGGER_WORDS", []),
                         getattr(config, "PAID_TRIGGER_EXTRA_REGEX", ""))


async def _keep_typing(client: TelegramClient, peer, stop: asyncio.Event) -> None:
    """
    Re-assert the typing action every ~4s while the model thinks.

    Uses the documented async context manager (the old code hand-called
    ``__aenter__`` and leaked it).
    """
    try:
        while not stop.is_set():
            try:
                async with client.action(peer, "typing"):
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=4.0)
            except (errors.RPCError, OSError) as exc:      # peer vanished, flood, …
                logger.debug("typing action stopped: %s", exc)
                return
    except asyncio.CancelledError:                          # normal shutdown path
        return


async def _notify_admins(client: TelegramClient, sid: str, text: str, *,
                         throttle: int = 300) -> None:
    """Tell the humans when money-relevant stuff breaks — but not every 5s."""
    if not getattr(config, "PAID_NOTIFY_ADMIN", True):
        return
    now = time.time()
    if now - _admin_notify_at.get(sid, 0) < throttle:
        return
    _admin_notify_at[sid] = now
    for admin_id in getattr(config, "ADMIN_IDS", []):
        try:
            await client.send_message(admin_id, f"[aibot · {sid[:8]}] {text}")
        except Exception as exc:                            # noqa: BLE001
            logger.debug("admin notify to %s failed: %s", admin_id, exc)


async def _deliver_paid(client: TelegramClient, sid: str, acc: dict, chat_id: int,
                        peer_id: str, trigger: str) -> bool:
    """
    Send the paid photo.  Returns True if it was delivered (or explicitly
    delivered for free), False if nothing was sent.
    """
    paid = acc.get("paid") or {}
    rel = paid.get("photo_file")
    if not rel:
        logger.info("[%s] trigger %r matched but no photo is set", sid[:8], trigger)
        return False

    stars = int(paid.get("stars") or 0)
    caption = paid.get("caption") or (
        config.PAID_LOCKED_CAPTION.format(stars=stars) if stars else config.PAID_FREE_CAPTION
    )
    teaser: dict = {}
    payload = f"aibot:{sid}:{trigger}"[:128]

    async def _ensure(ref_val, file_key, ref_key):
        if paidmedia.ref_ok(ref_val):
            return ref_val
        new = await paidmedia.persist(client, paid.get(file_key))
        db.update_paid(sid, **{ref_key: new})
        return new

    try:
        ref = await _ensure(paid.get("photo_ref"), "photo_file", "photo_ref")
        if stars > 0 and paid.get("teaser_file"):
            teaser = await _ensure(paid.get("teaser_ref"), "teaser_file", "teaser_ref")

        try:
            await paidmedia.send(client, chat_id, photo_ref=ref, stars=stars,
                                 caption=caption, teaser_ref=teaser, payload=payload)
        except paidmedia.NeedReupload:
            logger.info("[%s] photo handle expired for %s — re-importing", sid[:8], rel)
            ref = await paidmedia.persist(client, rel)
            db.update_paid(sid, photo_ref=ref)
            await paidmedia.send(client, chat_id, photo_ref=ref, stars=stars,
                                 caption=caption, teaser_ref=teaser, payload=payload)

    except (paidmedia.PaidUnsupported, paidmedia.MediaUnavailable) as exc:
        logger.error("[%s] paid send unavailable (%s): %s", sid[:8], type(exc).__name__, exc)
        await _notify_admins(
            client, sid,
            f"⚠️ Paid photo could NOT be sent to peer {peer_id}: {type(exc).__name__}: {exc}\n"
            f"stars={stars} file={rel}",
        )
        if getattr(config, "PAID_FALLBACK_FREE", False):
            with contextlib.suppress(Exception):
                await paidmedia.send_from_disk(client, chat_id, rel, caption="")
                db.bump_counter("paid_sends")
                db.record_peer(sid, peer_id, count_send=True)
                return True
        with contextlib.suppress(Exception):
            await client.send_message(chat_id, config.PAID_BUSY_TEXT)
        return False

    except errors.FloodWaitError as exc:
        logger.warning("[%s] flood wait %ss while sending paid photo", sid[:8], exc.seconds)
        db.set_rate_limited(sid, time.time() + int(exc.seconds or 0))
        return False

    except Exception as exc:                                # noqa: BLE001
        logger.exception("[%s] paid send failed: %s", sid[:8], exc)
        await _notify_admins(client, sid, f"⚠️ Paid send error: {exc}")
        return False

    db.bump_counter("paid_sends")
    db.record_peer(sid, peer_id, count_send=True)
    logger.info("[%s] 💎 paid photo sent to %s (%s ⭐, trigger=%r)",
                sid[:8], chat_id, stars or "free", trigger)
    return True


def register(client: TelegramClient, session_id: str) -> None:
    """Attach the DM handler to ``client`` (idempotent per client instance)."""
    if getattr(client, "_aibot_aichat_registered", False):
        return
    client._aibot_aichat_registered = True

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_private_message(event):
        sender = await event.get_sender()
        if sender is None:
            return
        if getattr(sender, "bot", False) and getattr(config, "IGNORE_BOTS", True):
            return

        try:
            own = await client.get_me()
            if own is not None and getattr(sender, "id", None) == own.id:
                return                                    # own DMs / Saved Messages
        except Exception:                                 # noqa: BLE001
            pass

        # ── ignore ancient messages replayed after a restart ─────────────
        max_age = getattr(config, "REPLY_MAX_AGE_SECONDS", 0)
        msg_date = getattr(event.message, "date", None)
        if max_age and msg_date is not None:
            stamp = getattr(msg_date, "timestamp", None)
            if callable(stamp):                       # datetime.timestamp()
                stamp = stamp()
            if isinstance(stamp, (int, float)):       # unparsable → don't drop it
                age = time.time() - float(stamp)
                if age > max_age:
                    logger.debug("[%s] ignoring %ss-old message", session_id[:8], int(age))
                    return

        text = (event.message.text or "").strip()
        if not text:
            return
        if len(text) > getattr(config, "MAX_INPUT_CHARS", 1000):
            text = text[: getattr(config, "MAX_INPUT_CHARS", 1000)]

        acc = db.get_account(session_id, config.DEFAULT_PERSONA)
        if acc is None:
            return

        peer_id = str(sender.id)
        lock = _get_lock(session_id, peer_id)

        async with lock:
            # ── 1. 💎 paid photo — runs whether or not AI is on ────────────
            trigger = _trigger_matches(text)
            if trigger and (acc.get("paid") or {}).get("photo_file"):
                await _deliver_paid(client, session_id, acc, event.chat_id, peer_id, trigger)
                return
            if trigger:
                logger.info("[%s] trigger %r from %s but no paid photo configured",
                            session_id[:8], trigger, peer_id)

            # ── 2. 🤖 AI auto-reply ───────────────────────────────────────
            if not db.is_global_ai_on():                # master kill-switch
                return
            if not acc.get("ai_enabled"):               # per-account preference
                return

            now = time.time()
            if now - db.peer_last_ts(session_id, peer_id) < getattr(config, "COOLDOWN_SECONDS", 0):
                return
            if now < float(acc.get("rate_limited_until") or 0):
                return

            db.record_peer(session_id, peer_id)        # stamp activity immediately

            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(_keep_typing(client, event.chat_id, stop_typing))
            try:
                history = db.peer_history(session_id, peer_id)
                reply_text, retry_after, ok = await generate_reply(
                    acc.get("persona") or config.DEFAULT_PERSONA, history, text)
            finally:
                stop_typing.set()
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await typing_task

            if retry_after:                            # 429 / 402 / auth
                db.set_rate_limited(session_id, time.time() + float(retry_after))
                logger.info("[%s] backing off until %s", session_id[:8],
                            time.strftime("%H:%M", time.localtime(time.time() + retry_after)))

            if not ok:
                db.bump_counter("calls_failed")
                if retry_after and retry_after >= getattr(config, "CRITICAL_BACKOFF_SECONDS", 3600):
                    await _notify_admins(
                        client, session_id,
                        f"⚠️ AI disabled for {retry_after}s — OpenRouter error "
                        f"(check key/credits). Model: {config.OPENROUTER_MODEL}",
                        throttle=3600)
                return                                 # silent: never expose quota to peers

            try:
                await event.reply(reply_text)
            except Exception as exc:                   # noqa: BLE001
                logger.error("[%s] reply send error: %s", session_id, exc)
                db.bump_counter("calls_failed")
                return

            db.bump_counter("calls_ok")
            # only REAL answers enter the history (no "busy right now" filler)
            db.record_peer(session_id, peer_id,
                           user_text=text, assistant_text=reply_text)

    logger.debug("[%s] AI chat handler registered.", session_id[:8])
