"""Smoke tests: run the whole pipeline with a fake LLM and an in-memory store. No network."""
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import memory
from bot.chat import handle_message
from bot.config import Config
from bot.consolidate import run_consolidation
from bot.digest import run_digest
from bot.llm import LLMError, parse_json
from bot.store import LocalStore
from bot.telegram import TelegramRejected, _split, private_chat_ids
from bot.store import SupabaseStore


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, system, user, **kw):
        if user.startswith("Review this draft market briefing"):
            return json.loads(user.split("\nDraft:\n", 1)[1].split("\n\nEvidence:\n", 1)[0])
        self.calls.append((system, user, kw))
        return self.responses.pop(0)


def make(tmp_path):
    cfg = Config()
    cfg.dry_run = False
    return cfg, LocalStore(str(tmp_path / "s.json"))


QUOTES = {"Nifty 50": {"last": 25482.45, "prev": 25800.6, "chg_pct": -1.23}}
HEADS = [{"source": "ET", "title": "Banks and metals drag Sensex as rupee slips past 93",
          "summary": "", "url": "https://example.com/market-wrap"}]


def model_report(**updates):
    return {
        "market_context": "Banks and metals underperformed.",
        "possible_drivers": [{"interpretation": "The report links a weaker rupee to dollar demand.", "source_ids": [1]}],
        "watch": "Watch whether bank stocks continue to lag.",
        **updates,
    }


def test_digest_saves_learning_and_message(tmp_path):
    cfg, store = make(tmp_path)
    sent = []
    llm = FakeLLM([model_report(
        new_notes=[
            {"kind": "hypothesis", "content": "Rupee weakness may coincide with selling in banks.",
             "tags": ["rupee"], "confidence": 0.5, "source_ids": [1]},
            {"kind": "pattern", "content": "Banks always fall when the rupee weakens.", "source_ids": [1]},
        ],
        predictions=[{"claim": "Smallcap index will underperform Nifty over the next 5 sessions",
                      "check_by": (date.today() + timedelta(days=7)).isoformat()}],
    )])
    out = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert sent and "Nifty 50 25,482.45 (-1.23%)" in sent[0]
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
    result = model_report(reinforce_ids=[n["id"]], possible_drivers=[
        {"interpretation": "Bank stocks weakened after FX volatility.", "source_ids": [1, 42, "invalid"]},
    ])
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
    llm = FakeLLM([model_report()])
    first = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    second = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert first["message"] == sent[0]
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


def test_market_snapshot_distinguishes_us_intraday_and_fx_direction():
    from bot.market_data import format_snapshot

    observation = {"quote_time": "2026-11-23 00:00:00", "retrieved_at": "2026-11-23T16:05:00+00:00"}
    quotes = {
        "Nifty 50": {**observation, "last": 25349.65, "chg_pct": -1.75},
        "S&P 500": {**observation, "last": 7245.34, "chg_pct": 4.85},
        "USD/INR": {**observation, "last": 92.9, "chg_pct": -0.42},
        "US 10Y yield": {**observation, "last": 4.95, "chg_pct": 0.69, "change_bps": 3.4},
    }
    rendered = format_snapshot(quotes, [])
    assert "Nifty 50: 25,349.65 (-1.75%); session=2026-11-23; after regular session" in rendered
    assert "S&P 500: 7,245.34 (+4.85%); session=2026-11-23; provisional intraday bar" in rendered
    assert "USD/INR lower, implying a stronger rupee" in rendered
    assert "US 10Y yield: 4.95% (+3.4 bps)" in rendered
    digest_input = format_snapshot(quotes, [], for_digest=True)
    assert "+4.85%" not in digest_input
    assert "₹92.90" not in digest_input
    assert "25,349.65" in digest_input


def test_opening_quote_excludes_unfinished_bars_and_uses_previous_close():
    import pandas as pd
    from bot.market_data import _opening_quote

    now = datetime(2026, 11, 23, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
    timestamps = pd.to_datetime([
        "2026-11-20T10:10:00+05:30", "2026-11-23T09:15:00+05:30",
        "2026-11-23T10:10:00+05:30", "2026-11-23T10:15:00+05:30",
    ], utc=True)
    bars = pd.DataFrame({"Open": [100, 103, 105, 106], "Close": [101, 104, 106, 110]}, index=timestamps)
    daily = {"last": 112, "prev": 100, "quote_time": "2026-11-23", "previous_quote_time": "2026-11-20"}

    quote = _opening_quote(bars, daily, now)
    assert quote["as_of"] == "2026-11-23T10:15:00+05:30"
    assert quote["last"] == 106
    assert quote["chg_pct"] == 6
    assert quote["previous_session"] == "2026-11-20"
    assert quote["since_open_pct"] == 2.91
    assert _opening_quote(bars, daily, now + timedelta(days=1)) is None
    assert _opening_quote(bars, daily, now + timedelta(minutes=36)) is None


def test_morning_digest_renders_and_saves_timestamped_intraday_moves(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([model_report()])
    opening = {"Nifty 50": {"last": 25523.15, "chg_pct": 0.27, "as_of": "2026-11-23T10:10:00+05:30",
                            "since_open_pct": -0.08}}
    sent = []
    run_digest(cfg, store, llm, "morning", quotes=QUOTES, headlines=HEADS, opening_quotes=opening, send=sent.append)
    assert "25,523.15 (+0.27% vs previous close) · 10:10 IST" in sent[0]
    assert "Quotes may be delayed" in sent[0]
    assert "-0.08% since open" in llm.calls[0][1]
    assert store.recent_digests()[0]["snapshot"]["opening_quotes"] == opening
    assert store.recent_observations()[0]["observation"]["opening_quotes"] == opening


def test_morning_digest_falls_back_to_completed_close_without_intraday_quotes(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([model_report()])
    quotes = {
        "Nifty 50": {"last": 25500, "chg_pct": -0.3, "quote_time": "2026-11-23",
                     "previous_quote_time": "2026-11-20", "prev": 25700, "retrieved_at": "2026-11-23T04:45:00+00:00"},
    }
    sent = []
    run_digest(cfg, store, llm, "morning", quotes=quotes, headlines=HEADS, send=sent.append)
    assert "Current Indian intraday quotes were unavailable" in sent[0]
    assert "25,700.00" in sent[0] and "2026-11-20 prior close" in sent[0]
    assert "25,500.00" not in sent[0]


def test_digest_sends_escaped_html_with_verified_links(tmp_path, monkeypatch):
    cfg, store = make(tmp_path)
    sent = {}
    llm = FakeLLM([model_report(market_context="Banks & metals < weaker than IT.")])

    def send_message(token, chat_id, text, *, parse_mode):
        sent.update(text=text, parse_mode=parse_mode)

    monkeypatch.setattr("bot.digest.send_message", send_message)
    out = run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS)
    assert sent["parse_mode"] == "HTML"
    assert "<b>📊 Market moves</b>" in sent["text"]
    assert "Banks &amp; metals &lt; weaker than IT." in sent["text"]
    assert '<a href="https://example.com/market-wrap">[1] ET</a>' in sent["text"]
    assert "Banks & metals < weaker than IT." in out["message"]
    assert "<a href=" not in store.recent_digests()[0]["content"]


def test_digest_review_failure_sends_observations_without_unreviewed_analysis(tmp_path, monkeypatch):
    cfg, store = make(tmp_path)
    llm = FakeLLM([model_report()])
    sent = []

    def unavailable(*args, **kwargs):
        raise LLMError("review call failed")

    monkeypatch.setattr("bot.report.review_report", unavailable)
    run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert "Nifty 50 25,482.45" in sent[0]
    assert "Banks and metals underperformed." not in sent[0]
    assert "The briefing has no source-backed explanation" in sent[0]


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
    llm = FakeLLM([model_report(new_notes=[{"kind": "hypothesis", "content": "Test result only.", "source_ids": [1]}])])
    sent = []
    run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=sent.append)
    assert not sent and not store.recent_digests() and not store.active_notes()
    assert not store.data["digests"]


def test_delivery_timeout_requires_reconciliation(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([model_report()])

    def timeout(_):
        raise RuntimeError("simulated network timeout")

    with pytest.raises(RuntimeError, match="simulated network timeout"):
        run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=timeout)
    with pytest.raises(RuntimeError, match="Delivery unconfirmed"):
        run_digest(cfg, store, llm, "evening", quotes=QUOTES, headlines=HEADS, send=timeout)
    assert not store.recent_digests()


def test_explicit_delivery_rejection_allows_retry(tmp_path):
    cfg, store = make(tmp_path)
    llm = FakeLLM([model_report(), model_report()])

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


def test_chat_id_identifies_bot_and_filters_out_group_messages(monkeypatch, capsys):
    from types import SimpleNamespace

    calls = []
    results = {
        "getMe": {"username": "ExampleMunimBot"},
        "getUpdates": [
            {"message": {"chat": {"id": -11, "type": "group"}}},
            {"message": {"chat": {"id": 42, "type": "private"}}},
        ],
    }

    def request(token, method, payload, timeout=30):
        calls.append((method, payload, timeout))
        return SimpleNamespace(status_code=200, json=lambda: {"result": results[method]})

    monkeypatch.setattr("bot.telegram._api_request", request)
    assert private_chat_ids("test-token") == [42]
    assert calls == [("getMe", {}, 30), ("getUpdates", {"allowed_updates": ["message"], "timeout": 45}, 55)]
    assert "https://t.me/ExampleMunimBot" in capsys.readouterr().out


def test_chat_id_explains_existing_webhook(monkeypatch):
    from types import SimpleNamespace

    results = {
        "getMe": (200, {"username": "ExampleMunimBot"}),
        "getUpdates": (409, {}),
        "getWebhookInfo": (200, {"url": "https://example.com/telegram"}),
    }

    def request(token, method, payload, timeout=30):
        status, result = results[method]
        return SimpleNamespace(status_code=status, json=lambda: {"result": result})

    monkeypatch.setattr("bot.telegram._api_request", request)
    with pytest.raises(RuntimeError, match="webhook is already registered"):
        private_chat_ids("test-token")


def test_chat_id_prompts_for_token_in_interactive_shell(monkeypatch, capsys):
    from types import SimpleNamespace
    from bot import run

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(run.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(run, "getpass", lambda prompt: " test-token ")

    def fetch_ids(token):
        assert token == "test-token"
        return [42]

    monkeypatch.setattr(run, "private_chat_ids", fetch_ids)
    assert run.main(["chat-id"]) == 0
    assert capsys.readouterr().out == "TELEGRAM_CHAT_ID=42\n"
