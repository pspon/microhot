"""The AIHOT-inspired features: heat markers, follow-ups, topics, boost/mute, search and feeds,
weekly/monthly reviews, overviews for big stories, ratings and failure alerts."""

import json
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx
import pytest
from conftest import NOW, FakeLLM, FeedServer, default_feeds, rss

from microhot import alerts
from microhot.__main__ import main
from microhot.channels import deliver, telegram
from microhot.channels.github_issue import edition_body
from microhot.config import load_settings
from microhot.feedback import collect_labels, eval_lines, evaluate, parse_labels
from microhot.model import Event
from microhot.pipeline import run
from microhot.reviews import due_reviews
from microhot.store import Store, read_jsonl
from microhot.textutil import to_iso
from microhot.topics import load_topics, validate_rules
from microhot.views import badges, temp_change

ENV = {"GEMINI_API_KEY": "g"}


def _run(project, fake=None, now=NOW, environ=ENV, server=None):
    server = server or FeedServer(default_feeds(NOW))
    fake = fake or FakeLLM()
    return run(project, now=now, environ=environ, http_client=server.client(), llm_adapter=fake, sleep=lambda s: None)


def _picked(project):
    store = Store(project.data_dir, project.tz)
    return {e.id: e for e in store.load_events().values() if e.selected_at}


# ---- hot markers -------------------------------------------------------------------------


def test_hot_markers_and_temperature_change():
    six_hours_ago = to_iso(NOW - timedelta(hours=6))
    spiking = Event(id="e1", created=to_iso(NOW - timedelta(hours=2)), heat=3.0, sources_48h=4, sources_6h=3,
                    heat_prev=1.0, heat_prev_at=six_hours_ago)
    assert badges(spiking, NOW) == ["spiking", "new"]  # spiking already says it is warming
    assert temp_change(spiking, NOW) == 1.2  # 38.3° now, 37.1° at the previous run

    warming = Event(id="e2", created=to_iso(NOW - timedelta(days=1)), heat=2.0, sources_48h=3, heat_prev=1.5,
                    heat_prev_at=six_hours_ago)
    assert badges(warming, NOW) == ["warming"]

    stale = Event(id="e3", created=to_iso(NOW - timedelta(days=1)), heat=2.0, heat_prev=1.0,
                  heat_prev_at=to_iso(NOW - timedelta(days=2)))
    assert badges(stale, NOW) == [] and temp_change(stale, NOW) is None  # too long ago to compare
    assert badges(spiking, None) == []


# ---- follow-ups and richer editions --------------------------------------------------------

MORE_SOURCES = """
[[source]]
id = "wire"
name = "Newswire"
url = "https://wire.test/rss"
beat = "ai"
tier = "T2"

[[source]]
id = "blog2"
name = "Dev Weekly"
url = "https://blog2.test/rss"
beat = "ai"
tier = "T2"
"""


def test_followups_reach_the_next_edition(project):
    _run(project)
    nova = next(e for e in _picked(project).values() if e.beat == "ai")

    # Next morning, two more outlets cover the Nova-2 story.
    with open(project.root / "config" / "sources.toml", "a", encoding="utf-8") as fh:
        fh.write(MORE_SOURCES)
    settings = load_settings(project.root)
    day2 = NOW + timedelta(hours=24)
    feeds = default_feeds(NOW)
    feeds["https://wire.test/rss"] = rss([("Acme Nova-2 model with 1M-token context window now open to developers",
                                           "https://wire.test/nova-2", day2 - timedelta(hours=2),
                                           "Acme's Nova-2 model and its 1M-token context window are now in the API.")])
    feeds["https://blog2.test/rss"] = rss([("Hands-on: Acme Nova-2 and its 1M-token context window",
                                            "https://blog2.test/nova-2", day2 - timedelta(hours=3),
                                            "We tried Acme's Nova-2 model with a 1M-token context window.")])
    report = _run(settings, now=day2, server=FeedServer(feeds))
    assert report["edition"] == {"date": "2026-10-21", "stories": 0, "followups": 1}

    store = Store(settings.data_dir, settings.tz)
    edition = store.load_edition("2026-10-21")
    follow = edition["followups"][0]
    assert follow["id"] == nova.id and follow["followup"]["previous"] == "2026-10-20"
    assert {link["source"] for link in follow["followup"]["new"]} == {"Newswire", "Dev Weekly"}
    assert edition["stats"]["followups"] == 1
    event = store.load_events()[nova.id]
    assert event.editions == ["2026-10-20", "2026-10-21"] and event.heat_prev is not None

    # Markdown copies in the data repo, and the edition page with its follow-up section.
    md = (settings.data_dir / "digest" / "2026-10-21.md").read_text(encoding="utf-8")
    assert "Follow-ups" in md and "Newswire" in md and "---" in md
    assert (settings.data_dir / "digest" / "latest.md").read_text(encoding="utf-8") == md
    page = (settings.public_dir / "d" / "2026-10-21.html").read_text(encoding="utf-8")
    assert "Newswire" in page and "2026-10-20.html" in page  # links back to the earlier edition
    first = store.load_edition("2026-10-20")
    assert first["lead"] in {s["id"] for s in first["stories"]} and first["stats"]["stories"] == 2


# ---- topics, boost and mute ----------------------------------------------------------------


def test_mute_boost_and_topic_pages(project):
    project.raw["adjust"].update(mute=["weekly roundup"], max_points=50,
                                 boost=[{"pattern": "retailer", "points": 50, "label": "retail"}])
    report = _run(project)
    assert sum(f["muted"] for f in report["fetch"]) == 1 and report["counts"]["new_items"] == 4
    assert report["counts"]["selected"] == 3  # the boosted customer story clears its threshold

    retail = next(e for e in _picked(project).values() if e.adjust)
    assert retail.adjust == {"points": 50, "why": ["retail"]}
    store = Store(project.data_dir, project.tz)
    stories = {s["id"]: s for s in store.load_edition("2026-10-20")["stories"]}
    assert stories[retail.id]["boost"] == 50 and stories[retail.id]["effective_score"] == retail.score + 50

    public = project.public_dir
    canada = (public / "t" / "canada.html").read_text(encoding="utf-8")
    assert "Headline" in canada  # the federal guidance story
    assert "canada.html" in (public / "topics.html").read_text(encoding="utf-8")
    assert (public / "zh-tw" / "t" / "canada.html").exists()

    project.raw["adjust"]["boost"] = [{"topic": "no-such-topic", "points": 5}]
    with pytest.raises(ValueError):
        validate_rules(project, load_topics(project))


# ---- search, last visit, feeds -------------------------------------------------------------


def test_search_index_visit_markers_and_feeds(project):
    _run(project)
    public = project.public_dir
    index = json.loads((public / "search.json").read_text(encoding="utf-8"))
    assert len(index) == 2 and all(row["u"].startswith("/e/") and len(row["t"]) == 2 for row in index)
    assert any("人工智慧" in row["t"][1] for row in index)
    for lang in ("", "zh-tw/"):
        assert (public / f"{lang}search.html").exists()
        for feed in ("feed.xml", "feed/ai.xml", "feed/gov.xml", "feed/editions.xml"):
            ET.fromstring((public / f"{lang}{feed}").read_text(encoding="utf-8"))
    home = (public / "index.html").read_text(encoding="utf-8")
    assert "data-picked=" in home and "visit.js" in home
    assert (public / "static" / "visit.js").exists() and (public / "static" / "search.js").exists()
    assert (public / "d" / "2026-10-20.md").exists()


# ---- overviews for big stories -------------------------------------------------------------


def test_overview_for_a_story_with_several_sources(project):
    project.raw["digest"]["min_sources"] = 2  # the Nova-2 story has two sources
    fake = FakeLLM()
    report = _run(project, fake)
    assert report["digest"]["written"] == 1 and [c[2] for c in fake.calls].count("digest") == 1
    nova = next(e for e in _picked(project).values() if e.digest)
    assert nova.digest["title_zh"] == "Nova-2 登場" and "開發者" in nova.digest["text_zh"]  # Taiwan guard
    assert nova.digest["n_sources"] == 2
    page = (project.public_dir / "e" / f"{nova.id}.html").read_text(encoding="utf-8")
    assert "Developers can use it today." in page and "Nova-2 arrives" in page

    _run(project, fake, now=NOW + timedelta(hours=6))
    assert [c[2] for c in fake.calls].count("digest") == 1  # no new sources, no new overview


# ---- weekly and monthly reviews ------------------------------------------------------------


def test_weekly_review_from_the_editions(project, monkeypatch):
    fake = FakeLLM()
    server = FeedServer(default_feeds(NOW))
    _run(project, fake, server=server)  # Tuesday 2026-10-20: the first edition
    monday = NOW + timedelta(days=6)  # 06:30 on Monday 2026-10-26
    report = _run(project, fake, now=monday, server=server)
    assert report["reviews"] == [{"id": "2026-W43", "stories": 2, "overview": True}]
    assert [c[2] for c in fake.calls].count("review") == 1

    store = Store(project.data_dir, project.tz)
    review = store.load_review("2026-W43")
    assert review["start"] == "2026-10-19" and review["end"] == "2026-10-25" and review["editions"] == 1
    assert review["overview"]["en"].startswith("A week of") and "這" in review["overview"]["zh-TW"]
    assert due_reviews(project, store.load_state(), monday) == []  # built once
    md = (project.data_dir / "digest" / "2026-W43.md").read_text(encoding="utf-8")
    assert "A week of model releases" in md and "Headline" in md

    public = project.public_dir
    assert (public / "w" / "2026-W43.html").exists() and (public / "zh-tw" / "w" / "2026-W43.html").exists()
    assert "/w/2026-W43.html" in (public / "editions.html").read_text(encoding="utf-8")
    assert "2026-W43" in (public / "feed" / "editions.xml").read_text(encoding="utf-8")

    sent = []
    monkeypatch.setattr(telegram, "send_review", lambda r, s, cfg, env: sent.append(r["id"]))
    results = deliver(project, store, now=monday, environ={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
    assert results["weekly telegram"] == "sent" and sent == ["2026-W43"]
    assert results["telegram"] == "skipped (empty)"  # Monday's own edition has nothing new


# ---- ratings and eval ----------------------------------------------------------------------


def _tick(body: str, event_id: str, which: str) -> str:
    marker = f"<!-- mh:{event_id}:{which} -->"
    lines = [line.replace("- [ ]", "- [x]", 1) if marker in line else line for line in body.splitlines()]
    return "\n".join(lines)


def test_ratings_from_issue_checkboxes(project):
    _run(project)
    store = Store(project.data_dir, project.tz)
    edition = store.load_edition("2026-10-20")
    body = edition_body(edition, project, ["en", "zh-TW"])
    a, b = (s["id"] for s in edition["stories"])
    miss = edition["near_misses"][0]["id"]
    assert body.count("<!-- mh:") == 4 + len(edition["near_misses"]) and "<!-- mh-edition:2026-10-20 -->" in body

    ticked = _tick(_tick(_tick(body, a, "up"), b, "down"), miss, "want")
    assert parse_labels(ticked) == {a: {"label": 1, "kind": "pick"}, b: {"label": 0, "kind": "pick"},
                                    miss: {"label": 1, "kind": "miss"}}

    issue = {"number": 1, "body": ticked}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/me/microhot/issues" and request.url.params["labels"] == "edition"
        return httpx.Response(200, json=[issue])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "me/microhot"}
    result = collect_labels(project, store, env, now=NOW, client=client)
    assert result["labels"] == 3 and result["changed"] == 3
    rows = read_jsonl(project.data_dir / "labels.jsonl")
    assert {r["event"] for r in rows} == {a, b, miss} and all(r["score"] is not None for r in rows)

    scores = evaluate(project, store)
    assert scores["picks_rated"] == 2 and scores["precision"] == 0.5 and scores["misses_wanted"] == 1
    assert any("precision of picks: 50%" in line for line in eval_lines(scores))
    project.raw["feedback"]["min_labels"] = 3
    assert evaluate(project, store)["suggestions"]["T1"]["suggested"] is not None

    issue["body"] = _tick(body, a, "up")  # the 👎 and the near-miss tick were removed
    assert collect_labels(project, store, env, now=NOW, client=client)["labels"] == 1
    assert collect_labels(project, store, {}, now=NOW)["status"].startswith("skipped")

    # The About page reports the ratings.
    _run(project, now=NOW + timedelta(hours=6))
    assert "1 of 1 rated picks were useful (100%)" in (project.public_dir / "about.html").read_text(encoding="utf-8")


def test_issue_target_prefers_data_repo():
    from microhot.channels import github_issue

    both = {"DATA_REPO": "me/data", "DATA_TOKEN": "dt", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}
    assert github_issue.issue_target(both) == ("me/data", "dt")
    assert github_issue.issue_target({"GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}) == ("me/code", "gt")
    assert github_issue.issue_target({"DATA_REPO": "", "DATA_TOKEN": "", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}) == ("me/code", "gt")
    # A half-set DATA pair never falls back to the code repo.
    half = {"DATA_REPO": "me/data", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}
    assert github_issue.issue_target(half) == ("me/data", "")
    assert github_issue.missing(half) == ["DATA_TOKEN"]
    assert github_issue.missing(both) == [] and github_issue.missing({"GITHUB_REPOSITORY": "a/b", "GITHUB_TOKEN": "t"}) == []
    assert github_issue.missing({})
    # In Actions there is no fallback to the code repo, whatever is set.
    ci = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}
    assert github_issue.issue_target(ci) == ("", "") and github_issue.missing(ci) == ["DATA_REPO", "DATA_TOKEN"]
    assert github_issue.issue_target({**ci, "DATA_REPO": "me/data", "DATA_TOKEN": " dt\n"}) == ("me/data", "dt")


def test_safe_error_hides_urls():
    from microhot.textutil import safe_error

    req = httpx.Request("GET", "https://api.github.com/repos/me/data/issues")
    status = httpx.HTTPStatusError("boom", request=req, response=httpx.Response(404, request=req))
    assert safe_error(status) == "HTTPStatusError: HTTP 404"
    assert "me/data" not in safe_error(httpx.ConnectError("failed for https://api.github.com/repos/me/data/issues now"))


def test_issue_channel_posts_to_data_repo(monkeypatch):
    from microhot.channels import github_issue

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200 if request.method == "GET" else 201, json=[] if request.method == "GET" else {})

    real = httpx.Client
    monkeypatch.setattr(github_issue.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    env = {"DATA_REPO": "me/data", "DATA_TOKEN": "dt", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}
    from types import SimpleNamespace

    github_issue.send_alert("hello", SimpleNamespace(site={"name": "MicroHOT"}), env)
    assert calls and all(path.startswith("/repos/me/data/") and auth == "Bearer dt" for _, path, auth in calls)
    assert any(m == "POST" and p == "/repos/me/data/issues" for m, p, _ in calls)


def test_ratings_read_from_data_repo(project):
    _run(project)
    store = Store(project.data_dir, project.tz)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {"DATA_REPO": "me/data", "DATA_TOKEN": "dt", "GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}
    assert collect_labels(project, store, env, now=NOW, client=client)["status"] == "ok"
    assert seen == [("/repos/me/data/issues", "Bearer dt")]
    # Fallback to the workflow's own repo when DATA_* is not set.
    seen.clear()
    collect_labels(project, store, {"GITHUB_REPOSITORY": "me/code", "GITHUB_TOKEN": "gt"}, now=NOW, client=client)
    assert seen == [("/repos/me/code/issues", "Bearer gt")]
    # A DATA_REPO without DATA_TOKEN is skipped, not sent to the code repo.
    assert collect_labels(project, store, {"DATA_REPO": "me/data", "GITHUB_TOKEN": "gt"}, now=NOW, client=client)["status"].startswith("skipped")


# ---- failure alerts ------------------------------------------------------------------------


def test_feed_failing_three_runs_raises_one_alert(project):
    feeds = default_feeds(NOW)
    del feeds["https://gov.test/rss"]  # 404 from now on
    server = FeedServer(feeds)
    counts = [_run(project, now=NOW + timedelta(hours=6 * i), server=server)["alerts"] for i in range(4)]
    assert counts == [0, 0, 1, 0]  # queued once, on the third failure in a row
    state = Store(project.data_dir, project.tz).load_state()
    assert state["alerts"]["pending"][0]["key"] == "feed:gov"


def test_alert_queue_and_delivery(project, monkeypatch):
    state = {"feeds": {}}
    report = {"llm": {"disabled": {"gemini": "auth error 401"}, "calls": 2, "ok_calls": 0, "errors": ["x"]},
              "triage": {"pending": 3}, "write": {}}
    assert alerts.detect(project, state, report, NOW) == 2  # provider off, and nothing worked
    assert alerts.detect(project, state, report, NOW + timedelta(hours=6)) == 0  # once a day

    sent = []
    monkeypatch.setattr(telegram, "send_alert", lambda text, s, env: sent.append(text))
    results = alerts.send_pending(project, state, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
    assert results == {"github_issue": "not configured", "telegram": "sent"}
    assert "gemini" in sent[0] and state["alerts"]["pending"] == []
    assert alerts.send_pending(project, state, {}) == {}


# ---- command line --------------------------------------------------------------------------


def test_cli_status_and_eval(project, capsys):
    assert main(["--root", str(project.root), "status"]) == 0
    out = capsys.readouterr().out
    assert "digest chain" in out and "review chain" in out and "topics: 31" in out
    assert main(["--root", str(project.root), "eval"]) == 0
    assert "labels: 0" in capsys.readouterr().out
