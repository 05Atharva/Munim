"""Persistence layer: Supabase (production) or a local JSON file (testing / first dry run).

Tables (see supabase_schema.sql): notes, predictions, digests, chat.
Both stores expose exactly the same methods, so the rest of the bot does not care which is used.
"""
import json
import os
import threading
import tempfile
from datetime import datetime, timezone
from typing import Optional

import requests

from .config import Config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    """JSON-file store. Good for tests and trying things on your laptop. NOT for GitHub Actions
    (the runner is wiped after every run) - use Supabase there."""

    TABLES = ("notes", "predictions", "digests", "chat", "processed_updates", "observations", "evidence")

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.data = {t: [] for t in self.TABLES}
        self.data["_next"] = {t: 1 for t in self.TABLES}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update(loaded)
            for table in self.TABLES:
                self.data.setdefault(table, [])
                self.data.setdefault("_next", {}).setdefault(table, max([r.get("id", 0) for r in self.data[table]] + [0]) + 1)

    def _save(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="munim-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _insert(self, table: str, row: dict) -> dict:
        with self._lock:
            return self._insert_locked(table, row)

    def _insert_locked(self, table: str, row: dict) -> dict:
        row = dict(row)
        row["id"] = self.data["_next"][table]
        self.data["_next"][table] += 1
        row.setdefault("created_at", _now())
        self.data[table].append(row)
        self._save()
        return dict(row)

    # notes -----------------------------------------------------------------
    def add_note(self, kind: str, content: str, tags: Optional[list] = None, confidence: float = 0.5) -> dict:
        return self._insert("notes", {
            "kind": kind, "content": content, "tags": tags or [], "confidence": float(confidence),
            "status": "active", "reinforced": 0, "updated_at": _now(),
        })

    def active_notes(self, limit: int = 200) -> list:
        rows = [r for r in self.data["notes"] if r["status"] == "active"]
        rows.sort(key=lambda r: (r["confidence"], r["updated_at"]), reverse=True)
        return [dict(r) for r in rows[:limit]]

    def get_note(self, note_id: int) -> Optional[dict]:
        for r in self.data["notes"]:
            if r["id"] == note_id:
                return dict(r)
        return None

    def update_note(self, note_id: int, **fields) -> None:
        with self._lock:
            for r in self.data["notes"]:
                if r["id"] == note_id:
                    r.update(fields)
                    r["updated_at"] = _now()
            self._save()

    # predictions -----------------------------------------------------------
    def add_prediction(self, claim: str, check_by: str) -> dict:
        return self._insert("predictions", {"claim": claim, "check_by": check_by, "status": "open", "outcome_note": ""})

    def open_predictions(self) -> list:
        return [dict(r) for r in self.data["predictions"] if r["status"] == "open"]

    def resolve_prediction(self, pred_id: int, status: str, note: str = "") -> None:
        with self._lock:
            for r in self.data["predictions"]:
                if r["id"] == pred_id:
                    r["status"] = status
                    r["outcome_note"] = note
            self._save()

    def prediction_scorecard(self) -> dict:
        out = {"right": 0, "wrong": 0, "unclear": 0, "open": 0}
        for r in self.data["predictions"]:
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    # digests ---------------------------------------------------------------
    def digest_status(self, slot: str, day: str):
        return next((r.get("delivery_status", "sent") for r in self.data["digests"]
                     if r["slot"] == slot and r["day"] == day), None)

    def claim_digest(self, slot: str, day: str, content: str, snapshot: dict):
        with self._lock:
            if self.digest_status(slot, day):
                return None
            return self._insert_locked("digests", {"slot": slot, "day": day, "content": content,
                                                   "snapshot": snapshot, "delivery_status": "sending"})["id"]

    def mark_digest_sent(self, digest_id: int):
        with self._lock:
            for row in self.data["digests"]:
                if row["id"] == digest_id:
                    row["delivery_status"] = "sent"
                    break
            self._save()

    def release_digest(self, digest_id: int):
        with self._lock:
            self.data["digests"] = [r for r in self.data["digests"] if r["id"] != digest_id]
            self._save()

    def mark_update_processed(self, update_id: int) -> bool:
        with self._lock:
            if any(r["update_id"] == update_id for r in self.data["processed_updates"]):
                return False
            self._insert_locked("processed_updates", {"update_id": update_id})
            return True

    def add_observation(self, payload: dict) -> dict:
        return self._insert("observations", {"observation": payload})

    def add_evidence(self, payload: dict) -> dict:
        return self._insert("evidence", {"evidence": payload})

    def recent_observations(self, n: int = 5) -> list:
        return [dict(r) for r in self.data["observations"][-n:]][::-1]

    def recent_digests(self, n: int = 4) -> list:
        sent = [r for r in self.data["digests"] if r.get("delivery_status", "sent") == "sent"]
        return [dict(r) for r in sent[-n:]][::-1]

    # chat ------------------------------------------------------------------
    def add_chat(self, role: str, content: str) -> dict:
        return self._insert("chat", {"role": role, "content": content})

    def recent_chat(self, n: int = 10) -> list:
        return [dict(r) for r in self.data["chat"][-n:]]


class SupabaseStore:
    """Talks to Supabase through its REST API with a backend secret or legacy service-role JWT."""

    def __init__(self, url: str, key: str):
        self.base = f"{url}/rest/v1"
        self.h = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_secret_"):
            self.h["Authorization"] = f"Bearer {key}"

    def _req(self, method: str, table: str, *, params=None, body=None, prefer: Optional[str] = None):
        headers = dict(self.h)
        if prefer:
            headers["Prefer"] = prefer
        r = requests.request(method, f"{self.base}/{table}", headers=headers, params=params,
                             data=json.dumps(body) if body is not None else None, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {method} {table} failed (HTTP {r.status_code})")
        return r.json() if r.text else None

    def _insert(self, table: str, row: dict) -> dict:
        res = self._req("POST", table, body=row, prefer="return=representation")
        return res[0]

    # notes
    def add_note(self, kind, content, tags=None, confidence=0.5):
        return self._insert("notes", {"kind": kind, "content": content, "tags": tags or [],
                                      "confidence": float(confidence), "status": "active"})

    def active_notes(self, limit=200):
        return self._req("GET", "notes", params={"status": "eq.active", "order": "confidence.desc,updated_at.desc",
                                                 "limit": str(limit)})

    def get_note(self, note_id):
        res = self._req("GET", "notes", params={"id": f"eq.{note_id}"})
        return res[0] if res else None

    def update_note(self, note_id, **fields):
        fields["updated_at"] = _now()
        self._req("PATCH", "notes", params={"id": f"eq.{note_id}"}, body=fields)

    # predictions
    def add_prediction(self, claim, check_by):
        return self._insert("predictions", {"claim": claim, "check_by": check_by, "status": "open"})

    def open_predictions(self):
        return self._req("GET", "predictions", params={"status": "eq.open", "order": "check_by.asc"})

    def resolve_prediction(self, pred_id, status, note=""):
        self._req("PATCH", "predictions", params={"id": f"eq.{pred_id}"}, body={"status": status, "outcome_note": note})

    def prediction_scorecard(self):
        out = {"right": 0, "wrong": 0, "unclear": 0, "open": 0}
        for r in self._req("GET", "predictions", params={"select": "status"}):
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    # digests
    def digest_status(self, slot, day):
        rows = self._req("GET", "digests", params={"select": "delivery_status", "slot": f"eq.{slot}",
                                                  "day": f"eq.{day}", "limit": "1"})
        return rows[0]["delivery_status"] if rows else None

    def claim_digest(self, slot, day, content, snapshot):
        rows = self._req("POST", "digests", params={"on_conflict": "slot,day"},
                         body={"slot": slot, "day": day, "content": content, "snapshot": snapshot,
                               "delivery_status": "sending"}, prefer="resolution=ignore-duplicates,return=representation")
        return rows[0]["id"] if rows else None

    def mark_digest_sent(self, digest_id):
        self._req("PATCH", "digests", params={"id": f"eq.{digest_id}"}, body={"delivery_status": "sent"})

    def release_digest(self, digest_id):
        self._req("DELETE", "digests", params={"id": f"eq.{digest_id}", "delivery_status": "eq.sending"})

    def mark_update_processed(self, update_id):
        rows = self._req("POST", "processed_updates", params={"on_conflict": "update_id"},
                         body={"update_id": update_id}, prefer="resolution=ignore-duplicates,return=representation")
        return bool(rows)

    def add_observation(self, payload):
        return self._insert("market_observations", {"observation": payload})

    def add_evidence(self, payload):
        return self._insert("evidence", {"evidence": payload})

    def recent_observations(self, n=5):
        return self._req("GET", "market_observations", params={"order": "created_at.desc", "limit": str(n)})

    def recent_digests(self, n=4):
        return self._req("GET", "digests", params={"delivery_status": "eq.sent", "order": "created_at.desc", "limit": str(n)})

    # chat
    def add_chat(self, role, content):
        return self._insert("chat", {"role": role, "content": content})

    def recent_chat(self, n=10):
        rows = self._req("GET", "chat", params={"order": "created_at.desc", "limit": str(n)})
        return rows[::-1]


def make_store(cfg: Config):
    if cfg.has_supabase():
        return SupabaseStore(cfg.supabase_url, cfg.supabase_key)
    if cfg.app_env == "test" or cfg.allow_local_store:
        return LocalStore(cfg.local_store_path)
    raise RuntimeError("Supabase is required outside explicit local/test mode.")
