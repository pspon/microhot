"""Overviews for big stories: picked stories with several independent sources.

Only stories with at least `min_sources` sources get one, at most `max_per_day` a day, hottest first.
An overview is rewritten when the story gains `regrow_sources` more sources. Each is one model call
(the "digest" chain) over up to `reports` reports, one per source, official sources first.
"""

from __future__ import annotations

from datetime import datetime

from .config import TIER_RANK, Settings
from .llm import Router
from .model import Event, Item
from .prompts import load_prompt
from .textutil import cjk_ratio, has_cjk, parse_iso, to_iso, truncate
from .zh import to_taiwan

FIELDS = ("title_en", "digest_en", "title_zh", "digest_zh")
MAX_TRIES = 3
SCHEMA = {
    "type": "object",
    "properties": {f: {"type": "string"} for f in FIELDS},
    "required": list(FIELDS),
    "additionalProperties": False,
}


def _validate(obj: dict) -> None:
    values = {f: str(obj.get(f) or "").strip() for f in FIELDS}
    empty = [f for f, v in values.items() if not v]
    if empty:
        raise ValueError(f"empty: {', '.join(empty)}")
    if not (has_cjk(values["title_zh"]) and has_cjk(values["digest_zh"])):
        raise ValueError("title_zh and digest_zh must be in Traditional Chinese")
    if cjk_ratio(values["title_en"] + values["digest_en"]) > 0.2:
        raise ValueError("title_en and digest_en must be in English")


def _reports(event: Event, items: dict[str, Item], settings: Settings, limit: int) -> list[Item]:
    """One report per source: official sources first, then the most recent."""
    best: dict[str, Item] = {}
    for item_id in event.item_ids:
        item = items.get(item_id)
        if not item:
            continue
        current = best.get(item.source_id)
        if current is None or len(item.snippet) > len(current.snippet):
            best[item.source_id] = item

    def key(item: Item):
        src = settings.source(item.source_id)
        return (TIER_RANK.get(src.tier if src else "T2", 2), -item.when().timestamp())

    return sorted(best.values(), key=key)[:limit]


def build_message(event: Event, items: dict[str, Item], settings: Settings) -> str:
    cfg = settings.raw["digest"]
    reports = _reports(event, items, settings, int(cfg["reports"]))
    lines = [f"Reports on one story ({len(reports)}), official sources first:", ""]
    for i, item in enumerate(reports, 1):
        src = settings.source(item.source_id)
        date = item.published[:10] if item.published else "unknown"
        lines.append(f"[{i}] {src.name if src else item.source_id} | {date}")
        lines.append(f"Title: {item.title}")
        if item.snippet:
            lines.append(f"Text: {truncate(item.snippet, int(cfg['report_chars']))}")
        lines.append("")
    return "\n".join(lines).strip()


def candidates(events: dict[str, Event], settings: Settings, now: datetime) -> list[Event]:
    cfg = settings.raw["digest"]
    out = []
    for event in events.values():
        if not (event.is_live and event.selected_at and event.story) or event.digest_tries >= MAX_TRIES:
            continue
        if event.n_sources < int(cfg["min_sources"]):
            continue
        if event.digest and event.n_sources < int(event.digest.get("n_sources", 0)) + int(cfg["regrow_sources"]):
            continue
        out.append(event)
    out.sort(key=lambda e: (-e.heat, e.id))
    return out


def digests_today(events: dict[str, Event], settings: Settings, now: datetime) -> int:
    today = now.astimezone(settings.tz).date()
    count = 0
    for event in events.values():
        at = parse_iso((event.digest or {}).get("at"))
        if at and at.astimezone(settings.tz).date() == today:
            count += 1
    return count


def digest_pending(events: dict[str, Event], items: dict[str, Item], router: Router, settings: Settings, now: datetime) -> dict:
    cfg = settings.raw["digest"]
    report = {"candidates": 0, "calls": 0, "written": 0, "failed": 0}
    if not cfg.get("enabled", True):
        return report
    pending = candidates(events, settings, now)
    report["candidates"] = len(pending)
    room = max(0, int(cfg["max_per_day"]) - digests_today(events, settings, now))
    system = load_prompt(settings.prompts_dir, "digest")
    for event in pending[:room]:
        if not router.available("digest"):
            break
        result = router.complete_json("digest", system, build_message(event, items, settings), SCHEMA, "digest", _validate, max_tokens=3000)
        report["calls"] += 1
        if result is None:
            event.digest_tries += 1
            report["failed"] += 1
            continue
        data = result.data
        event.digest = {
            "title_en": str(data["title_en"]).strip(),
            "text_en": str(data["digest_en"]).strip(),
            "title_zh": to_taiwan(str(data["title_zh"]).strip()),
            "text_zh": to_taiwan(str(data["digest_zh"]).strip()),
            "by": f"{result.provider}/{result.model}",
            "at": to_iso(now),
            "n_sources": event.n_sources,
            "item_ids": list(event.item_ids),
        }
        event.digest_tries = 0
        report["written"] += 1
    return report
