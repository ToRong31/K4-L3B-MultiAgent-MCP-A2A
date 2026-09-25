"""Durable, case-scoped agent history; compacting never deletes raw events."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

Summarizer = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


def estimate_tokens(value: Any) -> int:
    # Conservative fallback; replace with the selected model's tokenizer if available.
    return (len(json.dumps(value, ensure_ascii=False)) + 2) // 3


class AgentMemory:
    def __init__(self, path: Path, *, run_id: str | None = None) -> None:
        self.run_id = run_id or os.getenv("L3B_RUN_ID") or uuid4().hex
        if not self.run_id.strip():
            raise ValueError("run_id must be nonempty")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events_v2 (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                case_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_v2_scope ON events_v2(run_id,case_id,agent_id,id);
            CREATE TABLE IF NOT EXISTS checkpoints_v2 (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                agent_id TEXT NOT NULL, through_id INTEGER NOT NULL,
                summary TEXT NOT NULL, evidence_refs TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS evidence_records (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                tool_name TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id, evidence_ref)
            );
            CREATE TABLE IF NOT EXISTS evidence_cache (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id, tool_name, arguments)
            );
            """
        )

    def close(self) -> None:
        self._db.close()

    def append(self, case_id: str, agent_id: str, turn_id: str, kind: str, payload: Any) -> None:
        self._db.execute(
            "INSERT INTO events_v2(run_id,case_id,agent_id,turn_id,kind,payload) "
            "VALUES(?,?,?,?,?,?)",
            (
                self.run_id,
                case_id,
                agent_id,
                turn_id,
                kind,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._db.commit()

    def store_evidence(self, case_id: str, tool_name: str, evidence: dict[str, Any]) -> None:
        ref = evidence["evidence_ref"]
        serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        row = self._db.execute(
            "SELECT tool_name,payload FROM evidence_records "
            "WHERE run_id=? AND case_id=? AND evidence_ref=?",
            (self.run_id, case_id, ref),
        ).fetchone()
        if row and row != (tool_name, serialized):
            raise ValueError("evidence_ref collision within case/run")
        self._db.execute(
            "INSERT OR IGNORE INTO evidence_records VALUES(?,?,?,?,?)",
            (self.run_id, case_id, ref, tool_name, serialized),
        )
        self._db.commit()

    def get_evidence(self, case_id: str, evidence_ref: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT payload FROM evidence_records WHERE run_id=? AND case_id=? AND evidence_ref=?",
            (self.run_id, case_id, evidence_ref),
        ).fetchone()
        if not row:
            raise KeyError(f"evidence not found in current case/run: {evidence_ref}")
        return json.loads(row[0])

    def cache_evidence(
        self, case_id: str, tool_name: str, arguments: str, evidence: dict[str, Any]
    ) -> None:
        self.store_evidence(case_id, tool_name, evidence)
        self._db.execute(
            "INSERT OR REPLACE INTO evidence_cache VALUES(?,?,?,?,?)",
            (self.run_id, case_id, tool_name, arguments, evidence["evidence_ref"]),
        )
        self._db.commit()

    def cached_evidence(
        self, case_id: str, tool_name: str, arguments: str
    ) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT evidence_ref FROM evidence_cache "
            "WHERE run_id=? AND case_id=? AND tool_name=? AND arguments=?",
            (self.run_id, case_id, tool_name, arguments),
        ).fetchone()
        return self.get_evidence(case_id, row[0]) if row else None

    def history(self, case_id: str, agent_id: str) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT through_id,summary,evidence_refs FROM checkpoints_v2 "
            "WHERE run_id=? AND case_id=? AND agent_id=?",
            (self.run_id, case_id, agent_id),
        ).fetchone()
        through_id, summary, refs = row if row else (0, "", "[]")
        rows = self._db.execute(
            "SELECT id,turn_id,kind,payload FROM events_v2 "
            "WHERE run_id=? AND case_id=? AND agent_id=? AND id>? ORDER BY id",
            (self.run_id, case_id, agent_id, through_id),
        ).fetchall()
        return {
            "summary": summary,
            "evidence_refs": json.loads(refs),
            "events": [
                {"id": item_id, "turn_id": turn_id, "kind": kind, "payload": json.loads(payload)}
                for item_id, turn_id, kind, payload in rows
            ],
        }

    def raw_history(self, case_id: str, agent_id: str) -> list[dict[str, Any]]:
        """Read the append-only audit history, including events behind a checkpoint."""
        rows = self._db.execute(
            "SELECT id,turn_id,kind,payload FROM events_v2 "
            "WHERE run_id=? AND case_id=? AND agent_id=? ORDER BY id",
            (self.run_id, case_id, agent_id),
        ).fetchall()
        return [
            {"id": item_id, "turn_id": turn_id, "kind": kind, "payload": json.loads(payload)}
            for item_id, turn_id, kind, payload in rows
        ]

    async def compact_if_needed(
        self,
        case_id: str,
        agent_id: str,
        *,
        context_length: int,
        reserved_output_tokens: int,
        fixed_prompt_tokens: int,
        summarize: Summarizer,
        keep_recent_turns: int = 2,
    ) -> bool:
        available = context_length - reserved_output_tokens
        if available <= 0:
            raise ValueError("output reserve exceeds model context")
        history = self.history(case_id, agent_id)
        if fixed_prompt_tokens + estimate_tokens(history) <= int(available * 0.8):
            return False

        events = history["events"]
        turns = list(dict.fromkeys(event["turn_id"] for event in events))
        if len(turns) <= keep_recent_turns:
            raise ValueError("context over 80% but no complete old turn can be compacted")
        retained_turns = set(turns[-keep_recent_turns:])
        old = [event for event in events if event["turn_id"] not in retained_turns]
        new_summary = await summarize(history["summary"], old)
        refs = set(history["evidence_refs"])
        for event in old:
            refs.update(_evidence_refs(event["payload"]))
        self._db.execute(
            "INSERT INTO checkpoints_v2(run_id,case_id,agent_id,through_id,summary,evidence_refs) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,case_id,agent_id) DO UPDATE SET "
            "through_id=excluded.through_id,summary=excluded.summary,"
            "evidence_refs=excluded.evidence_refs",
            (self.run_id, case_id, agent_id, old[-1]["id"], new_summary, json.dumps(sorted(refs))),
        )
        self._db.commit()
        size = fixed_prompt_tokens + estimate_tokens(self.history(case_id, agent_id))
        if size > int(available * 0.8):
            raise ValueError("context still over 80% after compact; reduce selected evidence")
        return True


def _evidence_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        refs = set()
        for key, child in value.items():
            if key == "evidence_ref" and isinstance(child, str):
                refs.add(child)
            elif key == "evidence_refs" and isinstance(child, list):
                refs.update(ref for ref in child if isinstance(ref, str))
            else:
                refs.update(_evidence_refs(child))
        return refs
    if isinstance(value, list):
        return set().union(*(_evidence_refs(child) for child in value)) if value else set()
    return set()
