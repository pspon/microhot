"""Shared fixtures: a throwaway project folder, fake feeds over a mock transport, a fake model."""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from microhot.config import load_settings
from microhot.llm.adapters import LLMError, LLMResult

REPO = Path(__file__).resolve().parents[1]
# 06:30 in Toronto (EDT): the morning run that builds the edition.
NOW = datetime(2026, 10, 20, 10, 30, tzinfo=UTC)

SOURCES = """
[[source]]
id = "lab"
name = "Acme Lab Blog"
url = "https://lab.test/feed.xml"
beat = "ai"
tier = "T1"

[[source]]
id = "media"
name = "Tech Daily"
url = "https://media.test/atom.xml"
beat = "ai"
tier = "T2"

[[source]]
id = "gov"
name = "Government News (AI only)"
url = "https://gov.test/rss"
beat = "gov"
tier = "T1"
include = ['artificial intelligence', '\\bA\\.?I\\b']
"""


def rss(items: list[tuple[str, str, datetime, str]]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{format_datetime(when)}</pubDate>"
        f"<description>{text}</description></item>"
        for title, link, when, text in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


def atom(items: list[tuple[str, str, datetime, str]]) -> str:
    body = "".join(
        f'<entry><title>{title}</title><link href="{link}"/><id>{link}</id>'
        f"<updated>{when.isoformat()}</updated><summary>{text}</summary></entry>"
        for title, link, when, text in items
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>{body}</feed>'


def default_feeds(now: datetime = NOW) -> dict[str, str]:
    h = lambda n: now - timedelta(hours=n)  # noqa: E731
    return {
        "https://lab.test/feed.xml": rss(
            [
                ("Acme releases Nova-2 model with a 1M-token context window", "https://lab.test/nova-2?utm_source=rss", h(2),
                 "Acme today released Nova-2, a model with a 1M-token context window, available in the API at $1 per million tokens."),
                ("Customer story: a retailer uses Nova for support", "https://lab.test/retailer", h(3),
                 "A retailer uses Nova to answer support tickets."),
                ("An old post from last week", "https://lab.test/old", h(120), "Old news."),
            ]
        ),
        "https://media.test/atom.xml": atom(
            [
                ("Acme's Nova-2 model brings a 1M-token context window to developers", "https://media.test/acme-nova-2", h(1),
                 "Acme released Nova-2 with a 1M-token context window; developers can use it in the API today."),
                ("Weekly roundup: ten AI links", "https://media.test/roundup", h(4), "Ten links about AI this week."),
            ]
        ),
        "https://gov.test/rss": rss(
            [
                ("Canada publishes artificial intelligence guidance for federal departments", "https://gov.test/ai-guidance", h(5),
                 "The guidance requires departments to assess automated decision systems before deployment."),
                ("Minister announces bridge repair funding", "https://gov.test/bridge", h(5), "Funding for a bridge."),
            ]
        ),
    }


class FeedServer:
    """httpx mock transport serving fixture feeds; counts requests and honours ETags."""

    def __init__(self, feeds: dict[str, str]) -> None:
        self.feeds = feeds
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url not in self.feeds:
            return httpx.Response(404)
        body = self.feeds[url]
        etag = f'"{hash(body) & 0xFFFFFFFF:x}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, text=body, headers={"etag": etag, "content-type": "application/xml"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeLLM:
    """Stands in for every provider. Scores titles with obvious keywords high."""

    HIGH = ("releases", "guidance", "brings")

    def __init__(self, fail: dict[str, list] | None = None) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail = fail or {}  # provider name -> list of exceptions/strings to emit first

    def __call__(self, provider, model, system, user, max_tokens, json_mode, schema, schema_name, use_extra, client, environ=None):
        self.calls.append((provider.name, model, schema_name, json_mode))
        queue = self.fail.get(provider.name)
        if queue:
            nxt = queue.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return LLMResult(text=nxt, input_tokens=100, output_tokens=10)
        if schema_name == "triage":
            ids = re.findall(r"^\[(\d+)\] published", user, re.M)
            titles = re.findall(r"^Title: (.*)$", user, re.M)
            out = []
            for item_id, title in zip(ids, titles):
                low = title.lower()
                level = 9 if any(w in low for w in self.HIGH) else 2
                kind = "policy_action" if "guidance" in low else ("model_release" if "model" in low else "industry_event")
                out.append({"id": item_id, "type": kind, "beat": "gov" if kind == "policy_action" else "ai",
                            "sig": level, "nov": level, "cred": level, "res": level, "act": level, "dup": ""})
            return LLMResult(text=json.dumps({"items": out}), input_tokens=1200, output_tokens=200)
        ids = re.findall(r"^\[(\d+)\] Source", user, re.M)
        stories = [
            {"id": i, "title_en": f"Headline {i}", "summary_en": "Something specific happened. It matters to you.",
             "title_zh": "人工智能软件发布", "summary_zh": "这个模型的数据很好，用户可以在视频里看到。"}
            for i in ids
        ]
        return LLMResult(text=json.dumps({"stories": stories}), input_tokens=900, output_tokens=400)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    shutil.copy(REPO / "config" / "site.toml", root / "config" / "site.toml")
    (root / "config" / "sources.toml").write_text(SOURCES, encoding="utf-8")
    shutil.copytree(REPO / "prompts", root / "prompts")
    monkeypatch.setenv("MICROHOT_DATA", str(root / "data"))
    monkeypatch.setenv("MICROHOT_PUBLIC", str(root / "public"))
    monkeypatch.delenv("SITE_URL", raising=False)
    return load_settings(root)


@pytest.fixture
def feeds():
    return FeedServer(default_feeds())


__all__ = ["NOW", "FakeLLM", "FeedServer", "LLMError", "default_feeds"]
