"""The daily edition: compiled by rule from the stories picked since the last edition (0 tokens)."""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .model import Event, Item
from .textutil import parse_iso, to_iso
from .views import story_view


def edition_due(settings: Settings, state: dict, now: datetime) -> str | None:
    """Return today's local date if the edition should be built now, else None."""
    local = now.astimezone(settings.tz)
    if local.time() < settings.edition_after:
        return None
    date = local.date().isoformat()
    if state.get("editions", {}).get("last_date") == date:
        return None
    return date


def build_edition(
    date: str,
    events: dict[str, Event],
    items: dict[str, Item],
    settings: Settings,
    state: dict,
    now: datetime,
) -> dict:
    last_built = parse_iso(state.get("editions", {}).get("last_built_at"))
    since = now - timedelta(hours=24)
    if last_built:
        since = max(last_built, now - timedelta(hours=36))
    order = {beat: i for i, beat in enumerate(settings.beat_order)}
    picked = [
        e for e in events.values() if e.is_live and e.selected_at and (parse_iso(e.selected_at) or now) > since
    ]
    picked.sort(key=lambda e: (order.get(e.beat, 99), -e.heat, -(e.score or 0), e.id))
    edition = {
        "date": date,
        "built_at": to_iso(now),
        "since": to_iso(since),
        "stories": [story_view(e, items, settings) for e in picked],
        "delivered": {},
    }
    eds = state.setdefault("editions", {})
    eds["last_date"] = date
    eds["last_built_at"] = to_iso(now)
    return edition
