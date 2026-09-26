#!/usr/bin/env python3
"""Live city matrix: does every city really work against the real sites?

Not part of `unittest discover` (it needs the network and takes minutes). Run:

    python tests/live_city_matrix.py                 # resolve + fetch pages 1 and 2
    python tests/live_city_matrix.py --store         # also run a real capped scrape
    python tests/live_city_matrix.py --cities Larkana,Gilgit --sources olx
    python tests/live_city_matrix.py --json report.json

Per city x website it checks:

  1  the city is in what /api/config sends to Step 3, and a partial name finds it
  2  it can be planned as a run unit (handed to the scraper)
  3  a location/search request is generated, and its URL is clean
  4  the site accepts it (page scoped to that city; a missing page is reported
     as UNSUPPORTED, an unreachable one as FAILED, an empty one as EMPTY)
  5  the page holds ads
  6  pagination: page 2 brings ads page 1 did not have (or the city's ads end)
  7  NO OTHER CITY'S ADS ARE KEPT - each kept ad's own location is read
     independently of the collector and compared with the city searched
  8  --store: the ads are stored under the requested city, none under another

It uses the project's polite HTTP client (robots.txt, rate limits, cache).
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from core.db import Database  # noqa: E402
from core.http import FetchError, HttpClient  # noqa: E402
from core.jsonblob import extract_window_json  # noqa: E402
from core.locations import match_key  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from scrapers import build_collector  # noqa: E402

MATRIX = {
    "Punjab": ["Lahore", "Rawalpindi", "Faisalabad", "Multan", "Chakwal", "Sialkot",
               "Gujranwala", "Bahawalpur", "Sargodha"],
    "Sindh": ["Karachi", "Hyderabad", "Sukkur", "Larkana"],
    "Khyber Pakhtunkhwa": ["Peshawar", "Abbottabad", "Mardan", "Mingora"],
    "Balochistan": ["Quetta", "Gwadar", "Turbat"],
    "Islamabad Capital Territory": ["Islamabad"],
    "Azad Jammu & Kashmir": ["Muzaffarabad", "Mirpur"],
    "Gilgit-Baltistan": ["Gilgit", "Skardu"],
}

CLEAN_URL = re.compile(r"^https://[a-z0-9.\-]+/[^\s%]*$")
PW_CITY = re.compile(r"-for-sale-in-(.+?)-\d{5,}(?:[/?#]|$)", re.I)

# id -> the place an ad says it is in, filled while pages are read, used again
# when stored rows are checked.
PLACE_OF: dict[str, dict[str, str]] = {"olx": {}, "zameen": {}, "pakwheels": {}}


def independent_places(source: str, html: str, stubs) -> dict[str, str]:
    """Where each ad says it is - read straight from the page, NOT through the
    collector's own located_in, so the check is not marking its own homework."""
    places: dict[str, str] = {}
    if source == "pakwheels":
        for stub in stubs:
            m = PW_CITY.search(stub.url)
            places[stub.source_listing_id] = m.group(1).replace("-", " ") if m else ""
    else:
        state = extract_window_json(html, "state") or {}
        for hit in ((state.get("algolia") or {}).get("content") or {}).get("hits") or []:
            names = [e.get("name", "") for e in (hit.get("location") or [])
                     if isinstance(e, dict) and e.get("level", -1) >= 2]
            places[str(hit.get("externalID") or hit.get("id"))] = " | ".join(names)
    PLACE_OF[source].update(places)
    return places


def in_city(label: str, city) -> bool | None:
    """True/False, or None if the page did not say."""
    parts = [p.strip() for p in label.split("|") if p.strip()]
    if not parts:
        return None
    return any(match_key(p) in city.keys for p in parts)


def check_page(source, collector, city, category, http, url, page_no, row):
    """Fetch a page; return (kept stubs, raw count, status or None)."""
    try:
        response = http.get(url)
    except FetchError as exc:
        if exc.status == 404 and page_no == 1:
            row["status"] = "UNSUPPORTED"
            row["notes"].append(f"no {category.label} page for {city.name} (HTTP 404) - not a failure")
            return None, 0, "stop"
        if exc.status == 404:
            row["notes"].append("page 2 is a 404: the category ends on page 1")
            return [], 0, None
        row["status"] = "FAILED"
        row["notes"].append(f"page {page_no}: {exc}")
        return None, 0, "stop"

    if page_no == 1:
        issue = collector.validate_index(response, city, category)
        if issue:
            row["status"] = "UNSUPPORTED"
            row["notes"].append(issue)
            return None, 0, "stop"

    raw = collector.parse_index(response, city, category)
    kept, others = collector.split_by_city(raw, city)
    places = independent_places(source, response.text, raw)

    verdicts = [in_city(places.get(s.source_listing_id, ""), city) for s in kept]
    wrong = sum(1 for v in verdicts if v is False)
    unknown = sum(1 for v in verdicts if v is None)
    row[f"p{page_no}"] = f"{len(kept)} kept, {len(others)} other-place ads left out"
    row[f"others_p{page_no}"] = len(others)
    if wrong:
        row["checks"][f"no_mixing_p{page_no}"] = False
        row["notes"].append(f"page {page_no}: {wrong} kept ad(s) are NOT in {city.name}")
    else:
        row["checks"][f"no_mixing_p{page_no}"] = True
    if unknown and kept:
        row["notes"].append(f"page {page_no}: {unknown} kept ad(s) did not say where they are")
    return kept, len(raw), None


def check(source, collector, city, category, http) -> dict:
    row = {"city": city.name, "source": source, "checks": {}, "notes": []}
    ok = row["checks"]

    location = collector.resolve_city(city)
    snapshot = city.coverage.get(source)

    if location.status == "unsupported":
        row["status"] = "UNSUPPORTED"
        row["notes"].append(location.reason)
        ok["snapshot_agrees"] = snapshot is not True
        return finish(row)
    if location.status == "error":
        row["status"] = "FAILED"
        row["notes"].append(location.reason)
        return finish(row)

    ok["snapshot_agrees"] = snapshot is not False
    urls = collector.index_urls(city, location.identifier, category)
    page1_url = next(urls)
    row["search_url"] = page1_url
    ok["url_clean"] = bool(CLEAN_URL.match(page1_url))

    kept1, raw1, stop = check_page(source, collector, city, category, http, page1_url, 1, row)
    if stop:
        return finish(row)
    if raw1 == 0:
        row["status"] = "EMPTY"
        row["notes"].append("the site returned no ads for this city/category (not a failure)")
        return finish(row)
    if not kept1:
        row["status"] = "EMPTY"
        row["notes"].append(f"0 ads in {city.name}: page 1 held only other places' ads (left out)")
        return finish(row)

    page2_url = next(itertools.islice(urls, 0, 1))
    kept2, _, stop = check_page(source, collector, city, category, http, page2_url, 2, row)
    if stop:
        return finish(row)
    if kept2:
        ids1 = {s.source_listing_id for s in kept1}
        new = [s for s in kept2 if s.source_listing_id not in ids1]
        if new:
            ok["pagination"] = True
            row["notes"].append(f"page 2 brings {len(new)} new ads ({len(kept2) - len(new)} repeat)")
        elif row.get("others_p1"):
            # The site filled a short page with other places' ads / repeated page 1:
            # the city's own ads fit on one page, and the collector stops (2 stale pages).
            ok["pagination"] = True
            row["notes"].append("page 2 only repeats page 1 - this city's ads fit on one page")
        else:
            ok["pagination"] = False
            row["notes"].append("page 2 repeats a FULL page 1 - pagination looks broken")
    else:
        row["notes"].append("page 2 has none of this city's ads - the city's ads end on page 1")

    return finish(row)


def finish(row):
    if "status" not in row:
        row["status"] = "PASS" if all(row["checks"].values()) else "FAIL"
    return row


def store_check(config, http, source, cities, category) -> list[dict]:
    """A real capped run over every city at once, into a throwaway database."""
    with tempfile.TemporaryDirectory() as tmp:
        with Database(Path(tmp) / "matrix.db") as db:
            results: list[dict] = []
            stats = Pipeline(config, db, http).run(
                city_names=[c.name for c in cities], source_names=[source],
                category_keys=[category.key], limit=2, on_city_result=results.append,
            )
            rows = db.conn.execute("SELECT city, source_listing_id FROM listings").fetchall()

    by_city: dict[str, list[str]] = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r["source_listing_id"])
    wanted = {c.name: c for c in cities}
    reported = {r["city"]: r for r in results}

    out = []
    for name, city in wanted.items():
        r = reported.get(name)
        if r is None:
            out.append({"city": name, "source": source, "store": "MISSING - silently skipped", "ok": False})
            continue
        ids = by_city.get(name, [])
        wrong = [i for i in ids if in_city(PLACE_OF[source].get(i, ""), city) is False]
        consistent = (r["status"] != "completed") or len(ids) == r["listings"]
        out.append({
            "city": name, "source": source, "store": r["status"], "reported": r["listings"],
            "in_db": len(ids), "other_city_ads_in_db": len(wrong), "left_out": r["foreign_ads"],
            "ok": consistent and not wrong, "reason": r["reason"][:90],
        })
    stray = set(by_city) - set(wanted)
    if stray:
        out.append({"city": "*", "source": source, "store": f"rows under unrequested cities {stray}", "ok": False})
    out.append({"city": "(run)", "source": source,
                "store": f"errors={stats.errors} unsupported={stats.unsupported}", "ok": stats.errors == 0})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cities")
    ap.add_argument("--sources")
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    config = load_config()
    wanted_names = ([n.strip() for n in args.cities.split(",")] if args.cities
                    else [n for names in MATRIX.values() for n in names])
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else list(config.sources)
    region = {n: p for p, names in MATRIX.items() for n in names}

    import main as api

    sent = {c["name"] for c in api.get_config()["cities"]}
    front = {}
    for name in wanted_names:
        city = config.find_city(name)
        partial = name[1:5].lower() if len(name) > 5 else name.lower()
        front[name] = {
            "in_step3": name in sent,
            "partial_search": bool(city) and match_key(partial) in match_key(city.name),
        }

    report: list[dict] = []
    stores: list[dict] = []
    with HttpClient(config.http, config.cache_dir) as http:
        for source in sources:
            category = config.sources[source].categories[0]
            collector = build_collector(source, config, http, None)
            print(f"\n=== {source} / {category.label} ===", flush=True)
            for name in wanted_names:
                city = config.find_city(name)
                if city is None:
                    report.append({"city": name, "source": source, "status": "MISSING FROM LIST"})
                    print(f"  {name:<13} MISSING FROM CITY LIST", flush=True)
                    continue
                units = Pipeline(config, None, http).plan([name], None, None, {source: [category.key]})
                row = check(source, collector, city, category, http)
                row["checks"]["planned"] = len(units) == 1 and units[0][0].name == city.name
                row["checks"].update(front[name])
                if row["status"] == "PASS" and not all(row["checks"].values()):
                    row["status"] = "FAIL"
                report.append(row)
                detail = f"p1[{row.get('p1', '-')}] p2[{row.get('p2', '-')}]"
                print(f"  {name:<13} {row['status']:<12} {detail}  {'; '.join(row['notes'])[:120]}", flush=True)

        if args.store:
            for source in sources:
                cities = [config.find_city(n) for n in wanted_names if config.find_city(n)]
                print(f"\n--- stored run: {source} (limit 2 per city) ---", flush=True)
                for r in store_check(config, http, source, cities, config.sources[source].categories[0]):
                    stores.append(r)
                    print("  ", {k: v for k, v in r.items() if k != "source"}, flush=True)

    print("\n\n=== SUMMARY ===")
    header = f"{'Region':<28}{'City':<13}" + "".join(f"{s:<13}" for s in sources)
    print(header)
    print("-" * len(header))
    bad = 0
    for name in wanted_names:
        cells = []
        for s in sources:
            row = next((r for r in report if r["city"] == name and r["source"] == s), None)
            status = row["status"] if row else "-"
            bad += status in ("FAIL", "FAILED", "MISSING FROM LIST")
            cells.append(f"{status:<13}")
        print(f"{region.get(name, ''):<28}{name:<13}" + "".join(cells))

    tally: dict[str, int] = {}
    for r in report:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    print("\nTotals:", tally)
    if args.store:
        stored_bad = [r for r in stores if not r.get("ok", True)]
        bad += len(stored_bad)
        print(f"Stored-run problems: {len(stored_bad)}", stored_bad[:5])

    if args.json:
        Path(args.json).write_text(json.dumps({"rows": report, "store": stores, "front": front}, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
