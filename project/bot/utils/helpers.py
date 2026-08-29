"""
bot/utils/helpers.py
────────────────────
Shared formatting / utility functions.
"""

import time


def fmt_uptime(seconds: int) -> str:
    days,    rem  = divmod(seconds, 86400)
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
    cap = max_turns * 2
    if len(history) > cap:
        history = history[-cap:]
    # ensure history starts with a user turn
    while history and history[0].get("role") != "user":
        history = history[1:]
    return history


def is_admin(user_id: int, admin_ids: list[int]) -> bool:
    return user_id in admin_ids


def credits_text(cred: dict) -> str:
    if cred.get("used") is None:
        return "Credit info unavailable"
    used  = f"${cred['used']:.4f}"
    limit = f"${cred['limit']:.4f}"  if cred["limit"]     else "Unlimited"
    left  = f"${cred['remaining']:.4f}" if cred["remaining"] else "∞"
    return f"Used: {used}  |  Limit: {limit}  |  Left: {left}"
