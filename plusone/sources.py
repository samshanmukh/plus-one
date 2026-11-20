"""Event sources: Luma and Meetup search, plus link lookup for Luma, Meetup and Partiful.

Every function returns events in one normalized shape:
  {id, source, title, start, end, venue, price, is_free, sold_out,
   requires_approval, url, host}
`start`/`end` are ISO 8601 strings with a UTC offset.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from . import config

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) PlusOneEventsAgent/0.1"}
http = httpx.Client(timeout=20, headers=UA, follow_redirects=True)


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()


def _in_range(start: str | None, after: datetime | None, before: datetime | None) -> bool:
    if not start:
        return True
    t = datetime.fromisoformat(start)
    return (after is None or t >= after) and (before is None or t <= before)


def _words_match(query: str, *texts: str) -> bool:
    words = [w for w in re.split(r"[\s,]+", query.lower()) if w and w not in {"events", "event", "and", "or", "the"}]
    if not words:
        return True
    blob = " ".join(t for t in texts if t).lower()
    return any(re.search(rf"\b{re.escape(w)}", blob) for w in words)


# ---------------- Luma ----------------
def _luma_norm(entry: dict) -> dict:
    ev = entry.get("event", entry)
    ti = entry.get("ticket_info") or {}
    price = ti.get("price")
    geo = ev.get("geo_address_info") or {}
    hosts = entry.get("hosts") or []
    return {
        "id": f"luma:{ev.get('api_id')}",
        "source": "luma",
        "title": ev.get("name"),
        "start": _iso(ev.get("start_at")),
        "end": _iso(ev.get("end_at")),
        "venue": geo.get("address") or geo.get("sublocality") or geo.get("city_state") or ev.get("location_type"),
        "price": "Free" if ti.get("is_free", True) else (f"${price['cents'] / 100:.0f}" if price else "Paid"),
        "is_free": bool(ti.get("is_free", True)),
        "sold_out": bool(ti.get("is_sold_out")),
        "requires_approval": bool(ti.get("require_approval")),
        "url": f"https://luma.com/{ev.get('url')}",
        "host": (entry.get("calendar") or {}).get("name") or (hosts[0].get("name") if hosts else None),
    }


def search_luma(query: str, after: datetime | None, before: datetime | None, limit: int = 15) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(10):  # up to ~500 nearby events, chronological
        params: dict[str, Any] = {"pagination_limit": 50, "geo_latitude": config.HOME_LAT,
                                  "geo_longitude": config.HOME_LON}
        if cursor:
            params["pagination_cursor"] = cursor
        r = http.get("https://api.lu.ma/discover/get-paginated-events", params=params)
        r.raise_for_status()
        data = r.json()
        for entry in data.get("entries", []):
            e = _luma_norm(entry)
            if before and e["start"] and datetime.fromisoformat(e["start"]) > before:
                return out
            if _in_range(e["start"], after, before) and _words_match(
                    query, e["title"] or "", e["host"] or "",
                    " ".join(h.get("name") or "" for h in entry.get("hosts") or [])):
                out.append(e)
                if len(out) >= limit:
                    return out
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def luma_from_link(url: str) -> dict | None:
    m = re.search(r"(?:lu\.ma|luma\.com)/(?:event/)?([A-Za-z0-9-]+)", url)
    if not m:
        return None
    r = http.get("https://api.lu.ma/url", params={"url": m.group(1)})
    if r.status_code != 200:
        return None
    d = r.json()
    if d.get("kind") != "event":
        return None
    data = d["data"]
    e = _luma_norm(data)
    ev = data.get("event", {})
    e["description"] = (ev.get("description") or data.get("description_short") or "")[:600]
    return e


# ---------------- Meetup ----------------
MEETUP_GQL = "https://www.meetup.com/gql2"
_MEETUP_FIELDS = "id title dateTime endTime eventUrl venue{name city} feeSettings{amount currency} group{name} rsvpState"


def _meetup_norm(n: dict) -> dict:
    fee = n.get("feeSettings")
    venue = n.get("venue") or {}
    return {
        "id": f"meetup:{n.get('id')}",
        "source": "meetup",
        "title": n.get("title"),
        "start": _iso(n.get("dateTime")),
        "end": _iso(n.get("endTime")),
        "venue": ", ".join(x for x in [venue.get("name"), venue.get("city")] if x) or "Online/TBA",
        "price": f"${fee['amount']:.0f}" if fee and fee.get("amount") else "Free",
        "is_free": not (fee and fee.get("amount")),
        "sold_out": n.get("rsvpState") == "CLOSED",
        "requires_approval": False,
        "url": n.get("eventUrl"),
        "host": (n.get("group") or {}).get("name"),
    }


def _gql(query: str, variables: dict) -> dict:
    r = http.post(MEETUP_GQL, json={"query": query, "variables": variables})
    r.raise_for_status()
    d = r.json()
    if d.get("errors") and not d.get("data"):
        raise RuntimeError(d["errors"][0].get("message"))
    return d["data"]


def search_meetup(query: str, after: datetime | None, before: datetime | None, limit: int = 15) -> list[dict]:
    flt: dict[str, Any] = {"query": query or "events", "lat": config.HOME_LAT, "lon": config.HOME_LON, "radius": 25}
    if after:
        flt["startDateRange"] = after.isoformat()
    if before:
        flt["endDateRange"] = before.isoformat()
    q = f"query($f:EventSearchFilter!,$n:Int){{ eventSearch(filter:$f, first:$n){{ edges{{ node{{ {_MEETUP_FIELDS} }} }} }} }}"
    try:
        data = _gql(q, {"f": flt, "n": limit * 2})
    except Exception:
        # Date-range fields are picky; fall back to keyword-only and filter locally.
        flt.pop("startDateRange", None)
        flt.pop("endDateRange", None)
        data = _gql(q, {"f": flt, "n": 50})
    events = [_meetup_norm(e["node"]) for e in data["eventSearch"]["edges"]]
    events = [e for e in events if _in_range(e["start"], after, before)]
    # Meetup's keyword search also matches descriptions loosely; prefer title/group matches.
    strict = [e for e in events if _words_match(query, e["title"] or "", e["host"] or "")]
    return strict[:limit]


def meetup_from_link(url: str) -> dict | None:
    m = re.search(r"meetup\.com/.+?/events/(\d+)", url)
    if not m:
        return None
    q = f"query($id:ID!){{ event(id:$id){{ {_MEETUP_FIELDS} description }} }}"
    ev = _gql(q, {"id": m.group(1)}).get("event")
    if not ev:
        return None
    e = _meetup_norm(ev)
    e["description"] = (ev.get("description") or "")[:600]
    return e


# ---------------- Partiful / generic pages ----------------
def _meta(page: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"', page)
    return html.unescape(m.group(1)) if m else None


def page_from_link(url: str) -> dict | None:
    r = http.get(url)
    if r.status_code != 200:
        return None
    page = r.text
    ev: dict[str, Any] = {}
    # Partiful (Next.js) embeds the event object in __NEXT_DATA__.
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
            ev = props.get("event") or {}
        except json.JSONDecodeError:
            ev = {}
    # Schema.org Event (many event sites publish this).
    if not ev:
        for blob in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                j = json.loads(blob)
            except json.JSONDecodeError:
                continue
            for item in j if isinstance(j, list) else [j]:
                if isinstance(item, dict) and "Event" in str(item.get("@type", "")):
                    loc = item.get("location") or {}
                    offers = item.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    ev = {"title": item.get("name"), "startDate": item.get("startDate"),
                          "endDate": item.get("endDate"), "location": loc.get("name") if isinstance(loc, dict) else loc,
                          "price": offers.get("price"), "description": item.get("description")}
                    break
    title = ev.get("title") or ev.get("name") or _meta(page, "og:title")
    if not title:
        return None
    price = ev.get("price") or ev.get("ticketPrice")
    loc = ev.get("location") or ev.get("locationInfo") or {}
    if isinstance(loc, dict):
        loc = loc.get("displayName") or loc.get("name") or loc.get("address")
    source = "partiful" if "partiful.com" in url else re.sub(r"^www\.", "", httpx.URL(url).host)
    return {
        "id": f"{source}:{url.rstrip('/').rsplit('/', 1)[-1]}",
        "source": source,
        "title": title,
        "start": _iso(ev.get("startDate")) if isinstance(ev.get("startDate"), str) else None,
        "end": _iso(ev.get("endDate")) if isinstance(ev.get("endDate"), str) else None,
        "venue": loc or None,
        "price": "Free" if not price or str(price) in {"0", "0.0"} else f"${price}",
        "is_free": not price or str(price) in {"0", "0.0"},
        "sold_out": False,
        "requires_approval": False,
        "url": url,
        "host": None,
        "description": (ev.get("description") or _meta(page, "og:description") or "")[:600],
    }


def event_from_link(url: str) -> dict | None:
    if re.search(r"(lu\.ma|luma\.com)/", url):
        return luma_from_link(url) or page_from_link(url)
    if "meetup.com" in url:
        return meetup_from_link(url) or page_from_link(url)
    return page_from_link(url)


def search_all(query: str, after: datetime | None, before: datetime | None,
               max_price: float | None, sources: list[str] | None, limit: int = 12) -> dict:
    sources = sources or ["luma", "meetup"]
    results: list[dict] = []
    errors: dict[str, str] = {}
    for name, fn in (("luma", search_luma), ("meetup", search_meetup)):
        if name not in sources:
            continue
        try:
            results += fn(query, after, before, limit)
        except Exception as exc:  # one source failing should not sink the search
            errors[name] = str(exc)[:200]
    if max_price is not None:
        def cost(e: dict) -> float:
            if e["is_free"]:
                return 0.0
            m = re.search(r"[\d.]+", e["price"] or "")
            return float(m.group(0)) if m else 1e9
        results = [e for e in results if cost(e) <= max_price]
    results.sort(key=lambda e: e["start"] or "9999")
    return {"events": results[: limit * 2], "errors": errors,
            "searched": sources, "note": "Partiful has no public search; paste a partiful.com link to add one."}


def utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
