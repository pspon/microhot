"""Plain dict views of stories, shared by the site, the editions and the channels."""

from __future__ import annotations

from datetime import datetime

from .config import Settings
from .model import Event, Item
from .select import event_tier
from .textutil import hours_between, parse_iso, to_iso, truncate
from .topics import Topic, effective_score, topics_for

BASE_TEMP = 36.5  # °C: a story nobody else is covering
TEMP_PER_HEAT = 0.6  # each fresh independent source adds this much
MAX_TEMP = 41.0
NEW_HOURS = 6  # "new": first report under 6 hours old
COMPARE_MAX_HOURS = 13  # only compare with a previous run this recent (runs are 6 or 12 hours apart)


def temperature(heat: float) -> float:
    """MicroHOT shows heat as a body temperature: 微熱 means a slight fever."""
    return round(min(MAX_TEMP, BASE_TEMP + TEMP_PER_HEAT * max(0.0, heat)), 1)


def temp_class(temp: float) -> str:
    if temp >= 38.5:
        return "hot"
    if temp >= 37.5:
        return "warm"
    return "cool"


def source_name(settings: Settings, source_id: str) -> str:
    src = settings.source(source_id)
    return src.name if src else source_id


def _comparable_prev(event: Event, now: datetime) -> float | None:
    prev_at = parse_iso(event.heat_prev_at)
    if event.heat_prev is None or not prev_at or hours_between(now, prev_at) > COMPARE_MAX_HOURS:
        return None
    return event.heat_prev


def badges(event: Event, now: datetime | None) -> list[str]:
    """AIHOT's hot-list markers, adapted: spiking, new, warming."""
    if now is None:
        return []
    out = []
    spiking = event.sources_6h >= 3 and event.sources_48h > 0 and event.sources_6h / event.sources_48h >= 0.5
    if spiking:
        out.append("spiking")
    created = parse_iso(event.created)
    if created and hours_between(now, created) < NEW_HOURS:
        out.append("new")
    prev = _comparable_prev(event, now)
    if not spiking and prev and prev > 0 and (event.heat - prev) / prev > 0.15:
        out.append("warming")
    return out


def temp_change(event: Event, now: datetime | None) -> float | None:
    prev = None if now is None else _comparable_prev(event, now)
    if prev is None:
        return None
    change = round(temperature(event.heat) - temperature(prev), 1)
    return change if abs(change) >= 0.1 else 0.0


def _links(event: Event, items: dict[str, Item], settings: Settings) -> list[dict]:
    links = []
    for item_id in event.item_ids:
        item = items.get(item_id)
        if item:
            src = settings.source(item.source_id)
            links.append(
                {
                    "id": item.id,
                    "source": source_name(settings, item.source_id),
                    "official": bool(src and src.tier == "T1"),
                    "title": item.title,
                    "url": item.url,
                    "when": to_iso(item.when()),
                }
            )
    links.sort(key=lambda link: link["when"])
    return links


def story_view(
    event: Event,
    items: dict[str, Item],
    settings: Settings,
    now: datetime | None = None,
    topics: list[Topic] | None = None,
) -> dict:
    rep = items.get(event.rep)
    story = event.story or {}
    digest = event.digest or {}
    links = _links(event, items, settings)
    written_ids = set(digest.get("item_ids") or story.get("item_ids") or [])  # reports the visible summary covers
    updates = [link for link in links if written_ids and link["id"] not in written_ids]
    title_en = digest.get("title_en") or story.get("title_en") or (rep.title if rep else event.id)
    summary_en = story.get("summary_en") or (truncate(rep.snippet, 280) if rep and rep.snippet else "")
    summary_zh = story.get("summary_zh") or ""
    if digest:  # a multi-source overview replaces the first report's summary on cards
        summary_en = digest.get("text_en", "").split("\n\n")[0] or summary_en
        summary_zh = digest.get("text_zh", "").split("\n\n")[0] or summary_zh
    temp = temperature(event.heat)
    tier = event_tier(event, items, settings)
    matched = topics_for(event, items, topics) if topics else []
    return {
        "id": event.id,
        "beat": event.beat,
        "type": event.type,
        "score": event.score,
        "boost": (event.adjust or {}).get("points", 0),
        "boost_why": (event.adjust or {}).get("why", []),
        "effective_score": effective_score(event) if event.score is not None else None,
        "axes": event.axes or {},
        "opinions": event.opinions,
        "heat": event.heat,
        "temp": temp,
        "temp_class": temp_class(temp),
        "temp_change": temp_change(event, now),
        "badges": badges(event, now),
        "n_sources": event.n_sources or len({link["source"] for link in links}),
        "sources_48h": event.sources_48h,
        "sources_6h": event.sources_6h,
        "selected_at": event.selected_at,
        "first_seen": event.created,
        "last_seen": event.last_seen,
        "tier": tier,
        "official": tier == "T1",
        "written": bool(story),
        "by": story.get("by"),
        "title": {"en": title_en, "zh-TW": digest.get("title_zh") or story.get("title_zh") or title_en},
        "summary": {"en": summary_en, "zh-TW": summary_zh},
        "digest": (
            {"en": digest.get("text_en", ""), "zh-TW": digest.get("text_zh", ""), "by": digest.get("by"), "at": digest.get("at")}
            if digest
            else None
        ),
        "updates": updates,
        "editions": list(event.editions),
        "topics": [{"id": t.id, "en": t.name_en, "zh-TW": t.name_zh} for t in matched],
        "url": rep.url if rep else (links[0]["url"] if links else ""),
        "source": source_name(settings, rep.source_id) if rep else "",
        "links": links,
    }
