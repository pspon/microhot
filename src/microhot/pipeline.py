"""One scheduled run: fetch → cluster → triage → select → write → overviews → edition → reviews
→ alerts → save → render."""

from __future__ import annotations

import os
import time
from datetime import datetime

from . import __version__
from .alerts import detect as detect_alerts
from .archive import update_log
from .cluster import assign_events, get_embedder, roll_heat, update_heat
from .config import Settings
from .digest import digest_pending
from .edition import build_edition, edition_due
from .fetch import fetch_all
from .llm import Router, UsageLog
from .llm.adapters import call
from .markdown import bilingual, edition_markdown, review_markdown
from .render import SiteBuilder
from .reviews import build_due_reviews
from .select import select_events
from .store import Store
from .textutil import to_iso, utcnow
from .topics import apply_adjustments, load_topics, validate_rules
from .triage import triage_pending
from .write import write_pending


def run(
    settings: Settings,
    *,
    now: datetime | None = None,
    use_llm: bool = True,
    environ=None,
    http_client=None,
    llm_adapter=None,
    llm_client=None,
    sleep=None,
) -> dict:
    environ = os.environ if environ is None else environ
    now = now or utcnow()
    clock = time.monotonic()
    store = Store(settings.data_dir, settings.tz)
    keep_days = int(settings.retention["items_days"])
    items = store.load_items(now, keep_days)
    events = store.load_events()
    state = store.load_state()
    topics = load_topics(settings)
    validate_rules(settings, topics)
    report: dict = {
        "started": to_iso(now),
        "version": __version__,
        "counts": {},
        "errors": [],
        "fetch": [],
        "cluster": {},
        "triage": {},
        "write": {},
        "digest": {},
        "llm": {},
        "edition": None,
        "reviews": [],
        "alerts": 0,
        "log": {},
        "site": {},
    }

    # 1. Fetch feeds (rules, mute list and de-duplication only; no tokens).
    new_items, stats = fetch_all(settings, state["feeds"], items, now, client=http_client)
    for item in new_items:
        items[item.id] = item
    report["fetch"] = [s.to_dict() for s in stats]
    report["errors"] += [f"fetch {s.source_id}: {s.error}" for s in stats if s.status == "error"]

    # 2. Group into events, heat and boost rules (no tokens).
    embedder, threshold, warning = get_embedder(settings)
    if warning:
        report["errors"].append(warning)
    report["cluster"] = {**assign_events(new_items, items, events, settings, now, embedder, threshold), "embedder": embedder.name}
    roll_heat(events, now)
    update_heat(events, items, settings, now)
    report["cluster"]["boosted"] = apply_adjustments(events, items, settings, topics)

    # 3. Model steps, through the fallback chain.
    router = None
    if use_llm:
        router = Router(
            settings,
            UsageLog(store.usage_dir, settings.tz),
            now,
            adapter=llm_adapter or call,
            client=llm_client,
            sleep=sleep or time.sleep,
            environ=environ,
        )
        if router.available("triage"):
            report["triage"] = triage_pending(events, items, router, settings, now, seed=int(now.timestamp()))
            update_heat(events, items, settings, now)
            apply_adjustments(events, items, settings, topics)
        else:
            report["triage"] = {"note": "no triage provider configured"}
    selected = select_events(events, items, settings, now)
    if router and router.available("write"):
        report["write"] = write_pending(events, items, router, settings, now)
    elif router:
        report["write"] = {"note": "no write provider available"}
    if router and router.available("digest"):
        report["digest"] = digest_pending(events, items, router, settings, now)

    # 4. The morning edition and the weekly/monthly reviews (rule-built; reviews add one call).
    date = edition_due(settings, state, now)
    if date:
        edition = build_edition(date, events, items, settings, state, now, topics)
        store.save_edition(edition)
        md = bilingual(edition_markdown(edition, settings, "en"), edition_markdown(edition, settings, "zh-TW"))
        store.write_text(f"digest/{date}.md", md)
        store.write_text("digest/latest.md", md)
        report["edition"] = {"date": date, "stories": len(edition["stories"]), "followups": len(edition["followups"])}
    for review in build_due_reviews(store, settings, state, router, now):
        store.write_text(
            f"digest/{review['id']}.md",
            bilingual(review_markdown(review, settings, "en"), review_markdown(review, settings, "zh-TW")),
        )
        report["reviews"].append({"id": review["id"], "stories": len(review["stories"]), "overview": bool(review["overview"])})
    report["llm"] = router.summary() if router else {"active": {"triage": [], "write": []}, "note": "model steps disabled"}

    live = [e for e in events.values() if e.is_live]
    report["counts"] = {
        "new_items": len(new_items),
        "items": len(items),
        "events": len(live),
        "selected": len(selected),
        "written": int(report["write"].get("written", 0)) if isinstance(report["write"], dict) else 0,
        "digests": int(report["digest"].get("written", 0)) if isinstance(report["digest"], dict) else 0,
        "awaiting_summary": sum(1 for e in live if e.selected_at and not e.story),
    }
    report["alerts"] = detect_alerts(settings, state, report, now)

    # 5. Save state (the story log first, while every story's reports are still loaded), then render.
    report["log"] = update_log(store, events, items, settings, topics, now)
    store.save_items(items)
    store.prune_items(now, keep_days)
    store.save_events(events, now, int(settings.retention["events_days"]))
    store.save_state(state)
    report["seconds"] = round(time.monotonic() - clock, 1)
    store.save_run(report)
    try:
        report["site"] = SiteBuilder(settings, store, events, items, now, run_summary=report, topics=topics).build()
    except Exception as exc:  # keep the data even if rendering breaks
        report["errors"].append(f"render: {type(exc).__name__} (details in the private data repo)")
        report["site"] = {"error": str(exc)}
    report["seconds"] = round(time.monotonic() - clock, 1)
    store.update_latest_run(report)
    return report


def summary_lines(report: dict) -> list[str]:
    counts = report.get("counts", {})
    llm = report.get("llm", {})
    fetch = report.get("fetch", [])
    ok_feeds = sum(1 for f in fetch if f["status"] in ("ok", "not_modified", "baseline"))
    active = llm.get("active", {})
    lines = [
        f"MicroHOT run {report.get('started')} ({report.get('seconds', '?')} s)",
        (
            f"feeds: {ok_feeds}/{len(fetch)} ok, {counts.get('new_items', 0)} new items, "
            f"{report.get('cluster', {}).get('created', 0)} new events"
        ),
        (
            f"models: triage via {', '.join(active.get('triage', [])) or 'none'}; "
            f"write via {', '.join(active.get('write', [])) or 'none'}; "
            f"{llm.get('calls', 0)} calls, ${llm.get('usd', 0):.4f}"
        ),
        (
            f"picked {counts.get('selected', 0)}, written {counts.get('written', 0)}, "
            f"overviews {counts.get('digests', 0)}, awaiting summary {counts.get('awaiting_summary', 0)}"
        ),
    ]
    if llm.get("unconfigured"):
        lines.append("not configured (never called): " + ", ".join(sorted(llm["unconfigured"])))
    if llm.get("disabled"):
        lines.append("turned off this run: " + ", ".join(f"{k} ({v})" for k, v in llm["disabled"].items()))
    if report.get("edition"):
        ed = report["edition"]
        lines.append(f"edition {ed['date']}: {ed['stories']} stories, {ed.get('followups', 0)} follow-ups")
    for review in report.get("reviews", []):
        lines.append(f"review {review['id']}: {review['stories']} stories, overview {'yes' if review['overview'] else 'no'}")
    if report.get("alerts"):
        lines.append(f"alerts queued: {report['alerts']}")
    for err in report.get("errors", [])[:15]:
        lines.append(f"! {err}")
    return lines
