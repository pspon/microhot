"""The morning edition, compiled by rule (0 tokens).

It carries the stories picked since the previous edition, plus follow-ups: stories already covered
that gained real news since then (a new official source, or several new sources), as AIHOT does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import Settings
from .model import Event, Item
from .select import near_misses
from .textutil import has_cjk, parse_iso, to_iso
from .topics import Topic, effective_score
from .views import report_source, story_view

MAX_FOLLOWUPS = 5


def edition_due(settings: Settings, state: dict, now: datetime) -> str | None:
    """Return today's local date if the edition should be built now, else None."""
    local = now.astimezone(settings.tz)
    if local.time() < settings.edition_after:
        return None
    date = local.date().isoformat()
    if state.get("editions", {}).get("last_date") == date:
        return None
    return date


def followup_links(event: Event, items: dict[str, Item], settings: Settings, since: datetime) -> list[dict]:
    """Reports that arrived after `since` from sources new to the story, if they amount to news."""
    seen_before: set[str] = set()
    fresh: dict[str, list[Item]] = {}
    for item_id in event.item_ids:
        item = items.get(item_id)
        if not item:
            continue
        fetched = parse_iso(item.fetched) or since
        if fetched > since:
            fresh.setdefault(item.origin, []).append(item)
        else:
            seen_before.add(item.origin)
    new_sources = {origin: its for origin, its in fresh.items() if origin not in seen_before}
    if not new_sources:
        return []

    def is_official(item: Item) -> bool:
        src = settings.source(item.source_id)
        return bool(src and src.tier == "T1" and not item.publisher)

    official = any(is_official(its[0]) for its in new_sources.values())
    needed = int(settings.raw["editions"].get("followup_min_new_sources", 2))
    if not official and len(new_sources) < needed:
        return []
    links = []
    for its in new_sources.values():
        for item in its:
            links.append(
                {
                    "source": report_source(settings, item),
                    "official": is_official(item),
                    "title": item.title,
                    "url": item.url,
                    "when": to_iso(item.when()),
                }
            )
    links.sort(key=lambda link: link["when"])
    return links


def _stats(stories: list[dict], followups: list[dict]) -> dict:
    sources = {link["source"] for s in stories + followups for link in s["links"]}
    words_en = sum(len((s["title"]["en"] + " " + s["summary"]["en"]).split()) for s in stories + followups)
    chars_zh = sum(len(s["title"]["zh-TW"] + s["summary"]["zh-TW"]) for s in stories + followups if has_cjk(s["title"]["zh-TW"]))
    return {
        "stories": len(stories),
        "followups": len(followups),
        "sources": len(sources),
        "official": sum(1 for s in stories if s["official"]),
        "words_en": words_en,
        "chars_zh": chars_zh,
    }


def build_edition(
    date: str,
    events: dict[str, Event],
    items: dict[str, Item],
    settings: Settings,
    state: dict,
    now: datetime,
    topics: list[Topic] | None = None,
) -> dict:
    last_built = parse_iso(state.get("editions", {}).get("last_built_at"))
    since = now - timedelta(hours=24)
    if last_built:
        since = max(last_built, now - timedelta(hours=36))
    order = {beat: i for i, beat in enumerate(settings.beat_order)}

    live = [e for e in events.values() if e.is_live]
    picked = [e for e in live if e.selected_at and (parse_iso(e.selected_at) or now) > since]
    picked.sort(key=lambda e: (order.get(e.beat, 99), -e.heat, -effective_score(e), e.id))
    picked_ids = {e.id for e in picked}

    followups: list[tuple[Event, list[dict]]] = []
    for event in live:
        if event.id in picked_ids or not event.selected_at:
            continue
        covered_before = bool(event.editions) or (parse_iso(event.selected_at) or now) <= since
        if not covered_before:
            continue
        links = followup_links(event, items, settings, since)
        if links:
            followups.append((event, links))
    followups.sort(key=lambda pair: (-pair[0].heat, pair[0].id))
    followups = followups[:MAX_FOLLOWUPS]

    stories = [story_view(e, items, settings, now, topics) for e in picked]
    follow_views = []
    for event, links in followups:
        view = story_view(event, items, settings, now, topics)
        view["followup"] = {"previous": event.editions[-1] if event.editions else None, "new": links}
        follow_views.append(view)
    for event in picked + [e for e, _ in followups]:
        if date not in event.editions:
            event.editions.append(date)

    ranked = sorted(stories, key=lambda s: (-s["temp"], -(s["effective_score"] or 0), s["id"]))
    highlights_n = int(settings.raw["editions"].get("highlights", 3))
    edition = {
        "date": date,
        "built_at": to_iso(now),
        "since": to_iso(since),
        "lead": ranked[0]["id"] if ranked else None,
        "highlights": [s["id"] for s in ranked[1 : 1 + highlights_n]],
        "stories": stories,
        "followups": follow_views,
        "near_misses": near_misses(events, items, settings, now, limit=int(settings.raw["feedback"]["near_misses_in_issue"])),
        "stats": _stats(stories, follow_views),
        "delivered": {},
    }
    eds = state.setdefault("editions", {})
    eds["last_date"] = date
    eds["last_built_at"] = to_iso(now)
    return edition
