"""Write bilingual headlines and summaries for selected stories, about 4 per call."""

from __future__ import annotations

from datetime import datetime

from .config import Settings
from .llm import Router
from .model import Event, Item
from .prompts import load_prompt
from .textutil import cjk_ratio, has_cjk, to_iso, truncate
from .zh import changed_chars, to_taiwan

FIELDS = ("title_en", "summary_en", "title_zh", "summary_zh")
MAX_ATTEMPTS = 3

SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, **{f: {"type": "string"} for f in FIELDS}},
                "required": ["id", *FIELDS],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}


def make_validator(batch_ids: list[str]):
    def validate(obj: dict) -> None:
        entries = obj.get("stories")
        if not isinstance(entries, list):
            raise ValueError('expected {"stories": [...]}')
        by_id: dict[str, dict] = {}
        problems: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            story_id = str(entry.get("id", "")).strip().strip("[]")
            if story_id not in batch_ids:
                continue
            values = {f: str(entry.get(f) or "").strip() for f in FIELDS}
            empty = [f for f, v in values.items() if not v]
            if empty:
                problems.append(f"story {story_id}: empty {', '.join(empty)}")
                continue
            if not (has_cjk(values["title_zh"]) and has_cjk(values["summary_zh"])):
                problems.append(f"story {story_id}: title_zh and summary_zh must be in Traditional Chinese")
                continue
            if cjk_ratio(values["title_en"] + values["summary_en"]) > 0.2:
                problems.append(f"story {story_id}: title_en and summary_en must be in English")
                continue
            by_id[story_id] = values
        if not by_id:
            raise ValueError("; ".join(problems) or "no stories returned")
        obj["_by_id"] = by_id
        obj["_problems"] = problems

    return validate


def build_message(batch: list[tuple[str, Event]], items: dict[str, Item], settings: Settings) -> str:
    chars = int(settings.llm["write_text_chars"])
    lines = [f"Stories to write ({len(batch)}):", ""]
    for bid, event in batch:
        item = items[event.rep]
        src = settings.source(item.source_id)
        others = sorted(
            {
                (settings.source(items[i].source_id).name if settings.source(items[i].source_id) else items[i].source_id)
                for i in event.item_ids
                if i in items and items[i].source_id != item.source_id
            }
        )
        date = item.published[:10] if item.published else "unknown"
        lines.append(f"[{bid}] Source: {src.name if src else item.source_id} | Published: {date}")
        lines.append(f"Original title: {item.title}")
        lines.append(f"Text: {truncate(item.snippet, chars) if item.snippet else '(no text; use the title only)'}")
        if others:
            lines.append(f"Also reported by: {', '.join(others[:6])}")
        lines.append("")
    return "\n".join(lines).strip()


def write_pending(events: dict[str, Event], items: dict[str, Item], router: Router, settings: Settings, now: datetime) -> dict:
    report = {"pending": 0, "calls": 0, "written": 0, "failed": 0, "zh_fixed_chars": 0, "skipped": 0}
    pending = [
        e
        for e in events.values()
        if e.is_live and e.selected_at and e.story is None and e.rep in items and e.write_attempts < MAX_ATTEMPTS
    ]
    pending.sort(key=lambda e: (e.selected_at or "", e.id))
    report["pending"] = len(pending)
    if not pending:
        return report
    system = load_prompt(settings.prompts_dir, "write")
    size = max(1, int(settings.llm["write_batch_size"]))
    for start in range(0, len(pending), size):
        if not router.available("write"):
            report["skipped"] += len(pending) - start
            break
        chunk = pending[start : start + size]
        batch = [(str(i + 1), e) for i, e in enumerate(chunk)]
        message = build_message(batch, items, settings)
        result = router.complete_json(
            "write", system, message, SCHEMA, "stories", make_validator([b for b, _ in batch]), max_tokens=6000
        )
        report["calls"] += 1
        for _, event in batch:
            event.write_attempts += 1
        if result is None:
            report["failed"] += len(batch)
            continue
        by_id = result.data["_by_id"]
        for bid, event in batch:
            values = by_id.get(bid)
            if not values:
                report["failed"] += 1
                continue
            title_zh, summary_zh = to_taiwan(values["title_zh"]), to_taiwan(values["summary_zh"])
            report["zh_fixed_chars"] += changed_chars(values["title_zh"], title_zh) + changed_chars(values["summary_zh"], summary_zh)
            event.story = {
                "title_en": values["title_en"],
                "summary_en": values["summary_en"],
                "title_zh": title_zh,
                "summary_zh": summary_zh,
                "by": f"{result.provider}/{result.model}",
                "at": to_iso(now),
                "item_ids": list(event.item_ids),  # later reports become follow-ups
            }
            report["written"] += 1
    return report
