"""Pulls OLX's category list from OLX itself.

Hand-writing category slugs does not work: a wrong OLX category id returns an
empty page rather than an error, so a typo fails silently.

Two sources, in order of preference:

1. The category tree in the homepage's `window.state.categories.data`. This is
   what OLX's own menu renders, so it carries the real hierarchy (14 sections
   such as Mobiles and Vehicles) and OLX's own labels.
2. The categories sitemap, as a fallback. Flat - no sections - but it still
   yields every category id if the tree ever moves.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .jsonblob import extract_window_json

log = logging.getLogger(__name__)

CATEGORIES_SITEMAP = "/sitemap/searches/categories.xml"
SLUG_WITH_ID = re.compile(r"olx\.com\.pk/([a-z0-9\-]+)_c(\d+)", re.I)

UNGROUPED = "Other"

# Words that look wrong in Title Case, used only by the sitemap fallback.
ACRONYMS = {
    "tv": "TV", "ac": "AC", "it": "IT", "pc": "PC", "cctv": "CCTV",
    "gps": "GPS", "led": "LED", "lcd": "LCD", "usb": "USB", "suv": "SUV",
    "atv": "ATV", "hr": "HR", "ui": "UI", "ux": "UX", "seo": "SEO",
}
JOINERS = {"and", "or", "for", "of", "the", "on", "in", "with"}


def humanize(slug: str) -> str:
    """'electronics-home-appliances' -> 'Electronics Home Appliances'."""
    words = []
    for index, word in enumerate(slug.split("-")):
        lowered = word.lower()
        if lowered in ACRONYMS:
            words.append(ACRONYMS[lowered])
        elif index > 0 and lowered in JOINERS:
            words.append(lowered)
        else:
            words.append(word.capitalize())
    return " ".join(words)


# ------------------------------------------------------------------ the tree


def parse_category_tree(state: dict | None) -> list[dict]:
    """Flatten `state.categories.data` into config-shaped dicts.

    Every entry keeps `group` - the name of the top-level section it belongs
    to - so the UI can show Mobiles, Vehicles and so on as real sections
    instead of one flat list.
    """
    sections = ((state or {}).get("categories") or {}).get("data")
    if not isinstance(sections, list) or not sections:
        return []

    found: list[dict] = []

    def walk(node: dict, group: str, depth: int) -> None:
        slug = node.get("slug")
        external_id = node.get("externalID")
        name = node.get("name")
        if slug and external_id and name:
            found.append(
                {
                    "key": str(slug).lower(),
                    "label": str(name),
                    "path": f"{slug}_c{external_id}",
                    "category_id": str(external_id),
                    "group": group,
                    "level": depth,
                    "priority": node.get("displayPriority") or 0,
                }
            )
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, group, depth + 1)

    for section in sections:
        if isinstance(section, dict) and section.get("name"):
            walk(section, str(section["name"]), 0)

    return _dedupe(found)


def _dedupe(entries: list[dict]) -> list[dict]:
    """One entry per category id, with unique keys.

    A slug can be reused under two different ids - `houses` is both c1719
    (for sale) and c1721 (for rent) - so those keys get the id appended and
    the section name added to the label to tell them apart.
    """
    by_id: dict[str, dict] = {}
    for entry in entries:
        by_id.setdefault(entry["category_id"], entry)

    unique = list(by_id.values())
    key_counts = Counter(e["key"] for e in unique)

    for entry in unique:
        if key_counts[entry["key"]] > 1:
            entry["key"] = f"{entry['key']}-c{entry['category_id']}"
            if entry.get("group") and entry["group"] != entry["label"]:
                entry["label"] = f"{entry['label']} ({entry['group']})"

    return unique


# -------------------------------------------------------------- the fallback


def parse_categories(xml: str) -> list[dict]:
    """Flat category list from the sitemap. Used only if the tree is missing.

    A category id appears under several slugs (brand landing pages such as
    `apple-tablets_c1455` alongside `tablets_c1455`); the shortest is canonical.
    """
    by_id: dict[int, set[str]] = defaultdict(set)
    for slug, category_id in SLUG_WITH_ID.findall(xml):
        by_id[int(category_id)].add(slug.lower())

    canonical = {
        category_id: min(slugs, key=lambda s: (len(s), s))
        for category_id, slugs in by_id.items()
    }
    slug_counts = Counter(canonical.values())

    categories = []
    for category_id in sorted(canonical):
        slug = canonical[category_id]
        shared = slug_counts[slug] > 1
        categories.append(
            {
                "key": f"{slug}-c{category_id}" if shared else slug,
                "label": f"{humanize(slug)} (c{category_id})" if shared else humanize(slug),
                "path": f"{slug}_c{category_id}",
                "category_id": str(category_id),
                "group": UNGROUPED,
                "level": 1,
                "priority": 0,
            }
        )
    return categories


# ----------------------------------------------------------------- sync/load


def sync_olx_categories(http, base_url: str, config_dir: Path) -> list[dict]:
    """Fetch OLX's categories and write config/olx_categories.json."""
    base_url = base_url.rstrip("/")
    categories: list[dict] = []
    origin = "category tree"

    try:
        state = extract_window_json(http.get(base_url).text, "state")
        categories = parse_category_tree(state)
    except Exception as exc:
        log.warning("could not read the category tree: %s", exc)

    if not categories:
        log.info("falling back to the categories sitemap (no sections available)")
        origin = "sitemap"
        categories = parse_categories(http.get(f"{base_url}{CATEGORIES_SITEMAP}").text)

    if not categories:
        raise RuntimeError("OLX returned no categories from either source")

    target = config_dir / "olx_categories.json"
    target.write_text(
        json.dumps(
            {
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "source": f"{base_url} ({origin})",
                "categories": categories,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    sections = len({c.get("group") for c in categories})
    log.info("synced %s OLX categories across %s sections -> %s",
             len(categories), sections, target)
    return categories


def load_synced_categories(config_dir: Path) -> list[dict]:
    """Read the synced list, or an empty list if it has never been synced."""
    path = config_dir / "olx_categories.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("categories", [])
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
