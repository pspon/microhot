"""Fallback chain across providers, with budgets, retries and a per-run circuit breaker.

Rules:
- A provider is *active* only if it is defined in config and every env var it needs is set.
  Inactive providers are never called and never counted as failures.
- Rate limits, server errors and timeouts get one retry with backoff, then the chain moves on.
- An invalid reply gets one repair attempt on the same model, then the chain moves on.
- A rejected key (401/403) or exhausted credit (402) turns the provider off for this run.
- An unknown model (404) is skipped for this run; the provider's next model is tried.
- A provider that fails twice is skipped for the rest of the run.
- Daily caps: total calls across providers, total USD, and USD per paid provider.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..config import Provider, Settings
from ..textutil import to_iso
from .adapters import LLMError, LLMResult, call
from .jsonutil import extract_json
from .usage import UsageLog

_MODES = ["schema", "object", "prompt"]
MAX_FAILURES_PER_RUN = 2


@dataclass
class CallResult:
    data: dict
    provider: str
    model: str


def cost_usd(provider: Provider, result: LLMResult) -> float:
    if provider.free:
        return 0.0
    cached_price = provider.price_cached_in if provider.price_cached_in is not None else provider.price_in
    return (
        result.input_tokens * provider.price_in
        + result.cached_tokens * cached_price
        + result.cache_write_tokens * provider.price_in * 1.25
        + result.output_tokens * provider.price_out
    ) / 1_000_000


class Router:
    def __init__(
        self,
        settings: Settings,
        usage: UsageLog,
        now: datetime,
        *,
        adapter: Callable[..., LLMResult] = call,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        environ=None,
    ) -> None:
        self.settings = settings
        self.usage = usage
        self.now = now
        self.adapter = adapter
        self.client = client or httpx.Client()
        self.sleep = sleep
        self.environ = os.environ if environ is None else environ
        self.tz = settings.tz

        self.active: dict[str, Provider] = {}
        self.unconfigured: dict[str, str] = {}
        for name, provider in settings.providers.items():
            if not provider.models or not provider.base_url:
                self.unconfigured[name] = "no models or base_url"
                continue
            missing = provider.missing_env(self.environ)
            if missing:
                self.unconfigured[name] = "not set: " + ", ".join(missing)
            else:
                self.active[name] = provider

        self.disabled: dict[str, str] = {}
        self.disabled_models: set[tuple[str, str]] = set()
        self.failures: Counter = Counter()
        self.mode_override: dict[tuple[str, str], str] = {}
        self.no_extra: set[tuple[str, str]] = set()
        self.budget_skips: set[str] = set()
        self.exhausted = False
        self.errors: list[str] = []
        self.run_calls = 0
        self.run_ok = 0
        self.run_usd = 0.0
        self.calls_today, self.usd_today = usage.today_totals(now)

    # ---- introspection -------------------------------------------------------------------
    def chain(self, step: str) -> list[str]:
        return [name for name in self.settings.llm.get(step, []) if name in self.active]

    def available(self, step: str) -> bool:
        return bool(self.chain(step)) and not self.exhausted

    def summary(self) -> dict:
        return {
            "active": {step: self.chain(step) for step in ("triage", "write", "digest", "review")},
            "unconfigured": self.unconfigured,
            "disabled": self.disabled,
            "failures": dict(self.failures),
            "budget_skips": sorted(self.budget_skips),
            "exhausted": self.exhausted,
            "calls": self.run_calls,
            "ok_calls": self.run_ok,
            "usd": round(self.run_usd, 6),
            "errors": self.errors[-20:],
        }

    # ---- budget --------------------------------------------------------------------------
    def _total_usd_today(self) -> float:
        return sum(self.usd_today.values())

    def _within_budget(self, provider: Provider) -> bool:
        if self.calls_today >= int(self.settings.llm["max_calls_per_day"]):
            self.exhausted = True
            return False
        if provider.free:
            return True
        if self._total_usd_today() >= float(self.settings.llm["usd_cap_per_day"]):
            return False
        cap = provider.usd_cap_per_day
        return cap is None or self.usd_today.get(provider.name, 0.0) < cap

    def _log(self, step: str, provider: Provider, model: str, ok: bool, error: str | None, result: LLMResult | None) -> None:
        usd = cost_usd(provider, result) if result else 0.0
        if result is not None:  # rejected requests (no tokens) don't count toward the daily cap
            self.calls_today += 1
        self.run_calls += 1
        self.run_ok += 1 if ok else 0
        self.run_usd += usd
        self.usd_today[provider.name] = self.usd_today.get(provider.name, 0.0) + usd
        now = datetime.now(self.now.tzinfo) if self.now.tzinfo else self.now
        self.usage.append(
            {
                "ts": to_iso(now),
                "day": self.now.astimezone(self.tz).date().isoformat(),
                "step": step,
                "provider": provider.name,
                "model": model,
                "ok": ok,
                "error": error,
                "in": result.input_tokens if result else 0,
                "cached": result.cached_tokens if result else 0,
                "cache_write": result.cache_write_tokens if result else 0,
                "out": result.output_tokens if result else 0,
                "usd": round(usd, 7),
                "ms": result.ms if result else 0,
            }
        )
        if error:
            self.errors.append(f"{step}/{provider.name}/{model}: {error}"[:300])

    # ---- calling -------------------------------------------------------------------------
    def complete_json(
        self,
        step: str,
        system: str,
        user: str,
        schema: dict,
        schema_name: str,
        validate: Callable[[dict], None],
        max_tokens: int,
    ) -> CallResult | None:
        for name in self.chain(step):
            if self.exhausted:
                return None
            if name in self.disabled or self.failures[name] >= MAX_FAILURES_PER_RUN:
                continue
            provider = self.active[name]
            if not self._within_budget(provider):
                if not self.exhausted:
                    self.budget_skips.add(name)
                continue
            for model in provider.models:
                if (name, model) in self.disabled_models:
                    continue
                data = self._attempt(step, provider, model, system, user, schema, schema_name, validate, max_tokens)
                if data is not None:
                    return CallResult(data=data, provider=name, model=model)
                if self.exhausted or name in self.disabled or self.failures[name] >= MAX_FAILURES_PER_RUN:
                    break
        return None

    def _attempt(self, step, provider, model, system, user, schema, schema_name, validate, max_tokens):
        key = (provider.name, model)
        mode = self.mode_override.get(key, provider.json_mode)
        use_extra = key not in self.no_extra
        prompt = user
        retried = repaired = False
        for _ in range(6):
            if not self._within_budget(provider):
                return None
            try:
                result = self.adapter(
                    provider, model, system, prompt, max_tokens, mode, schema, schema_name, use_extra, self.client,
                    environ=self.environ,
                )
            except LLMError as exc:
                self._log(step, provider, model, False, f"{exc.kind}: {exc}"[:240], None)
                if exc.kind in ("rate", "server", "timeout", "other") and not retried:
                    retried = True
                    self.sleep(min(exc.retry_after or 3.0, 20.0))
                    continue
                if exc.kind == "bad_request":
                    message = str(exc).lower()
                    blames_extra = any(k.lower() in message for k in provider.extra)
                    if use_extra and provider.extra and blames_extra:
                        use_extra = False
                        self.no_extra.add(key)
                        continue
                    if mode in _MODES[:-1]:
                        mode = _MODES[_MODES.index(mode) + 1]
                        self.mode_override[key] = mode
                        continue
                    if use_extra and provider.extra:
                        use_extra = False
                        self.no_extra.add(key)
                        continue
                if exc.kind in ("auth", "payment"):
                    self.disabled[provider.name] = "key rejected" if exc.kind == "auth" else "out of credit"
                    return None
                if exc.kind == "not_found":
                    self.disabled_models.add(key)
                    return None
                self.failures[provider.name] += 1
                return None
            try:
                data = extract_json(result.text)
                validate(data)
            except ValueError as exc:
                self._log(step, provider, model, False, f"invalid: {exc}"[:240], result)
                if not repaired:
                    repaired = True
                    prompt = (
                        f"{user}\n\nYour previous reply was rejected: {exc}. "
                        "Return only the corrected JSON object in the required shape."
                    )
                    continue
                self.failures[provider.name] += 1
                return None
            self._log(step, provider, model, True, None, result)
            return data
        self.failures[provider.name] += 1
        return None
