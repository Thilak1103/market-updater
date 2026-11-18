"""Collectors. Each returns Item objects; storage and dedupe happen in ingest.store."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    url: str
    title: str
    text: str
    source_name: str
    source_type: str
    kind: str = "article"          # article | podcast | headline | email | inbox | transcript | discovery
    published: datetime | None = None
    is_subscriber: bool = False
    sectors: list[str] = field(default_factory=list)
    audio_url: str | None = None
