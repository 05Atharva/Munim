"""Telegram Bot API helpers: send (with message splitting) and webhook setup."""
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3900  # Telegram max is 4096 chars


class TelegramRejected(RuntimeError):
    """Telegram explicitly rejected the request before delivering any message."""


def _api_request(token: str, method: str, payload: dict, timeout: int = 30):
    try:
        return requests.post(API.format(token=token, method=method), json=payload, timeout=timeout)
    except requests.RequestException:
        # requests includes the full URL (and therefore the bot token) in its exception.
        raise RuntimeError(f"Telegram {method} network error or timeout") from None


def _split(text: str, limit: int = LIMIT) -> list:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            parts.append(cur.strip())
            cur = ""
        while len(para) > limit:  # a single giant paragraph
            parts.append(para[:limit])
            para = para[limit:]
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts or [text[:limit]]


def send_message(token: str, chat_id: str, text: str) -> None:
    for index, chunk in enumerate(_split(text)):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown", "disable_web_page_preview": True}
        r = _api_request(token, "sendMessage", payload)
        if r.status_code == 400:  # Markdown parse error from model output -> resend as plain text
            payload.pop("parse_mode")
            r = _api_request(token, "sendMessage", payload)
        if r.status_code != 200:
            error = TelegramRejected if index == 0 and 400 <= r.status_code < 500 else RuntimeError
            raise error(f"Telegram sendMessage failed (HTTP {r.status_code})")
        time.sleep(0.4)


def send_typing(token: str, chat_id: str) -> None:
    try:
        _api_request(token, "sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
    except RuntimeError:
        pass


def set_webhook(token: str, url: str, secret: str) -> dict:
    if not token or not secret or len(secret) < 16:
        raise ValueError("Telegram token and a webhook secret of at least 16 characters are required")
    r = _api_request(token, "setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message"]})
    if r.status_code != 200 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram setWebhook failed (HTTP {r.status_code})")


def check_private_chat(token: str, chat_id: str) -> None:
    r = _api_request(token, "getChat", {"chat_id": chat_id})
    if r.status_code != 200 or r.json().get("result", {}).get("type") != "private":
        raise RuntimeError("Telegram could not verify the private chat. Start the bot and check TELEGRAM_CHAT_ID.")


def private_chat_ids(token: str) -> list:
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in this PowerShell window before running chat-id.")
    bot = _api_request(token, "getMe", {})
    if bot.status_code in {401, 404}:
        raise RuntimeError("Telegram rejected the token. Recopy the full token for this bot from BotFather.")
    if bot.status_code != 200:
        raise RuntimeError(f"Telegram getMe failed (HTTP {bot.status_code})")

    username = bot.json()["result"]["username"]
    print(f"Open https://t.me/{username} privately and send a fresh message. Waiting up to 45 seconds...", flush=True)
    wait_seconds = 45
    r = _api_request(token, "getUpdates", {"allowed_updates": ["message"], "timeout": wait_seconds},
                     timeout=wait_seconds + 10)
    if r.status_code == 409:
        webhook = _api_request(token, "getWebhookInfo", {})
        if webhook.status_code == 200 and webhook.json().get("result", {}).get("url"):
            raise RuntimeError("A webhook is already registered. Telegram sends messages to it instead of getUpdates.")
        raise RuntimeError("Another process is reading updates for this bot. Stop that poller and rerun chat-id.")
    if r.status_code != 200:
        raise RuntimeError(f"Telegram getUpdates failed (HTTP {r.status_code})")
    chats = [update.get("message", {}).get("chat", {}) for update in r.json()["result"]]
    return sorted({chat["id"] for chat in chats if chat.get("type") == "private"})
