"""Usage log: one JSON line per model call, used for daily caps and the About page."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..store import append_jsonl, read_jsonl
from ..textutil import parse_iso


class UsageLog:
    def __init__(self, folder: Path, tz: ZoneInfo) -> None:
        self.folder = Path(folder)
        self.tz = tz

    def _path_for(self, local: datetime) -> Path:
        return self.folder / f"{local:%Y-%m}.jsonl"

    def append(self, entry: dict) -> None:
        ts = parse_iso(entry["ts"])
        assert ts is not None
        append_jsonl(self._path_for(ts.astimezone(self.tz)), entry)

    def rows_for_month(self, now: datetime) -> list[dict]:
        return read_jsonl(self._path_for(now.astimezone(self.tz)))

    def rows_for_day(self, now: datetime) -> list[dict]:
        day = now.astimezone(self.tz).date().isoformat()
        return [r for r in self.rows_for_month(now) if r.get("day") == day]

    def today_totals(self, now: datetime) -> tuple[int, dict[str, float]]:
        calls = 0
        usd: dict[str, float] = defaultdict(float)
        for row in self.rows_for_day(now):
            if row.get("ok") or int(row.get("in") or 0) + int(row.get("out") or 0) > 0:
                calls += 1  # requests rejected before any tokens were used don't count
            usd[row.get("provider", "?")] += float(row.get("usd") or 0.0)
        return calls, dict(usd)

    @staticmethod
    def summarize(rows: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for row in rows:
            key = row.get("provider", "?")
            agg = out.setdefault(key, {"calls": 0, "ok": 0, "in": 0, "cached": 0, "out": 0, "usd": 0.0})
            agg["calls"] += 1
            agg["ok"] += 1 if row.get("ok") else 0
            agg["in"] += int(row.get("in") or 0) + int(row.get("cache_write") or 0)
            agg["cached"] += int(row.get("cached") or 0)
            agg["out"] += int(row.get("out") or 0)
            agg["usd"] = round(agg["usd"] + float(row.get("usd") or 0.0), 6)
        return out
