"""Cross-source deduplication.

Two events are duplicates when:
  1. Exact: same normalized title + same day + same normalized venue, OR
  2. Fuzzy: same day, title similarity >= 0.85, and venues compatible
     (same normalized venue, or one venue is empty/contained in the other).

When merging, the richer record wins field-by-field and every source link
is preserved in `all_sources`.
"""
from __future__ import annotations
from difflib import SequenceMatcher

from .models import Event, norm_title, norm_venue

FUZZY = 0.85


def _venues_compatible(a: str, b: str) -> bool:
    a, b = norm_venue(a), norm_venue(b)
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _merge(keep: Event, dup: Event) -> Event:
    for f in ("description", "image", "category", "end", "url"):
        if not getattr(keep, f) and getattr(dup, f):
            setattr(keep, f, getattr(dup, f))
    if not keep.venue and dup.venue:
        keep.venue = dup.venue
    seen = {s["url"] for s in keep.all_sources}
    for s in dup.all_sources:
        if s["url"] not in seen:
            keep.all_sources.append(s)
    return keep


def dedupe(events: list[Event]) -> list[Event]:
    # Prefer records with more filled fields as the "keeper"
    def richness(e: Event):
        return sum(bool(getattr(e, f)) for f in
                   ("description", "image", "category", "end", "venue"))

    events = sorted(events, key=richness, reverse=True)

    by_exact: dict[str, Event] = {}
    by_day: dict[str, list[Event]] = {}
    out: list[Event] = []

    for e in events:
        day = (e.start or "")[:10]
        exact_key = f"{norm_title(e.title)}|{day}|{norm_venue(e.venue)}"
        if exact_key in by_exact:
            _merge(by_exact[exact_key], e)
            continue
        dup_of = None
        nt = norm_title(e.title)
        for other in by_day.get(day, []):
            if not _venues_compatible(e.venue, other.venue):
                continue
            if SequenceMatcher(None, nt, norm_title(other.title)).ratio() >= FUZZY:
                dup_of = other
                break
        if dup_of:
            _merge(dup_of, e)
        else:
            by_exact[exact_key] = e
            by_day.setdefault(day, []).append(e)
            out.append(e)
    return out
