"""Weekly 'sleep': tidy memory so it gets sharper instead of just bigger."""
from . import memory, prompts
from .config import Config


def run_consolidation(cfg: Config, store, llm) -> dict:
    notes = [note for note in store.active_notes(limit=100) if note["kind"] != "user"][:30]
    if len(notes) < 8:
        return {"skipped": "not enough notes yet"}

    listing = "\n".join(f"{n['id']} | {n['kind']} | c={n['confidence']:.2f} | x{n.get('reinforced', 0)} | {n['content']}"
                        for n in notes)
    result = llm.complete_json(prompts.PERSONA.format(name=cfg.bot_name),
                               prompts.CONSOLIDATE_USER.format(notes=listing), temperature=0.2, max_tokens=1800)

    valid = {n["id"] for n in notes}
    stats = {"retired": 0, "merged": 0, "promoted": 0, "new": 0}

    for nid in memory.model_list(result, "retire_ids", 10):
        if type(nid) is int and nid in valid:
            store.update_note(nid, status="retired")
            valid.remove(nid)
            stats["retired"] += 1

    for m in memory.model_list(result, "merge", 10):
        try:
            keep, drops, content = int(m["keep_id"]), {int(d) for d in m["drop_ids"]}, str(m["new_content"]).strip()
            if keep in valid and content and drops and keep not in drops and drops <= valid:
                store.update_note(keep, content=content[:memory.MAX_NOTE_CHARS])
                for d in drops:
                    store.update_note(d, status="retired")
                valid.difference_update(drops)
                stats["merged"] += 1
        except (KeyError, TypeError, ValueError):
            continue

    for p in memory.model_list(result, "promote", 10):
        try:
            nid, kind = int(p["id"]), str(p["new_kind"])
            if nid in valid and kind in ("pattern", "lesson"):
                store.update_note(nid, kind=kind, confidence=min(0.95, max(0.3, float(p.get("new_confidence", 0.7)))))
                stats["promoted"] += 1
        except (KeyError, TypeError, ValueError):
            continue

    stats["new"] = memory.apply_learning(store, result={"new_notes": result.get("new_notes")},
                                         note_kinds={"lesson"})["new"]
    return stats
