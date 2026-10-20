"""Failure alerts: a feed failing several runs in a row, model providers failing or turned off,
or the daily call cap being reached. The run step queues them in state.json; `microhot deliver`
sends them to the channels listed in [alerts] channels, then clears the queue.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from .config import Settings
from .textutil import safe_error, to_iso


def _queue(state: dict, key: str, text: str, now: datetime, settings: Settings, once_per_day: bool = True) -> None:
    alerts = state.setdefault("alerts", {"pending": [], "sent": {}})
    alerts.setdefault("pending", [])
    alerts.setdefault("sent", {})
    today = now.astimezone(settings.tz).date().isoformat()
    if once_per_day and alerts["sent"].get(key) == today:
        return
    if any(a["key"] == key for a in alerts["pending"]):
        return
    alerts["pending"].append({"key": key, "text": text, "at": to_iso(now)})
    alerts["sent"][key] = today


def detect(settings: Settings, state: dict, report: dict, now: datetime) -> int:
    """Queue alerts for this run. Returns how many were queued."""
    before = len(state.get("alerts", {}).get("pending", []))
    limit = int(settings.raw["alerts"].get("feed_errors_in_a_row", 3))
    for source in settings.sources:
        st = state.get("feeds", {}).get(source.id, {})
        if not source.enabled:
            continue
        if int(st.get("errors_in_a_row", 0)) == 0:
            st.pop("alerted", None)
        elif int(st.get("errors_in_a_row", 0)) >= limit and not st.get("alerted"):
            _queue(
                state,
                f"feed:{source.id}",
                f"Feed “{source.name}” has failed {st['errors_in_a_row']} runs in a row ({st.get('last_error')}). "
                "Fix or pause it in config/sources.toml.",
                now,
                settings,
                once_per_day=False,
            )
            st["alerted"] = True

    llm = report.get("llm") or {}
    for name, reason in (llm.get("disabled") or {}).items():
        _queue(state, f"provider:{name}", f"Model provider “{name}” was turned off this run: {reason}. Check its secret.", now, settings)
    if llm.get("exhausted"):
        _queue(state, "cap", "The daily model call cap was reached; remaining stories wait for tomorrow.", now, settings)
    pending_work = int((report.get("triage") or {}).get("pending", 0) or 0) + int((report.get("write") or {}).get("pending", 0) or 0)
    if pending_work and llm.get("calls", 0) and not llm.get("ok_calls", 0):
        errors = "; ".join((llm.get("errors") or [])[-3:])
        _queue(state, "llm-failed", f"No model provider worked this run ({errors}). Stories are waiting.", now, settings)
    return len(state.get("alerts", {}).get("pending", [])) - before


def send_pending(settings: Settings, state: dict, environ) -> dict[str, str]:
    from .channels import CHANNELS, missing_env

    pending = state.get("alerts", {}).get("pending", [])
    if not pending:
        return {}
    text = "\n".join(f"- {a['text']}" for a in pending)
    results: dict[str, str] = {}
    delivered_any = False
    for name in settings.raw["alerts"].get("channels", []):
        module = CHANNELS.get(name)
        if module is None or not settings.channels.get(name, {}).get("enabled"):
            results[name] = "off"
            continue
        if missing_env(module, environ):
            results[name] = "not configured"
            continue
        try:
            module.send_alert(text, settings, environ)
            results[name] = "sent"
            delivered_any = True
        except (httpx.HTTPError, OSError, KeyError) as exc:
            results[name] = "error: " + safe_error(exc)
    if delivered_any or not results or all(r in ("off", "not configured") for r in results.values()):
        state["alerts"]["pending"] = []  # nowhere else to send them; they stay in the run report
    return results
