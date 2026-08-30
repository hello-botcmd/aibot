"""
bot/utils/db.py
───────────────
Single source of truth for all persistent data.  Stored as JSON in
``data/store.json``, held in an in-memory cache, and flushed **atomically**.

Schema
------
{
  "accounts": {
    "<session_id>": {
      "session_string":    "...",
      "phone":             "...",
      "name":              "...",
      "ai_enabled":        false,
      "persona":           "...",
      "rate_limited_until": 0,
      "paid": {
        "photo_file":  "paid/<sid>-paid.jpg",   # relative to DATA_DIR
        "teaser_file": null,
        "photo_ref":   {},                      # cached Telethon InputPhoto
        "teaser_ref":  {},
        "stars":       0,
        "caption":     ""
      },
      "peers": {                                # per-conversation state
        "<peer_id>": {
          "history": [{"role": "user"|"assistant", "text": "..."}],
          "last_ts": 1720000000.0,
          "sends":   0
        }
      }
    }
  },
  "global_ai_on":    false,   # master kill-switch
  "uptime_start":    0,       # reset on every boot → this is *process* uptime
  "total_api_calls": 0,
  "counters": {"calls_ok": 0, "calls_failed": 0, "paid_sends": 0}
}

Concurrency
-----------
Everything runs on the admin bot's event loop, but ``save_soon()`` flushes on
a background timer so a burst of messages costs one write, not fifty.  All
writes go through a lock and land via ``os.replace`` — a crash can never leave
a half-written store behind.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from bot.utils.helpers import trim_history

logger = logging.getLogger(__name__)

_DATA_DIR  = Path(__file__).resolve().parents[2] / "data"
_DATA_FILE = _DATA_DIR / "store.json"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

try:                                            # keep db importable standalone
    import config
except ImportError:                             # pragma: no cover
    config = None


def _cfg(name: str, default):
    return getattr(config, name, default) if config is not None else default


_DEFAULTS: dict = {
    "accounts":        {},
    "global_ai_on":    False,
    "uptime_start":    0,
    "total_api_calls": 0,
    "counters":        {"calls_ok": 0, "calls_failed": 0, "paid_sends": 0},
}

_COUNTER_KEYS = ("calls_ok", "calls_failed", "paid_sends")

_PAID_DEFAULTS: dict = {
    "photo_file":  None,     # path relative to DATA_DIR
    "teaser_file": None,
    "photo_ref":   {},       # cached Telethon handle for photo_file
    "teaser_ref":  {},       # cached Telethon handle for teaser_file
    "stars":       0,
    "caption":     "",
}

_PEER_DEFAULTS: dict = {
    "history": [],
    "last_ts": 0.0,
    "sends":   0,
}

_ACCOUNT_DEFAULTS: dict = {
    "session_string":     "",
    "phone":              "",
    "name":               "",
    "ai_enabled":         False,
    "persona":            "",
    "peers":              {},
    "rate_limited_until": 0,
    "paid":               {},
}


# ── in-memory cache ──────────────────────────────────────────────────────────

_cache: dict | None = None
_io_lock      = threading.RLock()      # guards the actual disk write
_debounce_lock = threading.Lock()
_flush_timer: threading.Timer | None = None
_flush_delay  = 2.0                    # seconds; coalesces bursts into one write


# ── load / flush ─────────────────────────────────────────────────────────────

def _new_peer() -> dict:
    return copy.deepcopy(_PEER_DEFAULTS)


def _read_disk() -> dict | None:
    """
    Return the parsed store, or None when there is nothing sane to load.

    A corrupt store is *quarantined* (renamed to ``store.corrupt-<ts>.json``)
    instead of being silently overwritten — the old code reset to defaults and
    threw away every session string without a trace.
    """
    if not _DATA_FILE.exists():
        return None
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("store root is not a JSON object")
        return data
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        backup = _DATA_FILE.with_name(f"store.corrupt-{int(time.time())}.json")
        with contextlib.suppress(OSError):
            _DATA_FILE.replace(backup)
        logger.error("data/store.json is unreadable (%s). Quarantined as %s "
                     "and starting from a clean store.", exc, backup.name)
        return None


def _migrate_legacy_paid(acc: dict) -> None:
    """
    The dashboard used to store a Bot-API ``file_id`` minted by the *bot*,
    which the *userbot* can never resolve.  Those keys are unsalvageable, so
    drop them and say so rather than pretending a photo is configured.
    """
    if acc.pop("paid_photo_id", None) or acc.pop("paid_stars", None):
        logger.warning("Dropped legacy paid_photo_id for %s — a Bot-API file_id "
                       "cannot be used by the userbot. Re-upload the photo via "
                       "Dashboard → Set Paid Photo.",
                       acc.get("name") or acc.get("phone") or "?")


def _migrate_legacy_peers(acc: dict) -> None:
    """Fold the old flat ``history`` / ``last_msg_time`` maps into ``peers``."""
    peers = acc.get("peers")
    if not isinstance(peers, dict):
        peers = {}

    legacy_hist = acc.get("history")
    if isinstance(legacy_hist, dict):
        for pid, turns in legacy_hist.items():
            entry = peers.setdefault(str(pid), _new_peer())
            if isinstance(turns, list) and turns and not entry["history"]:
                entry["history"] = [t for t in turns if isinstance(t, dict)]

    legacy_ts = acc.get("last_msg_time")
    if isinstance(legacy_ts, dict):
        for pid, ts in legacy_ts.items():
            entry = peers.setdefault(str(pid), _new_peer())
            with contextlib.suppress(TypeError, ValueError):
                ts = float(ts)
                if ts > entry["last_ts"]:
                    entry["last_ts"] = ts

    acc.pop("history", None)
    acc.pop("last_msg_time", None)
    acc["peers"] = peers


def _coerce(acc: dict, key: str, default) -> None:
    """Force ``acc[key]`` to the right type, repairing broken/partial records."""
    value = acc.get(key)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            acc[key] = default if value is None else bool(value)
    elif isinstance(default, dict):
        if not isinstance(value, dict):
            acc[key] = copy.deepcopy(default)
    elif isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            acc[key] = default
    elif not isinstance(value, str):
        acc[key] = default


def _normalise_account(acc: dict, default_persona: str = "") -> dict:
    for key, value in _ACCOUNT_DEFAULTS.items():
        _coerce(acc, key, value)
    if not acc.get("persona"):
        acc["persona"] = default_persona or _cfg("DEFAULT_PERSONA", "")

    _migrate_legacy_paid(acc)

    paid = copy.deepcopy(_PAID_DEFAULTS)
    if isinstance(acc.get("paid"), dict):
        paid.update(acc["paid"])
    acc["paid"] = paid

    _migrate_legacy_peers(acc)

    with contextlib.suppress(TypeError, ValueError):
        acc["rate_limited_until"] = float(acc.get("rate_limited_until") or 0)
    return acc


def _init_cache() -> dict:
    global _cache
    data = _read_disk()
    if data is None:
        data = copy.deepcopy(_DEFAULTS)
    else:
        for key, value in _DEFAULTS.items():
            if key not in data:
                data[key] = copy.deepcopy(value)
        if not isinstance(data.get("accounts"), dict):
            data["accounts"] = {}
        if not isinstance(data.get("counters"), dict):
            data["counters"] = copy.deepcopy(_DEFAULTS["counters"])

    data["uptime_start"] = int(time.time())     # process uptime, not "since install"

    for key in _COUNTER_KEYS:
        data["counters"].setdefault(key, 0)

    _cache = data
    return _cache


def _write_atomic(data: dict) -> None:
    """Serialize ``data`` to store.json via a temp file + atomic rename."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(_DATA_DIR), prefix=".store-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _DATA_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _flush() -> None:
    """Write the in-memory cache to disk.  Safe to call from any thread."""
    global _cache
    with _io_lock:
        if _cache is None:
            return
        _write_atomic(_cache)
        logger.debug("store flushed (%d account(s))", len(_cache.get("accounts", {})))


def load() -> dict:
    global _cache
    if _cache is None:
        with _io_lock:
            if _cache is None:
                _init_cache()
    return _cache


def save() -> None:
    """Flush immediately (startup, shutdown, and anything user-visible)."""
    load()
    _flush()


def save_soon(delay: float | None = None) -> None:
    """
    Coalesced persist — schedule a write ``delay`` seconds from now unless one
    is already pending.  Used on the hot path (every incoming DM).
    """
    global _flush_timer
    with _debounce_lock:
        if _flush_timer is not None and _flush_timer.is_alive():
            return                                  # a write is already coming
        timer = threading.Timer(delay if delay is not None else _flush_delay,
                                _debounced_flush)
        timer.daemon = True
        _flush_timer = timer
        timer.start()


def _debounced_flush() -> None:
    global _flush_timer
    try:
        _flush()
    except Exception as exc:                        # noqa: BLE001
        logger.error("deferred store flush failed: %s", exc)
    finally:
        with _debounce_lock:
            _flush_timer = None


def flush_pending() -> None:
    """Cancel any pending debounce and write now (used on shutdown)."""
    global _flush_timer
    with _debounce_lock:
        timer, _flush_timer = _flush_timer, None
    if timer is not None:
        timer.cancel()
    _flush()


# ── account helpers ──────────────────────────────────────────────────────────

def get_accounts(default_persona: str = "") -> dict:
    data = load()
    for acc in data["accounts"].values():
        if isinstance(acc, dict):
            _normalise_account(acc, default_persona)
    return data["accounts"]


def add_account(session_id: str, session_string: str, default_persona: str = "") -> dict:
    data = load()
    acc = copy.deepcopy(_ACCOUNT_DEFAULTS)
    acc["session_string"] = session_string
    acc["persona"]        = default_persona or _cfg("DEFAULT_PERSONA", "")
    acc["ai_enabled"]     = bool(_cfg("AI_DEFAULT_ENABLED", False))
    acc["paid"]           = copy.deepcopy(_PAID_DEFAULTS)
    acc["peers"]          = {}
    data["accounts"][session_id] = acc
    save_soon()
    return acc


def remove_account(session_id: str, drop_files: bool = True) -> bool:
    """Remove an account and (optionally) its on-disk paid media."""
    data = load()
    acc = data["accounts"].pop(session_id, None)
    if acc is None:
        return False
    if drop_files and isinstance(acc, dict):
        paid = acc.get("paid") or {}
        for key in ("photo_file", "teaser_file"):
            rel = paid.get(key)
            if rel:
                delete_media(rel)
    save_soon()
    return True


def get_account(session_id: str, default_persona: str = "") -> dict | None:
    data = load()
    acc = data["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return None
    return _normalise_account(acc, default_persona)


def update_account(session_id: str, **kwargs) -> bool:
    """
    Patch an existing account.  Returns False (and does nothing) when the
    session id is unknown — the old version silently created a phantom
    account with none of the required defaults.
    """
    data = load()
    acc = data["accounts"].get(session_id)
    if not isinstance(acc, dict):
        logger.warning("update_account(%s): no such account — ignored", session_id)
        return False
    acc.update(kwargs)
    save_soon()
    return True


def account_exists(session_id: str) -> bool:
    return isinstance(load()["accounts"].get(session_id), dict)


# ── global / per-account AI switches ─────────────────────────────────────────

def is_global_ai_on() -> bool:
    return bool(load().get("global_ai_on"))


def set_global_ai(state: bool) -> None:
    """
    Master switch.  It sets ``global_ai_on`` *and* explicitly bulk-sets every
    account's ``ai_enabled``, so the dashboard and the per-account state can
    never disagree (the old code set only the flag and left stale per-account
    values behind).
    """
    data = load()
    data["global_ai_on"] = bool(state)
    for acc in data["accounts"].values():
        if isinstance(acc, dict):
            acc["ai_enabled"] = bool(state)
    save()


def set_account_ai(session_id: str, state: bool) -> bool:
    """Per-account preference (``.aichaton`` / ``.aichatoff``)."""
    return update_account(session_id, ai_enabled=bool(state))


def set_rate_limited(session_id: str, until_ts: float) -> bool:
    return update_account(session_id, rate_limited_until=float(until_ts))


# ── paid photo ───────────────────────────────────────────────────────────────

def get_paid(session_id: str) -> dict:
    acc = get_account(session_id) or {}
    paid = acc.get("paid")
    return paid if isinstance(paid, dict) else copy.deepcopy(_PAID_DEFAULTS)


def update_paid(session_id: str, **kwargs) -> bool:
    """
    Merge ``kwargs`` into ``account["paid"]``.

    Passing ``photo_file=`` automatically invalidates the cached
    ``photo_ref`` — otherwise a stale Telethon handle would keep serving the
    previously uploaded photo.
    """
    data = load()
    acc = data["accounts"].get(session_id)
    if not isinstance(acc, dict):
        logger.warning("update_paid(%s): no such account — ignored", session_id)
        return False

    paid = copy.deepcopy(_PAID_DEFAULTS)
    if isinstance(acc.get("paid"), dict):
        paid.update(acc["paid"])

    if kwargs.get("photo_file") is not None and kwargs["photo_file"] != paid.get("photo_file"):
        paid["photo_ref"] = {}
    if kwargs.get("teaser_file") is not None and kwargs["teaser_file"] != paid.get("teaser_file"):
        paid["teaser_ref"] = {}

    paid.update(kwargs)
    acc["paid"] = paid
    save_soon()
    return True


def clear_paid(session_id: str, drop_files: bool = True) -> bool:
    """Remove the paid configuration and its cached handles."""
    acc = (load()["accounts"].get(session_id) or {})
    if drop_files:
        for key in ("photo_file", "teaser_file"):
            rel = (acc.get("paid") or {}).get(key)
            if rel:
                delete_media(rel)
    return update_paid(session_id, **copy.deepcopy(_PAID_DEFAULTS))


def delete_media(rel_path: str) -> bool:
    """Delete a media file stored under DATA_DIR.  Refuses path traversal."""
    if not rel_path:
        return False
    try:
        path = (_DATA_DIR / rel_path).resolve()
        if _DATA_DIR.resolve() not in path.parents:
            logger.warning("refusing to delete %s (outside data dir)", rel_path)
            return False
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("could not delete media %s: %s", rel_path, exc)
        return False


# ── per-peer state (history, cooldown, stats) ────────────────────────────────

def _peer_entry(acc: dict, peer_id: str, create: bool = False) -> dict | None:
    peers = acc.setdefault("peers", {})
    if not isinstance(peers, dict):
        peers = acc["peers"] = {}
    entry = peers.get(peer_id)
    if entry is None:
        if not create:
            return None
        entry = peers[peer_id] = _new_peer()
    if not isinstance(entry, dict):
        entry = peers[peer_id] = _new_peer()
    for key, value in _PEER_DEFAULTS.items():
        entry.setdefault(key, copy.deepcopy(value))
    return entry


def _prune_peers(acc: dict) -> None:
    """Enforce PEER_TTL_DAYS (idle eviction) and MAX_HISTORY_PEERS (size cap)."""
    peers = acc.get("peers")
    if not isinstance(peers, dict) or not peers:
        return

    ttl_days = _cfg("PEER_TTL_DAYS", 0)
    try:
        ttl_days = float(ttl_days or 0)
    except (TypeError, ValueError):
        ttl_days = 0
    if ttl_days > 0:
        cutoff = time.time() - ttl_days * 86400
        for pid in [p for p, e in list(peers.items())
                    if isinstance(e, dict) and float(e.get("last_ts") or 0) < cutoff]:
            peers.pop(pid, None)

    cap = _cfg("MAX_HISTORY_PEERS", 0)
    try:
        cap = int(cap or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap > 0 and len(peers) > cap:
        ordered = sorted(peers.items(),
                         key=lambda kv: float(kv[1].get("last_ts") or 0) if isinstance(kv[1], dict) else 0.0)
        for pid, _ in ordered[: len(peers) - cap]:
            peers.pop(pid, None)


def peer_history(session_id: str, peer_id: str, max_turns: int | None = None) -> list:
    """Conversation history for one peer, already trimmed to the turn cap."""
    acc = load()["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return []
    entry = _peer_entry(acc, str(peer_id))
    if entry is None:
        return []
    turns = max_turns if max_turns is not None else _cfg("MAX_HISTORY_TURNS", 6)
    history = [t for t in (entry.get("history") or []) if isinstance(t, dict)]
    return trim_history(history, turns)


def peer_last_ts(session_id: str, peer_id: str) -> float:
    """Timestamp of the last handled message from this peer (0.0 if never)."""
    acc = load()["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return 0.0
    entry = _peer_entry(acc, str(peer_id))
    if entry is None:
        return 0.0
    with contextlib.suppress(TypeError, ValueError):
        return float(entry.get("last_ts") or 0.0)
    return 0.0


def record_peer(session_id: str, peer_id: str, *,
                user_text: str | None = None,
                assistant_text: str | None = None,
                count_send: bool = False) -> None:
    """
    Stamp activity for a peer and optionally append a turn / count a send.

    Called on the hot path, so it only ever schedules a debounced write.
    """
    data = load()
    acc = data["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return

    entry = _peer_entry(acc, str(peer_id), create=True)
    entry["last_ts"] = time.time()

    if count_send:
        with contextlib.suppress(TypeError, ValueError):
            entry["sends"] = int(entry.get("sends") or 0) + 1

    if user_text is not None or assistant_text is not None:
        history = entry.setdefault("history", [])
        if not isinstance(history, list):
            history = entry["history"] = []
        if user_text is not None:
            history.append({"role": "user", "text": user_text})
        if assistant_text is not None:
            history.append({"role": "assistant", "text": assistant_text})
        entry["history"] = trim_history(history, _cfg("MAX_HISTORY_TURNS", 6))

    _prune_peers(acc)
    save_soon()


def reset_peer_history(session_id: str, peer_id: str) -> bool:
    """Wipe one conversation.  Returns False when there was nothing to wipe."""
    data = load()
    acc = data["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return False
    peers = acc.get("peers")
    if not isinstance(peers, dict) or str(peer_id) not in peers:
        return False
    peers.pop(str(peer_id), None)
    save_soon()
    return True


def peer_count(session_id: str) -> int:
    acc = load()["accounts"].get(session_id)
    if not isinstance(acc, dict):
        return 0
    peers = acc.get("peers")
    return len(peers) if isinstance(peers, dict) else 0


# ── counters / stats ─────────────────────────────────────────────────────────

def bump_counter(name: str, by: int = 1) -> int:
    """Increment a named counter (``calls_ok``/``calls_failed``/``paid_sends``)."""
    data = load()
    counters = data.setdefault("counters", {})
    if not isinstance(counters, dict):
        counters = data["counters"] = {}
    try:
        value = int(counters.get(name, 0)) + int(by)
    except (TypeError, ValueError):
        value = int(by)
    counters[name] = value
    if name == "calls_ok":
        data["total_api_calls"] = int(data.get("total_api_calls") or 0) + int(by)
    save_soon()
    return value


def get_counters() -> dict:
    counters = load().get("counters") or {}
    return {key: int(counters.get(key, 0) or 0) for key in _COUNTER_KEYS}


def increment_api_calls() -> int:
    """Back-compat shim for the old ``total_api_calls`` counter."""
    return bump_counter("calls_ok")


def get_uptime_seconds() -> int:
    """Seconds since this process started (uptime_start is reset every boot)."""
    start = load().get("uptime_start") or int(time.time())
    return max(0, int(time.time()) - int(start))
