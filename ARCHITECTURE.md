# Kulturni zbiralnik — architecture & maintenance guide

> Display name only: the repo slug, the Pages URL, the ICS `UID:…@kulturko`
> and the `kulturko-*` localStorage keys all still say `kulturko` **on
> purpose** — see "Gotchas" before renaming any of them.

A developer-oriented map of the repo. The user-facing overview is in
`README.md`; this file is for whoever (human or AI) next works on the code.

## What it is

A zero-infrastructure aggregator of cultural events in Maribor, Slovenia.
A GitHub Action runs a Python scraper once a day, which visits ~20 venue
sources, normalizes and deduplicates the events, and writes static files
into `docs/`. GitHub Pages serves `docs/`, and the browser app
(`docs/index.html`) reads `docs/data/events.json` client-side. No server,
no database, no runtime AI — all parsing is deterministic.

```
GitHub Actions (.github/workflows/scrape.yml, weekly Mon 00:00 CEST)
  └─ python -m scraper.main
       ├─ load sources.yaml
       ├─ run one adapter per source          (scraper/adapters.py)
       ├─ dedupe across sources               (scraper/dedupe.py)
       ├─ assign a category to each event     (scraper/categories.py)
       ├─ filter to [today-7d, today+365d]
       ├─ preserve first_seen from prev run
       └─ write docs/data/*                   (scraper/outputs.py)
             events.json  — the app's database
             events.ics   — master calendar (Google Calendar subscribe)
             venues/*.ics  — per-venue calendars
             feed.xml      — RSS of newly-seen events
GitHub Pages → serves docs/ ; index.html fetches data/events.json
```

## File-by-file

| File | Role |
|---|---|
| `sources.yaml` | **The main knob.** Declares every source: its `adapter` and per-source config. Adding/fixing a venue is usually a change here only. |
| `scraper/main.py` | Pipeline orchestrator. Loads config, runs sources, dedupes, filters by date window, preserves `first_seen`, writes outputs. Exits non-zero (keeping old data) if *all* sources return nothing. |
| `scraper/adapters.py` | One function per adapter type + the `fetch()` helper and `run_source()` (which applies fallbacks and graceful failure). This is where per-CMS scraping logic lives. |
| `scraper/dates_sl.py` | Slovenian/generic date+time parsing. `parse_sl_datetime()` for a single string; `find_future_date()` scans free text for the earliest upcoming date (used by `follow_detail`). |
| `scraper/models.py` | The `Event` dataclass + normalization (`norm_title`, `norm_venue` with venue aliases) and the stable `id` hash (norm_title|day|norm_venue). |
| `scraper/dedupe.py` | Cross-source dedup: exact key match, then fuzzy (same day, title similarity ≥ 0.85, compatible venues). Richer record wins; all source links are kept in `all_sources`. |
| `scraper/categories.py` | Deterministic keyword → category (music/theatre/exhibition/film/kids/literature/education/festival/other). |
| `scraper/outputs.py` | Builds events.json, the ICS calendars, and feed.xml. |
| `docs/index.html` | The whole frontend (single file). Fetches `data/events.json`, renders filterable list, "N new" badge via localStorage, calendar buttons. |
| `docs/data/` | Generated output — committed by the bot each day. Don't hand-edit. |

## Adapters (the `adapter:` field in sources.yaml)

| adapter | Use when | Key config |
|---|---|---|
| `rss` | Any RSS/Atom feed. Reads dates from `<ical:dtstart>`/`<ev:startdate>` **namespace-agnostically**, venue from `<ical:location>`. | `url`, optional `venue` |
| `wp_v2` | WordPress site exposing events as a custom post type at `/wp-json/wp/v2/<rest_base>`. Reads date/venue from ACF fields via dot-paths. | `rest_base`, `date_path`, `venue_path`, `desc_path`, `orderby` |
| `tribe` | WordPress "The Events Calendar" plugin (`/wp-json/tribe/events/v1/events`). | `url` (site root) |
| `squarespace` | Squarespace event collection — append `?format=json`. Dates are epoch-ms. | `url` (the collection, e.g. `/koledar`) |
| `nuxt_payload` | Nuxt 3 sites — parses the `__NUXT_DATA__` devalue payload (inline or `data-src`). | `url` |
| `nextjs` | Next.js `__NEXT_DATA__` sites (prefers JSON-LD). | `url` |
| `jsonld` | Pages with schema.org Event JSON-LD. | `url` |
| `woo_store` | WooCommerce ticket shop — public Store API (`/wp-json/wc/store/products`); date parsed from product description text. | `url` (shop root) |
| `data_attr` | Event list embedded as JSON in an HTML attribute (e.g. SNG's `data-events`). | `selectors.item`, `data_attr`, `event_url_prefix` |
| `grouped_options` | A sign-up **form** is the only listing: each event is a checkbox option ("16.00 - Title") inside a group whose heading carries the date ("PETEK, 20. 3. 2026"). Groups with no parsable date (name/e-mail fields) are skipped. | `selectors.{group,group_date,item}`, `item_re` (hour/minute/title groups), `strip_re` (drop a marker like "/ ZAPRTE PRIJAVE") |
| `html` | Generic CSS-selector scrape. Honors `<base href>`. Options: `try_jsonld_first`, `year_from_url` (regex to pull the year from each event URL), `follow_detail` (fetch each event page and scan its body for a date when the listing has none, capped by `max_detail_fetches`). | `selectors.{item,title,url,date,venue}` |
| `ics` | A plain `.ics` URL. | `url` |
| `apify` | Public Facebook event pages via an [Apify](https://apify.com) actor (default `apify/facebook-events-scraper`). Starts an actor run, polls it, reads the dataset. Needs the `APIFY_TOKEN` secret; bills per result. Lives in its own module `scraper/adapter_apify.py`, self-registered on import. **The actor's `startUrls` is a list of URL *strings*, not `{"url":…}` objects** — a mismatch makes the run scrape nothing silently. | `page_url` (or `start_urls` / `search_queries`), `max_events`, optional `actor`, `actor_input` |
| `facebook_graph` | Facebook Page events — needs `FB_TOKEN` secret. Disabled by default (FB blocks anon scraping). | `page_id` |

`run_source()` catches all exceptions per source (never fatal), tries an
optional `fallback:` adapter, and logs a one-line result. A source that
returns zero events (e.g. a venue on summer break) is logged as
"nothing published", not FAILED.

## How to add a source (the workflow that works)

1. **Fetch the page and identify the CMS.** Look for `wp-json`, `__NEXT_DATA__`,
   `__NUXT_DATA__`, `?format=json` (Squarespace), tribe markup, JSON-LD,
   or a plain server-rendered list. `curl`/a scratch script + grepping the
   HTML for `class="..."` and date strings tells you fast.
2. **Prefer a structured endpoint over CSS scraping** — WP REST, tribe,
   Squarespace JSON, Nuxt payload are far more stable than selectors.
3. **Check for a Kulturnik feed first.** `https://dogodki.kulturnik.si/?where=<Venue>&format=rss`
   aggregates many Maribor venues with clean `<ical:dtstart>` dates —
   often the most reliable route, and the only route for Facebook-only
   venues (GT22, Klub KGB). Use the venue's exact Kulturnik name.
4. **Verify against real content.** If the venue is on summer break and its
   listing is empty, use the Wayback Machine
   (`http://web.archive.org/cdx/search/cdx?url=...&output=json`) to fetch a
   snapshot that *had* events, and test your selectors against it.
5. **Add the entry to `sources.yaml`**, run `python -m scraper.main`, and
   check the per-source count in the log.

## Gotchas learned the hard way

- **Everything was broken at first.** The repo shipped with `[demo]` seed
  data and adapters that didn't match the live sites; the "keep previous
  data if all sources fail" safety net meant the demo data persisted. If
  the site looks stale, check the Actions log for per-source counts.
- **WAFs.** Some venue sites (gustaf.si) block requests lacking a browser
  `Accept` header (415/403) and throttle repeated hits. `fetch()` sends a
  full browser-like header set. From the weekly Action (clean IP, one
  request) this is usually fine; hammering a site while developing will
  get you temporarily challenged/blocked.
- **Encoding.** `requests` guesses ISO-8859-1 when the charset header is
  missing; `fetch()` forces UTF-8 in that case (fixed mojibake on
  klub-kgb.si).
- **Date/time parsing is the fragile part.** Times are often split by HTML
  spans ("21 :00") or embedded next to date fragments ("18.3.2023" can be
  misread as 3:20). The parser prefers an `ob HH[:MM]` phrase and guards
  bare times with digit-boundary lookarounds. **If you touch
  `dates_sl.py`, run `scratchpad`'s date battery / add cases** — small
  changes here silently corrupt many events.
- **Date ranges resolve to the dated end, not the start.** For a range the
  parser takes the first date it can fully resolve, so
  "25. 9. 2026 → 27. 9. 2026" correctly yields 25 Sep, but
  "26. 6.-4. 7. 2026" (year only on the second date) yields 4 Jul. Seen on
  `najstarejsa-trta`; fixing it means teaching `dates_sl.py` about ranges,
  which is the risky file — weigh it against the handful of affected events.
- **Summer break.** Many Maribor venues publish nothing June–August, so a
  new source legitimately returning 0 today is expected — verify with a
  Wayback snapshot rather than assuming the adapter is broken.
- **The `kulturko` identifiers are load-bearing — don't "finish" the rename.**
  The project display name is *Kulturni zbiralnik*, but three things
  deliberately still say `kulturko`, and each breaks something if changed:
  `UID:{id}@kulturko` in `outputs.py` (an event's identity in subscribers'
  calendars — change it and every event is re-added as a duplicate);
  `kulturko-seen-ids` / `kulturko-lang` in `index.html` (change them and
  every visitor's "new" badge lights up for all events at once); and the
  repo slug itself, which the Pages URL and every published `.ics`
  subscription are built from. Renaming the GitHub repo silently kills
  existing calendar subscriptions.
- **Scheduled Actions auto-disable** after ~60 days of repo inactivity.
  If the weekly update stops, re-enable "Scrape events weekly" in the
  Actions tab.
- **Dedup keeps the richer record's fields** but each event keeps its own
  `start`; when the same event comes from a clean feed (Kulturnik) and a
  messy scrape, the feed's version usually wins as "richer".

## Local development

```bash
pip install -r requirements.txt
python -m scraper.main          # writes docs/data/
python -m http.server -d docs   # open http://localhost:8000
```

The scraper is safe to run locally; it only reads remote sites and writes
into `docs/data/`. Commit the regenerated `docs/data/` only if you intend
to publish (the weekly bot does this automatically).
