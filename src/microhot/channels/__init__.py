"""Delivery channels for the morning edition, the weekly/monthly reviews and alerts.

The private web page is always built and is the source of truth; every channel links back to it.
A channel runs only when it is enabled in config/site.toml AND its secrets are set.
"""

from __future__ import annotations

import os
from datetime import datetime

from .. import i18n
from ..config import Settings
from ..markdown import edition_title as _edition_title
from ..markdown import review_title as _review_title
from ..store import Store
from ..textutil import parse_iso, to_iso, utcnow
from . import email, github_issue, telegram

CHANNELS = {"github_issue": github_issue, "email": email, "telegram": telegram}


def langs_for(cfg: dict) -> list[str]:
    value = cfg.get("lang", "en")
    return ["en", "zh-TW"] if value == "both" else [value if value in ("en", "zh-TW") else "en"]


def edition_url(settings: Settings, date: str, lang: str = "en") -> str:
    return f"{settings.site_url}{i18n.prefix(lang)}/d/{date}.html"


def review_url(settings: Settings, review: dict, lang: str = "en") -> str:
    kind = "w" if review["kind"] == "weekly" else "m"
    return f"{settings.site_url}{i18n.prefix(lang)}/{kind}/{review['id']}.html"


def edition_title(settings: Settings, edition: dict, lang: str) -> str:
    return f"{settings.site['name']}: {_edition_title(settings, edition, lang)}"


def review_title(settings: Settings, review: dict, lang: str) -> str:
    return f"{settings.site['name']}: {_review_title(settings, review, lang)}"


def story_lines(story: dict, langs: list[str]) -> tuple[list[str], list[str]]:
    """Headlines and summaries in the requested languages (summaries skip empty ones)."""
    titles = [story["title"].get(lang) or story["title"]["en"] for lang in langs]
    summaries = [story["summary"].get(lang) for lang in langs if story["summary"].get(lang)]
    if len(titles) == 2 and titles[0] == titles[1]:
        titles = titles[:1]
    return titles, summaries


def why_lines(story: dict, langs: list[str]) -> list[str]:
    """The "why it matters" note in each requested language that has one."""
    why = story.get("why") or {}
    out = [i18n.t(lang, "why_line", text=why[lang]) for lang in langs if why.get(lang)]
    return list(dict.fromkeys(out))


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


def _send_all(doc: dict, sender: str, settings: Settings, environ, now: datetime, skip_empty: bool) -> dict[str, str]:
    results: dict[str, str] = {}
    delivered = doc.setdefault("delivered", {})
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
        if skip_empty and not cfg.get("send_empty", False):
            results[name] = "skipped (empty)"
            continue
        try:
            getattr(module, sender)(doc, settings, cfg, environ)
        except Exception as exc:  # a failing channel never fails the run
            results[name] = f"error: {type(exc).__name__}: {exc}"[:240]
            continue
        delivered[name] = to_iso(now)
        results[name] = "sent"
    return results


def deliver(settings: Settings, store: Store, now: datetime | None = None, environ=None) -> dict[str, str]:
    """Send today's edition, and any review built today, to every ready channel not yet sent to."""
    environ = os.environ if environ is None else environ
    now = now or utcnow()
    today = now.astimezone(settings.tz).date()
    results: dict[str, str] = {}
    edition = store.load_edition(today.isoformat())
    if edition:
        empty = not edition.get("stories") and not edition.get("followups")
        for name, result in _send_all(edition, "send", settings, environ, now, empty).items():
            results[name] = result
        store.save_edition(edition)
    else:
        results["edition"] = f"no edition for {today.isoformat()} yet"
    for review in store.list_reviews():
        built = parse_iso(review.get("built_at"))
        if not built or built.astimezone(settings.tz).date() != today:
            continue
        for name, result in _send_all(review, "send_review", settings, environ, now, not review.get("stories")).items():
            results[f"{review['kind']} {name}"] = result
        store.save_review(review)
    return results
