"""SQLite storage: schema, indexes, and duplicate-proof upserts."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Listing

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT    NOT NULL,
    source_listing_id  TEXT,
    fingerprint        TEXT    NOT NULL,
    city               TEXT    NOT NULL,
    area               TEXT,
    category           TEXT,
    title              TEXT    NOT NULL,
    description        TEXT,
    price              REAL,
    price_currency     TEXT    DEFAULT 'PKR',
    price_raw          TEXT,
    phone              TEXT,
    seller_name        TEXT,
    ad_date            TEXT,
    url                TEXT    NOT NULL,
    url_canonical      TEXT    NOT NULL,
    first_seen_at      TEXT    NOT NULL,
    last_seen_at       TEXT    NOT NULL,
    scraped_at         TEXT    NOT NULL
);

-- Primary dedup key: source + source_listing_id where available, canonical URL
-- otherwise. Computed in Listing.fingerprint so both layers agree.
CREATE UNIQUE INDEX IF NOT EXISTS ux_listings_fingerprint
    ON listings (fingerprint);

-- Safety net: the same ad id from the same source can never appear twice, even
-- if the fingerprint logic is ever changed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_listings_source_listing_id
    ON listings (source, source_listing_id)
    WHERE source_listing_id IS NOT NULL;

-- Catches the case where an ad is first seen without an id (URL fingerprint)
-- and later with one.
CREATE UNIQUE INDEX IF NOT EXISTS ux_listings_source_url
    ON listings (source, url_canonical);

CREATE INDEX IF NOT EXISTS ix_listings_city          ON listings (city);
CREATE INDEX IF NOT EXISTS ix_listings_source_city   ON listings (source, city);
CREATE INDEX IF NOT EXISTS ix_listings_city_category ON listings (city, category);
CREATE INDEX IF NOT EXISTS ix_listings_ad_date       ON listings (ad_date);
CREATE INDEX IF NOT EXISTS ix_listings_scraped_at    ON listings (scraped_at);
CREATE INDEX IF NOT EXISTS ix_listings_last_seen_at  ON listings (last_seen_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    sources       TEXT,
    cities        TEXT,
    inserted      INTEGER DEFAULT 0,
    updated       INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS rejected_listings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    url         TEXT,
    reasons     TEXT,
    payload     TEXT,
    rejected_at TEXT NOT NULL
);
"""

UPDATABLE_COLUMNS = (
    "city", "area", "category", "title", "description", "price", "price_currency",
    "price_raw", "phone", "seller_name", "ad_date", "url", "source_listing_id",
    "fingerprint",
)

# Columns added after the first release. SQLite cannot add a column that is
# already there, so each is applied only when missing.
MIGRATIONS = {
    "seller_name": "ALTER TABLE listings ADD COLUMN seller_name TEXT",
    "area": "ALTER TABLE listings ADD COLUMN area TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        existing = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(listings)")
        }
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                log.info("adding missing column %r to listings", column)
                self.conn.execute(statement)

    # ----------------------------------------------------------------- lookup

    def find_existing(self, listing: Listing) -> sqlite3.Row | None:
        """Look the listing up by fingerprint, then by source + canonical URL.

        The second lookup is what stops an ad from being stored twice when it
        was first seen without a visible id and later with one.
        """
        cur = self.conn.execute(
            "SELECT * FROM listings WHERE fingerprint = ?", (listing.fingerprint,)
        )
        row = cur.fetchone()
        if row:
            return row

        cur = self.conn.execute(
            "SELECT * FROM listings WHERE source = ? AND url_canonical = ?",
            (listing.source, listing.url_canonical),
        )
        return cur.fetchone()

    def detail_is_fresh(self, listing: Listing, max_age_hours: int) -> bool:
        """True if we already hold a recent detail fetch for this listing."""
        row = self.find_existing(listing)
        if row is None:
            return False
        try:
            scraped_at = datetime.fromisoformat(row["scraped_at"])
        except (TypeError, ValueError):
            return False
        age = datetime.now(timezone.utc) - scraped_at
        return age.total_seconds() < max_age_hours * 3600

    # ----------------------------------------------------------------- upsert

    def upsert(self, listing: Listing) -> str:
        """Insert or refresh one listing. Returns 'inserted' or 'updated'."""
        row_data = listing.to_row()
        now = _now()
        existing = self.find_existing(listing)

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO listings (
                    source, source_listing_id, fingerprint, city, area, category, title,
                    description, price, price_currency, price_raw, phone, seller_name,
                    ad_date, url, url_canonical, first_seen_at, last_seen_at, scraped_at
                ) VALUES (
                    :source, :source_listing_id, :fingerprint, :city, :area, :category, :title,
                    :description, :price, :price_currency, :price_raw, :phone, :seller_name,
                    :ad_date, :url, :url_canonical, :first_seen_at, :last_seen_at, :scraped_at
                )
                """,
                {**row_data, "first_seen_at": now, "last_seen_at": now},
            )
            return "inserted"

        # Refresh, but never let a thinner re-scrape erase richer data we hold.
        merged = dict(row_data)
        if len(merged.get("description") or "") < len(existing["description"] or ""):
            merged["description"] = existing["description"]
        for column in ("price", "phone", "seller_name", "ad_date", "source_listing_id", "area"):
            if merged.get(column) in (None, "") and existing[column] is not None:
                merged[column] = existing[column]

        assignments = ", ".join(f"{c} = :{c}" for c in UPDATABLE_COLUMNS)
        self.conn.execute(
            f"UPDATE listings SET {assignments}, last_seen_at = :last_seen_at, "
            f"scraped_at = :scraped_at WHERE id = :id",
            {**merged, "last_seen_at": now, "id": existing["id"]},
        )
        return "updated"

    def upsert_many(self, listings: Iterable[Listing]) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0}
        with self.conn:
            for listing in listings:
                counts[self.upsert(listing)] += 1
        return counts

    def record_rejection(self, listing: Listing, reasons: list[str]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO rejected_listings (source, url, reasons, payload, rejected_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (listing.source, listing.url, "; ".join(reasons),
                 str(listing.to_row()), _now()),
            )

    # ------------------------------------------------------------------- runs

    def start_run(self, sources: list[str], cities: list[str]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO scrape_runs (started_at, sources, cities) VALUES (?, ?, ?)",
                (_now(), ",".join(sources), ",".join(cities)),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, int], notes: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scrape_runs SET finished_at = ?, inserted = ?, updated = ?, "
                "skipped = ?, errors = ?, notes = ? WHERE id = ?",
                (_now(), stats.get("inserted", 0), stats.get("updated", 0),
                 stats.get("skipped", 0), stats.get("errors", 0), notes, run_id),
            )

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _since_clause(since: str | None, seen_since: str | None = None) -> tuple[str, tuple]:
        """Window clauses for the two different questions callers ask.

        `since` filters on first_seen_at - ads discovered for the first time,
        the daily "what's new" view.

        `seen_since` filters on last_seen_at - every ad a run touched, new or
        refreshed. That is what "export what I just scraped" means, because a
        listing collected again today is still part of that run's results.
        """
        clauses: list[str] = []
        params: list = []

        if since:
            clauses.append("first_seen_at >= ?")
            params.append(since)
        if seen_since:
            clauses.append("last_seen_at >= ?")
            params.append(seen_since)

        return " AND ".join(clauses), tuple(params)

    def all_listings(
        self,
        city: str | None = None,
        since: str | None = None,
        seen_since: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list = []

        if city:
            clauses.append("city = ?")
            params.append(city)

        window_sql, window_params = self._since_clause(since, seen_since)
        if window_sql:
            clauses.append(window_sql)
            params.extend(window_params)

        sql = "SELECT * FROM listings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY city, source, ad_date DESC, id DESC"
        return self.conn.execute(sql, tuple(params)).fetchall()

    def cities_present(
        self, since: str | None = None, seen_since: str | None = None
    ) -> list[str]:
        window_sql, params = self._since_clause(since, seen_since)
        sql = "SELECT DISTINCT city FROM listings"
        if window_sql:
            sql += f" WHERE {window_sql}"
        sql += " ORDER BY city"
        return [r["city"] for r in self.conn.execute(sql, params).fetchall()]

    def summary_by_city(
        self, since: str | None = None, seen_since: str | None = None
    ) -> list[sqlite3.Row]:
        window_sql, params = self._since_clause(since, seen_since)
        where = f"WHERE {window_sql}" if window_sql else ""
        return self.conn.execute(
            f"""
            SELECT city,
                   source,
                   COUNT(*)                          AS listings,
                   COUNT(price)                      AS with_price,
                   COUNT(phone)                      AS with_phone,
                   CAST(AVG(price) AS INTEGER)       AS avg_price,
                   MIN(ad_date)                      AS oldest_ad,
                   MAX(ad_date)                      AS newest_ad
            FROM listings
            {where}
            GROUP BY city, source
            ORDER BY city, source
            """,
            params,
        ).fetchall()

    SORTABLE = {
        "last_seen_at", "first_seen_at", "scraped_at", "ad_date",
        "price", "city", "source", "category", "title", "id",
    }

    def search(
        self,
        *,
        city: str | None = None,
        source: str | None = None,
        category: str | None = None,
        q: str | None = None,
        has_phone: bool | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        seen_since: str | None = None,
        sort: str = "last_seen_at",
        descending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[sqlite3.Row], int]:
        """Filtered, paginated listing search. Returns (rows, total_matches)."""
        clauses: list[str] = []
        params: list = []

        for column, value in (("city", city), ("source", source), ("category", category)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)

        if q:
            clauses.append("(title LIKE ? OR description LIKE ?)")
            pattern = f"%{q}%"
            params.extend([pattern, pattern])

        if has_phone is True:
            clauses.append("phone IS NOT NULL")
        elif has_phone is False:
            clauses.append("phone IS NULL")

        if min_price is not None:
            clauses.append("price >= ?")
            params.append(min_price)
        if max_price is not None:
            clauses.append("price <= ?")
            params.append(max_price)
        if seen_since:
            clauses.append("last_seen_at >= ?")
            params.append(seen_since)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM listings{where}", tuple(params)
        ).fetchone()["n"]

        # Whitelist the sort column - it is interpolated, not bound.
        column = sort if sort in self.SORTABLE else "last_seen_at"
        direction = "DESC" if descending else "ASC"

        rows = self.conn.execute(
            f"SELECT * FROM listings{where} ORDER BY {column} {direction}, id DESC "
            f"LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 500)), max(0, offset)),
        ).fetchall()

        return rows, total

    def distinct_values(self, column: str) -> list[str]:
        """Distinct non-null values of a whitelisted column, for filter menus."""
        if column not in {"city", "source", "category"}:
            raise ValueError(f"column {column!r} is not filterable")
        rows = self.conn.execute(
            f"SELECT DISTINCT {column} AS v FROM listings "
            f"WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
        return [r["v"] for r in rows]

    def counts_since(self, since: str) -> dict[str, int]:
        """New vs refreshed since a timestamp - the daily delta."""
        new = self.conn.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE first_seen_at >= ?", (since,)
        ).fetchone()["n"]
        seen = self.conn.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE last_seen_at >= ?", (since,)
        ).fetchone()["n"]
        return {"new": new, "refreshed": seen - new, "total_seen": seen}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
