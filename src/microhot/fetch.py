"""Fetch RSS/Atom feeds, apply rule filters and de-duplicate. Zero model tokens."""

from __future__ import annotations

import calendar
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import feedparser
import httpx

from .config import Settings, Source
from .model import Item
from .textutil import html_to_text, hours_between, item_id_for, title_fingerprint, to_iso, truncate

SNIPPET_CHARS = 1600


@dataclass
class FetchStat:
    source_id: str
    status: str = "ok"  # ok | not_modified | empty | error
    http: int | None = None
    entries: int = 0
    new: int = 0
    old: int = 0
    filtered: int = 0
    duplicate: int = 0
    error: str | None = None
    ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def _entry_text(entry) -> str:
    candidates = [c.get("value", "") for c in (entry.get("content") or []) if isinstance(c, dict)]
    candidates += [entry.get("summary", ""), entry.get("description", "")]
    texts = [html_to_text(c) for c in candidates if c]
    return max(texts, key=len) if texts else ""


def _entry_link(entry, base: str) -> str | None:
    link = entry.get("link")
    if not link:
        for candidate in entry.get("links") or []:
            if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                link = candidate["href"]
                break
    if not link:
        guid = str(entry.get("id") or "")
        link = guid if guid.startswith("http") else None
    if not link:
        return None
    link = urljoin(base, link.strip())
    return link if link.startswith(("http://", "https://")) else None


class _Seen:
    """Thread-safe record of item ids and (source, title fingerprint) pairs."""

    def __init__(self, items: dict[str, Item]) -> None:
        self.ids = set(items)
        self.fps = {(it.source_id, it.fp) for it in items.values() if it.fp}
        self.lock = threading.Lock()

    def claim(self, item_id: str, source_id: str, fp: str) -> bool:
        with self.lock:
            if item_id in self.ids or (source_id, fp) in self.fps:
                return False
            self.ids.add(item_id)
            self.fps.add((source_id, fp))
            return True


def fetch_source(
    client: httpx.Client,
    source: Source,
    feed_state: dict,
    settings: Settings,
    now: datetime,
    seen: _Seen,
) -> tuple[list[Item], FetchStat, dict]:
    cfg = settings.fetch
    stat = FetchStat(source.id)
    state = dict(feed_state)
    started = time.monotonic()
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    }
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]

    def finish(status: str, error: str | None = None):
        stat.status = status
        stat.error = error
        stat.ms = int((time.monotonic() - started) * 1000)
        state["last_status"] = status
        state["last_checked"] = to_iso(now)
        if status == "error":
            state["last_error"] = error
            state["errors_in_a_row"] = int(state.get("errors_in_a_row", 0)) + 1
        else:
            state["errors_in_a_row"] = 0
            state["last_ok"] = to_iso(now)

    try:
        resp = client.get(source.url, headers=headers, timeout=cfg["timeout_s"], follow_redirects=True)
    except httpx.HTTPError as exc:
        finish("error", f"{type(exc).__name__}: {exc}"[:200])
        return [], stat, state

    stat.http = resp.status_code
    if resp.status_code == 304:
        finish("not_modified")
        return [], stat, state
    if resp.status_code >= 400:
        finish("error", f"HTTP {resp.status_code}")
        return [], stat, state

    parsed = feedparser.parse(resp.content, response_headers={"content-type": resp.headers.get("content-type", "")})
    entries = list(parsed.entries or [])
    stat.entries = len(entries)
    if not entries and parsed.get("bozo"):
        finish("error", f"not a feed: {parsed.get('bozo_exception')}"[:200])
        return [], stat, state
    if not entries:
        finish("empty", "the feed has no entries")
        return [], stat, state
    # Some feeds list oldest first; look at the newest entries (undated ones keep feed order, last).
    entries.sort(key=lambda e: (_entry_time(e) is None, -(_entry_time(e) or now).timestamp()))

    first_import = not state.get("first_ok")
    undated_budget = int(cfg["first_import_undated_max"])
    age_limit = float(cfg["first_import_hours"] if first_import else cfg["max_age_hours"])
    items: list[Item] = []
    for entry in entries[: int(cfg["max_items_per_feed"])]:
        link = _entry_link(entry, source.url)
        if not link:
            continue
        title = html_to_text(entry.get("title")) or "(untitled)"
        text = _entry_text(entry)
        published = _entry_time(entry)
        if published and published > now + timedelta(hours=2):
            published = now
        if published:
            if hours_between(now, published) > age_limit:
                stat.old += 1
                continue
        elif first_import:
            if undated_budget <= 0:
                stat.old += 1
                continue
            undated_budget -= 1
        if not source.matches(f"{title} {text}"):
            stat.filtered += 1
            continue
        item_id = item_id_for(link)
        fp = title_fingerprint(title)
        if not seen.claim(item_id, source.id, fp):
            stat.duplicate += 1
            continue
        items.append(
            Item(
                id=item_id,
                source_id=source.id,
                url=link,
                title=truncate(title, 300),
                snippet=truncate(text, SNIPPET_CHARS),
                fetched=to_iso(now),
                published=to_iso(published) if published else None,
                lang=source.lang,
                fp=fp,
            )
        )

    stat.new = len(items)
    if resp.headers.get("etag"):
        state["etag"] = resp.headers["etag"]
    if resp.headers.get("last-modified"):
        state["last_modified"] = resp.headers["last-modified"]
    if first_import:
        state["first_ok"] = to_iso(now)
    state["items_total"] = int(state.get("items_total", 0)) + stat.new
    finish("ok")
    return items, stat, state


def fetch_all(
    settings: Settings,
    feed_states: dict,
    known_items: dict[str, Item],
    now: datetime,
    client: httpx.Client | None = None,
) -> tuple[list[Item], list[FetchStat]]:
    """Fetch every enabled source in parallel. Updates `feed_states` in place."""
    sources = [s for s in settings.sources if s.enabled]
    seen = _Seen(known_items)
    own_client = client is None
    client = client or httpx.Client(http2=False)
    results: list[tuple[list[Item], FetchStat, dict]] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, int(settings.fetch["workers"]))) as pool:
            futures = [
                pool.submit(fetch_source, client, src, feed_states.get(src.id, {}), settings, now, seen)
                for src in sources
            ]
            results = [f.result() for f in futures]
    finally:
        if own_client:
            client.close()

    new_items: list[Item] = []
    stats: list[FetchStat] = []
    for src, (items, stat, state) in zip(sources, results):
        feed_states[src.id] = state
        new_items.extend(items)
        stats.append(stat)
    new_items.sort(key=lambda it: (it.when(), it.id))
    return new_items, stats
