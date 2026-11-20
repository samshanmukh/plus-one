"""Drive-time estimates: OpenStreetMap Nominatim for addresses, the public OSRM router for times."""
from __future__ import annotations

import re
import time
from functools import lru_cache

import httpx

from . import config

_http = httpx.Client(timeout=15, headers={"User-Agent": "PlusOneEventsAgent/0.1 (hackathon demo)"})
_last_geocode = 0.0


@lru_cache(maxsize=256)
def geocode(place: str) -> tuple[float, float, str] | None:
    """(lat, lon, display name) for an address or place name, biased to the user's area."""
    global _last_geocode
    q = place.strip()
    if not q:
        return None
    for attempt in (q, re.sub(r"(?i)\b(suite|ste|unit|floor|fl)\b\.?\s*\w+,?", "", q), q.split(",", 1)[-1]):
        wait = 1.1 - (time.time() - _last_geocode)  # Nominatim allows 1 request/second
        if wait > 0:
            time.sleep(wait)
        _last_geocode = time.time()
        r = _http.get("https://nominatim.openstreetmap.org/search",
                      params={"q": attempt, "format": "json", "limit": 1,
                              "viewbox": f"{config.HOME_LON - 1},{config.HOME_LAT + 1},{config.HOME_LON + 1},{config.HOME_LAT - 1}"})
        if r.status_code == 200 and r.json():
            hit = r.json()[0]
            return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", attempt)
    return None


def drive_minutes(origin: tuple[float, float], dest: tuple[float, float]) -> tuple[int, float] | None:
    """(minutes, km) by car, padded 25% for city traffic and parking."""
    r = _http.get(f"https://router.project-osrm.org/route/v1/driving/"
                  f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}", params={"overview": "false"})
    if r.status_code != 200 or r.json().get("code") != "Ok":
        return None
    route = r.json()["routes"][0]
    return round(route["duration"] / 60 * 1.25) + 5, round(route["distance"] / 1000, 1)
