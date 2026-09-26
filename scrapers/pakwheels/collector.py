"""PakWheels collector.

PakWheels pages are server-rendered and carry schema.org markup, so JSON-LD is
the primary extraction path with CSS selectors as the fallback. City slugs go
straight into the URL (`ct_lahore`) - but PakWheels does not reject a slug it
does not know: it answers HTTP 200 with all-Pakistan results. So every slug is
checked against the page title before it is used (see lookup_city).
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator

from core.config import Category, City
from core.http import FetchError, Response
from core.locations import LocationResult, match_key, pakwheels_page_city, url_slug
from core.models import Listing
from core.normalize import clean_text, parse_ad_date, parse_price

from scrapers.base import (
    BaseCollector,
    extract_jsonld,
    first_text,
    jsonld_of_type,
    soup_of,
    text_of,
)

log = logging.getLogger(__name__)

# /used-cars/kia-sportage-2021-for-sale-in-lahore-12017889
# The ad id is the trailing number on the slug, not a separate path segment.
AD_HREF = re.compile(r"/(?:used-cars|used-bikes)/[^/?#]*?-(\d{5,})(?:[/?#]|$)", re.I)

# ...-for-sale-in-<city>-<id>: the city an ad is in, as PakWheels writes it.
AD_CITY = re.compile(r"-for-sale-in-(.+?)-\d{5,}(?:[/?#]|$)", re.I)

# The seller's text sits in the div immediately after the "Seller's Comments"
# heading, not inside it.
DESCRIPTION_SELECTORS = [
    "#scroll_seller_comments + div",
    "#scroll_seller_comments ~ div",
    ".seller-comment",
    "[itemprop='description']",
]

# "... Last Updated: Sep 19, 2026 Ad Ref # 12017889"
LAST_UPDATED = re.compile(r"Last Updated:\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", re.I)
AD_REF = re.compile(r"Ad Ref\s*#\s*(\d+)", re.I)

PRICE_SELECTORS = [
    ".price-box strong",
    ".price-box",
    "[itemprop='price']",
    ".generic-green strong",
]

DATE_SELECTORS = [
    "#scroll_car_detail .nomargin",
    ".detail-page-header small",
    "time",
]

# The seller block is marked up as schema.org AutoDealer, so the meta tag is
# the stablest read; the heading is the visual fallback.
SELLER_SELECTORS = [
    ".owner-details meta[itemprop='name']",
    ".owner-detail-main h5",
    ".owner-details h5",
    ".ad-numbers strong",
]


class PakWheelsCollector(BaseCollector):
    source_name = "pakwheels"

    # ------------------------------------------------------------- city lookup

    PROBE_TTL = timedelta(days=30)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._probe_path = self.config.cache_dir / "pakwheels_cities.json"
        self._probes: dict[str, dict] = self._load_probes()

    def _load_probes(self) -> dict[str, dict]:
        try:
            return json.loads(self._probe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_probes(self) -> None:
        try:
            self._probe_path.parent.mkdir(parents=True, exist_ok=True)
            self._probe_path.write_text(
                json.dumps(self._probes, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            log.debug("could not persist PakWheels city probes: %s", exc)

    def _remember(self, key: str, slug: str | None) -> None:
        self._probes[key] = {"slug": slug, "at": datetime.now(timezone.utc).isoformat()}
        self._save_probes()

    def lookup_city(self, city: City) -> LocationResult:
        """Find the `ct_` slug PakWheels actually accepts for the city.

        Tries the city's name, then its aliases, and keeps the first slug whose
        page is scoped to that city ('Cars for sale in <City>'). The answer -
        including "PakWheels does not have it" - is remembered for 30 days, so a
        city costs one small request once, not one per run.
        """
        key = match_key(city.name)
        cached = self._probes.get(key)
        if cached:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["at"])
            if age < self.PROBE_TTL:
                if cached["slug"]:
                    return LocationResult.resolved(cached["slug"], matched=cached["slug"])
                return LocationResult.unsupported(f"PakWheels has no listings page for {city.name}")

        slugs = list(dict.fromkeys(url_slug(n) for n in (city.name, *city.aliases)))[:4]
        for slug in slugs:
            try:
                html = self.http.peek(f"{self.base_url}/used-cars/search/-/ct_{slug}/?page=1").text
            except FetchError as exc:
                # A 4xx just means this spelling is not a page - try the next.
                # Anything else (network, 5xx, robots) means the check itself
                # could not be made, so nothing is recorded about the city.
                if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                    continue
                raise
            scoped = pakwheels_page_city(html)
            if scoped and match_key(scoped) in city.keys:
                self._remember(key, slug)
                return LocationResult.resolved(slug, matched=scoped)

        self._remember(key, None)
        return LocationResult.unsupported(f"PakWheels has no listings page for {city.name}")

    def validate_index(self, response: Response, city: City, category: Category) -> str | None:
        """The bikes (or any other) page must be scoped to the city too - the
        cars-page probe above says nothing about them."""
        scoped = pakwheels_page_city(response.text)
        if scoped is None or match_key(scoped) not in city.keys:
            served = "all-Pakistan" if scoped is None else scoped
            return (
                f"PakWheels served {served} results for {city.name} / {category.label}, "
                f"not {city.name} listings - skipped so other cities' ads are not filed "
                f"under {city.name}"
            )
        return None

    # ---------------------------------------------------------------- indexing

    def index_urls(self, city: City, identifier: str, category: Category) -> Iterator[str]:
        # robots.txt disallows `?sortby=`, so these pages come back in
        # PakWheels' own default order - a dated run has to read further in
        # rather than sort. Unbounded here - collect() in base.py enforces the
        # actual page budget (or, by default, reads until a page comes back
        # empty).
        for page in itertools.count(1):
            yield f"{self.base_url}/{category.path}/ct_{identifier}/?page={page}"

    def parse_index(self, response: Response, city: City, category: Category) -> list[Listing]:
        page = soup_of(response.text)
        listings: list[Listing] = []
        seen: set[str] = set()

        # `a.car-name` is the search-result title link. A looser selector also
        # picks up featured/related ads from other cities, which would pollute
        # the city-wise grouping.
        anchors = page.select("a.car-name")
        if not anchors:
            anchors = page.select("a[href*='/used-cars/'], a[href*='/used-bikes/']")

        # Prices come from the page's own JSON-LD, keyed by listing URL.
        prices = self._prices_by_url(response.text)

        for anchor in anchors:
            href = anchor.get("href") or ""
            match = AD_HREF.search(href)
            if not match:
                continue
            ad_id = match.group(1)
            if ad_id in seen:
                continue
            seen.add(ad_id)

            title = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
            if not title:
                continue

            url = self.absolute(href)
            listing = Listing(
                source=self.source_name,
                source_listing_id=ad_id,
                city=city.name,
                category=category.label,
                title=title,
                url=url,
            )
            in_url = AD_CITY.search(href)
            if in_url:
                listing.located_in = (in_url.group(1).replace("-", " "),)

            # Take the price from the index so a failed detail fetch still
            # leaves a usable row.
            price_value = prices.get(url) or prices.get(url.rstrip("/"))
            if price_value is not None:
                listing.price, listing.price_currency, listing.price_raw = parse_price(
                    str(price_value)
                )

            listings.append(listing)

        return listings

    @staticmethod
    def _prices_by_url(html: str) -> dict[str, object]:
        """Map listing URL -> price from the index page's JSON-LD Product blocks."""
        prices: dict[str, object] = {}
        for item in extract_jsonld(html):
            raw_type = item.get("@type", "")
            types = [raw_type] if isinstance(raw_type, str) else list(raw_type or [])
            if not any(str(t).lower() == "product" for t in types):
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict) and offers.get("url") and offers.get("price") is not None:
                prices[str(offers["url"])] = offers["price"]
        return prices

    # ------------------------------------------------------------------ detail

    def parse_detail(self, response: Response, listing: Listing) -> Listing:
        page = soup_of(response.text)
        jsonld = extract_jsonld(response.text)
        product = jsonld_of_type(jsonld, "Car", "Product", "Vehicle", "Motorcycle")

        if product:
            listing.title = clean_text(product.get("name") or "") or listing.title
            listing.description = clean_text(product.get("description") or "") or listing.description

            offers = product.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict) and offers.get("price") is not None:
                listing.price, listing.price_currency, listing.price_raw = parse_price(
                    str(offers.get("price"))
                )
                currency = offers.get("priceCurrency")
                if currency:
                    listing.price_currency = str(currency)

        if not listing.description:
            listing.description = first_text(page, DESCRIPTION_SELECTORS)

        if listing.price is None:
            price_text = first_text(page, PRICE_SELECTORS)
            if price_text:
                listing.price, listing.price_currency, listing.price_raw = parse_price(price_text)

        if listing.ad_date is None:
            listing.ad_date = self._parse_listed_date(page)

        if not listing.source_listing_id:
            listing.source_listing_id = self._parse_ad_ref(page)

        if not listing.seller_name:
            listing.seller_name = self._parse_seller(page)

        # PakWheels hides the seller's number behind a logged-in "Show Phone
        # Number" call. That is not automated here - any number captured is one
        # the seller wrote into the public ad text themselves.
        return self.apply_phone_policy(listing, listing.description, listing.title)

    @staticmethod
    def _parse_listed_date(page):
        """Read 'Last Updated: Sep 19, 2026' out of the spec block."""
        spec_text = text_of(page.select_one("#scroll_car_detail"))
        match = LAST_UPDATED.search(spec_text)
        if match:
            parsed = parse_ad_date(match.group(1))
            if parsed:
                return parsed
        return parse_ad_date(first_text(page, DATE_SELECTORS))

    @staticmethod
    def _parse_seller(page) -> str | None:
        """The seller's public display name from the Seller Details block."""
        for selector in SELLER_SELECTORS:
            node = page.select_one(selector)
            if not node:
                continue
            # The schema.org version carries the name in `content`.
            name = clean_text(node.get("content") or node.get_text(" ", strip=True))
            if name and len(name) <= 80:
                return name
        return None

    @staticmethod
    def _parse_ad_ref(page) -> str | None:
        """'Ad Ref # 12017889' - the listing id as PakWheels shows it."""
        match = AD_REF.search(text_of(page.select_one("#scroll_car_detail")))
        return match.group(1) if match else None
