# Setup

Everything is configured with repository secrets and variables: on GitHub, open **Settings → Secrets and
variables → Actions** (**New repository secret**, or the **Variables** tab). This is easiest from a browser (on a phone, use github.com in the browser).

Nothing breaks while a model or channel secret is missing (the data repo in step 3 is the exception). The run report (Actions → a run → Summary) and the site's About page
say what is skipped and why.

## 1. A model key (at least one)

The pipeline tries providers in the order listed in `config/site.toml` and only calls the ones you have keys
for. One key is enough to start.

| Secret | Where to get it | Cost at MicroHOT's volume |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio → Get API key. Leave billing off for that project to stay on the free tier. | Free (free-tier prompts may be used by Google to improve its products) |
| `ANTHROPIC_API_KEY` | Claude Console → API keys (platform.claude.com/settings/keys) | About $0.18/month if it did all the work |
| `GROQ_API_KEY` | GroqCloud console → API keys | Free plan |
| `OPENROUTER_API_KEY` | OpenRouter → Keys. Free models allow 50 requests/day, or 1,000 after a one-time $10 credit purchase. | Free models |
| `MISTRAL_API_KEY` | Mistral console → API keys (free mode) | Free mode |
| `DEEPSEEK_API_KEY` | DeepSeek platform → API keys | About $0.25–0.50/month if it did all the work |
| `CLOUDFLARE_AI_TOKEN` | Cloudflare → My Profile → API Tokens, with Workers AI permission (also needs `CLOUDFLARE_ACCOUNT_ID`) | Daily free allowance |

Use one account per provider. Splitting the workload across several free accounts breaks the providers'
terms and can get the account suspended; it is also unnecessary, since MicroHOT makes about 10–20 calls a day.

## 2. The private site (Cloudflare Pages)

1. Create a free Cloudflare account.
2. Copy your **Account ID** (shown on the account home page and in Workers & Pages).
3. **My Profile → API Tokens → Create Token → Create Custom Token**, with the permission
   *Account → Cloudflare Pages → Edit*. Copy the token.
4. Add three secrets:
   - `CLOUDFLARE_API_TOKEN`: the token
   - `CLOUDFLARE_ACCOUNT_ID`: the account ID
   - `SITE_PASSWORD`: a long password for the site (the username can be anything)
5. Run the workflow once (step 4). The run creates a Pages project called `microhot` and prints its address in
   the "Publish to Cloudflare Pages" step. If that address is not `https://microhot.pages.dev` (names are
   shared across all Cloudflare users), add a repository **variable** `SITE_URL` with the real address, so links
   in feeds and messages point to the right place.

Until Cloudflare is set up, a run on a private code repo attaches the built site as a downloadable artifact.
On a public code repo it does not, because anyone signed in to GitHub could download it.

## 3. The private data repo

This code repository can be public, but the digest (stories, editions, ratings, the morning issue) must not be.
It lives in a second, private repository.

1. **Create the repo.** On GitHub, **New repository**, for example `microhot-data`, set to **Private**. Empty or
   with a README is fine. Its default branch (`main`) holds the data.
2. **Create a token.** **GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token**:
   repository access *Only select repositories* → the data repo only; permissions *Contents: Read and write*,
   *Issues: Read and write* (*Metadata: Read* is added automatically). Set an expiry and put a reminder in
   your calendar to renew it: when it expires, runs fail until you replace the secret. If the data repo
   belongs to an organisation, choose that organisation as the token's *Resource owner*.
3. **Add it to this repo.** Secret `DATA_TOKEN`: the token. Variable `DATA_REPO`: `owner/name` of the data repo.
   Optional variable `DATA_BRANCH` if you do not want `main`.
4. **Get the notifications.** The morning edition, reviews and alerts are now issues in the data repo, so
   **Watch** that repo (custom: Issues) in the GitHub app. Issues in this repo no longer arrive.

Every run starts by checking this. It stops with an error, before touching any data, if `DATA_REPO` or
`DATA_TOKEN` is missing, if `DATA_REPO` is not private (or cannot be read with the token), or if it is this
repository (a different branch of this repo is allowed only when it is private). Nothing is posted or saved
in this repo, so its workflow token only needs read access.

The data repo's name is masked in the workflow logs.

Moving from the old setup: treat the old public `data` branch, and any issues and run artifacts already
created in this repo, as exposed. Deleting them does not recall copies someone already fetched, but do it
before the first private run: copy the `data` branch into the data repo's `main` first if you want to keep
the history, then delete the branch, close and delete the old issues, and delete the old artifacts
(Actions → a run → Artifacts).

## 4. Run it

**Actions → MicroHOT run → Run workflow**. After that it runs at about 6:25, 12:25 and 18:25 Toronto time.
The first run at or after 06:00 builds the morning edition.

### On-time runs (Cloudflare scheduler)

GitHub's own schedule is best-effort: runs can start hours late or be skipped. A small Cloudflare Worker in
`scheduler/` starts the workflow on time instead, and the GitHub schedule stays as a backup that stands down
whenever another run started in the last 3 hours. It is free and makes one call per run. To set it up:

1. **Let the Cloudflare token deploy Workers.** In Cloudflare, **My Profile → API Tokens**, edit the token in
   `CLOUDFLARE_API_TOKEN` and add the permission *Account → Workers Scripts → Edit*.
2. **Create a GitHub token for the Worker.** **GitHub → Settings → Developer settings → Fine-grained tokens →
   Generate new token**: repository access *Only select repositories* → this repository; permission
   *Actions → Read and write*. Add it as the repository secret `DISPATCH_TOKEN`.
3. **Deploy.** **Actions → Scheduler deploy → Run workflow** (it also runs when `scheduler/` changes).

When the GitHub token expires, scheduled runs fall back to GitHub's late schedule until you create a new one,
update `DISPATCH_TOKEN` and run **Scheduler deploy** again. The Worker's logs are under **Workers & Pages →
microhot-scheduler → Logs** in Cloudflare.

Open the site, sign in with any username and `SITE_PASSWORD`, and add it to your home screen.
Private RSS feeds (listed on the About page) are at `/feed.xml` for all picks, `/feed/ai.xml`, `/feed/ca.xml`
and `/feed/gov.xml` per beat, and `/feed/editions.xml` for the editions and reviews; add `/zh-tw` in front for
Traditional Chinese. Readers that support basic auth accept `https://any:PASSWORD@your-site/feed.xml`.

## 5. Delivery channels (optional)

Each channel runs only when it is `enabled = true` in `config/site.toml` **and** its secrets are set.
Choose the language with `lang = "en"`, `"zh-TW"` or `"both"`.

- **GitHub app**: on by default and uses the data repo from step 3. Each morning's edition is posted there as an
  issue (the previous one is closed), with 👍/👎 boxes for rating (step 6). Weekly and monthly reviews arrive as issues labelled
  `review`, alerts as issues labelled `alert`. To get a push notification, watch the data repository for issues in
  the GitHub app.
- **Email**: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` (optional `EMAIL_FROM`).
  For Gmail: turn on 2-Step Verification, create an app password, then use `smtp.gmail.com`, port `465`,
  your address as user and recipient, and the app password.
- **Telegram**: create a bot with @BotFather (`/newbot`) for `TELEGRAM_BOT_TOKEN`. Send your bot any message,
  then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into `TELEGRAM_CHAT_ID`.

Weekly reviews are built on Mondays (for the week before) and monthly reviews on the 1st, from the morning
editions, and go to the same channels.

**Alerts** go to the channels listed in `[alerts] channels` (GitHub issue and Telegram by default): a feed that
fails three runs in a row (sent once, until it works again), a model provider turned off because its key was
rejected, the daily call cap being reached, or a run where no provider worked. If a run fails outright, GitHub
emails you about the failed workflow as usual.

## 6. Rate the picks (optional)

The morning GitHub issue lists each pick with two boxes, 👍 *useful* and 👎 *not for me*, and a short list of
near misses with a 👍 box for any you would have wanted. Tick them in the GitHub app or on the web; the next
run reads the last 14 days of issues into `labels.jsonl` in the data repo. Unticking removes the rating.

The About page shows how many rated picks were useful. Once a tier has 10 ratings (`[feedback] min_labels`),
it also suggests a threshold, the one that best balances what you liked against what you would have wanted.
To see the details locally, clone the data repo as `data` next to the code and run the eval command:
`git clone git@github.com:OWNER/microhot-data.git data`, then `uv run microhot eval`. Thresholds never
change on their own: move them in `config/site.toml` when the suggestion looks right.

## 7. Tuning

- **What it covers**: `config/sources.toml` (feeds, news-search feeds and watched pages; beat, tier, keyword
  filters). After changing it, run **Actions → Check sources → Run workflow** (it also runs on pull requests
  that touch the file): it reads every source from GitHub's servers and lists, per source, the HTTP status,
  entries, how many pass the keyword rules in the last 14 days, and a sample headline. Locally:
  `uv run microhot sources --check`.
- **Beats**: `[beats.*]` in `config/site.toml`. Each beat is a section with its own daily quota; a beat can set
  its own `thresholds` (the Canada & Ontario beat has a lower bar than the others) and `why = true` for a
  one-line "why it matters" note on each story. The note's angle is set in the "Why it matters" section of
  `prompts/write.md`.
- **What gets picked**: thresholds, second opinions and daily quotas per beat in `config/site.toml`; the reader
  description and rubric in `prompts/triage.md`. The About page's "Near misses" table shows what just missed.
- **Your own nudges**: `[adjust]` in `config/site.toml`. `boost` adds points when a story matches a topic or a
  pattern (for example `{ topic = "canada", points = 8 }`); `mute` drops matching items before any model sees
  them. Boosted stories show their points on the story page.
- **Topic pages**: `config/topics.toml`, one entry per topic with a few patterns. The Topics page lists them.
- **Overviews and reviews**: `[digest]` (which stories get an overview, and how many a day) and `[reviews]`
  in `config/site.toml`; their wording in `prompts/digest.md` and `prompts/review.md`.
- **How it is written**: `prompts/write.md`.
- **Which models, in which order**: `[llm] triage` and `write` in `config/site.toml` (overviews and reviews use
  the `write` list unless you add `digest` or `review` lists). Remove a provider from the lists, or simply
  don't set its key, and it is never called.
- **Costs**: `max_calls_per_day` and `usd_cap_per_day` in `config/site.toml`, plus `usd_cap_per_day` per paid
  provider. Usage by provider is on the About page.

## Running locally

```bash
uv sync
uv run microhot status          # which providers and channels are configured (no calls)
GEMINI_API_KEY=... uv run microhot run
uv run microhot eval            # ratings vs thresholds
python -m http.server -d public  # then open http://localhost:8000
uv run pytest
```
