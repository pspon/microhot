"""What was not picked: the radar's window and per-beat slots, the story log, the "Not picked" pages,
the unpicked stories in the morning issue, and the beat and topic suggestions from your ratings."""

from datetime import timedelta

from conftest import NOW, FakeLLM, FeedServer, default_feeds

from microhot.archive import _merge, load_log, update_log
from microhot.feedback import eval_lines, evaluate, margin, thresholds_toml
from microhot.markdown import edition_markdown
from microhot.model import Event, Item
from microhot.pipeline import run
from microhot.radar import found_span, issue_unpicked, radar_by_beat
from microhot.render import SiteBuilder
from microhot.store import Store, read_jsonl
from microhot.textutil import to_iso
from microhot.topics import load_topics

ENV = {"GEMINI_API_KEY": "g"}


def _run(settings, now=NOW, server=None, fake=None):
    server = server or FeedServer(default_feeds(NOW))
    return run(settings, now=now, environ=ENV, http_client=server.client(), llm_adapter=fake or FakeLLM(), sleep=lambda s: None)


def _story(n: int, beat: str, *, fetched_h: float, published_h: float, heat: float = 1.0, score=40, kind="industry_event"):
    item = Item(id=f"{n:04x}", source_id="lab", url=f"https://lab.test/{n}", title=f"Story {n}", snippet="Text.",
                fetched=to_iso(NOW - timedelta(hours=fetched_h)), published=to_iso(NOW - timedelta(hours=published_h)))
    event = Event(id=f"e{item.id}", created=to_iso(item.when()), item_ids=[item.id], rep=item.id, beat=beat,
                  last_seen=to_iso(item.when()), type=kind, score=score, heat=heat)
    return event, item


# ---- the radar ------------------------------------------------------------------------------


def test_radar_counts_from_when_a_story_was_found_and_gives_each_beat_its_slots(project):
    project.radar["per_beat"] = 2
    rows = [
        _story(1, "ca", fetched_h=1, published_h=30, heat=0.4),  # published 30 h ago, found an hour ago
        _story(2, "ai", fetched_h=2, published_h=2, heat=3.0),
        _story(3, "ai", fetched_h=3, published_h=3, heat=2.0),
        _story(4, "ai", fetched_h=4, published_h=4, heat=1.0),  # third in its beat: no slot
        _story(5, "gov", fetched_h=80, published_h=80, heat=0.2),  # found 80 h ago: aged out
        _story(6, "gov", fetched_h=5, published_h=5, heat=0.9, kind="off_topic"),
    ]
    events = {e.id: e for e, _ in rows}
    items = {i.id: i for _, i in rows}
    radar = radar_by_beat(events, items, project, NOW)
    assert {beat: [e.id for e in found] for beat, found in radar.items()} == {"ai": ["e0002", "e0003"], "ca": ["e0001"]}
    assert list(radar) == ["ai", "ca"]  # site order
    # The Canada story counts from when it was found; by its publish time (the old rule) it was already gone.
    assert found_span(events["e0001"], items) == (NOW - timedelta(hours=1), NOW - timedelta(hours=1))


def test_today_page_groups_the_radar_and_links_to_the_archive(project):
    _run(project)
    home = (project.public_dir / "index.html").read_text(encoding="utf-8")
    radar = home[home.index('id="radar-title"'):]
    assert "Customer story: a retailer" in radar and 'href="/radar.html"' in radar
    assert "for 72 hours after the latest report" in radar
    assert 'href="/radar.html"' in home[: home.index("</nav>")]  # in the menu too

    # With stories in more than one beat, each beat gets its own heading.
    rows = [_story(1, "ca", fetched_h=1, published_h=1), _story(2, "ai", fetched_h=1, published_h=1)]
    store = Store(project.data_dir, project.tz)
    SiteBuilder(project, store, {e.id: e for e, _ in rows}, {i.id: i for _, i in rows}, NOW, run_summary={}).build()
    home = (project.public_dir / "index.html").read_text(encoding="utf-8")
    assert home.index("<h3>AI</h3>") < home.index("<h3>Canada &amp; Ontario</h3>")
    zh = (project.public_dir / "zh-tw" / "index.html").read_text(encoding="utf-8")
    assert "<h3>加拿大與安大略省</h3>" in zh and "找到最新一則報導後保留 72 小時" in zh


# ---- the story log --------------------------------------------------------------------------


def test_story_log_keeps_every_story_after_the_window_moves_on(project):
    report = _run(project)
    assert report["log"]["added"] == 4 and report["log"]["files"] == 1
    path = project.data_dir / "log" / "2026-10.jsonl"
    rows = {r["title"]: r for r in read_jsonl(path)}
    assert len(rows) == 4
    picked = [r for r in rows.values() if r["picked"]]
    assert len(picked) == 2 and all(r["editions"] == ["2026-10-20"] and r["title"].startswith("Headline") for r in picked)
    nova = next(r for r in picked if len(r["links"]) == 2)
    assert {link["source"] for link in nova["links"]} == {"Acme Lab Blog", "Tech Daily"}
    retail = rows["Customer story: a retailer uses Nova for support"]
    assert retail["picked"] is None and retail["score"] is not None and retail["threshold"] == 60 and retail["tier"] == "T1"

    # Nothing new six hours later: no file is rewritten.
    assert _run(project, now=NOW + timedelta(hours=6))["log"] == {"added": 0, "updated": 0, "files": 0}

    # Forty days on, the events and items are gone, but the log still has every story.
    later = NOW + timedelta(days=40)
    _run(project, now=later, server=FeedServer({}))
    store = Store(project.data_dir, project.tz)
    assert store.load_events() == {} and not list((project.data_dir / "items").glob("*.jsonl"))
    assert len(load_log(store, project, NOW - timedelta(days=1), later)) == 4


def test_log_rows_only_gain_information():
    old = {"id": "e1", "found": "2026-10-01T10:00:00Z", "last_found": "2026-10-02T10:00:00Z", "title": "Old headline",
           "url": "https://a.test/1", "source": "A", "summary": "Old text.", "links": [{"source": "A", "url": "https://a.test/1"}],
           "n_sources": 2, "temp_max": 38.1, "editions": ["2026-10-02"]}
    new = {**old, "found": "2026-10-02T10:00:00Z", "last_found": "2026-10-03T10:00:00Z", "title": "Other report",
           "url": "https://b.test/2", "source": "B", "summary": "Other text.", "links": [{"source": "B", "url": "https://b.test/2"}],
           "n_sources": 1, "temp_max": 36.9, "editions": [], "_rep_loaded": False, "_written": False}
    row = _merge(old, new)
    assert row["found"] == "2026-10-01T10:00:00Z" and row["last_found"] == "2026-10-03T10:00:00Z"
    assert [link["url"] for link in row["links"]] == ["https://a.test/1", "https://b.test/2"]
    assert row["title"] == "Old headline" and row["url"] == "https://a.test/1" and row["summary"] == "Old text."
    assert row["n_sources"] == 2 and row["temp_max"] == 38.1 and row["editions"] == ["2026-10-02"]


def test_merged_stories_are_marked_in_the_log(project):
    a, item_a = _story(1, "ai", fetched_h=2, published_h=2)
    b, item_b = _story(2, "ai", fetched_h=1, published_h=1)
    store = Store(project.data_dir, project.tz)
    items = {item_a.id: item_a, item_b.id: item_b}
    update_log(store, {a.id: a, b.id: b}, items, project, [], NOW)
    b.merged_into, a.item_ids = a.id, [item_a.id, item_b.id]
    b.item_ids = []
    assert update_log(store, {a.id: a, b.id: b}, items, project, [], NOW)["updated"] == 2
    rows = {r["id"]: r for r in read_jsonl(project.data_dir / "log" / "2026-10.jsonl")}
    assert rows[b.id]["merged_into"] == a.id and len(rows[a.id]["links"]) == 2
    assert [r["id"] for r in load_log(store, project, NOW - timedelta(days=1), NOW)] == [a.id]


# ---- the "Not picked" pages and search ------------------------------------------------------


def test_not_picked_pages_by_day(project):
    _run(project)
    public = project.public_dir
    index = (public / "radar.html").read_text(encoding="utf-8")
    assert "/r/2026-10-20.html" in index and "2 stories" in index
    day = (public / "r" / "2026-10-20.html").read_text(encoding="utf-8")
    assert "Customer story: a retailer uses Nova for support" in day and "Weekly roundup" in day
    assert "Headline" not in day  # picks live in the editions
    assert " of 60" in day and "Industry news" in day and 'id="s-e' in day
    zh = (public / "zh-tw" / "r" / "2026-10-20.html").read_text(encoding="utf-8")
    assert "未入選" in zh and "／60 分" in zh
    assert (public / "zh-tw" / "radar.html").exists()


# ---- the morning issue ----------------------------------------------------------------------


def test_each_unpicked_story_is_offered_in_one_issue_only(project):
    _run(project)
    store = Store(project.data_dir, project.tz)
    assert len(store.load_edition("2026-10-20")["unpicked"]) == 2
    _run(project, now=NOW + timedelta(hours=24))  # the next morning, nothing new found
    assert store.load_edition("2026-10-21")["unpicked"] == []


def test_issue_offers_the_closest_and_the_warmest_per_beat(project):
    project.raw["feedback"]["unpicked_per_beat"] = 2
    rows = [
        _story(1, "ai", fetched_h=1, published_h=1, heat=0.2, score=58),  # closest to the bar (60)
        _story(2, "ai", fetched_h=2, published_h=2, heat=0.1, score=50),
        _story(3, "ai", fetched_h=3, published_h=3, heat=4.0, score=20),  # warmest
        _story(4, "ai", fetched_h=30, published_h=30, heat=9.0, score=59),  # found before the last edition
        _story(5, "gov", fetched_h=2, published_h=2, heat=1.0, score=None),  # not scored yet
    ]
    events = {e.id: e for e, _ in rows}
    items = {i.id: i for _, i in rows}
    offered = issue_unpicked(events, items, project, NOW - timedelta(hours=24), [])
    assert [r["id"] for r in offered] == ["e0001", "e0003"]
    assert offered[0]["score"] == 58 and offered[0]["threshold"] == 60 and offered[0]["tier"] == "T1"


def test_editions_from_before_keep_their_near_miss_boxes(project):
    edition = {"date": "2026-10-19", "built_at": to_iso(NOW), "stories": [], "followups": [],
               "near_misses": [{"id": "eabc", "title": "Old near miss", "url": "https://x.test", "score": 58, "threshold": 60}]}
    md = edition_markdown(edition, project, "en", checkboxes=True)
    assert "<!-- mh:eabc:want -->" in md and "Not picked since" not in md


# ---- suggestions ----------------------------------------------------------------------------


def _label(n, beat, raw, label, *, kind="miss", topics=(), tier="T1", threshold=None, score=None):
    return {"event": f"e{n:04x}", "date": "2026-10-20", "label": label, "kind": kind, "raw": raw,
            "score": raw if score is None else score, "boost": 0, "boost_why": [], "threshold": threshold,
            "tier": tier, "beat": beat, "type": "policy_action", "axes": {}, "title": f"Story {n}", "topics": list(topics)}


def _labels(project):
    ca = [  # Canada & Ontario, bar 55 for T1: wanted down to 52, unwanted up to 49
        _label(1, "ca", 52, 1, topics=["ontario"], threshold=55),
        _label(2, "ca", 53, 1, topics=["ontario"], threshold=55),
        _label(3, "ca", 56, 1, kind="pick", threshold=55),
        _label(4, "ca", 60, 1, kind="pick", threshold=55),
        _label(5, "ca", 65, 1, kind="pick", threshold=55),
        _label(6, "ca", 58, 1, threshold=55),  # cleared the bar but the day's quota was full
        _label(7, "ca", 59, 1, threshold=55),
        _label(8, "ca", 40, 0, threshold=55),
        _label(9, "ca", 45, 0, threshold=55),
        _label(10, "ca", 49, 0, threshold=55),
    ]
    ai = [  # AI, bar 60: Ontario stories you wanted sit well below it; too few 👎 for a beat suggestion
        _label(11, "ai", 54, 1, topics=["ontario"], threshold=60),
        _label(12, "ai", 53, 1, topics=["ontario"], threshold=60),
        _label(13, "ai", 55, 1, topics=["ontario"], threshold=60),
        _label(14, "ai", 64, 1, kind="pick", threshold=60),
        _label(15, "ai", 54, 0, topics=["us-policy"], threshold=60),
        _label(16, "ai", 53, 0, threshold=60),
    ]
    Store(project.data_dir, project.tz).write_jsonl(project.data_dir / "labels.jsonl", ca + ai)


def test_suggestions_per_beat_and_topic_against_the_current_config(project):
    _labels(project)
    store = Store(project.data_dir, project.tz)
    result = evaluate(project, store)
    beats = {b["beat"]: b for b in result["beats"]}

    ca = beats["ca"]
    assert ca["ready"] and ca["shift"] == -3 and ca["f1"] == 1.0  # the smallest move that keeps every 👍 and drops every 👎
    assert ca["suggested"] == {"T1": 52, "T1.5": 57, "T2": 65} and ca["quota_cut"] == 2
    assert ca["toml"] == 'thresholds = { T1 = 52, "T1.5" = 57, T2 = 65 }'
    assert not beats["ai"]["ready"] and beats["ai"]["unwanted"] == 2

    # On top of that, Ontario stories need about 7 more points to get in.
    assert [(t["topic"], t["change"], t["points"]) for t in result["topics"]] == [("ontario", 7, 7)]
    assert result["topics"][0]["toml"] == '{ topic = "ontario", points = 7 },'

    lines = eval_lines(result)
    assert any("beat ca: 10 ratings (7 👍, 3 👎); lower its thresholds by 3" in line for line in lines)
    assert '  [beats.ca] thresholds = { T1 = 52, "T1.5" = 57, T2 = 65 }' in lines
    assert any("cleared the bar after the day's 5 picks" in line for line in lines)
    assert any(line.startswith('  { topic = "ontario", points = 7 },') for line in lines)

    # The About page shows them, ready to paste.
    events = store.load_events()
    SiteBuilder(project, store, events, {}, NOW, run_summary={}, topics=load_topics(project)).build()
    about = (project.public_dir / "about.html").read_text(encoding="utf-8")
    assert "Lower its thresholds by 3" in about and "[beats.ca] thresholds = { T1 = 52, &#34;T1.5&#34; = 57, T2 = 65 }" in about
    assert "{ topic = &#34;ontario&#34;, points = 7 }," in about and "Ontario" in about
    zh = (project.public_dir / "zh-tw" / "about.html").read_text(encoding="utf-8")
    assert "建議門檻降低 3 分" in zh and "主題加權" in zh

    # Apply both, and the suggestions are no longer made.
    project.beats["ca"]["thresholds"] = ca["suggested"]
    project.raw["adjust"]["boost"] = [{"topic": "ontario", "points": 7}]
    again = evaluate(project, store)
    assert {b["beat"]: b["shift"] for b in again["beats"]} == {"ai": 0, "ca": 0} and again["topics"] == []
    assert margin(_label(1, "ca", 52, 1, topics=["ontario"]), project) == 7  # 52 + 7 boost - 52


def test_ratings_saved_before_this_change_still_count(project):
    old = [{"event": f"e{n:04x}", "date": "2026-10-20", "label": n % 2, "kind": "pick", "score": 50 + n, "threshold": 60,
            "tier": "T1", "beat": "ai", "type": "model_release", "axes": {}, "title": "x"} for n in range(12)]
    Store(project.data_dir, project.tz).write_jsonl(project.data_dir / "labels.jsonl", old)
    result = evaluate(project, Store(project.data_dir, project.tz))
    ai = next(b for b in result["beats"] if b["beat"] == "ai")
    assert ai["labels"] == 12 and ai["ready"] and margin(old[0], project) == -10


def test_thresholds_toml_quotes_the_dotted_tier():
    assert thresholds_toml({"T1": 50, "T1.5": 55, "T2": 63}) == 'thresholds = { T1 = 50, "T1.5" = 55, T2 = 63 }'
