import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from icalendar import Calendar

log = logging.getLogger("canvas")

# Events whose SUMMARY starts with these prefixes are timetable slots, not assignments.
# Canvas typically formats them as "Lecture Week 3 [SUBJECT]" etc.
SKIP_TITLE_PREFIXES = (
    "lecture",
    "tutorial",
    "workshop",
    "lab",
    "seminar",
    "practicum",
    "studio",
    "clinic",
    "field trip",
    "excursion",
    "office hours",
    "consultation",
    "drop-in",
    "drop in",
    "timetabled",
)

# Events whose SUMMARY contains these anywhere are also skipped.
SKIP_KEYWORDS = {
    "no submission",
    "calendar event",
}

# Canvas sets CATEGORIES on timetable events — skip any event with these categories.
SKIP_CATEGORIES = {
    "lecture",
    "tutorial",
    "workshop",
    "lab",
    "laboratory",
    "seminar",
    "practicum",
    "studio",
    "clinic",
    "timetable",
    "class",
}


def _is_timetable_event(component) -> bool:
    """Return True if this iCal component looks like a timetable slot."""
    summary = str(component.get("SUMMARY", "")).strip().lower()

    # Check CATEGORIES field (most reliable for Canvas)
    raw_cats = component.get("CATEGORIES")
    if raw_cats is not None:
        # icalendar can return a vCategory object, a list, or a string
        if hasattr(raw_cats, "cats"):
            cats = [str(c).lower() for c in raw_cats.cats]
        elif isinstance(raw_cats, (list, tuple)):
            cats = [str(c).lower() for c in raw_cats]
        else:
            cats = [str(raw_cats).lower()]

        if any(c in SKIP_CATEGORIES for c in cats):
            log.debug(f"Skipping by category: {summary!r} → {cats}")
            return True

    # Check title prefix
    if any(summary.startswith(prefix) for prefix in SKIP_TITLE_PREFIXES):
        log.debug(f"Skipping by title prefix: {summary!r}")
        return True

    # Check keyword anywhere in title
    if any(kw in summary for kw in SKIP_KEYWORDS):
        log.debug(f"Skipping by keyword: {summary!r}")
        return True

    # Detect bare course-name events — Canvas timetable slots often have titles like:
    #   "Methods of Mathematical Statistics (MAST90105_2026_SM1) [MAST90105_2026_SM1]"
    # i.e. the text before the bracket is just the subject's full name with no
    # assignment descriptor. Real assignments look like:
    #   "Assignment 1 [MAST90105_2026_SM1]"
    #   "A. Group topic submission [COMP90050_2026_SM1]"
    # Pattern: title ends with [CODE_YEAR_SM?] and the part before the bracket
    # also ends with (CODE_YEAR_SM?) — meaning the bracket is just a repeat of
    # the inline subject code, with nothing descriptive added.
    bare_course = re.search(
        r'\(([A-Z]{2,}\d+_\d{4}_SM\d)\)\s*\[([A-Z]{2,}\d+_\d{4}_SM\d)\]\s*$',
        str(component.get("SUMMARY", "")),
    )
    if bare_course:
        log.debug(f"Skipping bare course-name event: {summary!r}")
        return True

    return False


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

            if _is_timetable_event(component):
                continue

            summary = str(component.get("SUMMARY", "Untitled"))

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