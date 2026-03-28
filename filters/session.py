"""
filters/session.py
Trading session window filter (UTC-based).
"""
from datetime import datetime, timezone
import config


def current_session() -> str:
    """Return 'london', 'new_york', or 'off_hours'."""
    hour = datetime.now(tz=timezone.utc).hour
    for sess, (h0, h1) in config.ACTIVE_SESSIONS.items():
        if h0 <= hour < h1:
            return sess
    return "off_hours"


def is_active_session() -> bool:
    """True if we are inside a configured trading session."""
    if config.TRADE_ALL_SESSIONS:
        return True
    return current_session() != "off_hours"


def session_emoji(session: str) -> str:
    return {
        "london":   "🇬🇧",
        "new_york": "🇺🇸",
        "off_hours": "🌙",
    }.get(session, "🌍")
