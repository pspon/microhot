"""Post the morning edition as an issue in this repo (the GitHub app sends a push notification)."""

from __future__ import annotations

import httpx

from .. import i18n
from ..config import Settings

REQUIRED = ["GITHUB_TOKEN", "GITHUB_REPOSITORY"]


def markdown(edition: dict, settings: Settings, langs: list[str]) -> str:
    from . import edition_url, story_lines

    lead_lang = langs[0]
    lines = [f"**[{i18n.t(lead_lang, 'read_site')}]({edition_url(settings, edition['date'], lead_lang)})**", ""]
    order = settings.beat_order
    for beat in order:
        stories = [s for s in edition["stories"] if s["beat"] == beat]
        if not stories:
            continue
        label = settings.beats.get(beat, {}).get("label_en" if lead_lang == "en" else "label_zh", beat)
        lines += [f"### {label}", ""]
        for story in stories:
            titles, summaries = story_lines(story, langs)
            lines.append(f"**[{titles[0]}]({story['url']})** {story['temp']:.1f}°  ")
            for extra in titles[1:]:
                lines.append(f"{extra}  ")
            for summary in summaries:
                lines.append(f"{summary}  ")
            details = f"{settings.site_url}{i18n.prefix(lead_lang)}/e/{story['id']}.html"
            lines.append(f"<sub>{story['source']}, [{i18n.t(lead_lang, 'details')}]({details})</sub>")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def send(edition: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import edition_title, langs_for

    langs = langs_for(cfg)
    repo = environ["GITHUB_REPOSITORY"]
    api = str(environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    label = cfg.get("label", "edition")
    with httpx.Client(timeout=20, headers=headers) as client:
        client.post(
            f"{api}/repos/{repo}/labels",
            json={"name": label, "color": "c98a00", "description": "MicroHOT morning edition"},
        )  # 422 when it already exists, which is fine
        if cfg.get("close_previous", True):
            resp = client.get(f"{api}/repos/{repo}/issues", params={"labels": label, "state": "open", "per_page": 30})
            if resp.status_code == 200:
                for issue in resp.json():
                    if "pull_request" not in issue:
                        client.patch(issue["url"], json={"state": "closed", "state_reason": "completed"})
        body = markdown(edition, settings, langs)
        resp = client.post(
            f"{api}/repos/{repo}/issues",
            json={"title": edition_title(settings, edition, langs[0]), "body": body, "labels": [label]},
        )
        resp.raise_for_status()
