"""Senso context layer: Plus One's trusted context (user rules, taste profile, booking receipts).

Docs: https://docs.senso.ai/docs/quickstart. No-op unless SENSO_API_KEY is set.
"""
from __future__ import annotations

import os
import threading

import httpx

from . import config  # noqa: F401  (loads .env)

API_KEY = os.getenv("SENSO_API_KEY", "").strip()
BASE = os.getenv("SENSO_BASE", "https://apiv2.senso.ai/api/v1")
_http = httpx.Client(base_url=BASE, timeout=20, headers={"X-API-Key": API_KEY, "Content-Type": "application/json"})


def enabled() -> bool:
    return bool(API_KEY)


def add_doc(title: str, text: str, background: bool = True) -> dict | None:
    """Add a document to the knowledge base (409 = identical text already stored, which is fine)."""
    if not API_KEY:
        return None

    def go() -> dict | None:
        try:
            r = _http.post("/org/kb/raw", json={"title": title[:200], "text": text})
            return r.json() if r.status_code in (200, 201, 202) else {"status": r.status_code}
        except Exception:
            return None

    if background:
        threading.Thread(target=go, daemon=True).start()
        return {"queued": True}
    return go()


def context(query: str, max_results: int = 4) -> list[dict]:
    """Passages relevant to a decision, each with the Senso content_id it came from."""
    if not API_KEY:
        return []
    try:
        r = _http.post("/org/search/context", json={"query": query, "max_results": max_results})
        if r.status_code != 200:
            return []
        return [{"text": x.get("chunk_text", "")[:300], "title": x.get("title"), "content_id": x.get("content_id"),
                 "score": round(x.get("score") or 0, 3)} for x in r.json().get("results", [])]
    except Exception:
        return []


def whoami() -> dict | None:
    if not API_KEY:
        return None
    r = _http.get("/org/me")
    return r.json() if r.status_code == 200 else {"status": r.status_code, "body": r.text[:200]}
