"""Twice-daily digest: gather data -> think with memory -> send to Telegram -> save what was learned."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import market_data, memory, prompts, report
from .config import Config
from .llm import LLMError
from .telegram import TelegramRejected, send_message


def run_digest(cfg: Config, store, llm, slot: str, *, quotes=None, headlines=None, opening_quotes=None, send=None) -> dict:
    """Run a morning or evening digest, with injectable market inputs for tests."""
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

    fetch_live = quotes is None
    quotes = market_data.fetch_quotes() if fetch_live else quotes
    if slot == "morning":
        if opening_quotes is None:
            opening_quotes = market_data.fetch_opening_quotes(quotes, now) if fetch_live else {}
    else:
        opening_quotes = None
    headlines = (market_data.fetch_headlines() if headlines is None else headlines)[:18]
    print(f"Market inputs: {len(quotes)} daily quotes, {len(opening_quotes or {})} intraday quotes, {len(headlines)} headlines")
    if not quotes and not headlines and not opening_quotes:
        raise RuntimeError("No market quotes or headlines fetched. Check the feeds and retry.")
    snapshot = market_data.format_snapshot(quotes, headlines, for_digest=True, opening_quotes=opening_quotes)
    title = f"{'Morning market update' if slot == 'morning' else 'Evening wrap'} · {now.strftime('%a %d %b')}"

    system = prompts.PERSONA.format(name=cfg.bot_name)
    user = prompts.DIGEST_USER.format(
        slot_focus=prompts.SLOT_FOCUS[slot],
        today=day, weekday=now.strftime("%A"), slot=slot,
        memory=memory.render_memory_context(store, min(cfg.max_notes_in_context, 12)),
        recent=memory.render_recent_digests(store, 2),
        history=memory.render_recent_observations(store, 3),
        snapshot=snapshot,
    )

    result = llm.complete_json(system, user, temperature=0.25, max_tokens=2200)
    if not isinstance(result.get("market_context"), str) or not isinstance(result.get("possible_drivers"), list):
        raise LLMError("Model report is missing the market context or driver list.")
    _, draft_sources = report.prepare_report(result, headlines)
    try:
        reviewed = report.review_report(llm, result, quotes, draft_sources, opening_quotes=opening_quotes)
    except LLMError:
        print("Report review unavailable. Using observed data only.")
        reviewed, result = {}, {}
    reviewed_headlines = [item if sid in draft_sources else {} for sid, item in enumerate(headlines, 1)]
    explanations, sources = report.prepare_report(reviewed, reviewed_headlines)
    message = report.render_report(title, quotes, reviewed, explanations, sources, opening_quotes=opening_quotes)

    if cfg.dry_run:
        print(message)
        return {"message": message, "learning": {}}

    if sources:
        seen_urls = {item.get("url") for previous in store.recent_digests(6)
                     for item in (previous.get("snapshot") or {}).get("headlines", [])}
        fresh_sources = set(sources) - {sid for sid, item in sources.items() if item["url"] in seen_urls}
    else:
        fresh_sources = set()

    retrieved = datetime.now(ZoneInfo("UTC")).isoformat()
    snapshot_record = {"quotes": quotes, "headlines": headlines, "retrieved_at": retrieved}
    observations = {"day": day, "slot": slot, "quotes": quotes, "retrieved_at": retrieved}
    if opening_quotes is not None:
        snapshot_record["opening_quotes"] = opening_quotes
        observations["opening_quotes"] = opening_quotes
    digest_id = store.claim_digest(slot, day, message, snapshot_record)
    if digest_id is None:
        raise RuntimeError("A concurrent run claimed this digest. Check its result before retrying.")
    try:
        if send is None:
            telegram_message = report.render_report(title, quotes, reviewed, explanations, sources, html=True,
                                                    opening_quotes=opening_quotes)
            send_message(cfg.telegram_token, cfg.telegram_chat_id, telegram_message, parse_mode="HTML")
        else:
            send(message)
    except TelegramRejected:
        store.release_digest(digest_id)
        raise
    store.mark_digest_sent(digest_id)
    store.add_observation(observations)
    for item in headlines:
        store.add_evidence({"day": day, "slot": slot, **item})
    if not fresh_sources:
        result = {**result, "reinforce_ids": [], "challenge_ids": []}
    hypotheses = [
        note for note in memory.model_list(reviewed, "new_notes", 2)
        if isinstance(note, dict) and note.get("kind") == "hypothesis"
        and type(note.get("confidence")) in (int, float) and 0.2 <= note["confidence"] <= 0.5
        and any(type(sid) is int and sid in fresh_sources for sid in memory.model_list(note, "source_ids", 3))
    ]
    stats = memory.apply_learning(store, {**result, "new_notes": hypotheses}, today,
                                  note_kinds=memory.VALID_KINDS - {"user"})
    return {"message": message, "learning": stats}
