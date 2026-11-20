"""Learn what events the user likes from their calendar history, and rank new events against it."""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from datetime import datetime, timedelta

from . import calendar_mac, config, store

STOP = set("""the and with for from your you our are this that into about at on in of to a an by or vs
san francisco sf bay area ca usa event events meetup meeting night day week edition series session sessions
2024 2025 2026 2027 new live special official group club community community's join free hosted host presents
min call interview sync placeholder item dinner lunch breakfast meet chat 1:1 between sam
build building personal tech coffee hours hour understand learn intro introduction workshop hands-on part edition"""
.split())
PROFILE_KEY = "taste:{chat}"
_building = threading.Lock()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z][a-z0-9+#.-]*", (text or "").lower())
    return [w.strip(".-") for w in words if (len(w) > 2 or w in {"ai", "ml", "vc", "ux"}) and w not in STOP]


def _history(days_back: int = 60) -> list[dict]:
    """Bulk-read past events (title, location) from the main calendar. Slow (~2 min), so cached."""
    now = datetime.now(config.TZ)
    cal = config.CALENDARS[0] if config.CALENDARS else "Calendar"
    q = f'every event of calendar {calendar_mac._q(cal)} whose start date ≥ d1 and start date ≤ d2'
    script = calendar_mac._as_date("d1", now - timedelta(days=days_back)) + calendar_mac._as_date("d2", now) + f'''
tell application "Calendar"
  set ts to summary of ({q})
  set ls to location of ({q})
end tell
set out to ""
repeat with i from 1 to count of ts
  set l to item i of ls
  if l is missing value then set l to ""
  set out to out & (item i of ts) & "{calendar_mac.SEP}" & l & "{calendar_mac.REC}"
end repeat
return out'''
    raw = calendar_mac._osa(script, timeout=300)
    out = []
    for rec in raw.split(calendar_mac.REC):
        parts = rec.split(calendar_mac.SEP)
        if len(parts) == 2 and parts[0].strip():
            out.append({"title": parts[0].strip(), "location": parts[1].strip()})
    return out


def build_profile(chat: str) -> dict:
    with _building:
        hist = _history()
        # Only things that look like events (have a place), not calls or reminders.
        events = [h for h in hist if h["location"] and not re.search(r"meet\.google|zoom\.us|teams\.microsoft|google meet", h["location"], re.I)]
        seen_titles = set()
        topics: Counter = Counter()
        venues: Counter = Counter()
        for e in events:
            if e["title"] in seen_titles:
                continue
            seen_titles.add(e["title"])
            topics.update(set(_tokens(e["title"])))
            venue = e["location"].split(",")[0].strip()
            if venue and not re.match(r"^\d", venue) and "http" not in venue:
                venues[venue] += 1
        profile = {"built": datetime.now(config.TZ).isoformat(), "events_learned_from": len(seen_titles),
                   "topics": dict(topics.most_common(40)), "venues": dict(venues.most_common(15)),
                   "examples": sorted(seen_titles)[:15]}
        store.set_state(PROFILE_KEY.format(chat=chat), profile)
        store.audit(chat, "taste", f"Learned taste from {len(seen_titles)} past events: "
                    + ", ".join(list(profile["topics"])[:8]))
        return profile


def get_profile(chat: str) -> dict | None:
    return store.get_state(PROFILE_KEY.format(chat=chat))


def refresh_in_background(chat: str, max_age_hours: int = 24) -> None:
    p = get_profile(chat)
    if p and datetime.fromisoformat(p["built"]) > datetime.now(config.TZ) - timedelta(hours=max_age_hours):
        return
    threading.Thread(target=lambda: _safe_build(chat), daemon=True).start()


def _safe_build(chat: str) -> None:
    try:
        build_profile(chat)
    except Exception as exc:  # never take the agent down over this
        import logging
        logging.getLogger("plusone").warning("taste profile build failed: %s", exc)


def score(event: dict, profile: dict, rules_text: str = "") -> tuple[float, list[str]]:
    topics, venues = profile.get("topics", {}), profile.get("venues", {})
    total = sum(topics.values()) or 1
    s, reasons = 0.0, []
    toks = set(_tokens(f"{event.get('title', '')} {event.get('host', '')}"))
    hits = sorted((t for t in toks if t in topics), key=lambda t: -topics[t])
    for t in hits:
        s += math.sqrt(topics[t])  # topics you go to a lot (hackathon x15) outweigh one-offs
    if hits:
        reasons.append("you often go to " + "/".join(hits[:3]) + " events")
    # interests the user stated explicitly count double
    for t in toks & set(_tokens(rules_text)):
        s += 2
        reasons.append(f"matches your interest '{t}'")
    venue = (event.get("venue") or "")
    for v, n in venues.items():
        if v.lower()[:18] in venue.lower() or venue.lower()[:18] in v.lower():
            s += 1.5
            reasons.append(f"at {v}, where you've been {n}x")
            break
    if event.get("is_free"):
        s += 0.3
    return s, reasons
