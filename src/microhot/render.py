"""Render the private static site: English at /, Traditional Chinese at /zh-tw/."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timedelta
from email.utils import format_datetime
from importlib import resources

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from . import i18n
from .config import LANGS, Settings
from .llm.usage import UsageLog
from .model import Event, Item
from .select import near_misses
from .store import Store, read_json
from .textutil import parse_iso
from .views import story_view, temp_class, temperature

TODAY_HOURS = 24
RADAR_LIMIT = 10
FEED_LIMIT = 50


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("microhot", "templates"),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


class SiteBuilder:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        events: dict[str, Event],
        items: dict[str, Item],
        now: datetime,
        run_summary: dict | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.events = events
        self.items = items
        self.now = now
        self.tz = settings.tz
        self.run_summary = run_summary or read_json(store.dir / "runs" / "latest.json", {}) or {}
        self.css_version = ""
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

    def _common(self, lang: str, page: str, path: str) -> dict:
        other = "zh-TW" if lang == "en" else "en"
        prefix = i18n.prefix(lang)
        return {
            "lang": lang,
            "page": page,
            "prefix": prefix,
            "path": path,
            "alt_url": f"{i18n.prefix(other)}{path}",
            "alt_lang_attr": i18n.t(other, "html_lang"),
            "site_name": self.settings.site["name"],
            "site_name_zh": self.settings.site["name_zh"],
            "tagline": self.settings.site["tagline_en" if lang == "en" else "tagline_zh"],
            "updated": i18n.fmt_datetime(self.now, lang, self.tz),
            "css_version": self.css_version,
            "t": lambda key, **kw: i18n.t(lang, key, **kw),
            "ago": lambda iso: i18n.ago(parse_iso(iso) or self.now, self.now, lang),
            "when": lambda iso: i18n.fmt_datetime(parse_iso(iso) or self.now, lang, self.tz),
            "sources_label": lambda n: i18n.sources_label(lang, n),
            "source_line": lambda s: self._source_line(s, lang),
            "event_pages": self.event_page_ids,
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

    # ---- data ----------------------------------------------------------------------------
    def _live(self) -> list[Event]:
        return [e for e in self.events.values() if e.is_live and e.rep in self.items]

    def _today_stories(self) -> list[dict]:
        since = self.now - timedelta(hours=TODAY_HOURS)
        picked = [e for e in self._live() if e.selected_at and (parse_iso(e.selected_at) or self.now) >= since]
        picked.sort(key=lambda e: (-e.heat, -(e.score or 0), e.id))
        return [story_view(e, self.items, self.settings) for e in picked]

    def _radar(self) -> list[dict]:
        since = self.now - timedelta(hours=TODAY_HOURS)
        rows = [
            e
            for e in self._live()
            if not e.selected_at and e.type != "off_topic" and (parse_iso(e.last_seen) or self.now) >= since
        ]
        rows.sort(key=lambda e: (-e.heat, -(e.score or -1), e.id))
        return [story_view(e, self.items, self.settings) for e in rows[:RADAR_LIMIT]]

    def _feed_stories(self) -> list[dict]:
        picked = [e for e in self._live() if e.selected_at]
        picked.sort(key=lambda e: e.selected_at or "", reverse=True)
        return [story_view(e, self.items, self.settings) for e in picked[:FEED_LIMIT]]

    # ---- build ---------------------------------------------------------------------------
    def build(self) -> dict:
        if self.out.exists():
            shutil.rmtree(self.out)
        self.out.mkdir(parents=True)
        static_src = resources.files("microhot") / "static"
        css = (static_src / "style.css").read_text(encoding="utf-8")
        self.css_version = hashlib.sha1(css.encode()).hexdigest()[:8]
        for name in ("style.css", "icon.svg"):
            self._write(f"static/{name}", (static_src / name).read_text(encoding="utf-8"))
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
        feed = self._feed_stories()
        editions = [self.store.load_edition(d) for d in self.store.list_editions()]
        editions = [ed for ed in editions if ed]
        llm_ready = bool(self.run_summary.get("llm", {}).get("active", {}).get("triage"))
        high = max((s["temp"] for s in today), default=None)

        usage = UsageLog(self.store.usage_dir, self.tz)
        about_ctx = {
            "sources": self._sources_table(),
            "providers": self._providers_table(),
            "usage_today": UsageLog.summarize(usage.rows_for_day(self.now)),
            "usage_month": UsageLog.summarize(usage.rows_for_month(self.now)),
            "caps": {
                "calls": self.settings.llm["max_calls_per_day"],
                "usd": self.settings.llm["usd_cap_per_day"],
            },
            "runs": self.store.recent_runs(10),
            "misses": near_misses(self.events, self.items, self.settings, self.now),
        }

        for lang in LANGS:
            p = i18n.prefix(lang)
            ctx = self._common(lang, "today", "/")
            self._write(
                f"{p}/index.html",
                self.env.get_template("today.html.j2").render(
                    **ctx,
                    groups=self._group(today, lang),
                    any_stories=bool(today),
                    radar=radar,
                    llm_ready=llm_ready,
                    high=high,
                    high_class=temp_class(high) if high else "cool",
                    day_label=i18n.fmt_date(self.now, lang, self.tz),
                    latest_edition=editions[0]["date"] if editions else None,
                    latest_edition_label=(
                        i18n.fmt_date(parse_iso(editions[0]["built_at"]) or self.now, lang, self.tz) if editions else ""
                    ),
                ),
            )
            for event in selected:
                story = story_view(event, self.items, self.settings)
                path = f"/e/{event.id}.html"
                self._write(
                    f"{p}{path}",
                    self.env.get_template("event.html.j2").render(
                        **self._common(lang, "event", path),
                        s=story,
                        beat_label=self._beat_label(story["beat"], lang),
                        threshold=self.settings.threshold_for(story["tier"]),
                        axes_order=["sig", "nov", "cred", "res", "act"],
                    ),
                )
            for ed in editions:
                path = f"/d/{ed['date']}.html"
                built = parse_iso(ed["built_at"]) or self.now
                self._write(
                    f"{p}{path}",
                    self.env.get_template("edition.html.j2").render(
                        **self._common(lang, "edition", path),
                        edition=ed,
                        title=i18n.t(lang, "edition_title", date=i18n.fmt_date(built, lang, self.tz)),
                        groups=self._group(ed["stories"], lang),
                    ),
                )
            self._write(
                f"{p}/editions.html",
                self.env.get_template("editions.html.j2").render(
                    **self._common(lang, "editions", "/editions.html"),
                    editions=[
                        {
                            "date": ed["date"],
                            "label": i18n.fmt_date(parse_iso(ed["built_at"]) or self.now, lang, self.tz),
                            "count": len(ed["stories"]),
                        }
                        for ed in editions
                    ],
                ),
            )
            self._write(
                f"{p}/about.html",
                self.env.get_template("about.html.j2").render(**self._common(lang, "about", "/about.html"), **about_ctx),
            )
            self._write(
                f"{p}/feed.xml",
                self.env.get_template("feed.xml.j2").render(
                    **self._common(lang, "feed", "/feed.xml"),
                    stories=feed,
                    site_url=self.settings.site_url,
                    rfc822=lambda iso: format_datetime(parse_iso(iso) or self.now),
                ),
            )
            self._write(f"{p}/404.html", self.env.get_template("404.html.j2").render(**self._common(lang, "404", "/")))
        return {"pages": self.written, "today": len(today), "radar": len(radar), "events": len(selected), "editions": len(editions)}

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
            steps = [step for step in ("triage", "write") if name in self.settings.llm.get(step, [])]
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
