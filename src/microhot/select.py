"""Pick stories: score threshold by best source tier (a beat may set its own), one slot per event,
daily quota per beat."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .config import TIER_RANK, Settings
from .model import Event, Item
from .textutil import hours_between, parse_iso, to_iso
from .topics import effective_score

_RANK_TO_TIER = {rank: tier for tier, rank in TIER_RANK.items()}


def event_tier(event: Event, items: dict[str, Item], settings: Settings) -> str:
    ranks = []
    for item_id in event.item_ids:
        item = items.get(item_id)
        src = settings.source(item.source_id) if item else None
        if src:
            ranks.append(TIER_RANK.get(src.tier, 2))
    return _RANK_TO_TIER[min(ranks)] if ranks else "T2"


def threshold_for_event(event: Event, items: dict[str, Item], settings: Settings) -> int:
    return settings.threshold_for(event_tier(event, items, settings), event.beat)


def is_candidate(event: Event, settings: Settings, now: datetime) -> bool:
    created = parse_iso(event.created)
    return bool(created) and hours_between(now, created) <= float(settings.selection["candidate_hours"])


def select_events(events: dict[str, Event], items: dict[str, Item], settings: Settings, now: datetime) -> list[Event]:
    tz = settings.tz
    today = now.astimezone(tz).date()
    taken: Counter = Counter()
    for event in events.values():
        if event.is_live and event.selected_at:
            selected = parse_iso(event.selected_at)
            if selected and selected.astimezone(tz).date() == today:
                taken[event.beat] += 1

    candidates = [
        e
        for e in events.values()
        if e.is_live
        and e.selected_at is None
        and e.score is not None
        and e.type != "off_topic"
        and is_candidate(e, settings, now)
        and effective_score(e) >= threshold_for_event(e, items, settings)
    ]
    candidates.sort(key=lambda e: (-effective_score(e), -e.heat, e.created))

    chosen: list[Event] = []
    for event in candidates:
        quota = int(settings.beats.get(event.beat, {}).get("daily_quota", 5))
        if taken[event.beat] >= quota:
            continue
        event.selected_at = to_iso(now)
        taken[event.beat] += 1
        chosen.append(event)
    return chosen


def near_misses(events: dict[str, Event], items: dict[str, Item], settings: Settings, now: datetime, limit: int = 12) -> list[dict]:
    """Unselected scored events closest to their threshold (for calibration on the About page)."""
    rows = []
    for e in events.values():
        if not e.is_live or e.selected_at or e.score is None or e.type == "off_topic":
            continue
        if not is_candidate(e, settings, now):
            continue
        threshold = threshold_for_event(e, items, settings)
        rows.append((threshold - effective_score(e), e, threshold))
    rows.sort(key=lambda r: (r[0], -r[1].heat))
    out = []
    for gap, event, threshold in rows[:limit]:
        rep = items.get(event.rep)
        out.append(
            {
                "id": event.id,
                "title": rep.title if rep else event.id,
                "url": rep.url if rep else "",
                "score": effective_score(event),
                "boost": (event.adjust or {}).get("points", 0),
                "threshold": threshold,
                "gap": gap,
                "type": event.type,
                "beat": event.beat,
                "tier": event_tier(event, items, settings),
                "axes": event.axes or {},
            }
        )
    return out
