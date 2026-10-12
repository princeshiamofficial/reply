"""
Corporate Reply Bot — Telegram
Send a screenshot or a client's message, get a ready-to-send corporate reply.
Follow-ups like "short", "under 3 line", "rate 8-9 tai meeting" refine the last reply.
"""
import asyncio
import base64
import logging
import os
from pathlib import Path

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
# httpx logs every request URL, and Telegram URLs contain the bot token — keep them out of logs
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Which AI to use: "gemini" (free tier available) or "claude"
PROVIDER = os.environ.get("AI_PROVIDER", "gemini").strip().lower()
if PROVIDER == "claude":
    MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
else:
    PROVIDER = "gemini"
    MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    # Tried in order when the main model is busy or out of free quota
    _fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    GEMINI_MODELS = [MODEL] + [m.strip() for m in _fallbacks.split(",") if m.strip() and m.strip() != MODEL]
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
if PROVIDER == "claude":
    from anthropic import AsyncAnthropic

    # Keys not scoped to a workspace need the workspace ID sent as a header
    _workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    claude_client = AsyncAnthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        default_headers={"anthropic-workspace-id": _workspace} if _workspace else None,
    )
else:
    from google import genai
    from google.genai import types

    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# History is stored in one neutral format:
# {"role": "user"|"assistant", "content": [{"type": "text", "text": ...} | {"type": "image", "mime": ..., "data": bytes}]}
histories: dict[int, list[dict]] = {}


def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


def trim(history: list[dict]) -> list[dict]:
    history = history[-MAX_HISTORY:]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


async def call_claude(history: list[dict]) -> str:
    messages = []
    for msg in history:
        blocks = []
        for b in msg["content"]:
            if b["type"] == "text":
                blocks.append({"type": "text", "text": b["text"]})
            else:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": b["mime"],
                               "data": base64.b64encode(b["data"]).decode()},
                })
        messages.append({"role": msg["role"], "content": blocks})

    resp = await claude_client.messages.create(
        model=MODEL, max_tokens=800, system=SYSTEM_PROMPT, messages=messages,
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def call_gemini(history: list[dict]) -> str:
    contents = []
    for msg in history:
        parts = []
        for b in msg["content"]:
            if b["type"] == "text":
                parts.append(types.Part.from_text(text=b["text"]))
            else:
                parts.append(types.Part.from_bytes(data=b["data"], mime_type=b["mime"]))
        contents.append(types.Content(role="model" if msg["role"] == "assistant" else "user", parts=parts))

    config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=2048)
    last_exc = None
    # Try the main model, then fallbacks, when a model is overloaded (503) or rate-limited (429)
    for attempt, model in enumerate(GEMINI_MODELS):
        try:
            resp = await gemini_client.aio.models.generate_content(model=model, contents=contents, config=config)
            if model != MODEL:
                log.info("Answered with fallback model %s", model)
            return (resp.text or "").strip()
        except Exception as exc:
            if not is_busy_error(exc):
                raise
            last_exc = exc
            log.warning("Model %s busy (%s), trying next", model, str(exc)[:80])
            await asyncio.sleep(1 + attempt)
    raise last_exc


def is_busy_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("503", "unavailable", "high demand", "overloaded", "429", "resource_exhausted"))


async def ask_ai(chat_id: int, content: list[dict]) -> str:
    history = histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": content})
    history[:] = trim(history)

    try:
        reply = await (call_claude(history) if PROVIDER == "claude" else call_gemini(history))
        if not reply:
            raise RuntimeError("empty reply")
    except Exception:
        history.pop()  # don't keep a turn with no answer
        raise
    history.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
    return reply


def error_text(exc: Exception) -> str:
    msg = str(exc).lower()
    if "503" in msg or "unavailable" in msg or "high demand" in msg or "overloaded" in msg:
        return "AI server ekhon onek busy. 1-2 minute por abar pathan."
    if "429" in msg or "quota" in msg or "resource_exhausted" in msg or "rate" in msg:
        return "Ajker free limit shesh ba onek druto request hoyeche. Kichukkhon por abar try korun."
    if "credit balance" in msg or "billing" in msg:
        return "AI account-e credit/billing somossha. Admin-ke janan."
    if "api key" in msg or "401" in msg or "permission" in msg:
        return "AI API key-e somossha. Admin-ke janan."
    return "Error hoyeche, abar try korun."


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
        reply = await ask_ai(update.effective_chat.id, [{"type": "text", "text": update.message.text}])
        await update.message.reply_text(reply)
    except Exception as exc:
        log.exception("text failed")
        await update.message.reply_text(error_text(exc))


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

        data = bytes(await tg_file.download_as_bytearray())
        caption = update.message.caption or "Write a reply to the client's latest message in this chat."
        content = [
            {"type": "image", "mime": media_type, "data": data},
            {"type": "text", "text": caption},
        ]
        reply = await ask_ai(update.effective_chat.id, content)
        await update.message.reply_text(reply)
    except Exception as exc:
        log.exception("photo failed")
        await update.message.reply_text(error_text(exc))


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_chat))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot running with %s model %s", PROVIDER, MODEL)

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
