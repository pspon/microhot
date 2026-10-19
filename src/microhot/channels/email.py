"""Send the morning edition by email over SMTP (Gmail works with an app password)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from jinja2 import Environment, select_autoescape

from .. import i18n
from ..config import Settings

REQUIRED = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]

_HTML = """<!doctype html>
<html><body style="margin:0;padding:24px 16px;background:#f3f6f6;color:#17212c;font-family:-apple-system,'Segoe UI',Roboto,'PingFang TC','Microsoft JhengHei',sans-serif;line-height:1.55">
<div style="max-width:620px;margin:0 auto">
<p style="margin:0 0 4px;font-weight:700;font-size:18px">{{ title }}</p>
<p style="margin:0 0 20px"><a href="{{ url }}" style="color:#17212c">{{ read_site }}</a></p>
{% for g in groups %}
<p style="margin:24px 0 6px;color:#586572;font-weight:600;border-top:1px solid #d5dcdf;padding-top:8px">{{ g.label }}</p>
{% for s in g.stories %}
<div style="margin:0 0 16px">
<p style="margin:0;font-size:16px;font-weight:650"><span style="color:{{ s.color }};font-variant-numeric:tabular-nums">{{ s.temp }}°</span>&nbsp; <a href="{{ s.url }}" style="color:#17212c;text-decoration:none">{{ s.titles[0] }}</a></p>
{% for extra in s.titles[1:] %}<p style="margin:2px 0 0;font-size:15px">{{ extra }}</p>{% endfor %}
{% for summary in s.summaries %}<p style="margin:4px 0 0">{{ summary }}</p>{% endfor %}
<p style="margin:4px 0 0;font-size:13px;color:#586572">{{ s.source }} · <a href="{{ s.details }}" style="color:#586572">{{ details }}</a></p>
</div>
{% endfor %}
{% endfor %}
</div></body></html>"""

_COLORS = {"cool": "#3c6a99", "warm": "#8f5c00", "hot": "#b8372b"}


def build(edition: dict, settings: Settings, langs: list[str]) -> tuple[str, str]:
    from . import edition_url, story_lines

    lead = langs[0]
    groups = []
    for beat in settings.beat_order:
        stories = []
        for s in (x for x in edition["stories"] if x["beat"] == beat):
            titles, summaries = story_lines(s, langs)
            stories.append(
                {
                    "temp": f"{s['temp']:.1f}",
                    "color": _COLORS.get(s.get("temp_class", "cool"), "#3c6a99"),
                    "titles": titles,
                    "summaries": summaries,
                    "url": s["url"],
                    "source": s["source"],
                    "details": f"{settings.site_url}{i18n.prefix(lead)}/e/{s['id']}.html",
                }
            )
        if stories:
            label = settings.beats.get(beat, {}).get("label_en" if lead == "en" else "label_zh", beat)
            groups.append({"label": label, "stories": stories})
    url = edition_url(settings, edition["date"], lead)
    env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
    html = env.from_string(_HTML).render(
        title=edition_title_for(edition, settings, lead),
        url=url,
        read_site=i18n.t(lead, "read_site"),
        details=i18n.t(lead, "details"),
        groups=groups,
    )
    text_lines = [edition_title_for(edition, settings, lead), url, ""]
    for g in groups:
        text_lines += [g["label"], ""]
        for s in g["stories"]:
            text_lines.append(f"{s['temp']}°  {s['titles'][0]}")
            text_lines += s["titles"][1:] + s["summaries"]
            text_lines += [s["url"], ""]
    return "\n".join(text_lines), html


def edition_title_for(edition: dict, settings: Settings, lang: str) -> str:
    from . import edition_title

    return edition_title(settings, edition, lang)


def send(edition: dict, settings: Settings, cfg: dict, environ) -> None:
    from . import langs_for

    langs = langs_for(cfg)
    text, html = build(edition, settings, langs)
    msg = EmailMessage()
    msg["Subject"] = edition_title_for(edition, settings, langs[0])
    msg["From"] = environ.get("EMAIL_FROM") or environ["SMTP_USER"]
    msg["To"] = environ["EMAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    host = environ["SMTP_HOST"]
    port = int(environ.get("SMTP_PORT") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(environ["SMTP_USER"], environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(environ["SMTP_USER"], environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
