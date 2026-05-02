"""
downloader/economic_calendar.py
================================

Fetches high-impact economic events from the Trading Economics (TE) API
and returns them as a list of dicts ready for options_events_calendar.

Free tier signup (no credit card): https://tradingeconomics.com/api/
Set the key via env var: OPT_TE_API_KEY

API endpoint used:
  GET https://api.tradingeconomics.com/calendar/country/{country}/{start}/{end}?c={key}

The job fetches events for the next `LOOK_AHEAD_DAYS` days for each
country in `FETCH_COUNTRIES`, filters to HIGH importance (Importance == 3),
maps TE's event names to our event_type codes, and returns rows.

The caller (events_seeder) is responsible for dedup + DB insert.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TE_BASE = "https://api.tradingeconomics.com/calendar/country"

# Countries to pull events for (TE country name, lowercase)
_FETCH_COUNTRIES = ["india", "united states"]

# How many days ahead to fetch on each run (weekly job → 60 days gives overlap)
_LOOK_AHEAD_DAYS = 60

# TE Importance == 3  →  HIGH
_HIGH_IMPORTANCE = 3

# Keyword → event_type mapping (lower-case TE event name substring → our code)
# Ordered from most-specific to least-specific.
_EVENT_TYPE_MAP: list[tuple[str, str]] = [
    ("fed interest rate",      "US_FOMC"),
    ("interest rate decision", "US_FOMC"),      # TE sometimes uses this for Fed
    ("rbi interest rate",      "RBI_MPC"),
    ("rbi monetary policy",    "RBI_MPC"),
    ("interest rate",          "RBI_MPC"),      # fallback for India interest rate
    ("union budget",           "UNION_BUDGET"),
    ("federal budget",         "UNION_BUDGET"),
    ("gdp growth rate",        "GDP_RELEASE"),
    ("gdp",                    "GDP_RELEASE"),
    ("cpi",                    "CPI_RELEASE"),
    ("inflation rate",         "CPI_RELEASE"),
]

_COUNTRY_INTEREST_RATE_TYPE = {
    "india":         "RBI_MPC",
    "united states": "US_FOMC",
}


def _classify_event(event_name: str, country: str) -> str:
    """Return our event_type code for a TE event name + country."""
    name_lower = event_name.lower()
    # Special-case: pure "Interest Rate Decision" depends on country
    if "interest rate" in name_lower:
        return _COUNTRY_INTEREST_RATE_TYPE.get(country.lower(), "MACRO_EVENT")
    for keyword, etype in _EVENT_TYPE_MAP:
        if keyword in name_lower:
            return etype
    return "MACRO_EVENT"


def fetch_high_impact_events(
    api_key: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[dict]:
    """Fetch HIGH-impact events from Trading Economics API.

    Returns a list of dicts with keys:
        date       (str YYYY-MM-DD)
        event_type (str)
        description (str)
        impact     ("HIGH")

    Never raises — logs warnings on failure and returns an empty list.
    """
    if not api_key:
        logger.warning("economic_calendar: OPT_TE_API_KEY not set, skipping API fetch")
        return []

    start = start or date.today()
    end   = end   or (start + timedelta(days=_LOOK_AHEAD_DAYS))
    start_str = start.strftime("%Y-%m-%d")
    end_str   = end.strftime("%Y-%m-%d")

    results: list[dict] = []

    for country in _FETCH_COUNTRIES:
        url = f"{_TE_BASE}/{country}/{start_str}/{end_str}?c={api_key}"
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            logger.warning("economic_calendar: failed to fetch %s events: %s", country, exc)
            continue

        if not isinstance(events, list):
            logger.warning("economic_calendar: unexpected response for %s: %r", country, events)
            continue

        for ev in events:
            # Only HIGH importance (3 stars on TE)
            if ev.get("Importance") != _HIGH_IMPORTANCE:
                continue

            raw_date = ev.get("Date", "")
            event_name = ev.get("Event", "Unknown event")
            # TE Date is ISO-like: "2026-06-17T00:00:00"
            event_date = raw_date[:10] if raw_date else None
            if not event_date:
                continue

            etype = _classify_event(event_name, country)
            country_label = "India" if country == "india" else "US"
            description = f"{event_name} ({country_label})"

            results.append({
                "date":        event_date,
                "event_type":  etype,
                "description": description,
                "impact":      "HIGH",
            })
            logger.debug("economic_calendar: found %s %s %s", event_date, etype, description)

    logger.info("economic_calendar: fetched %d HIGH-impact events (%s → %s)",
                len(results), start_str, end_str)
    return results
