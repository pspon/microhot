import json

from conftest import NOW, FakeLLM

from microhot.llm import Router, UsageLog
from microhot.llm.adapters import LLMError


def _router(project, fake, environ):
    usage = UsageLog(project.data_dir / "usage", project.tz)
    return Router(project, usage, NOW, adapter=fake, environ=environ, sleep=lambda s: None)


def _ok(data):
    return None


def test_unconfigured_providers_are_never_called(project):
    fake = FakeLLM()
    router = _router(project, fake, {"ANTHROPIC_API_KEY": "k"})
    assert router.chain("triage") == ["claude"]
    assert "gemini" in router.unconfigured and "GEMINI_API_KEY" in router.unconfigured["gemini"]
    # Cloudflare needs both its token and the account id in the URL.
    assert "CLOUDFLARE_ACCOUNT_ID" in router.unconfigured["cloudflare"]
    result = router.complete_json("triage", "sys", "[1] published x\nTitle: Acme releases model", {}, "triage", _ok, 100)
    assert result.provider == "claude"
    assert {c[0] for c in fake.calls} == {"claude"}


def test_rate_limit_tries_next_model_then_next_provider(project):
    # Each Gemini model gets one try and one retry; two failures switch the provider off for the run.
    fake = FakeLLM(fail={"gemini": [LLMError("rate", "429")] * 4})
    router = _router(project, fake, {"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "a"})
    result = router.complete_json("triage", "sys", "[1] published x\nTitle: t", {}, "triage", _ok, 100)
    assert result.provider == "claude"
    assert [c[1] for c in fake.calls if c[0] == "gemini"] == ["gemini-3.5-flash-lite"] * 2 + ["gemini-3.1-flash-lite"] * 2
    # Later calls in the same run skip Gemini entirely.
    router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert [c[0] for c in fake.calls].count("gemini") == 4


def test_rate_limit_on_one_model_falls_back_to_the_next_model(project):
    fake = FakeLLM(fail={"gemini": [LLMError("rate", "429"), LLMError("rate", "429")]})
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    result = router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert (result.provider, result.model) == ("gemini", "gemini-3.1-flash-lite")


def test_invalid_json_gets_one_repair_attempt(project):
    fake = FakeLLM(fail={"gemini": ["not json at all"]})
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    result = router.complete_json("triage", "sys", "[1] published x\nTitle: t", {}, "triage", _ok, 100)
    assert result.provider == "gemini"
    assert len(fake.calls) == 2


def test_rejected_key_turns_provider_off_for_the_run(project):
    fake = FakeLLM(fail={"gemini": [LLMError("auth", "401")]})
    router = _router(project, fake, {"GEMINI_API_KEY": "bad", "DEEPSEEK_API_KEY": "d"})
    first = router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert first.provider == "deepseek" and router.disabled["gemini"] == "key rejected"
    router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert [c[0] for c in fake.calls].count("gemini") == 1


def test_unknown_model_moves_to_the_providers_next_model(project):
    fake = FakeLLM(fail={"gemini": [LLMError("not_found", "404")]})
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    result = router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert result.model == "gemini-3.1-flash-lite"


def test_bad_request_steps_json_mode_down(project):
    fake = FakeLLM(fail={"gemini": [LLMError("bad_request", "400 response_format")]})
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert [c[3] for c in fake.calls] == ["schema", "object"]


def test_bad_request_about_an_extra_parameter_drops_it_first(project):
    fake = FakeLLM(fail={"gemini": [LLMError("bad_request", "HTTP 400: unknown field reasoning_effort")]})
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    assert [c[3] for c in fake.calls] == ["schema", "schema"]  # same JSON mode, extras removed
    assert ("gemini", "gemini-3.5-flash-lite") in router.no_extra


def test_daily_call_cap_stops_everything(project):
    project.llm["max_calls_per_day"] = 1
    fake = FakeLLM()
    router = _router(project, fake, {"GEMINI_API_KEY": "g"})
    assert router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100) is not None
    assert router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100) is None
    assert router.exhausted and len(fake.calls) == 1


def test_paid_provider_respects_its_dollar_cap(project):
    usage = UsageLog(project.data_dir / "usage", project.tz)
    usage.append({"ts": "2026-10-20T10:00:00Z", "day": "2026-10-20", "provider": "claude", "usd": 0.05, "ok": True})
    fake = FakeLLM()
    router = Router(project, usage, NOW, adapter=fake, environ={"ANTHROPIC_API_KEY": "a"}, sleep=lambda s: None)
    assert router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100) is None
    assert fake.calls == [] and "claude" in router.budget_skips


def test_usage_is_logged_with_cost(project):
    fake = FakeLLM()
    router = _router(project, fake, {"ANTHROPIC_API_KEY": "a"})
    router.complete_json("triage", "sys", "x", {}, "triage", _ok, 100)
    rows = UsageLog(project.data_dir / "usage", project.tz).rows_for_day(NOW)
    assert len(rows) == 1 and rows[0]["provider"] == "claude" and rows[0]["usd"] > 0
    assert json.loads(json.dumps(rows[0]))["ok"] is True
