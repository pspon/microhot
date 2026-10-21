"""Fetch sources, apply rule filters and de-duplicate. Zero model tokens.

Three kinds of source (`kind` in config/sources.toml):
- "feed": an RSS or Atom feed (the default).
- "search": a news-search feed. Its items come from many outlets, so each item keeps its publisher,
  which counts as its own independent source for heat; redirect links are unwrapped.
- "page": an HTML page without a feed, such as a newsroom list. Links matching `link_pattern` are
  news items. The first read only records the links already there; later reads report new ones.
  With `fetch_text`, each new item's own page is opened for its description (plain HTTP, 0 tokens).
Feed items that arrive with little or no text (some feeds carry only titles) get the same treatment,
so triage scores them on more than the headline; `fetch_text = true` on a feed does it for every item.
"""

from __future__ import annotations

import calendar
import contextlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import feedparser
import httpx

from .config import Settings, Source
from .model import Item
from .textutil import (
    collapse_ws,
    hours_between,
    html_to_text,
    item_id_for,
    normalize_title,
    parse_iso,
    short_hash,
    title_fingerprint,
    to_iso,
    truncate,
)
from .topics import muted

SNIPPET_CHARS = 1600
PAGE_SEEN_MAX = 1000  # links remembered per page source
PAGE_TEXT_MAX = 8  # new items per page source and run whose own page is opened for text
PAGE_TITLE_MIN = 12  # shorter link texts ("Read more") are not usable headlines on their own
PAGE_FLOOD = 15  # this many new links at once means the page changed shape: record them, report none
PAGE_TEXT_PAUSE_S = 0.5  # between opening item pages on the same site
FEED_TEXT_MIN = 80  # feed items with less text than this have their own page opened for more
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


@dataclass
class FetchStat:
    source_id: str
    status: str = "ok"  # ok | not_modified | empty | error | baseline
    http: int | None = None
    entries: int = 0
    new: int = 0
    old: int = 0
    filtered: int = 0
    muted: int = 0
    duplicate: int = 0
    error: str | None = None
    ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---- feeds ------------------------------------------------------------------------------


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


# ---- news-search feeds ------------------------------------------------------------------

# Search engines and alert services wrap each result in a redirect; the real article is a parameter.
_REDIRECTS = (
    ("bing.com", "/news/apiclick.aspx", ("url",)),
    ("google.com", "/url", ("url", "q")),
)


def unwrap_link(link: str) -> str:
    parts = urlsplit(link)
    host = parts.netloc.lower().removeprefix("www.")
    for domain, path, params in _REDIRECTS:
        if (host == domain or host.endswith("." + domain)) and parts.path.startswith(path):
            query = dict(parse_qsl(parts.query))
            for param in params:
                target = query.get(param, "")
                if target.startswith(("http://", "https://")):
                    return target
    return link


def _domain(link: str) -> str:
    return urlsplit(link).netloc.lower().removeprefix("www.")


def entry_publisher(entry, link: str) -> str:
    """The outlet behind a search result: the feed's <source>, Bing's <News:Source>, else the domain."""
    name = ""
    src = entry.get("source")
    if isinstance(src, dict):
        name = str(src.get("title") or "")
    name = name or str(entry.get("news_source") or "")
    name = collapse_ws(html_to_text(name))
    name = re.sub(r"\s+on MSN$", "", name)  # "Global News on MSN" is Global News's story
    return (name or _domain(link))[:80]


def _strip_publisher(title: str, publisher: str) -> str:
    for sep in (" - ", " | ", " – ", " — "):
        suffix = f"{sep}{publisher}"
        if publisher and title.endswith(suffix) and len(title) > len(suffix) + 10:
            return title[: -len(suffix)].rstrip()
    return title


# ---- pages ------------------------------------------------------------------------------

_SKIP = {"script", "style", "noscript", "svg", "template"}


class _LinkExtractor(HTMLParser):
    """Every <a href> on a page with its visible text (or its title/aria-label)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.base: str | None = None
        self._href: str | None = None
        self._label = ""
        self._text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self._skip += 1
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "a":
            if self._href is not None:
                self._flush()
            if a.get("href"):
                self._href = a["href"]
                self._label = a.get("title") or a.get("aria-label") or ""
                self._text = []

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self._flush()

    def handle_data(self, data):
        if self._href is not None and not self._skip:
            self._text.append(data)

    def _flush(self) -> None:
        text = collapse_ws(" ".join(self._text)) or collapse_ws(self._label)
        self.links.append((self._href or "", text))
        self._href = None


def page_links(html: str, page_url: str, pattern: str) -> list[tuple[str, str]]:
    """(url, title) for links matching `pattern`, in page order, one per URL (its longest text)."""
    parser = _LinkExtractor()
    with contextlib.suppress(Exception):  # malformed markup: keep what was read
        parser.feed(html)
        parser.close()
    base = urljoin(page_url, parser.base) if parser.base else page_url
    regex = re.compile(pattern, re.IGNORECASE)
    order: list[str] = []
    best: dict[str, tuple[str, str]] = {}
    page_id = item_id_for(page_url)
    for href, text in parser.links:
        url = urljoin(base, href.strip()).split("#", 1)[0]
        if not url.startswith(("http://", "https://")) or not regex.search(url):
            continue
        key = item_id_for(url)
        if key == page_id:
            continue
        if key not in best:
            order.append(key)
            best[key] = (url, text)
        elif len(text) > len(best[key][1]):
            best[key] = (best[key][0], text)
    return [best[key] for key in order]


_URL_DATE = re.compile(r"(?<!\d)(20\d{2})[/_-](0?[1-9]|1[0-2])[/_-](0?[1-9]|[12]\d|3[01])(?!\d)")


def date_from_url(url: str) -> datetime | None:
    match = _URL_DATE.search(urlsplit(url).path)
    if not match:
        return None
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3]), 12, tzinfo=UTC)
    except ValueError:
        return None


class _ArticleExtractor(HTMLParser):
    """Title, description, published time and the first paragraphs of an article page."""

    META_TEXT = ("og:description", "description", "twitter:description", "dcterms.description")
    META_TITLE = ("og:title", "twitter:title", "dcterms.title")
    META_TIME = ("article:published_time", "dcterms.issued", "dcterms.date", "date", "dc.date")
    CHROME = frozenset({"nav", "header", "footer", "aside"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.time = ""
        self.paragraphs: list[str] = []
        self._in_title = False
        self._p: list[str] | None = None
        self._skip = 0
        self._chrome = 0

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag in _SKIP:
            self._skip += 1
        elif tag in self.CHROME:
            self._chrome += 1
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title":
            self._in_title = True
        elif tag == "time" and a.get("datetime") and not self.time and not self._chrome:
            self.time = a["datetime"]
        elif tag == "p" and not self._chrome:
            self._p = []

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in self.CHROME and self._chrome:
            self._chrome -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._p is not None:
            text = collapse_ws(" ".join(self._p))
            if len(text) >= 40:
                self.paragraphs.append(text)
            self._p = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif self._p is not None:
            self._p.append(data)

    def result(self) -> dict:
        description = next((collapse_ws(self.meta[k]) for k in self.META_TEXT if self.meta.get(k)), "")
        title = next((collapse_ws(self.meta[k]) for k in self.META_TITLE if self.meta.get(k)), "") or collapse_ws(self.title)
        stamp = next((self.meta[k] for k in self.META_TIME if self.meta.get(k)), "") or self.time
        body = " ".join(p for p in self.paragraphs[:6] if p not in description)
        return {"title": title, "text": collapse_ws(f"{description} {body}"), "published": _parse_when(stamp)}


def _parse_when(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    dt = parse_iso(value.replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", value) else value)
    if dt is None and re.fullmatch(r"\d{4}-\d\d-\d\d", value[:10]):
        dt = parse_iso(value[:10] + "T12:00:00Z")
    return dt


def article_details(client: httpx.Client, url: str, settings: Settings) -> dict:
    """Open one item's page for its description; any failure just means no extra text."""
    cfg = settings.fetch
    try:
        resp = client.get(
            url,
            headers={"User-Agent": cfg["user_agent"], "Accept": PAGE_ACCEPT},
            timeout=cfg["timeout_s"],
            follow_redirects=True,
        )
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
            return {}
        parser = _ArticleExtractor()
        parser.feed(resp.text[:2_000_000])
        parser.close()
        return parser.result()
    except Exception:
        return {}


# ---- shared -----------------------------------------------------------------------------


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


RATE_LIMIT_WAIT_S = 10.0  # one retry after a 429, after Retry-After or this long
RATE_LIMIT_WAIT_MAX_S = 30.0


def _retry_after(value: str | None) -> float | None:
    if not value:
        return RATE_LIMIT_WAIT_S
    try:
        wait = float(value)
    except ValueError:
        return RATE_LIMIT_WAIT_S
    return wait if 0 <= wait <= RATE_LIMIT_WAIT_MAX_S else None


def _get(client: httpx.Client, source: Source, state: dict, settings: Settings) -> httpx.Response:
    cfg = settings.fetch
    headers = {"User-Agent": cfg["user_agent"], "Accept": PAGE_ACCEPT if source.kind == "page" else FEED_ACCEPT}
    if state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    if state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    resp = client.get(source.url, headers=headers, timeout=cfg["timeout_s"], follow_redirects=True)
    if resp.status_code == 429:  # rate limited: wait as asked (briefly) and try once more
        wait = _retry_after(resp.headers.get("retry-after"))
        if wait is not None:
            time.sleep(wait)
            resp = client.get(source.url, headers=headers, timeout=cfg["timeout_s"], follow_redirects=True)
    return resp


def _admit(
    source: Source,
    settings: Settings,
    stat: FetchStat,
    seen: _Seen,
    now: datetime,
    *,
    link: str,
    title: str,
    text: str,
    published: datetime | None,
    publisher: str = "",
) -> Item | None:
    """Keyword rules, mute list and de-duplication for one candidate item."""
    if not source.matches(f"{title} {text}"):
        stat.filtered += 1
        return None
    if muted(settings, f"{title} {text}"):
        stat.muted += 1
        return None
    item_id = item_id_for(link)
    fp = title_fingerprint(title)
    if source.track_updates:  # the same link with new text (a new stage) counts as a new report
        item_id = short_hash(f"{item_id} {normalize_title(text[:300])}")
        fp = title_fingerprint(f"{title} {text[:300]}")
    if not seen.claim(item_id, source.id, fp):
        stat.duplicate += 1
        return None
    return Item(
        id=item_id,
        source_id=source.id,
        url=link,
        title=truncate(title, 300),
        snippet=truncate(text, SNIPPET_CHARS),
        fetched=to_iso(now),
        published=to_iso(published) if published else None,
        lang=source.lang,
        fp=fp,
        publisher=publisher,
    )


def _feed_items(client, resp, source, state, settings, now, seen, stat, first_import: bool) -> tuple[list[Item], str | None]:
    cfg = settings.fetch
    parsed = feedparser.parse(resp.content, response_headers={"content-type": resp.headers.get("content-type", "")})
    entries = list(parsed.entries or [])
    stat.entries = len(entries)
    if not entries and parsed.get("bozo"):
        return [], f"not a feed: {parsed.get('bozo_exception')}"[:200]
    if not entries:
        return [], "empty"
    # Some feeds list oldest first; look at the newest entries (undated ones keep feed order, last).
    entries.sort(key=lambda e: (_entry_time(e) is None, -(_entry_time(e) or now).timestamp()))

    undated_budget = int(cfg["first_import_undated_max"])
    age_limit = float(cfg["first_import_hours"] if first_import else cfg["max_age_hours"])
    items: list[Item] = []
    opened = 0
    for entry in entries[: int(cfg["max_items_per_feed"])]:
        link = _entry_link(entry, source.url)
        if not link:
            continue
        title = html_to_text(entry.get("title")) or "(untitled)"
        publisher = ""
        if source.kind == "search":
            link = unwrap_link(link)
            publisher = entry_publisher(entry, link)
            title = _strip_publisher(title, publisher)
        text = _entry_text(entry)
        if source.kind == "search" and text.startswith(title):
            text = text[len(title) :].strip(" -–—|")  # search feeds often repeat the headline as the text
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
        item = _admit(source, settings, stat, seen, now, link=link, title=title, text=text, published=published, publisher=publisher)
        if not item:
            continue
        # A title-only entry gives triage nothing to go on: open its page once for the description.
        # Search items are skipped: their links lead to many outlets and they come with text.
        wants_text = source.fetch_text or len(text) < FEED_TEXT_MIN
        if wants_text and source.kind != "search" and opened < PAGE_TEXT_MAX:
            if opened:
                time.sleep(PAGE_TEXT_PAUSE_S)
            opened += 1
            extra = article_details(client, link, settings).get("text", "")
            if len(extra) > len(text):
                item.snippet = truncate(extra, SNIPPET_CHARS)
        items.append(item)
    return items, None


def _page_items(client, resp, source, state, settings, now, seen, stat, first_import: bool) -> tuple[list[Item], str | None]:
    links = page_links(resp.text, str(resp.url), source.link_pattern)
    stat.entries = len(links)
    if not links:
        return [], f"no links match link_pattern {source.link_pattern!r}"
    known = list(state.get("page_seen", []))
    known_set = set(known)
    fresh = [(url, title) for url, title in links if item_id_for(url) not in known_set]
    state["page_seen"] = (known + [item_id_for(url) for url, _ in fresh])[-PAGE_SEEN_MAX:]
    if first_import:
        return [], "baseline"  # the first read only records what is already on the page
    if len(fresh) > PAGE_FLOOD:
        return [], "baseline"  # a redesign or a new URL scheme, not a burst of news

    age_limit = float(settings.fetch["max_age_hours"])
    items: list[Item] = []
    opened = 0
    for url, title in fresh[: int(settings.fetch["max_items_per_feed"])]:
        published = date_from_url(url)
        text = ""
        if source.fetch_text and opened < PAGE_TEXT_MAX:
            if opened:
                time.sleep(PAGE_TEXT_PAUSE_S)
            opened += 1
            details = article_details(client, url, settings)
            text = details.get("text", "")
            published = published or details.get("published")
            if len(title) < PAGE_TITLE_MIN and details.get("title"):
                title = details["title"]
        if len(title) < PAGE_TITLE_MIN:
            stat.filtered += 1
            continue
        if published and published > now + timedelta(hours=2):
            published = now
        if published and hours_between(now, published) > age_limit:
            stat.old += 1
            continue
        item = _admit(source, settings, stat, seen, now, link=url, title=title, text=text, published=published)
        if item:
            items.append(item)
    return items, None


def fetch_source(
    client: httpx.Client,
    source: Source,
    feed_state: dict,
    settings: Settings,
    now: datetime,
    seen: _Seen,
) -> tuple[list[Item], FetchStat, dict]:
    stat = FetchStat(source.id)
    state = dict(feed_state)
    started = time.monotonic()

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
        resp = _get(client, source, state, settings)
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

    first_import = not state.get("first_ok")
    if source.kind == "page":
        items, problem = _page_items(client, resp, source, state, settings, now, seen, stat, first_import)
    else:
        items, problem = _feed_items(client, resp, source, state, settings, now, seen, stat, first_import)
    if problem == "empty":
        finish("empty", "the feed has no entries")
        return [], stat, state
    if problem and problem != "baseline":
        finish("error", problem)
        return [], stat, state

    stat.new = len(items)
    if resp.headers.get("etag"):
        state["etag"] = resp.headers["etag"]
    if resp.headers.get("last-modified"):
        state["last_modified"] = resp.headers["last-modified"]
    if first_import:
        state["first_ok"] = to_iso(now)
    state["items_total"] = int(state.get("items_total", 0)) + stat.new
    finish("baseline" if problem == "baseline" else "ok")
    return items, stat, state


def _by_host(sources: list[Source]) -> list[list[Source]]:
    """Sources grouped by host, so one site is never asked for several feeds at once."""
    groups: dict[str, list[Source]] = {}
    for src in sources:
        groups.setdefault(urlsplit(src.url).netloc.lower(), []).append(src)
    return list(groups.values())


def _one_host(group: list[Source], work, settings: Settings) -> list:
    """Run `work` for each source of one host in turn, pausing between requests to that host."""
    out = []
    for i, src in enumerate(group):
        if i:
            time.sleep(max(float(settings.fetch.get("same_host_pause_s", 1.0)), src.pause_s))
        out.append((src, work(src)))
    return out


def fetch_all(
    settings: Settings,
    feed_states: dict,
    known_items: dict[str, Item],
    now: datetime,
    client: httpx.Client | None = None,
) -> tuple[list[Item], list[FetchStat]]:
    """Fetch every enabled source, hosts in parallel. Updates `feed_states` in place."""
    sources = [s for s in settings.sources if s.enabled]
    seen = _Seen(known_items)
    own_client = client is None
    client = client or httpx.Client(http2=False)
    done: dict[str, tuple[list[Item], FetchStat, dict]] = {}
    try:
        work = lambda src: fetch_source(client, src, feed_states.get(src.id, {}), settings, now, seen)
        with ThreadPoolExecutor(max_workers=max(1, int(settings.fetch["workers"]))) as pool:
            futures = [pool.submit(_one_host, group, work, settings) for group in _by_host(sources)]
            for future in futures:
                done.update({src.id: result for src, result in future.result()})
    finally:
        if own_client:
            client.close()

    new_items: list[Item] = []
    stats: list[FetchStat] = []
    for src in sources:
        items, stat, state = done[src.id]
        feed_states[src.id] = state
        new_items.extend(items)
        stats.append(stat)
    new_items.sort(key=lambda it: (it.when(), it.id))
    return new_items, stats


# ---- checking sources (`microhot sources --check`) --------------------------------------

CHECK_SAMPLES = 5


def check_source(client: httpx.Client, source: Source, settings: Settings, now: datetime, days: int = 14) -> dict:
    """Read one source as if for the first time, without saving anything, and say what it would yield.

    `matches` counts what passes the keyword rules over the last `days` days (feeds) or on the page
    right now (pages), so a filter that never matches shows up before it costs a week of silence."""
    row = {"id": source.id, "kind": source.kind, "beat": source.beat, "tier": source.tier, "status": "ok", "http": None,
           "entries": 0, "matches": 0, "newest": None, "error": None, "sample": "", "samples": []}
    started = time.monotonic()
    try:
        resp = _get(client, source, {}, settings)
    except httpx.HTTPError as exc:
        row.update(status="error", error=f"{type(exc).__name__}: {exc}"[:160])
        return row
    row["http"] = resp.status_code
    if resp.status_code >= 400:
        row.update(status="error", error=f"HTTP {resp.status_code}")
        return row
    since = now - timedelta(days=days)
    if source.kind == "page":
        links = page_links(resp.text, str(resp.url), source.link_pattern)
        row["entries"] = len(links)
        hits = [title for _, title in links if len(title) >= PAGE_TITLE_MIN and source.matches(title)]
        row["matches"] = len(hits)
        row["sample"] = hits[0] if hits else (links[0][1] if links else "")
        row["samples"] = hits[:CHECK_SAMPLES]
        if not links:
            row.update(status="error", error="no links match link_pattern")
    else:
        parsed = feedparser.parse(resp.content, response_headers={"content-type": resp.headers.get("content-type", "")})
        entries = list(parsed.entries or [])
        row["entries"] = len(entries)
        if not entries:
            row.update(status="error" if parsed.get("bozo") else "empty",
                       error=f"not a feed: {parsed.get('bozo_exception')}"[:160] if parsed.get("bozo") else "no entries")
        dates = [d for d in (_entry_time(e) for e in entries) if d]
        row["newest"] = to_iso(max(dates)) if dates else None
        for entry in entries:
            when = _entry_time(entry)
            title = html_to_text(entry.get("title"))
            if source.kind == "search":
                title = _strip_publisher(title, entry_publisher(entry, unwrap_link(_entry_link(entry, source.url) or "")))
            if (when is None or when >= since) and source.matches(f"{title} {_entry_text(entry)}"):
                row["matches"] += 1
                row["sample"] = row["sample"] or title
                if len(row["samples"]) < CHECK_SAMPLES:
                    row["samples"].append(title)
    row["ms"] = int((time.monotonic() - started) * 1000)
    return row


def check_sources(settings: Settings, now: datetime, client: httpx.Client | None = None, include_disabled: bool = False,
                  only: list[str] | None = None) -> list[dict]:
    sources = [s for s in settings.sources if (s.enabled or include_disabled) and (not only or s.id in only)]
    own_client = client is None
    client = client or httpx.Client(http2=False)
    done: dict[str, dict] = {}
    try:
        work = lambda src: check_source(client, src, settings, now)
        with ThreadPoolExecutor(max_workers=max(1, int(settings.fetch["workers"]))) as pool:
            futures = [pool.submit(_one_host, group, work, settings) for group in _by_host(sources)]
            for future in futures:
                done.update({src.id: row for src, row in future.result()})
    finally:
        if own_client:
            client.close()
    rows = []
    for src in sources:
        done[src.id]["enabled"] = src.enabled
        rows.append(done[src.id])
    return rows
