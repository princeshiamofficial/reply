"""
Corporate Reply Bot — Telegram
Send a screenshot or a client's message, get a ready-to-send corporate reply.
Follow-ups like "short", "under 3 line", "rate 8-9 tai meeting" refine the last reply.
"""
import base64
import logging
import os
from pathlib import Path

from anthropic import AsyncAnthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reply-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
# Comma-separated Telegram user IDs allowed to use the bot.
# Leave empty, or set to "any" or "*", to let anyone use it.
_allowed_raw = os.environ.get("ALLOWED_USER_IDS", "").strip().lower()
ALLOWED = (
    set()
    if _allowed_raw in ("", "any", "*", "all")
    else {int(x) for x in _allowed_raw.split(",") if x.strip().lstrip("-").isdigit()}
)
MAX_HISTORY = 8  # messages kept per chat (user + assistant)

BASE_PROMPT = """You write WhatsApp/Messenger replies that a sales team sends to clients.

The user (a sales team member) will send you either:
- a screenshot of a client chat, or
- the client's message as text, or
- an instruction about the previous reply (e.g. "short", "under 3 line",
  "make it formal", "tell them we can meet at night 8-9").

Your job: write ONE ready-to-send reply to the client.

Rules:
- Write in natural Bangla, keeping technical terms in English
  (Meta Pixel, Server-Side Tracking, GTM, Ads Manager, Meeting).
- Corporate, polite and warm. Address the client as "স্যার" unless the chat shows otherwise.
- Default length: 2-4 short lines. WhatsApp style, no headings, no markdown, at most one emoji.
- Reply to the client's LATEST message. Don't repeat what was already said in the chat.
- Never promise guaranteed sales or ROAS.
- Usually end with one clear question that moves the client toward a short meeting.
- When the user gives an instruction, rewrite the previous reply following it.
- If the user writes in Banglish describing what to say, turn that into the polished client reply.
- Output ONLY the reply text. No explanation, no quotes, no options.
"""


def load_system_prompt() -> str:
    kb = Path(__file__).with_name("knowledge.txt")
    if kb.exists():
        return BASE_PROMPT + "\n\nCompany knowledge and sales script:\n" + kb.read_text(encoding="utf-8")
    return BASE_PROMPT


SYSTEM_PROMPT = load_system_prompt()
client = AsyncAnthropic(api_key=ANTHROPIC_KEY)
histories: dict[int, list[dict]] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


def trim(history: list[dict]) -> list[dict]:
    history = history[-MAX_HISTORY:]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


async def ask_claude(chat_id: int, content: list[dict]) -> str:
    history = histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": content})
    history[:] = trim(history)

    resp = await client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    reply = "".join(b.text for b in resp.content if b.type == "text").strip()
    history.append({"role": "assistant", "content": reply})
    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Client-er screenshot ba message pathan, ami corporate reply likhe dibo.\n"
        "Por-e 'short', 'under 3 line' etc. likhe reply change korte parben.\n"
        "/new — notun client shuru korte."
    )


async def new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    histories.pop(update.effective_chat.id, None)
    await update.message.reply_text("Notun conversation shuru. Screenshot ba message pathan.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        reply = await ask_claude(update.effective_chat.id, [{"type": "text", "text": update.message.text}])
        await update.message.reply_text(reply)
    except Exception:
        log.exception("text failed")
        await update.message.reply_text("Error hoyeche, abar try korun.")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
            media_type = "image/jpeg"
        else:  # image sent as a document (uncompressed)
            doc = update.message.document
            tg_file = await doc.get_file()
            media_type = doc.mime_type or "image/png"

        data = base64.b64encode(bytes(await tg_file.download_as_bytearray())).decode()
        caption = update.message.caption or "Write a reply to the client's latest message in this chat."
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": caption},
        ]
        reply = await ask_claude(update.effective_chat.id, content)
        await update.message.reply_text(reply)
    except Exception:
        log.exception("photo failed")
        await update.message.reply_text("Image process korte parini, abar try korun.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_chat))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot running with model %s", MODEL)

    # On Render (or any host with a public URL) use webhook mode; locally use polling.
    public_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path="telegram",
            webhook_url=public_url.rstrip("/") + "/telegram",
            secret_token=os.environ.get("WEBHOOK_SECRET") or None,
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
