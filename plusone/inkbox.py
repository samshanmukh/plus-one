"""Minimal Inkbox iMessage client (https://inkbox.ai/docs/api/imessage)."""
from __future__ import annotations

import httpx

from . import config

_client = httpx.Client(base_url=config.INKBOX_BASE, timeout=20,
                       headers={"X-API-Key": config.INKBOX_API_KEY, "Content-Type": "application/json"})


def unread_inbound(limit: int = 50) -> list[dict]:
    r = _client.get("/messages", params={"is_read": "false", "limit": limit})
    r.raise_for_status()
    msgs = [m for m in r.json() if m.get("direction") == "inbound" and not m.get("is_group")]
    return sorted(msgs, key=lambda m: m["created_at"])


def send(conversation_id: str, text: str) -> dict:
    r = _client.post("/messages", json={"conversation_id": conversation_id, "text": text})
    r.raise_for_status()
    return r.json()


def mark_read(conversation_id: str) -> None:
    _client.post("/mark-read", json={"conversation_id": conversation_id})


def typing(conversation_id: str) -> None:
    try:
        _client.post("/typing", json={"conversation_id": conversation_id})
    except httpx.HTTPError:
        pass


_mail = httpx.Client(base_url="https://inkbox.ai/api/v1/mail", timeout=20,
                     headers={"X-API-Key": config.INKBOX_API_KEY})


def recent_mail(limit: int = 20) -> list[dict]:
    r = _mail.get("/messages", params={"limit": limit})
    r.raise_for_status()
    return [m for m in r.json().get("items", []) if m.get("direction") == "inbound"]


def mail_body(message_id: str) -> dict:
    r = _mail.get(f"/messages/{message_id}")
    r.raise_for_status()
    return r.json()


def recent_imessages(limit: int = 50) -> list[dict]:
    r = _client.get("/messages", params={"limit": limit})
    r.raise_for_status()
    return r.json()
