"""Zameen.com collector.

Zameen URLs carry a `<City>-<id>` slug, read from the city list Zameen embeds in
its own pages (see core/locations.py). Detail pages expose both JSON-LD and an inline `window.state`
payload; JSON-LD is tried first, then the state blob, then CSS selectors.

Unlike OLX and PakWheels, Zameen agency listings often publish the contact
number directly in the public page payload - where it is present there, it is
already public and is read as-is. Nothing gated is unmasked.
"""

from __future__ import annotations

import itertools
import logging
import re
from typing import Iterator

from core.config import Category, City
from core.http import FetchError, Response
from core.locations import LocationResult, SiteLocation, parse_zameen_cities, pick_location
from core.models import Listing
from core.normalize import clean_text, normalize_phone, parse_ad_date, parse_price

from scrapers.base import (
    BaseCollector,
    area_from_location_hierarchy,
    extract_jsonld,
    extract_window_json,
    find_first_key,
    first_text,
    jsonld_of_type,
    places_from_location_hierarchy,
    soup_of,
)

log = logging.getLogger(__name__)

# /Property/<area>_<slug>-<listing id>-<area id>-<page>.html
#
# The listing id is the FIRST number in the trailing group and is always long;
# the short numbers after it are internal. Matching the last number instead
# yields "1" for every listing.
PROPERTY_HREF = re.compile(r"/Property/[^\s\"']*?-(\d{5,})(?:-\d+)*\.html", re.I)

DESCRIPTION_SELECTORS = [
    "[aria-label='Property description']",
    "div[class*='description']",
    "#preview-description",
    "[itemprop='description']",
]

PRICE_SELECTORS = [
    "[aria-label='Price']",
    "span[class*='price']",
    "[itemprop='price']",
]

DATE_SELECTORS = [
    "[aria-label='Creation date']",
    "span[class*='created']",
    "time",
]


class ZameenCollector(BaseCollector):
    source_name = "zameen"

    # ------------------------------------------------------------- city lookup

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cities: list[SiteLocation] | None = None

    def _city_index(self) -> list[SiteLocation]:
        """Zameen's full city list, read once per collector.

        Every Zameen page carries it in `window.state.cities`. The homepage's
        visible links only cover a dozen cities, and a slug like
        `Faisalabad_Jaranwala-1363` cannot be built from a name - so this is
        the only dependable source.
        """
        if self._cities is None:
            with self.http.cached_lookup():
                html = self.http.get(self.base_url).text
            found = parse_zameen_cities(html)
            if not found:
                raise FetchError(
                    "Zameen's page no longer carries its city list - its format may have changed"
                )
            self._cities = found
        return self._cities

    def lookup_city(self, city: City) -> LocationResult:
        """Zameen's '<City>-<id>' slug for the city.

        Reported unsupported, not guessed, when Zameen has no such city - a
        wrong id would silently collect a different city's listings.
        """
        hit = pick_location(self._city_index(), city.keys, city.province)
        if hit is None:
            return LocationResult.unsupported(f"Zameen has no city page for {city.name}")
        return LocationResult.resolved(hit.identifier, matched=hit.name)

    # ---------------------------------------------------------------- indexing

    def index_urls(self, city: City, identifier: str, category: Category) -> Iterator[str]:
        # Unbounded - collect() in base.py enforces the actual page budget
        # (or, by default, reads until a page comes back empty).
        for page in itertools.count(1):
            yield f"{self.base_url}/{category.path}/{identifier}-{page}.html"

    def parse_index(self, response: Response, city: City, category: Category) -> list[Listing]:
        page = soup_of(response.text)
        listings: list[Listing] = []
        seen: set[str] = set()

        # Where each ad says it is, from the same state blob, keyed by ad id.
        state = extract_window_json(response.text, "state") or {}
        hits = ((state.get("algolia") or {}).get("content") or {}).get("hits") or []
        places_by_id = {
            str(hit.get("externalID")): places_from_location_hierarchy(hit.get("location"))
            for hit in hits
            if isinstance(hit, dict)
        }

        for anchor in page.select("a[href*='/Property/']"):
            href = anchor.get("href") or ""

            # Share links (facebook/twitter/mailto) carry the listing URL in
            # their query string and would otherwise match.
            if not (href.startswith("/Property/") or href.startswith(f"{self.base_url}/Property/")):
                continue

            match = PROPERTY_HREF.search(href)
            if not match:
                continue
            property_id = match.group(1)
            if property_id in seen:
                continue
            seen.add(property_id)

            title = clean_text(
                anchor.get("title")
                or anchor.get("aria-label")
                or anchor.get_text(" ", strip=True)
            )

            listings.append(
                Listing(
                    source=self.source_name,
                    source_listing_id=property_id,
                    city=city.name,
                    category=category.label,
                    title=title or f"Zameen listing {property_id}",
                    url=self.absolute(href),
                    located_in=places_by_id.get(property_id, ()),
                )
            )

        return listings

    # ------------------------------------------------------------------ detail

    def parse_detail(self, response: Response, listing: Listing) -> Listing:
        page = soup_of(response.text)
        jsonld = extract_jsonld(response.text)
        product = jsonld_of_type(
            jsonld, "Product", "Residence", "Offer", "RealEstateListing", "Place"
        )

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
                if offers.get("priceCurrency"):
                    listing.price_currency = str(offers["priceCurrency"])

        state = extract_window_json(response.text, "state")
        if state:
            listing = self._apply_state(listing, state)

        if not listing.description:
            listing.description = first_text(page, DESCRIPTION_SELECTORS)
        if listing.price is None:
            price_text = first_text(page, PRICE_SELECTORS)
            if price_text:
                listing.price, listing.price_currency, listing.price_raw = parse_price(price_text)
        if listing.ad_date is None:
            listing.ad_date = parse_ad_date(first_text(page, DATE_SELECTORS))

        return self.apply_phone_policy(listing, listing.description, listing.title)

    @staticmethod
    def _published_phone(state: dict) -> str | None:
        """The contact number Zameen renders on the public listing page.

        Zameen serves this to every visitor - it is the number shown next to
        the "Call" button, and the payload flags whether a login is needed via
        `requiresLoginForContact`. When that flag is set, the number is gated
        and this returns None rather than reaching for it.

        Shapes seen in the wild:
            property.data.primaryPhoneNumber  -> "+923001234567"
            property.data.mobilePhoneNumber   -> "+923001234567"
            property.data.phoneNumber         -> {"mobileNumbers": [...],
                                                  "phoneNumbers":  [...],
                                                  "whatsapp": "92300..."}
        """
        data = (state.get("property") or {}).get("data")
        if not isinstance(data, dict):
            data = state

        if data.get("requiresLoginForContact") is True:
            log.debug("[zameen] contact is login-gated; leaving phone empty")
            return None

        candidates: list[object] = [
            data.get("primaryPhoneNumber"),
            data.get("mobilePhoneNumber"),
        ]

        block = data.get("phoneNumber")
        if isinstance(block, dict):
            for key in ("mobileNumbers", "phoneNumbers"):
                value = block.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
            candidates.append(block.get("whatsapp"))
        elif isinstance(block, str):
            candidates.append(block)

        for candidate in candidates:
            if isinstance(candidate, str):
                normalized = normalize_phone(candidate)
                if normalized:
                    return normalized
        return None

    def _apply_state(self, listing: Listing, state: dict) -> Listing:
        """Read fields out of Zameen's inline state blob."""
        if not listing.description:
            listing.description = clean_text(
                find_first_key(state, "description", "descriptionText") or ""
            )
        if listing.price is None:
            raw = find_first_key(state, "price", "priceValue")
            if raw is not None:
                listing.price, listing.price_currency, listing.price_raw = parse_price(str(raw))
        if listing.ad_date is None:
            listing.ad_date = parse_ad_date(
                find_first_key(state, "createdAt", "created_at", "activeDate", "creationDate")
            )
        if not listing.seller_name:
            data = (state.get("property") or {}).get("data") or state
            name = data.get("contactName") or find_first_key(state, "contactName")
            if name:
                listing.seller_name = clean_text(str(name)) or None

        if not listing.area:
            # The society/area Zameen shows under the listing title to every
            # visitor - see area_from_location_hierarchy() for what this is
            # and is not.
            data = (state.get("property") or {}).get("data") or state
            listing.area = area_from_location_hierarchy(data.get("location"))

        if not listing.source_listing_id:
            found = find_first_key(state, "externalID", "id", "listingId")
            listing.source_listing_id = str(found) if found else None

        # The contact number Zameen renders publicly on the page itself.
        if self.config.collection.collect_phone and not listing.phone:
            listing.phone = self._published_phone(state)

        return listing
