"""FastAPI backend for the listing collector.

Deployed separately from the frontend. Point the frontend at it with
NEXT_PUBLIC_API_URL, and allow that origin here with CORS_ORIGINS.

Run locally:
    cd backend
    uvicorn main:app --reload
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import settings  # noqa: F401  (adds PROJECT_ROOT to sys.path - must be first)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.config import load_config
from core.db import Database
from export.excel import export_to_excel
from jobs import JobAlreadyRunning, manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="Listing Collector API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_db() -> Database:
    """A fresh connection per request - sqlite3 objects are not thread-safe."""
    return Database(load_config().database_path)


def row_to_listing(row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "source_listing_id": row["source_listing_id"],
        "city": row["city"],
        "category": row["category"],
        "title": row["title"],
        "description": row["description"],
        "price": row["price"],
        "price_currency": row["price_currency"],
        "price_raw": row["price_raw"],
        "phone": row["phone"],
        "seller_name": row["seller_name"],
        "ad_date": row["ad_date"],
        "url": row["url"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


# ------------------------------------------------------------------- schemas


class RunRequest(BaseModel):
    cities: list[str] | None = None

    # Preferred: category keys chosen per source, e.g.
    #   {"olx": ["cars"], "pakwheels": ["used-cars"]}
    # Category keys are not unique across sources ("bikes" exists on both OLX
    # and PakWheels), so the flat `categories` field below cannot distinguish
    # them. It is kept for the CLI and older clients.
    selections: dict[str, list[str]] | None = None

    sources: list[str] | None = None
    categories: list[str] | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)

    # Keep only ads posted in this range, inclusive. YYYY-MM-DD.
    date_from: str | None = None
    date_to: str | None = None
    # What to do with an ad whose date could not be parsed. Keeping them is
    # the default so a parser gap never silently drops real listings.
    keep_undated: bool = True


# -------------------------------------------------------------------- routes


@app.get("/api/health")
def health() -> dict:
    active = manager.active
    return {
        "status": "ok",
        "active_job": active.id if active and active.status == "running" else None,
    }


@app.get("/api/config")
def get_config() -> dict:
    """Cities, sources and categories for the run form."""
    config = load_config()
    return {
        "cities": [
            {"name": c.name, "province": c.province, "enabled": c.enabled}
            for c in config.cities
        ],
        "sources": [
            {
                "name": s.name,
                "enabled": s.enabled,
                "categories": [
                    {
                        "key": c.key,
                        "label": c.label,
                        "curated": c.curated,
                        # The site's own section ("Mobiles", "Vehicles"), so
                        # the picker can group instead of showing a flat list.
                        "group": c.group,
                        "level": c.level,
                        "priority": c.priority,
                    }
                    for c in s.categories
                ],
            }
            for s in config.sources.values()
        ],
        "defaults": {
            "limit": config.collection.max_listings_per_city_category,
            "collect_phone": config.collection.collect_phone,
        },
    }


@app.post("/api/jobs", status_code=201)
def start_job(request: RunRequest) -> dict:
    for label, value in (("date_from", request.date_from), ("date_to", request.date_to)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(
                    status_code=422, detail=f"{label} must be YYYY-MM-DD, got {value!r}"
                ) from None

    if request.date_from and request.date_to and request.date_from > request.date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")

    try:
        job = manager.start(request.model_dump(exclude_none=True))
    except JobAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs() -> dict:
    """Recent job history. Logs are omitted - fetch one job for those."""
    return {"jobs": [j.to_dict(include_logs=False) for j in manager.list()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, log_offset: int = Query(0, ge=0)) -> dict:
    """Poll a job. `log_offset` returns only log lines after that index."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return job.to_dict(log_offset=log_offset)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not manager.cancel(job_id):
        raise HTTPException(status_code=409, detail="job is not running")
    return {"cancelled": True, "id": job_id}


@app.get("/api/listings")
def get_listings(
    city: str | None = None,
    source: str | None = None,
    category: str | None = None,
    q: str | None = None,
    has_phone: bool | None = None,
    seen_since: str | None = None,
    sort: str = "last_seen_at",
    descending: bool = True,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    """List stored listings.

    `seen_since` (ISO timestamp) narrows to listings a run touched at or after
    that moment - pass a job's created_at to see just that run's results.
    """
    with get_db() as db:
        rows, total = db.search(
            city=city,
            source=source,
            category=category,
            q=q,
            has_phone=has_phone,
            seen_since=seen_since,
            sort=sort,
            descending=descending,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return {
            "listings": [row_to_listing(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),
        }


@app.get("/api/filters")
def get_filters() -> dict:
    """Values actually present in the data, for the filter dropdowns."""
    with get_db() as db:
        return {
            "cities": db.distinct_values("city"),
            "sources": db.distinct_values("source"),
            "categories": db.distinct_values("category"),
        }


@app.get("/api/stats")
def get_stats() -> dict:
    with get_db() as db:
        summary = [dict(r) for r in db.summary_by_city()]
        totals = db.conn.execute(
            "SELECT COUNT(*) AS listings, COUNT(price) AS with_price, "
            "COUNT(phone) AS with_phone FROM listings"
        ).fetchone()
        return {
            "summary": summary,
            "totals": dict(totals),
            "cities": db.cities_present(),
        }


@app.get("/api/export")
def export(
    city: str | None = None,
    since: str | None = None,
    seen_since: str | None = None,
) -> FileResponse:
    """Build an .xlsx and return it as a download.

    `seen_since` scopes it to one run's results - pass that job's created_at
    and the workbook holds what the run collected, not the whole database.
    """
    config = load_config()
    with get_db() as db:
        if not db.all_listings(city=city, since=since, seen_since=seen_since):
            raise HTTPException(status_code=404, detail="no listings match that filter")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"listings_{city.lower()}_{stamp}.xlsx" if city else f"listings_{stamp}.xlsx"
        path: Path = export_to_excel(
            db,
            config.export_dir,
            filename=name,
            since=since,
            city=city,
            seen_since=seen_since,
        )

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
