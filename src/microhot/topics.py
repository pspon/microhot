"""Keyword topics and boost/mute rules from config. Plain rules, no model calls.

config/topics.toml defines topic pages (an event belongs to a topic when its text matches any
pattern). config/site.toml [adjust] adds boost points (by pattern or topic) before thresholds.
Mute patterns are applied while fetching, so muted items never reach a model.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

from .config import Settings
from .model import Event, Item

GROUPS = ("org", "gov", "tech")


@dataclass
class Topic:
    id: str
    name_en: str
    name_zh: str
    group: str = "tech"
    patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._regex = re.compile("|".join(f"(?:{p})" for p in self.patterns), re.IGNORECASE) if self.patterns else None

    def matches(self, text: str) -> bool:
        return bool(self._regex and self._regex.search(text))

    def name(self, lang: str) -> str:
        return self.name_zh if lang == "zh-TW" else self.name_en


def load_topics(settings: Settings) -> list[Topic]:
    path = settings.root / "config" / "topics.toml"
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    topics, seen = [], set()
    for entry in raw.get("topic", []):
        tid = str(entry["id"]).strip()
        if tid in seen or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", tid):
            raise ValueError(f"topic id {tid!r} must be unique, lowercase letters, digits and dashes")
        seen.add(tid)
        group = entry.get("group", "tech")
        if group not in GROUPS:
            raise ValueError(f"topic {tid}: group must be one of {GROUPS}")
        topics.append(
            Topic(
                id=tid,
                name_en=entry.get("name_en", tid),
                name_zh=entry.get("name_zh", entry.get("name_en", tid)),
                group=group,
                patterns=list(entry.get("patterns", [])),
            )
        )
    return topics


def event_text(event: Event, items: dict[str, Item]) -> str:
    parts = []
    story = event.story or {}
    parts += [story.get("title_en", ""), story.get("summary_en", "")]
    for item_id in event.item_ids[:8]:
        item = items.get(item_id)
        if item:
            parts.append(item.title)
            if item_id == event.rep:
                parts.append(item.snippet[:800])
    return " ".join(p for p in parts if p)


def topics_for(event: Event, items: dict[str, Item], topics: list[Topic]) -> list[Topic]:
    text = event_text(event, items)
    return [t for t in topics if t.matches(text)]


def effective_score(event: Event) -> int:
    return int(event.score or 0) + int((event.adjust or {}).get("points", 0))


def apply_adjustments(events: dict[str, Event], items: dict[str, Item], settings: Settings, topics: list[Topic]) -> int:
    """Set boost points on live events from the [adjust] rules. Returns how many events got points."""
    rules = settings.raw.get("adjust", {}).get("boost", [])
    if not rules:
        return 0
    by_id = {t.id: t for t in topics}
    cap = int(settings.raw.get("adjust", {}).get("max_points", 20))
    touched = 0
    for event in events.values():
        if not event.is_live:
            continue
        text = event_text(event, items)
        points, why = 0, []
        for rule in rules:
            pts = int(rule.get("points", 0))
            if "topic" in rule:
                topic = by_id.get(rule["topic"])
                if topic and topic.matches(text):
                    points += pts
                    why.append(f"topic {topic.id}")
            elif "pattern" in rule and re.search(rule["pattern"], text, re.IGNORECASE):
                points += pts
                why.append(rule.get("label") or rule["pattern"])
        points = max(-cap, min(cap, points))
        event.adjust = {"points": points, "why": why} if points else None
        touched += 1 if points else 0
    return touched


def muted(settings: Settings, text: str) -> bool:
    patterns = settings.raw.get("adjust", {}).get("mute", [])
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def validate_rules(settings: Settings, topics: list[Topic]) -> None:
    ids = {t.id for t in topics}
    adjust = settings.raw.get("adjust", {})
    for rule in adjust.get("boost", []):
        if "topic" in rule and rule["topic"] not in ids:
            raise ValueError(f"[adjust] boost refers to unknown topic {rule['topic']!r}")
        if "pattern" in rule:
            re.compile(rule["pattern"])
    for pattern in adjust.get("mute", []):
        re.compile(pattern)
