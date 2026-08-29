"""
bot/utils/ai.py
───────────────
OpenRouter API wrapper (async-safe via asyncio.to_thread).
"""

import asyncio
import logging

import requests

import config

logger = logging.getLogger(__name__)

# ── credit fetching ──────────────────────────────────────────────────────────

def _fetch_credits_sync() -> dict:
    """Returns {"used": float, "limit": float|None} or raises."""
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
    }
    resp = requests.get(config.OPENROUTER_CREDITS_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", {})
    limit_dollars = data.get("limit")           # None = unlimited
    usage_dollars = data.get("usage", 0.0)
    return {
        "used":      round(usage_dollars, 4),
        "limit":     round(limit_dollars, 4) if limit_dollars else None,
        "remaining": round(limit_dollars - usage_dollars, 4) if limit_dollars else None,
    }


async def fetch_credits() -> dict:
    try:
        return await asyncio.to_thread(_fetch_credits_sync)
    except Exception as exc:
        logger.error("Credit fetch failed: %s", exc)
        return {"used": None, "limit": None, "remaining": None}


# ── chat completion ──────────────────────────────────────────────────────────

def _call_openrouter_sync(persona: str, history: list, user_message: str) -> tuple[str, int | None]:
    """
    Synchronous OpenRouter call (meant to be run via asyncio.to_thread).
    Returns (reply_text, retry_after_seconds_or_None).
    """
    if not config.OPENROUTER_API_KEY:
        return "AI is not configured yet. Please set OPENROUTER_API_KEY in config.py. 🙏", None

    messages = [{"role": "system", "content": persona}]
    for turn in history:
        role = "assistant" if turn.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model":       config.OPENROUTER_MODEL,
        "messages":    messages,
        "max_tokens":  200,
        "temperature": 0.9,
    }
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/your-repo",
        "X-Title":       "TelegramUserbot",
    }

    last_error = None
    for attempt in range(2):
        try:
            resp = requests.post(
                config.OPENROUTER_URL,
                json=payload,
                headers=headers,
                timeout=45,
            )

            if resp.status_code == 429:
                retry_after = 60
                try:
                    retry_after = int(resp.headers.get("Retry-After", retry_after))
                except (TypeError, ValueError):
                    pass
                logger.warning("OpenRouter 429 — retry after %ds", retry_after)
                return "I'm a bit busy right now, let's talk in a little while! 🙏", retry_after

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                logger.warning("OpenRouter %s (attempt %d), retrying…", resp.status_code, attempt + 1)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip(), None

        except requests.exceptions.Timeout as exc:
            last_error = exc
            logger.warning("OpenRouter timeout (attempt %d): %s", attempt + 1, exc)
            continue
        except requests.exceptions.HTTPError as exc:
            logger.error("OpenRouter HTTP error: %s | body: %s", exc, exc.response.text)
            return "A little busy right now, try again in a moment! 🙏", None
        except requests.exceptions.RequestException as exc:
            logger.error("OpenRouter request error: %s", exc)
            return "A little busy right now, try again in a moment! 🙏", None
        except (KeyError, IndexError) as exc:
            logger.error("OpenRouter parse error: %s", exc)
            return "Couldn't understand the response, please try again. 🙏", None

    logger.error("OpenRouter failed after retries: %s", last_error)
    return "Running a bit slow right now, please send again. 🙏", None


async def generate_reply(persona: str, history: list, user_message: str) -> tuple[str, int | None]:
    """Async wrapper — safe to call from Telethon/Pyrogram event handlers."""
    return await asyncio.to_thread(_call_openrouter_sync, persona, history, user_message)
