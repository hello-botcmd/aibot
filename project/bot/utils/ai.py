"""
bot/utils/ai.py
───────────────
OpenRouter API wrapper.  Blocking ``requests`` calls never touch the event
loop: they run on a dedicated ``ThreadPoolExecutor`` (not the shared default
one) over a **pooled keep-alive ``Session``**, so a reply does not pay for a
fresh DNS lookup + TCP + TLS handshake every time — the reason the first
replies after a restart used to be slow.

Contract
--------
``generate_reply(persona, history, user_message)`` → ``AIReply``

* ``ok``          True only when the text is a real model answer.
* ``retry_after`` seconds the caller should back off for (0/None when not a
                  rate-limit or billing problem).
* ``text``        the reply when ``ok``, otherwise filler the caller may show.
* ``reason``      machine-readable cause (``"ok"``, ``"timeout"``,
                  ``"server_error"``, ``"billing"``, ``"no_api_key"``, …) so the
                  caller can alert an admin with something actionable instead
                  of "it failed".

Error filler is *never* silently treated as a real answer — the caller uses
``ok`` to keep "I'm busy" strings out of the conversation history, which
otherwise poisons every later reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import requests
from requests.adapters import HTTPAdapter

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
_CONNECT_TIMEOUT = 5      # TCP+TLS must not eat into the read budget

# ── Connection reuse ────────────────────────────────────────────────────────
# ``requests.post(...)`` opens a brand new TCP connection and TLS handshake on
# every call.  With no keep-alive every reply pays that cost, and it is at its
# worst right after a restart when the DNS cache is cold — which is exactly the
# "first replies after a restart are slow" symptom.  One pooled Session fixes it.
_SESSION = requests.Session()
_POOL = max(4, int(getattr(config, "OPENROUTER_POOL", 32) or 32))
_ADAPTER = HTTPAdapter(pool_connections=max(2, _POOL // 4),
                       pool_maxsize=_POOL, max_retries=0)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)

# Dedicated pool for AI calls.  ``asyncio.to_thread`` borrows the loop's default
# executor, which is also used by PTB and file IO — a burst of slow model calls
# would otherwise stall unrelated work and add latency to every reply.
_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        workers = int(getattr(config, "AI_MAX_WORKERS", 8) or 8)
        _executor = ThreadPoolExecutor(max_workers=max(2, workers),
                                       thread_name_prefix="aibot-ai")
    return _executor


class AIReply(NamedTuple):
    """Result of one OpenRouter call.  ``reason`` is always set, even on success."""
    text: str
    retry_after: int | None
    ok: bool
    reason: str = "ok"


#: human wording for the machine-readable ``reason`` codes
REASON_TEXT = {
    "ok":              "success",
    "no_api_key":      "OPENROUTER_API_KEY is not set",
    "rate_limited":    "OpenRouter returned 429 (rate limited)",
    "billing":         "OpenRouter rejected the key/credits (401/402/403)",
    "server_error":    "OpenRouter 5xx after retrying",
    "timeout":         "request timed out",
    "http_error":      "OpenRouter HTTP error",
    "network_error":   "network/DNS failure",
    "parse_error":     "unexpected response body",
    "empty_response":  "model returned no content",
}


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
    resp = _SESSION.get(config.OPENROUTER_CREDITS_URL,
                        headers=_auth_headers(),
                        timeout=(_CONNECT_TIMEOUT, _CREDITS_TIMEOUT))
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

def _timeout() -> tuple[float, float]:
    """(connect, read) — a hung connect must not consume the read budget."""
    read = float(getattr(config, "OPENROUTER_TIMEOUT", 20) or 20)
    connect = float(getattr(config, "OPENROUTER_CONNECT_TIMEOUT", _CONNECT_TIMEOUT)
                    or _CONNECT_TIMEOUT)
    return (connect, read)


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


def _call_openrouter_sync(persona: str, history: list, user_message: str) -> AIReply:
    """Synchronous OpenRouter call — run on the dedicated AI executor."""
    if not getattr(config, "OPENROUTER_API_KEY", ""):
        logger.error("OPENROUTER_API_KEY is not set — AI replies are disabled")
        return AIReply(_NOT_CONFIGURED, None, False, "no_api_key")

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
            resp = _SESSION.post(
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
                return AIReply(_BUSY, backoff, False, "billing")

            # ── rate limit: retry later, but not forever ──────────────────
            if status == 429:
                retry_after = _retry_after_seconds(resp)
                logger.warning("OpenRouter 429 — retry after %ds", retry_after)
                return AIReply(_RATE_LIMITED, retry_after, False, "rate_limited")

            # ── server-side: worth one more attempt ───────────────────────
            if status >= 500:
                last_error = f"HTTP {status}"
                logger.warning("OpenRouter %s (attempt %d/%d), retrying…",
                               status, attempt, max_attempts)
                if attempt < max_attempts:
                    continue
                return AIReply(_BUSY, None, False, "server_error")

            resp.raise_for_status()
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                logger.error("OpenRouter returned no content: %s", str(data)[:300])
                return AIReply(_PARSE_ERROR, None, False, "empty_response")
            return AIReply(content.strip(), None, True, "ok")

        except requests.exceptions.Timeout as exc:
            last_error = exc
            logger.warning("OpenRouter timeout (attempt %d/%d): %s",
                           attempt, max_attempts, exc)
            if attempt < max_attempts:
                continue
            return AIReply(_SLOW, None, False, "timeout")

        except requests.exceptions.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = (exc.response.text or "")[:300]
            logger.error("OpenRouter HTTP error: %s | body: %s", exc, body)
            return AIReply(_BUSY, None, False, "http_error")

        except requests.exceptions.RequestException as exc:
            logger.error("OpenRouter request error: %s", exc)
            return AIReply(_BUSY, None, False, "network_error")

        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("OpenRouter parse error: %s", exc)
            return AIReply(_PARSE_ERROR, None, False, "parse_error")

    logger.error("OpenRouter failed after %d attempt(s): %s", max_attempts, last_error)
    return AIReply(_SLOW, None, False, "server_error")


async def generate_reply(persona: str, history: list, user_message: str) -> AIReply:
    """Async wrapper — safe to call from Telethon / PTB event handlers."""
    loop = asyncio.get_running_loop()
    call = functools.partial(_call_openrouter_sync, persona, history, user_message)
    try:
        return await loop.run_in_executor(_pool(), call)
    except Exception as exc:                         # noqa: BLE001
        # Should not happen, but a failure here must still surface a reason
        # rather than taking the whole DM handler down.
        logger.exception("OpenRouter call crashed: %s", exc)
        return AIReply(_BUSY, None, False, "network_error")


async def warmup() -> None:
    """
    Prime the HTTPS connection (DNS + TCP + TLS) and validate the key.

    Call once at startup.  Without it the first real reply after a restart
    absorbs the full cold-start cost, which is the "late response after a
    restart" symptom.
    """
    if not getattr(config, "OPENROUTER_WARMUP", True):
        return
    if not getattr(config, "OPENROUTER_API_KEY", ""):
        return

    def _prime() -> None:
        try:
            resp = _SESSION.get(config.OPENROUTER_CREDITS_URL,
                                headers=_auth_headers(),
                                timeout=(_CONNECT_TIMEOUT, _CREDITS_TIMEOUT))
            logger.info("OpenRouter connection warmed up (HTTP %s)", resp.status_code)
        except Exception as exc:                     # noqa: BLE001
            logger.debug("OpenRouter warm-up failed (will retry on first use): %s", exc)

    await asyncio.to_thread(_prime)
