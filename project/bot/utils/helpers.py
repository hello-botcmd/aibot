"""
bot/utils/helpers.py
────────────────────
Shared formatting / matching utilities.

Two things live here because both the dashboard bot and the userbots
need them and they must agree exactly:

  * the paid-photo **trigger matcher** (whole-word, sentence-aware)
  * HTML escaping for every Telegram caption we build
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from html import escape as _html_escape

# ── 💎 trigger matching ───────────────────────────────────────────────────────
#
# Why not `word in text`:
#   >>> "send" in "sender name?"  →  True      # false positive, and it also
#                                             # swallows the AI reply
# We therefore compile ONE alternation of escaped words with non-word
# look-arounds.  That matches the word on its own, inside a sentence, next
# to punctuation or emoji — but never as a fragment of a longer word.

_TRIGGER_SEP = r"(?<!\w)" , r"(?!\w)"


@lru_cache(maxsize=32)
def _compile(words: tuple[str, ...], extra_regex: str) -> re.Pattern | None:
    parts: list[str] = []
    # longest first, so "send photo" wins over "send" inside the alternation
    for w in sorted({w.strip().lower() for w in words if w and w.strip()},
                    key=len, reverse=True):
        parts.append(re.escape(w))
    if extra_regex:
        parts.append(f"(?:{extra_regex})")
    if not parts:
        return None
    left, right = _TRIGGER_SEP
    return re.compile(
        rf"{left}(?:{'|'.join(parts)}){right}",
        re.IGNORECASE | re.UNICODE,
    )


def trigger_pattern(words, extra_regex: str = "") -> re.Pattern | None:
    """Compiled matcher for the configured trigger words (None if nothing set)."""
    return _compile(tuple(str(w).strip() for w in words if str(w).strip()), extra_regex or "")


def match_trigger(text: str, words, extra_regex: str = "") -> str | None:
    """
    Return the trigger word/phrase that matched, or None.

    >>> match_trigger("send", ["send"])
    'send'
    >>> match_trigger("hey can you send it?", ["send"])
    'send'
    >>> match_trigger("who is the sender?", ["send"]) is None
    True
    """
    if not text:
        return None
    rx = trigger_pattern(words, extra_regex)
    if rx is None:
        return None
    m = rx.search(text)
    return m.group(0).strip() if m else None


def describe_triggers(words, extra_regex: str = "") -> str:
    """Human-readable one-liner for the dashboard."""
    items = [w for w in words if w]
    if extra_regex:
        items.append(f"/{extra_regex}/")
    return ", ".join(f"`{i}`" for i in items) if items else "— (disabled)"


# ── misc formatting ───────────────────────────────────────────────────────────

def fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def trim_history(history: list, max_turns: int) -> list:
    """
    Keep the last `max_turns` complete exchanges (user+assistant = 2 items).

    Two guarantees the old version lacked:
      • it never returns a list that starts with an assistant turn
      • it never returns [] merely because the head was an assistant turn
        (previously 2 assistant rows in → 0 rows out, losing all context)
    """
    cap = max(1, int(max_turns)) * 2
    history = list(history[-cap:]) if len(history) > cap else list(history)
    if not history:
        return history
    if history[0].get("role") != "user":
        # Prefer "user … assistant" alignment over deleting everything.
        history = history[1:] if len(history) > 1 else history
    return history


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return user_id in (admin_ids or [])


def credits_text(cred: dict) -> str:
    """
    Render OpenRouter key info.

    Uses `is not None` everywhere: 0.0 is a *real* value (credits exhausted),
    not "unlimited" — the old truthiness test printed 'Left: ∞' at exactly the
    moment the admin needed to see they were broke.
    """
    if not cred or cred.get("used") is None:
        return "Credit info unavailable"
    used = f"${cred['used']:.4f}"
    limit = f"${cred['limit']:.4f}" if cred.get("limit") is not None else "Unlimited"
    rem = cred.get("remaining")
    if rem is None:
        left = "Unlimited"
    elif rem <= 0:
        left = "💳 $0.0000 — EXHAUSTED"
    else:
        left = f"${rem:.4f}"
    return f"Used: {used}  |  Limit: {limit}  |  Left: {left}"


def esc(value, *, code: bool = False) -> str:
    """
    Make arbitrary text safe for `parse_mode=HTML`.

    `code=True` also escapes the backtick so the value can be wrapped in
    `<code>…</code>` without the user breaking the entity.
    """
    out = _html_escape(str(value if value is not None else ""), quote=False)
    if code:
        out = out.replace("`", "&#96;")
    return out


def clip(text: str, n: int = 60) -> str:
    """Safe display truncation that never splits an HTML entity we just built."""
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def clip_bytes(text, limit: int = 64) -> str:
    """
    Truncate to at most ``limit`` UTF-8 *bytes*, never splitting a codepoint.

    Telegram measures inline-keyboard button text in bytes (limit 64), and an emoji
    is 4 of them — so a 40-character name built from emoji still blows up the whole
    keyboard with BUTTON_TEXT_INVALID. A plain :func:`clip` on characters is not
    enough for anything that ends up inside a button.
    """
    raw = str(text or "")
    if len(raw.encode("utf-8")) <= limit:             # lossless whenever it already fits
        return raw
    out, used, budget = "", 0, max(1, limit - 3)     # 3 bytes reserved for "…"
    for ch in raw:
        size = len(ch.encode("utf-8"))
        if used + size > budget:
            return (out.rstrip() + "\u2026").encode("utf-8")[:limit].decode("utf-8", "ignore")
        out += ch
        used += size
    return out


def fill(template, **values) -> str:
    """
    Brace-safe replacement for ``str.format`` on admin-editable text.

    Every template we render (the paid caption, /help) lives in ``config.py`` and is
    meant to be edited.  ``.format()`` raises ``KeyError`` on the first brace it does
    not know — so a caption like ``"unlock for {stars} ⭐ (reply {later})"`` used to
    throw *inside the paid send path*, and the peer simply never got the photo.
    We substitute only the keys we pass and leave every other brace pair literal,
    which is what the admin meant.
    """
    out = str(template or "")
    if not values:
        return out
    # ONE pass, so a brace pair carried by a *value* (e.g. a trigger word named
    # "{accounts}") is inserted verbatim instead of being substituted again.
    keys = "|".join(re.escape(k) for k in values)
    return re.sub(r"\{(" + keys + r")\}", lambda m: str(values[m.group(1)]), out)


def ago(ts: float | int | None) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def redact(session_string: str | None, keep: int = 4) -> str:
    """Logs must never contain a session string."""
    if not session_string:
        return "<empty>"
    s = str(session_string)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}…{len(s)}ch…{s[-keep:]}"
