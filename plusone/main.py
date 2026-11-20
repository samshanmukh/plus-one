"""Run Plus One: poll Inkbox for new iMessages and reply to each one.

    python -m plusone.main
"""
from __future__ import annotations

import logging
import re
import time
import traceback

from . import brain, calendar_mac, config, inkbox, store

log = logging.getLogger("plusone")


def process(msg: dict) -> None:
    if not store.mark_seen(msg["id"]):
        return
    number = msg.get("remote_number") or ""
    conv = msg["conversation_id"]
    text = (msg.get("content") or "").strip()
    inkbox.mark_read(conv)
    if number:
        store.set_state(f"conv:{number}", conv)  # lets Plus One text first later (alerts)
    if config.ALLOWED_NUMBERS and number not in config.ALLOWED_NUMBERS:
        log.info("ignoring message from %s (not in ALLOWED_NUMBERS)", number)
        return
    media = msg.get("media") or []
    if media:
        handled = handle_attachments(number or conv, conv, media)
        if handled and not text:
            return
    if not text:
        inkbox.send(conv, "I can read text, and LinkedIn's Connections.csv or export ZIP. Send me a topic or an event link.")
        return
    log.info("<- %s: %s", number, text)
    inkbox.typing(conv)
    try:
        reply = brain.handle(chat=number or conv, text=text)
    except Exception:
        traceback.print_exc()
        reply = "Something went wrong on my side. Try again in a moment."
    text, cards = _split_cards(reply)
    for chunk in _chunks(text):
        inkbox.send(conv, chunk)
    for url in cards:  # a bare link on its own renders as a rich event card in iMessage
        inkbox.send(conv, url)
    from . import tools
    for path in tools.outbox_media.pop(number or conv, []):
        try:
            inkbox.send(conv, None, media_url=inkbox.upload_media(path))
        except Exception as exc:
            log.warning("couldn't send image %s: %s", path, exc)
    log.info("-> %s: %s", number, reply)


def handle_attachments(chat: str, conv: str, media: list[dict]) -> bool:
    """Load a LinkedIn connections export sent as an iMessage attachment."""
    import httpx
    from . import linkedin
    for m in media:
        ctype = (m.get("content_type") or "").lower()
        url = m.get("url") or ""
        if not url or not any(k in ctype for k in ("csv", "zip", "text", "octet-stream", "excel")):
            continue
        try:
            data = httpx.get(url, timeout=60, follow_redirects=True).content
            people = linkedin.parse_export(data, url.split("?")[0])
        except Exception as exc:
            inkbox.send(conv, f"I couldn't read that file as a LinkedIn export ({str(exc)[:120]}).")
            return True
        n = linkedin.save_connections(chat, people)
        store.audit(chat, "linkedin", f"Connected LinkedIn: loaded {n} connections from export")
        inkbox.send(conv, f"LinkedIn connected: {n} connections loaded. I'll keep an eye on your upcoming events "
                          "and tell you when people you know are going.")
        log.info("loaded %s LinkedIn connections for %s", n, chat)
        try:
            check_people_at_events(force=True)
        except Exception as exc:
            log.warning("people check failed: %s", exc)
        return True
    return False


def nudge_missing_sources() -> None:
    """Once per platform: if the user's calendar shows events from a platform that isn't connected, ask them to connect it."""
    from . import tools
    for number in config.ALLOWED_NUMBERS:
        conv = store.get_state(f"conv:{number}")
        if not conv:
            continue
        for src in tools.connected_sources(number)["sources"]:
            key = f"nudged:{number}:{src['source']}"
            if src["source"] == "partiful" and src["events_seen"] and src["status"] == "not connected" and not store.get_state(key):
                msg = (f"I see {src['events_seen']} Partiful event(s) on your calendar. Partiful doesn't let apps sign in, "
                       "so to connect it just forward me your Partiful invite links (the texts Partiful sends). "
                       "I'll track them for clashes, leave-now alerts and reminders.")
                inkbox.send(conv, msg)
                store.set_state(key, True)
                store.add_message(number, "assistant", msg)
                store.audit(number, "nudge", "Asked to connect Partiful (events seen on calendar)")


PEOPLE_EVERY = 3600  # re-check who's going to your events hourly


def check_people_at_events(force: bool = False) -> None:
    """Text the user when their LinkedIn connections host or attend events they're going to."""
    from datetime import datetime, timedelta
    from . import linkedin, tools
    now = datetime.now(config.TZ)
    for number in config.ALLOWED_NUMBERS:
        conv = store.get_state(f"conv:{number}")
        if not conv or not any(linkedin.connections(number)):
            continue
        events = tools.my_events(number, now.isoformat(), (now + timedelta(days=7)).isoformat())["events"]
        for e in events:
            if e["source"] != "luma" or not e.get("link"):
                continue
            found = tools.who_is_going(number, e["link"]).get("connections_going") or []
            new = [p for p in found if not store.get_state(f"people:{number}:{e['start']}:{p['name']}")]
            if not new:
                continue
            names = "; ".join(f"{p['name']}{' (' + p['position'] + ' at ' + p['company'] + ')' if p.get('company') else ''}"
                              f"{', hosting' if p.get('role_at_event') == 'host' else ''}" for p in new[:4])
            msg = f"People you know at {e['title']} ({e['when']}): {names}."
            if len(new) > 4:
                msg += f" +{len(new) - 4} more."
            inkbox.send(conv, msg)
            for p in new:
                store.set_state(f"people:{number}:{e['start']}:{p['name']}", True)
            store.add_message(number, "assistant", msg)
            store.audit(number, "people_alert", f"Connections at {e['title'][:50]}: {len(new)}")
            log.info("-> %s (people): %s", number, msg)


ALERT_EVERY = 120  # seconds between leave-now checks
BUFFER_MIN = 10    # arrive this many minutes early


def check_leave_alerts() -> None:
    """Text the user when it's time to leave for an upcoming event on their calendar."""
    from datetime import datetime, timedelta
    from . import tools, travel
    now = datetime.now(config.TZ)
    owners = config.ALLOWED_NUMBERS or set()
    for number in owners:
        conv = store.get_state(f"conv:{number}")
        if not conv:
            continue
        events = tools.my_events(number, now.isoformat(), (now + timedelta(hours=5)).isoformat())["events"]
        for e in events:
            start = datetime.fromisoformat(e["start"])
            if e["all_day"] or not e["where"] or start <= now:
                continue
            key = f"alert:{number}:{e['title'][:60]}:{e['start']}"
            if store.get_state(key):
                continue
            lat, lon, origin_name = tools._origin(number)
            dest = travel.geocode(e["where"])
            res = travel.drive_minutes((lat, lon), dest[:2]) if dest else None
            minutes = res[0] if res else 30
            leave_at = start - timedelta(minutes=minutes + BUFFER_MIN)
            if now < leave_at - timedelta(minutes=10):
                continue  # not yet: alert ~10 min before it's time to go
            late = now > leave_at
            msg = (f"{'Leave now, you are cutting it close' if late else 'Time to get ready'}: {e['title']} "
                   f"starts {start.strftime('%-I:%M %p')} at {e['where'].split(',')[0]}. "
                   f"About {minutes} min by car from {origin_name}"
                   f"{'' if late else ', so leave by ' + leave_at.strftime('%-I:%M %p')}.")
            if e.get("link") and e["source"] == "luma":
                msg += f"\nYour ticket (QR): {e['link']}"
            inkbox.send(conv, msg)
            store.set_state(key, {"sent": now.isoformat(), "minutes": minutes})
            store.add_message(number, "assistant", msg)
            store.audit(number, "alert", f"Leave-now alert for {e['title'][:60]} ({minutes} min drive)",
                        {"event": e, "from": origin_name, "leave_at": leave_at.isoformat()})
            log.info("-> %s (alert): %s", number, msg)


HOST_MAIL = re.compile(r"lu\.ma|luma|partiful|meetup|eventbrite", re.I)
MAIL_EVERY = 60


def relay_host_mail() -> None:
    """Forward emails from event hosts (Luma, Partiful, Meetup...) that reach Plus One's inbox to iMessage."""
    first_run = not store.get_state("mail_initialized")
    for m in reversed(inkbox.recent_mail()):
        if not store.mark_seen(f"mail:{m['id']}") or first_run:
            continue  # on first run, just remember what's already there
        head = " ".join(str(m.get(k) or "") for k in ("from_address", "from_display_name", "subject", "snippet"))
        full = inkbox.mail_body(m["id"])
        body = (full.get("body_text") or re.sub(r"<[^>]+>", " ", full.get("body_html") or ""))[:4000]
        if not HOST_MAIL.search(head + " " + body[:1500]):
            continue
        summary = brain.summarize_host_mail(m.get("subject") or "", m.get("from_display_name") or m.get("from_address") or "", body)
        for number in config.ALLOWED_NUMBERS:
            conv = store.get_state(f"conv:{number}")
            if conv:
                inkbox.send(conv, summary)
                store.add_message(number, "assistant", summary)
                store.audit(number, "host_message", f"Relayed host email: {(m.get('subject') or '')[:80]}",
                            {"from": m.get("from_address"), "mail_id": m["id"]})
                log.info("-> %s (host mail): %s", number, summary)
    store.set_state("mail_initialized", True)


def seed_conversations() -> None:
    """Remember the chat with each allowed number so Plus One can text first (alerts, relays)."""
    for msg in inkbox.recent_imessages():
        n = msg.get("remote_number")
        if n in config.ALLOWED_NUMBERS and not store.get_state(f"conv:{n}"):
            store.set_state(f"conv:{n}", msg["conversation_id"])


EVENT_URL = re.compile(r"https?://(?:www\.)?(?:lu\.ma|luma\.com|partiful\.com|meetup\.com|eventbrite\.[a-z.]+)/\S+")


def _split_cards(reply: str, max_cards: int = 3) -> tuple[str, list[str]]:
    """Pull event links out of the text so each can be sent alone (iMessage shows a preview card)."""
    urls = []
    for u in EVENT_URL.findall(reply):
        u = u.rstrip(").,;")
        if u not in urls:
            urls.append(u)
    if not urls:
        return reply, []
    text = EVENT_URL.sub("", reply)
    text = re.sub(r"[ \t]*\b(Link|RSVP|Tickets?|Details):[ \t]*$", "", text, flags=re.M | re.I)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, urls[:max_cards]


def _chunks(text: str, size: int = 1800) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > size:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    return parts + ([cur] if cur else [])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if not config.INKBOX_API_KEY or not config.ANTHROPIC_API_KEY:
        raise SystemExit("Set INKBOX_API_KEY and ANTHROPIC_API_KEY in .env")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store.init()
    try:
        calendar_mac.warm()  # pre-load two weeks so replies don't wait on Calendar
    except Exception as exc:
        log.warning("calendar warm-up failed: %s", exc)
    log.info("Plus One is listening on iMessage (polling every %ss)", config.POLL_SECONDS)
    from . import taste
    for number in config.ALLOWED_NUMBERS:
        taste.refresh_in_background(number)  # learn taste from calendar history (slow, runs in background)
    try:
        seed_conversations()
    except Exception as exc:
        log.warning("couldn't seed conversations: %s", exc)
    last_alert_check = 0.0
    last_mail_check = 0.0
    last_people_check = 0.0
    while True:
        try:
            for msg in inkbox.unread_inbound():
                process(msg)
        except Exception as exc:
            log.warning("poll error: %s", exc)
        if time.time() - last_alert_check > ALERT_EVERY:
            last_alert_check = time.time()
            try:
                check_leave_alerts()
            except Exception as exc:
                log.warning("alert check error: %s", exc)
        if time.time() - last_people_check > PEOPLE_EVERY:
            last_people_check = time.time()
            try:
                check_people_at_events()
                nudge_missing_sources()
            except Exception as exc:
                log.warning("people check error: %s", exc)
        if time.time() - last_mail_check > MAIL_EVERY:
            last_mail_check = time.time()
            try:
                relay_host_mail()
            except Exception as exc:
                log.warning("mail relay error: %s", exc)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
