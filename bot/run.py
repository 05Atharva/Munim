"""Command line entry point (used by GitHub Actions).

  python -m bot.run morning        # morning briefing
  python -m bot.run evening        # evening wrap
  python -m bot.run consolidate    # weekly memory clean-up
  python -m bot.run check         # verify credentials and market feeds without sending
  python -m bot.run chat-id       # show chat IDs for this bot's pending private messages
  python -m bot.run set-webhook https://your-service.onrender.com/telegram
"""
import sys

from .config import Config
from .llm import GroqLLM
from .store import make_store
from .telegram import check_private_chat, private_chat_ids, set_webhook


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd, cfg = argv[0], Config()

    if cmd == "set-webhook":
        if len(argv) != 2 or not argv[1].startswith("https://"):
            print("Usage: python -m bot.run set-webhook https://YOUR-SERVICE/telegram")
            return 2
        set_webhook(cfg.telegram_token, argv[1], cfg.telegram_webhook_secret)
        print("Telegram webhook registered.")
        return 0
    if cmd == "chat-id":
        ids = private_chat_ids(cfg.telegram_token)
        if not ids:
            print("No private chats found. Open your bot in Telegram, press Start and send it a message.")
            return 2
        for chat_id in ids:
            print(chat_id)
        return 0

    if cmd not in {"morning", "evening", "consolidate", "check"}:
        print(__doc__)
        return 2
    missing = [n for n, v in (("GROQ_API_KEY", cfg.groq_api_key),) if not v]
    if cmd == "check" or (cmd in {"morning", "evening"} and not cfg.dry_run):
        missing += [n for n, v in (("TELEGRAM_BOT_TOKEN", cfg.telegram_token),
                                    ("TELEGRAM_CHAT_ID", cfg.telegram_chat_id)) if not v]
    if missing:
        print("Missing settings: " + ", ".join(missing))
        return 2

    cfg.validate_runtime()
    store, llm = make_store(cfg), GroqLLM(cfg)

    if cmd in ("morning", "evening"):
        from .digest import run_digest
        out = run_digest(cfg, store, llm, cmd)
        if out.get("skipped"):
            print(out["skipped"])
        else:
            print("Learning applied:", out["learning"])
    elif cmd == "consolidate":
        from .consolidate import run_consolidation
        print("Consolidation:", run_consolidation(cfg, store, llm))
    elif cmd == "check":
        from . import market_data

        store.active_notes(1)
        store.recent_digests(1)
        store.open_predictions()
        store.recent_observations(1)
        print("Supabase memory reads OK.")
        if llm.complete_json("Return a JSON object.", 'Reply with {"ok": true}.', max_tokens=300).get("ok") is not True:
            raise RuntimeError("Groq returned an unexpected readiness response.")
        print("Groq JSON completion OK.")
        check_private_chat(cfg.telegram_token, cfg.telegram_chat_id)
        print("Telegram private chat OK.")
        quotes, headlines = market_data.fetch_quotes(), market_data.fetch_headlines()
        print(f"Market inputs: {len(quotes)} quotes, {len(headlines)} headlines")
        if not quotes and not headlines:
            raise RuntimeError("No market sources returned data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
