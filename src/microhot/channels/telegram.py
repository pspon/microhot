"""Send the morning edition, reviews and alerts as Telegram messages."""

from __future__ import annotations

from html import escape

import httpx

from .. import i18n
from ..config import Settings

REQUIRED = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
LIMIT = 3800  # Telegram allows 4096 characters per message


def _story_block(story: dict, langs: list[str]) -> str:
    from . import story_lines, why_lines

    titles, summaries = story_lines(story, langs)
    parts = [f"{story['temp']:.1f}° <a href=\"{escape(story['url'], quote=True)}\">{escape(titles[0])}</a>"]
    parts += [escape(t) for t in titles[1:]]
    parts += [escape(s) for s in summaries]
    parts += [f"<i>{escape(line)}</i>" for line in why_lines(story, langs)]
    return "\n".join(parts)


def fair_share(stories: list[dict], beat_order: list[str], limit: int) -> list[dict]:
    """Up to `limit` stories, taken from each beat in turn (warmest first), then listed by beat."""
    order = {beat: i for i, beat in enumerate(beat_order)}
    queues: dict[str, list[dict]] = {}
    for story in sorted(stories, key=lambda s: -s["heat"]):
        queues.setdefault(story["beat"], []).append(story)
    beats = sorted(queues, key=lambda b: order.get(b, 99))
    chosen: list[dict] = []
    while len(chosen) < limit and any(queues.values()):
        for beat in beats:
            if queues[beat] and len(chosen) < limit:
                chosen.append(queues[beat].pop(0))
    return sorted(chosen, key=lambda s: (order.get(s["beat"], 99), -s["heat"]))


def _chunks(head: str, blocks: list[str]) -> list[str]:
    out, current = [], head
    for block in blocks:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > LIMIT and current:
            out.append(current)
            current = block
        else:
            current = candidate
    out.append(current)
    return out


def messages(edition: dict, settings: Settings, langs: list[str], max_stories: int) -> list[str]:
    from . import edition_title, edition_url

    lead = langs[0]
    head = f"<b>{escape(edition_title(settings, edition, lead))}</b>"
    stories = fair_share(edition["stories"], settings.beat_order, max_stories)
    blocks = [_story_block(s, langs) for s in stories]
    if edition.get("followups"):
        blocks.append(f"<b>{escape(i18n.t(lead, 'followups'))}</b>")
        blocks += [_story_block(s, langs) for s in edition["followups"]]
    tail = f"<a href=\"{escape(edition_url(settings, edition['date'], lead), quote=True)}\">{escape(i18n.t(lead, 'read_site'))}</a>"
    return _chunks(head, blocks + [tail])


def _post(environ, texts: list[str]) -> None:
    url = f"https://api.telegram.org/bot{environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with httpx.Client(timeout=20) as client:
        for text in texts:
            resp = client.post(
                url,
                json={"chat_id": environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            )
            resp.raise_for_status()


def send(edition: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import langs_for

    _post(environ, messages(edition, settings, langs_for(cfg), int(cfg.get("max_stories", 8))))


def send_review(review: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import langs_for, review_title, review_url

    langs = langs_for(cfg)
    lead = langs[0]
    head = f"<b>{escape(review_title(settings, review, lead))}</b>"
    blocks = [escape(review["overview"][lang]) for lang in langs if (review.get("overview") or {}).get(lang)]
    blocks += [_story_block(s, langs) for s in review["stories"][: int(cfg.get("max_stories", 8))]]
    blocks.append(f"<a href=\"{escape(review_url(settings, review, lead), quote=True)}\">{escape(i18n.t(lead, 'read_site'))}</a>")
    _post(environ, _chunks(head, blocks))


def send_alert(text: str, settings: Settings, environ) -> None:
    _post(environ, [f"⚠️ <b>{escape(settings.site['name'])}</b>\n{escape(text)}"])
