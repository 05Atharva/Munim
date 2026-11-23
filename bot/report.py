"""Render a short Telegram briefing from computed market moves and model analysis."""
import json
from html import escape
from textwrap import shorten

from . import market_data, memory, prompts
from .llm import LLMError


def _short(value, limit):
    return shorten(value, width=limit, placeholder="…") if isinstance(value, str) else ""


def prepare_report(result: dict, headlines: list) -> tuple[list, dict]:
    """Keep explanations with linkable evidence and choose at most three unique sources."""
    explanations, sources = [], {}
    for item in memory.model_list(result, "possible_drivers", 2):
        if not isinstance(item, dict):
            continue
        claim = _short(item.get("interpretation"), 350)
        if not claim:
            continue
        ids = memory.model_list(item, "source_ids", 3)
        linked = market_data.cited_articles(headlines, ids)
        references = []
        for source_id, article in linked.items():
            if source_id not in sources and len(sources) < 3:
                sources[source_id] = article
            if source_id in sources:
                references.append(f"[{source_id}]")
        if references:
            explanations.append(f"• {claim} {' '.join(references)}")
    return explanations, sources


def review_report(llm, result: dict, quotes: dict, sources: dict) -> dict:
    fields = ("market_context", "possible_drivers", "counterpoint", "watch", "new_notes")
    draft = {key: result.get(key) for key in fields}
    evidence = market_data.format_snapshot(quotes, [], for_digest=True)
    for source_id, article in sources.items():
        evidence += f"\n[{source_id}] {article['source']} · {article.get('published_at', 'unknown')}: {article['title']}. {article.get('summary', '')}"
    reviewed = llm.complete_json(
        "You are reviewing a market briefing against evidence, not writing a new market story.",
        prompts.VERIFY_REPORT.format(draft=json.dumps(draft, ensure_ascii=False), evidence=evidence),
        temperature=0.1, max_tokens=1100,
    )
    if not isinstance(reviewed.get("market_context"), str) or not isinstance(reviewed.get("possible_drivers"), list):
        raise LLMError("Report review returned invalid fields.")
    return {field: reviewed.get(field) for field in fields}


def render_report(title: str, quotes: dict, result: dict, explanations: list, sources: dict, *, html=False) -> str:
    def text(value):
        return escape(value) if html else value

    def heading(label):
        return f"<b>{text(label)}</b>" if html else label

    moves = []
    sessions = []
    for name in ("Nifty 50", "Sensex"):
        quote = quotes.get(name)
        if quote:
            day = quote.get("quote_time", "unknown")[:10]
            status = market_data.bar_status(name, quote)
            description = "close" if status == "after regular session; provider close" else status
            moves.append(f"• {name} {market_data.quote_move(name, quote)}")
            sessions.append(f"{day} {description}")
    if moves:
        if len(set(sessions)) == 1:
            moves.append(f"{sessions[0]} · Yahoo Finance")
        else:
            moves = [f"{move} · {session}" for move, session in zip(moves, sessions)]
            moves.append("Levels from Yahoo Finance.")
    context = _short(result.get("market_context"), 340)
    if context:
        moves.append(context)
    if not moves:
        moves.append("Indian benchmark levels were unavailable.")
    drivers = list(explanations) or ["The briefing has no source-backed explanation for this session."]
    counterpoint = _short(result.get("counterpoint"), 260)
    if counterpoint:
        drivers.append(f"Counterpoint — {counterpoint}")

    sections = [
        heading(title),
        heading("📊 Market moves") + "\n" + text("\n".join(moves)),
        heading("🔎 What the evidence suggests") + "\n" + text("\n".join(drivers)),
    ]
    watch = _short(result.get("watch"), 260)
    if not watch and {"Nifty Bank", "Nifty 50"} <= quotes.keys():
        watch = "Compare Nifty Bank with the Nifty 50 after the next Indian session closes."
    if watch:
        sections.append(heading("👀 What to watch") + "\n" + text(watch))

    if sources:
        links = []
        for source_id, article in sources.items():
            label = f"[{source_id}] {_short(article['source'], 100)}"
            url = article["url"]
            links.append(f'<a href="{escape(url, quote=True)}">{text(label)}</a>' if html else f"{label} — {url}")
        sections.append(heading("Sources") + "\n" + (" · " if html else "\n").join(links))
    return "\n\n".join(sections)
