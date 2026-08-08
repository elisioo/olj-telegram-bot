# OnlineJobs.ph Notifier Bot

Keep up with new job postings on OnlineJobs.ph — delivered straight to Telegram.

## What it does
- Monitors OnlineJobs.ph search results and notifies a configured Telegram chat when new job posts match your keywords.
- Supports interactive slash-style searches (`/search`, `/video_editor`), and shows recent or today's updated posts.
- Optional: upload a bundled profile image to your bot with `--set-profile-photo`.

## Quick Start: if you want to create your own bot just rename it.

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set required environment variables (examples):

- `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather (example: `123456789:ABCdefGHIjkl_MnoP`).
- `TELEGRAM_CHAT_ID` — numeric id of the chat or user that should receive messages (example: `987654321`).
- `OJ_KEYWORDS` — optional comma-separated keywords to filter (example: `virtual assistant, video editor`).
- `OJ_SEARCH_URL` — optional custom search URL (defaults to `https://www.onlinejobs.ph/jobseekers/jobsearch`).
- `CHECK_INTERVAL_SECONDS` — optional poll interval in seconds (default `600`).

Example (PowerShell):

```powershell
$env:TELEGRAM_BOT_TOKEN="123456789:ABCdef"
$env:TELEGRAM_CHAT_ID="987654321"
$env:OJ_KEYWORDS="virtual assistant,video editor"
python oj-notifier.py
```

3. Upload the bundled profile image (optional):

```bash
python oj-notifier.py --set-profile-photo
```

Make sure the file `asset/OLJ_BOT.jpg` exists before running the upload command.

## Telegram Commands (sent to the bot)

- `/refresh` — Check OnlineJobs now for new posts.
- `/recent` — Show the most recent job posts.
- `/view_now` — Show job posts updated on the newest displayed date.
- `/search <terms>` — Search current listings (also supports one-word slash commands like `/video_editor`).
- `/help` — Show usage information.

## Find your `TELEGRAM_CHAT_ID`
Send any message to your bot, then call:


```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```
Tips: Actually you will get it on messaging @userinfobot just send any message like "hi"

Look for the JSON field `"chat":{"id":...}` — that numeric value is your `TELEGRAM_CHAT_ID`.

## Troubleshooting
- The script exits if `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` are missing.
- If no jobs are parsed, OnlineJobs' HTML may have changed — the notifier logs a warning.

## Contributing / Contact
Pull requests and issues are welcome. Thanks for using the bot!

---
## I have deployed my bot already you can use it now.

`@olj_notifier_bot`

Thank you for using, happy job hunting.