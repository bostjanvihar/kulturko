"""Source adapters. Each takes a source config dict and returns [Event].

Design goals:
  * Zero AI at runtime — deterministic parsers only.
  * Degrade gracefully: a broken source logs a warning, never kills the run.
  * Transferable: adapters are generic; per-town specifics live in sources.yaml.
"""
from __future__ import annotations
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import Event
from .dates_sl import parse_sl_datetime

log = logging.getLogger("kulturko")

UA = ("Mozilla/5.0 (compatible; KulturkoBot/1.0; "
      "+https://github.com/YOUR-USERNAME/maribor-events)")
TIMEOUT = 30


def fetch(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Accept-Language": "sl,en;q=0.8"},
                     timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- RSS
def adapter_rss(src: dict) -> list[Event]:
    xml = fetch(src["url"]).content
    root = ET.fromstring(xml)
    events = []
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "ev": "http://purl.org/rss/1.0/modules/event/"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)
    for it in items:
        def g(tag):
            el = it.find(tag) if not tag.startswith("atom:") else it.find(tag, ns)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = g("title") or g("atom:title")
        link = g("link") or (it.find("atom:link", ns) is not None
                             and it.find("atom:link", ns).get("href", "")) or ""
        desc = re.sub(r"<[^>]+>", " ", g("description") or g("atom:summary"))
        # event-module start date, else scan description/pubDate
        start = g("ev:startdate")
        dt = None
        if start:
            try:
                dt = datetime.fromisoformat(start).replace(tzinfo=None)
            except ValueError:
                dt = parse_sl_datetime(start)
        if dt is None:
            dt = parse_sl_datetime(f"{title} {desc}")
        if dt is None:
            continue
        venue = src.get("venue", "")
        if not venue:
            # kulturnik puts "Venue, City" in category or description tail
            cat = g("category")
            venue = cat.split(",")[0].strip() if cat else ""
        events.append(Event(title=title, start=dt.isoformat(), venue=venue,
                            url=link, description=desc[:400],
                            source=src["id"], source_name=src["name"]))
    return events


# ------------------------------------------------------------- JSON-LD
def _walk_jsonld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
    elif isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any("Event" in str(x) for x in types):
            out.append(node)
        for v in node.values():
            _walk_jsonld(v, out)


def events_from_jsonld(html: str, src: dict, base_url: str) -> list[Event]:
    soup = BeautifulSoup(html, "lxml")
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_jsonld(data, found)
    events = []
    for e in found:
        start = e.get("startDate", "")
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=None)
        except ValueError:
            dt = parse_sl_datetime(start)
        if dt is None:
            continue
        loc = e.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        venue = (loc.get("name") if isinstance(loc, dict) else str(loc)) \
            or src.get("venue", "")
        img = e.get("image", "")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url", "")
        events.append(Event(
            title=e.get("name", "").strip(),
            start=dt.isoformat(),
            end=e.get("endDate") or None,
            venue=venue,
            url=urljoin(base_url, e.get("url", "") or base_url),
            description=re.sub(r"<[^>]+>", " ", str(e.get("description", "")))[:400],
            image=img if isinstance(img, str) else "",
            source=src["id"], source_name=src["name"]))
    return [e for e in events if e.title]


def adapter_jsonld(src: dict) -> list[Event]:
    html = fetch(src["url"]).text
    return events_from_jsonld(html, src, src["url"])


# ------------------------------------- WordPress "The Events Calendar"
def adapter_tribe(src: dict) -> list[Event]:
    base = src["url"].rstrip("/")
    api = f"{base}/wp-json/tribe/events/v1/events?per_page=50"
    events, page = [], 1
    while page <= 6:
        r = fetch(f"{api}&page={page}")
        data = r.json()
        for e in data.get("events", []):
            start = e.get("start_date", "").replace(" ", "T")
            try:
                dt = datetime.fromisoformat(start)
            except ValueError:
                continue
            venue = (e.get("venue") or {}).get("venue", "") or src.get("venue", "")
            img = (e.get("image") or {})
            events.append(Event(
                title=BeautifulSoup(e.get("title", ""), "lxml").get_text(),
                start=dt.isoformat(),
                end=e.get("end_date", "").replace(" ", "T") or None,
                venue=venue, url=e.get("url", ""),
                description=BeautifulSoup(e.get("description", ""),
                                          "lxml").get_text()[:400],
                image=img.get("url", "") if isinstance(img, dict) else "",
                category=", ".join(c.get("name", "")
                                   for c in e.get("categories", [])[:2]),
                source=src["id"], source_name=src["name"]))
        if page >= int(data.get("total_pages", 1)):
            break
        page += 1
    return events


# --------------------------------------------------- Next.js __NEXT_DATA__
def _hunt_events_in_json(node, hints, out, depth=0):
    """Recursively find lists of dicts that look like events."""
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                keys = set().union(*(d.keys() for d in v if isinstance(d, dict)))
                lk = {x.lower() for x in keys}
                has_title = lk & {"title", "naslov", "name", "ime"}
                has_date = any(any(w in x for w in
                                   ("date", "datum", "start", "zacetek"))
                               for x in lk)
                if has_title and has_date:
                    out.append(v)
            _hunt_events_in_json(v, hints, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _hunt_events_in_json(v, hints, out, depth + 1)


def adapter_nextjs(src: dict) -> list[Event]:
    html = fetch(src["url"]).text
    # JSON-LD is often present too — prefer it
    ld = events_from_jsonld(html, src, src["url"])
    if ld:
        return ld
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("no __NEXT_DATA__ found")
    data = json.loads(tag.string)
    candidates = []
    _hunt_events_in_json(data, src.get("nextjs_event_hints", []), candidates)
    events = []
    for lst in candidates:
        for d in lst:
            title = d.get("title") or d.get("naslov") or d.get("name") or ""
            raw_date = ""
            for k, v in d.items():
                if isinstance(v, str) and any(w in k.lower() for w in
                                              ("date", "datum", "start", "zacetek")):
                    raw_date = v
                    break
            dt = parse_sl_datetime(raw_date)
            if not (title and dt):
                continue
            slug = d.get("slug", "")
            url = urljoin(src["url"] + "/", slug) if slug else src["url"]
            events.append(Event(title=title.strip(), start=dt.isoformat(),
                                venue=src.get("venue", ""), url=url,
                                source=src["id"], source_name=src["name"]))
    return events


# ------------------------------------------------------- generic HTML
def adapter_html(src: dict) -> list[Event]:
    html = fetch(src["url"]).text
    if src.get("try_jsonld_first"):
        ld = events_from_jsonld(html, src, src["url"])
        if ld:
            return ld
    sel = src.get("selectors", {})
    soup = BeautifulSoup(html, "lxml")
    events = []
    year = datetime.now().year
    for item in soup.select(sel.get("item", "article")):
        t = item.select_one(sel.get("title", "h2, h3"))
        if not t:
            continue
        title = t.get_text(" ", strip=True)
        a = item.select_one(sel.get("url", "a"))
        url = urljoin(src["url"], a["href"]) if a and a.get("href") else src["url"]
        d = item.select_one(sel.get("date", "time, .date"))
        raw = ""
        if d:
            raw = d.get("datetime", "") or d.get_text(" ", strip=True)
        if not raw:
            raw = item.get_text(" ", strip=True)
        dt = parse_sl_datetime(raw, default_year=year)
        if dt is None:
            continue
        v = item.select_one(sel.get("venue", ".venue"))
        venue = (v.get_text(strip=True) if v else "") or src.get("venue", "")
        img = item.select_one("img")
        events.append(Event(
            title=title, start=dt.isoformat(), venue=venue, url=url,
            image=urljoin(src["url"], img.get("src", "")) if img else "",
            source=src["id"], source_name=src["name"]))
    return events


# --------------------------------------------------------------- ICS
def adapter_ics(src: dict) -> list[Event]:
    text = fetch(src["url"]).text
    events, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            dt = parse_sl_datetime(cur.get("DTSTART", ""))
            if dt and cur.get("SUMMARY"):
                events.append(Event(title=cur["SUMMARY"], start=dt.isoformat(),
                                    venue=cur.get("LOCATION",
                                                  src.get("venue", "")),
                                    url=cur.get("URL", src["url"]),
                                    description=cur.get("DESCRIPTION", "")[:400],
                                    source=src["id"], source_name=src["name"]))
            cur = None
        elif cur is not None and ":" in line:
            k, _, v = line.partition(":")
            cur[k.split(";")[0]] = v
    return events


# ---------------------------------------------------- Facebook Graph
def adapter_facebook_graph(src: dict) -> list[Event]:
    token = os.environ.get("FB_TOKEN")
    if not token:
        raise RuntimeError("FB_TOKEN secret not set — skipping")
    url = (f"https://graph.facebook.com/v19.0/{src['page_id']}/events"
           f"?fields=name,start_time,end_time,place,description,cover"
           f"&access_token={token}")
    data = fetch(url).json()
    events = []
    for e in data.get("data", []):
        try:
            dt = datetime.fromisoformat(e["start_time"]).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        events.append(Event(
            title=e.get("name", ""), start=dt.isoformat(),
            venue=(e.get("place") or {}).get("name", src.get("venue", "")),
            url=f"https://facebook.com/events/{e.get('id','')}",
            description=(e.get("description") or "")[:400],
            image=(e.get("cover") or {}).get("source", ""),
            source=src["id"], source_name=src["name"]))
    return events


ADAPTERS = {
    "rss": adapter_rss,
    "jsonld": adapter_jsonld,
    "tribe": adapter_tribe,
    "nextjs": adapter_nextjs,
    "html": adapter_html,
    "ics": adapter_ics,
    "facebook_graph": adapter_facebook_graph,
}


def run_source(src: dict) -> list[Event]:
    """Run a source, using its fallback config if the primary adapter fails
    or returns nothing."""
    if src.get("enabled") is False:
        return []
    try:
        events = ADAPTERS[src["adapter"]](src)
        if events:
            log.info("  %-24s %3d events (%s)", src["id"], len(events),
                     src["adapter"])
            return events
        raise RuntimeError("0 events")
    except Exception as exc:
        fb = src.get("fallback")
        if fb:
            merged = {**src, **fb}
            try:
                events = ADAPTERS[fb["adapter"]](merged)
                log.info("  %-24s %3d events (fallback %s)", src["id"],
                         len(events), fb["adapter"])
                return events
            except Exception as exc2:
                exc = exc2
        log.warning("  %-24s FAILED: %s", src["id"], exc)
        return []
