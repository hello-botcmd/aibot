"""
bot/utils/db.py
───────────────
Single source of truth for all persistent data.
Everything is stored in  data/store.json  as JSON.

Schema
------
{
  "accounts": {
    "<session_id>": {
      "session_string": "...",
      "phone":         "...",      # filled after connecting
      "name":          "...",      # Telegram display name
      "ai_enabled":    false,
      "persona":       "...",
      "paid_photo_id": null,       # Telegram file_id  (bytes-like str)
      "paid_stars":    0,
      "history":       {},         # { "peer_user_id": [ {role,text}, ... ] }
      "last_msg_time": {},         # { "peer_user_id": timestamp }
      "rate_limited_until": 0
    }
  },
  "global_ai_on":  false,          # master toggle — mirrors per-account ai_enabled
  "uptime_start":  0,              # unix timestamp set on first launch
  "total_api_calls": 0
}
"""

import copy
import json
import os
import time
from pathlib import Path

_DATA_DIR  = Path(__file__).resolve().parents[2] / "data"
_DATA_FILE = _DATA_DIR / "store.json"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULTS: dict = {
    "accounts":        {},
    "global_ai_on":    False,
    "uptime_start":    0,
    "total_api_calls": 0,
}

# ── in-memory cache ──────────────────────────────────────────────────────────
_cache: dict | None = None


def _init_cache() -> dict:
    global _cache
    if not _DATA_FILE.exists():
        _cache = copy.deepcopy(_DEFAULTS)
        _cache["uptime_start"] = int(time.time())
        _flush()
        return _cache
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _DEFAULTS.items():
            data.setdefault(k, copy.deepcopy(v))
        if data["uptime_start"] == 0:
            data["uptime_start"] = int(time.time())
        _cache = data
    except (json.JSONDecodeError, OSError):
        _cache = copy.deepcopy(_DEFAULTS)
        _cache["uptime_start"] = int(time.time())
    return _cache


def _flush() -> None:
    """Write in-memory cache to disk."""
    with open(_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)


def load() -> dict:
    global _cache
    if _cache is None:
        _init_cache()
    return _cache


def save() -> None:
    _flush()


# ── account helpers ──────────────────────────────────────────────────────────

_ACCOUNT_DEFAULTS = {
    "session_string":    "",
    "phone":             "",
    "name":              "",
    "ai_enabled":        False,
    "persona":           "",
    "paid_photo_id":     None,
    "paid_stars":        0,
    "history":           {},
    "last_msg_time":     {},
    "rate_limited_until": 0,
}


def _normalise_account(acc: dict, default_persona: str) -> dict:
    for k, v in _ACCOUNT_DEFAULTS.items():
        acc.setdefault(k, copy.deepcopy(v))
    if not acc["persona"]:
        acc["persona"] = default_persona
    return acc


def get_accounts(default_persona: str = "") -> dict:
    data = load()
    for acc in data["accounts"].values():
        _normalise_account(acc, default_persona)
    return data["accounts"]


def add_account(session_id: str, session_string: str, default_persona: str) -> dict:
    data = load()
    acc = copy.deepcopy(_ACCOUNT_DEFAULTS)
    acc["session_string"] = session_string
    acc["persona"]        = default_persona
    data["accounts"][session_id] = acc
    save()
    return acc


def remove_account(session_id: str) -> None:
    data = load()
    data["accounts"].pop(session_id, None)
    save()


def get_account(session_id: str, default_persona: str = "") -> dict | None:
    data = load()
    acc  = data["accounts"].get(session_id)
    if acc is None:
        return None
    return _normalise_account(acc, default_persona)


def update_account(session_id: str, **kwargs) -> None:
    data  = load()
    acc   = data["accounts"].get(session_id, {})
    acc.update(kwargs)
    data["accounts"][session_id] = acc
    save()


# ── global helpers ───────────────────────────────────────────────────────────

def set_global_ai(state: bool) -> None:
    data = load()
    data["global_ai_on"] = state
    # mirror to every account
    for acc in data["accounts"].values():
        acc["ai_enabled"] = state
    save()


def increment_api_calls() -> int:
    data = load()
    data["total_api_calls"] += 1
    save()
    return data["total_api_calls"]


def get_uptime_seconds() -> int:
    data = load()
    return int(time.time()) - data.get("uptime_start", int(time.time()))
