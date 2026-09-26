"""Orchestration: City -> Website -> Category -> Listings -> Validation -> DB."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from core.config import Config
from core.db import Database
from core.http import HttpClient
from core.locations import CityOutcome
from core.models import Listing

log = logging.getLogger(__name__)


def _as_date(value: str | date | None) -> date | None:
    """Accept a date, an ISO string, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"not a valid date: {value!r} (expected YYYY-MM-DD)") from None


@dataclass
class RunStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    filtered: int = 0      # outside the requested date range
    undated: int = 0       # no ad_date, so the date filter could not judge it
    unsupported: int = 0   # city/category units the site has no page for (not errors)
    cancelled: bool = False
    per_city: dict[str, int] = field(default_factory=dict)
    # One entry per city + source + category unit, whatever happened to it.
    city_results: list[dict] = field(default_factory=list)
    # Units where paging hit its budget with a date filter active, so the
    # result is a sample of the matching ads rather than all of them.
    incomplete: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "filtered": self.filtered,
            "undated": self.undated,
            "unsupported": self.unsupported,
        }

    @property
    def total(self) -> int:
        return self.inserted + self.updated


class Pipeline:
    def __init__(self, config: Config, db: Database, http: HttpClient):
        self.config = config
        self.db = db
        self.http = http

    def plan(
        self,
        city_names: list[str] | None = None,
        source_names: list[str] | None = None,
        category_keys: list[str] | None = None,
        selections: dict[str, list[str]] | None = None,
    ) -> list[tuple]:
        """The (city, source, category) units a run would work through.

        Computed up front so callers - the web UI in particular - can show
        real progress instead of a spinner.

        `selections` maps a source name to the category keys wanted from it,
        e.g. {"olx": ["cars", "bikes"], "pakwheels": ["used-cars"]}. Prefer it
        over the flat `category_keys`: category keys are NOT unique across
        sources - both OLX and PakWheels define "bikes" - so a flat list
        cannot express "OLX motorcycles but not PakWheels bikes". An empty
        list for a source means every category of that source.
        """
        cities = self.config.enabled_cities(city_names)

        if selections is not None:
            unknown = set(selections) - set(self.config.sources)
            if unknown:
                raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}")

            chosen: list[tuple] = []
            for name, keys in selections.items():
                source = self.config.sources[name]
                if not source.enabled:
                    log.info("[%s] disabled in settings.yml, skipping", name)
                    continue
                wanted = {k.strip().lower() for k in keys} if keys else None
                for category in source.categories:
                    if wanted is None or category.key.lower() in wanted:
                        chosen.append((source, category))

            return [(city, source, category) for city in cities for source, category in chosen]

        sources = self.config.enabled_sources(source_names)
        wanted = {k.strip().lower() for k in category_keys} if category_keys else None

        units = []
        for city in cities:
            for source in sources:
                for category in source.categories:
                    if wanted is None or category.key.lower() in wanted:
                        units.append((city, source, category))
        return units

    def run(
        self,
        city_names: list[str] | None = None,
        source_names: list[str] | None = None,
        category_keys: list[str] | None = None,
        limit: int | None = None,
        selections: dict[str, list[str]] | None = None,
        date_from: str | date | None = None,
        date_to: str | date | None = None,
        keep_undated: bool = True,
        stop_event=None,
        on_progress=None,
        on_city_result=None,
    ) -> RunStats:
        """Collect listings.

        `selections` picks categories per source and is preferred over the flat
        `category_keys` - see `plan()` for why.

        `date_from` / `date_to` keep only ads posted in that range (inclusive).
        The filter is applied after fetching, because the sites' category pages
        put featured ads first rather than sorting strictly by date - so there
        is no safe point at which to stop paging early. Asking for an old date
        therefore means paging through everything newer to reach it.

        `keep_undated` decides what happens to a listing whose date could not be
        parsed. Default True keeps it, so a parser gap never silently drops
        real ads; set False for a strict date window.

        `stop_event` is any object with `.is_set()` - checked between units and
        between listings, so a cancel takes effect quickly without losing the
        work already committed.

        `on_progress(done, total, label)` is called as each unit completes.

        `on_city_result(dict)` is called with each unit's outcome (completed,
        unsupported, failed or cancelled) - see core.locations.CityOutcome.
        Every selected city gets one, so none can be skipped without a trace.
        """
        from scrapers import build_collector  # imported here to avoid a cycle

        start = _as_date(date_from)
        end = _as_date(date_to)
        if start and end and start > end:
            raise ValueError(f"date_from ({start}) is after date_to ({end})")

        # A window that reaches today has to read live pages. The HTTP cache
        # holds responses for up to a day, so a page fetched yesterday simply
        # would not contain today's ads and the run would return nothing.
        wants_today = (start or end) and (end is None or end >= date.today())
        previous_skip = getattr(self.http, "skip_cache", False)
        if wants_today:
            log.info("date window includes today - bypassing the page cache")
            self.http.skip_cache = True

        units = self.plan(city_names, source_names, category_keys, selections)
        if not units:
            raise ValueError(
                "nothing to do - check the selected cities, sources and categories"
            )

        cities = self.config.enabled_cities(city_names)
        sources = (
            [self.config.sources[name] for name in selections if name in self.config.sources]
            if selections
            else self.config.enabled_sources(source_names)
        )

        stats = RunStats()
        run_id = self.db.start_run([s.name for s in sources], [c.name for c in cities])
        run_cap = self.config.collection.max_listings_per_run
        total = len(units)
        cancelled = False

        def cancelled_now() -> bool:
            return stop_event is not None and stop_event.is_set()

        try:
            collectors: dict[str, object] = {}

            for index, (city, source, category) in enumerate(units, start=1):
                if cancelled_now():
                    log.warning("cancelled - stopping after %s of %s units", index - 1, total)
                    cancelled = True
                    break

                if run_cap and stats.total >= run_cap:
                    log.warning("run cap of %s listings reached - stopping", run_cap)
                    break

                if source.name not in collectors:
                    collectors[source.name] = build_collector(
                        source.name, self.config, self.http, self.db
                    )

                label = f"{city.name} / {source.name} / {category.key}"
                log.info("=== %s === (%s/%s)", label, index, total)

                try:
                    self._collect_one(
                        collectors[source.name], city, category, stats, limit, stop_event,
                        date_window=(start, end, keep_undated),
                        on_city_result=on_city_result,
                    )
                except Exception as exc:
                    stats.errors += 1
                    log.exception("unhandled error on %s", label)
                    outcome = CityOutcome(
                        city=city.name, source=source.name, category=category.label,
                        status="failed", reason=f"unexpected error: {type(exc).__name__}: {exc}",
                    )
                    log.info("[City] %s (%s / %s)", city.name, source.name, category.label)
                    log.info("[Status] Failed")
                    log.info("[Reason] %s", outcome.reason)
                    self._record(outcome, stats, on_city_result)

                if on_progress is not None:
                    try:
                        on_progress(index, total, label)
                    except Exception:
                        log.debug("progress callback failed", exc_info=True)

                # Catches a cancel that landed while the unit just above was
                # collecting - without this, cancelling during the last unit
                # never gets noticed (there is no next loop iteration to
                # check cancelled_now() at the top) and the job reports
                # "completed" instead of "cancelled".
                if cancelled_now():
                    log.warning("cancelled - stopping after %s of %s units", index, total)
                    cancelled = True
                    break
        finally:
            self.db.conn.commit()
            self.http.skip_cache = previous_skip

        stats.cancelled = cancelled
        return self._finish(run_id, stats)

    def _record(self, outcome: CityOutcome, stats: RunStats, on_city_result=None) -> None:
        if outcome.status == "unsupported":
            stats.unsupported += 1
        stats.city_results.append(outcome.as_dict())
        if on_city_result is not None:
            try:
                on_city_result(outcome.as_dict())
            except Exception:
                log.debug("city result callback failed", exc_info=True)

    def _collect_one(
        self, collector, city, category, stats: RunStats, limit, stop_event=None,
        date_window: tuple = (None, None, True), on_city_result=None,
    ) -> None:
        batch: list[Listing] = []
        start, end, keep_undated = date_window
        filtered_before = stats.filtered

        # The window goes down to the collector too, not just used for filtering
        # here: these sites rank category pages by relevance rather than date,
        # so the collector has to read further in and stop on its own terms.
        for listing in collector.collect(
            city, category, limit=limit, date_window=(start, end), stop_event=stop_event
        ):
            if stop_event is not None and stop_event.is_set():
                log.info("cancel requested - stopping mid-category, keeping %s so far",
                         len(batch))
                break

            if start or end:
                if listing.ad_date is None:
                    if not keep_undated:
                        stats.undated += 1
                        continue
                elif (start and listing.ad_date < start) or (
                    end and listing.ad_date > end
                ):
                    stats.filtered += 1
                    continue

            problems = listing.validate()
            if problems:
                stats.skipped += 1
                log.debug("rejected %s: %s", listing.url, "; ".join(problems))
                self.db.record_rejection(listing, problems)
                continue
            batch.append(listing)

        dropped = stats.filtered - filtered_before
        outside = f", {dropped} outside the date range" if dropped else ""

        # Say how much of the category was actually read. Without this a small
        # result reads as "only 3 ads match", when it often means "3 of the
        # pages we had budget for match".
        coverage = getattr(collector, "last_coverage", None)
        if coverage is not None:
            # Out-of-window ads are dropped inside the collector now (so it
            # does not spend detail fetches or its listing budget on them), so
            # take that count from there rather than from the loop above.
            stats.filtered += coverage.out_of_window
            dropped += coverage.out_of_window
            outside = f", {dropped} outside the date range" if dropped else ""

            log.info("[%s] %s / %s coverage: %s",
                     collector.source_name, city.name, category.key, coverage.describe())
            # Worth flagging whenever a positive cap was hit, not only on a
            # dated run - the default is unlimited, so this only fires when a
            # cap was explicitly configured (or passed as `limit`).
            if coverage.exhausted:
                reason = (
                    "listing budget" if coverage.listing_budget_spent
                    else f"{coverage.page_budget}-page budget"
                )
                stats.incomplete.append(
                    f"{city.name}/{collector.source_name}/{category.key}: "
                    f"stopped at the {reason} after {coverage.pages_read} page(s)"
                    + (f" of {coverage.pages_available} available"
                       if coverage.pages_available else "")
                )

        outcome = collector.last_outcome

        # The city could not be searched: nothing was collected, and the
        # reason is already logged. `unsupported` is the site not having the
        # city; `failed` is something going wrong - only that counts as an error.
        if outcome.status in ("unsupported", "failed"):
            if outcome.status == "failed":
                stats.errors += 1
            self._record(outcome, stats, on_city_result)
            return

        cancelled = outcome.status == "pending" or (
            stop_event is not None and stop_event.is_set()
        )
        outcome.status = "cancelled" if cancelled else "completed"
        outcome.listings = len(batch)

        notes = [outcome.reason] if outcome.reason else []
        if outcome.foreign_ads:
            places = ", ".join(outcome.foreign_places[:3])
            notes.append(f"{outcome.foreign_ads} ad(s) from other places ({places}) were left out")
        if not batch:
            head = (
                f"0 listings kept - {dropped} ad(s) fell outside the date range"
                if dropped else f"no ads in {city.name} for this category"
            )
            if not outcome.reason:
                notes.insert(0, head)
        outcome.reason = "; ".join(notes)

        if not batch:
            log.info("no listings kept for %s / %s / %s%s",
                     city.name, collector.source_name, category.key, outside)
        else:
            counts = self.db.upsert_many(batch)
            stats.inserted += counts["inserted"]
            stats.updated += counts["updated"]
            stats.per_city[city.name] = stats.per_city.get(city.name, 0) + len(batch)

            log.info("%s / %s / %s -> %s new, %s refreshed%s",
                     city.name, collector.source_name, category.key,
                     counts["inserted"], counts["updated"], outside)

        log.info("[Results Found] %s", outcome.listings)
        log.info("[Status] %s", "Cancelled" if cancelled else "Completed")
        self._record(outcome, stats, on_city_result)

    def _finish(self, run_id: int, stats: RunStats) -> RunStats:
        self.db.finish_run(run_id, stats.as_dict())
        return stats
