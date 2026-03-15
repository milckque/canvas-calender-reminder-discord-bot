import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from icalendar import Calendar

log = logging.getLogger("canvas")

# Assignment types we care about (Canvas uses these in SUMMARY/DESCRIPTION)
SKIP_KEYWORDS = {"no submission", "calendar event"}


class CanvasCalendar:
    def __init__(self, timeout: int = 15):
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def fetch_assignments(self, ical_url: str) -> Optional[list[dict]]:
        """
        Fetch and parse a Canvas iCal feed.

        Returns a list of dicts:
            {
                "uid":   str,   # unique identifier for dedup
                "title": str,
                "due":   datetime (UTC, timezone-aware),
                "url":   str | None,
            }
        Returns None on network/parse error.
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(ical_url) as resp:
                    if resp.status != 200:
                        log.warning(f"iCal fetch returned HTTP {resp.status} for URL: {ical_url[:60]}…")
                        return None
                    raw = await resp.read()
        except asyncio.TimeoutError:
            log.warning("Timeout fetching iCal URL")
            return None
        except aiohttp.ClientError as e:
            log.warning(f"Network error fetching iCal: {e}")
            return None

        try:
            return self._parse(raw)
        except Exception as e:
            log.error(f"Failed to parse iCal data: {e}", exc_info=True)
            return None

    # ── Private ────────────────────────────────────────────────────────────────

    def _parse(self, raw: bytes) -> list[dict]:
        cal = Calendar.from_ical(raw)
        assignments = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            summary = str(component.get("SUMMARY", "Untitled"))

            # Skip non-assignment events Canvas sometimes includes
            if any(kw in summary.lower() for kw in SKIP_KEYWORDS):
                continue

            due = component.get("DTSTART") or component.get("DUE")
            if due is None:
                continue

            due_dt = self._to_utc(due.dt)
            if due_dt is None:
                continue

            uid = str(component.get("UID", f"{summary}-{due_dt.isoformat()}"))
            url = str(component.get("URL", "")) or None

            assignments.append({
                "uid":   uid,
                "title": summary,
                "due":   due_dt,
                "url":   url,
            })

        return assignments

    @staticmethod
    def _to_utc(dt) -> Optional[datetime]:
        """Normalise a date or datetime to a UTC-aware datetime."""
        if dt is None:
            return None
        # date (not datetime) → treat as midnight UTC
        if not isinstance(dt, datetime):
            return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
