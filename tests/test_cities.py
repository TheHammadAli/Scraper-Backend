"""City list and city -> website mapping. Offline: no network.

Run with:  python -m unittest tests.test_cities   (or `discover tests`)

The live counterpart, which hits the real sites for a city from every
province, is tests/live_city_matrix.py.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_config  # noqa: E402
from core.db import Database  # noqa: E402
from core.http import FetchError, Response  # noqa: E402
from core.locations import (  # noqa: E402
    PROVINCES,
    match_key,
    pakwheels_page_city,
    parse_olx_locations,
    parse_zameen_cities,
    pick_location,
    url_slug,
)
from core.pipeline import Pipeline  # noqa: E402
from scrapers import build_collector  # noqa: E402

CONFIG = load_config()

# Cities named in the requirements, one or more per region.
REQUIRED = {
    "Punjab": ["Lahore", "Rawalpindi", "Faisalabad", "Multan", "Chakwal", "Sialkot",
               "Gujranwala", "Bahawalpur", "Sargodha"],
    "Sindh": ["Karachi", "Hyderabad", "Sukkur", "Larkana"],
    "Khyber Pakhtunkhwa": ["Peshawar", "Abbottabad", "Mardan", "Mingora"],
    "Balochistan": ["Quetta", "Gwadar", "Turbat"],
    "Islamabad Capital Territory": ["Islamabad"],
    "Azad Jammu & Kashmir": ["Muzaffarabad", "Mirpur"],
    "Gilgit-Baltistan": ["Gilgit", "Skardu"],
}

OLX_SITEMAP = """<urlset>
<url><loc>https://www.olx.com.pk/punjab_g2003006</loc></url>
<url><loc>https://www.olx.com.pk/lahore_g4060673</loc></url>
<url><loc>https://www.olx.com.pk/johar-town_g5000042</loc></url>
<url><loc>https://www.olx.com.pk/chakwal_g4065543</loc></url>
<url><loc>https://www.olx.com.pk/kotli_g4065544</loc></url>
<url><loc>https://www.olx.com.pk/haroonabad_g1000000000002084</loc></url>
<url><loc>https://www.olx.com.pk/cantt_g5000047</loc></url>
<url><loc>https://www.olx.com.pk/khyber-pakhtunkhwa_g2003005</loc></url>
<url><loc>https://www.olx.com.pk/mingaora_g4060641</loc></url>
<url><loc>https://www.olx.com.pk/azad-kashmir_g2003000</loc></url>
<url><loc>https://www.olx.com.pk/kotli_g4065560</loc></url>
</urlset>"""

ZAMEEN_HOME = (
    "<html><script>window.state = "
    + json.dumps({"cities": {"data": [
        {"name": "Lahore", "slug": "/Lahore-1", "parentID": "1522"},
        {"name": "Jaranwala", "slug": "/Faisalabad_Jaranwala-1363", "parentID": "1522"},
        {"name": "Nankana Sahib", "slug": "/Nankana_Sahib_-1687", "parentID": "1522"},
        {"name": "Kotli", "slug": "/Kotli-968", "parentID": "961"},
        {"name": "Mingora", "slug": "/Mingora-13476", "parentID": "1525"},
    ]}})
    + ";</script></html>"
)


def city(name: str):
    found = CONFIG.find_city(name)
    assert found is not None, name
    return found


# ---------------------------------------------------------------------- dataset


class TestCityDataset(unittest.TestCase):
    def test_every_region_is_represented(self):
        present = {c.province for c in CONFIG.cities}
        self.assertEqual(present, set(PROVINCES))

    def test_required_cities_are_in_their_region(self):
        for province, names in REQUIRED.items():
            for name in names:
                with self.subTest(city=name):
                    self.assertEqual(city(name).province, province)

    def test_list_is_comprehensive_not_just_the_majors(self):
        self.assertGreaterEqual(len(CONFIG.cities), 250)
        # The old hand-kept list stopped at 15. Cities from every tier exist now.
        for name in ["Chakwal", "Muzaffarabad", "Skardu", "Turbat", "Mingora", "Kot Addu"]:
            self.assertIsNotNone(CONFIG.find_city(name), name)

    def test_names_are_unique_and_a_shared_spelling_is_always_province_separated(self):
        names = [c.name.lower() for c in CONFIG.cities]
        self.assertEqual(len(names), len(set(names)))

        owners: dict[str, list] = {}
        for c in CONFIG.cities:
            for key in c.keys:
                owners.setdefault(key, []).append(c)
        for key, cities in owners.items():
            if len(cities) > 1:
                provinces = [c.province for c in cities]
                self.assertEqual(
                    len(provinces), len(set(provinces)),
                    f"{key!r} names {[c.name for c in cities]} in the same province",
                )

    def test_a_cities_own_name_beats_another_cities_alias(self):
        self.assertEqual(CONFIG.find_city("Kotli").province, "Azad Jammu & Kashmir")
        self.assertEqual(CONFIG.find_city("Kotli Loharan").province, "Punjab")

    def test_every_city_is_supported_by_at_least_one_site(self):
        for c in CONFIG.cities:
            with self.subTest(city=c.name):
                self.assertTrue(any(c.coverage.values()), f"{c.name} is on no site")

    def test_alias_and_spelling_lookup(self):
        self.assertEqual(CONFIG.find_city("Mingaora").name, "Mingora")
        self.assertEqual(CONFIG.find_city("dera-ghazi-khan").name, "Dera Ghazi Khan")
        self.assertEqual(CONFIG.find_city("  RAWALPINDI ").name, "Rawalpindi")
        self.assertEqual(CONFIG.find_city("DG Khan").name, "Dera Ghazi Khan")
        self.assertIsNone(CONFIG.find_city("Atlantis"))

    def test_unknown_city_in_a_run_is_an_error_not_a_silent_skip(self):
        with self.assertRaisesRegex(ValueError, "unknown cities: Atlantis"):
            CONFIG.enabled_cities(["Lahore", "Atlantis"])

    def test_selection_order_does_not_matter_and_nothing_is_dropped(self):
        picked = [c.name for c in CONFIG.enabled_cities(["Gilgit", "Quetta", "Chakwal", "Lahore"])]
        self.assertEqual(sorted(picked), ["Chakwal", "Gilgit", "Lahore", "Quetta"])

    def test_api_returns_the_whole_list_uncapped(self):
        import main

        payload = main.get_config()
        self.assertEqual(len(payload["cities"]), len(CONFIG.cities))
        by_name = {c["name"]: c for c in payload["cities"]}
        self.assertIn("Skardu", by_name)
        self.assertIn("Mingaora", by_name["Mingora"]["aliases"])
        self.assertIn("olx", by_name["Lahore"]["coverage"])


class TestUrlHygiene(unittest.TestCase):
    """A slug must never carry a space, '%20', a doubled or a stray dash."""

    SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_slug_examples(self):
        self.assertEqual(url_slug("Rawalpindi "), "rawalpindi")
        self.assertEqual(url_slug("Dera  Ghazi--Khan"), "dera-ghazi-khan")
        self.assertEqual(url_slug("D.G. Khan"), "d-g-khan")
        self.assertEqual(url_slug("Khairpur Mir's"), "khairpur-mir-s")
        self.assertEqual(url_slug("Mingora"), "mingora")

    def test_every_city_and_alias_makes_a_clean_slug(self):
        for c in CONFIG.cities:
            for name in (c.name, *c.aliases):
                with self.subTest(name=name):
                    slug = url_slug(name)
                    self.assertRegex(slug, self.SLUG)
                    self.assertNotIn("%", slug)


class TestMatchKey(unittest.TestCase):
    def test_spelling_variants_collapse(self):
        self.assertEqual(match_key("Dera Ghazi Khan"), match_key("dera-ghazi-khan"))
        self.assertEqual(match_key("Rahim Yar Khan"), match_key("rahimyar-khan"))
        self.assertEqual(match_key("Nowshera (Cantt)"), match_key("Nowshera"))
        self.assertNotEqual(match_key("Mirpur"), match_key("Mirpur Khas"))
        self.assertNotEqual(match_key("Dina"), match_key("Dinga"))


# ----------------------------------------------------------------- site indexes


class TestOlxLocations(unittest.TestCase):
    def setUp(self):
        self.index = parse_olx_locations(OLX_SITEMAP)

    def test_areas_are_skipped_and_cities_kept(self):
        slugs = {loc.identifier.split("_g")[0] for loc in self.index}
        self.assertIn("lahore", slugs)
        self.assertNotIn("johar-town", slugs)   # area
        self.assertNotIn("cantt", slugs)        # area
        self.assertNotIn("punjab", slugs)       # province header

    def test_province_comes_from_document_order(self):
        provinces = {loc.identifier: loc.province for loc in self.index}
        self.assertEqual(provinces["kotli_g4065544"], "Punjab")
        self.assertEqual(provinces["kotli_g4065560"], "Azad Jammu & Kashmir")
        self.assertEqual(provinces["mingaora_g4060641"], "Khyber Pakhtunkhwa")

    def test_alias_spelling_resolves(self):
        hit = pick_location(self.index, city("Mingora").keys, "Khyber Pakhtunkhwa")
        self.assertEqual(hit.identifier, "mingaora_g4060641")

    def test_same_name_in_two_provinces_picks_the_right_one(self):
        ajk = pick_location(self.index, city("Kotli").keys, "Azad Jammu & Kashmir")
        punjab = pick_location(self.index, city("Kotli Loharan").keys, "Punjab")
        self.assertEqual(ajk.identifier, "kotli_g4065560")
        self.assertEqual(punjab.identifier, "kotli_g4065544")

    def test_a_town_of_the_same_name_in_another_province_is_never_taken(self):
        # Only a Punjab "kotli" exists here; the AJK city must not get it.
        only_punjab = [loc for loc in self.index if loc.identifier == "kotli_g4065544"]
        self.assertIsNone(pick_location(only_punjab, city("Kotli").keys, "Azad Jammu & Kashmir"))

    def test_city_the_site_lacks_is_none(self):
        self.assertIsNone(pick_location(self.index, city("Turbat").keys, "Balochistan"))

    def test_locality_level_is_a_fallback_only(self):
        hit = pick_location(self.index, city("Haroonabad").keys, "Punjab")
        self.assertEqual(hit.level, "locality")


class TestZameenCities(unittest.TestCase):
    def setUp(self):
        self.index = parse_zameen_cities(ZAMEEN_HOME)

    def test_exact_slugs_that_cannot_be_derived_from_the_name(self):
        jaranwala = pick_location(self.index, city("Jaranwala").keys, "Punjab")
        nankana = pick_location(self.index, city("Nankana Sahib").keys, "Punjab")
        self.assertEqual(jaranwala.identifier, "Faisalabad_Jaranwala-1363")
        self.assertEqual(nankana.identifier, "Nankana_Sahib_-1687")

    def test_province_is_read_from_parent_id(self):
        self.assertEqual({c.name: c.province for c in self.index}["Kotli"], "Azad Jammu & Kashmir")

    def test_punjab_kotli_does_not_match_the_ajk_one(self):
        self.assertIsNone(pick_location(self.index, city("Kotli Loharan").keys, "Punjab"))

    def test_page_without_a_city_list_parses_to_nothing(self):
        self.assertEqual(parse_zameen_cities("<html>no state</html>"), [])


class TestPakWheelsScope(unittest.TestCase):
    def test_reads_the_city_a_page_is_scoped_to(self):
        page = "<html><head><title>Cars for sale in Lahore | PakWheels</title></head>"
        self.assertEqual(pakwheels_page_city(page), "Lahore")

    def test_all_pakistan_fallback_is_not_a_city(self):
        page = "<title>Used Cars for sale in Pakistan | PakWheels</title>"
        self.assertIsNone(pakwheels_page_city(page))

    def test_bikes_and_missing_title(self):
        self.assertEqual(
            pakwheels_page_city("<title>Used Bikes for sale in Karachi | PakWheels</title>"), "Karachi"
        )
        self.assertIsNone(pakwheels_page_city("<html></html>"))


# ------------------------------------------------------------ collectors + run


class FakeHttp:
    """Answers from a dict; anything not listed is a 404, an Exception is raised."""

    skip_cache = False

    def __init__(self, pages=None, peeks=None):
        self.pages = pages or {}
        self.peeks = peeks or {}
        self.requested: list[str] = []

    @contextmanager
    def cached_lookup(self):
        yield

    def _answer(self, table, url):
        self.requested.append(url)
        value = table.get(url)
        if value is None:
            raise FetchError(f"HTTP 404 for {url}", 404)
        if isinstance(value, Exception):
            raise value
        return Response(url=url, status=200, text=value)

    def get(self, url, **_):
        return self._answer(self.pages, url)

    def peek(self, url, **_):
        return self._answer(self.peeks, url)


def olx_page(count: int, city: str = "Lahore", foreign: int = 0, foreign_city: str = "Astore",
             first_id: int = 1000, locate: bool = True) -> str:
    """An OLX index page: `count` ads in `city`, then `foreign` ads from another
    place - which is what OLX does when a small city runs out of its own ads."""

    def ad(number: int, place: str) -> dict:
        hit = {
            "externalID": str(first_id + number),
            "title": f"Honda Civic {number} for sale",
            "slug": f"honda-civic-{number}",
            "description": "Excellent condition, single owner, all documents complete and ready.",
            "extraFields": {"price": 4500000},
        }
        if locate:
            hit["location"] = [
                {"level": 0, "name": "Pakistan"}, {"level": 1, "name": "Some Province"},
                {"level": 2, "name": place}, {"level": 3, "name": "Some Area"},
            ]
        return hit

    hits = [ad(i, city) for i in range(count)]
    hits += [ad(count + i, foreign_city) for i in range(foreign)]
    state = {"algolia": {"content": {"hits": hits, "nbPages": 2}}}
    return f"<html><script>window.state = {json.dumps(state)};</script></html>"


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.config = dataclasses.replace(CONFIG, cache_dir=tmp / "cache")
        self.db = Database(tmp / "t.db")
        self.olx = self.config.sources["olx"]
        self.category = self.olx.categories[0]
        self.results: list[dict] = []

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def olx_url(self, identifier: str, page: int = 1) -> str:
        suffix = f"?page={page}" if page > 1 else ""
        return f"{self.olx.base_url}/{identifier}/{self.category.path}/{suffix}"

    def run_olx(self, http, cities):
        pipeline = Pipeline(self.config, self.db, http)
        stats = pipeline.run(
            city_names=cities, source_names=["olx"], category_keys=[self.category.key],
            on_city_result=self.results.append,
        )
        return stats

    def sitemap(self):
        return f"{self.olx.base_url}/sitemap/searches/locations.xml"


class TestEveryCityIsProcessed(PipelineCase):
    def test_lahore_chakwal_turbat_each_get_their_own_outcome(self):
        """Lahore has ads, Chakwal is searched but empty, Turbat is not on OLX."""
        http = FakeHttp(pages={
            self.sitemap(): OLX_SITEMAP,
            self.olx_url("lahore_g4060673"): olx_page(2),
            self.olx_url("lahore_g4060673", 2): olx_page(0),
            self.olx_url("chakwal_g4065543"): olx_page(0),
        })
        stats = self.run_olx(http, ["Lahore", "Turbat", "Chakwal"])

        by_city = {r["city"]: r for r in self.results}
        self.assertEqual(set(by_city), {"Lahore", "Chakwal", "Turbat"})

        self.assertEqual(by_city["Lahore"]["status"], "completed")
        self.assertEqual(by_city["Lahore"]["listings"], 2)
        self.assertEqual(by_city["Lahore"]["search_url"], self.olx_url("lahore_g4060673"))

        # Zero ads is a completed search, not a failure.
        self.assertEqual(by_city["Chakwal"]["status"], "completed")
        self.assertEqual(by_city["Chakwal"]["listings"], 0)
        self.assertIn("no ads", by_city["Chakwal"]["reason"])

        # A city the site does not have is reported as such - and is not an error.
        self.assertEqual(by_city["Turbat"]["status"], "unsupported")
        self.assertIn("no location page for Turbat", by_city["Turbat"]["reason"])

        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.unsupported, 1)
        self.assertEqual(len(stats.city_results), 3)

    def test_listings_keep_the_city_they_were_collected_for(self):
        http = FakeHttp(pages={
            self.sitemap(): OLX_SITEMAP,
            self.olx_url("lahore_g4060673"): olx_page(2),
            self.olx_url("lahore_g4060673", 2): olx_page(0),
            self.olx_url("chakwal_g4065543"): olx_page(0),
        })
        self.run_olx(http, ["Lahore", "Chakwal"])
        rows = self.db.conn.execute("SELECT DISTINCT city FROM listings").fetchall()
        self.assertEqual([r["city"] for r in rows], ["Lahore"])

    def test_a_failed_search_is_failed_not_zero_and_does_not_stop_other_cities(self):
        http = FakeHttp(pages={
            self.sitemap(): OLX_SITEMAP,
            self.olx_url("lahore_g4060673"): FetchError("HTTP 503 for x", 503),
            self.olx_url("chakwal_g4065543"): olx_page(0),
        })
        stats = self.run_olx(http, ["Lahore", "Chakwal"])
        by_city = {r["city"]: r for r in self.results}
        self.assertEqual(by_city["Lahore"]["status"], "failed")
        self.assertIn("search request failed", by_city["Lahore"]["reason"])
        self.assertEqual(by_city["Chakwal"]["status"], "completed")
        self.assertEqual(stats.errors, 1)

    def test_a_404_on_the_first_page_means_the_site_has_no_such_page(self):
        http = FakeHttp(pages={self.sitemap(): OLX_SITEMAP})   # city resolves, page 404s
        self.run_olx(http, ["Lahore"])
        self.assertEqual(self.results[0]["status"], "unsupported")
        self.assertIn("HTTP 404", self.results[0]["reason"])

    def test_failure_on_a_later_page_keeps_what_was_read(self):
        http = FakeHttp(pages={
            self.sitemap(): OLX_SITEMAP,
            self.olx_url("lahore_g4060673"): olx_page(2),
            self.olx_url("lahore_g4060673", 2): FetchError("HTTP 503 for x", 503),
        })
        self.run_olx(http, ["Lahore"])
        result = self.results[0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["listings"], 2)
        self.assertIn("stopped early at page 2", result["reason"])

    def test_a_lookup_that_cannot_run_is_reported_per_city(self):
        http = FakeHttp(pages={self.sitemap(): FetchError("HTTP 503 for x", 503)})
        stats = self.run_olx(http, ["Lahore", "Chakwal"])
        self.assertEqual([r["status"] for r in self.results], ["failed", "failed"])
        self.assertEqual(stats.errors, 2)

    def test_an_unexpected_exception_still_leaves_a_record(self):
        pipeline = Pipeline(self.config, self.db, FakeHttp(pages={self.sitemap(): OLX_SITEMAP}))

        def boom(*_, **__):
            raise RuntimeError("disk full")

        pipeline._collect_one = boom
        stats = pipeline.run(
            city_names=["Lahore"], source_names=["olx"], category_keys=[self.category.key],
            on_city_result=self.results.append,
        )
        self.assertEqual(self.results[0]["status"], "failed")
        self.assertIn("disk full", self.results[0]["reason"])
        self.assertEqual(stats.errors, 1)


class TestOlxRequests(PipelineCase):
    def collector(self, http=None):
        return build_collector("olx", self.config, http or FakeHttp({self.sitemap(): OLX_SITEMAP}), self.db)

    def test_alias_city_uses_olxs_own_spelling_in_the_url(self):
        c = self.collector()
        loc = c.resolve_city(city("Mingora"))
        self.assertTrue(loc.ok)
        url = next(c.index_urls(city("Mingora"), loc.identifier, self.category))
        self.assertEqual(url, self.olx_url("mingaora_g4060641"))
        self.assertNotIn("%", url)

    def test_two_kotlis_are_kept_apart(self):
        c = self.collector()
        self.assertEqual(c.resolve_city(city("Kotli")).identifier, "kotli_g4065560")
        self.assertEqual(c.resolve_city(city("Kotli Loharan")).identifier, "kotli_g4065544")

    def test_a_pinned_numeric_id_becomes_a_full_slug(self):
        pinned = dataclasses.replace(city("Chakwal"), olx_location_id="4065543")
        self.assertEqual(self.collector().resolve_city(pinned).identifier, "chakwal_g4065543")

    def test_sitemap_is_fetched_once_per_run(self):
        http = FakeHttp({self.sitemap(): OLX_SITEMAP})
        c = self.collector(http)
        for name in ("Lahore", "Chakwal", "Mingora"):
            c.resolve_city(city(name))
        self.assertEqual(http.requested.count(self.sitemap()), 1)


class TestZameenRequests(PipelineCase):
    def test_zameen_urls_use_the_exact_slug(self):
        zameen = self.config.sources["zameen"]
        cat = zameen.categories[0]
        http = FakeHttp({zameen.base_url: ZAMEEN_HOME})
        c = build_collector("zameen", self.config, http, self.db)

        loc = c.resolve_city(city("Jaranwala"))
        self.assertEqual(loc.identifier, "Faisalabad_Jaranwala-1363")
        self.assertEqual(
            next(c.index_urls(city("Jaranwala"), loc.identifier, cat)),
            f"{zameen.base_url}/{cat.path}/Faisalabad_Jaranwala-1363-1.html",
        )

    def test_unknown_and_wrong_province_cities_are_unsupported(self):
        http = FakeHttp({self.config.sources["zameen"].base_url: ZAMEEN_HOME})
        c = build_collector("zameen", self.config, http, self.db)
        self.assertEqual(c.resolve_city(city("Turbat")).status, "unsupported")
        self.assertEqual(c.resolve_city(city("Kotli Loharan")).status, "unsupported")


class TestPakWheelsRequests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.pw = self.config.sources["pakwheels"]

    def probe(self, slug):
        return f"{self.pw.base_url}/used-cars/search/-/ct_{slug}/?page=1"

    @staticmethod
    def title(text):
        return f"<html><head><title>{text}</title></head></html>"

    def test_alias_slug_is_tried_and_the_answer_cached(self):
        http = FakeHttp(peeks={
            self.probe("wah"): self.title("Used Cars for sale in Pakistan | PakWheels"),
            self.probe("wah-cantt"): self.title("Cars for sale in Wah Cantt | PakWheels"),
        })
        c = build_collector("pakwheels", self.config, http, self.db)
        first = c.resolve_city(city("Wah"))
        self.assertEqual((first.status, first.identifier), ("resolved", "wah-cantt"))
        asked = len(http.requested)

        # A fresh collector reads the persisted answer - no more requests.
        again = build_collector("pakwheels", self.config, http, self.db).resolve_city(city("Wah"))
        self.assertEqual(again.identifier, "wah-cantt")
        self.assertEqual(len(http.requested), asked)

    def test_a_city_pakwheels_does_not_serve_is_unsupported_and_remembered(self):
        http = FakeHttp(peeks={
            self.probe(url_slug("Kunjah")): self.title("Used Cars for sale in Pakistan | PakWheels"),
        })
        c = build_collector("pakwheels", self.config, http, self.db)
        self.assertEqual(c.resolve_city(city("Kunjah")).status, "unsupported")
        asked = len(http.requested)
        self.assertEqual(c.resolve_city(city("Kunjah")).status, "unsupported")
        self.assertEqual(len(http.requested), asked)

    def test_all_pakistan_results_are_never_filed_under_the_city(self):
        """The cars probe passes, but the bikes page comes back unscoped."""
        cat = self.pw.categories[-1]
        page_url = f"{self.pw.base_url}/{cat.path}/ct_lahore/?page=1"
        http = FakeHttp(
            peeks={self.probe("lahore"): self.title("Cars for sale in Lahore | PakWheels")},
            pages={page_url: self.title("Used Bikes for sale in Pakistan | PakWheels")},
        )
        pipeline = Pipeline(self.config, self.db, http)
        stats = pipeline.run(
            city_names=["Lahore"], source_names=["pakwheels"], category_keys=[cat.key],
            on_city_result=self.results.append,
        )
        self.assertEqual(self.results[0]["status"], "unsupported")
        self.assertIn("all-Pakistan", self.results[0]["reason"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 0)
        self.assertEqual(stats.errors, 0)


class TestForeignAdGuard(PipelineCase):
    """A small city runs out of ads and the site pads with a neighbour's - measured
    live: Skardu's OLX page 1 = 10 Skardu + 14 Astore/Hunza, page 2 = none of its own."""

    def sitemap_pages(self, **pages):
        return {self.sitemap(): OLX_SITEMAP, **pages}

    def test_padding_ads_are_not_filed_under_the_city_and_paging_stops(self):
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("chakwal_g4065543"): olx_page(3, "Chakwal", foreign=4),
            self.olx_url("chakwal_g4065543", 2): olx_page(0, foreign=5, first_id=2000),
            self.olx_url("chakwal_g4065543", 3): olx_page(0, foreign=5, first_id=3000),
            self.olx_url("chakwal_g4065543", 4): olx_page(0, foreign=5, first_id=4000),
        }))
        self.run_olx(http, ["Chakwal"])

        result = self.results[0]
        self.assertEqual((result["status"], result["listings"]), ("completed", 3))
        self.assertEqual(result["foreign_ads"], 14)
        self.assertIn("from other places (Astore)", result["reason"])
        # Two pages in a row with none of the city's ads = exhausted: page 4 is never asked for.
        self.assertNotIn(self.olx_url("chakwal_g4065543", 4), http.requested)
        cities = {r["city"] for r in self.db.conn.execute("SELECT city FROM listings")}
        self.assertEqual(cities, {"Chakwal"})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 3)

    def test_a_city_with_no_ads_of_its_own_is_zero_not_a_failure(self):
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("chakwal_g4065543"): olx_page(0, foreign=6),
            self.olx_url("chakwal_g4065543", 2): olx_page(0, foreign=6, first_id=2000),
        }))
        stats = self.run_olx(http, ["Chakwal"])
        result = self.results[0]
        self.assertEqual((result["status"], result["listings"]), ("completed", 0))
        self.assertIn("no ads in Chakwal", result["reason"])
        self.assertIn("left out", result["reason"])
        self.assertEqual(stats.errors, 0)

    def test_own_ads_on_a_later_page_are_still_found(self):
        """One padded page is not the end - only two in a row are."""
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("chakwal_g4065543"): olx_page(2, "Chakwal"),
            self.olx_url("chakwal_g4065543", 2): olx_page(0, foreign=3, first_id=2000),
            self.olx_url("chakwal_g4065543", 3): olx_page(2, "Chakwal", first_id=3000),
            self.olx_url("chakwal_g4065543", 4): olx_page(0, first_id=4000),
        }))
        self.run_olx(http, ["Chakwal"])
        self.assertEqual(self.results[0]["listings"], 4)

    def test_ads_that_do_not_say_where_they_are_are_kept(self):
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("chakwal_g4065543"): olx_page(3, "Chakwal", locate=False),
            self.olx_url("chakwal_g4065543", 2): olx_page(0),
        }))
        self.run_olx(http, ["Chakwal"])
        self.assertEqual(self.results[0]["listings"], 3)
        self.assertEqual(self.results[0]["foreign_ads"], 0)

    def test_the_alias_spelling_is_not_mistaken_for_another_place(self):
        """OLX writes Mingora as 'Mingaora' in an ad's location."""
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("mingaora_g4060641"): olx_page(3, "Mingaora"),
            self.olx_url("mingaora_g4060641", 2): olx_page(0),
        }))
        self.run_olx(http, ["Mingora"])
        self.assertEqual(self.results[0]["listings"], 3)


    def test_a_site_that_repeats_page_one_does_not_page_forever(self):
        """PakWheels serves page 1 again as page 2 for a small city. The repeats
        are de-duplicated, so without a stop rule the loop only ends at the
        20,000-page safety cap."""
        same = olx_page(3, "Chakwal")
        http = FakeHttp(pages=self.sitemap_pages(**{
            self.olx_url("chakwal_g4065543"): same,
            self.olx_url("chakwal_g4065543", 2): same,
            self.olx_url("chakwal_g4065543", 3): same,
            self.olx_url("chakwal_g4065543", 4): same,
        }))
        self.run_olx(http, ["Chakwal"])
        self.assertEqual(self.results[0]["listings"], 3)
        self.assertNotIn(self.olx_url("chakwal_g4065543", 4), http.requested)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 3)


class TestPerAdCity(PipelineCase):
    def test_pakwheels_reads_the_city_from_each_ads_url(self):
        pw = self.config.sources["pakwheels"]
        c = build_collector("pakwheels", self.config, FakeHttp(), self.db)
        page = "".join(
            f'<a class="car-name" title="Car {n}" '
            f'href="/used-cars/car-{n}-for-sale-in-{slug}-{1200000 + n}">x</a>'
            for n, slug in enumerate(["wah-cantt", "quetta", "wah-cantt", "lahore"])
        )
        stubs = c.parse_index(Response("u", 200, page), city("Wah"), pw.categories[0])
        own, others = c.split_by_city(stubs, city("Wah"))
        self.assertEqual(len(own), 2)
        self.assertEqual({s.located_in[0] for s in others}, {"quetta", "lahore"})

    def test_zameen_matches_state_locations_to_anchors_by_id(self):
        zameen = self.config.sources["zameen"]
        c = build_collector("zameen", self.config, FakeHttp(), self.db)

        def hit(ad_id, place):
            return {"externalID": ad_id, "location": [
                {"level": 0, "name": "Pakistan"}, {"level": 1, "name": "Punjab"},
                {"level": 2, "name": place}]}

        state = {"algolia": {"content": {"hits": [hit("5500001", "Lahore"), hit("5500002", "Gujrat")]}}}
        html = (
            f"<html><script>window.state = {json.dumps(state)};</script>"
            '<a href="/Property/a_house-5500001-9-1.html" title="House A">x</a>'
            '<a href="/Property/b_house-5500002-9-1.html" title="House B">x</a>'
            '<a href="/Property/c_house-5500003-9-1.html" title="House C, not in state">x</a>'
            "</html>"
        )
        stubs = c.parse_index(Response("u", 200, html), city("Lahore"), zameen.categories[0])
        own, others = c.split_by_city(stubs, city("Lahore"))
        self.assertEqual({s.source_listing_id for s in own}, {"5500001", "5500003"})  # unknown = kept
        self.assertEqual([s.source_listing_id for s in others], ["5500002"])

    def test_a_404_past_the_last_zameen_page_is_just_the_end(self):
        zameen = self.config.sources["zameen"]
        cat = zameen.categories[0]
        state = {"algolia": {"content": {"hits": [{"externalID": "5500001", "location": [
            {"level": 2, "name": "Chakwal"}]}]}}}
        page1 = (f"<html><script>window.state = {json.dumps(state)};</script>"
                 '<a href="/Property/a_house-5500001-9-1.html" title="House A">x</a></html>')
        home = ZAMEEN_HOME.replace('"Lahore"', '"Chakwal"').replace("/Lahore-1", "/Chakwal-751")
        http = FakeHttp(pages={
            zameen.base_url: home,
            f"{zameen.base_url}/{cat.path}/Chakwal-751-1.html": page1,
        })   # page 2 is not listed, so FakeHttp answers 404
        pipeline = Pipeline(self.config, self.db, http)
        pipeline.run(city_names=["Chakwal"], source_names=["zameen"], category_keys=[cat.key],
                     on_city_result=self.results.append)
        result = self.results[0]
        self.assertEqual((result["status"], result["listings"]), ("completed", 1))
        self.assertNotIn("stopped early", result["reason"])


if __name__ == "__main__":
    unittest.main()
