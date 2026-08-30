"""
bot/utils/ai.py
───────────────
OpenRouter API wrapper (blocking ``requests`` calls run off-loop via
``asyncio.to_thread`` so they never stall the event loop).

Contract
--------
``generate_reply(persona, history, user_message)`` → ``(text, retry_after, ok)``

* ``ok``          True only when the text is a real model answer.
* ``retry_after`` seconds the caller should back off for (0/None when not a
                  rate-limit or billing problem).
* ``text``        the reply when ``ok``, otherwise filler the caller may show.

Error filler is *never* silently treated as a real answer — the caller uses
``ok`` to keep "I'm busy" strings out of the conversation history, which
otherwise poisons every later reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import requests

import config

logger = logging.getLogger(__name__)

# Sentinel reply text — the caller only shows it when ok is True, so the exact
# wording here is mostly for logs.
_NOT_CONFIGURED = "AI is not configured yet. 🙏"
_BUSY           = "A little busy right now, try again in a moment! 🙏"
_RATE_LIMITED   = "I'm a bit busy right now, let's talk in a little while! 🙏"
_PARSE_ERROR    = "Couldn't understand the response, please try again. 🙏"
_SLOW           = "Running a bit slow right now, please send again. 🙏"

_CREDITS_TIMEOUT = 15


# ── credit fetching ──────────────────────────────────────────────────────────

def _auth_headers(json_body: bool = True) -> dict:
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    # Attribution is optional — a fake/placeholder URL is worse than nothing.
    if getattr(config, "OPENROUTER_SITE_URL", ""):
        headers["HTTP-Referer"] = config.OPENROUTER_SITE_URL
    if getattr(config, "OPENROUTER_APP_TITLE", ""):
        headers["X-Title"] = config.OPENROUTER_APP_TITLE
    return headers


def _fetch_credits_sync() -> dict:
    """Returns {"used": float, "limit": float|None, "remaining": float|None}."""
    resp = requests.get(config.OPENROUTER_CREDITS_URL,
                        headers=_auth_headers(), timeout=_CREDITS_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data", {}) or {}
    limit_dollars = data.get("limit")                # None = unlimited
    usage_dollars = data.get("usage") or 0.0
    try:
        usage_dollars = float(usage_dollars)
    except (TypeError, ValueError):
        usage_dollars = 0.0
    return {
        "used":      round(usage_dollars, 4),
        "limit":     round(float(limit_dollars), 4) if limit_dollars else None,
        "remaining": round(float(limit_dollars) - usage_dollars, 4) if limit_dollars else None,
    }


async def fetch_credits() -> dict:
    try:
        return await asyncio.to_thread(_fetch_credits_sync)
    except Exception as exc:                         # noqa: BLE001
        logger.error("Credit fetch failed: %s", exc)
        return {"used": None, "limit": None, "remaining": None}


# ── chat completion ──────────────────────────────────────────────────────────

def _timeout() -> int:
    return int(getattr(config, "OPENROUTER_TIMEOUT", 45) or 45)


def _retry_after_seconds(resp) -> int:
    """Honour Retry-After when the server sends one, else a sane default."""
    fallback = 60
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return fallback
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return fallback


def _build_messages(persona: str, history: list, user_message: str) -> list:
    messages = [{"role": "system", "content": persona}]
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        text = turn.get("text")
        if not text:
            continue
        role = "assistant" if turn.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_message})
    return messages


def _call_openrouter_sync(persona: str, history: list,
                          user_message: str) -> tuple[str, int | None, bool]:
    """
    Synchronous OpenRouter call (run via ``asyncio.to_thread``).
    Returns ``(reply_text, retry_after_seconds_or_None, ok)``.
    """
    if not getattr(config, "OPENROUTER_API_KEY", ""):
        logger.error("OPENROUTER_API_KEY is not set — AI replies are disabled")
        return _NOT_CONFIGURED, None, False

    payload = {
        "model":       config.OPENROUTER_MODEL,
        "messages":    _build_messages(persona, history, user_message),
        "max_tokens":  int(getattr(config, "OPENROUTER_MAX_TOKENS", 200) or 200),
        "temperature": float(getattr(config, "OPENROUTER_TEMPERATURE", 0.9)),
    }

    max_attempts = 2
    last_error: object = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                config.OPENROUTER_URL,
                json=payload,
                headers=_auth_headers(),
                timeout=_timeout(),
            )

            status = resp.status_code

            # ── billing / auth: stop hard, tell the humans ───────────────
            if status in (401, 402, 403):
                backoff = int(getattr(config, "CRITICAL_BACKOFF_SECONDS", 3600) or 3600)
                logger.error("OpenRouter %s — backing off %ss (check key/credits). body: %s",
                             status, backoff, (resp.text or "")[:300])
                return _BUSY, backoff, False

            # ── rate limit: retry later, but not forever ──────────────────
            if status == 429:
                retry_after = _retry_after_seconds(resp)
                logger.warning("OpenRouter 429 — retry after %ds", retry_after)
                return _RATE_LIMITED, retry_after, False

            # ── server-side: worth one more attempt ───────────────────────
            if status >= 500:
                last_error = f"HTTP {status}"
                logger.warning("OpenRouter %s (attempt %d/%d), retrying…",
                               status, attempt, max_attempts)
                if attempt < max_attempts:
                    continue
                return _BUSY, None, False

            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                logger.error("OpenRouter returned no content: %s", str(data)[:300])
                return _PARSE_ERROR, None, False
            return content.strip(), None, True

        except requests.exceptions.Timeout as exc:
            last_error = exc
            logger.warning("OpenRouter timeout (attempt %d/%d): %s",
                           attempt, max_attempts, exc)
            if attempt < max_attempts:
                continue
            return _SLOW, None, False

        except requests.exceptions.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = (exc.response.text or "")[:300]
            logger.error("OpenRouter HTTP error: %s | body: %s", exc, body)
            return _BUSY, None, False

        except requests.exceptions.RequestException as exc:
            logger.error("OpenRouter request error: %s", exc)
            return _BUSY, None, False

        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("OpenRouter parse error: %s", exc)
            return _PARSE_ERROR, None, False

    logger.error("OpenRouter failed after %d attempt(s): %s", max_attempts, last_error)
    return _SLOW, None, False


async def generate_reply(persona: str, history: list,
                         user_message: str) -> tuple[str, int | None, bool]:
    """Async wrapper — safe to call from Telethon / PTB event handlers."""
    return await asyncio.to_thread(_call_openrouter_sync, persona, history, user_message)
