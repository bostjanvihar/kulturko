"""Parse Slovenian (and generic) date strings into datetimes.

Handles formats seen on Maribor venue sites:
  "sobota, 10. oktober 2026", "10. 10. 2026 ob 20.00", "10.10.2026 20:00",
  "2026-10-10T20:00:00+02:00", "10. okt 2026", "petek, 6. november ob 19h",
  "7. 7. 26" (two-digit year), "11.09" (day.month, year from context)
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional

MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "marec": 3, "mar": 3,
    "april": 4, "apr": 4, "maj": 5, "junij": 6, "jun": 6, "julij": 7,
    "jul": 7, "avgust": 8, "avg": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "oktober": 10, "okt": 10, "oct": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12,
    # genitive forms used in running text ("10. oktobra")
    "januarja": 1, "februarja": 2, "marca": 3, "aprila": 4, "maja": 5,
    "junija": 6, "julija": 7, "avgusta": 8, "septembra": 9, "oktobra": 10,
    "novembra": 11, "decembra": 12,
    # English, since some sites have EN pages
    "january": 1, "february": 2, "march": 3, "june": 6, "july": 7,
    "august": 8, "october": 10,
}

_TIME_RE = re.compile(r"(?:ob\s*)?(\d{1,2})[.:](\d{2})|(?:ob\s*)(\d{1,2})\s*h", re.I)
_NUMERIC_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2,4})")
_NUMERIC_NOYEAR_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\b\.?(?!\s*\d)")
_TEXT_RE = re.compile(r"(\d{1,2})\.\s*([a-zčšž]+)\s*(\d{4})?", re.I)


def _find_time(text: str):
    m = _TIME_RE.search(text)
    if not m:
        return None
    if m.group(1):
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        h, mi = int(m.group(3)), 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def _next_occurrence(mo: int, d: int, t) -> Optional[datetime]:
    """Date without a year: assume the next time this day/month comes up."""
    now = datetime.now()
    for y in (now.year, now.year + 1):
        try:
            cand = datetime(y, mo, d, *t)
        except ValueError:
            continue
        if cand >= now.replace(hour=0, minute=0):
            return cand
    return None


def parse_sl_datetime(text: str, default_year: Optional[int] = None,
                      default_time=(19, 0)) -> Optional[datetime]:
    """Best-effort parse. Returns naive local datetime or None."""
    if not text:
        return None
    text = " ".join(text.split())

    # ISO first
    iso = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", text)
    if iso:
        try:
            dt = datetime.fromisoformat(iso.group(0).replace(" ", "T"))
            return dt.replace(tzinfo=None)
        except ValueError:
            pass

    m = _NUMERIC_RE.search(text)          # 10. 10. 2026 / 7. 7. 26
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d <= 31:
            rest = text[:m.start()] + " " + text[m.end():]
            t = _find_time(rest) or default_time
            try:
                return datetime(y, mo, d, *t)
            except ValueError:
                return None

    t = _find_time(text) or default_time

    m = _TEXT_RE.search(text.lower())     # 10. oktober 2026
    if m:
        d = int(m.group(1))
        mo = MONTHS.get(m.group(2))
        y = int(m.group(3)) if m.group(3) else default_year
        if mo and y:
            try:
                return datetime(y, mo, d, *t)
            except ValueError:
                return None
        if mo and default_year is None:
            cand = _next_occurrence(mo, d, t)
            if cand:
                return cand

    m = _NUMERIC_NOYEAR_RE.search(text)   # "11.09" / "8. 11." (no year)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        # skip obvious times like "ob 20.00"
        preceded_by_ob = re.search(r"\bob\s*$", text[:m.start()], re.I)
        if 1 <= mo <= 12 and 1 <= d <= 31 and not preceded_by_ob:
            # the matched text is the date — look for a time elsewhere
            rest = text[:m.start()] + " " + text[m.end():]
            t = _find_time(rest) or default_time
            if default_year:
                try:
                    return datetime(default_year, mo, d, *t)
                except ValueError:
                    return None
            return _next_occurrence(mo, d, t)
    return None
