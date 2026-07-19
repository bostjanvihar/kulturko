"""Apify adapter — scrape public Facebook event pages via Apify actors.

Facebook blocks anonymous scraping, so this adapter delegates the dirty
work to an Apify "actor" (a hosted headless-browser scraper) and reads
back clean JSON. No AI at runtime; the scheduled GitHub Action just makes
HTTPS calls.

Setup (once):
  1. Free account at https://apify.com (no credit card for the free plan).
  2. Copy your API token: Apify Console -> Settings -> Integrations.
  3. GitHub repo -> Settings -> Secrets and variables -> Actions ->
     New repository secret: name APIFY_TOKEN, value = the token.
  4. Pass it through in .github/workflows/scrape.yml (env: APIFY_TOKEN).

sources.yaml example:
  - id: fb-gt22
    name: GT22 (Facebook)
    adapter: apify
    page_url: "https://www.facebook.com/GT22Maribor/events"
    venue: GT22
    max_events: 20            # keep low; Apify bills per result
    # actor: "apify~facebook-events-scraper"   # override if needed
    # timezone: "Europe/Ljubljana"             # for other towns
    # If page-events URLs return nothing, the default actor also accepts
    # specific event URLs or free-text searches instead of page_url:
    #   start_urls: ["https://www.facebook.com/events/1023978871819924"]
    #   search_queries: ["Maribor koncert"]

Note: the default actor (apify/facebook-events-scraper) declares
`startUrls` as a list of URL *strings* (not {"url": ...} objects) — this
adapter sends them in that shape.

Registered automatically as adapter "apify" when this module is imported
(see the import line in scraper/main.py).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone as _tzutc
from zoneinfo import ZoneInfo

import requests

from .adapters import ADAPTERS
from .models import Event

log = logging.getLogger("kulturko")

APIFY_BASE = "https://api.apify.com/v2"
DEFAULT_ACTOR = "apify~facebook-events-scraper"
POLL_EVERY = 10          # seconds
MAX_WAIT = 360           # give the headless browser up to 6 minutes


# --------------------------------------------------------------- parsing
def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def _to_local(raw, tz: ZoneInfo) -> datetime | None:
    """Accept epoch seconds/millis, ISO strings (incl. Z / offsets)."""
    if raw in (None, "", 0):
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:            # milliseconds
            ts /= 1000.0
        try:
            dt = datetime.fromtimestamp(ts, tz=_tzutc.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.astimezone(tz).replace(tzinfo=None)
    if isinstance(raw, str):
        s = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo:
            return dt.astimezone(tz).replace(tzinfo=None)
        return dt
    return None


def _venue_of(item: dict, fallback: str) -> str:
    loc = _first(item, "location", "place", "locationName", default=None)
    if isinstance(loc, dict):
        loc = _first(loc, "name", "city", default="")
    if isinstance(loc, str) and loc:
        return loc.split(",")[0].strip()
    return fallback


def parse_apify_items(items: list, src: dict) -> list[Event]:
    """Map actor output items -> Events. Field names vary between actor
    versions, so every lookup tries several candidates."""
    tz = ZoneInfo(src.get("timezone", "Europe/Ljubljana"))
    events = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # actor marks off cancelled events (field name varies by version)
        if any(it.get(k) for k in
               ("isCanceled", "isCancelled", "canceled", "cancelled")):
            continue
        title = str(_first(it, "name", "title", "eventName")).strip()
        start = _to_local(
            _first(it, "utcStartDate", "startTimestamp", "startDate",
                   "start_time", "startTime", "date", default=None), tz)
        if not (title and start):
            continue
        end = _to_local(
            _first(it, "utcEndDate", "endTimestamp", "endDate",
                   "end_time", "endTime", default=None), tz)
        url = str(_first(it, "url", "eventUrl", "link", default=""))
        eid = _first(it, "id", "eventId", default="")
        if not url.startswith("http"):
            # prefer the specific event link; fall back to the page URL
            url = (f"https://www.facebook.com/events/{eid}" if eid
                   else src.get("page_url", ""))
        desc = str(_first(it, "description", "summary"))[:400]
        img = _first(it, "imageUrl", "image", "photo", default="")
        if isinstance(img, dict):
            img = _first(img, "url", "imageUri", default="")
        events.append(Event(
            title=title,
            start=start.isoformat(),
            end=end.isoformat() if end else None,
            venue=_venue_of(it, src.get("venue", "")),
            url=url,
            description=desc,
            image=img if isinstance(img, str) else "",
            source=src["id"],
            source_name=src["name"],
        ))
    return events


# ------------------------------------------------------------ actor run
def _apify(method: str, path: str, token: str, **kw) -> requests.Response:
    r = requests.request(method, f"{APIFY_BASE}{path}",
                         params={"token": token, **kw.pop("params", {})},
                         timeout=60, **kw)
    r.raise_for_status()
    return r


def adapter_apify(src: dict) -> list[Event]:
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN secret not set — skipping")
    if not (src.get("page_url") or src.get("start_urls")
            or src.get("search_queries")):
        raise RuntimeError("source needs page_url, start_urls or search_queries")

    actor = src.get("actor", DEFAULT_ACTOR)
    # This actor's `startUrls` is a stringList — plain URL strings, NOT
    # {"url": ...} objects. `searchQueries` is offered as an alternative
    # input if a venue's page-events URL doesn't yield results.
    start_urls = ([src["page_url"]] if src.get("page_url") else []) \
        + list(src.get("start_urls", []))
    run_input = {"maxEvents": int(src.get("max_events", 20))}
    if start_urls:
        run_input["startUrls"] = start_urls
    if src.get("search_queries"):
        run_input["searchQueries"] = list(src["search_queries"])
    run_input.update(src.get("actor_input", {}))   # extra actor options

    run = _apify("POST", f"/acts/{actor}/runs", token,
                 json=run_input).json()["data"]
    run_id = run["id"]

    waited = 0
    while waited < MAX_WAIT:
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
        run = _apify("GET", f"/actor-runs/{run_id}", token).json()["data"]
        status = run.get("status")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {status}")
    else:
        raise RuntimeError(f"Apify run still not done after {MAX_WAIT}s")

    items = _apify("GET", f"/datasets/{run['defaultDatasetId']}/items",
                   token, params={"clean": "true"}).json()
    log.info("  %-24s apify returned %d items", src["id"], len(items))
    return parse_apify_items(items, src)


# self-register so main.py only needs `from . import adapter_apify`
ADAPTERS["apify"] = adapter_apify
