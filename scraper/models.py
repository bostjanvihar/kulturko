"""Event model + normalization helpers. No AI anywhere — pure parsing."""
from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


_STOPWORDS = {"koncert", "predstava", "razstava", "v", "na", "z", "s",
              "the", "a", "an", "in", "at", "live", "tour"}


def norm_title(title: str) -> str:
    """Normalized title used for dedup keys."""
    t = strip_diacritics(title.lower())
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    words = [w for w in t.split() if w not in _STOPWORDS]
    return " ".join(words) or t.strip()


def norm_venue(venue: str) -> str:
    v = strip_diacritics((venue or "").lower())
    v = re.sub(r"[^a-z0-9 ]+", " ", v)
    # common aliases so "SNG Maribor" == "Slovensko narodno gledališče Maribor"
    aliases = {
        "slovensko narodno gledalisce maribor": "sng maribor",
        "narodni dom": "narodni dom maribor",
        "dvorana union": "narodni dom maribor",
        "lutkovno gledalisce maribor": "minoriti",
        "minoritska cerkev": "minoriti",
        "stuk stajerski tednik": "stuk",
        # same venue, different labels across feeds/ACF fields (often a
        # differently-cased Slovene noun form the substring check misses)
        "kgb maribor kulturno glasbeni brlog": "klub kgb",
        "vetrinjc barcoda": "vetrinjski dvor",
        "glasbeni paviljon": "mestni park",
        "paviljon mestnega parka": "mestni park",
        "viteska dvorana pokrajinskega muzeja maribor": "pokrajinski muzej maribor",
        "vinagova klet": "vinag vinoteka v maribor",
        "minoritska cerkev marijinega vnebovzetja s samostanom": "minoriti",
        "oder pod hrastom kulturna cetrt minoriti": "minoriti pod hrastom",
        "pod hrastom": "minoriti pod hrastom",
        "lutkovno gledalisce maribor avditorij": "minoriti",
    }
    v = " ".join(v.split())
    return aliases.get(v, v)


@dataclass
class Event:
    title: str
    start: str                      # ISO 8601, local time
    end: Optional[str] = None
    venue: str = ""
    url: str = ""
    description: str = ""
    image: str = ""
    category: str = ""
    source: str = ""                # source id
    source_name: str = ""
    all_sources: list = field(default_factory=list)
    first_seen: Optional[str] = None
    id: str = ""

    def __post_init__(self):
        if not self.id:
            day = (self.start or "")[:10]
            raw = f"{norm_title(self.title)}|{day}|{norm_venue(self.venue)}"
            self.id = hashlib.sha1(raw.encode()).hexdigest()[:16]
        if not self.all_sources:
            self.all_sources = [{"id": self.source, "name": self.source_name,
                                 "url": self.url}]

    @property
    def start_dt(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.start)
        except (ValueError, TypeError):
            return None

    def to_dict(self):
        return asdict(self)
