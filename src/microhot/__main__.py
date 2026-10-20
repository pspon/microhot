"""Command line: `microhot run | deliver | render | status | eval | sources`."""

from __future__ import annotations

import argparse
import os
import sys

from .alerts import send_pending
from .channels import channel_status, deliver
from .config import load_settings
from .feedback import collect_labels, eval_lines, evaluate
from .fetch import check_sources
from .llm import Router, UsageLog
from .pipeline import run, summary_lines
from .prompts import load_prompt
from .render import SiteBuilder
from .store import Store
from .textutil import utcnow
from .topics import load_topics, validate_rules

STEPS = ("triage", "write", "digest", "review")


def _step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(f"- {line}" if i else f"### {line}" for i, line in enumerate(lines)) + "\n")


def _source_checks_markdown(rows: list[dict], summary: str) -> str:
    lines = ["### Sources", "", summary, "", "| Source | Kind | Status | Entries | Matches | Newest | Note |", "|---|---|---|---:|---:|---|---|"]
    for r in rows:
        note = (r["error"] or r["sample"] or "").replace("|", "/")[:90]
        lines.append(f"| {r['id']} | {r['kind']} | {r['status']} | {r['entries']} | {r['matches']} | {(r.get('newest') or '')[:10]} | {note} |")
    lines += ["", "Matches: entries in the last 14 days that pass the keyword rules (pages: links on the page now)."]
    sampled = [r for r in rows if len(r.get("samples") or []) > 1]
    if sampled:
        lines += ["", "<details><summary>Sample headlines</summary>", ""]
        for r in sampled:
            lines.append(f"**{r['id']}**")
            lines += [f"- {title}" for title in r["samples"]]
            lines.append("")
        lines.append("</details>")
    return "\n".join(lines) + "\n"


def _print_source_checks(rows: list[dict], markdown_path: str = "") -> None:
    """A plain table for the terminal, and a Markdown table in the GitHub Actions summary (and a file)."""
    bad = [r for r in rows if r["status"] != "ok"]
    quiet = [r for r in rows if r["status"] == "ok" and not r["matches"]]
    for r in rows:
        mark = "OK " if r["status"] == "ok" else r["status"].upper()[:5]
        newest = (r.get("newest") or "")[:10]
        detail = r["error"] or r["sample"][:70]
        print(f"{mark:5s} {r['id']:22s} {r['kind']:6s} http={r['http'] or '-':<4} entries={r['entries']:<4} "
              f"matches={r['matches']:<3} newest={newest or '-':10s} {detail}")
    summary = f"{len(rows)} sources: {len(rows) - len(bad)} readable, {len(bad)} failing, {len(quiet)} readable with no matches"
    print(summary)
    table = _source_checks_markdown(rows, summary)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(table)
    if markdown_path:
        with open(markdown_path, "w", encoding="utf-8") as fh:
            fh.write(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="microhot", description="MicroHOT 微熱 news digest")
    parser.add_argument("--root", help="project folder (defaults to the current directory)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="one scheduled run: fetch, score, write, render")
    p_run.add_argument("--no-llm", action="store_true", help="skip all model calls")
    sub.add_parser("deliver", help="read your ratings, send today's edition/reviews and any alerts")
    sub.add_parser("render", help="rebuild the site from saved data")
    sub.add_parser("status", help="show which providers and channels are configured (no calls)")
    sub.add_parser("eval", help="how well the thresholds match your 👍/👎 ratings")
    p_src = sub.add_parser("sources", help="list sources; with --check, read each one now and report (no model calls, saves nothing)")
    p_src.add_argument("--check", action="store_true", help="fetch every source and show what it would yield")
    p_src.add_argument("--all", action="store_true", help="include paused sources")
    p_src.add_argument("--only", default="", help="comma-separated source ids")
    p_src.add_argument("--markdown", default="", help="also write the results as a Markdown table to this file")
    args = parser.parse_args(argv)

    settings = load_settings(args.root)

    if args.cmd == "run":
        report = run(settings, use_llm=not args.no_llm)
        lines = summary_lines(report)
        print("\n".join(lines))
        _step_summary(lines)
        return 0

    if args.cmd == "deliver":
        store = Store(settings.data_dir, settings.tz)
        lines = ["Delivery"]
        try:
            labels = collect_labels(settings, store, os.environ)
        except Exception as exc:  # ratings are a bonus; never block delivery
            labels = {"status": f"error: {type(exc).__name__}: {exc}"[:200]}
        lines.append("ratings: " + ", ".join(f"{k} {v}" for k, v in labels.items()))
        results = deliver(settings, store)
        lines += [f"{name}: {result}" for name, result in results.items()]
        state = store.load_state()
        alerts = send_pending(settings, state, os.environ)
        if alerts:
            store.save_state(state)
            lines += [f"alert {name}: {result}" for name, result in alerts.items()]
        print("\n".join(lines))
        _step_summary(lines)
        return 0

    if args.cmd == "render":
        store = Store(settings.data_dir, settings.tz)
        now = utcnow()
        items = store.load_items(now, int(settings.retention["items_days"]))
        stats = SiteBuilder(settings, store, store.load_events(), items, now, topics=load_topics(settings)).build()
        print(f"rendered {stats['pages']} files to {settings.public_dir}")
        return 0

    if args.cmd == "eval":
        lines = eval_lines(evaluate(settings, Store(settings.data_dir, settings.tz)))
        print("\n".join(lines))
        return 0

    if args.cmd == "sources":
        only = [x.strip() for x in args.only.split(",") if x.strip()]
        if not args.check:
            for src in settings.sources:
                if (src.enabled or args.all) and (not only or src.id in only):
                    print(f"{src.id:22s} {src.beat:4s} {src.tier:4s} {src.kind:6s} {'on ' if src.enabled else 'off'} {src.url}")
            return 0
        rows = check_sources(settings, utcnow(), include_disabled=args.all, only=only)
        _print_source_checks(rows, args.markdown)
        return 0

    if args.cmd == "status":
        for name in STEPS:
            load_prompt(settings.prompts_dir, name)
        topics = load_topics(settings)
        validate_rules(settings, topics)
        store = Store(settings.data_dir, settings.tz)
        router = Router(settings, UsageLog(store.usage_dir, settings.tz), utcnow())
        print(f"sources: {sum(1 for s in settings.sources if s.enabled)} enabled of {len(settings.sources)}")
        print(f"topics: {len(topics)}; boost rules: {len(settings.raw['adjust'].get('boost', []))}; "
              f"muted words: {len(settings.raw['adjust'].get('mute', []))}")
        for step in STEPS:
            print(f"{step} chain (configured only): {', '.join(router.chain(step)) or 'none'}")
        for name, reason in sorted(router.unconfigured.items()):
            print(f"  skipped {name}: {reason}")
        for name, status in channel_status(settings).items():
            print(f"channel {name}: {status}")
        print(f"alerts go to: {', '.join(settings.raw['alerts'].get('channels', [])) or 'nowhere (off)'}")
        print(f"site url: {settings.site_url or '(not set)'}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
