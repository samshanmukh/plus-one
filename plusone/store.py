"""Storage: chat history, user rules, every event Plus One has seen, bookings, and the audit trail.

Uses SingleStore when SINGLESTORE_URL is set (full-text search powers event recall),
otherwise a local SQLite file so the agent still runs offline.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config  # noqa: F401  (loads .env)

DB_PATH = Path(__file__).resolve().parent.parent / "plusone.db"
SINGLESTORE_URL = os.getenv("SINGLESTORE_URL", "").strip()
# Alternative to the URL: separate fields, so passwords with @ # / : need no escaping.
S2_PARTS = {k: os.getenv(f"SINGLESTORE_{k.upper()}", "").strip() for k in ("host", "port", "user", "password", "database")}
S2_ENABLED = bool(SINGLESTORE_URL or S2_PARTS["host"])
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- engine
class _DB:
    def __init__(self) -> None:
        self.kind = "singlestore" if S2_ENABLED else "sqlite"
        self._conn = None

    def _connect(self):
        if self.kind == "singlestore":
            import singlestoredb as s2
            if SINGLESTORE_URL:
                return s2.connect(SINGLESTORE_URL, results_type="dicts", autocommit=True)
            p = S2_PARTS
            return s2.connect(host=p["host"], port=int(p["port"] or 3306), user=p["user"] or "admin",
                              password=p["password"], database=p["database"] or "plusone",
                              results_type="dicts", autocommit=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.isolation_level = None  # autocommit
        return c

    def run(self, sql: str, params: tuple = (), fetch: bool = False) -> tuple[list[dict], int, int]:
        """Execute one statement. Write SQL with '?' placeholders; returns (rows, lastrowid, rowcount)."""
        if self.kind == "singlestore":
            sql = sql.replace("%", "%%").replace("?", "%s")
        with _lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None:
                        self._conn = self._connect()
                    cur = self._conn.cursor()
                    cur.execute(sql, params)
                    rows = [dict(r) for r in cur.fetchall()] if fetch else []
                    return rows, int(cur.lastrowid or 0), int(cur.rowcount or 0)
                except Exception as exc:
                    if self.kind == "singlestore" and attempt == 1:
                        self._conn = None  # dropped connection: reconnect once
                        continue
                    if self.kind == "singlestore" and "Can't connect" in str(exc):
                        # SingleStore unreachable: keep the agent alive on the local SQLite file.
                        import logging
                        logging.getLogger("plusone").warning("SingleStore unreachable, falling back to SQLite: %s", exc)
                        self.kind, self._conn = "sqlite", None
                        for stmt in SQLITE_SCHEMA:
                            self.run(stmt)
                    raise
        raise RuntimeError("unreachable")


db = _DB()


def _q(sql: str, *params: Any) -> list[dict]:
    return db.run(sql, params, fetch=True)[0]


def _x(sql: str, *params: Any) -> tuple[int, int]:
    _, lastrowid, rowcount = db.run(sql, params)
    return lastrowid, rowcount


SQLITE_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat TEXT, role TEXT, body TEXT, at TEXT)",
    "CREATE TABLE IF NOT EXISTS seen (msg_id TEXT PRIMARY KEY, at TEXT)",
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, at TEXT)",
    "CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, chat TEXT, kind TEXT, body TEXT, at TEXT)",
    """CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, chat TEXT, source TEXT, title TEXT,
        description TEXT, host TEXT, starts_at TEXT, price TEXT, url TEXT, data TEXT, seen_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS bookings (id INTEGER PRIMARY KEY, chat TEXT, status TEXT, event_json TEXT,
        reason TEXT, proposed_at TEXT, approved_at TEXT, approval_text TEXT, done_at TEXT, result TEXT)""",
    """CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, chat TEXT, kind TEXT, summary TEXT,
        detail TEXT, booking_id INTEGER, at TEXT)""",
]

S2_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS messages (id BIGINT AUTO_INCREMENT PRIMARY KEY, chat VARCHAR(64),
        role VARCHAR(16), body TEXT, at VARCHAR(40))""",
    "CREATE TABLE IF NOT EXISTS seen (msg_id VARCHAR(100) PRIMARY KEY, at VARCHAR(40))",
    "CREATE ROWSTORE REFERENCE TABLE IF NOT EXISTS kv (k VARCHAR(200) PRIMARY KEY, v LONGTEXT, at VARCHAR(40))",
    """CREATE ROWSTORE REFERENCE TABLE IF NOT EXISTS rules (id BIGINT AUTO_INCREMENT PRIMARY KEY, chat VARCHAR(64),
        kind VARCHAR(32), body TEXT, at VARCHAR(40))""",
    # Every event Plus One has shown or read, searchable by meaning-bearing words.
    """CREATE TABLE IF NOT EXISTS events (id VARCHAR(160) PRIMARY KEY, chat VARCHAR(64), source VARCHAR(32),
        title TEXT, description TEXT, host TEXT, starts_at VARCHAR(40), price VARCHAR(32), url TEXT,
        data LONGTEXT, seen_at VARCHAR(40), FULLTEXT (title, description, host))""",
    """CREATE ROWSTORE REFERENCE TABLE IF NOT EXISTS bookings (id BIGINT AUTO_INCREMENT PRIMARY KEY, chat VARCHAR(64),
        status VARCHAR(16), event_json LONGTEXT, reason TEXT, proposed_at VARCHAR(40), approved_at VARCHAR(40),
        approval_text TEXT, done_at VARCHAR(40), result LONGTEXT)""",
    """CREATE TABLE IF NOT EXISTS audit (id BIGINT AUTO_INCREMENT PRIMARY KEY, chat VARCHAR(64), kind VARCHAR(32),
        summary TEXT, detail LONGTEXT, booking_id BIGINT, at VARCHAR(40))""",
]


def init() -> None:
    if db.kind == "singlestore":
        try:
            db.run("SELECT 1", fetch=True)
        except Exception:
            pass  # run() has switched to SQLite if SingleStore is unreachable
    for stmt in (S2_SCHEMA if db.kind == "singlestore" else SQLITE_SCHEMA):
        _x(stmt)


# --------------------------------------------------------------------------- chat history
def add_message(chat: str, role: str, text: str) -> None:
    _x("INSERT INTO messages(chat, role, body, at) VALUES (?,?,?,?)", chat, role, text, now_iso())


def history(chat: str, limit: int = 16) -> list[dict[str, str]]:
    rows = _q("SELECT role, body FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?", chat, limit)
    return [{"role": r["role"], "content": r["body"]} for r in reversed(rows)]


def mark_seen(msg_id: str) -> bool:
    """Return True if this message id is new (and record it)."""
    ignore = "INSERT IGNORE" if db.kind == "singlestore" else "INSERT OR IGNORE"
    _, rowcount = _x(f"{ignore} INTO seen(msg_id, at) VALUES (?,?)", msg_id, now_iso())
    return rowcount > 0


# --------------------------------------------------------------------------- small state (location, conversation ids)
def set_state(key: str, value: Any) -> None:
    v = json.dumps(value, default=str)
    if db.kind == "singlestore":
        _x("INSERT INTO kv(k, v, at) VALUES (?,?,?) ON DUPLICATE KEY UPDATE v=VALUES(v), at=VALUES(at)", key, v, now_iso())
    else:
        _x("INSERT INTO kv(k, v, at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, at=excluded.at", key, v, now_iso())


def get_state(key: str, default: Any = None) -> Any:
    rows = _q("SELECT v FROM kv WHERE k=?", key)
    return json.loads(rows[0]["v"]) if rows else default


# --------------------------------------------------------------------------- rules
def add_rule(chat: str, kind: str, text: str) -> int:
    return _x("INSERT INTO rules(chat, kind, body, at) VALUES (?,?,?,?)", chat, kind, text, now_iso())[0]


def list_rules(chat: str) -> list[dict[str, Any]]:
    return [{"id": r["id"], "kind": r["kind"], "text": r["body"], "at": r["at"]}
            for r in _q("SELECT id, kind, body, at FROM rules WHERE chat=? ORDER BY id", chat)]


def delete_rule(chat: str, rule_id: int) -> bool:
    return _x("DELETE FROM rules WHERE chat=? AND id=?", chat, rule_id)[1] > 0


# --------------------------------------------------------------------------- events memory
def remember_events(chat: str, events: list[dict]) -> None:
    if db.kind == "singlestore":
        upsert = ("INSERT INTO events(id, chat, source, title, description, host, starts_at, price, url, data, seen_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE seen_at=VALUES(seen_at), data=VALUES(data)")
    else:
        upsert = ("INSERT INTO events(id, chat, source, title, description, host, starts_at, price, url, data, seen_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET seen_at=excluded.seen_at, data=excluded.data")
    for e in events:
        _x(upsert, e["id"], chat, e.get("source"), e.get("title"), e.get("description") or "", e.get("host") or "",
           e.get("start"), e.get("price"), e.get("url"), json.dumps(e, default=str), now_iso())


def get_event(event_id: str) -> dict | None:
    rows = _q("SELECT data FROM events WHERE id=?", event_id)
    return json.loads(rows[0]["data"]) if rows else None


def recall_events(chat: str, query: str, limit: int = 5) -> list[dict]:
    """Find events Plus One already showed this user, by words in title, description or host."""
    like = f"%{query.lower()}%"
    if db.kind == "singlestore":
        try:
            rows = _q(
                "SELECT data, MATCH(title, description, host) AGAINST (?) AS score FROM events "
                "WHERE chat=? AND (MATCH(title, description, host) AGAINST (?) OR LOWER(title) LIKE ? OR LOWER(host) LIKE ?) "
                "ORDER BY score DESC, seen_at DESC LIMIT ?", query, chat, query, like, like, limit)
        except Exception:
            rows = []
        if rows:
            return [json.loads(r["data"]) for r in rows]
    words = [w for w in query.split() if len(w) > 2] or [query]
    clause = " OR ".join(["LOWER(title) LIKE ? OR LOWER(description) LIKE ? OR LOWER(host) LIKE ?"] * len(words))
    params: list[Any] = [chat]
    for w in words:
        params += [f"%{w.lower()}%"] * 3
    rows = _q(f"SELECT data FROM events WHERE chat=? AND ({clause}) ORDER BY seen_at DESC LIMIT ?", *params, limit)
    return [json.loads(r["data"]) for r in rows]


# --------------------------------------------------------------------------- bookings
def propose_booking(chat: str, event: dict, reason: str) -> int:
    # Only one proposal waits at a time: older pending ones expire.
    _x("UPDATE bookings SET status='expired' WHERE chat=? AND status='pending'", chat)
    return _x("INSERT INTO bookings(chat, status, event_json, reason, proposed_at) VALUES (?,?,?,?,?)",
              chat, "pending", json.dumps(event, default=str), reason, now_iso())[0]


def get_booking(booking_id: int) -> dict | None:
    rows = _q("SELECT * FROM bookings WHERE id=?", booking_id)
    if not rows:
        return None
    d = rows[0]
    d["event"] = json.loads(d.pop("event_json"))
    return d


def latest_pending(chat: str) -> dict | None:
    rows = _q("SELECT id FROM bookings WHERE chat=? AND status='pending' ORDER BY id DESC LIMIT 1", chat)
    return get_booking(rows[0]["id"]) if rows else None


def approve_booking(booking_id: int, approval_text: str) -> None:
    _x("UPDATE bookings SET status='approved', approved_at=?, approval_text=? WHERE id=?",
       now_iso(), approval_text, booking_id)


def decline_booking(booking_id: int) -> None:
    _x("UPDATE bookings SET status='declined' WHERE id=?", booking_id)


def finish_booking(booking_id: int, result: dict) -> None:
    _x("UPDATE bookings SET status='done', done_at=?, result=? WHERE id=?",
       now_iso(), json.dumps(result, default=str), booking_id)


def list_bookings(chat: str, limit: int = 10) -> list[dict]:
    return [get_booking(r["id"]) for r in _q("SELECT id FROM bookings WHERE chat=? ORDER BY id DESC LIMIT ?", chat, limit)]


# --------------------------------------------------------------------------- audit trail
def audit(chat: str, kind: str, summary: str, detail: Any = None, booking_id: int | None = None) -> None:
    _x("INSERT INTO audit(chat, kind, summary, detail, booking_id, at) VALUES (?,?,?,?,?,?)",
       chat, kind, summary[:500], json.dumps(detail, default=str)[:4000] if detail is not None else None,
       booking_id, now_iso())


def audit_trail(chat: str, limit: int = 25, booking_id: int | None = None) -> list[dict]:
    if booking_id is not None:
        b = get_booking(booking_id)
        proposed = datetime.fromisoformat(b["proposed_at"]) if b else datetime.now(timezone.utc)
        # Everything from the search that led to the proposal through to execution.
        start = (proposed - timedelta(minutes=15)).isoformat(timespec="seconds")
        end = (b and b.get("done_at")) or now_iso()
        rows = _q("SELECT kind, summary, at, booking_id FROM audit WHERE chat=? AND "
                  "(booking_id=? OR (at >= ? AND at <= ?)) ORDER BY id DESC LIMIT ?",
                  chat, booking_id, start, end, limit)
    else:
        rows = _q("SELECT kind, summary, at, booking_id FROM audit WHERE chat=? ORDER BY id DESC LIMIT ?", chat, limit)
    return list(reversed(rows))
