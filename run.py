#!/usr/bin/env python3
"""Command line entry point for the city-wise listing collector.

    python run.py run --cities Lahore,Karachi --sources olx --limit 50
    python run.py export
    python run.py stats
    python run.py verify --source zameen --city Lahore
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config, load_config  # noqa: E402
from core.db import Database  # noqa: E402
from core.http import HttpClient  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from export.excel import export_to_excel  # noqa: E402

log = logging.getLogger("collector")


def setup_logging(config: Config, verbose: bool) -> None:
    level = logging.DEBUG if verbose else getattr(logging, config.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if config.log_file:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


# --------------------------------------------------------------------- commands


def cmd_run(args, config: Config) -> int:
    date_from, date_to = args.date_from, args.date_to
    if args.today:
        date_from = date_to = datetime.now().strftime("%Y-%m-%d")

    with Database(config.database_path) as db, HttpClient(config.http, config.cache_dir) as http:
        pipeline = Pipeline(config, db, http)
        stats = pipeline.run(
            city_names=csv_list(args.cities),
            source_names=csv_list(args.sources),
            category_keys=csv_list(args.categories),
            limit=args.limit,
            date_from=date_from,
            date_to=date_to,
            keep_undated=not args.drop_undated,
        )

        print()
        print("=" * 56)
        print(f"  Inserted : {stats.inserted}")
        print(f"  Updated  : {stats.updated}")
        print(f"  Skipped  : {stats.skipped}  (failed validation)")
        if stats.filtered or stats.undated:
            print(f"  Filtered : {stats.filtered}  (outside the date range)")
            if stats.undated:
                print(f"  Undated  : {stats.undated}  (no parseable ad date)")
        print(f"  Errors   : {stats.errors}")
        if stats.per_city:
            print("  By city  :")
            for city, count in sorted(stats.per_city.items()):
                print(f"    {city:<16} {count}")
        print("=" * 56)

        if stats.incomplete:
            print("\n  NOTE: these hit a configured page/listing cap before the")
            print("  category was exhausted, so the counts above are a sample:")
            for line in stats.incomplete:
                print(f"    - {line}")
            print("  Set the relevant cap to 0 in config/settings.yml (the")
            print("  default) to read every listing instead.")

        if not args.no_export:
            path = export_to_excel(db, config.export_dir)
            print(f"\nExcel written to: {path}")

    return 0


def cmd_export(args, config: Config) -> int:
    with Database(config.database_path) as db:
        if not db.all_listings():
            print("Database is empty - run a collection first.")
            return 1
        path = export_to_excel(db, config.export_dir, filename=args.output)
        print(f"Excel written to: {path}")
    return 0


def cmd_stats(args, config: Config) -> int:
    with Database(config.database_path) as db:
        rows = db.summary_by_city()
        if not rows:
            print("No listings stored yet.")
            return 0

        header = f"{'City':<16}{'Source':<12}{'Count':>8}{'Price':>8}{'Phone':>8}{'Avg Price':>14}"
        print(header)
        print("-" * len(header))
        total = 0
        for row in rows:
            total += row["listings"]
            avg = f"{row['avg_price']:,}" if row["avg_price"] else "-"
            print(
                f"{row['city']:<16}{row['source']:<12}{row['listings']:>8}"
                f"{row['with_price']:>8}{row['with_phone']:>8}{avg:>14}"
            )
        print("-" * len(header))
        print(f"{'TOTAL':<28}{total:>8}")
    return 0


def cmd_verify(args, config: Config) -> int:
    """Fetch one index page per source and report what actually parsed.

    Classified sites change their markup regularly. Run this first after any
    break to see which extraction path is still working before starting a
    full collection.
    """
    from scrapers import build_collector

    source_names = csv_list(args.source) or list(config.sources)
    city_name = args.city or config.enabled_cities()[0].name
    city = next(c for c in config.cities if c.name.lower() == city_name.lower())

    exit_code = 0
    with Database(config.database_path) as db, HttpClient(config.http, config.cache_dir) as http:
        for source_name in source_names:
            source = config.sources.get(source_name)
            if not source:
                print(f"[{source_name}] unknown source")
                exit_code = 1
                continue

            print(f"\n--- {source_name} / {city.name} ---")
            collector = build_collector(source_name, config, http, db)

            identifier = collector.resolve_city(city)
            if not identifier:
                print("  location   : NOT RESOLVED - set it in config/cities.yml")
                exit_code = 1
                continue
            print(f"  location   : {identifier}")

            category = source.categories[0]
            index_url = next(collector.index_urls(city, identifier, category))
            print(f"  index url  : {index_url}")

            try:
                response = http.get(index_url)
            except Exception as exc:
                print(f"  fetch      : FAILED - {exc}")
                exit_code = 1
                continue
            print(f"  fetch      : HTTP {response.status} ({len(response.text):,} bytes)")

            stubs = collector.parse_index(response, city, category)
            print(f"  parsed     : {len(stubs)} listings")
            if not stubs:
                print("  -> index selectors need updating for this source")
                exit_code = 1
                continue

            sample = collector._complete(stubs[0])
            print(f"  sample id  : {sample.source_listing_id}")
            print(f"  title      : {sample.title[:70]}")
            print(f"  price      : {sample.price} ({sample.price_raw[:30]})")
            print(f"  ad_date    : {sample.ad_date}")
            print(f"  desc chars : {len(sample.description)}")
            print(f"  phone      : {sample.phone or '(none public)'}")
            problems = sample.validate()
            print(f"  validation : {'OK' if not problems else '; '.join(problems)}")

    return exit_code


def cmd_sync_categories(args, config: Config) -> int:
    """Pull OLX's full category list from its published sitemap."""
    from core.categories import sync_olx_categories

    source = config.sources.get("olx")
    if source is None:
        print("no 'olx' source configured", file=sys.stderr)
        return 2

    with HttpClient(config.http, config.cache_dir) as http:
        categories = sync_olx_categories(http, source.base_url, config.root / "config")

    print(f"Synced {len(categories)} OLX categories to config/olx_categories.json")
    print("\nFirst 15:")
    for entry in categories[:15]:
        print(f"   {entry['key']:<38} {entry['label']}")
    print(f"\nRun `python run.py run --sources olx --categories <key>` to use one.")
    return 0


def cmd_clean_text(args, config: Config) -> int:
    """Re-normalise stored titles and descriptions.

    Rows collected before the markup stripping went in still hold raw HTML -
    Zameen writes `<br />` between lines. This rewrites them in place.
    """
    from core.normalize import clean_text

    with Database(config.database_path) as db:
        rows = db.conn.execute(
            "SELECT id, title, description FROM listings "
            "WHERE title LIKE '%<%' OR description LIKE '%<%' "
            "   OR title LIKE '%&%;%' OR description LIKE '%&%;%'"
        ).fetchall()

        if not rows:
            print("Nothing to clean - every stored title and description is plain text.")
            return 0

        changed = 0
        with db.conn:
            for row in rows:
                title = clean_text(row["title"])
                description = clean_text(row["description"])
                if title != row["title"] or description != (row["description"] or ""):
                    db.conn.execute(
                        "UPDATE listings SET title = ?, description = ? WHERE id = ?",
                        (title, description, row["id"]),
                    )
                    changed += 1

        print(f"Cleaned {changed} of {len(rows)} candidate rows.")
    return 0


def cmd_init_db(args, config: Config) -> int:
    with Database(config.database_path) as db:
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"Database ready at {config.database_path}")
        print("Tables: " + ", ".join(t["name"] for t in tables))
    return 0


# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="City-wise listing collector for OLX Pakistan, PakWheels and Zameen.",
    )
    parser.add_argument("--config-root", help="directory holding config/ (default: project root)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="collect listings")
    run_cmd.add_argument("--cities", help="comma separated, e.g. Lahore,Karachi (default: all enabled)")
    run_cmd.add_argument("--sources", help="comma separated: olx,pakwheels,zameen")
    run_cmd.add_argument("--categories", help="comma separated category keys")
    run_cmd.add_argument("--limit", type=int, help="max listings per city+category")
    run_cmd.add_argument("--date-from", help="keep ads posted on/after this date (YYYY-MM-DD)")
    run_cmd.add_argument("--date-to", help="keep ads posted on/before this date (YYYY-MM-DD)")
    run_cmd.add_argument("--today", action="store_true", help="shorthand for today's ads only")
    run_cmd.add_argument(
        "--drop-undated", action="store_true",
        help="with a date filter, also drop ads whose date could not be parsed",
    )
    run_cmd.add_argument("--no-export", action="store_true", help="skip the Excel export")
    run_cmd.set_defaults(func=cmd_run)

    sync_cmd = sub.add_parser(
        "sync-categories", help="pull OLX's full category list from its sitemap"
    )
    sync_cmd.set_defaults(func=cmd_sync_categories)

    export_cmd = sub.add_parser("export", help="write the database out to Excel")
    export_cmd.add_argument("--output", help="output filename")
    export_cmd.set_defaults(func=cmd_export)

    stats_cmd = sub.add_parser("stats", help="show what is stored")
    stats_cmd.set_defaults(func=cmd_stats)

    verify_cmd = sub.add_parser("verify", help="check selectors against live pages")
    verify_cmd.add_argument("--source", help="comma separated sources (default: all)")
    verify_cmd.add_argument("--city", help="city to test with (default: first enabled)")
    verify_cmd.set_defaults(func=cmd_verify)

    clean_cmd = sub.add_parser(
        "clean-text", help="strip leftover HTML from stored titles and descriptions"
    )
    clean_cmd.set_defaults(func=cmd_clean_text)

    init_cmd = sub.add_parser("init-db", help="create the database and indexes")
    init_cmd.set_defaults(func=cmd_init_db)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config, args.verbose)

    try:
        return args.func(args, config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted - partial results are already committed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
