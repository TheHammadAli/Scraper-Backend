"""City -> website location mapping.

Every site names its locations differently, so a city the user picks has to be
translated before it can go in a URL:

    OLX        /<slug>_g<numeric id>/     both come from OLX's locations sitemap
    Zameen     /<Category>/<Name>-<id>-N  slugs come from the page's own state
    PakWheels  /used-cars/search/-/ct_<slug>/  slug is validated against the page

None of these can be derived from the display name alone ("Jaranwala" is
`Faisalabad_Jaranwala-1363` on Zameen, "Mingora" is `mingaora` on OLX), and a
wrong guess is worse than no guess - PakWheels answers an unknown city with
HTTP 200 and all-Pakistan results. So each source's real location list is read
and matched, and a city a site does not have is reported as unsupported
instead of being searched anyway.

Everything here is pure (no network) so it can be tested against saved pages.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .jsonblob import extract_window_json

PROVINCES = (
    "Punjab",
    "Sindh",
    "Khyber Pakhtunkhwa",
    "Balochistan",
    "Islamabad Capital Territory",
    "Gilgit-Baltistan",
    "Azad Jammu & Kashmir",
)

# ------------------------------------------------------------------ normalising


def match_key(name: str) -> str:
    """Spelling-insensitive key: 'Dera Ghazi Khan', 'dera-ghazi-khan' and
    'DeraGhaziKhan' all collapse to the same string."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\(.*?\)", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def city_keys(name: str, aliases: tuple[str, ...] | list[str] = ()) -> set[str]:
    """Every key a city may be found under: its name plus each alias."""
    return {key for key in (match_key(n) for n in (name, *aliases)) if key}


def url_slug(name: str) -> str:
    """'Dera Ghazi Khan' -> 'dera-ghazi-khan'. Never has stray '-' or '%20'."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class LocationResult:
    """Outcome of translating one city for one site.

    `resolved`     - identifier is valid for that site.
    `unsupported`  - the site has no location for this city. Not an error: there
                     is simply nothing to search there.
    `error`        - the lookup itself failed (network, blocked, page changed),
                     so nothing can be said about whether the site has the city.
    """

    status: str
    identifier: str | None = None
    matched: str = ""   # what it matched on the site, e.g. "mingaora"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "resolved"

    @classmethod
    def resolved(cls, identifier: str, matched: str = "") -> "LocationResult":
        return cls("resolved", identifier, matched)

    @classmethod
    def unsupported(cls, reason: str) -> "LocationResult":
        return cls("unsupported", reason=reason)

    @classmethod
    def error(cls, reason: str) -> "LocationResult":
        return cls("error", reason=reason)


@dataclass
class CityOutcome:
    """What happened when one city was scraped for one source + category."""

    city: str
    source: str
    category: str
    status: str = "pending"   # completed | unsupported | failed | cancelled
    reason: str = ""
    search_url: str = ""
    matched: str = ""
    pages_read: int = 0
    listings: int = 0
    # Ads the site padded the page with from other places, left out. The
    # sample names where they were from, for the report.
    foreign_ads: int = 0
    foreign_places: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "city": self.city,
            "source": self.source,
            "category": self.category,
            "status": self.status,
            "reason": self.reason,
            "search_url": self.search_url,
            "matched": self.matched,
            "pages_read": self.pages_read,
            "listings": self.listings,
            "foreign_ads": self.foreign_ads,
        }


# ------------------------------------------------------------------ site indexes


@dataclass(frozen=True)
class SiteLocation:
    """One location a website publishes."""

    source: str
    name: str
    identifier: str
    province: str | None = None
    level: str = "city"   # "city" | "locality"


OLX_PROVINCES = {
    "punjab": "Punjab",
    "sindh": "Sindh",
    "islamabad-capital-territory": "Islamabad Capital Territory",
    "khyber-pakhtunkhwa": "Khyber Pakhtunkhwa",
    "balochistan": "Balochistan",
    "azad-kashmir": "Azad Jammu & Kashmir",
    "northern-areas": "Gilgit-Baltistan",
}

# Zameen's parentID on a city entry is its province's own location id.
ZAMEEN_PROVINCES = {
    "1522": "Punjab",
    "1523": "Sindh",
    "1524": "Balochistan",
    "1525": "Khyber Pakhtunkhwa",
    "1526": "Gilgit-Baltistan",
    "961": "Azad Jammu & Kashmir",
    "1562": "Islamabad Capital Territory",
}

_OLX_LOC = re.compile(r"olx\.com\.pk/([a-z0-9\-]+)_g(\d+)", re.I)


def parse_olx_locations(xml: str) -> list[SiteLocation]:
    """OLX's published locations sitemap -> its city-level locations.

    Ids encode the level: 20xxxxx provinces, 40xxxxx cities, 50xxxxx areas
    (neighbourhoods, which are skipped). A newer 16-digit id scheme is used for
    a mix of areas and a few towns (e.g. Haroonabad), kept as "locality" so a
    city only OLX lists that way can still be found - but a proper city entry
    always wins over one.

    The sitemap lists each province, then that province's locations, so the
    province of an entry is the last province header seen. That is what tells
    apart the two `kotli` entries (Punjab vs Azad Kashmir).
    """
    province: str | None = None
    found: list[SiteLocation] = []

    for slug, ident in _OLX_LOC.findall(xml):
        if len(ident) == 7 and ident.startswith("20"):
            province = OLX_PROVINCES.get(slug, province)
        elif len(ident) == 7 and ident.startswith("40"):
            found.append(SiteLocation("olx", slug.replace("-", " ").title(), f"{slug}_g{ident}", province, "city"))
        elif len(ident) > 7 and ident.startswith("10"):
            found.append(SiteLocation("olx", slug.replace("-", " ").title(), f"{slug}_g{ident}", province, "locality"))
    return found


def parse_zameen_cities(html: str) -> list[SiteLocation]:
    """Zameen's complete city list, from the `window.state` blob every page carries.

    Each entry has the exact slug ('/Faisalabad_Jaranwala-1363') - which cannot
    be rebuilt from the city name - and a parentID naming its province.
    """
    state = extract_window_json(html, "state")
    if not isinstance(state, dict):
        return []
    data = ((state.get("cities") or {}).get("data")) or []

    found: list[SiteLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name, slug = entry.get("name"), entry.get("slug")
        if not (name and slug):
            continue
        found.append(
            SiteLocation(
                "zameen",
                str(name),
                str(slug).lstrip("/"),
                ZAMEEN_PROVINCES.get(str(entry.get("parentID"))),
                "city",
            )
        )
    return found


# --------------------------------------------------------------------- matching


def pick_location(
    candidates: list[SiteLocation],
    keys: set[str],
    province: str | None = None,
) -> SiteLocation | None:
    """The site location for a city, or None if the site does not have it.

    Names repeat across provinces (OLX has two `kotli`, Zameen and OLX each
    have a Kotli in Azad Kashmir while another Kotli is in Punjab), so a
    candidate whose province is known and differs from the city's is never
    taken - even when it is the only name match. Wrongly answering
    "unsupported" costs a reported gap; taking a different town's ads would be
    silent wrong data.
    """
    matches = [c for c in candidates if match_key(c.name) in keys]
    if province:
        matches = [c for c in matches if c.province in (None, province)]
    if not matches:
        return None

    # A real city entry beats a locality-level one.
    if any(c.level == "city" for c in matches):
        matches = [c for c in matches if c.level == "city"]
    return matches[0]


# -------------------------------------------------------------------- PakWheels

_TITLE = re.compile(r"<title>\s*([^<]*?)\s*</title>", re.I)
_TITLE_CITY = re.compile(r"\bfor sale in\s+(.+?)\s*(?:\||$)", re.I)


def pakwheels_page_city(html: str) -> str | None:
    """The city a PakWheels listing page is actually scoped to.

    Read from the page title ('Cars for sale in Lahore | PakWheels'). An
    unknown `ct_` slug does NOT 404 - it serves all-Pakistan results under
    'Used Cars for sale in Pakistan' - so this is the only reliable signal that
    the city filter was accepted.
    """
    title = _TITLE.search(html)
    if not title:
        return None
    match = _TITLE_CITY.search(title.group(1))
    if not match:
        return None
    city = match.group(1).strip()
    return None if match_key(city) in ("pakistan", "") else city
