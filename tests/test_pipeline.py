"""Smoke tests: run the whole pipeline with a fake LLM and an in-memory store. No network."""
import json
import os
import sys
from datetime import date, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import memory
from bot.chat import handle_message
from bot.config import Config
from bot.consolidate import run_consolidation
from bot.digest import run_digest
from bot.llm import parse_json
from bot.store import LocalStore
from bot.telegram import TelegramRejected, _split
from bot.store import SupabaseStore


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system, user, **kw):
        self.calls.append((system, user, kw))
        return self.responses.pop(0)


def make(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    return cfg, LocalStore(str(tmp_path / "s.json"))


QUOTES = {"Nifty 50": {"last": 25482.45, "prev": 25800.6, "chg_pct": -1.23}}
HEADS = [{"source": "ET", "title": "Banks and metals drag Sensex as rupee slips past 93",
          "summary": "", "url": "https://example.com/market-wrap"}]
MSG = "*Evening wrap*\nNifty fell 1.23% as banks and metals dragged while the rupee weakened. " * 2


def test_digest_saves_learning_and_message(tmp_path):
    cfg, store = make(tmp_path)
    sent = []
    llm = FakeLLM([{
        "message": MSG,
        "new_notes": [{"kind": "pattern", "content": "Rupee weakness often coincides with FII selling in banks.",
                       "tags": ["rupee"], "confidence": 0.5}],
        "predictions": [{"claim": "Smallcap index will underperform Nifty over the next 5 sessions",
                         "check_by": (date.today() + timedelta(days=7)).isoformat()}],
    }])
    out = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert sent and "Nifty fell" in sent[0]
    assert out["learning"]["new"] == 1 and out["learning"]["predictions"] == 1
    assert len(store.recent_digests()) == 1
    # the fresh data must have reached the prompt, and the persona must forbid advice
    system, user, _ = llm.calls[0]
    assert "25,482.45" in user and "rupee slips past 93" in user
    assert "Never issue personalised buy/sell instructions" in system


def test_memory_feeds_back_and_reinforces(tmp_path):
    cfg, store = make(tmp_path)
    n = store.add_note("pattern", "Midcaps fall harder than Nifty on risk-off days.", ["midcap"], 0.5)
    sent = []
    result = {"message": MSG, "reinforce_ids": [n["id"]], "source_ids": [1, 42, "https://example.com/invalid"]}
    llm = FakeLLM([result, result])
    run_digest(cfg, store, llm, "morning", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert f"[#{n['id']} c=0.50]" in llm.calls[0][1]          # memory reached the prompt
    assert store.get_note(n["id"])["confidence"] == 0.53       # capped small reinforcement, not certainty inflation
    assert sent[0].endswith("https://example.com/market-wrap")
    run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert store.get_note(n["id"])["confidence"] == 0.53


def test_challenge_retires_weak_notes(tmp_path):
    _, store = make(tmp_path)
    n = store.add_note("hypothesis", "Smallcaps always rally after RBI meetings.", [], 0.3)
    memory.apply_learning(store, {"challenge_ids": [n["id"]]})
    assert store.get_note(n["id"])["status"] == "retired"


def test_duplicate_notes_are_skipped_without_reinforcing(tmp_path):
    _, store = make(tmp_path)
    store.add_note("fact", "Rupee weakness and stronger crude can raise India's import costs.", [], 0.6)
    stats = memory.apply_learning(store, {"new_notes": [
        {"kind": "fact", "content": "Rupee weakness and higher crude can raise India's import costs.", "confidence": 0.5}]})
    assert stats["new"] == 0 and stats["reinforced"] == 0 and len(store.active_notes()) == 1


def test_prediction_resolution_and_scorecard(tmp_path):
    _, store = make(tmp_path)
    p = store.add_prediction("Nifty closes above 25,000 by Friday", date.today().isoformat())
    # LLM-proposed grading is ignored; only an independent verifier/operator can resolve.
    memory.apply_learning(store, {"resolved": [{"id": p["id"], "result": "right", "note": "closed 25,300"}]})
    assert store.prediction_scorecard()["open"] == 1
    store.resolve_prediction(p["id"], "right", "Independently checked against index close data")
    assert store.prediction_scorecard()["right"] == 1
    assert "1 right" in memory.render_memory_context(store)


def test_bad_model_output_does_not_crash(tmp_path):
    _, store = make(tmp_path)
    stats = memory.apply_learning(store, {
        "new_notes": [{"kind": "nonsense", "content": "x" * 30}, "garbage", {"kind": "fact", "content": "short"}],
        "reinforce_ids": ["abc", None, 9999], "predictions": [{"claim": "ok claim that is long enough", "check_by": "not-a-date"}],
        "resolved": [{"id": "zz"}]})
    assert stats["new"] == 0 and stats["predictions"] == 0


def test_chat_learns_about_user(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([{"reply": "Got it - 5 year horizon noted.",
                    "new_notes": [{"kind": "user", "content": "The user's investing horizon is about 5 years.", "confidence": 0.8}]}])
    reply = handle_message(cfg, store, llm, "I plan to hold for 5 years", quotes={})
    assert "horizon" in reply
    assert any(n["kind"] == "user" for n in store.active_notes())
    assert len(store.recent_chat()) == 2


def test_consolidation_applies_safely(tmp_path):
    cfg, store = make(tmp_path)
    ids = [store.add_note("fact", f"note number {i} about markets and things", [], 0.5)["id"] for i in range(9)]
    llm = FakeLLM([{"retire_ids": [ids[0], 12345], "merge": [{"keep_id": ids[1], "drop_ids": [ids[2]], "new_content": "merged note"}],
                    "promote": [{"id": ids[3], "new_kind": "lesson", "new_confidence": 0.8}], "new_notes": []}])
    stats = run_consolidation(cfg, store, llm)
    assert stats["retired"] == 1 and stats["merged"] == 1 and stats["promoted"] == 1
    assert store.get_note(ids[2])["status"] == "retired" and store.get_note(ids[3])["kind"] == "lesson"


def test_parse_json_tolerates_fences_and_noise():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} hope that helps') == {"a": 2}


def test_long_messages_split_for_telegram():
    parts = _split("para one\n\n" + ("x" * 3000 + "\n\n") * 3)
    assert len(parts) >= 3 and all(len(p) <= 3900 for p in parts)


def test_digest_duplicate_is_skipped(tmp_path):
    cfg, store = make(tmp_path)
    sent = []
    llm = FakeLLM([{"message": MSG}])
    first = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    second = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert first["message"] == MSG.strip()
    assert second["skipped"].startswith("digest already recorded")
    assert len(sent) == 1 and len(llm.calls) == 1


def test_production_requires_supabase(tmp_path):
    cfg = Config()
    cfg.app_env = "production"
    cfg.supabase_url = ""
    cfg.supabase_key = ""
    try:
        cfg.validate_runtime()
        assert False, "expected production storage validation failure"
    except RuntimeError as exc:
        assert "SUPABASE_URL" in str(exc)


def test_market_snapshot_labels_daily_data_and_missing_breadth():
    from bot.market_data import format_snapshot
    rendered = format_snapshot(QUOTES, HEADS)
    assert "today's bar may be provisional" in rendered
    assert "MARKET BREADTH: unavailable" in rendered


def test_recent_observations_are_available_to_memory(tmp_path):
    _, store = make(tmp_path)
    store.add_observation({"day": "2026-11-20", "slot": "evening", "quotes": QUOTES})
    rendered = memory.render_recent_observations(store)
    assert "2026-11-20 evening" in rendered and "Nifty 50" in rendered


def test_telegram_help_commands_do_not_call_llm(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([])
    assert "/predictions" in handle_message(cfg, store, llm, "/help")
    assert handle_message(cfg, store, llm, "/predictions") == "No open predictions."
    assert llm.calls == []


def test_preview_leaves_slot_and_memory_untouched(tmp_path):
    cfg, store = make(tmp_path)
    cfg.dry_run = True
    llm = FakeLLM([{"message": MSG, "new_notes": [{"kind": "lesson", "content": "Test result only."}]}])
    sent = []
    run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert not sent and not store.recent_digests() and not store.active_notes()
    assert not store.data["digests"]


def test_delivery_timeout_requires_reconciliation(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([{"message": MSG}])

    def timeout(_):
        raise RuntimeError("simulated network timeout")

    with pytest.raises(RuntimeError, match="simulated network timeout"):
        run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=timeout)
    with pytest.raises(RuntimeError, match="Delivery unconfirmed"):
        run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=timeout)
    assert not store.recent_digests()


def test_explicit_delivery_rejection_allows_retry(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([{"message": MSG}, {"message": MSG}])

    def reject(_):
        raise TelegramRejected("simulated rejection")

    with pytest.raises(TelegramRejected):
        run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=reject)
    sent = []
    run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert len(sent) == len(store.recent_digests()) == 1


def test_supabase_accepts_new_secret_and_legacy_jwt():
    secret = SupabaseStore("https://example.supabase.co", "sb_secret_example")
    legacy = SupabaseStore("https://example.supabase.co", "legacy.example.jwt")
    assert secret.h["apikey"] == "sb_secret_example" and "Authorization" not in secret.h
    assert legacy.h["Authorization"] == "Bearer legacy.example.jwt"
