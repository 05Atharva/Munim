# Munim

Munim is a personal Indian-market research companion. It gathers daily market bars and RSS headlines, compares them with earlier reports and notes, and sends two Telegram briefings each day. Each briefing explains the observed moves, plausible drivers, competing explanations and one thing to check in the next session.

GitHub Actions runs the Python jobs at **08:35 IST** and **18:15 IST** daily. A Sunday job reviews the research notes at **10:00 IST**. Weekend reports use the last observed Indian session and developments since then. Supabase stores reports, observed data, source evidence, notes and predictions. The model uses this memory in later runs without fine-tuning.

## Set up Supabase

1. Open your existing Supabase project. Open **SQL Editor → New query**, paste [supabase_schema.sql](supabase_schema.sql) and click **Run**. Existing Munim installations with duplicate digest rows should review and reconcile those rows before reapplying the schema. The migration preserves historical reports.
2. Open **Connect** and copy the **Project URL**, which looks like `https://PROJECT_REF.supabase.co`. Use it as `SUPABASE_URL`.
3. Go to **Settings → API Keys → Secret keys**. Copy or create a backend key beginning with `sb_secret_`. Use it as `SUPABASE_SERVICE_KEY`. A legacy `service_role` JWT also works. Keep the backend key in GitHub Actions secrets or server environment variables.

[Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys) · [Free project pausing](https://supabase.com/docs/guides/platform/free-project-pausing)

## Set up Telegram and Groq

1. Create a bot using **@BotFather → /newbot**. Keep its token as `TELEGRAM_BOT_TOKEN`. Open the bot's private chat, press **Start** and send a message so Telegram exposes the chat to the bot.
2. Run the following in PowerShell from the project directory. Paste the full BotFather token at the hidden prompt. When the command shows a Telegram link, open it privately and send a fresh message while the command waits. If Python is installed as `py` on your machine, substitute `py` for `python`. If you already configured a webhook, obtain the ID from your existing configuration.

   ```powershell
   python -m pip install -r requirements-actions.txt
   python -m bot.run chat-id
   ```

3. Copy the number after `TELEGRAM_CHAT_ID=`. Save it as the GitHub Actions secret `TELEGRAM_CHAT_ID`. Create a Groq key as `GROQ_API_KEY`. Defaults are `openai/gpt-oss-120b` and `openai/gpt-oss-20b`. If needed, override them with repository Actions variables `LLM_MODEL` and `LLM_FALLBACK_MODEL`.

[Telegram BotFather tutorial](https://core.telegram.org/bots/tutorial) · [Groq API keys](https://console.groq.com/keys) · [Groq quotas](https://console.groq.com/docs/rate-limits)

## Host and test the scheduled reports

Use **GitHub Actions** to run the scheduled reports. Standard GitHub-hosted runners are free for public repositories. Supabase and Groq are used within their free-tier quotas. No computer needs to stay on. GitHub can delay scheduled runs and disables public schedules after 60 days without repository activity.

1. Push the code to the repo's default branch. Open **Settings → Secrets and variables → Actions → New repository secret**. Add these five entries:

   | Name | Value |
   | --- | --- |
   | `SUPABASE_URL` | Supabase Project URL |
   | `SUPABASE_SERVICE_KEY` | Supabase backend secret key |
   | `GROQ_API_KEY` | Groq API key |
   | `TELEGRAM_BOT_TOKEN` | BotFather token |
   | `TELEGRAM_CHAT_ID` | Numeric private chat ID |

2. Open **Actions → Munim digests → Run workflow**. Select **check** to verify Supabase reads, Groq JSON output, the private Telegram chat and market feed availability. The run sends no Telegram message.
3. Run it again with **evening** for the end-to-end test. Verify the report arrives in Telegram and that **Table Editor → digests** shows `delivery_status = sent`. A manual run counts as that day's slot. Use **morning** if an evening report was already sent today.

If Telegram delivery times out, inspect the private chat and the matching `sending` row in **Table Editor → digests**. If the full report arrived, set that row to `sent`. If it definitively did not arrive, remove the matching `sending` row and rerun that slot. A read timeout has an ambiguous result, so the bot pauses that slot instead of risking a duplicate.

[GitHub repository secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) · [Runner pricing](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job) · [Schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## Optional two-way chat

To ask Munim questions and use commands like `/report` and `/memory`, create a **Render Blueprint** from the repo using [render.yaml](render.yaml). Enter the same five values under the service's environment variables and add `TELEGRAM_WEBHOOK_SECRET`. Generate a random value locally and save the same value in Render and your temporary shell environment:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Once the service is live, run this from the project directory with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` in your shell:

```powershell
python -m bot.run set-webhook https://YOUR-SERVICE.onrender.com/telegram
```

Send `/help` to the bot. The first reply after idle time can take longer because Render spins down its free services. Render's cron jobs have a monthly minimum charge. Cloudflare Workers Free has a 10 ms Cron CPU budget and would require reworking this Python job.

[Render free web services](https://render.com/docs/free) · [Render Cron pricing](https://render.com/docs/cronjobs) · [Cloudflare Worker limits](https://developers.cloudflare.com/workers/platform/limits/)

## Research inputs and memory

- Yahoo Finance provides unofficial daily bars. The briefing labels equity session status and withholds provisional closes or non-equity bars with unverified settlement times from its analysis. The raw snapshot remains in Supabase.
- Reports use short sections with bold Telegram headings and clickable source references. A second Groq pass checks the draft against its evidence. If that review fails, the bot sends observed data only. Exchange-wide breadth, verified institutional flows, earnings calendars and company announcements still need dedicated data sources.
- Original report snapshots, observations and source links are kept in Supabase. Repeating a similar note does not increase its confidence. Evidence-based confidence changes still depend on the model's interpretation. Predictions need independent verification before the scorecard changes.
- Recent reports and memory are sent to Groq when generating the next reply. The private Telegram chat can store explicitly provided preferences.

## Local verification

Use Python 3.12 on a hosted runner. The CLI reads environment variables directly. [.env.example](.env.example) lists their names.

```powershell
python -m pip install -r requirements.txt pytest
$env:APP_ENV = "test"
python -m pytest -q
```

For a local preview, set `APP_ENV=development`, `ALLOW_LOCAL_STORE=1`, `DRY_RUN=1` and `GROQ_API_KEY` in the environment, then run `python -m bot.run evening`. The preview prints a report without sending it or writing memory.
