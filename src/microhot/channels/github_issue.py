"""Post the morning edition (with 👍/👎 rating boxes), reviews and alerts as issues in this repo.
The GitHub app sends a push notification for each new issue in a repository you watch."""

from __future__ import annotations

import httpx

from .. import i18n
from ..config import Settings
from ..markdown import edition_markdown, review_markdown

REQUIRED = ["GITHUB_TOKEN", "GITHUB_REPOSITORY"]
LABEL_COLORS = {"edition": "c98a00", "review": "3c6a99", "alert": "b8372b"}


def _client(environ) -> tuple[httpx.Client, str, str]:
    api = str(environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return httpx.Client(timeout=20, headers=headers), api, environ["GITHUB_REPOSITORY"]


def _open_issue(environ, title: str, body: str, label: str, close_previous: bool) -> None:
    client, api, repo = _client(environ)
    with client:
        client.post(
            f"{api}/repos/{repo}/labels",
            json={"name": label, "color": LABEL_COLORS.get(label, "586572"), "description": f"MicroHOT {label}"},
        )  # 422 when it already exists, which is fine
        if close_previous:
            resp = client.get(f"{api}/repos/{repo}/issues", params={"labels": label, "state": "open", "per_page": 30})
            if resp.status_code == 200:
                for issue in resp.json():
                    if "pull_request" not in issue:
                        client.patch(issue["url"], json={"state": "closed", "state_reason": "completed"})
        resp = client.post(f"{api}/repos/{repo}/issues", json={"title": title, "body": body, "labels": [label]})
        resp.raise_for_status()


def edition_body(edition: dict, settings: Settings, langs: list[str]) -> str:
    lead = langs[0]
    parts = [edition_markdown(edition, settings, lead, checkboxes=True, heading=False)]
    for lang in langs[1:]:
        parts.append(edition_markdown(edition, settings, lang, heading=False))
    note = i18n.t(lead, "rate_note")
    return "\n---\n\n".join(parts) + f"\n<sub>{note}</sub>\n<!-- mh-edition:{edition['date']} -->\n"


def markdown(edition: dict, settings: Settings, langs: list[str]) -> str:
    """Kept for callers that only want the issue body."""
    return edition_body(edition, settings, langs)


def send(edition: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import edition_title, langs_for

    langs = langs_for(cfg)
    _open_issue(
        environ,
        edition_title(settings, edition, langs[0]),
        edition_body(edition, settings, langs),
        cfg.get("label", "edition"),
        cfg.get("close_previous", True),
    )


def send_review(review: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import langs_for, review_title

    langs = langs_for(cfg)
    body = "\n---\n\n".join(review_markdown(review, settings, lang) for lang in langs)
    _open_issue(environ, review_title(settings, review, langs[0]), body, "review", cfg.get("close_previous", True))


def send_alert(text: str, settings: Settings, environ) -> None:
    _open_issue(environ, f"{settings.site['name']} alert", text + "\n", "alert", False)
