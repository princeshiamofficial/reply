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

from audit import audit_context_for_ai, find_url, format_report, run_audit
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

BASE_PROMPT = """You are the top sales closer of AurovixSoft. You write WhatsApp/Messenger replies
that the sales team sends to clients. Your one goal: move every client toward buying.

The user (a sales team member) will send you either:
- a screenshot of a client chat, or
- the client's message as text, or
- an instruction about the previous reply (e.g. "short", "under 3 line",
  "make it formal", "aj rat 9tai call fix koro").

Your job: write ONE ready-to-send reply to the client.

How a closer replies:
- Lead with what the CLIENT gains (more reliable conversion data, smarter ad spend,
  less wasted budget, clear reporting), not with technical features.
- Show you understand their problem first, then position AurovixSoft as the clear answer.
- Use the real current offer as the hook: $8 Ad Campaign free with GTM Server-Side Tracking
  Setup, and the free Tracking Audit. Create urgency from the offer ("চলমান অফার", "এখনই শুরু
  করলে"), never from made-up facts.
- Build confidence: speak like an expert who has done this many times; be specific about the
  next step and what happens in it.
- Handle objections calmly: agree with the feeling, reframe toward value, then ask for the next step.
- Always end with ONE easy yes-question that moves forward: confirm a call/meeting, ask for the
  website link, ask which platform they use, or ask to start the setup.
- Make saying yes easy. Never refuse or argue with the time the client wants. Agree warmly with
  their time ("জি স্যার, অবশ্যই") and move forward (ask for their number or WhatsApp for the call,
  or say the expert will connect). Only mention a specific time if the user instructs it.
- If the client asks for a recommendation (e.g. "best agency"), recommend AurovixSoft confidently
  and give 1-2 concrete reasons.
- If the client is ready to buy, stop selling and guide them to the next action immediately.

Hard rules:
- Write in natural Bangla, keeping technical terms in English
  (Meta Pixel, Server-Side Tracking, GTM, Ads Manager, Meeting).
- Corporate, polite and warm. Address the client as "স্যার" unless the chat shows otherwise.
- Default length: 2-4 short lines. WhatsApp style, no headings, no markdown, at most one emoji.
- Reply to the client's LATEST message. Don't repeat what was already said in the chat.
- Never promise guaranteed sales, ROAS or specific numbers. Never invent client names, results,
  reviews, statistics or discounts that are not in the company knowledge.
- Never be pushy or rude; persuasive, confident and helpful.
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
        timeout=float(os.environ.get("AI_TIMEOUT_SECONDS", "25")) * 2,
        max_retries=1,
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

    last_exc: Exception = RuntimeError("no model answered")
    # Try the main model, then fallbacks, when a model is busy, slow, or rate-limited
    for model in GEMINI_MODELS:
        try:
            text = await gemini_once(model, contents)
            if text:
                if model != MODEL:
                    log.info("Answered with fallback model %s", model)
                return text
            last_exc = RuntimeError(f"empty reply from {model}")
            log.warning("Model %s gave an empty reply, trying next", model)
        except Exception as exc:
            if not is_busy_error(exc):
                raise
            last_exc = exc
            log.warning("Model %s busy/slow (%s), trying next", model, str(exc)[:80] or type(exc).__name__)
    raise last_exc


async def gemini_once(model: str, contents) -> str:
    # Low thinking keeps short replies fast; drop it if a model doesn't support the setting
    thinking = types.ThinkingConfig(thinking_level="low") if model not in _NO_THINKING_CFG else None
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT, max_output_tokens=2048, thinking_config=thinking,
    )
    try:
        resp = await asyncio.wait_for(
            gemini_client.aio.models.generate_content(model=model, contents=contents, config=config),
            timeout=GEMINI_TIMEOUT,
        )
    except Exception as exc:
        if thinking is not None and "thinking" in str(exc).lower():
            _NO_THINKING_CFG.add(model)
            return await gemini_once(model, contents)
        raise
    return (resp.text or "").strip()


_NO_THINKING_CFG: set[str] = set()
GEMINI_TIMEOUT = float(os.environ.get("AI_TIMEOUT_SECONDS", "25"))


def is_busy_error(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in (
        "503", "500", "504", "unavailable", "high demand", "overloaded", "deadline",
        "429", "resource_exhausted", "timeout", "timed out",
    ))


_chat_locks: dict[int, asyncio.Lock] = {}


async def ask_ai(chat_id: int, content: list[dict]) -> str:
    # Different chats run in parallel; messages within one chat are answered in order
    async with _chat_locks.setdefault(chat_id, asyncio.Lock()):
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


async def with_website_audit(update: Update, text: str) -> str:
    """If the message has a website link, audit it, send the internal report, and add findings for the AI."""
    url = find_url(text)
    if not url or os.environ.get("WEBSITE_AUDIT", "1") == "0":
        return text
    status = await update.message.reply_text(f"🔍 {url} check korchi...")
    try:
        result = await run_audit(url)
        await status.edit_text(format_report(result))
        return f"{text}\n\n[{audit_context_for_ai(result)}]"
    except Exception:
        log.exception("audit failed")
        await status.edit_text("Website audit kora jayni, shudhu reply likhchi.")
        return text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Client-er screenshot ba message pathan, ami corporate reply likhe dibo.\n"
        "Por-e 'short', 'under 3 line' etc. likhe reply change korte parben.\n"
        "Website link dile ami auto audit kore report dibo (platform, GTM, Pixel, server-side).\n"
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
        text = await with_website_audit(update, update.message.text)
        reply = await ask_ai(update.effective_chat.id, [{"type": "text", "text": text}])
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
        caption = await with_website_audit(update, caption)
        content = [
            {"type": "image", "mime": media_type, "data": data},
            {"type": "text", "text": caption},
        ]
        reply = await ask_ai(update.effective_chat.id, content)
        await update.message.reply_text(reply)
    except Exception as exc:
        log.exception("photo failed")
        await update.message.reply_text(error_text(exc))


async def keep_awake(public_url: str):
    """Ping our own public URL so Render's free plan never spins the bot down (it sleeps after 15 idle min)."""
    import httpx

    url = public_url.rstrip("/") + "/health"
    interval = int(os.environ.get("KEEP_AWAKE_MINUTES", "10")) * 60
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            await asyncio.sleep(interval)
            try:
                await http.get(url)
            except Exception as exc:
                log.warning("keep-awake ping failed: %s", exc)


async def run_webhook_server(app: Application, public_url: str):
    from aiohttp import web

    secret = os.environ.get("WEBHOOK_SECRET") or None
    port = int(os.environ.get("PORT", "10000"))

    async def telegram_hook(request: web.Request):
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return web.Response(status=403)
        try:
            update = Update.de_json(await request.json(), app.bot)
        except Exception:
            return web.Response(status=400)
        await app.update_queue.put(update)  # answer Telegram right away; work happens in the background
        return web.Response(text="ok")

    async def health(_request):
        return web.Response(text="ok")

    web_app = web.Application(client_max_size=10 * 1024 * 1024)
    web_app.add_routes([web.post("/telegram", telegram_hook), web.get("/", health), web.get("/health", health)])
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web server listening on %s", port)

    async with app:
        await app.bot.set_webhook(
            url=public_url.rstrip("/") + "/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
        await app.start()
        if os.environ.get("KEEP_AWAKE", "1") != "0":
            asyncio.create_task(keep_awake(public_url))
        try:
            await asyncio.Event().wait()
        finally:
            await app.stop()
            await runner.cleanup()


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new_chat))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot running with %s model %s", PROVIDER, MODEL)

    # On Render (or any host with a public URL) use webhook mode; locally use polling.
    public_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:
        asyncio.run(run_webhook_server(app, public_url))
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
