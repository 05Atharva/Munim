"""Optional always-on chat endpoint (Render free web service). Telegram calls /telegram when you message the bot.

The twice-daily digests do NOT need this - GitHub Actions sends them. This only adds two-way chat.
Render's free tier sleeps after ~15 min idle; the first message after a nap can take ~30-50 s, which is fine
for chatting (Telegram retries the webhook automatically if we are slow).
"""
import logging
from hmac import compare_digest

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from bot.chat import handle_message
from bot.config import Config
from bot.llm import GroqLLM
from bot.store import make_store
from bot.telegram import send_message, send_typing

app = FastAPI()
logger = logging.getLogger(__name__)
cfg = Config()
cfg.validate_runtime(webhook=True)
store = make_store(cfg)
llm = GroqLLM(cfg)


@app.get("/")
def health():
    return {"ok": True, "bot": cfg.bot_name}


def _process(chat_id: str, text: str):
    try:
        send_typing(cfg.telegram_token, chat_id)
        reply = handle_message(cfg, store, llm, text)
        if reply:
            send_message(cfg.telegram_token, chat_id, reply)
    except Exception as exc:
        logger.warning("Chat processing failed (%s)", type(exc).__name__)
        try:
            send_message(cfg.telegram_token, chat_id, "I couldn't complete that reply. Try sending your message again.")
        except RuntimeError:
            logger.warning("Could not send chat failure notice")


@app.post("/telegram")
def telegram_webhook(update: dict, background: BackgroundTasks,
                     x_telegram_bot_api_secret_token: str = Header(default="")):
    if not compare_digest(x_telegram_bot_api_secret_token.encode(), cfg.telegram_webhook_secret.encode()):
        raise HTTPException(status_code=403, detail="bad or missing secret")

    uid = update.get("update_id")
    if type(uid) is not int:
        raise HTTPException(status_code=400, detail="missing update_id")

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = msg.get("text", "")

    # Only the owner may use the bot (protects your Groq quota and your memory).
    if chat.get("type") != "private" or chat_id != cfg.telegram_chat_id or not text:
        return {"ok": True}

    try:
        if not store.mark_update_processed(uid):
            return {"ok": True}
    except Exception:
        raise HTTPException(status_code=503, detail="deduplication store unavailable") from None
    background.add_task(_process, chat_id, text)
    return {"ok": True}
