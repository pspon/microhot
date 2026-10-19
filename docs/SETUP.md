# Setup

Everything is configured with repository secrets: on GitHub, open **Settings → Secrets and variables →
Actions → New repository secret**. This is easiest from a browser (on a phone, use github.com in the browser).

Nothing breaks while a secret is missing. The run report (Actions → a run → Summary) and the site's About page
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
terms and can get the account suspended; it is also unnecessary, since MicroHOT makes about 10–15 calls a day.

## 2. The private site (Cloudflare Pages)

1. Create a free Cloudflare account.
2. Copy your **Account ID** (shown on the account home page and in Workers & Pages).
3. **My Profile → API Tokens → Create Token → Create Custom Token**, with the permission
   *Account → Cloudflare Pages → Edit*. Copy the token.
4. Add three secrets:
   - `CLOUDFLARE_API_TOKEN`: the token
   - `CLOUDFLARE_ACCOUNT_ID`: the account ID
   - `SITE_PASSWORD`: a long password for the site (the username can be anything)
5. Run the workflow once (step 3). The run creates a Pages project called `microhot` and prints its address in
   the "Publish to Cloudflare Pages" step. If that address is not `https://microhot.pages.dev` (names are
   shared across all Cloudflare users), add a repository **variable** `SITE_URL` with the real address, so links
   in feeds and messages point to the right place.

Until Cloudflare is set up, each run attaches the built site to the run as a downloadable artifact.

## 3. Run it

**Actions → MicroHOT run → Run workflow**. After that it runs at about 6:25, 12:25 and 18:25 Toronto time.
The first run at or after 06:00 builds the morning edition.

Open the site, sign in with any username and `SITE_PASSWORD`, and add it to your home screen.
Private RSS feeds are at `/feed.xml` and `/zh-tw/feed.xml` (readers that support basic auth accept
`https://any:PASSWORD@your-site/feed.xml`).

## 4. Delivery channels (optional)

Each channel runs only when it is `enabled = true` in `config/site.toml` **and** its secrets are set.
Choose the language with `lang = "en"`, `"zh-TW"` or `"both"`.

- **GitHub app**: on by default and needs no secrets. Each morning's edition is posted as an issue (the previous
  one is closed). To get a push notification, watch the repository for issues in the GitHub app.
- **Email**: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` (optional `EMAIL_FROM`).
  For Gmail: turn on 2-Step Verification, create an app password, then use `smtp.gmail.com`, port `465`,
  your address as user and recipient, and the app password.
- **Telegram**: create a bot with @BotFather (`/newbot`) for `TELEGRAM_BOT_TOKEN`. Send your bot any message,
  then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` into `TELEGRAM_CHAT_ID`.

## 5. Tuning

- **What it covers**: `config/sources.toml` (feeds, beat, tier, keyword filters).
- **What gets picked**: thresholds, second opinions and daily quotas per beat in `config/site.toml`; the reader
  description and rubric in `prompts/triage.md`. The About page's "Near misses" table shows what just missed.
- **How it is written**: `prompts/write.md`.
- **Which models, in which order**: `[llm] triage` and `write` in `config/site.toml`. Remove a provider from both
  lists, or simply don't set its key, and it is never called.
- **Costs**: `max_calls_per_day` and `usd_cap_per_day` in `config/site.toml`, plus `usd_cap_per_day` per paid
  provider. Usage by provider is on the About page.

## Running locally

```bash
uv sync
uv run microhot status          # which providers and channels are configured (no calls)
GEMINI_API_KEY=... uv run microhot run
python -m http.server -d public  # then open http://localhost:8000
uv run pytest
```
