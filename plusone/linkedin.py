"""Match people at an event against the user's LinkedIn connections (from LinkedIn's own data export).

LinkedIn -> Settings -> Data privacy -> Get a copy of your data -> Connections.
Put Connections.csv in ./data/ (git-ignored) or set LINKEDIN_CSV.
"""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

CSV_PATH = Path(os.getenv("LINKEDIN_CSV") or Path(__file__).resolve().parent.parent / "data" / "Connections.csv")


def _norm(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name or "")
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _handle(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


def parse_export(data: bytes, filename: str = "") -> list[dict]:
    """People from LinkedIn's Connections.csv, or from the whole export ZIP."""
    import zipfile
    if data[:2] == b"PK" or filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("connections.csv")), None)
            if not name:
                raise ValueError("That ZIP has no Connections.csv in it.")
            data = z.read(name)
    text = data.decode("utf-8", errors="ignore")
    start = text.find("First Name")  # the export starts with a few lines of notes
    if start < 0:
        raise ValueError("That doesn't look like LinkedIn's Connections.csv.")
    people = []
    for r in csv.DictReader(io.StringIO(text[start:])):
        name = f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip()
        if name:
            people.append({"name": name, "company": r.get("Company", ""), "position": r.get("Position", ""),
                           "url": r.get("URL", ""), "connected_on": r.get("Connected On", "")})
    return people


def save_connections(chat: str, people: list[dict]) -> int:
    from . import store
    store.set_state(f"linkedin:{chat}", people)
    store.set_state(f"linkedin_meta:{chat}", {"count": len(people), "loaded": store.now_iso()})
    _cache.pop(chat, None)
    return len(people)


_cache: dict[str, tuple[dict, dict]] = {}


def connections(chat: str | None = None) -> tuple[dict, dict]:
    """(by_handle, by_name) lookups for this user: from their uploaded export, else data/Connections.csv."""
    key = chat or "_file"
    if key in _cache:
        return _cache[key]
    people: list[dict] = []
    if chat:
        from . import store
        people = store.get_state(f"linkedin:{chat}") or []
    if not people and CSV_PATH.exists():
        people = parse_export(CSV_PATH.read_bytes())
    by_handle, by_name = {}, {}
    for person in people:
        if _handle(person.get("url", "")):
            by_handle[_handle(person["url"])] = person
        by_name[_norm(person["name"])] = person
    _cache[key] = (by_handle, by_name)
    return by_handle, by_name


def match(people: list[dict], chat: str | None = None) -> list[dict]:
    """people: [{name, linkedin_handle, role}] -> those who are the user's connections."""
    by_handle, by_name = connections(chat)
    found = []
    for p in people:
        h = (p.get("linkedin_handle") or "").strip("/").split("/")[-1].lower()
        hit = by_handle.get(h) if h else None
        how = "linkedin profile"
        if not hit:
            hit, how = by_name.get(_norm(p.get("name", ""))), "name"
        if hit:
            found.append({"name": hit["name"], "role_at_event": p.get("role"), "company": hit["company"],
                          "position": hit["position"], "matched_by": how, "avatar_url": p.get("avatar_url")})
    return found
