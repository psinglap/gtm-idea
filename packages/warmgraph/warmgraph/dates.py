"""Date parsing + freshness filter for signals (enforce the ≤3-month window strictly)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_FMTS = ["%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
         "%B %Y", "%b %Y", "%Y-%m", "%Y"]


_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$")


def parse_datetime(s: str) -> Optional[datetime]:
    """Full ISO-8601 timestamp -> tz-aware datetime, falling back to `parse_date` for the
    loose date formats found in scraped text.

    Needed because `parse_date` only handles date-shaped strings: given Luma's
    "2026-08-11T00:30:00.000Z" its last-resort "%Y" branch matches and silently returns
    January 1st. Every event would then look months old, which breaks the "has this ended"
    check, the `{when}` phrase in the email, and the staleness guard.
    """
    raw = (s or "").strip()
    m = _ISO.match(raw)
    if not m:
        return parse_date(raw)
    year, month, day, hour, minute, second, tz = m.groups()
    dt = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    if tz in (None, "Z", "+00:00", "+0000"):
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    body = tz[1:].replace(":", "")
    offset = timedelta(hours=int(body[:2]), minutes=int(body[2:4]))
    return dt.replace(tzinfo=timezone(sign * offset))


def parse_date(s: str) -> Optional[datetime]:
    s = (s or "").strip().rstrip(".")
    if not s:
        return None
    for f in _FMTS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{4})", s)  # "March 2026"
    if m:
        for f in ("%B %Y", "%b %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)}", f)
            except ValueError:
                continue
    m = re.search(r"(20\d{2})", s)  # bare year
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y")
        except ValueError:
            pass
    return None


def is_stale(date_str: str, days: int = 100) -> bool:
    """True only if the date PARSES and is older than `days`. Undated → not provably stale (kept;
    the LLM is told to only include recent signals)."""
    d = parse_date(date_str)
    return d is not None and d < datetime.now() - timedelta(days=days)


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def cutoff_str(days: int = 100) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
