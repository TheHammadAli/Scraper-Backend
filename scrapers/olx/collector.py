"""OLX Pakistan collector.

OLX has a JSON search endpoint under /api/, but its robots.txt disallows /api/
outright, so it is not used. Instead both the category pages and the ad pages
embed a `window.state` blob that already contains the full ad records:

    index page  -> state.algolia.content.hits[]
    detail page -> state.ad.data

Reading the index blob means one request per 25 ads instead of 26, which is
both faster and far lighter on the site. CSS/anchor scraping remains as the
fallback if that blob ever moves.

Cities are resolved from OLX's own published locations sitemap, which is the
sanctioned way to discover location URLs.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator

from core.config import Category, City
from core.http import FetchError, Response
from core.models import Listing
from core.normalize import city_slug, clean_text, parse_ad_date, parse_price

from scrapers.base import (
    BaseCollector,
    extract_jsonld,
    extract_window_json,
    find_first_key,
    jsonld_of_type,
    soup_of,
)

log = logging.getLogger(__name__)

# /item/<slug>-iid-<numeric id>
ITEM_HREF = re.compile(r"/item/([^/?#]*?)-iid-(\d+)", re.I)

LOCATIONS_SITEMAP = "/sitemap/searches/locations.xml"


class OlxCollector(BaseCollector):
    source_name = "olx"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nb_pages: int | None = None

    # ------------------------------------------------------------- city lookup

    def lookup_city(self, city: City) -> str | None:
        """Resolve a city name to OLX's numeric location id.

        Reads OLX's published locations sitemap and matches `<slug>_g<id>`.
        Returns None rather than guessing - a wrong id would quietly collect a
        different city's ads.
        """
        slug = city_slug(city.name)

        try:
            xml = self.http.get(f"{self.base_url}{LOCATIONS_SITEMAP}").text
        except FetchError as exc:
            log.warning("[olx] could not read locations sitemap: %s", exc)
            return None

        match = re.search(rf"/{re.escape(slug)}_g(\d+)", xml, re.I)
        return match.group(1) if match else None

    # ---------------------------------------------------------------- indexing

    def index_urls(self, city: City, identifier: str, category: Category) -> Iterator[str]:
        """Yield category pages, newest-capable ordering not being available.

        OLX ranks a category page by `productScore desc` and only then by
        timestamp, so these pages are NOT in date order. `?sorting=desc-creation`
        is accepted by the page and does change
        `state.algolia.settings.sort.key`, but the server-rendered `hits` come
        back in an identical order - the chosen sort is applied in the browser
        against a search endpoint whose path robots.txt disallows. Measured on
        Lahore/mobile-phones: page 1's freshest ad was 91 minutes old while
        page 10 held one 55 minutes old.

        So the parameter is deliberately not sent (it would only fragment
        OLX's CDN cache for no gain) and date coverage comes from reading more
        pages instead - see CollectionSettings.max_pages_when_dated.
        """
        limits = self.config.collection
        pages = max(limits.max_pages_per_city_category, limits.max_pages_when_dated)
        slug = f"{city_slug(city.name)}_g{identifier}"
        for page in range(1, pages + 1):
            suffix = f"?page={page}" if page > 1 else ""
            yield f"{self.base_url}/{slug}/{category.path}/{suffix}"

    def total_pages(self) -> int | None:
        return self._nb_pages

    def parse_index(self, response: Response, city: City, category: Category) -> list[Listing]:
        state = extract_window_json(response.text, "state")

        # nbPages is what OLX says the full result set runs to. Keep it so the
        # run can report "read 5 of 818 pages" instead of implying it saw
        # everything. (nbHits is per-slot and much smaller - not the total.)
        content = ((state or {}).get("algolia") or {}).get("content") or {}
        nb_pages = content.get("nbPages")
        if isinstance(nb_pages, int) and nb_pages > 0:
            self._nb_pages = nb_pages

        hits = self._search_hits(state)

        if hits:
            listings = []
            for ad in hits:
                listing = self._listing_from_ad(ad, city, category)
                if listing:
                    listings.append(listing)
            if listings:
                return listings
            log.debug("[olx] state blob held no usable ads; falling back to anchors")

        return self._parse_anchor_index(response, city, category)

    @staticmethod
    def _search_hits(state: dict | None) -> list[dict]:
        """state.algolia.content.hits - the ads rendered on a category page."""
        if not isinstance(state, dict):
            return []
        content = (state.get("algolia") or {}).get("content") or {}
        hits = content.get("hits")
        return [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []

    def _parse_anchor_index(
        self, response: Response, city: City, category: Category
    ) -> list[Listing]:
        """Fallback: scrape /item/<slug>-iid-<id> links off the rendered page."""
        page = soup_of(response.text)
        listings: list[Listing] = []
        seen: set[str] = set()

        for anchor in page.select("a[href*='/item/']"):
            href = anchor.get("href") or ""
            match = ITEM_HREF.search(href)
            if not match:
                continue
            ad_id = match.group(2)
            if ad_id in seen:
                continue
            seen.add(ad_id)

            title = clean_text(anchor.get_text(" ", strip=True))
            listings.append(
                Listing(
                    source=self.source_name,
                    source_listing_id=ad_id,
                    city=city.name,
                    category=category.label,
                    title=title or match.group(1).replace("-", " ").title(),
                    url=self.absolute(href),
                )
            )
        return listings

    # ------------------------------------------------------------- ad -> model

    def _listing_from_ad(self, ad: dict, city: City, category: Category) -> Listing | None:
        ad_id = ad.get("externalID") or ad.get("id")
        title = clean_text(ad.get("title") or "")
        if not (ad_id and title):
            return None

        slug = ad.get("slug") or city_slug(title)[:80]
        url = f"{self.base_url}/item/{slug}-iid-{ad_id}"

        price, currency, price_raw = self._price_of(ad)
        description = clean_text(ad.get("description") or "")

        listing = Listing(
            source=self.source_name,
            source_listing_id=str(ad_id),
            city=city.name,
            category=category.label,
            title=title,
            description=description,
            price=price,
            price_currency=currency,
            price_raw=price_raw,
            seller_name=self._seller_of(ad),
            ad_date=parse_ad_date(ad.get("createdAt") or ad.get("updatedAt")),
            url=url,
        )

        # OLX's payload carries only contactInfo.roles ("show_phone_number")
        # and the seller's display name - never the number itself, which sits
        # behind an authenticated reveal this project does not call. So the
        # only number available is one the seller typed into their own ad text.
        return self.apply_phone_policy(listing, description, title)

    @staticmethod
    def _seller_of(ad: dict) -> str | None:
        """The seller's public display name.

        A dealer listing carries an agency name, which is the more useful
        label; a private ad carries the person's first name in contactInfo.
        Both are rendered on the page to every visitor.
        """
        agency = ad.get("agency")
        if isinstance(agency, dict) and agency.get("name"):
            return clean_text(str(agency["name"])) or None

        contact = ad.get("contactInfo")
        if isinstance(contact, dict) and contact.get("name"):
            return clean_text(str(contact["name"])) or None

        return None

    @staticmethod
    def _price_of(ad: dict) -> tuple[Any, str, str]:
        """Find the ad's price.

        For vehicles and property OLX leaves the top-level `price` at 0 and
        puts the real figure in `extraFields.price`, so that is checked first.
        `formattedExtraFields` covers categories that shape it differently.
        """
        empty = (None, 0, "0", "")

        extra = ad.get("extraFields")
        raw = extra.get("price") if isinstance(extra, dict) else None

        if raw in empty:
            raw = ad.get("price")

        if raw in empty:
            for field in ad.get("formattedExtraFields") or []:
                if isinstance(field, dict) and str(field.get("attribute", "")).lower() == "price":
                    raw = field.get("formattedValue")
                    break

        if isinstance(raw, dict):
            raw = find_first_key(raw, "value", "raw", "display_value")

        if raw in empty:
            return None, "PKR", ""

        return parse_price(str(raw))

    # ------------------------------------------------------------------ detail

    def needs_detail(self, listing: Listing) -> bool:
        # The index blob already carries the full description, so a detail
        # fetch is only worth it when that came back empty.
        return not listing.description or len(listing.description) < 40

    def parse_detail(self, response: Response, listing: Listing) -> Listing:
        state = extract_window_json(response.text, "state")
        ad = None
        if isinstance(state, dict):
            # Target state.ad.data specifically - a loose search would happily
            # return one of the relatedAds objects instead.
            ad = (state.get("ad") or {}).get("data")

        if isinstance(ad, dict):
            listing.title = clean_text(ad.get("title") or "") or listing.title
            listing.description = clean_text(ad.get("description") or "") or listing.description
            if listing.price is None:
                listing.price, listing.price_currency, listing.price_raw = self._price_of(ad)
            if listing.ad_date is None:
                listing.ad_date = parse_ad_date(ad.get("createdAt") or ad.get("updatedAt"))
            if not listing.source_listing_id and ad.get("externalID"):
                listing.source_listing_id = str(ad["externalID"])
            listing.seller_name = listing.seller_name or self._seller_of(ad)
        else:
            # Last resort: the ad page's schema.org markup.
            product = jsonld_of_type(extract_jsonld(response.text), "Product", "Car", "WebPage")
            if product:
                listing.description = (
                    clean_text(product.get("description") or "") or listing.description
                )
                listing.title = clean_text(product.get("name") or "") or listing.title
                offers = product.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if listing.price is None and isinstance(offers, dict) and offers.get("price"):
                    listing.price, listing.price_currency, listing.price_raw = parse_price(
                        str(offers["price"])
                    )
                if not listing.source_listing_id and product.get("sku"):
                    listing.source_listing_id = str(product["sku"])

        return self.apply_phone_policy(listing, listing.description, listing.title)
