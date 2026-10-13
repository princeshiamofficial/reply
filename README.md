# Corporate Reply Bot

Telegram bot that turns a client chat screenshot or message into a ready-to-send corporate Bangla reply for the AurovixSoft sales team. Follow-ups like "short" or "under 3 line" rewrite the last reply.

## Files
- `bot.py` — the bot (webhook mode on Render, polling locally)
- `knowledge.txt` — company info, offer, pricing rules and sales script. Edit this to change what the bot knows.
- `requirements.txt` — Python dependencies

## Environment variables
| Name | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From @BotFather |
| `AI_PROVIDER` | no | `gemini` (default, free tier) or `claude` |
| `GEMINI_API_KEY` | if gemini | Free key from aistudio.google.com |
| `GEMINI_MODEL` | no | Defaults to `gemini-3.8-flash` |
| `GEMINI_FALLBACK_MODELS` | no | Tried in order when the main model is busy. Default: `gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` |
| `ANTHROPIC_API_KEY` | if claude | From console.anthropic.com |
| `CLAUDE_MODEL` | no | Defaults to `claude-haiku-5-5` |
| `ANTHROPIC_WORKSPACE_ID` | only if your Claude key is not workspace-scoped | Workspace ID from console.anthropic.com |
| `ALLOWED_USER_IDS` | recommended | Comma-separated Telegram user IDs, or `any` |
| `WEBHOOK_SECRET` | recommended | Any random string |
| `AI_TIMEOUT_SECONDS` | no | Seconds to wait per model before trying the next. Default `25` |
| `KEEP_AWAKE` | no | Set `0` to stop the bot pinging itself every 10 min. Default on |

Note: Gemini free-tier data may be used by Google to improve its products. Use a paid key for sensitive client data.

## Deploy on Render (free)
New → Web Service → this repo
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Instance: Free

Render sets `RENDER_EXTERNAL_URL` automatically, and the bot registers its webhook on start.

Render's free plan sleeps a service after 15 idle minutes. The bot pings its own `/health` page every 10 minutes to stay awake. One always-on service uses about 744 of the 750 free hours a month, so keep only this one free service running. For extra safety, add a free monitor (e.g. cron-job.org or UptimeRobot) that opens `https://<your-app>.onrender.com/health` every 10 minutes.

## Run locally
```
pip install -r requirements.txt
python bot.py
```

## Commands
- Send a screenshot or text → get a reply
- `/new` → start a new client conversation
