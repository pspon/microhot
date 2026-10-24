"""Render the private static site: English at /, Traditional Chinese at /zh-tw/."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import datetime, timedelta
from email.utils import format_datetime
from importlib import resources

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from . import i18n
from .archive import load_log
from .config import LANGS, Settings
from .feedback import evaluate
from .llm.usage import UsageLog
from .markdown import edition_markdown, edition_title, review_markdown, review_title
from .model import Event, Item
from .radar import radar_by_beat
from .select import near_misses
from .store import Store, read_json
from .textutil import parse_iso, truncate
from .topics import GROUPS, Topic, effective_score, load_topics, topics_for
from .views import story_view, temp_class, temperature

TODAY_HOURS = 24
FEED_LIMIT = 50
TOPIC_OTHER_DAYS = 7
SEARCH_LIMIT = 3000  # picks, and separately stories not picked
SEARCH_SUMMARY_CHARS = 200
AXES = ["sig", "nov", "cred", "res", "act"]
STATIC_FILES = ("style.css", "icon.svg", "search.js", "visit.js")


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("microhot", "templates"),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _minutes(stats: dict, lang: str) -> int:
    if lang == "zh-TW" and stats.get("chars_zh"):
        return max(1, math.ceil(stats["chars_zh"] / 400))
    return max(1, math.ceil(stats.get("words_en", 0) / 220))


class SiteBuilder:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        events: dict[str, Event],
        items: dict[str, Item],
        now: datetime,
        run_summary: dict | None = None,
        topics: list[Topic] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.events = events
        self.items = items
        self.now = now
        self.tz = settings.tz
        self.run_summary = run_summary or read_json(store.dir / "runs" / "latest.json", {}) or {}
        self.topics = load_topics(settings) if topics is None else topics
        self.static_version = ""
        self.event_page_ids: set[str] = set()
        self.env = _env()
        self.out = settings.public_dir
        self.written = 0

    # ---- helpers -------------------------------------------------------------------------
    def _write(self, rel: str, content: str) -> None:
        path = self.out / rel.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.written += 1

    def _render(self, template: str, rel: str, lang: str, page: str, path: str, **ctx) -> None:
        self._write(rel, self.env.get_template(template).render(**self._common(lang, page, path), **ctx))

    def _common(self, lang: str, page: str, path: str) -> dict:
        other = "zh-TW" if lang == "en" else "en"
        return {
            "lang": lang,
            "page": page,
            "prefix": i18n.prefix(lang),
            "path": path,
            "alt_url": f"{i18n.prefix(other)}{path}",
            "alt_lang_attr": i18n.t(other, "html_lang"),
            "site_name": self.settings.site["name"],
            "site_name_zh": self.settings.site["name_zh"],
            "tagline": self.settings.site["tagline_en" if lang == "en" else "tagline_zh"],
            "updated": i18n.fmt_datetime(self.now, lang, self.tz),
            "static_version": self.static_version,
            "t": lambda key, **kw: i18n.t(lang, key, **kw),
            "ago": lambda iso: i18n.ago(parse_iso(iso) or self.now, self.now, lang),
            "when": lambda iso: i18n.fmt_datetime(parse_iso(iso) or self.now, lang, self.tz),
            "day": lambda iso: i18n.fmt_date(parse_iso(iso) or self.now, lang, self.tz),
            "sources_label": lambda n: i18n.sources_label(lang, n),
            "source_line": lambda s: self._source_line(s, lang),
            "event_pages": self.event_page_ids,
            "fmt_change": lambda c: ("↑" if c > 0 else "↓" if c < 0 else "±") + f"{abs(c):.1f}°",
            "edition_label": lambda d: i18n.t(lang, "edition_title", date=i18n.fmt_date(parse_iso(f"{d}T12:00:00Z") or self.now, lang, self.tz)),
            "editions_label": lambda n: i18n.t(lang, "edition_1") if n == 1 else i18n.t(lang, "editions_n", n=n),
        }

    @staticmethod
    def _source_line(story: dict, lang: str) -> str:
        names = []
        for link in story.get("links", []):
            if link["source"] not in names:
                names.append(link["source"])
        first = story.get("source") or (names[0] if names else "")
        others = len([n for n in names if n != first])
        if others:
            return i18n.t(lang, "and_more", name=first, n=others, total=others + 1)
        return first

    def _beat_label(self, beat: str, lang: str) -> str:
        cfg = self.settings.beats.get(beat, {})
        return cfg.get("label_en" if lang == "en" else "label_zh") or i18n.t(lang, f"beat_{beat}")

    def _group(self, stories: list[dict], lang: str) -> list[dict]:
        groups = []
        for beat in self.settings.beat_order:
            rows = [s for s in stories if s["beat"] == beat]
            if rows:
                groups.append({"beat": beat, "label": self._beat_label(beat, lang), "stories": rows})
        return groups

    def view(self, event: Event) -> dict:
        return story_view(event, self.items, self.settings, self.now, self.topics)

    # ---- data ----------------------------------------------------------------------------
    def _live(self) -> list[Event]:
        return [e for e in self.events.values() if e.is_live and e.rep in self.items]

    def _today_stories(self) -> list[dict]:
        since = self.now - timedelta(hours=TODAY_HOURS)
        picked = [e for e in self._live() if e.selected_at and (parse_iso(e.selected_at) or self.now) >= since]
        picked.sort(key=lambda e: (-e.heat, -effective_score(e), e.id))
        return [self.view(e) for e in picked]

    def _radar(self) -> list[dict]:
        """[{"beat", "stories"}] for the Today page, beats in site order."""
        by_beat = radar_by_beat(self.events, self.items, self.settings, self.now)
        return [{"beat": beat, "stories": [self.view(e) for e in rows]} for beat, rows in by_beat.items()]

    def _not_picked(self) -> list[dict]:
        """Stories in the log found in the last [radar] archive_days and never picked, newest first."""
        days = int(self.settings.radar["archive_days"])
        if days <= 0:
            return []
        rows = load_log(self.store, self.settings, self.now - timedelta(days=days), self.now)
        return [r for r in rows if not r.get("picked") and r.get("type") != "off_topic"]

    def _archive_days(self, rows: list[dict]) -> list[dict]:
        """The not-picked stories grouped by the local day they were found, newest day first."""
        days: dict[str, list[dict]] = {}
        for row in rows:
            found = parse_iso(row["found"]) or self.now
            days.setdefault(found.astimezone(self.tz).date().isoformat(), []).append(self._archive_view(row))
        out = []
        for date in sorted(days, reverse=True):
            stories = days[date]
            stories.sort(key=lambda v: (v["score"] is None, -(v["score"] or 0), -v["temp"], v["id"]))
            counts = {beat: sum(1 for v in stories if v["beat"] == beat) for beat in self.settings.beat_order}
            out.append({"date": date, "stories": stories, "counts": {b: n for b, n in counts.items() if n}})
        return out

    def _archive_view(self, row: dict) -> dict:
        score = None if row.get("score") is None else int(row["score"]) + int(row.get("boost") or 0)
        temp = float(row.get("temp_max") or temperature(0))
        names = list(dict.fromkeys(link["source"] for link in row.get("links", [])))
        return {
            "id": row["id"],
            "beat": row.get("beat"),
            "type": row.get("type"),
            "title": {"en": row["title"], "zh-TW": row.get("title_zh") or row["title"]},
            "summary": row.get("summary") or "",
            "url": row.get("url", ""),
            "source": row.get("source", ""),
            "others": len([n for n in names if n != row.get("source")]),
            "score": score,
            "threshold": row.get("threshold"),
            "temp": temp,
            "temp_class": temp_class(temp),
            "found": row["found"],
        }

    def _picked_views(self) -> list[dict]:
        picked = [e for e in self._live() if e.selected_at]
        picked.sort(key=lambda e: e.selected_at or "", reverse=True)
        return [self.view(e) for e in picked]

    def _topic_pages(self, picked_views: list[dict]) -> list[dict]:
        other_since = self.now - timedelta(days=TOPIC_OTHER_DAYS)
        others = [
            e
            for e in self._live()
            if not e.selected_at and e.type != "off_topic" and (parse_iso(e.last_seen) or self.now) >= other_since
        ]
        other_matches = {e.id: [t.id for t in topics_for(e, self.items, self.topics)] for e in others}
        pages = []
        for topic in self.topics:
            picked = [v for v in picked_views if any(tp["id"] == topic.id for tp in v["topics"])]
            rest = [self.view(e) for e in others if topic.id in other_matches[e.id]]
            rest.sort(key=lambda v: (-v["heat"], v["id"]))
            pages.append({"topic": topic, "picked": picked, "other": rest[:15], "count": len(picked)})
        return pages

    def _search_index(self, editions: list[dict], picked_views: list[dict], archive: list[dict]) -> list[dict]:
        """Every pick, then the stories not picked in the archive window (marked "x": 1)."""
        rows: dict[str, dict] = {}

        def add(story: dict, date: str) -> None:
            rows[story["id"]] = {
                "i": story["id"],
                "d": date,
                "u": f"/e/{story['id']}.html" if story["id"] in self.event_page_ids else story["url"],
                "s": story["source"],
                "t": [story["title"]["en"], story["title"]["zh-TW"]],
                "m": [story["summary"]["en"], story["summary"]["zh-TW"]],
            }

        for edition in reversed(editions):  # oldest first, so newer snapshots win
            for story in edition.get("stories", []) + edition.get("followups", []):
                add(story, edition["date"])
        for view in picked_views:
            if view["id"] not in rows:
                selected = parse_iso(view["selected_at"]) or self.now
                add(view, selected.astimezone(self.tz).date().isoformat())
        ordered = sorted(rows.values(), key=lambda r: (r["d"], r["i"]), reverse=True)[:SEARCH_LIMIT]
        unpicked = []
        for day in archive:
            for story in day["stories"]:
                if story["id"] in rows:
                    continue
                unpicked.append({
                    "i": story["id"],
                    "d": day["date"],
                    "u": f"/r/{day['date']}.html#s-{story['id']}",
                    "s": story["source"],
                    "t": [story["title"]["en"], story["title"]["zh-TW"]],
                    "m": [truncate(story["summary"], SEARCH_SUMMARY_CHARS), ""],
                    "x": 1,
                })
        merged = ordered + unpicked[:SEARCH_LIMIT]
        merged.sort(key=lambda r: r["d"], reverse=True)  # stable: picks stay ahead of a day's other stories
        return merged

    def _ratings(self) -> dict:
        try:
            return evaluate(self.settings, self.store)
        except (KeyError, ValueError, TypeError):
            return {"labels": 0}

    # ---- build ---------------------------------------------------------------------------
    def build(self) -> dict:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        static_src = resources.files("microhot") / "static"
        static = {name: (static_src / name).read_text(encoding="utf-8") for name in STATIC_FILES}
        # One version for all static files: they are cached as immutable, so any change needs a new URL.
        self.static_version = hashlib.sha1("".join(static.values()).encode()).hexdigest()[:8]
        for name, text in static.items():
            self._write(f"static/{name}", text)
        self._write("robots.txt", "User-agent: *\nDisallow: /\n")
        self._write(
            "_headers",
            "/*\n  X-Robots-Tag: noindex, nofollow\n  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n"
            "/static/*\n  Cache-Control: public, max-age=31536000, immutable\n",
        )

        selected = [e for e in self._live() if e.selected_at]
        self.event_page_ids = {e.id for e in selected}
        today = self._today_stories()
        radar = self._radar()
        archive = self._archive_days(self._not_picked())
        picked_views = self._picked_views()
        editions = [ed for ed in (self.store.load_edition(d) for d in self.store.list_editions()) if ed]
        reviews = self.store.list_reviews()
        topic_pages = self._topic_pages(picked_views)
        llm_ready = bool(self.run_summary.get("llm", {}).get("active", {}).get("triage"))
        high = max((s["temp"] for s in today), default=None)
        search = self._search_index(editions, picked_views, archive)
        self._write("search.json", json.dumps(search, ensure_ascii=False, separators=(",", ":")))

        usage = UsageLog(self.store.usage_dir, self.tz)
        about_ctx = {
            "sources": self._sources_table(),
            "providers": self._providers_table(),
            "usage_today": UsageLog.summarize(usage.rows_for_day(self.now)),
            "usage_month": UsageLog.summarize(usage.rows_for_month(self.now)),
            "caps": {"calls": self.settings.llm["max_calls_per_day"], "usd": self.settings.llm["usd_cap_per_day"]},
            "runs": self.store.recent_runs(10),
            "misses": near_misses(self.events, self.items, self.settings, self.now),
            "ratings": self._ratings(),
            "beats": [(b, self.settings.beats[b]) for b in self.settings.beat_order],
            "beat_labels": {b: {lang: self._beat_label(b, lang) for lang in LANGS} for b in self.settings.beat_order},
            "topic_names": {tp.id: {"en": tp.name_en, "zh-TW": tp.name_zh} for tp in self.topics},
        }

        for lang in LANGS:
            p = i18n.prefix(lang)
            self._render(
                "today.html.j2",
                f"{p}/index.html",
                lang,
                "today",
                "/",
                groups=self._group(today, lang),
                any_stories=bool(today),
                radar=[{**g, "label": self._beat_label(g["beat"], lang)} for g in radar],
                radar_hours=int(self.settings.radar["hours"]),
                has_archive=bool(archive),
                llm_ready=llm_ready,
                high=high,
                high_class=temp_class(high) if high else "cool",
                day_label=i18n.fmt_date(self.now, lang, self.tz),
                latest_edition=editions[0]["date"] if editions else None,
                latest_edition_label=i18n.fmt_date(parse_iso(editions[0]["built_at"]) or self.now, lang, self.tz) if editions else "",
            )
            for event in selected:
                story = self.view(event)
                path = f"/e/{event.id}.html"
                self._render(
                    "event.html.j2",
                    f"{p}{path}",
                    lang,
                    "event",
                    path,
                    s=story,
                    beat_label=self._beat_label(story["beat"], lang),
                    threshold=self.settings.threshold_for(story["tier"], story["beat"]),
                    axes_order=AXES,
                )
            for i, ed in enumerate(editions):
                path = f"/d/{ed['date']}.html"
                by_id = {s["id"]: s for s in ed["stories"]}
                lead = by_id.get(ed.get("lead"))
                rest = [s for s in ed["stories"] if not lead or s["id"] != lead["id"]]
                highlights = [by_id[h] for h in ed.get("highlights", []) if h in by_id]
                stats = ed.get("stats") or {}
                self._render(
                    "edition.html.j2",
                    f"{p}{path}",
                    lang,
                    "edition",
                    path,
                    edition=ed,
                    title=edition_title(self.settings, ed, lang),
                    lead=lead,
                    highlights=highlights if len(rest) > len(highlights) else [],  # a skim list only helps long editions
                    groups=self._group(rest, lang),
                    followups=ed.get("followups", []),
                    stats_line=i18n.t(
                        lang,
                        "stats_line",
                        stories=i18n.t(lang, "story_1") if stats.get("stories") == 1 else i18n.t(lang, "stories_n", n=stats.get("stories", 0)),
                        sources=i18n.sources_label(lang, stats.get("sources", 0)),
                        official=stats.get("official", 0),
                        minutes=_minutes(stats, lang),
                    )
                    if stats
                    else "",
                    newer=editions[i - 1]["date"] if i > 0 else None,
                    older=editions[i + 1]["date"] if i + 1 < len(editions) else None,
                )
                self._write(f"{p}/d/{ed['date']}.md", edition_markdown(ed, self.settings, lang))
            for review in reviews:
                kind = "w" if review["kind"] == "weekly" else "m"
                path = f"/{kind}/{review['id']}.html"
                self._render(
                    "review.html.j2",
                    f"{p}{path}",
                    lang,
                    "edition",
                    path,
                    review=review,
                    title=review_title(self.settings, review, lang),
                    groups=self._group(review["stories"], lang),
                )
                self._write(f"{p}/{kind}/{review['id']}.md", review_markdown(review, self.settings, lang))
            self._render(
                "editions.html.j2",
                f"{p}/editions.html",
                lang,
                "editions",
                "/editions.html",
                editions=[
                    {
                        "date": ed["date"],
                        "label": i18n.fmt_date(parse_iso(ed["built_at"]) or self.now, lang, self.tz),
                        "count": len(ed["stories"]),
                    }
                    for ed in editions
                ],
                reviews=[
                    {"url": f"/{'w' if r['kind'] == 'weekly' else 'm'}/{r['id']}.html", "label": review_title(self.settings, r, lang), "count": len(r["stories"])}
                    for r in reviews
                ],
            )
            self._render(
                "topics.html.j2",
                f"{p}/topics.html",
                lang,
                "topics",
                "/topics.html",
                groups=[
                    {"label": i18n.t(lang, f"group_{g}"), "pages": [tp for tp in topic_pages if tp["topic"].group == g]}
                    for g in GROUPS
                    if any(tp["topic"].group == g for tp in topic_pages)
                ],
            )
            for tp in topic_pages:
                path = f"/t/{tp['topic'].id}.html"
                self._render("topic.html.j2", f"{p}{path}", lang, "topics", path, tp=tp, name=tp["topic"].name(lang))
            self._render_archive(archive, lang)
            self._render("search.html.j2", f"{p}/search.html", lang, "search", "/search.html")
            self._render("about.html.j2", f"{p}/about.html", lang, "about", "/about.html", **about_ctx)
            rfc822 = lambda iso: format_datetime(parse_iso(iso) or self.now)
            site_url = self.settings.site_url
            self._render("feed.xml.j2", f"{p}/feed.xml", lang, "feed", "/feed.xml", stories=picked_views[:FEED_LIMIT], site_url=site_url, rfc822=rfc822, feed_title="")
            for beat in self.settings.beat_order:
                stories = [v for v in picked_views if v["beat"] == beat][:FEED_LIMIT]
                self._render(
                    "feed.xml.j2",
                    f"{p}/feed/{beat}.xml",
                    lang,
                    "feed",
                    f"/feed/{beat}.xml",
                    stories=stories,
                    site_url=site_url,
                    rfc822=rfc822,
                    feed_title=self._beat_label(beat, lang),
                )
            issues = [
                {"title": edition_title(self.settings, ed, lang), "url": f"/d/{ed['date']}.html", "at": ed["built_at"], "id": ed["date"],
                 "titles": [s["title"][lang] for s in ed["stories"]]}
                for ed in editions[:30]
            ] + [
                {"title": review_title(self.settings, r, lang), "url": f"/{'w' if r['kind'] == 'weekly' else 'm'}/{r['id']}.html", "at": r["built_at"],
                 "id": r["id"], "titles": [s["title"][lang] for s in r["stories"]]}
                for r in reviews[:24]
            ]
            issues.sort(key=lambda x: x["at"], reverse=True)
            self._render("feed_issues.xml.j2", f"{p}/feed/editions.xml", lang, "feed", "/feed/editions.xml", issues=issues, site_url=site_url, rfc822=rfc822)
            self._render("404.html.j2", f"{p}/404.html", lang, "404", "/")
        return {
            "pages": self.written,
            "today": len(today),
            "radar": sum(len(g["stories"]) for g in radar),
            "not_picked_days": len(archive),
            "events": len(selected),
            "editions": len(editions),
            "reviews": len(reviews),
            "topics": len(topic_pages),
        }

    # ---- not picked, by day ------------------------------------------------------------------
    def _day_label(self, date: str, lang: str) -> str:
        return i18n.fmt_date(parse_iso(f"{date}T12:00:00Z") or self.now, lang, self.tz)

    def _render_archive(self, archive: list[dict], lang: str) -> None:
        p = i18n.prefix(lang)
        self._render(
            "radar.html.j2",
            f"{p}/radar.html",
            lang,
            "radar",
            "/radar.html",
            days=[
                {
                    "date": day["date"],
                    "label": self._day_label(day["date"], lang),
                    "total": len(day["stories"]),
                    "counts": [(self._beat_label(beat, lang), n) for beat, n in day["counts"].items()],
                }
                for day in archive
            ],
            archive_days=int(self.settings.radar["archive_days"]),
        )
        for i, day in enumerate(archive):
            path = f"/r/{day['date']}.html"
            self._render(
                "radar_day.html.j2",
                f"{p}{path}",
                lang,
                "radar",
                path,
                day_label=self._day_label(day["date"], lang),
                groups=[
                    {"beat": beat, "label": self._beat_label(beat, lang), "stories": [s for s in day["stories"] if s["beat"] == beat]}
                    for beat in day["counts"]
                ],
                newer=archive[i - 1]["date"] if i > 0 else None,
                older=archive[i + 1]["date"] if i + 1 < len(archive) else None,
            )

    # ---- about page tables -----------------------------------------------------------------
    def _sources_table(self) -> list[dict]:
        feeds = self.store.load_state().get("feeds", {})
        last = {row["source_id"]: row for row in self.run_summary.get("fetch", [])}
        rows = []
        for src in self.settings.sources:
            st = feeds.get(src.id, {})
            run = last.get(src.id, {})
            rows.append(
                {
                    "name": src.name,
                    "url": src.homepage or src.url,
                    "beat": src.beat,
                    "tier": src.tier,
                    "enabled": src.enabled,
                    "status": run.get("status") or st.get("last_status") or "never",
                    "error": run.get("error") or (st.get("last_error") if st.get("last_status") == "error" else None),
                    "new": run.get("new", 0),
                    "total": st.get("items_total", 0),
                    "last_ok": st.get("last_ok"),
                }
            )
        return rows

    def _providers_table(self) -> list[dict]:
        llm = self.run_summary.get("llm", {})
        unconfigured = llm.get("unconfigured", {})
        disabled = llm.get("disabled", {})
        rows = []
        for name, provider in self.settings.providers.items():
            steps = [step for step in ("triage", "write", "digest", "review") if name in self.settings.llm.get(step, [])]
            if name in disabled:
                status = f"turned off this run: {disabled[name]}"
            elif name in unconfigured:
                status = f"not configured ({unconfigured[name]})"
            elif llm:
                status = "active"
            else:
                status = "unknown (no run yet)"
            rows.append(
                {
                    "name": name,
                    "models": ", ".join(provider.models),
                    "steps": ", ".join(steps) or "not in any chain",
                    "free": provider.free,
                    "status": status,
                    "price": "free tier" if provider.free else f"${provider.price_in:g} in / ${provider.price_out:g} out per 1M",
                }
            )
        return rows


__all__ = ["SiteBuilder", "temperature"]
