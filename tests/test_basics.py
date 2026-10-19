from datetime import timedelta

from conftest import NOW, FeedServer, default_feeds

from microhot.cluster import LexicalEmbedder, assign_events, update_heat
from microhot.fetch import fetch_all
from microhot.textutil import canonical_url, html_to_text, item_id_for, tokens
from microhot.zh import to_taiwan


def test_canonical_url_drops_tracking_and_www():
    a = canonical_url("http://www.Example.com/post/?utm_source=rss&b=2&a=1#frag")
    assert a == "https://example.com/post?a=1&b=2"
    assert item_id_for("https://example.com/post?a=1&b=2") == item_id_for("http://www.example.com/post/?b=2&a=1&utm_medium=x")


def test_html_to_text_strips_markup_and_scripts():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><script>x()</script>") == "Hello world"


def test_tokens_handle_english_and_chinese():
    toks = tokens("OpenAI releases GPT-6 模型")
    assert "openai" in toks and "gpt-6" in toks and "模型" in toks
    assert "openai releas" in toks  # light stemming: releases / released / release


def test_taiwan_guard_converts_simplified_and_keeps_taiwan_forms():
    assert to_taiwan("人工智能软件的数据库") == "人工智慧軟體的資料庫"
    assert to_taiwan("台灣的大數據項目") == "台灣的大數據項目"
    assert to_taiwan("政府公布新規定") == "政府公布新規定"


def test_fetch_filters_dedupes_and_respects_etag(project):
    server = FeedServer(default_feeds())
    state: dict = {}
    items, stats = fetch_all(project, state, {}, NOW, client=server.client())
    titles = {it.title for it in items}
    assert "An old post from last week" not in titles  # older than the first-import window
    assert "Minister announces bridge repair funding" not in titles  # include filter (AI only)
    assert "Canada publishes artificial intelligence guidance for federal departments" in titles
    assert len(items) == 5
    by_source = {s.source_id: s for s in stats}
    assert by_source["gov"].filtered == 1 and by_source["lab"].old == 1
    # Second run: ETags give 304s and nothing is new.
    known = {it.id: it for it in items}
    again, stats2 = fetch_all(project, state, known, NOW + timedelta(hours=6), client=server.client())
    assert again == [] and all(s.status == "not_modified" for s in stats2)


def test_fetch_reports_errors_without_crashing(project):
    server = FeedServer({})
    items, stats = fetch_all(project, {}, {}, NOW, client=server.client())
    assert items == [] and all(s.status == "error" and s.error == "HTTP 404" for s in stats)


def test_clustering_merges_same_story_and_heat_counts_sources(project):
    server = FeedServer(default_feeds())
    items_list, _ = fetch_all(project, {}, {}, NOW, client=server.client())
    items = {it.id: it for it in items_list}
    events: dict = {}
    stats = assign_events(items_list, items, events, project, NOW, LexicalEmbedder(), 0.2)
    nova = [e for e in events.values() if any("Nova-2" in items[i].title for i in e.item_ids)]
    assert len(nova) == 1 and len(nova[0].item_ids) == 2, "the two Nova-2 reports should be one event"
    assert stats["created"] == 4 and stats["joined"] == 1
    update_heat(events, items, project, NOW)
    assert 1.9 < nova[0].heat < 2.0  # two fresh sources, slightly decayed
    assert nova[0].n_sources == 2
