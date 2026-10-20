import json
import xml.etree.ElementTree as ET
from datetime import timedelta

from conftest import NOW, FakeLLM, FeedServer, default_feeds

from microhot.channels import deliver
from microhot.channels.github_issue import markdown
from microhot.channels.telegram import messages
from microhot.pipeline import run, summary_lines
from microhot.store import Store

ENV = {"GEMINI_API_KEY": "g"}


def _run(project, fake, now=NOW, environ=ENV, server=None):
    server = server or FeedServer(default_feeds(NOW))
    return run(project, now=now, environ=environ, http_client=server.client(), llm_adapter=fake, sleep=lambda s: None)


def test_full_run_picks_writes_and_publishes(project):
    fake = FakeLLM()
    report = _run(project, fake)
    assert report["counts"]["new_items"] == 5
    # One triage call for all four events, one write call for the picked ones.
    kinds = [c[2] for c in fake.calls]
    assert kinds.count("triage") == 1 and kinds.count("stories") == 1
    assert report["counts"]["selected"] == 2 and report["counts"]["written"] == 2
    assert report["llm"]["unconfigured"]["claude"].startswith("not set")

    store = Store(project.data_dir, project.tz)
    events = store.load_events()
    picked = [e for e in events.values() if e.selected_at]
    assert {e.beat for e in picked} == {"ai", "gov"}
    for e in picked:
        assert e.story["title_zh"] == "人工智慧軟體發布"  # Simplified output fixed by the Taiwan guard
        assert "資料" in e.story["summary_zh"] and "使用者" in e.story["summary_zh"]

    # 06:30 Toronto: the morning edition is built.
    assert report["edition"] == {"date": "2026-10-20", "stories": 2, "followups": 0}

    public = project.public_dir
    for page in ["index.html", "zh-tw/index.html", "about.html", "zh-tw/about.html", "editions.html", "d/2026-10-20.html",
                 "zh-tw/d/2026-10-20.html", "feed.xml", "zh-tw/feed.xml", "robots.txt", "_headers", "static/style.css"]:
        assert (public / page).exists(), page
    for e in picked:
        assert (public / "e" / f"{e.id}.html").exists() and (public / "zh-tw" / "e" / f"{e.id}.html").exists()
    zh_index = (public / "zh-tw" / "index.html").read_text(encoding="utf-8")
    assert 'lang="zh-Hant-TW"' in zh_index and "人工智慧軟體發布" in zh_index and "治理與政策" in zh_index
    en_index = (public / "index.html").read_text(encoding="utf-8")
    assert "Headline 1" in en_index and "On the radar" in en_index and "°" in en_index
    ET.fromstring((public / "feed.xml").read_text(encoding="utf-8"))  # well-formed RSS
    assert any("feeds: 3/3 ok" in line for line in summary_lines(report))


def test_second_run_spends_nothing_on_old_items(project):
    fake = FakeLLM()
    server = FeedServer(default_feeds(NOW))
    _run(project, fake, server=server)
    before = len(fake.calls)
    report = _run(project, fake, now=NOW + timedelta(hours=6), server=server)
    assert len(fake.calls) == before
    assert report["counts"]["new_items"] == 0 and report["edition"] is None  # one edition per day


def test_runs_without_any_model_provider(project):
    fake = FakeLLM()
    report = _run(project, fake, environ={})
    assert fake.calls == []
    assert report["llm"]["active"] == {"triage": [], "write": [], "digest": [], "review": []}
    index = (project.public_dir / "index.html").read_text(encoding="utf-8")
    assert "Summaries are off" in index and "Acme releases Nova-2" in index  # radar still shows headlines


def test_quota_limits_stories_per_beat(project):
    project.beats["ai"]["daily_quota"] = 0
    report = _run(project, FakeLLM())
    store = Store(project.data_dir, project.tz)
    picked = [e for e in store.load_events().values() if e.selected_at]
    assert report["counts"]["selected"] == 1 and picked[0].beat == "gov"


def test_delivery_channels(project, monkeypatch):
    _run(project, FakeLLM())
    store = Store(project.data_dir, project.tz)
    edition = store.load_edition("2026-10-20")
    md = markdown(edition, project, ["en", "zh-TW"])
    assert "Headline" in md and "人工智慧軟體發布" in md and "/e/" in md
    texts = messages(edition, project, ["en"], 8)
    assert texts and all(len(t) <= 3800 for t in texts) and "°" in texts[0]

    sent = []
    from microhot.channels import telegram

    monkeypatch.setattr(telegram, "send", lambda ed, s, cfg, env: sent.append(ed["date"]))
    results = deliver(project, store, now=NOW, environ={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
    assert results["telegram"] == "sent" and sent == ["2026-10-20"]
    assert results["email"].startswith("not configured") and results["github_issue"].startswith("not configured")
    again = deliver(project, store, now=NOW, environ={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
    assert again["telegram"] == "already sent" and sent == ["2026-10-20"]
    saved = json.loads((project.data_dir / "editions" / "2026-10-20.json").read_text(encoding="utf-8"))
    assert "telegram" in saved["delivered"]
