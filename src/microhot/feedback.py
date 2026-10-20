"""Your ratings: 👍/👎 checkboxes in the morning GitHub issue become labels, and `microhot eval`
measures how well the thresholds match them (AIHOT's SelectBench idea, without an admin UI).

Labels: a picked story ticked 👍 is useful (1) and ticked 👎 is not (0); a near miss ticked 👍 is one
you wanted (1). Unticked boxes are no label. Edits in the last 14 days of issues are re-read each run.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

from .channels.github_issue import issue_target
from .config import TIER_RANK, Settings
from .store import Store, read_jsonl
from .textutil import to_iso, utcnow

BOX = re.compile(r"^\s*[-*] \[([ xX])\].*?<!--\s*mh:(e[0-9a-f]+):(up|down|want)\s*-->", re.MULTILINE)
LOOKBACK_DAYS = 14


def parse_labels(body: str) -> dict[str, dict]:
    """Return {event_id: {"label": 0|1, "kind": "pick"|"miss"}} from one issue body."""
    ticks: dict[str, set[str]] = {}
    for checked, event_id, which in BOX.findall(body or ""):
        if checked.strip():
            ticks.setdefault(event_id, set()).add(which)
    out = {}
    for event_id, marks in ticks.items():
        if marks == {"up"}:
            out[event_id] = {"label": 1, "kind": "pick"}
        elif marks == {"down"}:
            out[event_id] = {"label": 0, "kind": "pick"}
        elif marks == {"want"}:
            out[event_id] = {"label": 1, "kind": "miss"}
    return out


def _snapshot(edition: dict, event_id: str, settings: Settings) -> dict | None:
    for story in edition.get("stories", []) + edition.get("followups", []):
        if story["id"] == event_id:
            score = story.get("effective_score") or story.get("score")
            return {
                "score": score,
                "threshold": settings.threshold_for(story.get("tier", "T2"), story.get("beat")),
                "tier": story.get("tier"),
                "beat": story.get("beat"),
                "type": story.get("type"),
                "axes": story.get("axes"),
                "title": story["title"]["en"],
            }
    for miss in edition.get("near_misses", []):
        if miss["id"] == event_id:
            return {
                "score": miss["score"],
                "threshold": miss["threshold"],
                "tier": miss["tier"],
                "beat": miss["beat"],
                "type": miss["type"],
                "axes": miss.get("axes"),
                "title": miss["title"],
            }
    return None


def collect_labels(settings: Settings, store: Store, environ, now: datetime | None = None, client: httpx.Client | None = None) -> dict:
    """Read recent edition issues and update data/labels.jsonl. Reads the issues of DATA_REPO with DATA_TOKEN
    (or GITHUB_REPOSITORY / GITHUB_TOKEN when those are not set)."""
    now = now or utcnow()
    repo, token = issue_target(environ)
    if not token or not repo:
        return {"status": "skipped (no DATA_REPO / DATA_TOKEN or GITHUB_REPOSITORY / GITHUB_TOKEN)"}
    api = str(environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    label = settings.channels.get("github_issue", {}).get("label", "edition")
    since = to_iso(now - timedelta(days=LOOKBACK_DAYS))
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.get(
            f"{api}/repos/{repo}/issues",
            params={"labels": label, "state": "all", "since": since, "per_page": 50},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        issues = [i for i in resp.json() if "pull_request" not in i]
    finally:
        if own:
            client.close()

    existing = {row["event"]: row for row in read_jsonl(store.dir / "labels.jsonl")}
    seen_dates = set()
    updated = 0
    for issue in issues:
        match = re.search(r"<!--\s*mh-edition:(\d{4}-\d{2}-\d{2})\s*-->", issue.get("body") or "")
        if not match:
            continue
        date = match.group(1)
        seen_dates.add(date)
        edition = store.load_edition(date)
        if not edition:
            continue
        labels = parse_labels(issue.get("body") or "")
        # Boxes unticked since the last read: drop their labels for this edition.
        for event_id, row in list(existing.items()):
            if row.get("date") == date and event_id not in labels:
                del existing[event_id]
                updated += 1
        for event_id, value in labels.items():
            snap = _snapshot(edition, event_id, settings)
            if not snap:
                continue
            row = {"event": event_id, "date": date, **value, **snap, "at": to_iso(now)}
            if existing.get(event_id, {}).get("label") != row["label"] or existing.get(event_id, {}).get("kind") != row["kind"]:
                updated += 1
            existing[event_id] = row
    rows = sorted(existing.values(), key=lambda r: (r["date"], r["event"]))
    store.write_jsonl(store.dir / "labels.jsonl", rows)
    return {"status": "ok", "issues": len(issues), "editions": len(seen_dates), "labels": len(rows), "changed": updated}


def evaluate(settings: Settings, store: Store) -> dict:
    """Precision of picks, wanted near misses, and a suggested threshold per tier (max F1)."""
    rows = [r for r in read_jsonl(store.dir / "labels.jsonl") if r.get("score") is not None]
    picks = [r for r in rows if r["kind"] == "pick"]
    misses = [r for r in rows if r["kind"] == "miss"]
    useful = sum(r["label"] for r in picks)
    out = {
        "labels": len(rows),
        "picks_rated": len(picks),
        "picks_useful": useful,
        "precision": round(useful / len(picks), 3) if picks else None,
        "misses_wanted": len(misses),
        "by_type": {},
        "suggestions": {},
    }
    for kind in sorted({r.get("type") or "unknown" for r in picks}):
        group = [r for r in picks if (r.get("type") or "unknown") == kind]
        out["by_type"][kind] = {"rated": len(group), "useful": sum(r["label"] for r in group)}
    min_labels = int(settings.raw["feedback"].get("min_labels", 10))
    for tier in TIER_RANK:
        # Suggestions are for the shared [selection] thresholds, so leave out beats that set their own.
        group = [r for r in rows if r.get("tier") == tier and tier not in settings.beats.get(r.get("beat"), {}).get("thresholds", {})]
        current = settings.threshold_for(tier)
        if len(group) < min_labels:
            out["suggestions"][tier] = {"labels": len(group), "current": current, "suggested": None}
            continue
        best = None
        for threshold in range(30, 96):
            tp = sum(1 for r in group if r["score"] >= threshold and r["label"] == 1)
            fp = sum(1 for r in group if r["score"] >= threshold and r["label"] == 0)
            fn = sum(1 for r in group if r["score"] < threshold and r["label"] == 1)
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            key = (round(f1, 4), -abs(threshold - current))
            if best is None or key > best[0]:
                best = (key, threshold, precision, recall, f1)
        _, threshold, precision, recall, f1 = best
        out["suggestions"][tier] = {
            "labels": len(group),
            "current": current,
            "suggested": threshold,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        }
    return out


def eval_lines(result: dict) -> list[str]:
    lines = [f"labels: {result['labels']} ({result['picks_rated']} rated picks, {result['misses_wanted']} wanted near misses)"]
    if result["precision"] is not None:
        lines.append(f"precision of picks: {result['precision']:.0%} ({result['picks_useful']}/{result['picks_rated']} useful)")
    for kind, row in result["by_type"].items():
        lines.append(f"  {kind}: {row['useful']}/{row['rated']} useful")
    for tier, row in result["suggestions"].items():
        if row["suggested"] is None:
            lines.append(f"{tier}: {row['labels']} labels, need more for a suggestion (threshold now {row['current']})")
        else:
            lines.append(
                f"{tier}: suggested threshold {row['suggested']} (now {row['current']}), "
                f"precision {row['precision']:.0%}, recall {row['recall']:.0%} on {row['labels']} labels"
            )
    return lines
