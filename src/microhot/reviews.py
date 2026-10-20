"""Weekly and monthly reviews, compiled from the daily editions.

Stories are chosen by rule; one model call (the "review" chain) writes a short overview in English
and Traditional Chinese. Without a provider the review is still built, just without the overview.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import Settings
from .llm import Router
from .prompts import load_prompt
from .store import Store
from .textutil import cjk_ratio, has_cjk, to_iso
from .zh import to_taiwan

SCHEMA = {
    "type": "object",
    "properties": {"overview_en": {"type": "string"}, "overview_zh": {"type": "string"}},
    "required": ["overview_en", "overview_zh"],
    "additionalProperties": False,
}


def _previous_week(today: date) -> tuple[str, date, date]:
    start = today - timedelta(days=today.weekday() + 7)
    end = start + timedelta(days=6)
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}", start, end


def _previous_month(today: date) -> tuple[str, date, date]:
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    return f"{start:%Y-%m}", start, end


def due_reviews(settings: Settings, state: dict, now: datetime) -> list[tuple[str, str, date, date]]:
    """Reviews to build now: the previous week from Monday, the previous month from the 1st."""
    local = now.astimezone(settings.tz)
    if local.time() < settings.edition_after:
        return []
    done = state.setdefault("reviews", {})
    out = []
    cfg = settings.raw["reviews"]
    if cfg.get("weekly", True):
        week_id, start, end = _previous_week(local.date())
        if done.get("last_weekly") != week_id:
            out.append(("weekly", week_id, start, end))
    if cfg.get("monthly", True):
        month_id, start, end = _previous_month(local.date())
        if done.get("last_monthly") != month_id:
            out.append(("monthly", month_id, start, end))
    return out


def _collect(store: Store, start: date, end: date) -> tuple[list[dict], int]:
    stories: dict[str, dict] = {}
    editions = 0
    day = start
    while day <= end:
        edition = store.load_edition(day.isoformat())
        day += timedelta(days=1)
        if not edition:
            continue
        editions += 1
        for story in edition.get("stories", []):
            stories[story["id"]] = story  # a later snapshot of the same story wins
    return list(stories.values()), editions


def _overview_message(kind: str, stories: list[dict]) -> str:
    sentences = "3–4 sentences" if kind == "weekly" else "4–5 sentences"
    lines = [f"Kind: {kind} review. Overview length: {sentences}.", "", "Selected stories:"]
    for i, story in enumerate(stories, 1):
        lines.append(f"[{i}] {story['title']['en']}: {story['summary']['en']}")
    return "\n".join(lines)


def _validate(obj: dict) -> None:
    en, zh = str(obj.get("overview_en") or "").strip(), str(obj.get("overview_zh") or "").strip()
    if not en or not zh:
        raise ValueError("overview_en and overview_zh are required")
    if not has_cjk(zh):
        raise ValueError("overview_zh must be in Traditional Chinese")
    if cjk_ratio(en) > 0.2:
        raise ValueError("overview_en must be in English")


def build_review(
    kind: str,
    period_id: str,
    start: date,
    end: date,
    store: Store,
    settings: Settings,
    router: Router | None,
    now: datetime,
) -> dict | None:
    stories, editions = _collect(store, start, end)
    if not stories:
        return None
    cfg = settings.raw["reviews"]
    limit = int(cfg.get("weekly_stories" if kind == "weekly" else "monthly_stories", 12))
    ranked = sorted(stories, key=lambda s: (-(s.get("effective_score") or s.get("score") or 0), -s["temp"], s["id"]))
    chosen = [{**s, "badges": [], "temp_change": None} for s in ranked[:limit]]  # point-in-time markers
    order = {beat: i for i, beat in enumerate(settings.beat_order)}
    chosen.sort(key=lambda s: (order.get(s["beat"], 99), -(s.get("effective_score") or s.get("score") or 0)))
    overview = None
    if cfg.get("overview", True) and router is not None and router.available("review"):
        result = router.complete_json(
            "review",
            load_prompt(settings.prompts_dir, "review"),
            _overview_message(kind, chosen),
            SCHEMA,
            "review",
            _validate,
            max_tokens=2000,
        )
        if result:
            overview = {
                "en": str(result.data["overview_en"]).strip(),
                "zh-TW": to_taiwan(str(result.data["overview_zh"]).strip()),
                "by": f"{result.provider}/{result.model}",
            }
    return {
        "kind": kind,
        "id": period_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "built_at": to_iso(now),
        "editions": editions,
        "total": len(stories),
        "stories": chosen,
        "overview": overview,
        "delivered": {},
    }


def build_due_reviews(store: Store, settings: Settings, state: dict, router: Router | None, now: datetime) -> list[dict]:
    built = []
    for kind, period_id, start, end in due_reviews(settings, state, now):
        review = build_review(kind, period_id, start, end, store, settings, router, now)
        state["reviews"]["last_weekly" if kind == "weekly" else "last_monthly"] = period_id
        if review:
            store.save_review(review)
            built.append(review)
    return built
