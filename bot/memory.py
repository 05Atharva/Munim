"""The 'learning' part: what goes into the model's context, and how its lessons are written back.

Design (simple on purpose, no embeddings needed at this scale):
  * notes        - things Munim believes. kind = fact | pattern | lesson | hypothesis | user
                   each has a confidence (0-1). Seeing evidence again reinforces it; a contradiction
                   lowers it; below 0.2 it is retired. Over months the high-confidence notes become
                   its "experience".
  * predictions  - falsifiable claims with a check date. An independent verifier can update their
                   outcomes for the scorecard.
  * digests      - what it already told you (so it does not repeat itself and can refer back).
"""
import re
from datetime import date, timedelta
from typing import Optional

VALID_KINDS = {"fact", "pattern", "lesson", "hypothesis", "user"}
MAX_NOTE_CHARS = 420


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- reading
def render_memory_context(store, max_notes: int = 28, query: str = "") -> str:
    """Compact text block with what Munim currently knows. Keep it short: Groq free tier has tight limits."""
    notes = store.active_notes(limit=200)

    if query:  # chat: pull in notes related to the question, even if lower confidence
        q = _tokens(query)
        notes.sort(key=lambda n: (len(q & _tokens(n["content"] + " " + " ".join(n.get("tags") or []))),
                                  n["confidence"]), reverse=True)
    notes = notes[:max_notes]

    by_kind = {}
    for n in notes:
        by_kind.setdefault(n["kind"], []).append(n)

    order = [("user", "User preferences"), ("lesson", "Lessons I have learned"), ("pattern", "Patterns I have noticed"),
             ("fact", "Standing facts / context"), ("hypothesis", "Hypotheses I am testing (unproven)")]
    lines = []
    for kind, title in order:
        if kind not in by_kind:
            continue
        lines.append(f"## {title}")
        for n in by_kind[kind]:
            lines.append(f"[#{n['id']} c={n['confidence']:.2f}] {n['content']}")

    preds = store.open_predictions()
    if preds:
        lines.append("## Open predictions awaiting independent verification")
        for p in preds[:12]:
            lines.append(f"[P#{p['id']} check_by={p['check_by']}] {p['claim']}")

    sc = store.prediction_scorecard()
    judged = sc.get("right", 0) + sc.get("wrong", 0)
    if judged:
        lines.append(f"## My track record: {sc['right']} right, {sc['wrong']} wrong, {sc.get('unclear', 0)} unclear "
                     f"({100 * sc['right'] // judged}% hit rate). Stay humble accordingly.")

    return "\n".join(lines) if lines else "(Memory is empty - I am just starting out. Be curious, and say so.)"


def render_recent_digests(store, n: int = 3, max_chars: int = 700) -> str:
    rows = store.recent_digests(n)
    if not rows:
        return "(no previous digests)"
    out = []
    for r in rows:
        text = r["content"].replace("\n", " ")
        out.append(f"- {r['day']} {r['slot']}: {text[:max_chars]}")
    return "\n".join(out)


def render_recent_observations(store, n: int = 5) -> str:
    """Compact historical context from prior stored research snapshots."""
    if not hasattr(store, "recent_observations"):
        return "(historical observations unavailable)"
    rows = store.recent_observations(n)
    if not rows:
        return "(no prior structured observations yet)"
    lines = []
    for row in rows:
        payload = row.get("observation", row)
        day, slot = payload.get("day", "unknown date"), payload.get("slot", "unknown slot")
        quotes = payload.get("quotes") or {}
        selected = []
        for key in ("Nifty 50", "Nifty Bank", "Nifty Midcap 150", "Nifty Smallcap 250",
                    "Nifty Midcap 100", "Nifty Smallcap 100", "S&P 500", "USD/INR", "Brent crude"):
            if key in quotes:
                quote = quotes[key]
                selected.append(f"{key} {quote.get('chg_pct', 'n/a')}% [{quote.get('quote_time', 'unknown')[:10]}]")
        lines.append(f"- {day} {slot}: " + ("; ".join(selected) if selected else "no key quotes"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- writing
def model_list(result: dict, key: str, limit: int) -> list:
    value = result.get(key)
    return value[:limit] if isinstance(value, list) else []


def apply_learning(store, result: dict, today: Optional[date] = None, *, note_kinds=VALID_KINDS) -> dict:
    """Apply the structured learning output from the model. Everything is validated; a bad entry is skipped,
    never fatal."""
    today = today or date.today()
    stats = {"new": 0, "reinforced": 0, "challenged": 0, "retired": 0, "predictions": 0, "resolved": 0}

    existing = store.active_notes(limit=300)
    touched_ids = set()

    # 1. new notes (merge into an existing one if it is basically the same idea)
    for item in model_list(result, "new_notes", 4):
        try:
            kind = str(item.get("kind", "fact")).lower()
            content = str(item.get("content", "")).strip()[:MAX_NOTE_CHARS]
            if kind not in note_kinds or len(content) < 12:
                continue
            tags = [t.lower()[:24] for t in model_list(item, "tags", 5) if isinstance(t, str)]
            conf = min(0.8, max(0.2, float(item.get("confidence", 0.5))))
            if not any(n["kind"] == kind and _similar(n["content"], content) >= 0.6 for n in existing):
                created = store.add_note(kind, content, tags, conf)
                existing.append(created)
                touched_ids.add(created["id"])
                stats["new"] += 1
        except (TypeError, ValueError, AttributeError):
            continue

    # 2. evidence for / against existing notes
    for nid in model_list(result, "challenge_ids", 8):
        n = _find(existing, nid)
        if n and n["kind"] in note_kinds and n["id"] not in touched_ids:
            touched_ids.add(n["id"])
            new_conf = round(max(0.0, n["confidence"] - 0.2), 2)
            fields = {"confidence": new_conf}
            if new_conf < 0.2:
                fields["status"] = "retired"
                stats["retired"] += 1
            store.update_note(n["id"], **fields)
            n["confidence"] = new_conf
            stats["challenged"] += 1
    for nid in model_list(result, "reinforce_ids", 8):
        n = _find(existing, nid)
        if n and n["kind"] in note_kinds and n["id"] not in touched_ids:
            _reinforce(store, n, stats)
            touched_ids.add(n["id"])

    # 3. new falsifiable predictions
    predictions = model_list(result, "predictions", 2)
    existing_predictions = {p["claim"].casefold() for p in store.open_predictions()} if predictions else set()
    for p in predictions:
        try:
            claim = p.get("claim", "").strip()[:300]
            if len(claim) < 12 or claim.casefold() in existing_predictions:
                continue
            check_by = date.fromisoformat(p.get("check_by", ""))
            if not today <= check_by <= today + timedelta(days=21):
                continue
            store.add_prediction(claim, check_by.isoformat())
            existing_predictions.add(claim.casefold())
            stats["predictions"] += 1
        except (TypeError, ValueError, AttributeError):
            continue

    # Prediction outcomes are never accepted from the LLM. They must be verified independently by code or a human.

    return stats


def _find(notes: list, nid) -> Optional[dict]:
    try:
        nid = int(nid)
    except (TypeError, ValueError, AttributeError):
        return None
    return next((n for n in notes if n["id"] == nid), None)


def _reinforce(store, note: dict, stats: dict) -> None:
    new_conf = round(min(0.90, note["confidence"] + 0.03), 2)
    store.update_note(note["id"], confidence=new_conf, reinforced=int(note.get("reinforced", 0)) + 1)
    stats["reinforced"] += 1
