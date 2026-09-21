"""A polite HTTP client: robots.txt, rate limiting, retries, disk cache.

The rate limiting and robots handling here are not optional extras - they are
the difference between research traffic and something that gets the IP banned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import HttpSettings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Raised when a URL could not be fetched after all retries."""


class RobotsDisallowed(FetchError):
    """Raised when robots.txt forbids the URL and respect_robots is on."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


class HttpClient:
    def __init__(self, settings: HttpSettings, cache_dir: Path):
        self.settings = settings
        self.cache_dir = cache_dir / "http"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-PK,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            }
        )
        if settings.contact_email:
            self.session.headers["From"] = settings.contact_email

        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

        # Set for a run that must see the live page - asking for "today's ads"
        # against a page cached yesterday would return nothing.
        self.skip_cache = False

    # ---------------------------------------------------------------- robots

    def _robots_for(self, url: str) -> RobotFileParser | None:
        host = urlsplit(url).netloc
        if host in self._robots:
            return self._robots[host]

        robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            self._throttle(host)
            response = self.session.get(robots_url, timeout=self.settings.timeout_seconds)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                # No robots.txt served - the convention is that everything is
                # allowed. Log it so the decision is visible.
                log.info("no robots.txt at %s (HTTP %s)", robots_url, response.status_code)
                parser = None
        except requests.RequestException as exc:
            log.warning("could not fetch %s (%s); proceeding without it", robots_url, exc)
            parser = None

        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    # ------------------------------------------------------------- throttling

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self.settings.delay_seconds - (time.monotonic() - last)
            wait += random.uniform(0, self.settings.jitter_seconds)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # ------------------------------------------------------------------ cache

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str) -> Response | None:
        if not self.settings.cache_responses or self.skip_cache:
            return None
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

        if datetime.now(timezone.utc) - fetched_at > timedelta(
            hours=self.settings.cache_ttl_hours
        ):
            return None
        return Response(url=url, status=payload["status"], text=payload["text"], from_cache=True)

    def _write_cache(self, response: Response) -> None:
        if not self.settings.cache_responses:
            return
        payload = {
            "url": response.url,
            "status": response.status,
            "text": response.text,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._cache_path(response.url).write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except OSError as exc:
            log.debug("could not write cache for %s: %s", response.url, exc)

    # ------------------------------------------------------------------ fetch

    def get(self, url: str, *, headers: dict | None = None, allow_cache: bool = True) -> Response:
        """Fetch a URL, honouring robots, rate limits, cache and retries."""
        if allow_cache:
            cached = self._read_cache(url)
            if cached is not None:
                log.debug("cache hit %s", url)
                return cached

        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        host = urlsplit(url).netloc
        last_error: Exception | None = None

        for attempt in range(1, self.settings.max_retries + 1):
            self._throttle(host)
            try:
                response = self.session.get(
                    url, headers=headers, timeout=self.settings.timeout_seconds
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("attempt %s/%s failed for %s: %s",
                            attempt, self.settings.max_retries, url, exc)
            else:
                if response.status_code in RETRYABLE_STATUS:
                    last_error = FetchError(f"HTTP {response.status_code} for {url}")
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        # The server told us how long to wait. Believe it.
                        time.sleep(min(int(retry_after), 120))
                        continue
                    log.warning("attempt %s/%s got HTTP %s for %s",
                                attempt, self.settings.max_retries,
                                response.status_code, url)
                elif response.status_code >= 400:
                    # 404/403 etc. are not going to improve on retry.
                    raise FetchError(f"HTTP {response.status_code} for {url}")
                else:
                    result = Response(url=url, status=response.status_code, text=response.text)
                    self._write_cache(result)
                    return result

            if attempt < self.settings.max_retries:
                time.sleep(self.settings.backoff_base_seconds * (2 ** (attempt - 1)))

        raise FetchError(f"giving up on {url} after {self.settings.max_retries} attempts: {last_error}")

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
