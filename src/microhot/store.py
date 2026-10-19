"""JSON state kept on the `data` branch (checked out at ./data in Actions).

Layout:
  items/YYYY-MM-DD.jsonl   fetched items, partitioned by local fetch date
  events.jsonl             rolling window of events (stories), one per line
  editions/YYYY-MM-DD.json daily editions (immutable snapshots, plus delivery status)
  state.json               feed state (ETag, first import), edition bookkeeping
  usage/YYYY-MM.jsonl      one line per model call
  runs/latest.json         last run report; runs/YYYY-MM.jsonl keeps history
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .model import Event, Item
from .textutil import parse_iso


def write_json(path: Path, obj, *, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


class Store:
    def __init__(self, data_dir: Path, tz: ZoneInfo) -> None:
        self.dir = Path(data_dir)
        self.tz = tz
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- items ---------------------------------------------------------------------------
    def _partition(self, item: Item) -> str:
        fetched = parse_iso(item.fetched)
        assert fetched is not None, f"item {item.id} has no fetch time"
        return fetched.astimezone(self.tz).date().isoformat()

    def load_items(self, now: datetime, days: int) -> dict[str, Item]:
        oldest = (now.astimezone(self.tz) - timedelta(days=days)).date().isoformat()
        items: dict[str, Item] = {}
        folder = self.dir / "items"
        if not folder.exists():
            return items
        for path in sorted(folder.glob("*.jsonl")):
            if path.stem < oldest:
                continue
            for row in read_jsonl(path):
                item = Item.from_dict(row)
                items[item.id] = item
        return items

    def save_items(self, items: dict[str, Item]) -> None:
        groups: dict[str, list[Item]] = {}
        for item in items.values():
            groups.setdefault(self._partition(item), []).append(item)
        folder = self.dir / "items"
        folder.mkdir(parents=True, exist_ok=True)
        for day, rows in groups.items():
            rows.sort(key=lambda it: (it.fetched, it.id))
            path = folder / f"{day}.jsonl"
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".jsonl")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in rows:
                    fh.write(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp, path)

    def prune_items(self, now: datetime, keep_days: int) -> int:
        oldest = (now.astimezone(self.tz) - timedelta(days=keep_days)).date().isoformat()
        removed = 0
        for path in (self.dir / "items").glob("*.jsonl"):
            if path.stem < oldest:
                path.unlink()
                removed += 1
        return removed

    # ---- events --------------------------------------------------------------------------
    def load_events(self) -> dict[str, Event]:
        rows = read_jsonl(self.dir / "events.jsonl")
        return {row["id"]: Event.from_dict(row) for row in rows}

    def save_events(self, events: dict[str, Event], now: datetime, keep_days: int) -> None:
        cutoff = now - timedelta(days=keep_days)
        kept = []
        for event in events.values():
            seen = parse_iso(event.last_seen) or parse_iso(event.created)
            if seen and seen < cutoff:
                continue
            kept.append(event)
        kept.sort(key=lambda e: (e.created, e.id))
        path = self.dir / "events.jsonl"
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for event in kept:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, path)

    # ---- state ---------------------------------------------------------------------------
    def load_state(self) -> dict:
        state = read_json(self.dir / "state.json", {}) or {}
        state.setdefault("feeds", {})
        state.setdefault("editions", {})
        return state

    def save_state(self, state: dict) -> None:
        write_json(self.dir / "state.json", state)

    # ---- editions ------------------------------------------------------------------------
    def edition_path(self, date: str) -> Path:
        return self.dir / "editions" / f"{date}.json"

    def load_edition(self, date: str) -> dict | None:
        return read_json(self.edition_path(date))

    def save_edition(self, edition: dict) -> None:
        write_json(self.edition_path(edition["date"]), edition)

    def list_editions(self) -> list[str]:
        folder = self.dir / "editions"
        if not folder.exists():
            return []
        return sorted((p.stem for p in folder.glob("*.json")), reverse=True)

    # ---- run reports ---------------------------------------------------------------------
    def save_run(self, report: dict) -> None:
        """Full report in latest.json; a compact line per run in runs/YYYY-MM.jsonl."""
        write_json(self.dir / "runs" / "latest.json", report)
        compact = {k: v for k, v in report.items() if k not in ("fetch", "site")}
        compact["fetch_errors"] = [f["source_id"] for f in report.get("fetch", []) if f.get("status") == "error"]
        month = (report.get("started") or "")[:7] or "unknown"
        append_jsonl(self.dir / "runs" / f"{month}.jsonl", compact)

    def update_latest_run(self, report: dict) -> None:
        write_json(self.dir / "runs" / "latest.json", report)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        folder = self.dir / "runs"
        if not folder.exists():
            return []
        rows: list[dict] = []
        for path in sorted(folder.glob("*.jsonl"), reverse=True):
            rows = read_jsonl(path) + rows
            if len(rows) >= limit:
                break
        return rows[-limit:][::-1]

    @property
    def usage_dir(self) -> Path:
        return self.dir / "usage"
