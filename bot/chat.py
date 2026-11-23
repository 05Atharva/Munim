"""Two-way chat: you message the bot on Telegram, it answers using its memory and learns about you."""
from . import market_data, memory, prompts
from .config import Config

# Attach the latest available daily data for market questions.
LIVE_HINTS = ("today", "now", "market", "nifty", "sensex", "down", "up", "crash", "rally", "rupee", "why", "what happened")


def handle_message(cfg: Config, store, llm, text: str, *, quotes=None) -> str:
    text = (text or "").strip()[:2000]
    if not text:
        return ""

    command = text.split()[0].lower()
    if command in ("/start", "/help"):
        return ("I'm Munim, your market research companion.\n\n"
                "/help — show commands\n/memory — show saved research notes\n"
                "/predictions — show open predictions\n/report — show the latest digest\n"
                "Ask /research-style questions naturally, e.g. 'Why did banks underperform today?' ")
    if command == "/memory":
        return memory.render_memory_context(store, cfg.max_notes_in_context)
    if command == "/predictions":
        rows = store.open_predictions()
        return "\n".join(f"P#{p['id']} (check by {p['check_by']}): {p['claim']}" for p in rows[:20]) or "No open predictions."
    if command == "/report":
        rows = store.recent_digests(1)
        return rows[0]["content"] if rows else "No report has been saved yet."

    live_block = ""
    if any(h in text.lower() for h in LIVE_HINTS):
        q = market_data.fetch_quotes() if quotes is None else quotes
        live_block = "=== LATEST RETRIEVED DAILY DATA ===\n" + market_data.format_snapshot(q, [], 0)

    history = [{"role": r["role"], "content": r["content"]} for r in store.recent_chat(8)]
    system = prompts.PERSONA.format(name=cfg.bot_name)
    user = prompts.CHAT_USER.format(
        name=cfg.bot_name,
        memory=memory.render_memory_context(store, cfg.max_notes_in_context, query=text),
        recent=memory.render_recent_digests(store, 2, 500),
        live_block=live_block,
        message=text,
    )

    result = llm.complete_json(system, user, history=history, temperature=0.6, max_tokens=900)
    reply = str(result.get("reply", "")).strip() or "I did not quite get that - could you rephrase?"

    store.add_chat("user", text)
    store.add_chat("assistant", reply)
    memory.apply_learning(store, {"new_notes": result.get("new_notes") or []})
    return reply
