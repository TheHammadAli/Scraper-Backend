"""Per-site collectors. Each returns the same `Listing` shape."""

from __future__ import annotations

from core.config import Config
from core.db import Database
from core.http import HttpClient

from .base import BaseCollector
from .olx.collector import OlxCollector
from .pakwheels.collector import PakWheelsCollector
from .zameen.collector import ZameenCollector

COLLECTORS: dict[str, type[BaseCollector]] = {
    "olx": OlxCollector,
    "pakwheels": PakWheelsCollector,
    "zameen": ZameenCollector,
}


def build_collector(
    source_name: str, config: Config, http: HttpClient, db: Database | None = None
) -> BaseCollector:
    try:
        collector_cls = COLLECTORS[source_name]
    except KeyError:
        raise ValueError(f"no collector registered for source {source_name!r}") from None
    return collector_cls(config=config, source=config.sources[source_name], http=http, db=db)


__all__ = [
    "BaseCollector",
    "OlxCollector",
    "PakWheelsCollector",
    "ZameenCollector",
    "COLLECTORS",
    "build_collector",
]
