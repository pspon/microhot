"""Load MicroHOT settings from config/site.toml and config/sources.toml."""

from __future__ import annotations

import copy
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

TIER_RANK = {"T1": 0, "T1.5": 1, "T2": 2}
LANGS = ("en", "zh-TW")
# How a source is read: "feed" (RSS/Atom), "page" (links on an HTML page, for sites without a feed)
# or "search" (a news-search feed whose items come from many publishers).
SOURCE_KINDS = ("feed", "page", "search")
_BEAT_ID = re.compile(r"[a-z][a-z0-9-]*")

DEFAULTS: dict = {
    "site": {
        "name": "MicroHOT",
        "name_zh": "微熱",
        "tagline_en": "AI news, served slightly warm.",
        "tagline_zh": "不燙口的 AI 新聞。",
        "url": "",
        "timezone": "America/Toronto",
        "edition_after": "06:00",
        "beat_order": ["ai", "gov"],
    },
    "beats": {
        "ai": {"label_en": "AI", "label_zh": "AI 動態", "daily_quota": 8},
        "gov": {"label_en": "Governance & policy", "label_zh": "治理與政策", "daily_quota": 5},
    },
    "selection": {
        "thresholds": {"T1": 60, "T1.5": 65, "T2": 76},
        "borderline": 5,
        "second_opinion": True,
        "candidate_hours": 36,
    },
    "fetch": {
        "first_import_hours": 48,
        "first_import_undated_max": 5,
        "max_age_hours": 72,
        "max_items_per_feed": 40,
        "timeout_s": 20,
        "workers": 8,
        "same_host_pause_s": 1.0,
        "user_agent": "Mozilla/5.0 (compatible; MicroHOT/0.1; private news digest)",
    },
    "cluster": {"window_hours": 72, "embedder": "lexical", "threshold": 0.2},
    "heat": {"window_hours": 48, "half_life_hours": 24},
    "llm": {
        "triage": [],
        "write": [],
        # "digest" and "review" default to the write chain when not set.
        "triage_batch_size": 20,
        "write_batch_size": 4,
        "max_calls_per_day": 40,
        "usd_cap_per_day": 0.05,
        "triage_text_chars": 600,
        "write_text_chars": 1500,
        "recent_events_in_prompt": 25,
    },
    "adjust": {"boost": [], "mute": [], "max_points": 20},
    "editions": {"highlights": 3, "followup_min_new_sources": 2},
    "digest": {"enabled": True, "min_sources": 3, "max_per_day": 2, "regrow_sources": 2, "reports": 6, "report_chars": 700},
    "reviews": {"weekly": True, "monthly": True, "weekly_stories": 12, "monthly_stories": 20, "overview": True},
    "radar": {"hours": 72, "per_beat": 5, "archive_days": 90},
    "feedback": {"unpicked_per_beat": 4, "min_labels": 10},
    "alerts": {"feed_errors_in_a_row": 3, "channels": ["github_issue", "telegram"]},
    "providers": {},
    "channels": {},
    "retention": {"items_days": 30, "events_days": 30},
}

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass
class Source:
    id: str
    name: str
    url: str
    beat: str = "ai"
    tier: str = "T2"
    lang: str = "en"
    homepage: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    enabled: bool = True
    kind: str = "feed"
    # kind = "page": which links on the page are news items (a regex matched against each absolute URL),
    # and whether to open each new item's page for a short description (a few plain HTTP requests, 0 tokens).
    link_pattern: str = ""
    fetch_text: bool = False
    # Seconds to wait before this source when another source on the same host was just read
    # (some APIs allow one request every few seconds).
    pause_s: float = 0.0
    # An entry that keeps its link but changes its text (a bill reaching a new stage) is a new item.
    track_updates: bool = False

    def matches(self, text: str) -> bool:
        """Apply the keyword rules (no model call): no `exclude` pattern, every `require` pattern,
        and at least one `include` pattern when there are any."""
        if self.exclude and any(re.search(p, text, re.IGNORECASE) for p in self.exclude):
            return False
        if self.require and not all(re.search(p, text, re.IGNORECASE) for p in self.require):
            return False
        if self.include:
            return any(re.search(p, text, re.IGNORECASE) for p in self.include)
        return True


@dataclass
class Provider:
    name: str
    api: str = "openai"
    base_url: str = ""
    models: list[str] = field(default_factory=list)
    key_env: str | None = None
    json_mode: str = "object"
    max_tokens_param: str = "max_tokens"
    price_in: float = 0.0
    price_out: float = 0.0
    price_cached_in: float | None = None
    usd_cap_per_day: float | None = None
    timeout_s: float = 90.0
    free: bool = False
    extra: dict = field(default_factory=dict)

    def env_vars_needed(self) -> list[str]:
        needed = _ENV_REF.findall(self.base_url)
        if self.key_env:
            needed.insert(0, self.key_env)
        return needed

    def missing_env(self, environ=None) -> list[str]:
        environ = os.environ if environ is None else environ
        return [v for v in self.env_vars_needed() if not str(environ.get(v, "")).strip()]

    def is_configured(self, environ=None) -> bool:
        return bool(self.models) and bool(self.base_url) and not self.missing_env(environ)

    def resolved_base_url(self, environ=None) -> str:
        environ = os.environ if environ is None else environ
        return _ENV_REF.sub(lambda m: str(environ.get(m.group(1), "")).strip(), self.base_url).rstrip("/")

    def api_key(self, environ=None) -> str | None:
        environ = os.environ if environ is None else environ
        if not self.key_env:
            return None
        return str(environ.get(self.key_env, "")).strip() or None


@dataclass
class Settings:
    root: Path
    raw: dict
    sources: list[Source]
    providers: dict[str, Provider]

    # -- convenience accessors ------------------------------------------------------------
    @property
    def site(self) -> dict:
        return self.raw["site"]

    @property
    def beats(self) -> dict:
        return self.raw["beats"]

    @property
    def selection(self) -> dict:
        return self.raw["selection"]

    @property
    def fetch(self) -> dict:
        return self.raw["fetch"]

    @property
    def cluster(self) -> dict:
        return self.raw["cluster"]

    @property
    def heat(self) -> dict:
        return self.raw["heat"]

    @property
    def llm(self) -> dict:
        return self.raw["llm"]

    @property
    def channels(self) -> dict:
        return self.raw["channels"]

    @property
    def radar(self) -> dict:
        return self.raw["radar"]

    @property
    def feedback(self) -> dict:
        return self.raw["feedback"]

    @property
    def retention(self) -> dict:
        return self.raw["retention"]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.site["timezone"])

    @property
    def edition_after(self) -> time:
        hh, mm = (int(x) for x in str(self.site["edition_after"]).split(":"))
        return time(hh, mm)

    @property
    def beat_order(self) -> list[str]:
        order = [b for b in self.site.get("beat_order", []) if b in self.beats]
        return order + [b for b in self.beats if b not in order]

    @property
    def site_url(self) -> str:
        return (os.environ.get("SITE_URL", "").strip() or str(self.site.get("url", ""))).rstrip("/")

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("MICROHOT_DATA", self.root / "data"))

    @property
    def public_dir(self) -> Path:
        return Path(os.environ.get("MICROHOT_PUBLIC", self.root / "public"))

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    def source(self, source_id: str) -> Source | None:
        return self._source_index.get(source_id)

    def threshold_for(self, tier: str, beat: str | None = None) -> int:
        """Score needed by an event whose best source has `tier`; a beat may lower or raise it."""
        own = self.beats.get(beat, {}).get("thresholds", {}) if beat else {}
        if tier in own:
            return int(own[tier])
        th = self.selection["thresholds"]
        return int(th.get(tier, th.get("T2", 76)))

    def writes_why(self, beat: str) -> bool:
        """Whether stories in this beat get a one-line "why it matters" note."""
        return bool(self.beats.get(beat, {}).get("why", False))

    def __post_init__(self) -> None:
        self._source_index = {s.id: s for s in self.sources}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _parse_sources(raw: dict, beats: tuple[str, ...]) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw.get("source", []):
        sid = str(entry["id"]).strip()
        if sid in seen:
            raise ValueError(f"duplicate source id: {sid}")
        seen.add(sid)
        beat = entry.get("beat", "ai")
        tier = entry.get("tier", "T2")
        kind = entry.get("kind", "feed")
        if beat not in beats:
            raise ValueError(f"source {sid}: beat must be one of {beats} (beats are set in config/site.toml)")
        if tier not in TIER_RANK:
            raise ValueError(f"source {sid}: tier must be one of {tuple(TIER_RANK)}")
        if kind not in SOURCE_KINDS:
            raise ValueError(f"source {sid}: kind must be one of {SOURCE_KINDS}")
        if kind == "page" and not entry.get("link_pattern"):
            raise ValueError(f"source {sid}: a page source needs link_pattern (which links are news items)")
        patterns = list(entry.get("include", [])) + list(entry.get("exclude", [])) + list(entry.get("require", []))
        for pattern in patterns + ([entry["link_pattern"]] if entry.get("link_pattern") else []):
            re.compile(pattern)
        sources.append(
            Source(
                id=sid,
                name=entry.get("name", sid),
                url=entry["url"],
                beat=beat,
                tier=tier,
                lang=entry.get("lang", "en"),
                homepage=entry.get("homepage", ""),
                include=list(entry.get("include", [])),
                exclude=list(entry.get("exclude", [])),
                require=list(entry.get("require", [])),
                enabled=bool(entry.get("enabled", True)),
                kind=kind,
                link_pattern=str(entry.get("link_pattern", "")),
                fetch_text=bool(entry.get("fetch_text", False)),
                pause_s=float(entry.get("pause_s", 0.0)),
                track_updates=bool(entry.get("track_updates", False)),
            )
        )
    return sources


def _validate_beats(beats: dict) -> tuple[str, ...]:
    for beat, cfg in beats.items():
        if not _BEAT_ID.fullmatch(beat):
            raise ValueError(f"beat id {beat!r} must be lowercase letters, digits and dashes")
        if not isinstance(cfg, dict):
            raise ValueError(f"[beats.{beat}] must be a table")
        int(cfg.get("daily_quota", 5))
        for tier, value in dict(cfg.get("thresholds", {})).items():
            if tier not in TIER_RANK:
                raise ValueError(f"[beats.{beat}] thresholds: tier must be one of {tuple(TIER_RANK)}")
            int(value)
    return tuple(beats)


def _parse_providers(raw: dict) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for name, entry in raw.items():
        api = entry.get("api", "openai")
        if api not in ("openai", "anthropic"):
            raise ValueError(f"provider {name}: api must be 'openai' or 'anthropic'")
        json_mode = entry.get("json_mode", "object")
        if json_mode not in ("schema", "object", "prompt"):
            raise ValueError(f"provider {name}: json_mode must be schema, object or prompt")
        providers[name] = Provider(
            name=name,
            api=api,
            base_url=entry.get("base_url", ""),
            models=list(entry.get("models", [])),
            key_env=(entry.get("key_env") or None),
            json_mode=json_mode,
            max_tokens_param=entry.get("max_tokens_param", "max_tokens"),
            price_in=float(entry.get("price_in", 0.0)),
            price_out=float(entry.get("price_out", 0.0)),
            price_cached_in=(float(entry["price_cached_in"]) if "price_cached_in" in entry else None),
            usd_cap_per_day=(float(entry["usd_cap_per_day"]) if "usd_cap_per_day" in entry else None),
            timeout_s=float(entry.get("timeout_s", 90.0)),
            free=bool(entry.get("free", False)),
            extra=dict(entry.get("extra", {})),
        )
    return providers


def load_settings(root: Path | str | None = None) -> Settings:
    root = Path(root or os.environ.get("MICROHOT_ROOT") or Path.cwd()).resolve()
    site_path = root / "config" / "site.toml"
    sources_path = root / "config" / "sources.toml"
    user = tomllib.loads(site_path.read_text(encoding="utf-8")) if site_path.exists() else {}
    raw = _deep_merge(DEFAULTS, user)
    beats = _validate_beats(raw["beats"])
    sources_raw = tomllib.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    sources = _parse_sources(sources_raw, beats)
    providers = _parse_providers(raw.get("providers", {}))
    for step in ("digest", "review"):
        raw["llm"].setdefault(step, list(raw["llm"].get("write", [])))
    for step in ("triage", "write", "digest", "review"):
        unknown = [p for p in raw["llm"].get(step, []) if p not in providers]
        if unknown:
            raise ValueError(f"llm.{step} lists undefined providers: {', '.join(unknown)}")
    for pattern in raw["adjust"].get("mute", []) + [r["pattern"] for r in raw["adjust"].get("boost", []) if "pattern" in r]:
        re.compile(pattern)
    ZoneInfo(raw["site"]["timezone"])  # validate early
    return Settings(root=root, raw=raw, sources=sources, providers=providers)
