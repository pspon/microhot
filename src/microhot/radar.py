"""What was not picked: the Today page's radar and the unpicked stories offered for rating in the
morning issue. Plain rules, no model calls."""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .model import Event, Item
from .select import event_tier, threshold_for_event
from .textutil import parse_iso, to_iso
from .topics import Topic, effective_score, topics_for
from .views import report_source, temperature


def found_span(event: Event, items: dict[str, Item]) -> tuple[datetime | None, datetime | None]:
    """When MicroHOT first and last fetched a report on this story. Publish times stand in once the
    story's items have aged out."""
    times = [parse_iso(items[i].fetched) for i in event.item_ids if i in items]
    times = [t for t in times if t]
    if times:
        return min(times), max(times)
    first = parse_iso(event.created)
    return first, parse_iso(event.last_seen) or first


def _unpicked(events: dict[str, Event], items: dict[str, Item]) -> list[Event]:
    return [
        e
        for e in events.values()
        if e.is_live and not e.selected_at and e.type != "off_topic" and e.rep in items
    ]


def radar_by_beat(events: dict[str, Event], items: dict[str, Item], settings: Settings, now: datetime) -> dict[str, list[Event]]:
    """Unpicked stories with a report found in the last [radar] hours, warmest first, up to
    [radar] per_beat in each beat (beats in site order; empty beats left out)."""
    since = now - timedelta(hours=float(settings.radar["hours"]))
    per_beat = int(settings.radar["per_beat"])
    recent = [e for e in _unpicked(events, items) if (found_span(e, items)[1] or now) >= since]
    out: dict[str, list[Event]] = {}
    for beat in settings.beat_order:
        rows = [e for e in recent if e.beat == beat]
        rows.sort(key=lambda e: (-e.heat, -(e.score if e.score is not None else -1), e.id))
        if rows and per_beat > 0:
            out[beat] = rows[:per_beat]
    return out


def unpicked_row(event: Event, items: dict[str, Item], settings: Settings, topics: list[Topic]) -> dict:
    """A snapshot of an unpicked story, as stored in the edition and read back with your ratings."""
    rep = items.get(event.rep)
    first, _ = found_span(event, items)
    adjust = event.adjust or {}
    return {
        "id": event.id,
        "title": rep.title if rep else event.id,
        "url": rep.url if rep else "",
        "source": report_source(settings, rep) if rep else "",
        "score": effective_score(event),
        "raw": event.score,
        "boost": adjust.get("points", 0),
        "boost_why": list(adjust.get("why", [])),
        "threshold": threshold_for_event(event, items, settings),
        "type": event.type,
        "beat": event.beat,
        "tier": event_tier(event, items, settings),
        "axes": event.axes or {},
        "temp": temperature(event.heat),
        "topics": [t.id for t in topics_for(event, items, topics)],
        "found": to_iso(first) if first else None,
    }


def issue_unpicked(
    events: dict[str, Event],
    items: dict[str, Item],
    settings: Settings,
    since: datetime,
    topics: list[Topic],
) -> list[dict]:
    """Scored stories first found after `since` (the previous edition) and not picked, for 👍/👎 in the
    morning issue. Per beat: half the closest to the bar (those that cleared it but lost to the daily
    quota come first), then the warmest, up to [feedback] unpicked_per_beat. Each story gets one issue."""
    per_beat = int(settings.feedback.get("unpicked_per_beat", 4))
    if per_beat <= 0:
        return []
    pool = [e for e in _unpicked(events, items) if e.score is not None and (found_span(e, items)[0] or since) > since]
    rows: list[dict] = []
    for beat in settings.beat_order:
        mine = [e for e in pool if e.beat == beat]
        if not mine:
            continue
        near = sorted(mine, key=lambda e: (threshold_for_event(e, items, settings) - effective_score(e), -e.heat, e.id))
        warm = sorted(mine, key=lambda e: (-e.heat, -effective_score(e), e.id))
        chosen: list[Event] = []
        for event in near[: (per_beat + 1) // 2] + warm + near:
            if len(chosen) >= per_beat:
                break
            if event not in chosen:
                chosen.append(event)
        rows += [unpicked_row(e, items, settings, topics) for e in chosen]
    return rows
