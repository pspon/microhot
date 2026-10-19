"""Data records stored on the data branch as JSON."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from .textutil import parse_iso


def _from_dict(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Item:
    """One entry fetched from a feed."""

    id: str
    source_id: str
    url: str
    title: str
    snippet: str
    fetched: str
    published: str | None = None
    lang: str = "en"
    fp: str = ""
    event_id: str | None = None

    def when(self) -> datetime:
        """Best guess of when the item appeared: its published time unless that looks wrong."""
        fetched = parse_iso(self.fetched)
        published = parse_iso(self.published)
        if published and fetched and published <= fetched:
            return published
        return fetched or published  # type: ignore[return-value]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Item:
        return _from_dict(cls, data)


@dataclass
class Event:
    """A story: one or more items from different sources about the same thing."""

    id: str
    created: str
    item_ids: list[str] = field(default_factory=list)
    rep: str = ""  # representative item id (sent to the models)
    beat: str = "ai"
    last_seen: str = ""
    # triage
    type: str | None = None
    axes: dict | None = None
    score: int | None = None
    opinions: int = 0
    triaged_at: str | None = None
    triaged_by: str | None = None
    triage_tries: int = 0
    # selection and writing
    selected_at: str | None = None
    story: dict | None = None
    write_attempts: int = 0
    # ranking
    heat: float = 0.0
    n_sources: int = 0
    merged_into: str | None = None

    @property
    def is_live(self) -> bool:
        return self.merged_into is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        return _from_dict(cls, data)
