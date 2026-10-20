"""Markdown copies of editions and reviews (for the site, the data branch and the GitHub issue)."""

from __future__ import annotations

from . import i18n
from .config import Settings
from .textutil import parse_iso, utcnow


def _beat_label(settings: Settings, beat: str, lang: str) -> str:
    cfg = settings.beats.get(beat, {})
    return cfg.get("label_en" if lang == "en" else "label_zh") or beat


def edition_title(settings: Settings, edition: dict, lang: str) -> str:
    built = parse_iso(edition.get("built_at")) or utcnow()
    return i18n.t(lang, "edition_title", date=i18n.fmt_date(built, lang, settings.tz))


def review_title(settings: Settings, review: dict, lang: str) -> str:
    start, end = parse_iso(review["start"] + "T12:00:00Z"), parse_iso(review["end"] + "T12:00:00Z")
    if review["kind"] == "weekly":
        return i18n.t(
            lang,
            "weekly_title",
            week=review["id"].split("-W")[-1].lstrip("0"),
            start=i18n.fmt_day(start, lang),
            end=i18n.fmt_day(end, lang),
        )
    return i18n.t(lang, "monthly_title", month=i18n.fmt_month(start, lang))


def _story_lines(story: dict, settings: Settings, lang: str, *, checkboxes: bool = False) -> list[str]:
    title = story["title"].get(lang) or story["title"]["en"]
    summary = story["summary"].get(lang) or story["summary"]["en"]
    tags = [f"{story['temp']:.1f}°"]
    if story.get("official"):
        tags.append(i18n.t(lang, "official"))
    lines = [f"**[{title}]({story['url']})** {' · '.join(tags)}  "]
    if summary:
        lines.append(f"{summary}  ")
    why = (story.get("why") or {}).get(lang) or (story.get("why") or {}).get("en")
    if why:
        lines.append(f"_{i18n.t(lang, 'why_line', text=why)}_  ")
    if story.get("followup"):
        new = story["followup"]["new"]
        names = ", ".join(dict.fromkeys(link["source"] for link in new))
        lines.append(f"_{i18n.t(lang, 'followup_md', n=len(new), sources=names)}_  ")
    details = f"{settings.site_url}{i18n.prefix(lang)}/e/{story['id']}.html"
    lines.append(f"<sub>{story['source']}, [{i18n.t(lang, 'details')}]({details})</sub>")
    if checkboxes:
        lines.append(f"- [ ] 👍 {i18n.t(lang, 'rate_up')} <!-- mh:{story['id']}:up -->")
        lines.append(f"- [ ] 👎 {i18n.t(lang, 'rate_down')} <!-- mh:{story['id']}:down -->")
    lines.append("")
    return lines


def edition_markdown(edition: dict, settings: Settings, lang: str, *, checkboxes: bool = False, heading: bool = True) -> str:
    lines: list[str] = []
    if heading:
        lines += [f"# {settings.site['name']}: {edition_title(settings, edition, lang)}", ""]
    url = f"{settings.site_url}{i18n.prefix(lang)}/d/{edition['date']}.html"
    lines += [f"[{i18n.t(lang, 'read_site')}]({url})", ""]
    for beat in settings.beat_order:
        stories = [s for s in edition["stories"] if s["beat"] == beat]
        if not stories:
            continue
        lines += [f"## {_beat_label(settings, beat, lang)}", ""]
        for story in stories:
            lines += _story_lines(story, settings, lang, checkboxes=checkboxes)
    if edition.get("followups"):
        lines += [f"## {i18n.t(lang, 'followups')}", ""]
        for story in edition["followups"]:
            lines += _story_lines(story, settings, lang, checkboxes=checkboxes)
    if not edition["stories"] and not edition.get("followups"):
        lines += [i18n.t(lang, "edition_empty"), ""]
    if checkboxes and edition.get("near_misses"):
        lines += [f"## {i18n.t(lang, 'about_misses')}", "", i18n.t(lang, "rate_misses_note"), ""]
        for miss in edition["near_misses"]:
            lines.append(f"- [ ] 👍 [{miss['title']}]({miss['url']}) ({miss['score']}/{miss['threshold']}) <!-- mh:{miss['id']}:want -->")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def review_markdown(review: dict, settings: Settings, lang: str) -> str:
    lines = [f"# {settings.site['name']}: {review_title(settings, review, lang)}", ""]
    kind = "w" if review["kind"] == "weekly" else "m"
    lines += [f"[{i18n.t(lang, 'read_site')}]({settings.site_url}{i18n.prefix(lang)}/{kind}/{review['id']}.html)", ""]
    overview = (review.get("overview") or {}).get(lang)
    if overview:
        lines += [overview, ""]
    for beat in settings.beat_order:
        stories = [s for s in review["stories"] if s["beat"] == beat]
        if not stories:
            continue
        lines += [f"## {_beat_label(settings, beat, lang)}", ""]
        for story in stories:
            lines += _story_lines(story, settings, lang)
    return "\n".join(lines).strip() + "\n"


def bilingual(markdown_en: str, markdown_zh: str) -> str:
    return markdown_en.rstrip() + "\n\n---\n\n" + markdown_zh
