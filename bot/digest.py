"""Twice-daily digest: gather data -> think with memory -> send to Telegram -> save what was learned."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import market_data, memory, prompts
from .config import Config
from .llm import LLMError
from .telegram import TelegramRejected, send_message


def run_digest(cfg: Config, store, llm, slot: str, *, quotes=None, headlines=None, send=None) -> dict:
    """slot: 'morning' | 'evening'. quotes/headlines/send can be injected for testing."""
    if slot not in prompts.SLOT_FOCUS:
        raise ValueError("slot must be 'morning' or 'evening'")

    now = datetime.now(ZoneInfo(cfg.timezone))
    today = now.date()

    day = today.isoformat()
    if not cfg.dry_run:
        status = store.digest_status(slot, day)
        if status == "sent":
            return {"skipped": "digest already recorded for this slot and date", "learning": {}}
        if status == "sending":
            raise RuntimeError(f"Delivery unconfirmed for {day} {slot}. Check Telegram before clearing the sending row.")

    quotes = market_data.fetch_quotes() if quotes is None else quotes
    headlines = (market_data.fetch_headlines() if headlines is None else headlines)[:18]
    print(f"Market inputs: {len(quotes)} quotes, {len(headlines)} headlines")
    if not quotes and not headlines:
        raise RuntimeError("No market quotes or headlines fetched. Check the feeds and retry.")
    snapshot = market_data.format_snapshot(quotes, headlines)

    system = prompts.PERSONA.format(name=cfg.bot_name)
    user = prompts.DIGEST_USER.format(
        slot_focus=prompts.SLOT_FOCUS[slot],
        today=day, weekday=now.strftime("%A"), slot=slot,
        memory=memory.render_memory_context(store, min(cfg.max_notes_in_context, 12)),
        recent=memory.render_recent_digests(store, 2),
        history=memory.render_recent_observations(store, 3),
        snapshot=snapshot,
        digest_format=prompts.DIGEST_FORMAT.format(
            title=f"{'Morning briefing' if slot == 'morning' else 'Evening wrap'} - {now.strftime('%a %d %b')}"),
    )

    result = llm.complete_json(system, user, temperature=0.4, max_tokens=2200)
    raw_message = result.get("message")
    message = raw_message.strip() if isinstance(raw_message, str) else ""
    if not 40 <= len(message) <= 2000:
        raise LLMError("Model report must be between 40 and 2,000 characters")
    references = result.get("source_ids")
    links = market_data.source_links(headlines, references) if isinstance(references, list) else ""
    message += links

    if cfg.dry_run:
        print(message)
        return {"message": message, "learning": {}}

    if links:
        seen_urls = {item.get("url") for report in store.recent_digests(6)
                     for item in (report.get("snapshot") or {}).get("headlines", [])}
        fresh_headlines = [item if item.get("url") not in seen_urls else {} for item in headlines]
        links = market_data.source_links(fresh_headlines, references)

    retrieved = datetime.now(ZoneInfo("UTC")).isoformat()
    snapshot_record = {"quotes": quotes, "headlines": headlines, "retrieved_at": retrieved}
    digest_id = store.claim_digest(slot, day, message, snapshot_record)
    if digest_id is None:
        raise RuntimeError("A concurrent run claimed this digest. Check its result before retrying.")
    try:
        if send is None:
            send_message(cfg.telegram_token, cfg.telegram_chat_id, message)
        else:
            send(message)
    except TelegramRejected:
        store.release_digest(digest_id)
        raise
    store.mark_digest_sent(digest_id)
    store.add_observation({"day": day, "slot": slot, "quotes": quotes, "retrieved_at": retrieved})
    for item in headlines:
        store.add_evidence({"day": day, "slot": slot, **item})
    if not links:
        result = {**result, "reinforce_ids": [], "challenge_ids": []}
    stats = memory.apply_learning(store, result, today, note_kinds=memory.VALID_KINDS - {"user"})
    return {"message": message, "learning": stats}
