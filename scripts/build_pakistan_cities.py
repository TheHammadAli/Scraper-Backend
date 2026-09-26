#!/usr/bin/env python3
"""Build config/pakistan_cities.json - the master list behind the city picker.

The list is the union of three independent sources, so it is both complete and
scrapable:

  * GeoNames (https://www.geonames.org, CC BY 4.0) - district / tehsil
    headquarters plus every place of 50,000+ people, with province and
    coordinates. This is what makes the list cover all seven regions.
  * OLX's published locations sitemap - every city OLX has a page for.
  * Zameen's own city list (embedded in its pages) - every city Zameen has.

Cities are matched across sources by a spelling-insensitive key; the handful of
genuine spelling differences (Mingora / Mingaora ...) are listed in NAME_MERGES.

Usage:
    python scripts/build_pakistan_cities.py                 # rebuild the file
    python scripts/build_pakistan_cities.py --report        # + review output
    python scripts/build_pakistan_cities.py --probe-pakwheels   # + PakWheels coverage
                                                            #   (one request per city)
    python scripts/build_pakistan_cities.py --keep-pakwheels    # rebuild, reuse that snapshot
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from core.config import load_config  # noqa: E402
from core.http import FetchError, HttpClient  # noqa: E402
from core.locations import (  # noqa: E402
    PROVINCES,
    city_keys,
    match_key,
    pakwheels_page_city,
    parse_olx_locations,
    parse_zameen_cities,
    pick_location,
    url_slug,
)

log = logging.getLogger("build_cities")

OUT = ROOT / "config" / "pakistan_cities.json"
GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"

# Admin seats (provincial / district / tehsil HQs) are always kept; ordinary
# places only above this population. GeoNames also lists many suburbs,
# numbered villages and housing societies, which are not what anyone means by
# "city".
MIN_POPULATION = 50_000
ADMIN_SEAT_CODES = {"PPLC", "PPLA", "PPLA2", "PPLA3"}

# GeoNames admin1 codes for Pakistan (admin1CodesASCII.txt).
GEONAMES_PROVINCE = {
    "PK.08": "Islamabad Capital Territory",
    "PK.05": "Sindh",
    "PK.04": "Punjab",
    "PK.03": "Khyber Pakhtunkhwa",
    "PK.07": "Gilgit-Baltistan",
    "PK.02": "Balochistan",
    "PK.06": "Azad Jammu & Kashmir",
}

# canonical name -> other spellings the three sources use for the same place.
NAME_MERGES: dict[str, list[str]] = {
    "Ahmadpur East": ["Ahmedpur East"],
    "Battagram": ["Batagram"],
    "Depalpur": ["Dipalpur"],
    "Dunyapur": ["Duniya Pur"],
    "Hasan Abdal": ["Hassan Abdal"],
    "Hujra Shah Muqeem": ["Hujra Shah Muqim"],
    "Jhang": ["Jhang Sadar", "Jhang Sadr"],
    "Mian Channu": ["Mian Channun", "Mian Chunnu"],
    "Mingora": ["Mingaora"],
    "Muridke": ["Muridike"],
    "Naushahro Feroze": ["Naushahro Firoz"],
    "Sadiqabad": ["Saddiqabad"],
    "Sheikhupura": ["Shekhupura"],
    "Chishtian": ["Chishtian Mandi"],
    "Umerkot": ["Umarkot"],
    "Kamoke": ["Kamoki"],
    "Nawabshah": ["Shaheed Benazirabad"],
    "Wah": ["Wah Cantt", "Wah Cantonment"],
    "Attock": ["Attock City"],
    "Daska": ["Daska Kalan"],
    "Malakwal": ["Malakwal City"],
    "Hazro": ["Hazro City"],
    "Nowshera": ["Nowshera Kalan", "Nowshera Cantonment"],
    "Mirpur": ["New Mirpur City"],
    "Khairpur": ["Khairpur Mir's", "Khairpur Mirs"],
    "Haroonabad": ["Harunabad"],
    "Vehari": ["Vihari"],
    "Risalpur": ["Risalpur Cantonment"],
    "Hub": ["Hub Chowki"],
}

# Well-known alternate names people type - only for search, not for matching.
ALTERNATE_NAMES: dict[str, list[str]] = {
    "Rawalpindi": ["Pindi"],
    "Faisalabad": ["Lyallpur"],
    "Dera Ghazi Khan": ["DG Khan", "D.G. Khan"],
    "Dera Ismail Khan": ["DI Khan", "D.I. Khan"],
    "Rahim Yar Khan": ["RYK", "Rahimyar Khan"],
    "Islamabad": ["ICT"],
    "Sheikhupura": ["Sheikhupura City"],
    "Mirpur": ["Mirpur AJK", "Mirpur Azad Kashmir"],
    "Gilgit": ["Gilgit City"],
    "Kotli": ["Kotli AJK", "Kotli Azad Kashmir"],
    "Quetta": ["Kwatah"],
    "Bahawalpur": ["Bahawalpur City"],
    "Hyderabad": ["Hyderabad Sindh"],
}

# Entries that are artefacts, suburbs of a bigger city, or regions rather than
# a city anyone would pick. (Matched on match_key.)
EXCLUDE = {
    "malal", "fata", "waziristan", "makran",       # Zameen artefacts / tribal regions
    "modeltown", "sharifabad", "malircantonment",   # suburbs of Lahore / Karachi
    "kurkalisindhpakistan",                         # malformed GeoNames name
}


def is_junk_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("chak ")             # numbered canal-colony villages
        or lowered == "chak"
        or "housing society" in lowered
        or "employees" in lowered
    )

# Same-named places in different provinces need a distinct display name.
DISPLAY_NAME_OVERRIDES = {("kotli", "Punjab"): "Kotli Loharan"}


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    dest.write_bytes(response.content)
    return dest


def load_geonames(cache: Path) -> list[dict]:
    archive = download(GEONAMES_URL, cache / "cities5000.zip")

    with zipfile.ZipFile(archive) as z:
        rows = [line.split("\t") for line in z.read("cities5000.txt").decode("utf-8").splitlines()]

    places = []
    for r in rows:
        if r[8] != "PK":
            continue
        population = int(r[14] or 0)
        if r[7] not in ADMIN_SEAT_CODES and population < MIN_POPULATION:
            continue
        places.append(
            {
                "name": r[2],
                "province": GEONAMES_PROVINCE.get(f"PK.{r[10]}"),
                "lat": round(float(r[4]), 4),
                "lon": round(float(r[5]), 4),
                "population": population,
            }
        )
    return places


def build(args) -> dict:
    config = load_config()
    cache = config.cache_dir / "geonames"

    geo = load_geonames(cache)
    log.info("GeoNames places kept: %s", len(geo))

    with HttpClient(config.http, config.cache_dir) as http:
        olx_xml = http.get(f"{config.sources['olx'].base_url}/sitemap/searches/locations.xml").text
        zameen_html = http.get(config.sources["zameen"].base_url).text

    olx = parse_olx_locations(olx_xml)
    zameen = parse_zameen_cities(zameen_html)
    log.info("OLX locations: %s (city-level %s) | Zameen cities: %s",
             len(olx), sum(1 for o in olx if o.level == "city"), len(zameen))
    if not olx or not zameen:
        raise SystemExit("a site returned no locations - refusing to write a partial list")

    # Map every spelling to one canonical key.
    key_alias: dict[str, str] = {}
    for canonical, variants in NAME_MERGES.items():
        for variant in variants:
            key_alias[match_key(variant)] = match_key(canonical)

    def ckey(name: str) -> str:
        key = match_key(name)
        return key_alias.get(key, key)

    entries: dict[str, dict] = {}

    def entry(name: str, province: str | None) -> dict:
        key = ckey(name)
        # OLX/Zameen both hold two "Kotli"s in different provinces - keep apart.
        if (key, province) in DISPLAY_NAME_OVERRIDES:
            key = f"{key}-{match_key(province or '')}"
        return entries.setdefault(
            key, {"names": {}, "provinces": {}, "lat": None, "lon": None, "population": 0}
        )

    for g in geo:
        if is_junk_name(g["name"]) or match_key(g["name"]) in EXCLUDE:
            continue
        e = entry(g["name"], g["province"])
        e["names"]["geonames"] = g["name"]
        e["provinces"]["geonames"] = g["province"]
        e["lat"], e["lon"], e["population"] = g["lat"], g["lon"], g["population"]

    for o in olx:
        if o.level != "city":
            continue
        e = entry(o.name, o.province)
        e["names"]["olx"] = o.name
        e["provinces"]["olx"] = o.province

    for z in zameen:
        if match_key(z.name) in EXCLUDE:
            continue
        e = entry(z.name, z.province)
        e["names"]["zameen"] = z.name
        e["provinces"]["zameen"] = z.province

    # --- assemble ---------------------------------------------------------
    cities = []
    conflicts = []
    for key, e in entries.items():
        # OLX and Zameen name the very location the scraper will search, so
        # they outrank GeoNames, which was only matched to them by name (there
        # are several towns called Nasirabad, Shorkot, Arifwala ...).
        site_provinces = [e["provinces"].get(s) for s in ("olx", "zameen") if e["provinces"].get(s)]
        province = (
            Counter(site_provinces).most_common(1)[0][0] if site_provinces
            else e["provinces"].get("geonames")
        )
        if province not in PROVINCES:
            conflicts.append(f"{key}: no usable province {e['provinces']}")
            continue
        geonames_province = e["provinces"].get("geonames")
        if geonames_province and geonames_province != province:
            conflicts.append(
                f"{key}: GeoNames says {geonames_province}, sites say {province} - "
                f"GeoNames match discarded (different town of the same name)"
            )
            e["lat"], e["lon"], e["population"] = None, None, 0

        canonical = next(
            (c for c in NAME_MERGES if match_key(c) == key.split("-")[0] or match_key(c) == key), None
        )
        name = (
            canonical
            or DISPLAY_NAME_OVERRIDES.get((key.split("-")[0], province))
            or e["names"].get("zameen")
            or e["names"].get("olx")
            or e["names"]["geonames"]
        )
        aliases = {n for n in e["names"].values() if n and match_key(n) != match_key(name)}
        aliases.update(NAME_MERGES.get(name, []))
        aliases.update(ALTERNATE_NAMES.get(name, []))
        aliases = sorted(a for a in aliases if match_key(a) != match_key(name))

        keys = city_keys(name, aliases)
        cities.append(
            {
                "name": name,
                "province": province,
                "aliases": aliases,
                "lat": e["lat"],
                "lon": e["lon"],
                "population": e["population"],
                "coverage": {
                    "olx": pick_location([o for o in olx], keys, province) is not None,
                    "zameen": pick_location(zameen, keys, province) is not None,
                    "pakwheels": None,
                },
            }
        )

    # Zameen carries coordinates too - use them where GeoNames had none.
    zam_geo = {}
    for entry_ in _zameen_geo(zameen_html):
        zam_geo[match_key(entry_["name"])] = entry_
    for c in cities:
        if c["lat"] is None:
            hit = zam_geo.get(match_key(c["name"]))
            if hit:
                c["lat"], c["lon"] = hit["lat"], hit["lon"]

    # Biggest first: a multi-city run then does the busiest places first.
    cities.sort(key=lambda c: (-c["population"], c["name"]))

    names = Counter(c["name"].lower() for c in cities)
    dup = [n for n, k in names.items() if k > 1]
    if dup:
        raise SystemExit(f"duplicate display names would be ambiguous: {dup}")

    dropped: list[str] = []
    if args.probe_pakwheels:
        probe_pakwheels(cities, config)
        # A city none of the three sites has cannot be scraped, so listing it
        # would only promise something the scraper cannot do. Recorded in the
        # file's metadata so the omission is visible, not silent.
        dropped = [c["name"] for c in cities if all(v is False for v in c["coverage"].values())]
        cities = [c for c in cities if not all(v is False for v in c["coverage"].values())]
    elif args.keep_pakwheels and OUT.exists():
        # Re-derive OLX/Zameen coverage without re-probing PakWheels (one
        # request per city): reuse the snapshot from the existing file.
        previous = json.loads(OUT.read_text(encoding="utf-8"))
        known = {c["name"]: c["coverage"].get("pakwheels") for c in previous["cities"]}
        dropped = list(previous["_meta"].get("dropped_uncovered", []))
        known.update({name: False for name in dropped})   # they were probed, and PakWheels had none
        for c in cities:
            c["coverage"]["pakwheels"] = known.get(c["name"])
        keep = [c for c in cities if not all(v is False for v in c["coverage"].values())]
        dropped += [c["name"] for c in cities if c not in keep and c["name"] not in dropped]
        cities = keep

    if args.report:
        report(cities, conflicts)

    return {
        "_meta": {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "count": len(cities),
            "sources": [
                "GeoNames (cities5000, https://www.geonames.org) - CC BY 4.0",
                "OLX Pakistan locations sitemap",
                "Zameen.com city list",
            ],
            "attribution": "Place names, provinces and coordinates in part from GeoNames "
                           "(https://www.geonames.org), licensed CC BY 4.0.",
            "coverage_note": "coverage.<site> is a snapshot at build time (true/false); "
                             "null means not checked. Runs always resolve live.",
            "dropped_uncovered": dropped,
        },
        "cities": cities,
    }


def _zameen_geo(html: str) -> list[dict]:
    from core.jsonblob import extract_window_json

    state = extract_window_json(html, "state") or {}
    out = []
    for c in ((state.get("cities") or {}).get("data")) or []:
        geo = c.get("geography") or {}
        if c.get("name") and "lat" in geo and "lng" in geo:
            out.append({"name": c["name"], "lat": round(geo["lat"], 4), "lon": round(geo["lng"], 4)})
    return out


def probe_pakwheels(cities: list[dict], config) -> None:
    """One request per city: does PakWheels scope a page to it?"""
    base = config.sources["pakwheels"].base_url
    with HttpClient(config.http, config.cache_dir) as http:
        for i, c in enumerate(cities, 1):
            keys = city_keys(c["name"], c["aliases"])
            slugs = [url_slug(n) for n in (c["name"], *c["aliases"])][:3]
            found = False
            for slug in dict.fromkeys(slugs):
                try:
                    html = http.peek(f"{base}/used-cars/search/-/ct_{slug}/?page=1").text
                except FetchError:
                    continue
                scoped = pakwheels_page_city(html)
                if scoped and match_key(scoped) in keys:
                    found = True
                    break
            c["coverage"]["pakwheels"] = found
            if i % 25 == 0:
                log.info("PakWheels probe %s/%s", i, len(cities))


def report(cities: list[dict], conflicts: list[str]) -> None:
    print(f"\n=== {len(cities)} cities ===")
    by_province = Counter(c["province"] for c in cities)
    for p in PROVINCES:
        print(f"  {p:30} {by_province.get(p, 0)}")
    for site in ("olx", "zameen", "pakwheels"):
        vals = Counter(c["coverage"][site] for c in cities)
        print(f"  coverage {site:10} true={vals.get(True, 0)} false={vals.get(False, 0)} unchecked={vals.get(None, 0)}")
    none = [c["name"] for c in cities if not any(c["coverage"][s] for s in ("olx", "zameen"))]
    print(f"\n  in neither OLX nor Zameen ({len(none)}): {none[:60]}{' ...' if len(none) > 60 else ''}")
    print("\n  province conflicts / gaps:")
    for line in conflicts or ["    none"]:
        print("   ", line)
    keys = sorted({match_key(c["name"]) for c in cities})
    print("\n  near-duplicate names (review - add to NAME_MERGES if they are one place):")
    seen = set()
    for a in keys:
        for b in difflib.get_close_matches(a, keys, n=3, cutoff=0.82):
            if a < b and (a, b) not in seen:
                seen.add((a, b))
                print("    ", a, "~", b)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--probe-pakwheels", action="store_true")
    parser.add_argument("--keep-pakwheels", action="store_true",
                        help="reuse PakWheels coverage from the existing file instead of re-probing")
    parser.add_argument("--dry-run", action="store_true", help="build and report, do not write")
    args = parser.parse_args()

    data = build(args)
    if args.dry_run:
        print(f"dry run: {data['_meta']['count']} cities, nothing written")
        return
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {data['_meta']['count']} cities -> {OUT}")


if __name__ == "__main__":
    main()
