"""Batched triage: about 20 events per call, scored on AIHOT's five-axis rubric.

The model returns five integer axes per item; the 0–100 score is computed here from
per-type weights, so the arithmetic is exact and the axes stay available for calibration.
"""

from __future__ import annotations

import random
from datetime import datetime

from .cluster import merge_events, resolve
from .config import Settings
from .llm import Router
from .model import Event, Item
from .prompts import load_prompt
from .select import is_candidate, threshold_for_event
from .textutil import to_iso, truncate

AXES = ("sig", "nov", "cred", "res", "act")
# Weights per content type for (sig, nov, cred, res, act); each row sums to 10 → scores 0–100.
WEIGHTS: dict[str, tuple[int, int, int, int, int]] = {
    "model_release": (3, 2, 2, 2, 1),
    "product_launch": (2, 2, 1, 2, 3),
    "tool_or_prompt": (1, 2, 1, 2, 4),
    "research_paper": (5, 3, 1, 0, 1),
    "industry_event": (3, 1, 2, 4, 0),
    "opinion_analysis": (1, 3, 1, 4, 1),
    "tutorial_explainer": (1, 1, 1, 3, 4),
    "policy_action": (3, 1, 2, 2, 2),
    "governance_analysis": (2, 3, 2, 2, 1),
    "off_topic": (0, 0, 0, 0, 0),
}
GOV_TYPES = {"policy_action", "governance_analysis"}
MAX_TRIES = 3

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": list(WEIGHTS)},
                    "beat": {"type": "string", "enum": ["ai", "gov"]},
                    "sig": {"type": "integer"},
                    "nov": {"type": "integer"},
                    "cred": {"type": "integer"},
                    "res": {"type": "integer"},
                    "act": {"type": "integer"},
                    "dup": {"type": "string"},
                },
                "required": ["id", "type", "beat", "sig", "nov", "cred", "res", "act", "dup"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def compute_score(kind: str, axes: dict[str, int]) -> int:
    weights = WEIGHTS.get(kind, WEIGHTS["opinion_analysis"])
    return int(sum(w * axes[a] for w, a in zip(weights, AXES)))


def _clamp(value) -> int:
    if isinstance(value, str):
        value = value.strip()
        value = int(float(value)) if value.replace(".", "", 1).isdigit() else 0
    return max(0, min(10, int(round(float(value)))))


def make_validator(batch_ids: list[str]):
    def validate(obj: dict) -> None:
        entries = obj.get("items")
        if not isinstance(entries, list):
            raise ValueError('expected {"items": [...]}')
        by_id: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip().strip("[]")
            if item_id not in batch_ids:
                continue
            kind = entry.get("type")
            if kind not in WEIGHTS:
                raise ValueError(f"item {item_id}: unknown type {kind!r}")
            try:
                axes = {a: _clamp(entry.get(a, 0)) for a in AXES}
            except (TypeError, ValueError):
                raise ValueError(f"item {item_id}: axes must be integers 0-10") from None
            by_id[item_id] = {
                "type": kind,
                "beat": entry.get("beat") if entry.get("beat") in ("ai", "gov") else None,
                "axes": axes,
                "dup": str(entry.get("dup") or "").strip().strip("[]"),
            }
        missing = [i for i in batch_ids if i not in by_id]
        if len(missing) > max(1, len(batch_ids) // 5):
            raise ValueError(f"missing ids: {', '.join(missing[:12])}")
        obj["_by_id"] = by_id

    return validate


def _recent_for_prompt(events: dict[str, Event], items: dict[str, Item], exclude: set[str], limit: int) -> list[Event]:
    recent = [
        e
        for e in events.values()
        if e.is_live and e.id not in exclude and e.score is not None and e.type != "off_topic" and e.rep in items
    ]
    recent.sort(key=lambda e: (e.selected_at is None, -e.heat, -(e.score or 0)))
    return recent[:limit]


def build_message(
    batch: list[tuple[str, Event]],
    recent: list[tuple[str, Event]],
    items: dict[str, Item],
    settings: Settings,
    now: datetime,
) -> str:
    chars = int(settings.llm["triage_text_chars"])
    local = now.astimezone(settings.tz)
    lines = [f"Today: {local:%Y-%m-%d} ({settings.site['timezone']})", ""]
    if recent:
        lines.append("Already covered (for duplicate detection only; do not score these):")
        for rid, event in recent:
            title = (event.story or {}).get("title_en") or items[event.rep].title
            lines.append(f"{rid}: {title}")
        lines.append("")
    lines.append(f"Items to score ({len(batch)}):")
    for bid, event in batch:
        item = items[event.rep]
        date = item.published[:10] if item.published else "unknown"
        lines.append(f"[{bid}] published {date}")
        lines.append(f"Title: {item.title}")
        if item.snippet:
            lines.append(f"Text: {truncate(item.snippet, chars)}")
        lines.append("")
    return "\n".join(lines).strip()


def _apply(event: Event, result: dict, provider: str, settings: Settings, items: dict[str, Item], now: datetime) -> None:
    axes = result["axes"]
    event.type = result["type"]
    event.axes = axes
    event.score = compute_score(event.type, axes)
    event.opinions = 1
    event.triaged_at = to_iso(now)
    event.triaged_by = provider
    if event.type in GOV_TYPES:
        event.beat = "gov"
    elif result["beat"] in ("ai", "gov"):
        event.beat = result["beat"]


def _run_batches(
    events_to_score: list[Event],
    events: dict[str, Event],
    items: dict[str, Item],
    router: Router,
    settings: Settings,
    now: datetime,
    rng: random.Random,
    second: bool,
    report: dict,
) -> list[Event]:
    system = load_prompt(settings.prompts_dir, "triage")
    size = max(1, int(settings.llm["triage_batch_size"]))
    done: list[Event] = []
    for start in range(0, len(events_to_score), size):
        if not router.available("triage"):
            report["skipped"] += len(events_to_score) - start
            break
        chunk = events_to_score[start : start + size]
        rng.shuffle(chunk)
        batch = [(str(i + 1), e) for i, e in enumerate(chunk)]
        exclude = {e.id for e in chunk}
        recent_events = [] if second else _recent_for_prompt(events, items, exclude, int(settings.llm["recent_events_in_prompt"]))
        recent = [(f"E{i + 1}", e) for i, e in enumerate(recent_events)]
        message = build_message(batch, recent, items, settings, now)
        result = router.complete_json(
            "triage", system, message, SCHEMA, "triage", make_validator([b for b, _ in batch]), max_tokens=6000
        )
        report["calls"] += 1
        if result is None:
            for _, event in batch:
                event.triage_tries += 1
            report["failed"] += len(batch)
            continue
        by_id = result.data["_by_id"]
        provider = f"{result.provider}/{result.model}"
        id_to_event = dict(batch)
        recent_map = dict(recent)
        for bid, event in batch:
            entry = by_id.get(bid)
            if entry is None:
                event.triage_tries += 1
                continue
            if second:
                first_score = event.score or 0
                second_score = compute_score(entry["type"], entry["axes"])
                event.score = int(round((first_score + second_score) / 2))
                event.opinions = 2
                done.append(event)
                continue
            _apply(event, entry, provider, settings, items, now)
            done.append(event)
            report["scored"] += 1
        if second:
            continue
        # Duplicate hints: fold a later item into the event it duplicates.
        for bid, event in batch:
            entry = by_id.get(bid)
            if not entry or not entry["dup"] or entry["dup"] == bid:
                continue
            target = recent_map.get(entry["dup"]) or (id_to_event.get(entry["dup"]) if entry["dup"].isdigit() and int(entry["dup"]) < int(bid) else None)
            target = resolve(events, target.id) if target else None
            if target and target.id != event.id and event.is_live:
                merge_events(target, event, items)
                report["merged"] += 1
    return done


def triage_pending(
    events: dict[str, Event],
    items: dict[str, Item],
    router: Router,
    settings: Settings,
    now: datetime,
    seed: int = 0,
) -> dict:
    report = {"pending": 0, "calls": 0, "scored": 0, "second_opinions": 0, "merged": 0, "failed": 0, "skipped": 0}
    pending = [
        e
        for e in events.values()
        if e.is_live and e.score is None and e.rep in items and e.triage_tries < MAX_TRIES and is_candidate(e, settings, now)
    ]
    pending.sort(key=lambda e: (-e.heat, e.created))
    report["pending"] = len(pending)
    if not pending:
        return report
    rng = random.Random(seed)
    scored = _run_batches(pending, events, items, router, settings, now, rng, False, report)

    if settings.selection.get("second_opinion", True) and router.available("triage"):
        margin = int(settings.selection["borderline"])
        borderline = [
            e
            for e in scored
            if e.is_live and e.type != "off_topic" and e.opinions == 1 and e.score is not None
            and abs(e.score - threshold_for_event(e, items, settings)) <= margin
        ]
        if borderline:
            again = _run_batches(borderline, events, items, router, settings, now, rng, True, report)
            report["second_opinions"] = len(again)
    return report
