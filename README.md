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
| `ANTHROPIC_API_KEY` | yes | From console.anthropic.com |
| `ALLOWED_USER_IDS` | recommended | Comma-separated Telegram user IDs |
| `WEBHOOK_SECRET` | recommended | Any random string |
| `CLAUDE_MODEL` | no | Defaults to `claude-sonnet-5-5` |

## Deploy on Render (free)
New → Web Service → this repo
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Instance: Free

Render sets `RENDER_EXTERNAL_URL` automatically, and the bot registers its webhook on start.

## Run locally
```
pip install -r requirements.txt
python bot.py
```

## Commands
- Send a screenshot or text → get a reply
- `/new` → start a new client conversation
