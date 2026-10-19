"""HTTP adapters: any OpenAI-compatible endpoint, and the native Claude API."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..config import Provider


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0  # uncached input tokens
    cached_tokens: int = 0  # input tokens read from a prompt cache
    cache_write_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    stop: str | None = None


class LLMError(Exception):
    """kind: auth | payment | not_found | rate | bad_request | server | timeout | other"""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


def _error_from_response(resp: httpx.Response) -> LLMError:
    status = resp.status_code
    body = resp.text[:300].replace("\n", " ")
    retry_after = None
    header = resp.headers.get("retry-after")
    if header:
        try:
            retry_after = float(header)
        except ValueError:
            retry_after = None
    if status in (401, 403):
        kind = "auth"
    elif status == 402:
        kind = "payment"
    elif status == 404:
        kind = "not_found"
    elif status == 429:
        kind = "rate"
    elif status in (400, 413, 422):
        kind = "bad_request"
    elif status >= 500:
        kind = "server"
    else:
        kind = "other"
    return LLMError(kind, f"HTTP {status}: {body}", status, retry_after)


def _post(client: httpx.Client, url: str, headers: dict, body: dict, timeout: float) -> tuple[dict, int]:
    started = time.monotonic()
    try:
        resp = client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMError("timeout", f"timeout: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError("server", f"{type(exc).__name__}: {exc}") from exc
    ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        raise _error_from_response(resp)
    try:
        return resp.json(), ms
    except ValueError as exc:
        raise LLMError("server", f"non-JSON response: {resp.text[:200]}") from exc


def call_openai(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    json_mode: str,
    schema: dict,
    schema_name: str,
    use_extra: bool,
    client: httpx.Client,
    environ=None,
) -> LLMResult:
    url = provider.resolved_base_url(environ) + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = provider.api_key(environ)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        provider.max_tokens_param: max_tokens,
    }
    if json_mode == "schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
    elif json_mode == "object":
        body["response_format"] = {"type": "json_object"}
    if use_extra:
        body.update(provider.extra)
    data, ms = _post(client, url, headers, body, provider.timeout_s)
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
    cached = min(cached, prompt)
    return LLMResult(
        text=str(content),
        input_tokens=prompt - cached,
        cached_tokens=cached,
        output_tokens=int(usage.get("completion_tokens") or 0),
        ms=ms,
        stop=choice.get("finish_reason"),
    )


def call_anthropic(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    json_mode: str,
    schema: dict,
    schema_name: str,
    use_extra: bool,
    client: httpx.Client,
    environ=None,
) -> LLMResult:
    url = provider.resolved_base_url(environ) + "/messages"
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": provider.api_key(environ) or "",
    }
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        # The system prompt is identical on every call, so it is cached (min. 512 tokens on Haiku 5.5).
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if json_mode == "schema":
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if use_extra:
        body.update(provider.extra)
    data, ms = _post(client, url, headers, body, provider.timeout_s)
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    usage = data.get("usage") or {}
    return LLMResult(
        text=text,
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        ms=ms,
        stop=data.get("stop_reason"),
    )


def call(provider: Provider, *args, **kwargs) -> LLMResult:
    if provider.api == "anthropic":
        return call_anthropic(provider, *args, **kwargs)
    return call_openai(provider, *args, **kwargs)
