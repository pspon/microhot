"""The story log: one row for every story MicroHOT has seen, kept for good in the private data repo.

  log/YYYY-MM.jsonl   one row per story, filed by the local month MicroHOT first found it

A row is refreshed while its story is in the rolling events window (30 days by default) and only ever
gains information: reports that age out of the item store keep their links, the first-found time and
the peak temperature never go back. Once the story leaves the window, its row is its final record:
title, links to every report, beat, type, score against the bar of the time, peak temperature, whether
and when it was picked, the editions that carried it, and its topics. Merged stories keep their row,
marked with `merged_into`. Plain rules, no model calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .model import Event, Item
from .radar import found_span
from .select import event_tier
from .store import Store, read_jsonl
from .textutil import parse_iso, to_iso, truncate
from .topics import Topic, topics_for
from .views import report_source, temperature

LOG_DIR = "log"
SUMMARY_CHARS = 280


def _month(iso: str | None, settings: Settings) -> str | None:
    when = parse_iso(iso)
    return when.astimezone(settings.tz).strftime("%Y-%m") if when else None


def _months_from(start: date, end: date) -> list[str]:
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def story_row(event: Event, items: dict[str, Item], settings: Settings, topics: list[Topic]) -> dict | None:
    """The log row for a story from its loaded reports, or None when none are loaded."""
    present = [items[i] for i in event.item_ids if i in items]
    if not present:
        return None
    rep = items.get(event.rep) or present[0]
    first, last = found_span(event, items)
    story = event.story or {}
    adjust = event.adjust or {}
    links, seen = [], set()
    for item in sorted(present, key=lambda it: (it.when(), it.id)):
        if item.url not in seen:
            seen.add(item.url)
            links.append({"source": report_source(settings, item), "url": item.url})
    tier = event_tier(event, items, settings)
    return {
        "id": event.id,
        "found": to_iso(first) if first else event.created,
        "last_found": to_iso(last) if last else event.last_seen,
        "published": event.created,
        "beat": event.beat,
        "type": event.type,
        "score": event.score,
        "boost": adjust.get("points", 0),
        "boost_why": list(adjust.get("why", [])),
        "threshold": settings.threshold_for(tier, event.beat),
        "tier": tier,
        "axes": event.axes or {},
        "title": story.get("title_en") or rep.title,
        "title_zh": story.get("title_zh") or "",
        "summary": story.get("summary_en") or truncate(rep.snippet, SUMMARY_CHARS),
        "url": rep.url,
        "source": report_source(settings, rep),
        "links": links,
        "n_sources": max(event.n_sources, len({link["source"] for link in links})),
        "temp_max": temperature(event.heat),
        "picked": event.selected_at,
        "editions": list(event.editions),
        "topics": [t.id for t in topics_for(event, items, topics)],
        "merged_into": None,
        "_rep_loaded": event.rep in items,
        "_written": bool(story),
    }


def _merge(old: dict | None, new: dict) -> dict:
    """Refresh a row without losing what it already knew."""
    rep_loaded = new.pop("_rep_loaded")
    written = new.pop("_written")
    if not old:
        return new
    out = dict(new)
    out["found"] = min(filter(None, [old.get("found"), new["found"]]))
    out["last_found"] = max(filter(None, [old.get("last_found"), new["last_found"]]))
    known = {link["url"] for link in old.get("links", [])}
    out["links"] = list(old.get("links", [])) + [link for link in new["links"] if link["url"] not in known]
    out["n_sources"] = max(int(old.get("n_sources") or 0), new["n_sources"])
    out["temp_max"] = max(float(old.get("temp_max") or 0), new["temp_max"])
    out["editions"] = list(old.get("editions", [])) + [d for d in new["editions"] if d not in old.get("editions", [])]
    if not rep_loaded:  # the representative report aged out: keep what the row said about it
        keys = ("url", "source") if written else ("url", "source", "title", "summary")
        for key in keys:
            out[key] = old.get(key) or new[key]
    return out


def _path(store: Store, month: str) -> Path:
    return store.dir / LOG_DIR / f"{month}.jsonl"


def update_log(
    store: Store,
    events: dict[str, Event],
    items: dict[str, Item],
    settings: Settings,
    topics: list[Topic],
    now: datetime,
) -> dict:
    """Add or refresh the rows of the stories in `events`; rewrite only the month files that changed."""
    files: dict[str, dict[str, dict]] = {}
    where: dict[str, str] = {}

    def month_rows(month: str) -> dict[str, dict]:
        if month not in files:
            files[month] = {r["id"]: r for r in read_jsonl(_path(store, month))}
            for row_id in files[month]:
                where.setdefault(row_id, month)
        return files[month]

    # Every month a story still in the window can be filed under, so moves and merges are found.
    keep = max(int(settings.retention["events_days"]), int(settings.retention["items_days"])) + 3
    for month in _months_from((now - timedelta(days=keep)).astimezone(settings.tz).date(), now.astimezone(settings.tz).date()):
        month_rows(month)

    dirty: set[str] = set()
    added = updated = 0
    for event in events.values():
        if event.merged_into:
            month = where.get(event.id)
            if month and files[month][event.id].get("merged_into") != event.merged_into:
                files[month][event.id]["merged_into"] = event.merged_into
                dirty.add(month)
                updated += 1
            continue
        new = story_row(event, items, settings, topics)
        if new is None:
            continue
        old_month = where.get(event.id)
        old = files[old_month][event.id] if old_month else None
        row = _merge(old, new)
        month = _month(row["found"], settings)
        if month is None or (old == row and old_month == month):
            continue
        if old_month and old_month != month:  # an earlier report joined the story (a merge)
            del files[old_month][event.id]
            dirty.add(old_month)
        month_rows(month)[event.id] = row
        where[event.id] = month
        dirty.add(month)
        if old:
            updated += 1
        else:
            added += 1

    for month in sorted(dirty):
        rows = sorted(files[month].values(), key=lambda r: (r.get("found") or "", r["id"]))
        store.write_jsonl(_path(store, month), rows)
    return {"added": added, "updated": updated, "files": len(dirty)}


def load_log(store: Store, settings: Settings, since: datetime, now: datetime) -> list[dict]:
    """Rows of stories first found on or after `since`, newest first; merged stories left out."""
    rows = []
    for month in _months_from(since.astimezone(settings.tz).date(), now.astimezone(settings.tz).date()):
        for row in read_jsonl(_path(store, month)):
            found = parse_iso(row.get("found"))
            if row.get("merged_into") or not found or found < since:
                continue
            rows.append(row)
    rows.sort(key=lambda r: (r.get("found") or "", r["id"]), reverse=True)
    return rows
