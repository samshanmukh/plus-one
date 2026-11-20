"""Tools the agent can call. Every call is written to the audit trail by `run_tool`."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import calendar_mac, config, sources, store


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=config.TZ)


def _local(iso: str | None) -> str | None:
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone(config.TZ).strftime("%a %b %-d, %-I:%M%p")


def _compact(e: dict) -> dict:
    out = {k: e.get(k) for k in ("id", "source", "title", "price", "sold_out", "requires_approval", "url", "host", "venue")}
    out["when"] = _local(e.get("start"))
    out["start"], out["end"] = e.get("start"), e.get("end")
    if e.get("description"):
        out["description"] = e["description"][:300]
    return out


# Events seen during this process, so propose_booking can take an id instead of a full object.
_seen_events: dict[str, dict] = {}


def search_events(chat: str, query: str = "", start: str | None = None, end: str | None = None,
                  max_price: float | None = None, sources_: list[str] | None = None) -> dict:
    after = _dt(start) or datetime.now(config.TZ)
    before = _dt(end) or after + timedelta(days=7)
    res = sources.search_all(query, after, before, max_price, sources_)
    for e in res["events"]:
        _seen_events[e["id"]] = e
    store.remember_events(chat, res["events"])
    res["events"] = [_compact(e) for e in res["events"]]
    return res


def get_event_from_link(chat: str, url: str) -> dict:
    e = sources.event_from_link(url)
    if not e:
        return {"error": "Could not read an event from that link."}
    _seen_events[e["id"]] = e
    store.remember_events(chat, [e])
    return _compact(e)


def recall_events(chat: str, query: str) -> dict:
    found = store.recall_events(chat, query)
    for e in found:
        _seen_events[e["id"]] = e
    return {"events": [_compact(e) for e in found], "backend": store.db.kind}


def check_calendar(chat: str, start: str, end: str) -> dict:
    s, f = _dt(start), _dt(end)
    events = calendar_mac.list_events(s, f)
    return {"events": [{"calendar": e["calendar"], "title": e["title"], "when": _local(e["start"]),
                        "start": e["start"], "end": e["end"]} for e in events]}


_SOURCES = [("luma", r"lu\.ma/|luma\.com/"), ("partiful", r"partiful\.com"), ("meetup", r"meetup\.com"),
            ("eventbrite", r"eventbrite\."), ("plusone", r"Booked by (Plus One|Bluetail)")]


def _link(text: str) -> str | None:
    import re
    m = re.search(r"https?://(?:www\.)?(?:lu\.ma|luma\.com|partiful\.com|meetup\.com|eventbrite\.[a-z.]+)/[^\s<>\")]+", text)
    return m.group(0) if m else None


def my_events(chat: str, start: str | None = None, end: str | None = None) -> dict:
    """The user's upcoming events from their calendar, tagged by platform, with double-bookings flagged."""
    import re
    s = _dt(start) or datetime.now(config.TZ)
    f = _dt(end) or s + timedelta(days=7)
    events = []
    for e in calendar_mac.list_events(s, f):
        blob = " ".join([e.get("url", ""), e.get("location", ""), e.get("notes", ""), e["title"], e["calendar"]])
        source = next((name for name, pat in _SOURCES if re.search(pat, blob, re.I)), "calendar")
        es, ee = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        events.append({"title": e["title"], "when": _local(e["start"]), "until": _local(e["end"]),
                       "start": e["start"], "end": e["end"], "source": source,
                       "link": _link(blob) or (e.get("url") or None), "where": (e.get("location") or "")[:160],
                       "all_day": (ee - es).total_seconds() >= 20 * 3600})
    timed = [e for e in events if not e["all_day"]]
    clashes = []
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if b["start"] < a["end"] and a["start"] < b["end"]:
                clashes.append({"a": a["title"], "b": b["title"], "when": a["when"]})
    return {"events": events, "double_booked": clashes,
            "ticket_note": "For Luma events, `link` is the user's personal ticket link (it carries their guest key): it opens their ticket with the check-in QR code.",
            "counts": {k: sum(1 for e in events if e["source"] == k) for k in {e["source"] for e in events}}}


def _origin(chat: str) -> tuple[float, float, str]:
    loc = store.get_state(f"location:{chat}")
    if loc:
        return loc["lat"], loc["lon"], loc["name"]
    return config.HOME_LAT, config.HOME_LON, "San Francisco (default)"


def set_location(chat: str, place: str) -> dict:
    from . import travel
    hit = travel.geocode(place)
    if not hit:
        return {"error": f"Couldn't find '{place}'. Try a street address or a well-known place."}
    store.set_state(f"location:{chat}", {"lat": hit[0], "lon": hit[1], "name": place, "resolved": hit[2]})
    return {"saved": True, "location": place, "resolved": hit[2][:120]}


def travel_time(chat: str, destination: str) -> dict:
    from . import travel
    lat, lon, name = _origin(chat)
    dest = travel.geocode(destination)
    if not dest:
        return {"error": f"Couldn't locate '{destination}'."}
    res = travel.drive_minutes((lat, lon), dest[:2])
    if not res:
        return {"error": "Routing service unavailable right now."}
    return {"from": name, "to": destination, "drive_minutes": res[0], "km": res[1],
            "note": "Car estimate incl. traffic/parking padding; transit not included."}


def who_is_going(chat: str, url: str) -> dict:
    """Which of the user's LinkedIn connections are hosting or featured at a Luma event."""
    import re
    from . import linkedin
    by_handle, _ = linkedin.connections()
    if not by_handle and not linkedin.connections()[1]:
        return {"error": "No LinkedIn connections loaded yet. Ask the user to export Connections from LinkedIn "
                         "(Settings > Data privacy > Get a copy of your data) and drop Connections.csv in Plus One's data folder."}
    m = re.search(r"(?:lu\.ma|luma\.com)/(?:event/)?([A-Za-z0-9-]+)", url)
    if not m:
        return {"error": "Only Luma events show their guests publicly right now; send a Luma link."}
    d = sources.http.get("https://api.lu.ma/url", params={"url": m.group(1)}).json().get("data", {})
    people = [{"name": h.get("name"), "linkedin_handle": h.get("linkedin_handle"), "role": "host"} for h in d.get("hosts") or []]
    people += [{"name": g.get("name"), "linkedin_handle": g.get("linkedin_handle"), "role": "guest"}
               for g in d.get("featured_guests") or []]
    found = linkedin.match(people)
    return {"event": (d.get("event") or {}).get("name"), "connections_going": found,
            "checked": len(people), "guest_count": d.get("guest_count"),
            "note": "Luma only shows hosts and featured guests publicly, so others may be going too."}


# Images a tool wants sent with the next reply (main.py uploads and attaches them).
outbox_media: dict[str, list[str]] = {}
QR_DIR = Path(__file__).resolve().parent.parent / "data" / "qr"


def ticket_qr(chat: str, event: str) -> dict:
    """Make the user's Luma check-in QR for an event they're registered for, to send as an image."""
    import re
    import qrcode
    now = datetime.now(config.TZ)
    mine = my_events(chat, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=30)).isoformat())["events"]
    words = [w for w in re.findall(r"[a-z0-9]+", event.lower()) if len(w) > 2]
    def score(e: dict) -> int:
        return sum(w in e["title"].lower() for w in words)
    luma = [e for e in mine if e["source"] == "luma" and e.get("link") and "pk=" in e["link"]]
    if not luma:
        return {"error": "No Luma events with a personal ticket link on your calendar in the next 30 days."}
    best = max(luma, key=score)
    if words and score(best) == 0:
        return {"error": f"Couldn't match '{event}'.", "your_luma_events": [e["title"] for e in luma]}
    m = re.search(r"(?:lu\.ma|luma\.com)/([A-Za-z0-9-]+)\?pk=([A-Za-z0-9_-]+)", best["link"])
    if not m:
        return {"error": "That event's link has no guest key."}
    slug, pk = m.groups()
    data = sources.http.get("https://api.lu.ma/url", params={"url": slug}).json().get("data", {})
    event_id = data.get("api_id") or (data.get("event") or {}).get("api_id")
    if not event_id:
        return {"error": "Luma didn't return the event id."}
    QR_DIR.mkdir(parents=True, exist_ok=True)
    path = QR_DIR / f"{event_id}.png"
    img = qrcode.make(f"https://luma.com/check-in/{event_id}?pk={pk}", box_size=12, border=3)
    img.save(path)
    outbox_media.setdefault(chat, []).append(str(path))
    store.audit(chat, "ticket", f"Generated check-in QR for {best['title'][:60]}")
    return {"qr_ready": True, "event": best["title"], "when": best["when"], "where": best["where"],
            "note": "The QR image is attached to your reply automatically. Mention it is their Luma check-in code."}


def suggest_for_me(chat: str, start: str | None = None, end: str | None = None, limit: int = 5) -> dict:
    """Rank upcoming events by the user's taste (calendar history + stated interests), skipping clashes."""
    import re
    from . import linkedin, taste
    profile = taste.get_profile(chat)
    if not profile:
        taste.refresh_in_background(chat, max_age_hours=0)
        return {"error": "Still learning your taste from your calendar (takes ~2 minutes the first time). Ask again shortly, or tell me a topic."}
    after = _dt(start) or datetime.now(config.TZ)
    before = _dt(end) or after + timedelta(days=7)
    rules = store.list_rules(chat)
    rules_text = " ".join(r["text"] for r in rules if r["kind"] in ("interest", "preference"))
    budget = None
    for r in rules:
        m = re.search(r"\$\s*(\d+)", r["text"])
        if r["kind"] == "budget" and m:
            budget = float(m.group(1))
    pool = sources.search_luma("", after, before, limit=400)
    top_topics = list(profile["topics"])[:3]
    for t in top_topics:
        try:
            pool += sources.search_meetup(t, after, before, limit=10)
        except Exception:
            pass
    going = {e["title"].lower() for e in my_events(chat, after.isoformat(), before.isoformat())["events"]}
    ranked = []
    seen = set()
    for e in pool:
        key = re.sub(r"[^a-z0-9]", "", (e["title"] or "").lower())[:40]
        if key in seen or e.get("sold_out") or (e["title"] or "").lower() in going:
            continue
        seen.add(key)
        if budget is not None and not e["is_free"]:
            m = re.search(r"[\d.]+", e["price"] or "")
            if m and float(m.group(0)) > budget:
                continue
        sc, reasons = taste.score(e, profile, rules_text)
        if sc > 0.5:
            ranked.append((sc, e, reasons))
    ranked.sort(key=lambda x: -x[0])
    picks = []
    for sc, e, reasons in ranked[: limit * 2]:
        s_, f_ = _dt(e.get("start")), _dt(e.get("end"))
        try:
            clash = calendar_mac.conflicts(s_, f_ or s_ + timedelta(hours=2)) if s_ else []
        except Exception:
            clash = []
        if clash:
            continue
        if e["source"] == "luma" and any(linkedin.connections()):
            try:
                people = who_is_going(chat, e["url"]).get("connections_going") or []
                if people:
                    reasons.append(f"{len(people)} of your LinkedIn connections: " + ", ".join(p["name"] for p in people[:3]))
                    sc += 2 * len(people)
            except Exception:
                pass
        _seen_events[e["id"]] = e
        picks.append({**_compact(e), "why": reasons, "score": round(sc, 1)})
        if len(picks) >= limit:
            break
    store.remember_events(chat, [_seen_events[p["id"]] for p in picks])
    return {"picks": picks, "learned_from": profile["events_learned_from"],
            "your_top_topics": list(profile["topics"])[:8], "checked": len(seen)}


def save_rule(chat: str, text: str, kind: str = "preference") -> dict:
    rid = store.add_rule(chat, kind, text)
    return {"saved": True, "rule_id": rid, "rules": store.list_rules(chat)}


def list_rules(chat: str) -> dict:
    return {"rules": store.list_rules(chat)}


def delete_rule(chat: str, rule_id: int) -> dict:
    return {"deleted": store.delete_rule(chat, rule_id), "rules": store.list_rules(chat)}


def propose_booking(chat: str, event_id: str, reason: str, rules_applied: list[str] | None = None) -> dict:
    e = _seen_events.get(event_id) or store.get_event(event_id)
    if not e:
        return {"error": f"Unknown event_id {event_id}. Search or read the link first."}
    if e.get("sold_out"):
        return {"error": "That event is sold out; suggest another or offer to watch the waitlist."}
    s, f = _dt(e.get("start")), _dt(e.get("end"))
    clashes = []
    if s:
        f = f or s + timedelta(hours=2)
        try:
            clashes = calendar_mac.conflicts(s, f)
        except Exception as exc:  # calendar unavailable should not block a proposal
            clashes = [{"title": f"(calendar check failed: {exc})"}]
    bid = store.propose_booking(chat, e, reason + (f" | rules: {'; '.join(rules_applied)}" if rules_applied else ""))
    store.audit(chat, "proposal", f"Proposed booking #{bid}: {e['title']} ({e['price']})",
                {"event": _compact(e), "reason": reason, "rules_applied": rules_applied, "conflicts": clashes},
                booking_id=bid)
    return {"booking_id": bid, "event": _compact(e), "conflicts": clashes,
            "next": "Ask the user to reply YES to book (or NO). Do not call execute_booking until they approve."}


def execute_booking(chat: str, booking_id: int) -> dict:
    b = store.get_booking(booking_id)
    if not b or b["chat"] != chat:
        return {"error": "No such booking."}
    if b["status"] != "approved":
        # Approval is recorded by code from the user's own message, never by the model.
        return {"error": f"Booking #{booking_id} is '{b['status']}', not approved by the user. Ask them to reply YES."}
    e = b["event"]
    s = _dt(e.get("start"))
    f = _dt(e.get("end")) or (s + timedelta(hours=2) if s else None)
    result: dict[str, Any] = {"event": e["title"], "url": e["url"]}
    if s:
        try:
            uid = calendar_mac.add_event(
                e["title"], s, f, location=e.get("venue") or "", url=e["url"],
                notes=f"Booked by Plus One (booking #{booking_id}). {e['price']}. Why: {b['reason']}")
            result["calendar"] = {"added": True, "calendar": (config.CALENDARS or ["default"])[0], "uid": uid}
        except Exception as exc:
            result["calendar"] = {"added": False, "error": str(exc)[:200]}
    # Registration: the host's page is the only reliable way to RSVP or pay for every source.
    if e.get("is_free"):
        result["rsvp"] = {"status": "rsvp_link", "how": "Tap to confirm your free spot", "link": e["url"]}
    else:
        result["rsvp"] = {"status": "checkout_link", "price": e["price"],
                          "how": "Tap to pay on the host's checkout page; Plus One never handles card details",
                          "link": e["url"]}
    if e.get("requires_approval"):
        result["rsvp"]["note"] = "Host approves guests; you'll get an email from the host."
    store.finish_booking(booking_id, result)
    store.audit(chat, "booking", f"Executed booking #{booking_id}: {e['title']}", result, booking_id=booking_id)
    return result


def get_audit_trail(chat: str, booking_id: int | None = None, limit: int = 20) -> dict:
    trail = store.audit_trail(chat, limit=limit, booking_id=booking_id)
    bookings = store.list_bookings(chat, limit=5)
    return {
        "trail": [{"at": _local(t["at"]), "kind": t["kind"], "what": t["summary"]} for t in trail],
        "recent_bookings": [{"id": b["id"], "status": b["status"], "event": b["event"]["title"],
                             "approved_at": _local(b["approved_at"]), "approval_text": b["approval_text"],
                             "reason": b["reason"]} for b in bookings],
    }


TOOLS: list[dict] = [
    {
        "name": "search_events",
        "description": "Search upcoming in-person events near the user on Luma and Meetup. Returns events with price, time, sold-out status and link. Use ISO 8601 dates in the user's time zone.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "description": "Topic keywords, e.g. 'AI agents' or 'climbing'. Empty for anything."},
            "start": {"type": "string", "description": "Earliest start, ISO 8601, e.g. 2026-11-21T00:00:00-08:00"},
            "end": {"type": "string", "description": "Latest start, ISO 8601"},
            "max_price": {"type": "number", "description": "Max ticket price in USD; 0 for free only"},
            "sources": {"type": "array", "items": {"type": "string", "enum": ["luma", "meetup"]}},
        }, "required": []},
    },
    {
        "name": "get_event_from_link",
        "description": "Read one event from a Luma, Meetup, Partiful or other event page URL the user sent.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "recall_events",
        "description": "Search events Plus One already showed or read for this user (e.g. 'that robotics thing from earlier', 'the Partiful party'). Use before searching again when the user refers back to something.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"query": {"type": "string", "description": "Words from the title, host or topic"}},
                         "required": ["query"]},
    },
    {
        "name": "my_events",
        "description": "List the events the user is already going to (their Luma, Partiful, Meetup, Eventbrite RSVPs and other calendar events), tagged by platform, with double-bookings flagged. Use for 'what am I doing this week', 'what's on tonight', 'any clashes?', or to help them pick which overlapping event to skip.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "start": {"type": "string", "description": "ISO 8601; default now"},
            "end": {"type": "string", "description": "ISO 8601; default 7 days after start"}}, "required": []},
    },
    {
        "name": "set_location",
        "description": "Save where the user is now (or their home base) when they tell you, e.g. 'I'm at Salesforce Park' or 'I live in the Mission'. Used for travel times and leave-now alerts.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"place": {"type": "string", "description": "Address or place name"}},
                         "required": ["place"]},
    },
    {
        "name": "travel_time",
        "description": "Estimate drive time from the user's saved location to an address or venue (e.g. an event's location from my_events).",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"destination": {"type": "string"}}, "required": ["destination"]},
    },
    {
        "name": "who_is_going",
        "description": "Check which of the user's LinkedIn connections are hosting or going to a Luma event (from the event's public hosts and featured guests). Use when they ask who they know at an event, or when recommending events.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"url": {"type": "string", "description": "Luma event URL"}}, "required": ["url"]},
    },
    {
        "name": "ticket_qr",
        "description": "Send the user's Luma check-in QR code (as an image) for an event they're registered for, e.g. 'my QR for HackwithBay', 'ticket for tonight'.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"event": {"type": "string", "description": "Event name or words from it"}},
                         "required": ["event"]},
    },
    {
        "name": "suggest_for_me",
        "description": "Personal picks: upcoming events ranked by what the user actually goes to (learned from their calendar history), their stated interests and budget, skipping clashes, boosted when LinkedIn connections attend. Use this first for open requests like 'anything for me this weekend?', 'what should I go to?'.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "start": {"type": "string", "description": "ISO 8601; default now"},
            "end": {"type": "string", "description": "ISO 8601; default 7 days later"}}, "required": []},
    },
    {
        "name": "check_calendar",
        "description": "List the user's calendar events between two ISO 8601 times, to find conflicts or free time.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "start": {"type": "string"}, "end": {"type": "string"}}, "required": ["start", "end"]},
    },
    {
        "name": "save_rule",
        "description": "Save a lasting preference or rule the user states (budget cap, interests, times to avoid, people/venues to prefer).",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "text": {"type": "string", "description": "The rule in the user's words, normalized, e.g. 'Max $30 per ticket'"},
            "kind": {"type": "string", "enum": ["budget", "interest", "schedule", "preference"]}},
            "required": ["text", "kind"]},
    },
    {
        "name": "list_rules",
        "description": "List the user's saved rules and preferences.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "name": "delete_rule",
        "description": "Delete a saved rule by id.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"rule_id": {"type": "integer"}}, "required": ["rule_id"]},
    },
    {
        "name": "propose_booking",
        "description": "Propose booking (RSVP or buying a ticket for) one event. Checks the calendar for conflicts and records the proposal. The user must then approve by text before anything is booked.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "event_id": {"type": "string", "description": "id from search_events or get_event_from_link"},
            "reason": {"type": "string", "description": "Why this event, in one sentence"},
            "rules_applied": {"type": "array", "items": {"type": "string"}, "description": "Saved rules this choice satisfies"}},
            "required": ["event_id", "reason"]},
    },
    {
        "name": "execute_booking",
        "description": "Carry out a booking the user has approved: adds it to their calendar and returns the RSVP or checkout link. Fails unless the user approved it.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"booking_id": {"type": "integer"}}, "required": ["booking_id"]},
    },
    {
        "name": "get_audit_trail",
        "description": "Get the step-by-step record of what the agent did and why (searches, sources, rules, proposals, approvals, bookings). Use when the user asks 'why?', 'what did you do?' or 'history'.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {
            "booking_id": {"type": "integer", "description": "Limit to one booking"},
            "limit": {"type": "integer"}}, "required": []},
    },
]

_FUNCS = {
    "search_events": lambda chat, a: search_events(chat, a.get("query", ""), a.get("start"), a.get("end"),
                                                   a.get("max_price"), a.get("sources")),
    "get_event_from_link": lambda chat, a: get_event_from_link(chat, a["url"]),
    "recall_events": lambda chat, a: recall_events(chat, a["query"]),
    "my_events": lambda chat, a: my_events(chat, a.get("start"), a.get("end")),
    "set_location": lambda chat, a: set_location(chat, a["place"]),
    "travel_time": lambda chat, a: travel_time(chat, a["destination"]),
    "who_is_going": lambda chat, a: who_is_going(chat, a["url"]),
    "ticket_qr": lambda chat, a: ticket_qr(chat, a["event"]),
    "suggest_for_me": lambda chat, a: suggest_for_me(chat, a.get("start"), a.get("end")),
    "check_calendar": lambda chat, a: check_calendar(chat, a["start"], a["end"]),
    "save_rule": lambda chat, a: save_rule(chat, a["text"], a.get("kind", "preference")),
    "list_rules": lambda chat, a: list_rules(chat),
    "delete_rule": lambda chat, a: delete_rule(chat, int(a["rule_id"])),
    "propose_booking": lambda chat, a: propose_booking(chat, a["event_id"], a["reason"], a.get("rules_applied")),
    "execute_booking": lambda chat, a: execute_booking(chat, int(a["booking_id"])),
    "get_audit_trail": lambda chat, a: get_audit_trail(chat, a.get("booking_id"), int(a.get("limit") or 20)),
}


def _summarize(name: str, args: dict, result: Any) -> str:
    if name == "search_events":
        n = len(result.get("events", [])) if isinstance(result, dict) else 0
        return (f"Searched {', '.join(result.get('searched', []))} for '{args.get('query', '')}' "
                f"{args.get('start', '')}..{args.get('end', '')} max ${args.get('max_price', '-')}: {n} events")
    if name == "my_events":
        return (f"Read your upcoming events {args.get('start', 'now')}..{args.get('end', '+7d')}: "
                f"{len(result.get('events', []))} events {result.get('counts')}, {len(result.get('double_booked', []))} clashes")
    if name == "check_calendar":
        return f"Checked calendar {args.get('start')}..{args.get('end')}: {len(result.get('events', []))} events"
    if name == "recall_events":
        return f"Recalled past events for '{args.get('query')}' ({result.get('backend')}): {len(result.get('events', []))} found"
    if name == "get_event_from_link":
        return f"Read event link {args.get('url')}: {result.get('title', result.get('error'))}"
    return f"{name}({json.dumps(args)[:200]})"


def run_tool(chat: str, name: str, args: dict) -> tuple[str, bool]:
    fn = _FUNCS.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool {name}"}), True
    try:
        result = fn(chat, args)
        is_error = isinstance(result, dict) and "error" in result
    except Exception as exc:
        result, is_error = {"error": f"{type(exc).__name__}: {exc}"[:400]}, True
    if name not in ("propose_booking", "execute_booking", "get_audit_trail"):  # those log themselves / are reads
        store.audit(chat, "tool", _summarize(name, args, result), {"args": args, "error": is_error})
    return json.dumps(result, default=str), is_error
