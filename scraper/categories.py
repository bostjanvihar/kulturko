"""Assign every event a canonical category key.

Canonical keys (frontend translates them): music, theatre, exhibition,
film, kids, literature, education, festival, other.

Deterministic keyword matching only — sources label categories
inconsistently or not at all, so we normalize labels first and fall back
to scanning the title/description.
"""
from __future__ import annotations
import re
from .models import Event, strip_diacritics

# explicit label -> key (labels seen on Slovenian sites + kulturnik)
LABELS = {
    "glasba": "music", "koncert": "music", "koncerti": "music",
    "music": "music", "opera": "music",
    "gledalisce": "theatre", "predstava": "theatre", "drama": "theatre",
    "balet": "theatre", "ples": "theatre", "theatre": "theatre",
    "razstava": "exhibition", "razstave": "exhibition",
    "exhibition": "exhibition", "galerija": "exhibition",
    "film": "film", "kino": "film",
    "za otroke": "kids", "otroci": "kids", "lutke": "kids", "kids": "kids",
    "knjiga": "literature", "tisk": "literature", "literatura": "literature",
    "izobrazevanje": "education", "delavnica": "education",
    "predavanje": "education", "workshop": "education",
    "festival": "festival", "festivali": "festival",
}

# keyword regex -> key, checked against title + description + category text
KEYWORDS = [
    (r"lutk|otro[sk]|pravljic|mladin", "kids"),          # before theatre!
    (r"festival", "festival"),
    (r"koncert|glasb|jazz|rock|zbor|orkester|simfoni|recital|dj\b|opera",
     "music"),
    (r"predstav|gledalis|drama|komedij|balet|plesn|monodram|stand.?up",
     "theatre"),
    (r"razstav|galerij|vernisa|odprtje razstave|likovn", "exhibition"),
    (r"film|kino|projekcij|dokumentar", "film"),
    (r"knjig|literar|pesni|branje|avtor", "literature"),
    (r"delavnic|predavanj|okrogl[a]? miz|seminar|tecaj", "education"),
]


def _norm(s: str) -> str:
    return strip_diacritics((s or "").lower())


def canonical_category(e: Event) -> str:
    for part in re.split(r"[,/;]", _norm(e.category)):
        part = part.strip()
        if part in LABELS:
            return LABELS[part]
    hay = _norm(f"{e.category} {e.title} {e.description}")
    for pat, key in KEYWORDS:
        if re.search(pat, hay):
            return key
    return "other"


def assign_categories(events: list[Event]) -> None:
    for e in events:
        e.category = canonical_category(e)
