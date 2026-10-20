# MicroHOT 微熱

*AI news, served slightly warm. 不燙口的 AI 新聞。*

A daily briefing on AI, Canadian public-sector AI and AI governance, in English and Traditional Chinese
(Taiwan), that costs nothing to host and a handful of model calls a day to run.

MicroHOT keeps the editorial brain of [AIHOT](https://github.com/KKKKhazix/AIHOT) (its selection rubric,
writing rules, heat formula and rule-built daily edition) and replaces the server stack with one scheduled
GitHub Actions job and a password-protected static site on Cloudflare Pages.

## How it works

Three times a day the job reads your sources, drops duplicates and off-topic items with plain rules, groups
reports of the same story into events, and asks a model to score about 20 events per call. The best stories,
within a daily quota per beat, get an English and a Traditional Chinese headline and summary in one call per
four stories.

There are three beats: **AI**, **Canada & Ontario** and **Governance & policy**. Canada & Ontario follows how
Canadian governments use and govern AI: federal and Ontario announcements, the AI minister, Treasury Board, the
privacy and human-rights commissions, Parliament and the Ontario legislature, Ontario Health, the City of
Toronto, health-technology trade press, legal and policy analysts, the politics and local sections of CBC,
Global News, CityNews and the Globe and Mail, and narrow news searches. Its stories carry a one-line **why it
matters** note, written in the same call as the summary. Sources can be RSS or Atom feeds, news-search feeds
(each outlet in the results counts as its own source) or plain web pages watched for new links, for sites
without a feed such as the Prime Minister's news releases.

Heat counts each independent source once over 48 hours, halving every 24 hours, and the site
shows it as a body temperature: 微熱 means a slight fever. The first run after 06:00 Toronto time builds the
morning edition and sends it to the channels you have turned on.

Most of what you see costs no tokens at all:

- **Hot markers**: stories show their temperature, its change since the last run, and *spiking*, *new* or
  *warming* badges.
- **Richer editions**: a lead story, highlights, a stats line, links to the previous and next edition, a
  Markdown copy, and **follow-ups** when an earlier story gains an official source or several new ones.
- **Topics** (`config/topics.toml`): keyword pages such as Canada, the EU AI Act or agents, which you can also
  **boost** by a few points or **mute** entirely.
- **Search, feeds and "new since your last visit"**: a static search index, RSS per beat and for editions, and
  a dot on stories picked since you last opened the site (kept in your browser only).
- **Ratings**: tick 👍 or 👎 in the morning GitHub issue; `microhot eval` and the About page tell you whether
  the thresholds match your taste.
- **Alerts**: a feed failing three runs in a row, or a model provider failing, sends you a note.

A few things spend a little: an **overview** for big stories with three or more sources (at most two a day),
and **weekly and monthly reviews** with a short overview (one call each).

Details: [docs/PLAN.md](docs/PLAN.md).

## Set up

See [docs/SETUP.md](docs/SETUP.md). In short: add one model key (a free Gemini key is enough), add the three
Cloudflare secrets, then run **Actions → MicroHOT run**.

## Change what it does

| To change | Edit |
|---|---|
| Sources (feeds, news searches, watched pages), their beat, tier and keyword filters | `config/sources.toml` |
| Topic pages | `config/topics.toml` |
| Beats, thresholds, quotas, boost and mute rules, model order, caps, channels, alerts | `config/site.toml` |
| What counts as important, for whom, and which beat a story belongs to | `prompts/triage.md` |
| How stories and "why it matters" notes are written in English and 繁中 | `prompts/write.md` |
| Overviews for big stories, and the review overviews | `prompts/digest.md`, `prompts/review.md` |
| Run times | `scheduler/wrangler.toml` (on-time runs) and `.github/workflows/run.yml` (backup schedule) |

## Commands

```bash
uv run microhot status    # configured providers and channels; makes no calls
uv run microhot run       # one full run (use --no-llm to skip model calls)
uv run microhot deliver   # read your ratings, send today's edition, reviews and alerts
uv run microhot render    # rebuild the site from saved data
uv run microhot eval      # how well the thresholds match your 👍/👎 ratings
uv run microhot sources --check   # read every source now and report what each would yield
uv run pytest
```

## Credits

Adapted from AIHOT by 数字生命卡兹克 (MIT). See [NOTICE](NOTICE).
