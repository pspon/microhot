"""One scheduled run: fetch → cluster → triage → select → write → edition → save → render."""

from __future__ import annotations

import os
import time
from datetime import datetime

from . import __version__
from .cluster import assign_events, get_embedder, update_heat
from .config import Settings
from .edition import build_edition, edition_due
from .fetch import fetch_all
from .llm import Router, UsageLog
from .llm.adapters import call
from .render import SiteBuilder
from .select import select_events
from .store import Store
from .textutil import to_iso, utcnow
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
    report: dict = {
        "started": to_iso(now),
        "version": __version__,
        "counts": {},
        "errors": [],
        "fetch": [],
        "cluster": {},
        "triage": {},
        "write": {},
        "llm": {},
        "edition": None,
        "site": {},
    }

    # 1. Fetch feeds (rules and de-duplication only; no tokens).
    new_items, stats = fetch_all(settings, state["feeds"], items, now, client=http_client)
    for item in new_items:
        items[item.id] = item
    report["fetch"] = [s.to_dict() for s in stats]
    report["errors"] += [f"fetch {s.source_id}: {s.error}" for s in stats if s.status == "error"]

    # 2. Group into events and compute heat (no tokens).
    embedder, threshold, warning = get_embedder(settings)
    if warning:
        report["errors"].append(warning)
    report["cluster"] = {**assign_events(new_items, items, events, settings, now, embedder, threshold), "embedder": embedder.name}
    update_heat(events, items, settings, now)

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
        else:
            report["triage"] = {"note": "no triage provider configured"}
    selected = select_events(events, items, settings, now)
    if router and router.available("write"):
        report["write"] = write_pending(events, items, router, settings, now)
    elif router:
        report["write"] = {"note": "no write provider available"}
    report["llm"] = router.summary() if router else {"active": {"triage": [], "write": []}, "note": "model steps disabled"}

    # 4. The morning edition (rule-built, no tokens).
    date = edition_due(settings, state, now)
    if date:
        edition = build_edition(date, events, items, settings, state, now)
        store.save_edition(edition)
        report["edition"] = {"date": date, "stories": len(edition["stories"])}

    live = [e for e in events.values() if e.is_live]
    report["counts"] = {
        "new_items": len(new_items),
        "items": len(items),
        "events": len(live),
        "selected": len(selected),
        "written": int(report["write"].get("written", 0)) if isinstance(report["write"], dict) else 0,
        "awaiting_summary": sum(1 for e in live if e.selected_at and not e.story),
    }

    # 5. Save state, then render the site.
    store.save_items(items)
    store.prune_items(now, keep_days)
    store.save_events(events, now, int(settings.retention["events_days"]))
    store.save_state(state)
    report["seconds"] = round(time.monotonic() - clock, 1)
    store.save_run(report)
    try:
        report["site"] = SiteBuilder(settings, store, events, items, now, run_summary=report).build()
    except Exception as exc:  # keep the data even if rendering breaks
        report["errors"].append(f"render: {type(exc).__name__}: {exc}")
        report["site"] = {"error": str(exc)}
    report["seconds"] = round(time.monotonic() - clock, 1)
    store.update_latest_run(report)
    return report


def summary_lines(report: dict) -> list[str]:
    counts = report.get("counts", {})
    llm = report.get("llm", {})
    fetch = report.get("fetch", [])
    lines = [
        f"MicroHOT run {report.get('started')} ({report.get('seconds', '?')} s)",
        f"feeds: {sum(1 for f in fetch if f['status'] in ('ok', 'not_modified'))}/{len(fetch)} ok, "
        f"{counts.get('new_items', 0)} new items, {report.get('cluster', {}).get('created', 0)} new events",
        f"models: triage via {', '.join(llm.get('active', {}).get('triage', [])) or 'none'}; "
        f"write via {', '.join(llm.get('active', {}).get('write', [])) or 'none'}; "
        f"{llm.get('calls', 0)} calls, ${llm.get('usd', 0):.4f}",
        f"picked {counts.get('selected', 0)}, written {counts.get('written', 0)}, "
        f"awaiting summary {counts.get('awaiting_summary', 0)}",
    ]
    if llm.get("unconfigured"):
        lines.append("not configured (never called): " + ", ".join(sorted(llm["unconfigured"])))
    if llm.get("disabled"):
        lines.append("turned off this run: " + ", ".join(f"{k} ({v})" for k, v in llm["disabled"].items()))
    if report.get("edition"):
        lines.append(f"edition {report['edition']['date']}: {report['edition']['stories']} stories")
    for err in report.get("errors", [])[:15]:
        lines.append(f"! {err}")
    return lines
