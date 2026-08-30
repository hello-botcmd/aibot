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
* AI replies run through a per-peer pipeline (``_PeerQueue``) that holds **no
  lock across the network call**: one generation in flight, newest message
  wins, nothing is dropped.  The old design serialised every follow-up behind
  an in-flight generation and then silently discarded whatever arrived inside
  COOLDOWN_SECONDS — the "too much delayed" and "doesn't reply" symptoms.
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
from bot.utils import ai as ai_module
from bot.utils.ai import generate_reply
from bot.utils.helpers import match_trigger
from userbot import paidmedia

logger = logging.getLogger(__name__)

# Per-account, per-peer locks:  session_id → OrderedDict[peer_id → Lock]
# Bounded LRU — the old dict grew for every peer the account ever met.
_LOCKS_PER_ACCOUNT = 2048
_locks: dict[str, OrderedDict[str, asyncio.Lock]] = {}

_admin_notify_at: dict[str, float] = {}

# Own user id per session.  ``client.get_me()`` issues a fresh GetUsersRequest
# on *every* call (only ``get_me(input_peer=True)`` is served from Telethon's
# entity cache), so resolving it once per session removes one RPC per DM.
_self_ids: dict[str, int] = {}


class _PeerQueue:
    """
    Serialises AI work for one conversation.

    ``pending`` holds the newest message; a message that arrives while a reply
    is being generated simply *replaces* it.  That is the fix for the two worst
    symptoms:

      * the old code held an ``asyncio.Lock`` across the whole generation
        (up to 2 x OPENROUTER_TIMEOUT), so every follow-up queued behind it and
        paid another full generation — replies arrived minutes late;
      * COOLDOWN_SECONDS then silently *discarded* whatever arrived inside the
        window, so a person typing two short messages got one reply and the
        other vanished with only a debug-level log.

    Now: at most one generation in flight per peer, nothing is ever dropped,
    and a reply that has already been superseded is discarded so the answer
    always addresses what the person last said.
    """

    __slots__ = ("lock", "pending", "busy", "last_start", "superseded")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.pending: tuple | None = None      # (event, text)
        self.busy = False
        self.last_start = 0.0
        self.superseded = 0                    # consecutive discarded replies


# Bounded LRU, like the lock table — one queue per conversation we have *ever*
# seen would leak on a busy account.
_QUEUES_PER_ACCOUNT = 2048
_peer_queues: dict[str, OrderedDict[str, _PeerQueue]] = {}


def _peer_queue(session_id: str, peer_id: str) -> _PeerQueue:
    peers = _peer_queues.setdefault(session_id, OrderedDict())
    queue = peers.get(peer_id)
    if queue is None:
        queue = peers[peer_id] = _PeerQueue()
        if len(peers) > _QUEUES_PER_ACCOUNT:        # evict idle queues only
            for victim, victim_queue in list(peers.items()):
                if victim == peer_id or victim_queue.busy or victim_queue.lock.locked():
                    continue
                peers.pop(victim, None)
                if len(peers) <= _QUEUES_PER_ACCOUNT:
                    break
    else:
        peers.move_to_end(peer_id)
    return queue


def _cooldown_gap(queue: _PeerQueue) -> float:
    """Seconds to wait before starting the next generation for this peer."""
    cooldown = float(getattr(config, "COOLDOWN_SECONDS", 0) or 0)
    if cooldown <= 0:
        return 0.0
    return max(0.0, cooldown - (time.time() - queue.last_start))


# Consecutive AI failures per account, and when we last alerted about them.
# Reset on the first successful reply, so this tracks *sustained* breakage
# rather than the occasional blip.
_ai_failures: dict[str, int] = {}
_ai_alerted_at: dict[str, float] = {}


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
    _self_ids.pop(session_id, None)
    _ai_failures.pop(session_id, None)
    _ai_alerted_at.pop(session_id, None)
    _peer_queues.pop(session_id, None)


def failure_count(session_id: str) -> int:
    """Consecutive AI failures on this account (0 when healthy)."""
    return _ai_failures.get(session_id, 0)


def _note_failure(session_id: str) -> int:
    count = _ai_failures.get(session_id, 0) + 1
    _ai_failures[session_id] = count
    return count


def _clear_failures(session_id: str) -> None:
    """
    The link is healthy again — forget the streak *and* the "we already told
    the human" timer.  Without resetting ``_admin_notify_at`` a fresh outage
    right after a recovery would stay silent for a whole throttle window.
    """
    _ai_failures.pop(session_id, None)
    _admin_notify_at.pop(session_id, None)
    _ai_alerted_at.pop(session_id, None)


async def _own_id(client: TelegramClient, session_id: str) -> int | None:
    """This account's own user id, resolved once and then cached."""
    cached = _self_ids.get(session_id)
    if cached is not None:
        return cached
    try:
        # input_peer=True is answered from Telethon's entity cache — no RPC.
        peer = await client.get_me(input_peer=True)
        uid = getattr(peer, "user_id", None)
        if uid is None:                     # fall back for builds/edge cases
            me = await client.get_me()
            uid = getattr(me, "id", None)
        if uid is not None:
            _self_ids[session_id] = int(uid)
    except Exception as exc:                # noqa: BLE001
        # Unresolved is survivable: the handler simply proceeds.
        logger.debug("[%s] could not resolve own id: %s", session_id[:8], exc)
    return _self_ids.get(session_id)


def _trigger_matches(text: str) -> str | None:
    """Return the matched trigger word/phrase (or None)."""
    return match_trigger(text, getattr(config, "PAID_TRIGGER_WORDS", []),
                         getattr(config, "PAID_TRIGGER_EXTRA_REGEX", ""))


async def _keep_typing(client: TelegramClient, peer, stop: asyncio.Event) -> None:
    """
    Hold the typing indicator for as long as the model is thinking.

    Telethon's ``client.action()`` context manager already re-asserts the
    action every ``delay`` seconds (default 4s) via its own background task,
    so this must simply stay open until ``stop`` fires.  Wrapping it in an
    outer re-entry loop — as an earlier revision did — tears the context down
    every 4s, and because ``auto_cancel`` defaults to True that also fires a
    ``SendMessageCancelAction`` mid-generation, making the recipient's typing
    indicator flicker off and doubling the request count.
    """
    try:
        async with client.action(peer, "typing"):
            await stop.wait()
    except asyncio.CancelledError:                          # normal shutdown path
        return
    except (errors.RPCError, OSError) as exc:               # peer vanished, flood, …
        logger.debug("typing action stopped: %s", exc)
    except Exception as exc:                                # noqa: BLE001
        logger.debug("typing action failed: %s", exc)


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


async def _maybe_alert_failures(client: TelegramClient, sid: str, failures: int,
                                reason: str) -> None:
    """
    Tell the humans when AI replies keep failing for a non-billing reason.

    Without this a broken key, a DNS outage or a dead model silently turns the
    account into a black hole — peers get nothing and nothing is logged at a
    level anyone reads.
    """
    threshold = int(getattr(config, "AI_FAILURE_ALERT_THRESHOLD", 3) or 3)
    if failures < threshold:
        return
    throttle = int(getattr(config, "AI_FAILURE_ALERT_THROTTLE", 900) or 900)
    now = time.time()
    if now - _ai_alerted_at.get(sid, 0) < throttle:
        return
    _ai_alerted_at[sid] = now
    why = ai_module.REASON_TEXT.get(reason, reason)
    await _notify_admins(
        client, sid,
        f"⚠️ AI auto-reply is failing on this account: {failures} in a row.\n"
        f"Cause: {why} ({reason}). Model: {config.OPENROUTER_MODEL}.\n"
        f"Peers are getting no reply at all — check the OpenRouter key/credits "
        f"and the network.",
        throttle=throttle,
    )


async def _enqueue(client: TelegramClient, session_id: str, acc: dict,
                   peer_id: str, event, text: str) -> None:
    """
    Hand a message to the peer's pipeline: newest wins, never dropped.
    """
    queue = _peer_queue(session_id, peer_id)

    # Record the turn the moment it arrives, not when we get round to
    # answering it.  Messages coalesced into a newer one never reach
    # _run_turn, so recording there would drop them from the history and the
    # model would answer blind to half of what the person said.
    db.record_peer(session_id, peer_id, user_text=text)

    async with queue.lock:
        queue.pending = (event, text)
        if queue.busy:
            logger.info("[%s] peer %s: message coalesced into the in-flight reply",
                        session_id[:8], peer_id)
            return
        queue.busy = True

    try:
        while True:
            async with queue.lock:
                item, queue.pending = queue.pending, None
                if item is None:
                    queue.busy = False
                    break
            turn_event, turn_text = item
            replied = False
            try:
                replied = await _run_turn(client, session_id, acc, peer_id, queue,
                                          turn_event, turn_text)
            except Exception as exc:                    # noqa: BLE001
                logger.exception("[%s] peer %s: turn failed: %s",
                                 session_id[:8], peer_id, exc)

            # Pace only when more work is already waiting.  Sleeping on an idle
            # queue would tack COOLDOWN_SECONDS onto the *last* reply of a
            # burst, making every conversation feel laggy for no reason.
            async with queue.lock:
                if queue.pending is None:
                    queue.busy = False
                    break

            # And only after actually answering.  If this reply was discarded
            # as superseded the person is mid-sentence and waiting — pacing
            # here is the "why is it so slow?" bug all over again.
            if replied:
                gap = _cooldown_gap(queue)
                if gap > 0:
                    await asyncio.sleep(gap)
    except asyncio.CancelledError:
        async with queue.lock:
            queue.busy = False
        raise
    except Exception:                                   # noqa: BLE001
        async with queue.lock:
            queue.busy = False
            queue.pending = None
        raise


async def _run_turn(client: TelegramClient, session_id: str, acc: dict,
                    peer_id: str, queue: _PeerQueue, event, text: str) -> bool:
    """
    Generate and send one reply for ``text``.

    Returns True when a reply was actually sent, False when it was dropped
    (failed, or superseded by a newer message).
    """
    queue.last_start = time.time()

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(client, event.chat_id, stop_typing))
    try:
        history = db.peer_history(session_id, peer_id)
        reply = await generate_reply(
            acc.get("persona") or config.DEFAULT_PERSONA, history, text)
    finally:
        stop_typing.set()
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await typing_task

    if reply.retry_after:                          # 429 / 402 / auth
        db.set_rate_limited(session_id, time.time() + float(reply.retry_after))
        logger.info("[%s] backing off until %s", session_id[:8],
                    time.strftime("%H:%M",
                                  time.localtime(time.time() + reply.retry_after)))

    if not reply.ok:
        db.bump_counter("calls_failed")
        failures = _note_failure(session_id)
        why = ai_module.REASON_TEXT.get(reply.reason, reply.reason)

        if reply.retry_after and reply.retry_after >= getattr(
                config, "CRITICAL_BACKOFF_SECONDS", 3600):
            # Money/auth problems are always escalated immediately.
            await _notify_admins(
                client, session_id,
                f"⚠️ AI disabled for {reply.retry_after}s — OpenRouter error "
                f"(check key/credits). Cause: {why} ({reply.reason}). "
                f"Model: {config.OPENROUTER_MODEL}",
                throttle=3600)
        elif getattr(config, "AI_NOTIFY_ADMIN_ON_FAILURE", True):
            await _maybe_alert_failures(client, session_id, failures, reply.reason)

        fallback = getattr(config, "AI_PEER_FALLBACK", "") or ""
        if fallback and failures == int(getattr(config, "AI_FAILURE_ALERT_THRESHOLD", 3)):
            with contextlib.suppress(Exception):
                await client.send_message(event.chat_id, fallback)
        return False                               # silent: never expose quota to peers

    # ── did they say something new while the model was thinking? ──────────
    async with queue.lock:
        superseded = queue.pending is not None
    max_supersede = int(getattr(config, "AI_MAX_COALESCE", 3) or 3)
    if superseded and queue.superseded < max_supersede:
        queue.superseded += 1
        logger.info("[%s] peer %s: reply superseded by a newer message — discarding",
                    session_id[:8], peer_id)
        return False                               # the loop answers the newest one
    queue.superseded = 0

    try:
        await event.reply(reply.text)
    except Exception as exc:                       # noqa: BLE001
        logger.error("[%s] reply send error: %s", session_id, exc)
        db.bump_counter("calls_failed")
        return False

    _clear_failures(session_id)                    # link is healthy again
    db.bump_counter("calls_ok")
    db.record_peer(session_id, peer_id, assistant_text=reply.text)
    return True


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

        own_id = await _own_id(client, session_id)
        if own_id is not None and getattr(sender, "id", None) == own_id:
            return                                        # own DMs / Saved Messages

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

        peer_id = str(sender.id)
        acc = db.get_account(session_id, config.DEFAULT_PERSONA)
        if acc is None:
            return

        now = time.time()
        limited_until = float(acc.get("rate_limited_until") or 0)
        if now < limited_until:
            # Backoff is real (429 / billing) — but say so loudly.  A silent
            # skip here was indistinguishable from a broken bot.
            logger.info("[%s] muted for another %ds (rate_limited_until)",
                        session_id[:8], int(limited_until - now))
            return

        # ── 1. 💎 paid photo — runs whether or not AI is on ───────────────
        # Keeps its own short lock; it must never wait behind a slow model call.
        lock = _get_lock(session_id, peer_id)
        async with lock:
            fresh_acc = db.get_account(session_id, config.DEFAULT_PERSONA) or acc
            trigger = _trigger_matches(text)
            if trigger and (fresh_acc.get("paid") or {}).get("photo_file"):
                await _deliver_paid(client, session_id, fresh_acc, event.chat_id,
                                    peer_id, trigger)
                return
            if trigger:
                logger.info("[%s] trigger %r from %s but no paid photo configured",
                            session_id[:8], trigger, peer_id)

            if not db.is_global_ai_on():            # master kill-switch
                return
            if not acc.get("ai_enabled"):           # per-account preference
                return

        # ── 2. 🤖 AI auto-reply — coalesced per conversation ──────────────
        await _enqueue(client, session_id, acc, peer_id, event, text)

    logger.debug("[%s] AI chat handler registered.", session_id[:8])
