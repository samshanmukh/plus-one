"""Settings loaded from .env (see .env.example)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Talk to the Anthropic API directly unless .env says otherwise (ignores any shell proxy).
ANTHROPIC_BASE_URL = os.getenv("BLUETAIL_ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "low")  # chat replies should be quick

INKBOX_API_KEY = os.getenv("INKBOX_API_KEY", "")
INKBOX_BASE = os.getenv("INKBOX_BASE", "https://inkbox.ai/api/v1/imessage")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "3"))
ALLOWED_NUMBERS = {n.strip() for n in os.getenv("ALLOWED_NUMBERS", "").split(",") if n.strip()}

HOME_LAT = float(os.getenv("HOME_LAT", "37.7749"))
HOME_LON = float(os.getenv("HOME_LON", "-122.4194"))
TZ_NAME = os.getenv("HOME_TZ", "America/Los_Angeles")
TZ = ZoneInfo(TZ_NAME)
CALENDARS = [c.strip() for c in os.getenv("CALENDARS", "").split(",") if c.strip()]
OWNER_NAME = os.getenv("OWNER_NAME", "there")
