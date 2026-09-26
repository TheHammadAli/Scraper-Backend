"""Background scrape jobs.

A scrape takes minutes, so the API starts it on a worker thread and the UI
polls for progress. Only one job runs at a time - the collectors are rate
limited per host anyway, so running two would not be faster, just ruder.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import settings  # noqa: E402  (adds PROJECT_ROOT to sys.path)

from core.config import load_config
from core.db import Database
from core.http import HttpClient
from core.pipeline import Pipeline

log = logging.getLogger(__name__)

JobStatus = Literal["running", "completed", "failed", "cancelled"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    status: JobStatus = "running"
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    done: int = 0
    total: int = 0
    label: str = "starting..."
    stats: dict[str, int] = field(default_factory=dict)
    # One entry per city/source/category unit as it finishes - see
    # core.locations.CityOutcome. Lets the UI show which cities worked, which
    # the site does not have, and which failed, instead of one merged total.
    city_results: list[dict] = field(default_factory=list)
    error: str | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=settings.JOB_LOG_LIMIT))
    stop_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, log_offset: int = 0, include_logs: bool = True) -> dict:
        lines = list(self.logs)
        return {
            "id": self.id,
            "status": self.status,
            "params": self.params,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "progress": {
                "done": self.done,
                "total": self.total,
                "label": self.label,
                "percent": round(100 * self.done / self.total) if self.total else 0,
            },
            "stats": self.stats,
            "city_results": list(self.city_results),
            "error": self.error,
            "logs": lines[log_offset:] if include_logs else [],
            "log_count": len(lines),
        }


class _JobLogHandler(logging.Handler):
    """Routes collector log records into the running job's buffer."""

    def __init__(self, manager: "JobManager"):
        super().__init__(level=logging.INFO)
        self.manager = manager
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        job = self.manager.active
        if job is None:
            return
        try:
            job.logs.append(self.format(record))
        except Exception:  # a logging failure must never break a scrape
            pass


class JobAlreadyRunning(RuntimeError):
    pass


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self.active: Job | None = None

        handler = _JobLogHandler(self)
        for name in ("core", "scrapers", "export"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)

    # ------------------------------------------------------------------ query

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    # ------------------------------------------------------------------ start

    def start(self, params: dict[str, Any]) -> Job:
        with self._lock:
            if self.active is not None and self.active.status == "running":
                raise JobAlreadyRunning(
                    f"job {self.active.id} is still running - cancel it or wait"
                )

            job = Job(id=uuid.uuid4().hex[:12], params=params)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune()
            self.active = job

        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        job.stop_event.set()
        job.logs.append("cancel requested - finishing the current listing...")
        return True

    def _prune(self) -> None:
        while len(self._order) > settings.JOB_HISTORY_LIMIT:
            oldest = self._order.popleft()
            self._jobs.pop(oldest, None)

    # -------------------------------------------------------------- execution

    def _run(self, job: Job) -> None:
        """Runs on a worker thread. Owns its own DB and HTTP client."""
        config = load_config()

        try:
            with Database(config.database_path) as db, HttpClient(
                config.http, config.cache_dir
            ) as http:
                pipeline = Pipeline(config, db, http)

                units = pipeline.plan(
                    job.params.get("cities"),
                    job.params.get("sources"),
                    job.params.get("categories"),
                    job.params.get("selections"),
                )
                job.total = len(units)

                def on_progress(done: int, total: int, label: str) -> None:
                    job.done, job.total, job.label = done, total, label

                stats = pipeline.run(
                    city_names=job.params.get("cities"),
                    source_names=job.params.get("sources"),
                    category_keys=job.params.get("categories"),
                    selections=job.params.get("selections"),
                    date_from=job.params.get("date_from"),
                    date_to=job.params.get("date_to"),
                    keep_undated=job.params.get("keep_undated", True),
                    limit=job.params.get("limit"),
                    stop_event=job.stop_event,
                    on_progress=on_progress,
                    on_city_result=job.city_results.append,
                )

                job.stats = stats.as_dict()
                job.status = "cancelled" if stats.cancelled else "completed"
                job.label = "cancelled" if stats.cancelled else "finished"

        except Exception as exc:
            log.exception("job %s failed", job.id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.label = "failed"
        finally:
            job.finished_at = _now()
            with self._lock:
                if self.active is job:
                    self.active = None


manager = JobManager()
