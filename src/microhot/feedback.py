"""Your ratings: 👍/👎 checkboxes in the morning GitHub issue become labels, and `microhot eval` and the
About page turn them into suggestions for each beat's thresholds and for topic boosts.

Labels: a pick ticked 👍 is useful (1) and ticked 👎 is not (0). A story that was not picked, ticked 👍
"wanted", should have been in (1); ticked 👎 "rightly skipped", it should have stayed out (0). Unticked
boxes are no label. Edits in the last 14 days of issues are re-read each run.

Suggestions are worked out against the current config, so one you apply stops being made:
- per beat, one shift for all its tiers (pooling the tiers needs far fewer ratings): the shift that best
  separates what you wanted from what you did not (max F1; the smallest change on ties). It needs
  `min_labels` ratings in the beat, at least 3 of them 👍 and 3 👎;
- then, on top of the beat shifts, boost points for up to three topics, one at a time, each only when it
  improves that separation.
Nothing changes on its own: the suggestions come as lines to paste into config/site.toml.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

from .channels.github_issue import issue_target
from .config import TIER_RANK, Settings
from .store import Store, read_jsonl
from .textutil import to_iso, utcnow

BOX = re.compile(r"^\s*[-*] \[([ xX])\].*?<!--\s*mh:(e[0-9a-f]+):(up|down|want|skip)\s*-->", re.MULTILINE)
MARKS = {
    frozenset({"up"}): (1, "pick"),
    frozenset({"down"}): (0, "pick"),
    frozenset({"want"}): (1, "miss"),
    frozenset({"skip"}): (0, "miss"),
}
LOOKBACK_DAYS = 14
MIN_EACH = 3  # a beat suggestion needs at least this many 👍 and this many 👎
MAX_SHIFT = 20  # a suggestion moves a bar, or a topic's boost, by at most this many points
MIN_TOPIC_POINTS = 3  # smaller boost changes are noise
MIN_TOPIC_GAIN = 0.02  # how much a topic boost must improve F1 to be suggested
MAX_TOPICS = 3


def parse_labels(body: str) -> dict[str, dict]:
    """Return {event_id: {"label": 0|1, "kind": "pick"|"miss"}} from one issue body. Both boxes of one
    story ticked is no label."""
    ticks: dict[str, set[str]] = {}
    for checked, event_id, which in BOX.findall(body or ""):
        if checked.strip():
            ticks.setdefault(event_id, set()).add(which)
    out = {}
    for event_id, marks in ticks.items():
        hit = MARKS.get(frozenset(marks))
        if hit:
            out[event_id] = {"label": hit[0], "kind": hit[1]}
    return out


def _snapshot(edition: dict, event_id: str, settings: Settings) -> dict | None:
    for story in edition.get("stories", []) + edition.get("followups", []):
        if story["id"] == event_id:
            return {
                "score": story.get("effective_score") or story.get("score"),
                "raw": story.get("score"),
                "boost": story.get("boost", 0),
                "boost_why": list(story.get("boost_why") or []),
                "threshold": settings.threshold_for(story.get("tier", "T2"), story.get("beat")),
                "tier": story.get("tier"),
                "beat": story.get("beat"),
                "type": story.get("type"),
                "axes": story.get("axes"),
                "title": story["title"]["en"],
                "topics": [t["id"] for t in story.get("topics", [])],
            }
    # "near_misses": editions built before the unpicked section
    for row in edition.get("unpicked", []) + edition.get("near_misses", []):
        if row["id"] == event_id:
            return {
                "score": row["score"],
                "raw": row.get("raw"),
                "boost": row.get("boost", 0),
                "boost_why": list(row.get("boost_why") or []),
                "threshold": row["threshold"],
                "tier": row["tier"],
                "beat": row["beat"],
                "type": row["type"],
                "axes": row.get("axes"),
                "title": row["title"],
                "topics": list(row.get("topics") or []),
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


# ---- evaluation --------------------------------------------------------------------------------


def topic_points(settings: Settings) -> dict[str, int]:
    """Boost points each topic gets from the current [adjust] rules."""
    points: dict[str, int] = {}
    for rule in settings.raw.get("adjust", {}).get("boost", []):
        if "topic" in rule:
            points[rule["topic"]] = points.get(rule["topic"], 0) + int(rule.get("points", 0))
    return points


def _boost_now(row: dict, settings: Settings) -> int:
    """Boost points the rated story would get under the current rules: topic rules from its topics,
    pattern rules when they matched it at the time."""
    adjust = settings.raw.get("adjust", {})
    cap = int(adjust.get("max_points", 20))
    topics = set(row.get("topics") or [])
    fired = set(row.get("boost_why") or [])
    points = 0
    for rule in adjust.get("boost", []):
        if "topic" in rule:
            points += int(rule.get("points", 0)) if rule["topic"] in topics else 0
        elif "pattern" in rule and (rule.get("label") or rule["pattern"]) in fired:
            points += int(rule.get("points", 0))
    return max(-cap, min(cap, points))


def margin(row: dict, settings: Settings) -> int | None:
    """How far above (+) or below (−) its bar a rated story is under the current config."""
    if row.get("score") is None or not row.get("tier"):
        return None
    raw = row.get("raw")
    if raw is None:  # ratings saved before raw scores were kept
        raw = int(row["score"]) - int(row.get("boost") or 0)
    return int(raw) + _boost_now(row, settings) - settings.threshold_for(row["tier"], row.get("beat"))


def _counts(pairs, shift: int = 0) -> tuple[int, int, int]:
    """True positives, false positives and false negatives of "in when margin >= shift"."""
    tp = fp = fn = 0
    for value, label in pairs:
        inside = value >= shift
        if label:
            tp += inside
            fn += not inside
        else:
            fp += inside
    return tp, fp, fn


def _scores(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return f1, precision, recall


def _f1(pairs, shift: int = 0) -> tuple[float, float, float]:
    """F1, precision and recall of "in when margin >= shift" against the labels."""
    return _scores(*_counts(pairs, shift))


def _best_shift(pairs: list[tuple[int, int]]) -> dict:
    best = None
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        f1, precision, recall = _f1(pairs, shift)
        key = (round(f1, 4), -abs(shift))
        if best is None or key > best[0]:
            best = (key, shift, f1, precision, recall)
    _, shift, f1, precision, recall = best
    return {"shift": shift, "f1": round(f1, 3), "precision": round(precision, 3), "recall": round(recall, 3)}


def thresholds_toml(values: dict[str, int]) -> str:
    """The line to paste under a [beats.*] table, e.g. thresholds = { T1 = 51, "T1.5" = 56, T2 = 64 }."""
    parts = [f'"{tier}" = {value}' if "." in tier else f"{tier} = {value}" for tier, value in values.items()]
    return "thresholds = { " + ", ".join(parts) + " }"


def _beat_entries(rated: list[dict], settings: Settings, min_labels: int) -> list[dict]:
    entries = []
    for beat in settings.beat_order:
        mine = [r for r in rated if r.get("beat") == beat]
        if not mine:
            continue
        wanted = sum(r["label"] for r in mine)
        current = {tier: settings.threshold_for(tier, beat) for tier in TIER_RANK}
        quota_cut = sum(
            1
            for r in mine
            if r["kind"] == "miss" and r["label"] == 1 and r.get("threshold") is not None and r["score"] >= r["threshold"]
        )
        entry = {
            "beat": beat,
            "labels": len(mine),
            "wanted": wanted,
            "unwanted": len(mine) - wanted,
            "ready": len(mine) >= min_labels and wanted >= MIN_EACH and len(mine) - wanted >= MIN_EACH,
            "shift": 0,
            "current": current,
            "suggested": None,
            "toml": "",
            "quota": int(settings.beats.get(beat, {}).get("daily_quota", 5)),
            "quota_cut": quota_cut,
        }
        if entry["ready"]:
            entry.update(_best_shift([(r["_margin"], r["label"]) for r in mine]))
            if entry["shift"]:
                entry["suggested"] = {tier: value + entry["shift"] for tier, value in current.items()}
                entry["toml"] = thresholds_toml(entry["suggested"])
        entries.append(entry)
    return entries


def _topic_entries(rated: list[dict], settings: Settings, beats: list[dict], min_labels: int) -> list[dict]:
    """Greedy: the topic boost that most improves F1 on top of the beat shifts, then the next."""
    wanted = sum(r["label"] for r in rated)
    if len(rated) < min_labels or wanted < MIN_EACH or len(rated) - wanted < MIN_EACH:
        return []
    shift = {b["beat"]: b["shift"] for b in beats if b["ready"]}
    rows = [[r["_margin"] - shift.get(r.get("beat"), 0), r["label"], set(r.get("topics") or [])] for r in rated]
    f1_now = _f1([(m, y) for m, y, _ in rows])[0]
    min_rows = max(5, min_labels // 2)
    current = topic_points(settings)
    cap = int(settings.raw.get("adjust", {}).get("max_points", 20))
    out: list[dict] = []
    for _ in range(MAX_TOPICS):
        best = None
        for topic in sorted({t for _, _, ts in rows for t in ts} - {e["topic"] for e in out}):
            inside = [(m, y) for m, y, ts in rows if topic in ts]
            n = len(inside)
            if n < min_rows:
                continue
            rest = _counts([(m, y) for m, y, ts in rows if topic not in ts])  # unchanged by this topic's boost
            for change in range(-MAX_SHIFT, MAX_SHIFT + 1):
                if abs(change) < MIN_TOPIC_POINTS:
                    continue
                tp, fp, fn = _counts(inside, -change)  # margin + change >= 0
                f1 = _scores(tp + rest[0], fp + rest[1], fn + rest[2])[0]
                key = (round(f1, 4), -abs(change), n, topic)
                if best is None or key > best[0]:
                    best = (key, topic, change, f1, n)
        if best is None or best[3] - f1_now < MIN_TOPIC_GAIN:
            break
        _, topic, change, f1, n = best
        for row in rows:
            if topic in row[2]:
                row[0] += change
        points = max(-cap, min(cap, current.get(topic, 0) + change))
        out.append({
            "topic": topic,
            "labels": n,
            "change": change,
            "current": current.get(topic, 0),
            "points": points,
            "f1_before": round(f1_now, 3),
            "f1_after": round(f1, 3),
            "toml": f'{{ topic = "{topic}", points = {points} }},',
        })
        f1_now = f1
    return out


def evaluate(settings: Settings, store: Store) -> dict:
    """Precision of picks, how the unpicked stories were rated, and suggestions per beat and topic."""
    rows = [r for r in read_jsonl(store.dir / "labels.jsonl") if r.get("score") is not None]
    picks = [r for r in rows if r["kind"] == "pick"]
    misses = [r for r in rows if r["kind"] == "miss"]
    useful = sum(r["label"] for r in picks)
    min_labels = int(settings.feedback.get("min_labels", 10))
    rated = []
    for row in rows:
        value = margin(row, settings)
        if value is not None and row.get("beat"):
            rated.append({**row, "_margin": value})
    beats = _beat_entries(rated, settings, min_labels)
    out = {
        "labels": len(rows),
        "picks_rated": len(picks),
        "picks_useful": useful,
        "precision": round(useful / len(picks), 3) if picks else None,
        "misses_rated": len(misses),
        "misses_wanted": sum(r["label"] for r in misses),
        "misses_skipped": sum(1 for r in misses if not r["label"]),
        "by_type": {},
        "beats": beats,
        "topics": _topic_entries(rated, settings, beats, min_labels),
        "min_labels": min_labels,
        "min_each": MIN_EACH,
    }
    for kind in sorted({r.get("type") or "unknown" for r in picks}):
        group = [r for r in picks if (r.get("type") or "unknown") == kind]
        out["by_type"][kind] = {"rated": len(group), "useful": sum(r["label"] for r in group)}
    return out


def eval_lines(result: dict) -> list[str]:
    lines = [f"labels: {result['labels']} ({result['picks_rated']} rated picks, {result.get('misses_rated', 0)} rated stories not picked)"]
    if result["precision"] is not None:
        lines.append(f"precision of picks: {result['precision']:.0%} ({result['picks_useful']}/{result['picks_rated']} useful)")
    if result.get("misses_rated"):
        lines.append(f"not picked: {result['misses_wanted']} wanted, {result['misses_skipped']} rightly skipped")
    for kind, row in result["by_type"].items():
        lines.append(f"  {kind}: {row['useful']}/{row['rated']} useful")
    for beat in result.get("beats", []):
        head = f"beat {beat['beat']}: {beat['labels']} ratings ({beat['wanted']} 👍, {beat['unwanted']} 👎)"
        if not beat["ready"]:
            lines.append(f"{head}; a suggestion needs {result['min_labels']}, with at least {result['min_each']} of each")
        elif not beat["shift"]:
            lines.append(f"{head}; thresholds look right (precision {beat['precision']:.0%}, recall {beat['recall']:.0%})")
        else:
            verb = "raise" if beat["shift"] > 0 else "lower"
            lines.append(f"{head}; {verb} its thresholds by {abs(beat['shift'])} (precision {beat['precision']:.0%}, recall {beat['recall']:.0%}):")
            lines.append(f"  [beats.{beat['beat']}] {beat['toml']}")
        if beat["quota_cut"] >= 2:
            lines.append(f"  {beat['quota_cut']} stories you wanted cleared the bar after the day's {beat['quota']} picks: consider a higher daily_quota")
    if result.get("topics"):
        lines.append("topic boosts, on top of the beat changes ([adjust] boost = [...]):")
        for topic in result["topics"]:
            lines.append(
                f"  {topic['toml']}  # {topic['change']:+d} on {topic['current']}, {topic['labels']} ratings, "
                f"F1 {topic['f1_before']:.0%} → {topic['f1_after']:.0%}"
            )
    return lines
