"""Plain dict views of stories, shared by the site, the editions and the channels."""

from __future__ import annotations

from .config import Settings
from .model import Event, Item
from .select import event_tier
from .textutil import to_iso, truncate

BASE_TEMP = 36.5  # °C: a story nobody else is covering
TEMP_PER_HEAT = 0.6  # each fresh independent source adds this much
MAX_TEMP = 41.0


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


def story_view(event: Event, items: dict[str, Item], settings: Settings) -> dict:
    rep = items.get(event.rep)
    story = event.story or {}
    links = []
    for item_id in event.item_ids:
        item = items.get(item_id)
        if item:
            links.append(
                {"source": source_name(settings, item.source_id), "title": item.title, "url": item.url, "when": to_iso(item.when())}
            )
    links.sort(key=lambda link: link["when"])
    title_en = story.get("title_en") or (rep.title if rep else event.id)
    summary_en = story.get("summary_en") or (truncate(rep.snippet, 280) if rep and rep.snippet else "")
    temp = temperature(event.heat)
    return {
        "id": event.id,
        "beat": event.beat,
        "type": event.type,
        "score": event.score,
        "axes": event.axes or {},
        "opinions": event.opinions,
        "heat": event.heat,
        "temp": temp,
        "temp_class": temp_class(temp),
        "n_sources": event.n_sources or len({link["source"] for link in links}),
        "selected_at": event.selected_at,
        "first_seen": event.created,
        "last_seen": event.last_seen,
        "tier": event_tier(event, items, settings),
        "written": bool(story),
        "by": story.get("by"),
        "title": {"en": title_en, "zh-TW": story.get("title_zh") or title_en},
        "summary": {"en": summary_en, "zh-TW": story.get("summary_zh") or ""},
        "url": rep.url if rep else (links[0]["url"] if links else ""),
        "source": source_name(settings, rep.source_id) if rep else "",
        "links": links,
    }
