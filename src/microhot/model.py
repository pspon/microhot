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
    publisher: str = ""  # the outlet behind an item from a news-search feed (empty for ordinary feeds)

    @property
    def origin(self) -> str:
        """The independent source this report counts as: its publisher when it came through a
        news-search feed (many outlets in one feed), otherwise the feed itself."""
        return f"pub:{self.publisher.casefold()}" if self.publisher else self.source_id

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
    adjust: dict | None = None  # boost points from config rules: {"points": int, "why": [...]}
    # selection and writing
    selected_at: str | None = None
    story: dict | None = None
    write_attempts: int = 0
    digest: dict | None = None  # overview for big stories (several sources)
    digest_tries: int = 0
    editions: list[str] = field(default_factory=list)  # editions that carried this story
    # ranking
    heat: float = 0.0
    heat_at: str | None = None
    heat_prev: float | None = None  # heat at the previous run, for the change shown on the site
    heat_prev_at: str | None = None
    n_sources: int = 0
    sources_48h: int = 0  # independent sources inside the heat window
    sources_6h: int = 0  # sources whose first report on this story is under 6 hours old
    merged_into: str | None = None

    @property
    def is_live(self) -> bool:
        return self.merged_into is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        return _from_dict(cls, data)
