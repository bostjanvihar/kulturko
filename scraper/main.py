"""Run the full pipeline:  python -m scraper.main

1. Load sources.yaml
2. Run every enabled source adapter (failures are logged, not fatal)
3. Deduplicate across sources
4. Preserve `first_seen` timestamps from the previous events.json
   (this powers the "new events" RSS feed and in-app notifications)
5. Write events.json, ICS calendars, feed.xml into docs/data/
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .adapters import run_source
from .categories import assign_categories
from .dedupe import dedupe
from .outputs import write_outputs

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("kulturko")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"


def main():
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    log.info("Kulturko — %s — %s", cfg["town"],
             datetime.now().strftime("%Y-%m-%d %H:%M"))

    # previous run, for first_seen continuity
    prev_seen = {}
    prev_file = OUT / "events.json"
    if prev_file.exists():
        try:
            for e in json.loads(prev_file.read_text(encoding="utf-8"))["events"]:
                prev_seen[e["id"]] = e.get("first_seen")
        except (json.JSONDecodeError, KeyError):
            pass

    all_events = []
    for src in cfg["sources"]:
        all_events.extend(run_source(src))

    log.info("collected: %d raw events", len(all_events))
    events = dedupe(all_events)
    assign_categories(events)
    log.info("after dedup: %d unique events", len(events))

    # window filter
    now = datetime.now()
    lo = now - timedelta(days=int(cfg.get("keep_past_days", 7)))
    hi = now + timedelta(days=int(cfg.get("horizon_days", 365)))
    events = [e for e in events if e.start_dt and lo <= e.start_dt <= hi]

    stamp = now.isoformat(timespec="seconds")
    for e in events:
        e.first_seen = prev_seen.get(e.id) or stamp

    if not events and prev_seen:
        log.error("All sources returned nothing — keeping previous data.")
        sys.exit(1)

    write_outputs(events, cfg, OUT)
    log.info("wrote %d events -> %s", len(events), OUT)


if __name__ == "__main__":
    main()
