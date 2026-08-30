"""
bot/utils/helpers.py
────────────────────
Shared formatting / utility functions.

``match_trigger`` lives here because both the userbot (firing the paid
photo) and the dashboard (explaining it) need the exact same notion of
"this message triggered the paywall".
"""

from __future__ import annotations

import re
import time

# Compiled trigger patterns are cached — the dashboard calls this for every
# incoming DM, and re-compiling a dozen patterns per message is pure waste.
_WORD_CACHE: dict[str, re.Pattern[str]] = {}
_PHRASE_CACHE: dict[tuple[str, str], re.Pattern[str] | None] = {}


def fmt_uptime(seconds: int) -> str:
    days,    rem  = divmod(max(int(seconds), 0), 86400)
    hours,   rem  = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def trim_history(history: list, max_turns: int) -> list:
    """Keep last max_turns complete exchanges (each = 2 items)."""
    cap = max(0, int(max_turns)) * 2
    if cap and len(history) > cap:
        history = history[-cap:]
    # ensure history starts with a user turn
    while history and history[0].get("role") != "user":
        history = history[1:]
    return history


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return user_id in admin_ids


def credits_text(cred: dict) -> str:
    """Render an OpenRouter credit summary.  Never raises on missing keys."""
    if not cred or cred.get("used") is None:
        return "Credit info unavailable"
    used  = f"${cred['used']:.4f}"
    limit = f"${cred['limit']:.4f}" if cred.get("limit") else "Unlimited"
    left  = f"${cred['remaining']:.4f}" if cred.get("remaining") else "∞"
    return f"Used: {used}  |  Limit: {limit}  |  Left: {left}"


# ── paid-photo trigger matching ───────────────────────────────────────────────

def _word_pattern(word: str) -> re.Pattern[str] | None:
    """
    Whole-word, case-insensitive pattern for ``word``.

    ``\\b`` anchors keep "sender"/"sending"/"recommend" from matching the
    trigger "send", while still matching "send" buried inside a sentence.
    Multi-word phrases ("send pic") are anchored around the whole phrase.
    """
    word = (word or "").strip()
    if not word:
        return None
    pat = _WORD_CACHE.get(word)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        _WORD_CACHE[word] = pat
    return pat


def match_trigger(text: str, words=(), extra_regex: str = "") -> str | None:
    """
    Return the *configured* trigger that ``text`` matches, or ``None``.

    The configured trigger is returned (not the raw substring the user typed)
    so the value is stable — it ends up in the Stars payment payload, which
    must be reproducible for accounting.

    Matching is case-insensitive and whole-word:
        "send"                    ✅  → "send"
        "hey can you send it? 🙏" ✅  → "send"
        "sender", "recommend"     ❌
    """
    if not text:
        return None

    for word in words or ():
        pat = _word_pattern(word)
        if pat is not None and pat.search(text):
            return (word or "").strip()

    extra = (extra_regex or "").strip()
    if extra:
        key = ("extra", extra)
        if key not in _PHRASE_CACHE:
            try:
                _PHRASE_CACHE[key] = re.compile(extra, re.IGNORECASE)
            except re.error:
                # A bad regex must never take the reply path down with it.
                _PHRASE_CACHE[key] = None
        pat = _PHRASE_CACHE[key]
        if pat is not None:
            m = pat.search(text)
            if m:
                return m.group(0)
    return None


def ago(ts: float) -> str:
    """Human '5m ago' style helper used by the dashboard."""
    delta = max(0, int(time.time() - float(ts or 0)))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
