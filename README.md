# Plus One: your events agent, in iMessage

https://github.com/user-attachments/assets/35fc064d-2cca-4c0f-b415-5dfa1bfec26b

▶️ 55-second promo (turn sound on)

No app, no website. You text Plus One like a friend, and it:

- **Suggests events you'd actually go to**: learns your taste from your calendar history (topics, venues), your stated interests and budget, skips clashes, and boosts events your LinkedIn connections attend. Each pick says why.
- **Shows each pick as a tappable event card** (bare links render as rich previews in iMessage)
- **Finds events** on **Luma** and **Meetup** by topic, or reads any **Luma / Meetup / Partiful** link you send
- **Books them for you** (RSVP for free events, checkout link for paid ones), **only after you reply YES**
- **Knows what you're already going to**: your RSVPs from your calendar, with double-bookings flagged
- **Texts you your check-in QR**: "my ticket for HackwithBay" → the Luma check-in QR as an image, built from the guest key in your calendar invite
- **Tells you when to leave**: drive time from where you are, texted before it's time to go
- **Shows you their faces**: sends the Luma profile photos of connections hosting or going
- **Connects your LinkedIn over iMessage**: text "connect my LinkedIn", export your connections from LinkedIn, and send the file to Plus One. It stores them privately per user and then **monitors your upcoming events**, texting you when people you know are hosting or going
- **Connects your sources as it finds them**: sees which platforms your events come from; Luma connects automatically from your calendar invites, and for Partiful (no app sign-in) it asks you to forward your invite links and then tracks those events for clashes and leave-now alerts
- **Relays host messages**: emails from Luma, Partiful or Meetup hosts arrive as a short text
- **Explains itself**: every search, rule, approval and booking is in an **audit trail**; text "why?"

Built for the Tokens& Personal Agents Hackathon (challenge: *a shopping agent with an audit trail*), San Francisco, Nov 20 2026.

## What it looks like

A real exchange from testing:

```
you:       anything good on robotics or AI agents this weekend? keep it under 30 bucks
plus one:  Your weekend is wide open. Friday night you've already got two 6:30 events,
           so I skipped tonight. Everything good I found is free:

           1. Embodied AI Hackathon with X Square Robot: Sat 10 AM, runs through Sun 6 PM,
              Clarendon Heights SF. Free. Hands-on robotics plus AI, my top pick if you
              want to build something.
           2. Founder Pitch Day #5, Robotics & Physical AI: Sat 3 to 6:30 PM, Stanford.
              Free. Startups pitching, good if you'd rather watch and meet founders.
           3. SF AI Code & Coffee: Sun 2 to 5 PM, Salesforce Park. Free. Casual coworking
              with AI builders.

           The first two need the host's approval, so booking one sends a request.
           Want me to book one? And should I make under $30 a standing rule?
```

Then: "book 1" → proposal with reason, rules and calendar check → "yes" → added to your calendar + RSVP link. "why?" → the step-by-step trail.

Plus One also texts first:

```
plus one:  Time to get ready: STARTUPS MIX & PITCH starts 6:30 PM at Hacker Dojo.
           About 72 min by car from San Francisco, so leave by 5:08 PM.
```

## How it works

```
iMessage ──> Inkbox (iMessage line + email inbox) ──poll──> plusone.main
                                                              │  ├─ leave-now alerts (every 2 min)
                                                              │  └─ host-email relay (every 1 min)
                                       plusone.brain: Claude (claude-opus-5-5) tool loop
                                                              │
  suggest_for_me · search_events · get_event_from_link · recall_events · my_events · check_calendar
  propose_booking / execute_booking · who_is_going · set_location / travel_time
  save_rule / list_rules · get_audit_trail
        │                 │                    │                       │
   Luma, Meetup,     macOS Calendar       LinkedIn export          OpenStreetMap
   Partiful pages    (Google/iCloud)      (Connections.csv)        + OSRM routing
                                                              │
                 SingleStore: rules, bookings, audit trail, location, and every
                 event seen (FULLTEXT-indexed for recall). SQLite fallback offline.
```

### Guardrails (the audit-trail part)

- **The model can't approve its own purchase.** `propose_booking` records a pending booking. Approval is decided in code (`brain._approval_note`) from your own short reply ("yes", "book it", 👍). `execute_booking` refuses anything not approved.
- **One proposal at a time.** A new proposal expires the old one, so "yes" always means the last thing shown.
- **No card details, ever.** Paid tickets end with the host's own checkout link.
- **Only you can talk to it.** `ALLOWED_NUMBERS` restricts who it answers.
- **Everything is logged:** searches (sources, query, filters, counts), calendar reads, rules, proposals (reason, rules applied, conflicts), approvals (your exact words and time), bookings, alerts and relayed emails.
- Event pages, emails and search results are treated as data, not instructions.

### How picks are personal

`plusone/taste.py` reads ~60 days of your calendar (in the background, cached daily) and keeps a profile of the topics and venues you actually show up for, e.g. `ai ×17, hackathon ×15, agents ×6`, `AWS Builder Loft ×4`. `suggest_for_me` pulls this week's Luma and Meetup events, scores each against that profile plus your saved interests, drops sold-out, over-budget and clashing ones, and adds +2 per LinkedIn connection attending.

### Trusted context on Senso

Your rules, taste profile and every booking receipt are written to a **Senso** knowledge base. Before proposing a booking, Plus One calls Senso's `/org/search/context` and grounds its recommendation in the returned passages ("fits your $30 cap"); the cited Senso `content_id`s are saved in the audit trail next to the proposal, so every purchase decision traces back to the context it used.

### Analytics on Amplitude

Every audit-trail event (searches, picks, proposals, approvals, bookings, alerts, teasers, LinkedIn connects) is also sent to **Amplitude** with a hashed user id and no message text, so the funnel from suggestion to approved booking is measurable.

### Memory on SingleStore

Every event Plus One shows or reads goes into a SingleStore `events` table with a `FULLTEXT` index on title, description and host. `recall_events` uses `MATCH ... AGAINST`, so "book that robotics thing from earlier" works without searching again. Rules, bookings (reference table, so ids stay #1, #2…), your location, alert history and the audit trail live there too. If SingleStore is unreachable, Plus One switches to a local SQLite file automatically and keeps answering.

## Run it

Requires macOS (for Calendar), Python 3.11+, an [Inkbox](https://inkbox.ai) identity with iMessage on (free plan works), an Anthropic API key, and optionally a SingleStore workspace.

```bash
uv venv .venv && uv pip install -p .venv -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY, INKBOX_API_KEY, CALENDARS, ALLOWED_NUMBERS, SINGLESTORE_URL
```

Try it in the terminal (same brain, no iMessage):

```bash
.venv/bin/python -m plusone.cli
```

Go live on iMessage:

```bash
.venv/bin/python -m plusone.main
```

From your phone, text `connect @<your-inkbox-agent-handle>` to the Inkbox iMessage number once, then just talk to it. The first calendar access shows a macOS permission prompt: allow it.

Optional:
- **LinkedIn:** text "connect my LinkedIn" and follow the steps: LinkedIn's export (Connections), then send the `Connections.csv` or ZIP to Plus One in iMessage. (LinkedIn's sign-in API doesn't expose connections, so the export is the supported route.)
- **Host messages:** in Gmail, forward mail from Luma/Partiful/Meetup to your Inkbox agent's email address.

## Tools used

- **Claude** (`claude-opus-5-5`, Messages API tool use, server-side refusal fallback)
- **Inkbox** for the iMessage line and the agent's email inbox
- **SingleStore** (Helios) for memory, full-text event recall and the audit trail
- **Senso** context layer for trusted context behind booking decisions
- **Amplitude** for agent analytics
- **ElevenLabs** voice, music and sound effects for the promo video (built with HyperFrames)
- **Luma** public discover + event lookup, **Meetup** GraphQL search, **Partiful** page parsing
- **macOS Calendar** via AppleScript (works with Google/iCloud calendars synced to it)
- **OpenStreetMap Nominatim** + **OSRM** for drive times
- **LinkedIn** data export for connection matching

## Limits / next steps

- RSVP and payment finish on the host's page: Luma, Meetup and Partiful have no attendee booking API.
- Luma shows only hosts and ~10 featured guests publicly, so "who you know" can miss people.
- Drive times are car estimates with padding; no transit yet.
- Partiful has no public search; send a link. Recurring calendar events aren't expanded.
