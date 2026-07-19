# Kulturni zbiralnik — kulturni dogodki na enem mestu

A self-updating aggregator of cultural events. Scrapes venue websites weekly,
deduplicates across sources, and publishes a filterable website, calendar
feeds (Google Calendar sync), and an RSS feed of newly announced events.

**No servers. No databases. No AI at runtime.**

### What it covers

Events are collected from venue websites, the Kulturnik aggregator, and
Facebook for a selected list of venue pages (scraped via [Apify](https://apify.com);
see the `apify` adapter and the `fb-*` sources in `sources.yaml`). Facebook
covers only those explicitly listed pages, so an event announced only on a
Facebook page that isn't in the list — and isn't picked up by Kulturnik —
can still be missing. See the source table below for what each venue
contributes.

> The project is named *Kulturni zbiralnik*; the repository slug is still
> `kulturko`, which keeps the Pages URL and every published `.ics`
> subscription working. Only the display name changed.

```
GitHub Actions (weekly cron, Mon 00:00 CEST)
  └─ python -m scraper.main
       ├─ runs one adapter per source (sources.yaml)
       ├─ deduplicates events across sources
       └─ writes docs/data/{events.json, events.ics, venues/*.ics, feed.xml}
GitHub Pages
  └─ serves docs/ — the app reads events.json in the browser
```

## Deploy (10 minutes)

1. Create a new GitHub repository (e.g. `maribor-events`) and push this folder.
2. **Settings → Pages** → Source: *Deploy from a branch* → Branch: `main`,
   folder: `/docs`. Your site goes live at
   `https://YOUR-USERNAME.github.io/maribor-events/`.
3. Edit `sources.yaml`: set `site_url` to that address.
4. **Actions** tab → *Scrape events weekly* → **Run workflow** to do the first
   real scrape (this replaces the demo data). It then runs automatically every
   week, Monday 00:00 CEST (Sunday 22:00 UTC).
5. **Settings → Actions → General** → Workflow permissions → *Read and write*
   (needed so the bot can commit updated data).

## Calendar sync (Google)

- **Subscribe (auto-updating):** the site's *Sinhroniziraj s koledarjem*
  button, or in Google Calendar: *Other calendars → + → From URL* →
  `https://YOUR-USERNAME.github.io/maribor-events/data/events.ics`.
  Google re-fetches subscribed calendars roughly every 12–24 h, so new events
  appear on their own.
- **Per-venue calendars:** `data/venues/<venue>.ics` (e.g. `sng-maribor.ics`).
- **Single event:** every event card has a *+ Koledar* button.

## Notifications for new events

- **RSS (works everywhere):** `data/feed.xml` lists events first seen in the
  last 7 days. Subscribe in any RSS reader, or wire it to email/phone push
  with a free IFTTT/Zapier "RSS → notification" applet.
- **In the browser:** the site remembers which events you've seen
  (localStorage) and shows a "N novih" badge; if you enable the 🔔 button it
  also fires a system notification on visit. Note: a purely static site
  cannot push notifications while closed — that's what the RSS feed is for.

## Source status & maintenance

| Source | Adapter | Notes |
|---|---|---|
| Kulturnik (Maribor + Dvorana Tabor + GT22 + Pekarna) | `rss` | Most reliable; also covers many **Facebook-only events** (GT22, Klub KGB, …). Dates come from `<ical:dtstart>`. |
| MC Pekarna | `html` | Server-rendered napovednik list ("Datum: sobota, 19 september 2026"). |
| GT22 Intimni oder | `html` + `follow_detail` | Category posts are undated production pages — each post page is fetched and scanned for a future date; scheduled shows also arrive via the Kulturnik GT22 feed. |
| Dvorana Gustaf | `html` (+ Kulturnik feed) | Joomla category-blog; date is in the article body. Sits behind a WAF that intermittently challenges bots, so the Kulturnik Gustaf feed is the reliable source. |
| Narodni dom | `wp_v2` | Their `dogodek` custom post type via the standard WP REST API; date/venue read from ACF fields. Also covers Vetrinjski dvor and Dvorana Union. |
| SNG Maribor | `data_attr` | The program page embeds the season as JSON in `data-events` attributes. |
| Minoriti / Lutkovno gledališče | `nuxt_payload` | Nuxt 3 site; events parsed from the `__NUXT_DATA__` payload. |
| ŠTUK | `woo_store` | WooCommerce ticket shop (public Store API); event date parsed from the product description. |
| MKC Maribor | `squarespace` | Squarespace events collection at `mkc.si/koledar?format=json`. |
| Mladi Maribor, ZPM Maribor | `tribe` | WordPress Events Calendar REST API. |
| Klub KGB, Rozmarin, UGM | `html` | CSS-selector scrape. **Verify selectors if a site redesigns** — check the Actions log; a failing source is logged, never fatal. |
| Najstarejša trta | `html` | Salient post grid; date is a custom field in `data-key="datum"` (the WP REST API does not expose it). Lists only the few big annual events (rez trte, festival, trgatev, martinovanje). |
| Rajzefiber (Festival sprehodov) | `grouped_options` | The walks exist only as checkbox options in the signup form, grouped under a date heading. The festival runs once a year, so this legitimately returns 0 outside the signup window. |
| Facebook pages (Hiša Stare trte, Poštna ulica, Piaf, Luft) | `apify` | Public Facebook event pages scraped through the [Apify](https://apify.com) Facebook Events Scraper actor. Needs the `APIFY_TOKEN` repo secret; add/remove venues via the `fb-*` entries in `sources.yaml`. Apify bills per result, so `max_events` is kept low. |
| GT22 | disabled | Site no longer publishes a program; their Facebook events surface via Kulturnik. |
| Visit Maribor | disabled | Client-side Angular app with no public JSON endpoint; largely covered by Kulturnik. |
| Facebook pages you administer | `facebook_graph` (disabled) | Alternative to Apify for pages you own: get a Graph API token, add it as the `FB_TOKEN` repo secret, and enable a `facebook_graph` source with the page ID. |

If a venue redesigns its site, only its entry in `sources.yaml` (selectors)
needs updating — check the weekly Action log for `FAILED` lines.

Scraping etiquette: one request per source per day, an identifying
User-Agent, and public program pages only. Set your repo URL in the
`UA` string in `scraper/adapters.py`.

## Moving to another town

1. Copy the repo.
2. In `sources.yaml`: change `town`, `timezone`, and replace the `sources`
   list. For each venue, pick the easiest adapter that works:
   RSS feed → `rss`; WordPress with events plugin → `tribe`; modern site →
   `jsonld` usually works; anything else → `html` with CSS selectors;
   ICS export → `ics`.
3. Run locally to test: `pip install -r requirements.txt && python -m scraper.main`
4. Push. Done — the frontend adapts automatically (town name, venues,
   calendars are all data-driven).

## Local development

```bash
pip install -r requirements.txt
python -m scraper.main          # writes docs/data/
python -m http.server -d docs   # open http://localhost:8000
```
