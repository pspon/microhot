"""The Canada & Ontario beat: configurable beats with their own thresholds, news-search feeds,
page sources for sites without a feed, tracked bill stages, "why it matters" notes and source checks."""

import json
import re
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
from conftest import NOW, FakeLLM, rss

from microhot.channels.email import build as email_build
from microhot.channels.telegram import fair_share, messages
from microhot.config import load_settings
from microhot.fetch import check_sources, fetch_all, page_links, unwrap_link
from microhot.llm.adapters import LLMResult
from microhot.markdown import edition_markdown
from microhot.model import Event, Item
from microhot.pipeline import run
from microhot.store import Store
from microhot.triage import _apply

ENV = {"GEMINI_API_KEY": "g"}
H = lambda n: NOW - timedelta(hours=n)

CANADA_SOURCES = """
[[source]]
id = "lab"
name = "Acme Lab Blog"
url = "https://lab.test/feed.xml"
beat = "ai"
tier = "T1"

[[source]]
id = "on-news"
name = "Ontario News (AI only)"
url = "https://on.test/rss"
beat = "ca"
tier = "T1"
include = ['artificial intelligence', '\\bA\\.?I\\b']

[[source]]
id = "news-search"
name = "News search"
kind = "search"
url = "https://search.test/rss"
beat = "ca"
tier = "T2"
include = ['artificial intelligence', '\\bA\\.?I\\b']

[[source]]
id = "regulator"
name = "Privacy Regulator"
kind = "page"
url = "https://reg.test/news"
link_pattern = '/news/[a-z0-9-]+$'
fetch_text = true
beat = "ca"
tier = "T1"
"""


class SiteServer:
    """Serves feeds and HTML pages, each with its own content type."""

    def __init__(self, routes: dict[str, tuple[str, str]]) -> None:
        self.routes = routes  # url -> (content type, body)
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url not in self.routes:
            return httpx.Response(404)
        ctype, body = self.routes[url]
        return httpx.Response(200, text=body, headers={"content-type": ctype})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def search_rss(items: list[tuple[str, str, str]]) -> str:
    """A news-search feed: Bing-style redirect links and a <source> per item."""
    body = "".join(
        f"<item><title>{title}</title>"
        f"<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url={httpx.URL(link)}&amp;c=1</link>"
        f"<pubDate>{format_datetime(H(2))}</pubDate><description>{title}. Ontario's ministry said more.</description>"
        f'<source url="https://{publisher.lower().replace(" ", "")}.test">{publisher}</source></item>'
        for title, link, publisher in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>s</title>{body}</channel></rss>'


def news_page(links: list[tuple[str, str]]) -> str:
    rows = "".join(f'<li><a href="{href}">{text}</a> <a href="{href}">Read more</a></li>' for href, text in links)
    return f"<html><body><nav><a href='/about'>About us</a></nav><ul>{rows}</ul><a href='/news?page=2'>Next</a></body></html>"


def article(title: str, description: str, published: str) -> str:
    return (
        f'<html><head><title>{title} | Regulator</title><meta property="og:description" content="{description}">'
        f'<meta property="article:published_time" content="{published}"></head>'
        f"<body><nav><p>Skip to the menu of this website, which is long enough to count.</p></nav>"
        f"<p>The regulator also said the guidance applies to every provincial institution from next year.</p></body></html>"
    )


def base_routes() -> dict[str, tuple[str, str]]:
    return {
        "https://lab.test/feed.xml": ("application/rss+xml", rss([
            ("Acme releases Nova-2 model", "https://lab.test/nova-2", H(2), "Acme released Nova-2 in the API."),
        ])),
        "https://on.test/rss": ("application/rss+xml", rss([
            ("Ontario releases artificial intelligence guidance for ministries", "https://on.test/ai-guidance", H(3),
             "Ontario's new guidance requires ministries to test AI systems before use."),
            ("Ontario opens a new bridge", "https://on.test/bridge", H(3), "A bridge."),
        ])),
        "https://search.test/rss": ("application/rss+xml", search_rss([
            ("Ontario releases AI guidance for ministries - Global News", "https://globalnews.test/ai-guidance", "Global News on MSN"),
            ("Toronto hospital network adopts AI scribes in emergency rooms", "https://cbc.test/ai-scribes", "CBC"),
            ("Hockey night in Ontario", "https://cbc.test/hockey", "CBC"),
        ])),
        "https://reg.test/news": ("text/html; charset=utf-8", news_page([
            ("/news/annual-report-2026", "Regulator releases its 2026 annual report"),
            ("/news/transparency-challenge", "Regulator launches the transparency challenge"),
        ])),
    }


@pytest.fixture
def canada(project):
    (project.root / "config" / "sources.toml").write_text(CANADA_SOURCES, encoding="utf-8")
    settings = load_settings(project.root)
    settings.fetch["same_host_pause_s"] = 0
    return settings


class CanadaLLM(FakeLLM):
    """Puts Ontario stories in the Canada beat and writes a why-it-matters note when asked."""

    def __call__(self, provider, model, system, user, max_tokens, json_mode, schema, schema_name, use_extra, client, environ=None):
        if schema_name == "triage":
            ids = re.findall(r"^\[(\d+)\] published", user, re.MULTILINE)
            titles = re.findall(r"^Title: (.*)$", user, re.MULTILINE)
            out = []
            for item_id, title in zip(ids, titles):
                low = title.lower()
                ontario = any(w in low for w in ("ontario", "toronto", "regulator"))
                level = 9 if ("releases" in low or "guidance" in low) else (7 if ontario else 2)
                kind = "policy_action" if ontario else "model_release"
                out.append({"id": item_id, "type": kind, "beat": "ca" if ontario else "ai",
                            "sig": level, "nov": level, "cred": level, "res": level, "act": level, "dup": ""})
            self.calls.append((provider.name, model, schema_name, json_mode))
            return LLMResult(text=json.dumps({"items": out}), input_tokens=1000, output_tokens=200)
        if schema_name == "stories":
            self.calls.append((provider.name, model, schema_name, json_mode))
            blocks = re.findall(r"^\[(\d+)\] Source.*?^Why it matters: (yes|no)$", user, re.MULTILINE | re.DOTALL)
            stories = [
                {"id": i, "title_en": f"Headline {i}", "summary_en": "Something specific happened.",
                 "title_zh": "安大略省發布指引", "summary_zh": "安大略省發布新的人工智慧指引。",
                 "why_en": "Models used in provincial programs will need documented testing before launch." if want == "yes" else "",
                 "why_zh": "省府計畫使用的模型上線前須留下測試紀錄。" if want == "yes" else ""}
                for i, want in blocks
            ]
            return LLMResult(text=json.dumps({"stories": stories}), input_tokens=900, output_tokens=400)
        return super().__call__(provider, model, system, user, max_tokens, json_mode, schema, schema_name, use_extra, client, environ)


def _run(settings, server, fake=None, now=NOW):
    return run(settings, now=now, environ=ENV, http_client=server.client(), llm_adapter=fake or CanadaLLM(), sleep=lambda s: None)


# ---- configuration -------------------------------------------------------------------------


def test_beats_come_from_config(canada, project):
    assert canada.beat_order == ["ai", "ca", "gov"]
    assert canada.threshold_for("T2", "ca") == 68 and canada.threshold_for("T2", "ai") == 76
    assert canada.threshold_for("T1.5", "gov") == 65
    assert canada.writes_why("ca") and not canada.writes_why("ai")

    (project.root / "config" / "sources.toml").write_text(
        '[[source]]\nid = "x"\nname = "X"\nurl = "https://x.test/rss"\nbeat = "sports"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="beat must be one of"):
        load_settings(project.root)
    (project.root / "config" / "sources.toml").write_text(
        '[[source]]\nid = "x"\nname = "X"\nurl = "https://x.test/"\nkind = "page"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="link_pattern"):
        load_settings(project.root)


def test_require_include_and_exclude_rules(canada):
    src = canada.source("on-news")
    src.require, src.exclude = ["Ontario"], ["bridge"]
    assert src.matches("Ontario adopts an artificial intelligence policy")
    assert not src.matches("Quebec adopts an artificial intelligence policy")  # require
    assert not src.matches("Ontario uses AI to inspect a bridge")  # exclude
    assert not src.matches("Ontario budget")  # include


# ---- news-search feeds -----------------------------------------------------------------------


def test_unwrap_redirect_links():
    wrapped = "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&url=https%3a%2f%2fnews.test%2fa%3fid%3d1&c=2"
    assert unwrap_link(wrapped) == "https://news.test/a?id=1"
    assert unwrap_link("https://www.google.com/url?rct=j&sa=t&url=https://news.test/b&ct=ga") == "https://news.test/b"
    assert unwrap_link("https://news.test/c") == "https://news.test/c"


def test_search_feed_keeps_publishers_and_counts_each_as_a_source(canada):
    server = SiteServer(base_routes())
    items, _ = fetch_all(canada, {}, {}, NOW, client=server.client())
    found = {it.url: it for it in items if it.source_id == "news-search"}
    assert set(found) == {"https://globalnews.test/ai-guidance", "https://cbc.test/ai-scribes"}  # hockey filtered out
    gn = found["https://globalnews.test/ai-guidance"]
    assert gn.publisher == "Global News" and gn.title == "Ontario releases AI guidance for ministries"
    assert gn.origin == "pub:global news" and found["https://cbc.test/ai-scribes"].origin == "pub:cbc"


def test_one_search_feed_can_make_a_story_warm(canada):
    report = _run(canada, SiteServer(base_routes()))
    assert report["counts"]["new_items"] == 4  # lab, Ontario guidance, two search results; the page is a baseline
    events = Store(canada.data_dir, canada.tz).load_events()
    items = Store(canada.data_dir, canada.tz).load_items(NOW, 30)
    guidance = next(e for e in events.values() if any("guidance" in items[i].title.lower() for i in e.item_ids if i in items))
    sources = {items[i].origin for i in guidance.item_ids}
    assert {"on-news", "pub:global news"} <= sources
    assert guidance.n_sources == len(sources) >= 2  # an outlet in a search feed is its own source


# ---- page sources ----------------------------------------------------------------------------


def test_page_links_picks_news_links_once_with_their_best_text():
    html = news_page([("/news/a-story", "A real headline here"), ("https://reg.test/news/b-story#top", "Another headline")])
    links = page_links(html, "https://reg.test/news", "/news/[a-z0-9-]+$")
    assert links == [("https://reg.test/news/a-story", "A real headline here"), ("https://reg.test/news/b-story", "Another headline")]


def test_page_source_records_a_baseline_then_reports_new_links(canada):
    routes = base_routes()
    server = SiteServer(routes)
    state: dict = {}
    items, stats = fetch_all(canada, state, {}, NOW, client=server.client())
    page = {s.source_id: s for s in stats}["regulator"]
    assert page.status == "baseline" and page.entries == 2 and not [i for i in items if i.source_id == "regulator"]

    routes["https://reg.test/news"] = ("text/html", news_page([
        ("/news/ai-guidance-for-institutions", "Regulator issues AI guidance for public institutions"),
        ("/news/annual-report-2026", "Regulator releases its 2026 annual report"),
        ("/news/transparency-challenge", "Regulator launches the transparency challenge"),
    ]))
    routes["https://reg.test/news/ai-guidance-for-institutions"] = (
        "text/html", article("AI guidance for institutions", "New guidance tells institutions how to assess AI systems.", "2026-10-20T09:00:00-04:00")
    )
    known = {it.id: it for it in items}
    later = NOW + timedelta(hours=6)
    new, stats = fetch_all(canada, state, known, later, client=server.client())
    page_items = [i for i in new if i.source_id == "regulator"]
    assert len(page_items) == 1
    item = page_items[0]
    assert item.title == "Regulator issues AI guidance for public institutions"
    assert item.snippet.startswith("New guidance tells institutions how to assess AI systems.")
    assert "every provincial institution" in item.snippet and "Skip to the menu" not in item.snippet
    assert item.published == "2026-10-20T13:00:00Z"
    assert {s.source_id: s for s in stats}["regulator"].status == "ok"


def test_page_source_treats_a_flood_of_links_as_a_redesign(canada):
    routes = base_routes()
    server = SiteServer(routes)
    state: dict = {}
    fetch_all(canada, state, {}, NOW, client=server.client())
    routes["https://reg.test/news"] = ("text/html", news_page([(f"/news/story-{i}", f"Moved story number {i}") for i in range(30)]))
    new, stats = fetch_all(canada, state, {}, NOW + timedelta(hours=6), client=server.client())
    assert not [i for i in new if i.source_id == "regulator"]
    assert {s.source_id: s for s in stats}["regulator"].status == "baseline"
    assert len(state["regulator"]["page_seen"]) == 32


def test_tracked_updates_turn_a_new_bill_stage_into_a_new_item(project):
    (project.root / "config" / "sources.toml").write_text(
        '[[source]]\nid = "bills"\nname = "Bills"\nurl = "https://parl.test/rss"\nbeat = "ca"\ntier = "T1"\ntrack_updates = true\n',
        encoding="utf-8",
    )
    settings = load_settings(project.root)
    feed = lambda stage, when: rss([("C-8 (45-1) - An Act respecting cyber security", "https://parl.test/bill/C-8", when, f"Latest activity: {stage}")])
    routes = {"https://parl.test/rss": ("application/rss+xml", feed("First reading", H(5)))}
    server = SiteServer(routes)
    state: dict = {}
    first, _ = fetch_all(settings, state, {}, NOW, client=server.client())
    known = {it.id: it for it in first}
    same, _ = fetch_all(settings, state, known, NOW + timedelta(hours=6), client=server.client())
    routes["https://parl.test/rss"] = ("application/rss+xml", feed("Third reading", NOW + timedelta(hours=11)))
    moved, _ = fetch_all(settings, state, known, NOW + timedelta(hours=12), client=server.client())
    assert len(first) == 1 and same == [] and len(moved) == 1
    assert moved[0].url == first[0].url and moved[0].id != first[0].id


# ---- the beat in a full run ------------------------------------------------------------------


def test_canada_beat_gets_its_own_section_threshold_and_why_note(canada):
    fake = CanadaLLM()
    report = _run(canada, SiteServer(base_routes()), fake)
    store = Store(canada.data_dir, canada.tz)
    picked = [e for e in store.load_events().values() if e.selected_at]
    beats = {e.beat for e in picked}
    assert beats == {"ai", "ca"}, beats
    ca_stories = [e for e in picked if e.beat == "ca"]
    assert ca_stories and all(e.story and e.story["why_en"].startswith("Models used") for e in ca_stories)
    assert all("why_en" not in e.story for e in picked if e.beat == "ai")
    # The hospital story, from a media source only, scores 70: under the shared T2 bar (76), over the Canada beat's (68).
    items = store.load_items(NOW, 30)
    scribes = next(e for e in picked if any("scribes" in items[x].title for x in e.item_ids))
    assert scribes.score == 70 and scribes.beat == "ca"
    assert report["write"]["why"] == len(ca_stories)

    edition = store.load_edition("2026-10-20")
    md = edition_markdown(edition, canada, "en")
    assert md.index("## AI") < md.index("## Canada & Ontario")
    assert "_Why it matters: Models used in provincial programs" in md
    assert "為何重要：省府計畫使用的模型" in edition_markdown(edition, canada, "zh-TW")
    text, html = email_build(edition, canada, ["en", "zh-TW"])
    assert "Why it matters: Models used" in text and "為何重要" in html
    assert "<i>Why it matters: Models used" in "\n".join(messages(edition, canada, ["en"], 8))

    index = (canada.public_dir / "index.html").read_text(encoding="utf-8")
    assert "Canada &amp; Ontario" in index and 'class="why"' in index and "Why it matters" in index
    zh = (canada.public_dir / "zh-tw" / "index.html").read_text(encoding="utf-8")
    assert "加拿大與安大略省" in zh and "為何重要" in zh
    event_page = (canada.public_dir / "e" / f"{ca_stories[0].id}.html").read_text(encoding="utf-8")
    assert "Models used in provincial programs" in event_page and "this source tier needs" in event_page
    assert (canada.public_dir / "feed" / "ca.xml").exists()


def test_triage_beats_policy_to_policy_beat_but_keeps_canada(canada):
    event = Event(id="e1", created="2026-10-20T08:00:00Z")
    result = {"type": "policy_action", "beat": "ai", "axes": {a: 5 for a in ("sig", "nov", "cred", "res", "act")}}
    _apply(event, result, "fake/m", canada, {}, NOW)
    assert event.beat == "gov"  # policy stories never land in the general AI beat
    _apply(event, {**result, "beat": "ca"}, "fake/m", canada, {}, NOW)
    assert event.beat == "ca"
    _apply(event, {**result, "type": "model_release", "beat": None}, "fake/m", canada, {}, NOW)
    assert event.beat == "ca"  # no usable beat from the model: keep the source's


def test_telegram_gives_every_beat_a_share():
    stories = [{"id": f"a{i}", "beat": "ai", "heat": 5 - i * 0.1} for i in range(10)] + [
        {"id": "c1", "beat": "ca", "heat": 0.5}, {"id": "g1", "beat": "gov", "heat": 0.4}]
    chosen = fair_share(stories, ["ai", "ca", "gov"], 5)
    assert [s["id"] for s in chosen] == ["a0", "a1", "a2", "c1", "g1"]


# ---- checking sources ------------------------------------------------------------------------


def test_check_sources_reports_what_each_source_would_yield(canada):
    routes = base_routes()
    del routes["https://lab.test/feed.xml"]
    rows = {r["id"]: r for r in check_sources(canada, NOW, client=SiteServer(routes).client())}
    assert rows["lab"]["status"] == "error" and rows["lab"]["error"] == "HTTP 404"
    assert rows["on-news"]["entries"] == 2 and rows["on-news"]["matches"] == 1
    assert rows["on-news"]["sample"].startswith("Ontario releases artificial intelligence")
    assert rows["news-search"]["matches"] == 2
    assert rows["regulator"]["kind"] == "page" and rows["regulator"]["entries"] == 2


def test_sources_command_lists_and_checks(canada, capsys, monkeypatch):
    from microhot import __main__ as cli

    assert cli.main(["--root", str(canada.root), "sources"]) == 0
    out = capsys.readouterr().out
    assert "regulator" in out and "page" in out
    monkeypatch.setattr(cli, "check_sources", lambda settings, now, include_disabled, only: [
        {"id": "x", "kind": "feed", "status": "ok", "http": 200, "entries": 3, "matches": 0, "newest": None, "error": None, "sample": ""}])
    summary = canada.root / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["--root", str(canada.root), "sources", "--check"]) == 0
    assert "1 readable with no matches" in capsys.readouterr().out
    assert "| x | feed | ok | 3 | 0 |" in summary.read_text(encoding="utf-8")


def test_items_saved_before_publishers_existed_still_load():
    old = {"id": "abc", "source_id": "lab", "url": "https://lab.test/x", "title": "t", "snippet": "", "fetched": "2026-10-19T10:00:00Z"}
    item = Item.from_dict(old)
    assert item.publisher == "" and item.origin == "lab"


def test_a_rate_limited_source_is_retried_once(canada, monkeypatch):
    import microhot.fetch as fetch_mod

    waits = []
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: waits.append(s))
    routes = base_routes()
    calls = {"n": 0}

    def handler(request):
        if str(request.url) == "https://on.test/rss":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "7"})
        ctype, body = routes.get(str(request.url), ("text/plain", ""))
        return httpx.Response(200 if str(request.url) in routes else 404, text=body, headers={"content-type": ctype})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    _, stats = fetch_all(canada, {}, {}, NOW, client=client)
    assert calls["n"] == 2 and 7.0 in waits
    assert {s.source_id: s for s in stats}["on-news"].status == "ok"


def test_bing_style_search_items_keep_their_outlet(canada):
    bing = (
        '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" xmlns:News="https://www.bing.com:443/news/search?q=x&amp;format=rss">'
        "<channel><title>x - BingNews</title><item><title>Ontario public-sector unions push province for workplace AI safeguards</title>"
        "<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fwww.msn.com%2fen-ca%2fnews%2fontario-ai%2far-AA1&amp;c=1</link>"
        f"<description>Union bargaining units are urged to examine how AI is used at work.</description><pubDate>{format_datetime(H(3))}</pubDate>"
        "<News:Source>Global News on MSN</News:Source></item></channel></rss>"
    )
    routes = {**base_routes(), "https://search.test/rss": ("application/rss+xml", bing)}
    items, _ = fetch_all(canada, {}, {}, NOW, client=SiteServer(routes).client())
    found = [it for it in items if it.source_id == "news-search"]
    assert [(it.url, it.publisher) for it in found] == [("https://www.msn.com/en-ca/news/ontario-ai/ar-AA1", "Global News")]


def test_title_only_feed_items_get_their_page_text(canada):
    routes = base_routes()
    routes["https://lab.test/feed.xml"] = ("application/rss+xml", rss([
        ("FairStress: a deeper evaluation of fairness in LLM decisions", "https://lab.test/fairstress", H(2), ""),
    ]))
    routes["https://lab.test/fairstress"] = (
        "text/html", article("FairStress", "A benchmark of demographic bias in LLM decisions across hiring and lending.", "2026-10-20T09:00:00-04:00")
    )
    server = SiteServer(routes)
    items, _ = fetch_all(canada, {}, {}, NOW, client=server.client())
    item = next(i for i in items if i.source_id == "lab")
    assert item.snippet.startswith("A benchmark of demographic bias in LLM decisions")
    # Search items already carry text and link to other outlets: their pages are never opened.
    assert not [u for u in server.requests if "globalnews.test" in u or "cbc.test" in u]
