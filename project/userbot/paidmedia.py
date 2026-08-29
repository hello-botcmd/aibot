"""
userbot/paidmedia.py
────────────────────
Delivers the paid photo **as the userbot**, priced in Telegram Stars.

Why this module exists
──────────────────────
The old flow was broken twice over:

1. the photo's ``file_id`` was minted by the *dashboard bot* — a completely
   different Telegram identity — so the userbot could not resolve it and every
   send failed (only a log line said so);
2. ``paid_stars`` was read from the DB and then ignored, so the media went out
   for free even when the admin configured a price.

New flow
────────
* the dashboard bot **downloads the bytes** it received and writes them to
  ``data/paid/<sid>-paid.jpg`` → the file on disk is the source of truth;
* the userbot uploads those bytes to **its own** account once and caches the
  resulting ``InputPhoto`` handle in the store (no re-upload per message);
* each send wraps that handle in ``InputMediaPaidMedia(stars_amount=N, …)`` so
  the recipient really has to pay N Stars to unlock the media.  An optional
  free "teaser" photo is placed in front of it (Telegram treats the **last**
  item as the locked one).

Requires Telethon ≥ 1.41 for ``InputMediaPaidMedia`` (pinned in requirements).
"""

from __future__ import annotations

import base64
import io
import logging
import time

from telethon import errors, utils
from telethon.tl import functions, types

import config

logger = logging.getLogger(__name__)

_REF_KEYS = ("id", "hash", "ref")
_EXPIRY_MARKERS = ("FILE_REFERENCE", "ACCESS_HASH_INVALID", "STORAGE_FILE_INVALID",
                   "IMAGE_FILE_INVALID", "WEBPAGE_CURL_FAILED")


class MediaUnavailable(Exception):
    """Stored file or cached reference is gone and could not be rebuilt."""


class NeedReupload(Exception):
    """Telegram expired our cached handle → persist() again, then retry once."""


class PaidUnsupported(Exception):
    """This Telethon/Telegram build cannot request Stars."""


def supports_stars() -> bool:
    return hasattr(types, "InputMediaPaidMedia")


def file_for(rel_path: str | None):
    """Absolute path for a stored media file, or None if it is missing."""
    if not rel_path:
        return None
    path = config.DATA_DIR / rel_path
    return path if path.exists() else None


def ref_ok(ref) -> bool:
    return isinstance(ref, dict) and all(ref.get(k) for k in _REF_KEYS)


def _to_input_photo(ref: dict) -> types.InputPhoto:
    try:
        return types.InputPhoto(
            id=int(ref["id"]),
            access_hash=int(ref["hash"]),
            file_reference=base64.b64decode(ref["ref"]),
        )
    except (TypeError, ValueError) as exc:            # corrupt cache entry
        raise NeedReupload(f"unusable photo_ref: {exc}") from exc


async def persist(client, rel_path: str) -> dict:
    """
    Upload the on-disk image to *this* account and return a storable handle.

    The self-chat message that carries the upload is deliberately **kept**: it
    is what keeps the photo's access_hash alive.  (If you prefer a clean Saved
    Messages folder, delete it — ``NeedReupload`` will just re-import it from
    disk on the next send.)
    """
    path = file_for(rel_path)
    if path is None:
        raise MediaUnavailable(f"missing file: {rel_path}")
    data = path.read_bytes()
    if not data:
        raise MediaUnavailable(f"empty file: {rel_path}")

    msg = await client.send_file(
        "me",
        io.BytesIO(data),
        file_size=len(data),
        caption=f"aibot paid-media import · {rel_path} · {int(time.time())}",
        force_document=False,
        silent=True,
    )
    photo = getattr(msg, "photo", None)
    if photo is None:
        raise MediaUnavailable("upload did not produce a photo")

    handle = utils.get_input_photo(photo)
    logger.info("Persisted paid media %s (photo id=%s, %d bytes)",
                rel_path, handle.id, len(data))
    return {
        "id": str(handle.id),
        "hash": str(handle.access_hash),
        "ref": base64.b64encode(bytes(handle.file_reference or b"")).decode(),
        "kind": "photo",
        "bytes": len(data),
        "path": rel_path,
        "saved_at": int(time.time()),
    }


def _message_id(update) -> int | None:
    mid = getattr(update, "id", None)
    if mid:
        return int(mid)
    for holder in ("messages", "updates"):
        for item in reversed(getattr(update, holder, None) or []):
            candidate = getattr(item, "id", None) or getattr(
                getattr(item, "message", None), "id", None)
            if candidate:
                return int(candidate)
    return None


async def send(client, peer, *, photo_ref: dict, stars: int = 0, caption: str = "",
               teaser_ref: dict | None = None, noforwards: bool | None = None,
               payload: str = "") -> int | None:
    """
    Send the cached photo to ``peer``, optionally locked behind ``stars`` Stars.

    Returns the sent message id when Telegram tells us one.
    """
    stars = int(stars or 0)
    media = types.InputMediaPhoto(id=_to_input_photo(photo_ref))
    nofwd = config.PAID_NO_FORWARDS if noforwards is None else bool(noforwards)

    if stars > 0:
        if not supports_stars():
            raise PaidUnsupported(
                "telethon has no InputMediaPaidMedia — `pip install -U 'telethon>=1.41'`")
        extended = ([types.InputMediaPhoto(id=_to_input_photo(teaser_ref))]
                    if ref_ok(teaser_ref or {}) else []) + [media]
        final_media = types.InputMediaPaidMedia(
            stars_amount=stars,
            extended_media=extended,
            payload=(payload or "aibot")[:128] or None,
        )
    else:
        final_media = media

    input_peer = await client.get_input_entity(peer)
    req = functions.messages.SendMediaRequest(
        peer=input_peer,
        media=final_media,
        message=caption or "",
        noforwards=nofwd,
        clear_draft=True,
    )

    try:
        update = await client(req)
    except errors.FileReferenceExpiredError as exc:
        raise NeedReupload(str(exc)) from exc
    except errors.BadRequestError as exc:
        blob = str(exc).upper()
        if any(marker in blob for marker in _EXPIRY_MARKERS):
            raise NeedReupload(blob) from exc
        if stars > 0 and "PAID" in blob:
            raise PaidUnsupported(blob) from exc
        raise
    return _message_id(update)


async def send_from_disk(client, peer, rel_path: str, *, caption: str = "",
                         noforwards: bool = False) -> int | None:
    """Last-resort path: upload+send in one go, free of charge."""
    path = file_for(rel_path)
    if path is None:
        raise MediaUnavailable(f"missing file: {rel_path}")
    msg = await client.send_file(peer, str(path), caption=caption or "",
                                 force_document=False, silent=False)
    return _message_id(msg) if msg is not None else None


def describe(ref: dict | None) -> str:
    if not ref_ok(ref or {}):
        return "not cached yet (will import on first send)"
    return (f"photo id `{ref['id']}` · {ref.get('bytes', '?')} bytes · "
            f"imported {time.strftime('%Y-%m-%d %H:%M', time.localtime(ref.get('saved_at', 0)))}")
