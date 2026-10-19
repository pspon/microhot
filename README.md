# MicroHOT 微熱

*AI news, served slightly warm. 不燙口的 AI 新聞。*

A private daily briefing on AI and AI governance, in English and Traditional Chinese (Taiwan), that costs
nothing to host and a handful of model calls a day to run.

MicroHOT keeps the editorial brain of [AIHOT](https://github.com/KKKKhazix/AIHOT) (its selection rubric,
writing rules, heat formula and rule-built daily edition) and replaces the server stack with one scheduled
GitHub Actions job and a password-protected static site on Cloudflare Pages.

## How it works

Three times a day the job fetches your feeds, drops duplicates and off-topic items with plain rules, groups
reports of the same story into events, and asks a model to score about 20 events per call. The best stories,
within a daily quota per beat, get an English and a Traditional Chinese headline and summary in one call per
four stories. Heat counts each independent source once over 48 hours, halving every 24 hours, and the site
shows it as a body temperature: 微熱 means a slight fever. The first run after 06:00 Toronto time builds the
morning edition and sends it to the channels you have turned on.

Details: [docs/PLAN.md](docs/PLAN.md).

## Set up

See [docs/SETUP.md](docs/SETUP.md). In short: add one model key (a free Gemini key is enough), add the three
Cloudflare secrets, then run **Actions → MicroHOT run**.

## Change what it does

| To change | Edit |
|---|---|
| Feeds, beats, source tiers, keyword filters | `config/sources.toml` |
| Thresholds, quotas, schedule-related settings, model order, caps, channels | `config/site.toml` |
| What counts as important, and for whom | `prompts/triage.md` |
| How stories are written in English and 繁中 | `prompts/write.md` |
| Run times | `.github/workflows/run.yml` |

## Commands

```bash
uv run microhot status    # configured providers and channels; makes no calls
uv run microhot run       # one full run (use --no-llm to skip model calls)
uv run microhot deliver   # send today's edition to ready channels
uv run microhot render    # rebuild the site from saved data
uv run pytest
```

## Credits

Adapted from AIHOT by 数字生命卡兹克 (MIT). See [NOTICE](NOTICE).
