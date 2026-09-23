"""Shared collector machinery.

The pipeline for every source is the same:

    City -> Website -> Category/search page -> Listing URLs -> Listing details
         -> Validation -> Database

Subclasses only supply the site-specific parts: how to resolve a city to that
site's identifier, how to page through a category, how to read an index page,
and how to read a detail page.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.config import Category, City, Config, Source
from core.db import Database
from core.http import FetchError, HttpClient, RobotsDisallowed, Response
from core.jsonblob import extract_window_json as _extract_window_json
from core.models import Listing
from core.normalize import clean_text, extract_phone

log = logging.getLogger(__name__)

# A backstop against a genuine runaway loop (a site bug, or one of ours, that
# keeps returning a non-empty page forever) when a budget is left at its
# unlimited (0) default. No real category should ever get near this - it
# exists purely so an infinite loop fails loudly instead of running forever.
_HARD_PAGE_SAFETY_CAP = 20_000


@dataclass
class PageCoverage:
    """How much of a category the collector actually looked at.

    Worth reporting because a small result count has two very different
    causes - "only three ads match" and "only three of the ads we looked at
    match, and we looked at 5 pages out of 818".
    """

    pages_read: int
    page_budget: int  # 0 means no cap was configured for this run
    pages_available: int | None = None
    dated: bool = False
    out_of_window: int = 0
    listing_budget_spent: bool = False

    @property
    def exhausted(self) -> bool:
        """True when paging stopped at a configured ceiling rather than
        because the site itself ran out of results."""
        page_cap_hit = self.page_budget > 0 and self.pages_read >= self.page_budget
        return page_cap_hit or self.listing_budget_spent

    def describe(self) -> str:
        total = self.pages_available
        of_total = f" of {total} the site reports" if total else ""
        note = ""
        if self.listing_budget_spent:
            note = "  <- listing budget reached"
        elif self.page_budget > 0 and self.pages_read >= self.page_budget and (
            total is None or total > self.pages_read
        ):
            note = "  <- page budget reached, more ads exist beyond this point"
        elif self.page_budget == 0:
            note = "  <- no cap set, read until the category was exhausted"
        return f"read {self.pages_read} page(s){of_total}{note}"


def _within(value: date | None, start: date | None, end: date | None) -> bool:
    """Is this ad date inside the requested window? Undated counts as inside."""
    if value is None:
        return True
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


# --------------------------------------------------------------- parse helpers


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def extract_jsonld(html: str) -> list[dict]:
    """Every JSON-LD object on the page, flattened out of @graph wrappers.

    All three sites emit schema.org markup for listings, which changes far less
    often than their CSS classes - so this is the preferred extraction path.
    """
    items: list[dict] = []
    for tag in soup_of(html).find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph")
            if isinstance(graph, list):
                items.extend(g for g in graph if isinstance(g, dict))
            items.append(entry)
    return items


def jsonld_of_type(items: list[dict], *types: str) -> dict | None:
    """First JSON-LD object whose @type matches one of `types`."""
    wanted = {t.lower() for t in types}
    for item in items:
        raw_type = item.get("@type", "")
        found = [raw_type] if isinstance(raw_type, str) else list(raw_type or [])
        if any(str(t).lower() in wanted for t in found):
            return item
    return None


def extract_next_data(html: str) -> dict | None:
    """The __NEXT_DATA__ blob from a Next.js page (OLX)."""
    tag = soup_of(html).find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    try:
        return json.loads(tag.string or tag.get_text() or "")
    except (json.JSONDecodeError, ValueError):
        return None


# Lives in core so core.categories can use it too; re-exported here because
# every collector imports its parse helpers from this module.
extract_window_json = _extract_window_json


def find_first_key(obj: Any, *keys: str, max_depth: int = 12) -> Any:
    """Depth-first search for the first of `keys` present in a nested structure.

    Site JSON payloads get reshaped often. Searching by key name instead of
    hard-coding a path means a moved field still resolves.
    """
    if max_depth <= 0:
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, "", [], {}):
                return obj[key]
        for value in obj.values():
            found = find_first_key(value, *keys, max_depth=max_depth - 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_first_key(value, *keys, max_depth=max_depth - 1)
            if found is not None:
                return found
    return None


def area_from_location_hierarchy(location: object, city_level: int = 2) -> str:
    """The publicly displayed area/neighbourhood from a levelled location list.

    OLX and Zameen both publish an ad's location the same way: a list of
    {"level": N, "name": ...} entries running Country(0) -> Province(1) ->
    City(2) -> Area(3), rendered under every ad's title to every visitor -
    this is the field a seller picks from the site's own location dropdown,
    not a street address.

    Returns the deepest entry's name only when it sits below `city_level` -
    i.e. it is a genuine area distinct from the city already stored
    separately in `Listing.city` - and "" when the hierarchy has nothing more
    specific than the city itself.
    """
    if not isinstance(location, list):
        return ""
    deepest = max(
        (entry for entry in location if isinstance(entry, dict) and entry.get("name")),
        key=lambda entry: entry.get("level", -1),
        default=None,
    )
    if deepest and deepest.get("level", -1) > city_level:
        return clean_text(str(deepest["name"]))
    return ""


def text_of(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def first_text(page: BeautifulSoup, selectors: list[str]) -> str:
    """Try selectors in order and return the first non-empty match.

    Several fallbacks per field is deliberate: when a site tweaks its markup,
    one dead selector degrades a field instead of breaking the run.
    """
    for selector in selectors:
        node = page.select_one(selector)
        if node:
            value = text_of(node)
            if value:
                return value
    return ""


# ------------------------------------------------------------------ collector


class BaseCollector(ABC):
    source_name: str = ""

    def __init__(self, config: Config, source: Source, http: HttpClient, db: Database | None = None):
        self.config = config
        self.source = source
        self.http = http
        self.db = db
        self.base_url = source.base_url
        self._location_cache_path: Path = config.cache_dir / "locations.json"
        self._location_cache: dict[str, str] = self._load_location_cache()
        # Set at the end of each collect() so the pipeline can report how much
        # of the category was actually read.
        self.last_coverage: PageCoverage | None = None

    # --------------------------------------------------------- location cache

    def _load_location_cache(self) -> dict[str, str]:
        if self._location_cache_path.exists():
            try:
                return json.loads(self._location_cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("location cache unreadable; starting fresh")
        return {}

    def _save_location_cache(self) -> None:
        self._location_cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._location_cache_path.write_text(
                json.dumps(self._location_cache, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            log.debug("could not persist location cache: %s", exc)

    def cached_location(self, city: City) -> str | None:
        return self._location_cache.get(f"{self.source_name}:{city.name.lower()}")

    def cache_location(self, city: City, identifier: str) -> None:
        self._location_cache[f"{self.source_name}:{city.name.lower()}"] = identifier
        self._save_location_cache()

    def resolve_city(self, city: City) -> str | None:
        """Config override, then cache, then a live lookup. None means skip."""
        configured = city.slug_for(self.source_name)
        if configured:
            return configured

        cached = self.cached_location(city)
        if cached:
            return cached

        try:
            resolved = self.lookup_city(city)
        except (FetchError, RobotsDisallowed) as exc:
            log.warning("[%s] could not look up %s: %s", self.source_name, city.name, exc)
            return None

        if resolved:
            self.cache_location(city, resolved)
            log.info("[%s] resolved %s -> %s", self.source_name, city.name, resolved)
        else:
            log.warning(
                "[%s] no location id found for %s. Set it manually in config/cities.yml.",
                self.source_name, city.name,
            )
        return resolved

    # ------------------------------------------------------- subclass contract

    @abstractmethod
    def lookup_city(self, city: City) -> str | None:
        """Resolve a city name to this site's identifier via a live lookup."""

    @abstractmethod
    def index_urls(self, city: City, identifier: str, category: Category) -> Iterator[str]:
        """Yield paginated category/search URLs for this city."""

    @abstractmethod
    def parse_index(self, response: Response, city: City, category: Category) -> list[Listing]:
        """Parse an index page into partial listings (detail may be missing)."""

    def needs_detail(self, listing: Listing) -> bool:
        """Whether the detail page must be fetched to complete this listing."""
        return not listing.description or len(listing.description) < 80

    def parse_detail(self, response: Response, listing: Listing) -> Listing:
        """Enrich a partial listing from its detail page. Default: unchanged."""
        return listing

    # --------------------------------------------------------------- pipeline

    def absolute(self, href: str) -> str:
        return urljoin(self.base_url + "/", href)

    def apply_phone_policy(self, listing: Listing, *sources: str) -> Listing:
        """Set the phone field from publicly visible text only.

        This never touches a site's gated "show phone number" endpoint. It
        reads what is already rendered on the public page - typically a number
        the seller typed into their own ad text.
        """
        if not self.config.collection.collect_phone:
            listing.phone = None
            return listing
        if listing.phone:
            return listing
        for text in sources:
            found = extract_phone(text)
            if found:
                listing.phone = found
                break
        return listing

    def collect(
        self,
        city: City,
        category: Category,
        limit: int | None = None,
        date_window: tuple = (None, None),
        stop_event=None,
    ) -> Iterator[Listing]:
        """Run the full pipeline for one city + category.

        `limit` and the page budget both follow the project-wide convention
        of "0 or None means unlimited": by default this reads pages until the
        site itself says the category is exhausted (an index page returns no
        listings), because a fixed sample cannot be trusted to contain
        everything - see `CollectionSettings` for the measurements behind
        that. Pass an explicit positive `limit`, or set a positive budget in
        config, to trade completeness for a faster, bounded run.

        `date_window` is the (start, end) the caller will filter on. It is
        passed down rather than applied here because these sites do not order
        a category page by date - see `CollectionSettings.max_pages_when_dated`
        - so the only way to honour a date window is to read further into the
        result set and stop once the category is exhausted.

        `stop_event` is checked before each page fetch and before each detail
        fetch, not just between yielded listings - a dated run can spend many
        pages with nothing in the window, so waiting for a yield to notice a
        cancellation would leave it unresponsive for as long as that page run
        lasts.
        """
        limits = self.config.collection
        start, end = date_window
        dated = bool(start or end)

        if limit is None:
            limit = (
                limits.max_listings_when_dated if dated
                else limits.max_listings_per_city_category
            )
        limit = limit or None  # 0 -> None (unlimited)

        # 0 means unlimited; checked explicitly in the loop below rather than
        # folded into `limit` since it governs a different guard (pages, not
        # listings produced).
        max_pages = (
            limits.max_pages_when_dated if dated else limits.max_pages_per_city_category
        )

        identifier = self.resolve_city(city)
        if not identifier:
            return

        produced = 0
        seen_urls: set[str] = set()
        barren_pages = 0          # consecutive pages with nothing in the window
        out_of_window = 0         # dropped here, so the pipeline can still count them
        self.last_coverage = None  # set below so the pipeline can report it

        pages_read = 0
        for page_number, index_url in enumerate(
            self.index_urls(city, identifier, category), start=1
        ):
            page_cap_reached = max_pages > 0 and page_number > max_pages
            listing_cap_reached = limit is not None and produced >= limit
            if page_cap_reached or listing_cap_reached:
                break

            if stop_event is not None and stop_event.is_set():
                log.info("[%s] %s / %s: cancel requested - stopping pagination",
                          self.source_name, city.name, category.key)
                break

            if page_number > _HARD_PAGE_SAFETY_CAP:
                log.error(
                    "[%s] %s / %s: hit the runaway safety cap of %s pages - "
                    "stopping. No real category should reach this; treat it as "
                    "a bug (a page kept returning listings without ever going "
                    "empty).",
                    self.source_name, city.name, category.key, _HARD_PAGE_SAFETY_CAP,
                )
                break

            try:
                response = self.http.get(index_url)
            except RobotsDisallowed as exc:
                log.warning("[%s] %s", self.source_name, exc)
                return
            except FetchError as exc:
                log.warning("[%s] index page failed: %s", self.source_name, exc)
                break

            try:
                stubs = self.parse_index(response, city, category)
            except Exception:  # a parse failure on one page should not kill the run
                log.exception("[%s] could not parse index %s", self.source_name, index_url)
                break

            if not stubs:
                log.info("[%s] no listings on %s - stopping pagination",
                         self.source_name, index_url)
                break

            pages_read = page_number
            in_window = sum(1 for s in stubs if _within(s.ad_date, start, end))
            log.info("[%s] %s / %s page %s: %s listings, %s in date window",
                     self.source_name, city.name, category.key,
                     page_number, len(stubs), in_window if dated else "n/a")

            for stub in stubs:
                if limit is not None and produced >= limit:
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                if stub.url_canonical in seen_urls:
                    continue
                seen_urls.add(stub.url_canonical)

                # Drop out-of-window ads here rather than in the pipeline.
                # Doing it after _complete() would spend a detail fetch on an
                # ad about to be discarded, and - because `limit` counts what
                # this loop produces - would let a date-filtered run burn its
                # whole listing budget on ads it never keeps, ending pagination
                # long before the page budget was spent. An undated stub is
                # kept: the pipeline's keep_undated decides those.
                if dated and not _within(stub.ad_date, start, end):
                    out_of_window += 1
                    continue

                listing = self._complete(stub)
                if listing is None:
                    continue

                produced += 1
                yield listing

            # Optional early stop for a dated run, off by default. Ranking is
            # by relevance, not date, so in-window ads come in clusters with
            # long barren stretches between them - stopping on a run of empty
            # pages drops every cluster that lay beyond the gap. See
            # CollectionSettings.stop_after_barren_pages for the measurements.
            if dated and limits.stop_after_barren_pages > 0:
                barren_pages = 0 if in_window else barren_pages + 1
                if barren_pages >= limits.stop_after_barren_pages:
                    log.warning(
                        "[%s] %s / %s: stopping at page %s after %s barren pages. "
                        "Ordering is by relevance, so in-window ads may exist "
                        "further in - set stop_after_barren_pages: 0 to read the "
                        "full page budget.",
                        self.source_name, city.name, category.key,
                        page_number, barren_pages,
                    )
                    break

        self.last_coverage = PageCoverage(
            pages_read=pages_read,
            page_budget=max_pages,
            pages_available=self.total_pages(),
            dated=dated,
            out_of_window=out_of_window,
            listing_budget_spent=limit is not None and produced >= limit,
        )

    def total_pages(self) -> int | None:
        """How many index pages the site says exist, if it says so at all.

        Used only to report coverage honestly. None means unknown.
        """
        return None

    def _complete(self, listing: Listing) -> Listing | None:
        """Fetch and parse the detail page when the stub is not good enough."""
        if not self.needs_detail(listing):
            return listing

        if self.db is not None and self.db.detail_is_fresh(
            listing, self.config.collection.refetch_detail_after_hours
        ):
            log.debug("[%s] detail still fresh, skipping fetch: %s",
                      self.source_name, listing.url)
            return listing

        try:
            response = self.http.get(listing.url)
        except RobotsDisallowed as exc:
            log.debug("[%s] %s", self.source_name, exc)
            return listing
        except FetchError as exc:
            log.warning("[%s] detail fetch failed for %s: %s",
                        self.source_name, listing.url, exc)
            return listing

        try:
            return self.parse_detail(response, listing)
        except Exception:
            log.exception("[%s] could not parse detail %s", self.source_name, listing.url)
            return listing
