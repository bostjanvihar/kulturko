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
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .models import Event
from .dates_sl import parse_sl_datetime, find_future_date

log = logging.getLogger("kulturko")

# Browser-like UA (several venue sites block obvious bots) with a bot hint
# and contact URL appended, per scraping etiquette.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
      "KulturkoBot/1.0 (+https://github.com/bostjanvihar/kulturko)")
TIMEOUT = 45


# Full browser-like header set. Some venue WAFs (e.g. gustaf.si) reject
# requests that lack an Accept header with 415/403. The "*/*" fallback
# keeps JSON API endpoints working.
HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/json;q=0.9,*/*;q=0.8"),
    "Accept-Language": "sl,en;q=0.8",
}


def fetch(url: str, **kw) -> requests.Response:
    headers = {**HEADERS, **kw.pop("headers", {})}
    r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    # requests falls back to ISO-8859-1 when the header omits charset;
    # modern sites are UTF-8 (fixes mojibake on e.g. klub-kgb.si)
    ct = r.headers.get("content-type", "").lower()
    if "charset" not in ct and (r.encoding or "").lower() in ("iso-8859-1", ""):
        r.encoding = "utf-8"
    return r


# ---------------------------------------------- "Add to Google Calendar"
# Many event plugins (e.g. EventON) render a Google Calendar link on the
# event's own page with a clean UTC dates=<start>/<end> param — far more
# reliable than scanning free text for a Slovene date when the site's own
# structured fields (ACF/REST meta) don't expose the event date.
_GCAL_DATES_RE = re.compile(
    r"google\.com/calendar/event\?[^\"'>]*?dates=(\d{8}T\d{6}Z)(?:%2F|/)?(\d{8}T\d{6}Z)?")


def _extract_gcal_dt(html: str, tz_name: str = "Europe/Ljubljana"):
    m = _GCAL_DATES_RE.search(html)
    if not m:
        return None
    start_utc = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ") \
        .replace(tzinfo=timezone.utc)
    return start_utc.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


# ---------------------------------------------------------------- RSS
def _localname(tag: str) -> str:
    """'{http://ns}dtstart' -> 'dtstart' (namespace-agnostic matching)."""
    return tag.rsplit("}", 1)[-1].lower()


def adapter_rss(src: dict) -> list[Event]:
    xml = fetch(src["url"]).content
    root = ET.fromstring(xml)
    events = []
    items = [el for el in root.iter() if _localname(el.tag) in ("item", "entry")]
    for it in items:
        # collect child elements by local tag name regardless of namespace
        # (kulturnik uses <ical:dtstart>, others <ev:startdate> etc.)
        f = {}
        for child in it:
            name = _localname(child.tag)
            text = (child.text or "").strip()
            if name == "link" and not text:      # Atom <link href="..."/>
                text = child.get("href", "")
            if text and name not in f:
                f[name] = text
        title = f.get("title", "")
        link = f.get("link", "")
        desc = re.sub(r"<[^>]+>", " ", f.get("description") or f.get("summary", ""))
        start = f.get("dtstart") or f.get("startdate", "")
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
            # kulturnik puts "Venue, City" in ical:location; else category
            loc = f.get("location") or f.get("category", "")
            venue = loc.split(",")[0].strip()
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
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    api = f"{base}/wp-json/tribe/events/v1/events?per_page=50&start_date={since}"
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
    # honor <base href> (e.g. ugm.si uses root-based relative links)
    base_el = soup.find("base", href=True)
    base_url = urljoin(src["url"], base_el["href"]) if base_el else src["url"]
    events = []
    year = datetime.now().year
    year_re = src.get("year_from_url")   # e.g. '/dogodki/(\\d{4})/'
    detail_budget = int(src.get("max_detail_fetches", 12))
    for item in soup.select(sel.get("item", "article")):
        t = item.select_one(sel.get("title", "h2, h3"))
        if not t:
            continue
        title = t.get_text(" ", strip=True)
        a = item.select_one(sel.get("url", "a"))
        url = urljoin(base_url, a["href"]) if a and a.get("href") else src["url"]
        d = item.select_one(sel.get("date", "time, .date"))
        raw = ""
        if d:
            raw = d.get("datetime", "") or d.get_text(" ", strip=True)
        if not raw:
            raw = item.get_text(" ", strip=True)
        default_year = year
        if year_re:
            m = re.search(year_re, url)
            if m:
                default_year = int(m.group(1))
        dt = parse_sl_datetime(raw, default_year=default_year)
        if dt is None and src.get("follow_detail") and url != src["url"] \
                and detail_budget > 0:
            # listing has no date — read the event's own page for one
            detail_budget -= 1
            try:
                dsoup = BeautifulSoup(fetch(url).text, "lxml")
                for tag in dsoup(["script", "style", "header",
                                  "footer", "nav"]):
                    tag.decompose()
                dt = find_future_date(dsoup.get_text(" ", strip=True))
            except requests.RequestException:
                dt = None
        if dt is None:
            continue
        v = item.select_one(sel.get("venue", ".venue"))
        venue = (v.get_text(strip=True) if v else "") or src.get("venue", "")
        img = item.select_one("img")
        events.append(Event(
            title=title, start=dt.isoformat(), venue=venue, url=url,
            image=urljoin(base_url, img.get("src", "")) if img else "",
            source=src["id"], source_name=src["name"]))
    return events


# ------------------------------------------ WordPress custom post type
def _dot_get(obj, path: str, default=""):
    """'acf.dogodek.0.Lokacija' -> nested value, '' if any hop missing."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
        if cur is None:
            return default
    return cur


def adapter_wp_v2(src: dict) -> list[Event]:
    """Standard WP REST API (/wp-json/wp/v2/<rest_base>) for sites that
    expose events as a custom post type (e.g. Narodni dom's 'dogodek').
    Event date/venue live in ACF fields — configure dot-paths in the source.

    Some sites disable pretty permalinks, so /wp-json/... 404s; set
    `rest_style: query` to use the `?rest_route=/wp/v2/<rest_base>` form
    instead (auto-discoverable via the page's <link rel="https://api.w.org/">).

    If the post type doesn't expose its event date via REST at all (e.g.
    EventON's custom fields aren't registered for REST), set `date_path: ""`
    and `gcal_detail: true` to fetch each event's own page and read the
    UTC date off its "Add to Google Calendar" link instead — capped by
    `max_detail_fetches`.
    """
    base = src["url"].rstrip("/")
    rest = src.get("rest_base", "posts")
    per = int(src.get("per_page", 100))
    max_pages = int(src.get("max_pages", 4))
    orderby = src.get("orderby", "modified")
    query_style = src.get("rest_style") == "query"
    detail_budget = int(src.get("max_detail_fetches", 20))
    events = []
    for page in range(1, max_pages + 1):
        if query_style:
            url = (f"{base}/index.php?rest_route=/wp/v2/{rest}"
                   f"&per_page={per}&page={page}&orderby={orderby}&order=desc")
        else:
            url = (f"{base}/wp-json/wp/v2/{rest}"
                   f"?per_page={per}&page={page}&orderby={orderby}&order=desc")
        r = fetch(url)
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        for e in data:
            raw_date = str(_dot_get(e, src.get("date_path", "date")))
            dt = parse_sl_datetime(raw_date)
            if dt is None and src.get("gcal_detail") and detail_budget > 0 \
                    and e.get("link"):
                detail_budget -= 1
                try:
                    dt = _extract_gcal_dt(fetch(e["link"]).text)
                except requests.RequestException:
                    dt = None
            if dt is None:
                continue
            title = BeautifulSoup(str(_dot_get(e, "title.rendered")),
                                  "lxml").get_text(" ", strip=True)
            if not title:
                continue
            venue = ""
            if src.get("venue_path"):
                venue = str(_dot_get(e, src["venue_path"])).split(",")[0].strip()
            desc = re.sub(r"<[^>]+>", " ",
                          str(_dot_get(e, src.get("desc_path", ""), "")))
            events.append(Event(
                title=title, start=dt.isoformat(),
                venue=venue or src.get("venue", ""),
                url=e.get("link", base), description=desc[:400].strip(),
                source=src["id"], source_name=src["name"]))
        if len(data) < per:
            break
    return events


# ------------------------------------ JSON embedded in a JS <script> var
_JSVAR_TIME_RE = re.compile(r",\s*(\d{1,2})[:.](\d{2})")


def adapter_jsvar(src: dict) -> list[Event]:
    """Sites whose event list is client-templated (mustache-style
    placeholders in the HTML) but whose full dataset is embedded
    server-side as a plain JS array assigned to a variable, e.g.
    visitmaribor.si's Umbraco 'catalogueList' widget:
        var jsonData_<guid> = [ {"Title": "...", "DateFrom": "...",
                                  "Dates": "[[20260715, 20260822]]", ...}, ... ];
    `var_prefix` matches the variable name up to its (CMS-instance-specific)
    suffix. `Dates` holds machine-readable [[start_yyyymmdd, end_yyyymmdd]]
    pairs; a time after a comma in `DatesAsString` (e.g. "..., 18:00 → 21:00")
    is used as the start time when present.
    """
    html = fetch(src["url"]).text
    prefix = src.get("var_prefix", "jsonData_")
    m = re.search(rf"var\s+{re.escape(prefix)}\w+\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        raise RuntimeError(f"no {prefix}* variable found")
    data = json.loads(m.group(1))
    events, seen = [], set()
    for d in data:
        title = (d.get("Title") or "").strip()
        if not title:
            continue
        try:
            ymd = json.loads(d.get("Dates") or "[]")[0][0]
            y, mo, day = ymd // 10000, (ymd // 100) % 100, ymd % 100
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        hh, mm = 19, 0
        tm = _JSVAR_TIME_RE.search(d.get("DatesAsString") or "")
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
        try:
            dt = datetime(y, mo, day, hh, mm)
        except ValueError:
            continue
        key = (title, dt.isoformat())
        if key in seen:
            continue
        seen.add(key)
        events.append(Event(
            title=title, start=dt.isoformat(), venue=src.get("venue", ""),
            url=d.get("LinkMore") or src["url"],
            image=urljoin(src["url"], d["Image"]) if d.get("Image") else "",
            source=src["id"], source_name=src["name"]))
    return events


# ------------------------------------------------- Squarespace events
def adapter_squarespace(src: dict) -> list[Event]:
    """Squarespace event collections expose JSON at <collection>?format=json
    (e.g. mkc.si/koledar). startDate/endDate are epoch milliseconds UTC."""
    url = src["url"].rstrip("/")
    d = fetch(url + "?format=json").json()
    items = (d.get("upcoming") or []) + (d.get("items") or [])
    tz = ZoneInfo(src.get("timezone", "Europe/Ljubljana"))

    def ms_to_iso(ms):
        return datetime.fromtimestamp(ms / 1000, tz).replace(tzinfo=None) \
                       .isoformat()

    events = []
    for it in items:
        ms = it.get("startDate")
        title = (it.get("title") or "").strip()
        if not (ms and title):
            continue
        loc = it.get("location") or {}
        events.append(Event(
            title=title, start=ms_to_iso(ms),
            end=ms_to_iso(it["endDate"]) if it.get("endDate") else None,
            venue=loc.get("addressTitle", "") or src.get("venue", ""),
            url=urljoin(url + "/", it.get("fullUrl", "")),
            description=re.sub(r"<[^>]+>", " ",
                               it.get("excerpt", ""))[:400].strip(),
            image=it.get("assetUrl", ""),
            category=", ".join((it.get("tags") or [])[:2]),
            source=src["id"], source_name=src["name"]))
    return events


# --------------------------------------------- Nuxt 3 payload (devalue)
_NUXT_MARKERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef",
                 "EmptyRef", "EmptyShallowRef", "NuxtError", "Set", "Map"}
_DATE_KEYS = ("dateandtime", "startdate", "start_date", "datum", "date",
              "start", "zacetek")
_TITLE_KEYS = ("title", "naslov", "name", "ime")


def _devalue_resolve(arr, idx=0, memo=None):
    """Nuxt 3 serializes state as a flat array where dict values / list
    elements are integer indices into the same array ('devalue' format)."""
    if memo is None:
        memo = {}
    if idx in memo:
        return memo[idx]
    val = arr[idx]
    if isinstance(val, dict):
        out = {}
        memo[idx] = out
        for k, v in val.items():
            out[k] = (_devalue_resolve(arr, v, memo)
                      if isinstance(v, int) and not isinstance(v, bool)
                      and 0 <= v < len(arr) else v)
        return out
    if isinstance(val, list):
        if val and isinstance(val[0], str) and val[0] in _NUXT_MARKERS:
            if len(val) > 1 and isinstance(val[1], int):
                return _devalue_resolve(arr, val[1], memo)
            return None
        out = []
        memo[idx] = out
        for v in val:
            out.append(_devalue_resolve(arr, v, memo)
                       if isinstance(v, int) and not isinstance(v, bool)
                       and 0 <= v < len(arr) else v)
        return out
    return val


def _pick_title(d: dict) -> str:
    for want in _TITLE_KEYS:
        for k, v in d.items():
            if k.lower() == want and isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def adapter_nuxt_payload(src: dict) -> list[Event]:
    """Nuxt 3 sites (e.g. minoriti.si): event data lives in the
    __NUXT_DATA__ payload, either inline or behind a data-src URL."""
    html = fetch(src["url"]).text
    m = re.search(r'id="__NUXT_DATA__"[^>]*data-src="([^"]+)"', html)
    if m:
        payload = fetch(urljoin(src["url"], m.group(1))).json()
    else:
        m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
                      html, re.S)
        if not m:
            raise RuntimeError("no __NUXT_DATA__ found")
        payload = json.loads(m.group(1))
    tree = _devalue_resolve(payload)

    found = []

    def walk(node, depth=0):
        if depth > 15:
            return
        if isinstance(node, dict):
            lk = {k.lower(): k for k in node}
            dk = next((lk[k] for k in _DATE_KEYS if k in lk), None)
            if dk and isinstance(node.get(dk), str):
                found.append((node, dk))
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(tree)
    events, seen = [], set()
    for node, dk in found:
        raw = node[dk]
        try:
            dt = datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            dt = parse_sl_datetime(raw)
        if dt is None or node.get("cancelled") is True:
            continue
        # title on the node itself, or on a nested record (e.g. 'event')
        title = _pick_title(node)
        slug = node.get("slug", "")
        if not title:
            for v in node.values():
                if isinstance(v, dict):
                    t = _pick_title(v)
                    if t:
                        title = t
                        slug = v.get("slug", "") or slug
                        break
        if not title:
            continue
        venue = src.get("venue", "")
        loc = node.get("location") or node.get("lokacija")
        if isinstance(loc, dict):
            venue = loc.get("name") or loc.get("title") or venue
        elif isinstance(loc, str) and loc:
            venue = loc
        key = (title, dt.isoformat())
        if key in seen:
            continue
        seen.add(key)
        url = urljoin(src["url"].rstrip("/") + "/", slug) if slug else src["url"]
        events.append(Event(title=title, start=dt.isoformat(), venue=venue,
                            url=url,
                            source=src["id"], source_name=src["name"]))
    return events


# ------------------------------------------- WooCommerce ticket shops
def adapter_woo_store(src: dict) -> list[Event]:
    """WooCommerce Store API (public, no auth). Used for ticket shops like
    vstopnice.stuk.org where each product is an event and the date is in
    the product description ('Datum: 19. 9. 2026')."""
    base = src["url"].rstrip("/")
    products = fetch(f"{base}/wp-json/wc/store/products?per_page=100").json()
    events = []
    for p in products:
        name = BeautifulSoup(p.get("name", ""), "lxml").get_text(" ", strip=True)
        desc = BeautifulSoup((p.get("short_description") or "") + " " +
                             (p.get("description") or ""),
                             "lxml").get_text(" ", strip=True)
        dt = parse_sl_datetime(desc)
        if not (name and dt):
            continue
        imgs = p.get("images") or []
        events.append(Event(
            title=name, start=dt.isoformat(), venue=src.get("venue", ""),
            url=p.get("permalink", base), description=desc[:400],
            image=imgs[0].get("src", "") if imgs else "",
            source=src["id"], source_name=src["name"]))
    return events


# --------------------------------------- JSON embedded in data-* attrs
def adapter_data_attr(src: dict) -> list[Event]:
    """Sites that embed their event list as JSON in an HTML attribute,
    e.g. sng-mb.si: <div class="calendar-table" data-events='[...]'>."""
    html = fetch(src["url"]).text
    soup = BeautifulSoup(html, "lxml")
    sel = src.get("selectors", {}).get("item", "[data-events]")
    attr = src.get("data_attr", "data-events")
    events, seen = [], set()
    for el in soup.select(sel):
        try:
            data = json.loads(el.get(attr) or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            continue
        for d in data:
            if not isinstance(d, dict):
                continue
            title = (d.get("title") or d.get("name") or "").strip()
            raw = f"{d.get('date', '')} ob {d.get('time', '')}"
            dt = parse_sl_datetime(raw)
            if not (title and dt):
                continue
            if d.get("is_cancelled"):
                continue
            place = d.get("place") or d.get("location") or ""
            if isinstance(place, list):
                place = ", ".join(str(x) for x in place)
            genre = d.get("genre", "")
            if isinstance(genre, list):
                genre = ", ".join(str(x) for x in genre)
            author = d.get("author") or ""
            key = (title, dt.isoformat())
            if key in seen:
                continue
            seen.add(key)
            slug = _dot_get(d, "post.post_name")
            prefix = src.get("event_url_prefix", "")
            url = (urljoin(src["url"], f"{prefix}{slug}/")
                   if slug and prefix else src["url"])
            desc = " · ".join(x for x in (author, place) if x)
            events.append(Event(
                title=title, start=dt.isoformat(),
                venue=src.get("venue", "") or place, url=url,
                description=desc[:400], category=genre,
                source=src["id"], source_name=src["name"]))
    return events


# ------------------------------- sign-up forms grouped by a date heading
# "16.00 - Poetičen Maribor" -> (16, 00, "Poetičen Maribor")
_OPTION_RE = re.compile(r"^\s*(\d{1,2})[.:](\d{2})\s*[-–—]\s*(.+)$")


def adapter_grouped_options(src: dict) -> list[Event]:
    """Sign-up forms where each event is a checkbox option and the date
    lives on the heading of the group the option sits in (e.g. rajzefiber's
    Festival sprehodov form: a 'PETEK, 20. 3. 2026' fieldset containing
    '16.00 - <walk title>' options).

    The date comes from `selectors.group_date`, the time+title from each
    `selectors.item` via `item_re` (groups: hour, minute, title). Groups
    whose heading holds no date (the name/e-mail fields) are skipped.
    """
    soup = BeautifulSoup(fetch(src["url"]).text, "lxml")
    sel = src.get("selectors", {})
    item_re = re.compile(src["item_re"]) if src.get("item_re") else _OPTION_RE
    # e.g. a "/ ZAPRTE PRIJAVE" marker — signups are closed but the walk
    # still takes place, so strip the marker and keep the event.
    strip_re = re.compile(src["strip_re"], re.I) if src.get("strip_re") else None
    events, seen = [], set()
    for group in soup.select(sel.get("group", "fieldset")):
        head = group.select_one(sel.get("group_date", "legend"))
        if not head:
            continue
        day = parse_sl_datetime(head.get_text(" ", strip=True))
        if day is None:
            continue
        for opt in group.select(sel.get("item", "label")):
            m = item_re.match(opt.get_text(" ", strip=True))
            if not m:
                continue
            title = m.group(3).strip()
            if strip_re:
                title = strip_re.sub("", title).strip(" /-–—")
            if not title:
                continue
            try:
                dt = day.replace(hour=int(m.group(1)), minute=int(m.group(2)))
            except ValueError:                    # e.g. "25.00 - ..."
                continue
            key = (title, dt.isoformat())
            if key in seen:
                continue
            seen.add(key)
            events.append(Event(
                title=title, start=dt.isoformat(), venue=src.get("venue", ""),
                url=src["url"], source=src["id"], source_name=src["name"]))
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
    "nuxt_payload": adapter_nuxt_payload,
    "wp_v2": adapter_wp_v2,
    "squarespace": adapter_squarespace,
    "woo_store": adapter_woo_store,
    "data_attr": adapter_data_attr,
    "grouped_options": adapter_grouped_options,
    "jsvar": adapter_jsvar,
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
        if str(exc) == "0 events":
            log.info("  %-24s   0 events (source has nothing published)",
                     src["id"])
        else:
            log.warning("  %-24s FAILED: %s", src["id"], exc)
        return []
