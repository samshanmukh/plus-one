"""Product analytics: every audit-trail event also goes to Amplitude (HTTP V2 API), if AMPLITUDE_API_KEY is set.

User ids are hashed, so phone numbers never leave this machine. Sends happen on a background thread
and failures are ignored, so analytics can never slow down or break a reply.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

import httpx

from . import config  # noqa: F401  (loads .env)

API_KEY = os.getenv("AMPLITUDE_API_KEY", "").strip()
ENDPOINT = os.getenv("AMPLITUDE_ENDPOINT", "https://api2.amplitude.com/2/httpapi")


def _user_id(chat: str) -> str:
    return "u_" + hashlib.sha256(chat.encode()).hexdigest()[:16]


def track(chat: str, event_type: str, props: dict | None = None) -> None:
    if not API_KEY:
        return
    event = {"user_id": _user_id(chat), "event_type": f"plusone_{event_type}",
             "time": int(time.time() * 1000), "event_properties": props or {},
             "platform": "iMessage", "app_version": "plus-one-hackathon"}

    def send() -> None:
        try:
            httpx.post(ENDPOINT, json={"api_key": API_KEY, "events": [event]}, timeout=10)
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()
