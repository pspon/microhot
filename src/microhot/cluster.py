"""Group items into events (stories) and compute heat. No model calls.

The default similarity index is lexical (TF-IDF over word unigrams/bigrams and CJK character
bigrams), which needs no downloads. A small local embedding model (model2vec) can be switched
on in config/site.toml. The triage step adds semantic duplicate hints on top of this.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import TIER_RANK, Settings
from .model import Event, Item
from .textutil import hours_between, to_iso, tokens

LEXICAL_THRESHOLD = 0.2


class LexicalEmbedder:
    name = "lexical"

    def vectors(self, texts: list[str]) -> list[dict[str, float]]:
        docs = [Counter(tokens(t)) for t in texts]
        df: Counter = Counter()
        for doc in docs:
            df.update(doc.keys())
        n = len(docs)
        out = []
        for doc in docs:
            vec = {tok: (1.0 + math.log(tf)) * (math.log((1 + n) / (1 + df[tok])) + 1.0) for tok, tf in doc.items()}
            norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
            out.append({k: x / norm for k, x in vec.items()})
        return out

    @staticmethod
    def similarity(a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(k, 0.0) for k, x in a.items())


class Model2VecEmbedder:
    def __init__(self, model_name: str) -> None:
        from model2vec import StaticModel  # optional dependency

        self.model = StaticModel.from_pretrained(model_name)
        self.name = f"model2vec:{model_name}"

    def vectors(self, texts: list[str]):
        import numpy as np

        emb = np.asarray(self.model.encode(texts), dtype="float32")
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return list(emb / norms)

    @staticmethod
    def similarity(a, b) -> float:
        return float(a @ b)


def get_embedder(settings: Settings) -> tuple[object, float, str | None]:
    """Return (embedder, threshold, warning)."""
    spec = str(settings.cluster.get("embedder", "lexical"))
    threshold = float(settings.cluster.get("threshold", LEXICAL_THRESHOLD))
    if spec.startswith("model2vec:"):
        try:
            return Model2VecEmbedder(spec.split(":", 1)[1]), threshold, None
        except Exception as exc:  # missing package or model download failed
            return LexicalEmbedder(), LEXICAL_THRESHOLD, f"embedder {spec} unavailable ({exc}); using lexical"
    return LexicalEmbedder(), threshold, None


def _sim_text(item: Item) -> str:
    return f"{item.title} {item.title} {item.snippet[:300]}"


def resolve(events: dict[str, Event], event_id: str | None) -> Event | None:
    seen = set()
    event = events.get(event_id) if event_id else None
    while event and event.merged_into and event.id not in seen:
        seen.add(event.id)
        event = events.get(event.merged_into)
    return event


def choose_rep(event: Event, items: dict[str, Item], settings: Settings) -> str:
    """Representative item: best source tier, then the most text, then the earliest."""

    def key(item_id: str):
        it = items.get(item_id)
        if not it:
            return (9, 0, "")
        src = settings.source(it.source_id)
        tier = TIER_RANK.get(src.tier if src else "T2", 2)
        return (tier, -len(it.snippet), it.when().isoformat())

    candidates = [i for i in event.item_ids if i in items]
    return min(candidates, key=key) if candidates else event.rep


def new_event(item: Item, settings: Settings) -> Event:
    src = settings.source(item.source_id)
    when = to_iso(item.when())
    return Event(
        id=f"e{item.id}",
        created=when,
        item_ids=[item.id],
        rep=item.id,
        beat=src.beat if src else "ai",
        last_seen=when,
    )


def attach(item: Item, event: Event, items: dict[str, Item], settings: Settings) -> None:
    if item.id not in event.item_ids:
        event.item_ids.append(item.id)
    item.event_id = event.id
    when = to_iso(item.when())
    if when > (event.last_seen or ""):
        event.last_seen = when
    if event.score is None and event.story is None:
        event.rep = choose_rep(event, items, settings)


def merge_events(target: Event, other: Event, items: dict[str, Item]) -> None:
    """Fold `other` into `target` (e.g. on a duplicate hint from the triage model)."""
    if target.id == other.id:
        return
    for item_id in other.item_ids:
        if item_id not in target.item_ids:
            target.item_ids.append(item_id)
        if item_id in items:
            items[item_id].event_id = target.id
    if other.last_seen > target.last_seen:
        target.last_seen = other.last_seen
    if other.score is not None and (target.score is None or other.score > target.score):
        target.type, target.axes, target.score = other.type, other.axes, other.score
        target.opinions, target.triaged_at, target.triaged_by = other.opinions, other.triaged_at, other.triaged_by
        target.beat = other.beat
    if other.selected_at and not target.selected_at:
        target.selected_at, target.story = other.selected_at, other.story
    other.item_ids = []
    other.merged_into = target.id


def assign_events(
    new_items: list[Item],
    items: dict[str, Item],
    events: dict[str, Event],
    settings: Settings,
    now: datetime,
    embedder=None,
    threshold: float | None = None,
) -> dict:
    """Attach each new item to a recent event about the same story, or start a new event."""
    if embedder is None:
        embedder, threshold, _ = get_embedder(settings)
    threshold = LEXICAL_THRESHOLD if threshold is None else threshold
    window_start = now - timedelta(hours=float(settings.cluster["window_hours"]))

    members: dict[str, list[Item]] = defaultdict(list)
    for item in items.values():
        if not item.event_id or item.when() < window_start:
            continue
        event = resolve(events, item.event_id)
        if event:
            members[event.id].append(item)

    pending = [it for it in new_items if not it.event_id]
    stats = {"joined": 0, "created": 0}
    if not pending:
        return stats

    corpus = [it for group in members.values() for it in group] + pending
    vectors = embedder.vectors([_sim_text(it) for it in corpus])
    vec_of = {it.id: vec for it, vec in zip(corpus, vectors)}
    event_vecs = {eid: [vec_of[it.id] for it in group] for eid, group in members.items()}

    for item in sorted(pending, key=lambda it: (it.when(), it.id)):
        vec = vec_of[item.id]
        best_id, best_sim = None, 0.0
        for eid, vecs in event_vecs.items():
            sim = max(embedder.similarity(vec, other) for other in vecs)
            if sim > best_sim:
                best_id, best_sim = eid, sim
        if best_id is not None and best_sim >= threshold:
            attach(item, events[best_id], items, settings)
            event_vecs[best_id].append(vec)
            stats["joined"] += 1
        else:
            event = new_event(item, settings)
            events[event.id] = event
            item.event_id = event.id
            event_vecs[event.id] = [vec]
            stats["created"] += 1
    return stats


def update_heat(events: dict[str, Event], items: dict[str, Item], settings: Settings, now: datetime) -> None:
    """Heat = each independent source counted once within the window, halving every half-life."""
    window = float(settings.heat["window_hours"])
    half_life = float(settings.heat["half_life_hours"])
    for event in events.values():
        if not event.is_live:
            continue
        latest: dict[str, float] = {}
        sources: set[str] = set()
        for item_id in event.item_ids:
            item = items.get(item_id)
            if not item:
                continue
            sources.add(item.source_id)
            age = max(0.0, hours_between(now, item.when()))
            if age <= window:
                latest[item.source_id] = min(latest.get(item.source_id, age), age)
        event.heat = round(sum(0.5 ** (age / half_life) for age in latest.values()), 3)
        if sources:
            event.n_sources = len(sources)
