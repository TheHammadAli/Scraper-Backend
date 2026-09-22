"""The common listing record every collector returns."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOURCES = ("olx", "pakwheels", "zameen")

_TRACKING_PARAM = re.compile(
    r"^(utm_|gclid|fbclid|ref$|referrer|campaign|_ga|msclkid|igshid)", re.I
)


def canonical_url(url: str) -> str:
    """Strip tracking noise so one ad cannot dedupe into two rows."""
    parts = urlsplit(url.strip())
    keep = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAM.match(k)]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit((parts.scheme or "https", netloc, path, urlencode(sorted(keep)), ""))


@dataclass(slots=True)
class Listing:
    """One ad, normalized across all three sources.

    `fingerprint` is the dedup key: it prefers source + source_listing_id,
    falls back to the canonical URL, and only then to a content hash - so a
    listing with no visible id still cannot land in the table twice.
    """

    source: str
    city: str
    category: str
    title: str
    url: str

    source_listing_id: str | None = None
    description: str = ""
    price: Decimal | None = None
    price_currency: str = "PKR"
    price_raw: str = ""
    phone: str | None = None
    # The seller's display name as the site shows it publicly - a person's
    # first name on OLX/PakWheels, an agency name on Zameen.
    seller_name: str | None = None
    # The publicly displayed neighbourhood/society/area within the city, e.g.
    # "DHA Phase 5" or "Gulshan-e-Iqbal" - a category the seller picked from
    # the site's own location picker, shown to every visitor under the ad
    # title. This is NOT a street/house address and never comes from a gated
    # source: OLX and Zameen both expose it as the deepest entry in the ad's
    # public location hierarchy (Country > Province > City > Area). PakWheels
    # does not surface anything finer than city on a listing, so this stays
    # empty there.
    area: str = ""
    ad_date: date | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- identity

    @property
    def url_canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def fingerprint(self) -> str:
        if self.source_listing_id:
            return f"{self.source}:id:{self.source_listing_id}"
        if self.url:
            return f"{self.source}:url:{self.url_canonical}"
        blob = f"{self.source}|{self.city}|{self.category}|{self.title}|{self.price_raw}"
        return f"{self.source}:hash:{hashlib.sha256(blob.encode()).hexdigest()[:32]}"

    # -------------------------------------------------------------- validation

    def validate(self) -> list[str]:
        """Return a list of problems. An empty list means the record is storable."""
        problems: list[str] = []

        if self.source not in SOURCES:
            problems.append(f"unknown source {self.source!r}")
        if not self.city or not self.city.strip():
            problems.append("city is empty")
        if not self.title or not self.title.strip():
            problems.append("title is empty")
        elif len(self.title) > 500:
            problems.append("title is implausibly long")
        if not self.url.strip().startswith(("http://", "https://")):
            problems.append(f"url is not absolute: {self.url!r}")
        if self.price is not None:
            if self.price < 0:
                problems.append(f"negative price: {self.price}")
            elif self.price > Decimal("1e12"):
                problems.append(f"price out of plausible range: {self.price}")
        if self.ad_date is not None:
            today = datetime.now(timezone.utc).date()
            if self.ad_date > today:
                problems.append(f"ad_date is in the future: {self.ad_date}")
            elif self.ad_date.year < 2005:
                problems.append(f"ad_date is implausibly old: {self.ad_date}")

        return problems

    @property
    def is_valid(self) -> bool:
        return not self.validate()

    # ------------------------------------------------------------ serialization

    def to_row(self) -> dict:
        """Flat dict whose keys match the listings table columns."""
        return {
            "source": self.source,
            "source_listing_id": self.source_listing_id,
            "fingerprint": self.fingerprint,
            "city": self.city.strip(),
            "area": (self.area or "").strip(),
            "category": self.category,
            "title": self.title.strip(),
            "description": (self.description or "").strip(),
            "price": float(self.price) if self.price is not None else None,
            "price_currency": self.price_currency,
            "price_raw": self.price_raw,
            "phone": self.phone,
            "seller_name": (self.seller_name or "").strip() or None,
            "ad_date": self.ad_date.isoformat() if self.ad_date else None,
            "url": self.url,
            "url_canonical": self.url_canonical,
            "scraped_at": self.scraped_at.isoformat(),
        }
