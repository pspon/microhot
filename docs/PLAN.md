# MicroHOT 微熱: plan

*AI news, served slightly warm. 不燙口的 AI 新聞。*

Agreed on 2026-10-19. This is the reference for what MicroHOT is and why it is built this way.

## Decisions

| Topic | Decision |
|---|---|
| Name | MicroHOT 微熱: 微 = micro (small footprint), 微熱 = only slightly warm; still says HOT as a nod to AIHOT |
| Relationship to AIHOT | A new private repo, not a GitHub fork (forks of public repos cannot be private). It ports AIHOT's editorial brain (rubric, writing rules, heat formula, rule-built edition) with credit in `NOTICE`, and replaces its server stack |
| Beats | General AI news, plus AI governance and policy (Canada and Ontario first, then the EU, US, UK) |
| Audience | Just the owner: a password-protected site, not public, not indexed |
| Stack | Python, one scheduled GitHub Actions job, static HTML on Cloudflare Pages, state as JSON on a `data` branch. No server, no database, no Docker |
| Languages | English by default at `/`, Traditional Chinese (Taiwan usage) at `/zh-tw/`, one language at a time with a toggle |
| Cadence | 3 runs a day, about 6:25, 12:25, 18:25 Toronto time; the first run after 06:00 builds the morning edition |
| Delivery | The private web page is the source of truth. Optional channels link back to it: GitHub issue (on by default), email, Telegram. Each runs only when enabled and configured |
| Models | Fallback chain, free first: Gemini Flash-Lite (free tier) → Groq → OpenRouter free models → Mistral free mode → Cloudflare Workers AI → Claude Haiku 5.5 → DeepSeek → any OpenAI-compatible endpoint. Order is set per step in config |
| Unconfigured providers | Never called, never counted as failures. A provider is active only when its key (and any `${VAR}` in its URL) is set |
| Multiple free accounts | Not supported: spreading one workload across several free accounts breaks provider terms (Google restricts projects for quota circumvention; OpenRouter says extra accounts don't add capacity). Headroom comes from more providers, one account each, and more models per account |

## Architecture

```
GitHub Actions, 3× a day
 → fetch RSS/Atom            rules, filters, dedupe     0 tokens
 → group into events          lexical similarity         0 tokens
 → triage, ~20 per call       five-axis rubric           LLM
 → select                     tier thresholds + quotas   0 tokens
 → write EN + 繁中, ~4/call    top stories only           LLM
 → Taiwan guard               OpenCC s2twp               0 tokens
 → morning edition            rule-built                 0 tokens
 → render static site → Cloudflare Pages (password) → channels
 → commit state to the data branch
```

## Token budget

AIHOT averages about 6 model calls per item (930 calls for its first 152 items). MicroHOT targets
10–15 calls per **day** for a similar volume, roughly 30k input and 6k output tokens:

1. Rules before models: canonical-URL and title dedupe, keyword filters on broad feeds, first import limited to
   the last 48 hours.
2. One triage call per ~20 events, scoring only one representative per event; the rubric sits in the system
   prompt, which never changes, so providers can cache it. One score per event; a second opinion only within
   5 points of the threshold.
3. Only picked stories are written, about 4 per call, with English and Traditional Chinese in the same reply.
4. Summaries are written from the feed text (up to 1,500 characters), never the full article.
5. Hard caps: 40 calls and $0.05 a day across providers, plus a dollar cap per paid provider. Usage is logged per
   call and shown on the About page.

## Cost

| Item | Monthly |
|---|---|
| GitHub Actions | about 300 of 2,000 free minutes (private repo) |
| Cloudflare Pages | about 90 of 500 free builds |
| Models | $0 on free tiers; about $0.18 if Claude Haiku 5.5 did all the work; about $0.25–0.50 for DeepSeek |

## Done in the MVP

RSS/Atom sources with keyword filters; clustering and heat (shown as a temperature, since 微熱 means a slight
fever); batched triage with second opinions; per-beat quotas; bilingual writing with the Taiwan guard; the
private site (Today, story pages, morning editions, About with sources, models, usage, near misses, run
history); private RSS per language; GitHub issue, email and Telegram channels; tests with fixture feeds and a
fake model.

## Later

- Weekly edition (one model call a week for an overview).
- Site search (a static index).
- Calibration set: label a few hundred items and measure selection precision and recall against the thresholds.
- Sources without RSS (web pages, JSON APIs).
- Optional small local embedding model (`model2vec`) for clustering, already wired in config.
