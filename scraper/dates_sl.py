"""Parse Slovenian (and generic) date strings into datetimes.

Handles formats seen on Maribor venue sites:
  "sobota, 10. oktober 2026", "10. 10. 2026 ob 20.00", "10.10.2026 20:00",
  "2026-10-10T20:00:00+02:00", "10. okt 2026", "petek, 6. november ob 19h",
  "7. 7. 26" (two-digit year), "11.09" (day.month, year from context)
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta
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

# "ob 20:00", "ob 20.00", "ob 21 :00" (span-split), "ob 21h", "ob 21"
_TIME_OB_RE = re.compile(
    r"\bob\s*(\d{1,2})(?:\s*[.:]\s*(\d{2}))?\s*(?:h|ura|uri)?\b", re.I)
# a bare clock time; digit guards stop it matching inside a date like
# "18.3.2023" (which would otherwise read as 3:20)
_TIME_BARE_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_NUMERIC_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2,4})")
_NUMERIC_NOYEAR_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\b\.?(?!\s*\d)")
# dot after the day is optional: "10. oktober 2026" and "19 september 2026"
_TEXT_RE = re.compile(r"(\d{1,2})\.?\s*([a-zčšž]+)\s*(\d{4})?", re.I)


def _valid(h, mi):
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def _find_time(text: str):
    # Prefer an "ob HH[:MM]" phrase — it's how Slovenian sites state the
    # start time and is safe from being confused with a date.
    m = _TIME_OB_RE.search(text)
    if m:
        v = _valid(int(m.group(1)), int(m.group(2) or 0))
        if v:
            return v
    m = _TIME_BARE_RE.search(text)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)))
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
    return _parse_noyear(m, text, default_year, default_time)


def _parse_noyear(m, text, default_year, default_time):
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


def find_future_date(text: str,
                     default_time=(19, 0)) -> Optional[datetime]:
    """Scan free text (e.g. an event detail page) for every date mention
    and return the earliest one that is not in the past. Used when a
    listing page has no dates and we must read the article body."""
    if not text:
        return None
    text = " ".join(text.split())
    now = datetime.now()
    cands = []
    for m in _NUMERIC_RE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        t = _find_time(text[m.end():m.end() + 40]) or default_time
        try:
            cands.append(datetime(y, mo, d, *t))
        except ValueError:
            pass
    for m in _TEXT_RE.finditer(text.lower()):
        d, mo = int(m.group(1)), MONTHS.get(m.group(2))
        if not (mo and 1 <= d <= 31):
            continue
        t = _find_time(text[m.end():m.end() + 40]) or default_time
        if m.group(3):
            try:
                cands.append(datetime(int(m.group(3)), mo, d, *t))
            except ValueError:
                pass
        else:
            cand = _next_occurrence(mo, d, t)
            if cand:
                cands.append(cand)
    future = [c for c in cands if c >= now - timedelta(days=1)]
    return min(future) if future else None
