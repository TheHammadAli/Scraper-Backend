"""Loads cities.yml and settings.yml into typed objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .categories import load_synced_categories
from .normalize import city_slug


@dataclass(frozen=True)
class City:
    name: str
    enabled: bool = True
    province: str = ""
    olx_location_id: str | None = None
    zameen_slug: str | None = None
    pakwheels_slug: str | None = None

    def slug_for(self, source: str) -> str | None:
        """The site-specific identifier, or None if it must be resolved live."""
        if source == "olx":
            return self.olx_location_id
        if source == "zameen":
            return self.zameen_slug
        if source == "pakwheels":
            return self.pakwheels_slug or city_slug(self.name)
        return None


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    path: str
    category_id: str | None = None
    curated: bool = False  # listed by hand in settings.yml, not auto-synced
    group: str = ""        # the site's own top-level section, e.g. "Mobiles"
    level: int = 0         # 0 = section itself, 1+ = nested under it
    priority: int = 0      # the site's own display ordering


@dataclass(frozen=True)
class Source:
    name: str
    enabled: bool
    base_url: str
    categories: list[Category]

    def category(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)


@dataclass(frozen=True)
class HttpSettings:
    user_agent: str
    contact_email: str = ""
    timeout_seconds: int = 30
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    delay_seconds: float = 3.0
    jitter_seconds: float = 2.0
    respect_robots: bool = True
    cache_responses: bool = True
    cache_ttl_hours: int = 24


@dataclass(frozen=True)
class CollectionSettings:
    """Page and listing budgets.

    Every budget below follows one rule: 0 means no cap - keep paging until
    the site itself says the category is exhausted (an index page parses to
    zero listings). That is what "collect everything" actually requires:
    none of these three sites order a category page in a way a fixed sample
    can be trusted against, so ANY positive cap is a sample, not a total,
    even outside a date filter. See scrapers/base.py's collect() and the OLX
    collector's index_urls() for the measurements this is based on.

    The cost of leaving a budget at 0 is time, not correctness: sweeping an
    entire busy category can take well over an hour (OLX reported 2,308
    pages for one city's mobile-phones category alone, at roughly 2s/page).
    Set an explicit positive number on any of these, or pass a `limit` to a
    run, to trade completeness for a faster, bounded run.
    """

    max_pages_per_city_category: int = 0
    max_listings_per_city_category: int = 0

    # Global safety net across an ENTIRE run (every city x source x category
    # combined) - not a per-category budget. Kept separate and non-zero by
    # default because it guards against a genuinely unattended run spiralling
    # across many cities and categories at once, which the per-category
    # budgets above do not protect against on their own now that they default
    # to unlimited. 0 disables this too, for anyone who deliberately wants no
    # ceiling at all.
    max_listings_per_run: int = 200_000

    refetch_detail_after_hours: int = 168
    collect_phone: bool = True

    # Page and listing budgets specifically for the date-filtered path, kept
    # separate from the two above so a dated run can be tuned independently.
    # Same 0-means-unlimited convention.
    #
    # None of these sites order a category page by date. OLX ranks by
    # `productScore desc` ("Most relevant") and only then by timestamp, so
    # today's ads are scattered through the whole result set rather than
    # sitting on page 1 - a measured example: page 1's freshest ad was 91
    # minutes old while page 10 held one 55 minutes old, and a separate run
    # found clusters of today's ads on page 1, page 10 AND page 40 with nothing
    # in between - ordering reshuffles between requests, so there is no page
    # count that is safe to stop at early.
    #
    # There is no URL parameter that fixes this. `?sorting=desc-creation` is
    # accepted and does flip `state.algolia.settings.sort.key`, but the
    # server-rendered hits come back in exactly the same order - OLX applies
    # the chosen sort in the browser, against an endpoint this project does
    # not call. So the only way to honestly satisfy "give me all of today's
    # ads" is to read until the category is exhausted.
    max_pages_when_dated: int = 0
    max_listings_when_dated: int = 0

    # Stop a dated run early after this many consecutive pages with nothing in
    # the window. 0 disables it, which is the default, because on OLX it is
    # not safe: today's ads arrive in clusters separated by long barren
    # stretches rather than tailing off. Measured on Lahore/mobile-phones
    # across 40 pages: page 1 had 5 ads from today, pages 2-9 none, page 10
    # had 16, pages 11-39 none, page 40 had 8 - gaps of 8 and 29 barren pages
    # between live clusters, with the last page sampled still producing. Any
    # threshold here would stop inside a gap and silently drop the clusters
    # past it, which is the exact failure this setting looks like it prevents.
    #
    # An exhausted category is already handled without this: a page that
    # parses to zero listings ends pagination on its own regardless.
    stop_after_barren_pages: int = 0


@dataclass
class Config:
    root: Path
    cities: list[City]
    sources: dict[str, Source]
    http: HttpSettings
    collection: CollectionSettings
    database_path: Path
    export_dir: Path
    cache_dir: Path
    log_level: str = "INFO"
    log_file: Path | None = None

    # ------------------------------------------------------------- selection

    def enabled_cities(self, only: list[str] | None = None) -> list[City]:
        """Cities to process, optionally narrowed by name (case-insensitive)."""
        cities = [c for c in self.cities if c.enabled]
        if only:
            wanted = {name.strip().lower() for name in only}
            known = {c.name.lower() for c in self.cities}
            unknown = wanted - known
            if unknown:
                raise ValueError(
                    f"unknown cities: {', '.join(sorted(unknown))}. "
                    f"Add them to config/cities.yml first."
                )
            cities = [c for c in self.cities if c.name.lower() in wanted]
        return cities

    def enabled_sources(self, only: list[str] | None = None) -> list[Source]:
        sources = [s for s in self.sources.values() if s.enabled]
        if only:
            wanted = {name.strip().lower() for name in only}
            unknown = wanted - set(self.sources)
            if unknown:
                raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}")
            sources = [self.sources[name] for name in self.sources if name in wanted]
        return sources


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"missing required key {key!r} in {where}")
    return mapping[key]


def load_config(root: Path | str | None = None) -> Config:
    """Read config/cities.yml and config/settings.yml under `root`."""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    config_dir = root / "config"

    cities_file = config_dir / "cities.yml"
    settings_file = config_dir / "settings.yml"
    for path in (cities_file, settings_file):
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")

    cities_raw = yaml.safe_load(cities_file.read_text(encoding="utf-8")) or {}
    settings_raw = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}

    cities = [
        City(
            name=_require(entry, "name", "cities.yml"),
            enabled=entry.get("enabled", True),
            province=entry.get("province", ""),
            olx_location_id=(
                str(entry["olx_location_id"])
                if entry.get("olx_location_id") is not None
                else None
            ),
            zameen_slug=entry.get("zameen_slug"),
            pakwheels_slug=entry.get("pakwheels_slug"),
        )
        for entry in cities_raw.get("cities", [])
    ]
    if not cities:
        raise ValueError("cities.yml defines no cities")

    synced = load_synced_categories(config_dir)

    sources: dict[str, Source] = {}
    for name, block in (settings_raw.get("sources") or {}).items():
        categories = [
            Category(
                key=_require(c, "key", f"sources.{name}.categories"),
                label=c.get("label", c["key"]),
                path=_require(c, "path", f"sources.{name}.categories"),
                category_id=str(c["category_id"]) if c.get("category_id") else None,
                curated=True,
                group=c.get("group", ""),
                level=int(c.get("level", 1)),
                priority=int(c.get("priority", 0)),
            )
            for c in block.get("categories", [])
        ]

        # Fold in every category synced from OLX's sitemap. Curated entries win
        # on key collision - they carry the better label and are verified.
        if name == "olx" and synced:
            # Once synced, the site's own list wins: its slugs, labels and
            # sections are authoritative, so a category is addressed by the
            # same key everywhere. The curated block in settings.yml is the
            # fallback for a checkout that has never run `sync-categories`,
            # and only contributes ids the sync did not return.
            curated_by_id = {c.category_id: c for c in categories if c.category_id}

            merged = [
                Category(
                    key=entry["key"],
                    label=entry["label"],
                    path=entry["path"],
                    category_id=entry.get("category_id"),
                    curated=entry.get("category_id") in curated_by_id,
                    group=entry.get("group", ""),
                    level=entry.get("level", 1),
                    priority=entry.get("priority", 0),
                )
                for entry in synced
            ]

            synced_ids = {e.get("category_id") for e in synced}
            merged.extend(c for c in categories if c.category_id not in synced_ids)
            categories = merged

        sources[name] = Source(
            name=name,
            enabled=block.get("enabled", True),
            base_url=_require(block, "base_url", f"sources.{name}").rstrip("/"),
            categories=categories,
        )

    storage = settings_raw.get("storage") or {}
    http_raw = settings_raw.get("http") or {}
    collection_raw = settings_raw.get("collection") or {}
    logging_raw = settings_raw.get("logging") or {}

    def _resolve(value: str, default: str, env_var: str | None = None) -> Path:
        # An environment variable wins, so a deployment can point storage at a
        # mounted disk without editing settings.yml. Render, for instance,
        # mounts persistent storage outside the checkout.
        override = os.getenv(env_var, "").strip() if env_var else ""
        path = Path(override or value or default)
        return path if path.is_absolute() else root / path

    return Config(
        root=root,
        cities=cities,
        sources=sources,
        http=HttpSettings(
            user_agent=http_raw.get("user_agent", "ListingResearchBot/1.0"),
            contact_email=http_raw.get("contact_email", ""),
            timeout_seconds=int(http_raw.get("timeout_seconds", 30)),
            max_retries=int(http_raw.get("max_retries", 3)),
            backoff_base_seconds=float(http_raw.get("backoff_base_seconds", 2.0)),
            delay_seconds=float(http_raw.get("delay_seconds", 3.0)),
            jitter_seconds=float(http_raw.get("jitter_seconds", 2.0)),
            respect_robots=bool(http_raw.get("respect_robots", True)),
            cache_responses=bool(http_raw.get("cache_responses", True)),
            cache_ttl_hours=int(http_raw.get("cache_ttl_hours", 24)),
        ),
        collection=CollectionSettings(
            max_pages_per_city_category=int(
                collection_raw.get("max_pages_per_city_category", 0)
            ),
            max_listings_per_city_category=int(
                collection_raw.get("max_listings_per_city_category", 0)
            ),
            max_listings_per_run=int(collection_raw.get("max_listings_per_run", 200_000)),
            refetch_detail_after_hours=int(
                collection_raw.get("refetch_detail_after_hours", 168)
            ),
            collect_phone=bool(collection_raw.get("collect_phone", True)),
            max_pages_when_dated=int(collection_raw.get("max_pages_when_dated", 0)),
            max_listings_when_dated=int(collection_raw.get("max_listings_when_dated", 0)),
            stop_after_barren_pages=int(collection_raw.get("stop_after_barren_pages", 0)),
        ),
        database_path=_resolve(storage.get("database"), "data/listings.db", "DATABASE_PATH"),
        export_dir=_resolve(storage.get("export_dir"), "exports", "EXPORT_DIR"),
        cache_dir=_resolve(storage.get("cache_dir"), ".cache", "CACHE_DIR"),
        log_level=logging_raw.get("level", "INFO"),
        log_file=_resolve(logging_raw.get("file"), "logs/collector.log"),
    )
