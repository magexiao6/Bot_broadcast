Telegram bilingual news bot -- complete files (October 2026)

REPLACE in your existing GitHub repository:
  bot.py
  gemini_translate.py
  .github/workflows/bot.yml
  requirements.txt

DO NOT replace or delete your existing state.json.
Do NOT upload a local .env, API keys, or bot tokens.

GitHub Actions settings:
  Repository Secrets:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    GEMINI_API_KEY
  Repository Variable:
    TG_EN_URL = https://t.me/s/zaihua_news

Optional in bot.yml:
  MAX_HISTORY_PAGES = 25 (more older preview pages after a long schedule gap)
  MAX_BATCHES_PER_RUN = 12 (up to 12 Gemini calls per run)
  MAX_SEND_PER_RUN = 60 (up to 60 full news posts per run)

Behavior:
  - Existing RSS-era state: the first run archives old pending and marks
    currently visible preview posts as skipped; it sends nothing during migration.
  - Existing Gemini v1 state: preserves pending translations and derives the
    history checkpoint from recorded post IDs. No mass reset.
  - Subsequent runs: paginate Telegram history backward until the previous
    checkpoint has been reached, then fetch and enqueue unseen posts.
  - Batch 1-5 posts per Gemini call immediately; NO 30-minute waiting.
  - If more posts remain than configured per-run budget, later runs continue.
  - If preview pagination did not reach the old checkpoint, the job fails
    without advancing the checkpoint. Discovered posts are saved as pending.
  - Sent images and text progress are checkpointed locally after each step.
    The workflow commits state.json even when bot.py exits with an error.
  - A rare duplicate is still possible if Telegram accepts a message but
    the runner stops before state.json is durably pushed to GitHub.

Testing without network or real API calls:
  cd to the repository folder and run:
    python -m unittest -v test_offline.py

Go to GitHub Actions -> Telegram News Bot -> Run workflow after uploading.
Check logs for: Scanned ... Telegram pages; Gemini request ...; Sent ...

Note:
  Public Telegram HTML preview and ?before pagination are not a formal
  durable API and could change. A schedule on GitHub Actions is best effort.

