"""Read and write the macOS Calendar app (which syncs Google/iCloud calendars) via AppleScript."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta

from . import config

SEP = "\x1f"
REC = "\x1e"


def _osa(script: str, timeout: int = 60) -> str:
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "osascript failed")
    return p.stdout.strip()


def _q(s: str) -> str:
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_date(var: str, dt: datetime) -> str:
    """AppleScript lines that build a date in local time without locale parsing."""
    d = dt.astimezone(config.TZ)
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {d.year}\n"
            f"set month of {var} to {d.month}\n"
            f"set day of {var} to {d.day}\n"
            f"set time of {var} to {d.hour * 3600 + d.minute * 60}\n")


_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 300  # seconds; AppleScript calendar reads are slow


def list_events(start: datetime, end: datetime) -> list[dict]:
    """Events in [start, end] across the configured calendars, cached per day for a few minutes."""
    s = start.astimezone(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    f = end.astimezone(config.TZ)
    out: list[dict] = []
    day = s
    missing_from = None
    while day <= f:
        hit = _CACHE.get(day.date().isoformat())
        if not hit or time.time() - hit[0] > _TTL:
            missing_from = missing_from or day
        day += timedelta(days=1)
    if missing_from:
        fetched = _list_events_raw(missing_from, f.replace(hour=23, minute=59))
        day = missing_from
        while day <= f:
            key = day.date().isoformat()
            _CACHE[key] = (time.time(), [e for e in fetched if e["start"][:10] == key])
            day += timedelta(days=1)
    day = s
    while day <= f:
        out += _CACHE.get(day.date().isoformat(), (0, []))[1]
        day += timedelta(days=1)
    return [e for e in out if start <= datetime.fromisoformat(e["end"]) and datetime.fromisoformat(e["start"]) <= end]


def warm(days: int = 14) -> None:
    now = datetime.now(config.TZ)
    list_events(now, now + timedelta(days=days))


def _list_events_raw(start: datetime, end: datetime) -> list[dict]:
    """Events in [start, end] across the configured calendars (recurring series are not expanded)."""
    cals = config.CALENDARS or []
    cal_filter = ("{" + ", ".join(_q(c) for c in cals) + "}") if cals else "name of every calendar"
    script = _as_date("d1", start) + _as_date("d2", end) + f"""
set out to ""
tell application "Calendar"
  repeat with cname in {cal_filter}
    try
      set cal to calendar (cname as text)
      set evs to (every event of cal whose start date ≥ d1 and start date ≤ d2)
      repeat with e in evs
        set s to start date of e
        set f to end date of e
        set u to ""
        try
          set u to (url of e) as text
        end try
        set l to ""
        try
          set l to (location of e) as text
        end try
        set n to ""
        try
          set n to (description of e) as text
          if (length of n) > 400 then set n to text 1 thru 400 of n
        end try
        set out to out & (cname as text) & "{SEP}" & (summary of e) & "{SEP}" & ¬
          ((year of s) as text) & "-" & ((month of s as integer) as text) & "-" & ((day of s) as text) & " " & ((time of s) as text) & "{SEP}" & ¬
          ((year of f) as text) & "-" & ((month of f as integer) as text) & "-" & ((day of f) as text) & " " & ((time of f) as text) & "{SEP}" & ¬
          u & "{SEP}" & l & "{SEP}" & n & "{REC}"
      end repeat
    end try
  end repeat
end tell
return out"""
    raw = _osa(script, timeout=90)
    events = []
    for rec in raw.split(REC):
        parts = rec.strip("\n").split(SEP)
        if len(parts) != 7:
            continue
        cal, title, s, f, url, loc, notes = (("" if p == "missing value" else p) for p in parts)
        events.append({"calendar": cal, "title": title, "start": _parse(s), "end": _parse(f),
                       "url": url, "location": loc, "notes": notes})
    events.sort(key=lambda e: e["start"])
    return events


def _parse(s: str) -> str:
    date, secs = s.split(" ")
    y, m, d = (int(x) for x in date.split("-"))
    secs_i = int(float(secs))
    dt = datetime(y, m, d, secs_i // 3600, (secs_i % 3600) // 60, tzinfo=config.TZ)
    return dt.isoformat()


def conflicts(start: datetime, end: datetime) -> list[dict]:
    clashes = []
    for e in list_events(start.replace(hour=0, minute=0), end.replace(hour=23, minute=59)):
        es, ee = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        if es < end and ee > start and (ee - es).total_seconds() < 20 * 3600:  # skip all-day events
            clashes.append(e)
    return clashes


def add_event(title: str, start: datetime, end: datetime, location: str = "", url: str = "", notes: str = "") -> str:
    cal = config.CALENDARS[0] if config.CALENDARS else None
    target = f"calendar {_q(cal)}" if cal else "first calendar"
    script = _as_date("d1", start) + _as_date("d2", end) + f"""
tell application "Calendar"
  tell {target}
    set e to make new event with properties {{summary:{_q(title)}, start date:d1, end date:d2, location:{_q(location)}, url:{_q(url)}, description:{_q(notes)}}}
  end tell
  return uid of e
end tell"""
    uid = _osa(script)
    _CACHE.pop(start.astimezone(config.TZ).date().isoformat(), None)
    return uid
