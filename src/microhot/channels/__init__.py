"""Delivery channels for the morning edition.

The private web page is always built and is the source of truth; every channel links back to it.
A channel runs only when it is enabled in config/site.toml AND its secrets are set.
"""

from __future__ import annotations

import os
from datetime import datetime

from .. import i18n
from ..config import Settings
from ..store import Store
from ..textutil import parse_iso, to_iso, utcnow
from . import email, github_issue, telegram

CHANNELS = {"github_issue": github_issue, "email": email, "telegram": telegram}


def langs_for(cfg: dict) -> list[str]:
    value = cfg.get("lang", "en")
    return ["en", "zh-TW"] if value == "both" else [value if value in ("en", "zh-TW") else "en"]


def edition_url(settings: Settings, date: str, lang: str = "en") -> str:
    return f"{settings.site_url}{i18n.prefix(lang)}/d/{date}.html"


def edition_title(settings: Settings, edition: dict, lang: str) -> str:
    built = parse_iso(edition.get("built_at")) or utcnow()
    return f"{settings.site['name']}: " + i18n.t(lang, "edition_title", date=i18n.fmt_date(built, lang, settings.tz))


def story_lines(story: dict, langs: list[str]) -> tuple[list[str], list[str]]:
    """Headlines and summaries in the requested languages (summaries skip empty ones)."""
    titles = [story["title"].get(lang) or story["title"]["en"] for lang in langs]
    summaries = [story["summary"].get(lang) for lang in langs if story["summary"].get(lang)]
    if len(titles) == 2 and titles[0] == titles[1]:
        titles = titles[:1]
    return titles, summaries


def missing_env(module, environ) -> list[str]:
    return [name for name in module.REQUIRED if not str(environ.get(name, "")).strip()]


def channel_status(settings: Settings, environ=None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    out = {}
    for name, module in CHANNELS.items():
        cfg = settings.channels.get(name, {})
        if not cfg.get("enabled"):
            out[name] = "off in config"
            continue
        missing = missing_env(module, environ)
        out[name] = "ready" if not missing else "not configured (" + ", ".join(missing) + ")"
    return out


def deliver(settings: Settings, store: Store, now: datetime | None = None, environ=None) -> dict[str, str]:
    """Send today's edition to every ready channel that has not received it yet."""
    environ = os.environ if environ is None else environ
    now = now or utcnow()
    date = now.astimezone(settings.tz).date().isoformat()
    edition = store.load_edition(date)
    if not edition:
        return {"edition": f"no edition for {date} yet"}
    results: dict[str, str] = {}
    delivered = edition.setdefault("delivered", {})
    for name, module in CHANNELS.items():
        cfg = settings.channels.get(name, {})
        if not cfg.get("enabled"):
            results[name] = "off in config"
            continue
        missing = missing_env(module, environ)
        if missing:
            results[name] = "not configured (" + ", ".join(missing) + ")"
            continue
        if delivered.get(name):
            results[name] = "already sent"
            continue
        if not edition.get("stories") and not cfg.get("send_empty", False):
            results[name] = "skipped (empty edition)"
            continue
        try:
            module.send(edition, settings, cfg, environ)
        except Exception as exc:  # a failing channel never fails the run
            results[name] = f"error: {type(exc).__name__}: {exc}"[:240]
            continue
        delivered[name] = to_iso(now)
        results[name] = "sent"
    store.save_edition(edition)
    return results
