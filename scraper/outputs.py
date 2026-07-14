"""Build the static outputs the website and calendar apps consume:

  docs/data/events.json   — the app's database
  docs/data/events.ics    — master calendar (subscribe in Google Calendar)
  docs/data/venues/*.ics  — per-venue calendars
  docs/data/feed.xml      — RSS of newly discovered events (notifications)
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .models import Event, strip_diacritics


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;") \
                    .replace(",", "\\,").replace("\n", "\\n")


def _ics_dt(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%Y%m%dT%H%M%S")


def build_ics(events: list[Event], calname: str, tz: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//kulturko//event-aggregator//SL",
             f"X-WR-CALNAME:{_ics_escape(calname)}",
             f"X-WR-TIMEZONE:{tz}",
             "X-PUBLISHED-TTL:PT12H"]
    for e in events:
        try:
            start = _ics_dt(e.start)
        except (ValueError, TypeError):
            continue
        end = None
        if e.end:
            try:
                end = _ics_dt(e.end.replace(" ", "T"))
            except ValueError:
                end = None
        if not end:
            end = _ics_dt((datetime.fromisoformat(e.start)
                           + timedelta(hours=2)).isoformat())
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e.id}@kulturko",
            f"DTSTART;TZID={tz}:{start}",
            f"DTEND;TZID={tz}:{end}",
            f"SUMMARY:{_ics_escape(e.title)}",
            f"LOCATION:{_ics_escape(e.venue)}",
            f"DESCRIPTION:{_ics_escape((e.description or '')[:300] + ' ' + e.url)}",
            f"URL:{e.url}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def slugify(s: str) -> str:
    s = strip_diacritics(s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "venue"


def build_feed(new_events: list[Event], cfg: dict) -> str:
    site = cfg.get("site_url", "")
    items = []
    for e in new_events:
        items.append(f"""  <item>
    <title>{_xml(e.title)} — {_xml(e.venue)}</title>
    <link>{_xml(e.url or site)}</link>
    <guid isPermaLink="false">{e.id}</guid>
    <pubDate>{_rfc822(e.first_seen)}</pubDate>
    <description>{_xml(e.start[:16].replace('T', ' '))} · {_xml(e.venue)}. {_xml(e.description[:200])}</description>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Novi dogodki — {_xml(cfg.get('town', ''))}</title>
  <link>{_xml(site)}</link>
  <description>Newly announced cultural events in {_xml(cfg.get('town', ''))}</description>
{chr(10).join(items)}
</channel>
</rss>
"""


def _xml(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;")


def _rfc822(iso) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%a, %d %b %Y %H:%M:%S +0000")
    except (ValueError, TypeError):
        return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")


def write_outputs(events: list[Event], cfg: dict, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "venues").mkdir(exist_ok=True)
    tz = cfg.get("timezone", "Europe/Ljubljana")
    town = cfg.get("town", "")

    events.sort(key=lambda e: e.start)

    # per-venue ICS + venue index
    venues = {}
    for e in events:
        v = e.venue or "Drugo"
        venues.setdefault(v, []).append(e)
    venue_index = []
    for v, evs in sorted(venues.items(), key=lambda kv: -len(kv[1])):
        slug = slugify(v)
        (out_dir / "venues" / f"{slug}.ics").write_text(
            build_ics(evs, f"{v} — dogodki", tz), encoding="utf-8")
        venue_index.append({"name": v, "slug": slug, "count": len(evs)})

    (out_dir / "events.ics").write_text(
        build_ics(events, f"Kultura {town}", tz), encoding="utf-8")

    now = datetime.now()
    new_events = [e for e in events if e.first_seen and
                  (now - datetime.fromisoformat(e.first_seen)).days <= 7]
    (out_dir / "feed.xml").write_text(build_feed(
        sorted(new_events, key=lambda e: e.first_seen, reverse=True)[:50], cfg),
        encoding="utf-8")

    payload = {
        "generated": now.isoformat(timespec="seconds"),
        "town": town,
        "timezone": tz,
        "venues": venue_index,
        "events": [e.to_dict() for e in events],
    }
    (out_dir / "events.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
