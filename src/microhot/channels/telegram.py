"""Send the top stories of the morning edition as a Telegram message."""

from __future__ import annotations

from html import escape

import httpx

from .. import i18n
from ..config import Settings

REQUIRED = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
LIMIT = 3800  # Telegram allows 4096 characters per message


def messages(edition: dict, settings: Settings, langs: list[str], max_stories: int) -> list[str]:
    from . import edition_title, edition_url, story_lines

    lead = langs[0]
    head = f"<b>{escape(edition_title(settings, edition, lead))}</b>"
    blocks = []
    order = {beat: i for i, beat in enumerate(settings.beat_order)}
    stories = sorted(edition["stories"], key=lambda s: (order.get(s["beat"], 99), -s["heat"]))[:max_stories]
    for story in stories:
        titles, summaries = story_lines(story, langs)
        parts = [f"{story['temp']:.1f}° <a href=\"{escape(story['url'], quote=True)}\">{escape(titles[0])}</a>"]
        parts += [escape(t) for t in titles[1:]]
        parts += [escape(s) for s in summaries]
        blocks.append("\n".join(parts))
    tail = f"<a href=\"{escape(edition_url(settings, edition['date'], lead), quote=True)}\">{escape(i18n.t(lead, 'read_site'))}</a>"
    out, current = [], head
    for block in blocks + [tail]:
        candidate = f"{current}\n\n{block}"
        if len(candidate) > LIMIT and current:
            out.append(current)
            current = block
        else:
            current = candidate
    out.append(current)
    return out


def send(edition: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import langs_for

    langs = langs_for(cfg)
    url = f"https://api.telegram.org/bot{environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with httpx.Client(timeout=20) as client:
        for text in messages(edition, settings, langs, int(cfg.get("max_stories", 8))):
            resp = client.post(
                url,
                json={
                    "chat_id": environ["TELEGRAM_CHAT_ID"],
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
