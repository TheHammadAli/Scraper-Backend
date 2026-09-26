"""Loads cities.yml and settings.yml into typed objects."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .categories import load_synced_categories
from .locations import city_keys, match_key


@dataclass(frozen=True)
class City:
    name: str
    enabled: bool = True
    province: str = ""
    # Site identifiers pinned by hand in cities.yml. Left empty, each collector
    # resolves the city from the site's own location list (core/locations.py).
    olx_location_id: str | None = None
    zameen_slug: str | None = None
    pakwheels_slug: str | None = None
    aliases: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    # Build-time snapshot of which sites have this city: {"olx": True, ...}.
    # A hint for the picker only - runs always resolve live.
    coverage: dict = field(default_factory=dict, compare=False, hash=False)

    def slug_for(self, source: str) -> str | None:
        """An identifier pinned in cities.yml, or None to resolve it live."""
        return {
            "olx": self.olx_location_id,
            "zameen": self.zameen_slug,
            "pakwheels": self.pakwheels_slug,
        }.get(source)

    @property
    def keys(self) -> set[str]:
        return city_keys(self.name, self.aliases)


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

    def find_city(self, name: str) -> City | None:
        """A city by name, else by alias - spelling-insensitively.

        A city's own name always beats another city's alias: Kotli Loharan
        answers to "Kotli" only because OLX spells it that way, but asking for
        "Kotli" must still give the Azad Kashmir city of that name.
        """
        key = match_key(name)
        by_name = next((c for c in self.cities if match_key(c.name) == key), None)
        return by_name or next((c for c in self.cities if key in c.keys), None)

    def enabled_cities(self, only: list[str] | None = None) -> list[City]:
        """Cities to process, optionally narrowed by name or alias."""
        if not only:
            return [c for c in self.cities if c.enabled]

        chosen: list[City] = []
        unknown: list[str] = []
        for name in only:
            city = self.find_city(name)
            if city is None:
                unknown.append(name.strip())
            elif city not in chosen:
                chosen.append(city)
        if unknown:
            raise ValueError(
                f"unknown cities: {', '.join(sorted(unknown))}. "
                f"They are not in config/pakistan_cities.json - add one under "
                f"`cities:` in config/cities.yml to use it."
            )
        # Keep the master list's order (busiest cities first) so a run does not
        # depend on the order the caller happened to list them in.
        return [c for c in self.cities if c in chosen]

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


def _pinned(entry: dict, key: str) -> str | None:
    value = entry.get(key)
    return str(value) if value is not None else None


def _load_cities(config_dir: Path, overrides_raw: list[dict]) -> list[City]:
    """Master list + cities.yml overrides, master order preserved."""
    dataset_file = config_dir / "pakistan_cities.json"
    records: list[dict] = []
    if dataset_file.exists():
        records = json.loads(dataset_file.read_text(encoding="utf-8")).get("cities", [])

    overrides = [(city_keys(_require(e, "name", "cities.yml"), e.get("aliases") or []), e)
                 for e in overrides_raw]
    used: set[int] = set()

    def override_for(keys: set[str]) -> dict:
        for index, (ov_keys, entry) in enumerate(overrides):
            if ov_keys & keys:
                used.add(index)
                return entry
        return {}

    cities: list[City] = []
    for rec in records:
        aliases = tuple(rec.get("aliases") or [])
        ov = override_for(city_keys(rec["name"], aliases))
        cities.append(
            City(
                name=rec["name"],
                enabled=ov.get("enabled", True),
                province=ov.get("province", rec.get("province", "")),
                olx_location_id=_pinned(ov, "olx_location_id"),
                zameen_slug=ov.get("zameen_slug"),
                pakwheels_slug=ov.get("pakwheels_slug"),
                aliases=aliases + tuple(ov.get("aliases") or []),
                lat=rec.get("lat"),
                lon=rec.get("lon"),
                coverage=dict(rec.get("coverage") or {}),
            )
        )

    # A cities.yml entry the master list does not know is a city of its own.
    for index, (_, entry) in enumerate(overrides):
        if index in used:
            continue
        cities.append(
            City(
                name=entry["name"],
                enabled=entry.get("enabled", True),
                province=entry.get("province", ""),
                olx_location_id=_pinned(entry, "olx_location_id"),
                zameen_slug=entry.get("zameen_slug"),
                pakwheels_slug=entry.get("pakwheels_slug"),
                aliases=tuple(entry.get("aliases") or []),
            )
        )
    return cities


def load_config(root: Path | str | None = None) -> Config:
    """Read the city list and config/settings.yml under `root`.

    Cities come from config/pakistan_cities.json (the master list, built by
    scripts/build_pakistan_cities.py). config/cities.yml is a small override
    file on top: disable a city, pin a site identifier, add a one-off place.
    """
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    config_dir = root / "config"

    cities_file = config_dir / "cities.yml"
    settings_file = config_dir / "settings.yml"
    for path in (cities_file, settings_file):
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")

    cities_raw = yaml.safe_load(cities_file.read_text(encoding="utf-8")) or {}
    settings_raw = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}

    cities = _load_cities(config_dir, cities_raw.get("cities") or [])
    if not cities:
        raise ValueError("no cities defined - config/pakistan_cities.json is missing and cities.yml is empty")

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
