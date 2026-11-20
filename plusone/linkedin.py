"""Match people at an event against the user's LinkedIn connections (from LinkedIn's own data export).

LinkedIn -> Settings -> Data privacy -> Get a copy of your data -> Connections.
Put Connections.csv in ./data/ (git-ignored) or set LINKEDIN_CSV.
"""
from __future__ import annotations

import csv
import io
import os
import re
from functools import lru_cache
from pathlib import Path

CSV_PATH = Path(os.getenv("LINKEDIN_CSV") or Path(__file__).resolve().parent.parent / "data" / "Connections.csv")


def _norm(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name or "")
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _handle(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url or "")
    return m.group(1).lower() if m else ""


@lru_cache(maxsize=1)
def connections() -> tuple[dict, dict]:
    """(by_handle, by_name) lookups, or empty if no export is present."""
    if not CSV_PATH.exists():
        return {}, {}
    text = CSV_PATH.read_text(encoding="utf-8", errors="ignore")
    start = text.find("First Name")  # the export starts with a few lines of notes
    rows = csv.DictReader(io.StringIO(text[start:] if start >= 0 else text))
    by_handle, by_name = {}, {}
    for r in rows:
        person = {"name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(),
                  "company": r.get("Company", ""), "position": r.get("Position", ""), "url": r.get("URL", "")}
        if _handle(person["url"]):
            by_handle[_handle(person["url"])] = person
        if person["name"]:
            by_name[_norm(person["name"])] = person
    return by_handle, by_name


def match(people: list[dict]) -> list[dict]:
    """people: [{name, linkedin_handle, role}] -> those who are the user's connections."""
    by_handle, by_name = connections()
    found = []
    for p in people:
        h = (p.get("linkedin_handle") or "").strip("/").split("/")[-1].lower()
        hit = by_handle.get(h) if h else None
        how = "linkedin profile"
        if not hit:
            hit, how = by_name.get(_norm(p.get("name", ""))), "name"
        if hit:
            found.append({"name": hit["name"], "role_at_event": p.get("role"), "company": hit["company"],
                          "position": hit["position"], "matched_by": how})
    return found
