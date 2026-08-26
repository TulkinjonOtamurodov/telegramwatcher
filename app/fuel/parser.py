"""Load-confirmation detection and pickup parsing.

Two separate jobs, deliberately kept apart from the keyword engine:

* :func:`looks_like_load_confirmation` decides whether a message is a load
  confirmation at all. It scores structural markers, so ordinary conversation
  cannot trip it even if it happens to contain the word "pickup".
* :func:`parse_pickup` extracts the pickup date, time and location, resolves the
  timezone, and computes the fuel deadline.

Nothing here guesses. Every failure returns a reason code, and the caller turns
that into NEED TO CHECK rather than inventing a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.fuel.timezones import load_zone, resolve_timezone

# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
#: Structural markers of our normal load-confirmation format. Each is worth one
#: point; the threshold is what stops normal chat being mistaken for a load.
MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("load_number", re.compile(r"load\s*number\s*[:#]", re.I)),
    ("pickup_number", re.compile(r"pick\s*up\s*#", re.I)),
    ("pu_block", re.compile(r"\d+\s*#\s*PU", re.I)),
    ("del_block", re.compile(r"\d+\s*#\s*DEL", re.I)),
    ("date_field", re.compile(r"date\s*:", re.I)),
    ("time_field", re.compile(r"time\s*:", re.I)),
    ("price", re.compile(r"price\s*:", re.I)),
    ("weight", re.compile(r"weight\s*:", re.I)),
    ("miles", re.compile(r"miles\s*:", re.I)),
)

#: A message must hit at least this many distinct markers.
MIN_MARKER_SCORE = 4

#: ...and must always carry these, or it is not a load confirmation we can use.
REQUIRED_MARKERS = frozenset({"pu_block", "date_field", "time_field"})


@dataclass(frozen=True)
class Detection:
    is_load: bool
    score: int
    matched: tuple[str, ...]

    @property
    def reason(self) -> str:
        return f"score={self.score} markers={','.join(self.matched) or 'none'}"


def looks_like_load_confirmation(text: str) -> Detection:
    """Score a message against the structural markers of our load format."""
    if not text:
        return Detection(False, 0, ())

    matched = tuple(name for name, pattern in MARKERS if pattern.search(text))
    score = len(matched)
    is_load = score >= MIN_MARKER_SCORE and REQUIRED_MARKERS.issubset(set(matched))
    return Detection(is_load, score, matched)


# --------------------------------------------------------------------------- #
# pickup parsing
# --------------------------------------------------------------------------- #
#: Everything from the first pickup block up to the delivery block. Pickup and
#: delivery both carry Date/Time lines, so the section must be isolated first or
#: the delivery date would be read as the pickup date.
_PU_SECTION = re.compile(
    r"\d+\s*#\s*PU\s*[^\n]*\n(?P<body>.*?)(?=\d+\s*#\s*DEL|\Z)",
    re.I | re.S,
)

_DATE = re.compile(r"date\s*:\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", re.I)
_TIME_LINE = re.compile(r"time\s*:\s*(?P<value>[^\n]+)", re.I)

#: "08:00AM", "5 PM", "17:00", "11:30 AM"
_CLOCK = re.compile(r"(?P<hour>\d{1,2})\s*:?\s*(?P<minute>\d{2})?\s*(?P<meridiem>[AP]\.?M\.?)?", re.I)

#: "TEXARKANA, AR 71854" -- city, two-letter state, optional ZIP.
_CITY_STATE = re.compile(
    r"^\s*(?P<city>[A-Za-z][A-Za-z .'-]*?)\s*,\s*(?P<state>[A-Za-z]{2})\b\s*(?P<zip>\d{5})?",
    re.M,
)

APPOINTMENT = re.compile(r"\bAPPT\b|\bAPPOINTMENT\b", re.I)

# failure reasons
NO_PU_SECTION = "no_pickup_section"
NO_DATE = "no_pickup_date"
NO_TIME = "no_pickup_time"
BAD_DATE = "unparsable_pickup_date"
BAD_TIME = "unparsable_pickup_time"
NO_LOCATION = "no_pickup_location"
NO_TIMEZONE = "timezone_unresolved"
NO_TZDATA = "timezone_database_missing"


@dataclass(frozen=True)
class Pickup:
    """A successfully parsed pickup, with its deadline already computed."""

    date_text: str
    time_text: str
    city: str
    state: str
    timezone: str
    timezone_reason: str
    is_appointment: bool
    pickup_local: datetime
    pickup_utc: datetime
    deadline_local: datetime
    deadline_utc: datetime


@dataclass(frozen=True)
class ParseFailure:
    reason: str
    detail: str = ""


def _parse_clock(fragment: str) -> tuple[int, int] | None:
    """``08:00AM`` -> ``(8, 0)``. Returns None when it is not a clock time."""
    match = _CLOCK.search(fragment)
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").replace(".", "").upper()

    # "17:00 PM" appears in real confirmations: a 24-hour clock with a stray
    # meridiem. An hour above 12 is already 24-hour, so the meridiem is noise.
    if hour > 12:
        pass
    elif meridiem == "AM" and hour == 12:
        hour = 0
    elif meridiem == "PM" and hour != 12:
        hour += 12

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def earliest_pickup_time(time_text: str) -> tuple[int, int] | None:
    """The time the fuel deadline is measured from.

    A window uses its **earliest** time -- the safer operational choice, since
    the truck can be called at the start of it. ``APPT`` has a single time and
    uses it exactly.
    """
    if not time_text:
        return None

    # Split a window on the dash, ignoring dashes inside a ZIP-style token.
    parts = re.split(r"\s*[-–—]\s*", time_text.strip())
    candidates = [_parse_clock(part) for part in parts if part.strip()]
    found = [value for value in candidates if value is not None]

    if not found:
        return None
    # Earliest by clock position within the day.
    return min(found, key=lambda hm: hm[0] * 60 + hm[1])


def parse_pickup(
    text: str,
    *,
    deadline_hours_before: float = 3.0,
) -> Pickup | ParseFailure:
    """Extract the pickup and compute the fuel deadline, or say why not."""
    section_match = _PU_SECTION.search(text or "")
    if not section_match:
        return ParseFailure(NO_PU_SECTION)
    section = section_match.group("body")

    date_match = _DATE.search(section)
    if not date_match:
        return ParseFailure(NO_DATE)

    time_match = _TIME_LINE.search(section)
    if not time_match:
        return ParseFailure(NO_TIME)
    time_text = time_match.group("value").strip()

    month, day, year = (int(part) for part in date_match.groups())
    if year < 100:
        year += 2000
    try:
        pickup_date = datetime(year, month, day)
    except ValueError:
        return ParseFailure(BAD_DATE, date_match.group(0))

    clock = earliest_pickup_time(time_text)
    if clock is None:
        return ParseFailure(BAD_TIME, time_text)
    hour, minute = clock

    location = _CITY_STATE.search(section)
    if not location:
        return ParseFailure(NO_LOCATION)
    city = location.group("city").strip()
    state = location.group("state").strip().upper()

    zone_name, zone_reason = resolve_timezone(state, city)
    if zone_name is None:
        return ParseFailure(NO_TIMEZONE, f"{city}, {state} ({zone_reason})")

    zone = load_zone(zone_name)
    if zone is None:
        return ParseFailure(NO_TZDATA, zone_name)

    pickup_local = pickup_date.replace(hour=hour, minute=minute, tzinfo=zone)
    deadline_local = pickup_local - timedelta(hours=deadline_hours_before)

    return Pickup(
        date_text=date_match.group(0),
        time_text=time_text,
        city=city,
        state=state,
        timezone=zone_name,
        timezone_reason=zone_reason,
        is_appointment=bool(APPOINTMENT.search(time_text)),
        pickup_local=pickup_local,
        pickup_utc=pickup_local.astimezone(timezone.utc),
        deadline_local=deadline_local,
        deadline_utc=deadline_local.astimezone(timezone.utc),
    )
