"""Recover a Codex lead turn only from native completed-turn evidence.

Some Codex followup turns deliver user SubagentStop hooks but omit a plugin's
SubagentStop command. The root's next hook can reconcile the same recorded
lead without trusting an assistant's prose alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .model import Event, ProjectState


_CODEX_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _reported_status(message: object) -> str | None:
    if not isinstance(message, str):
        return None
    markers = [line.strip().removeprefix("SYMPHONY_OUTCOME:").strip()
               for line in message.splitlines()
               if line.strip().startswith("SYMPHONY_OUTCOME:")]
    if len(markers) != 1:
        return None
    try:
        outcome = json.loads(markers[0])
    except (TypeError, ValueError):
        return None
    status = outcome.get("status") if isinstance(outcome, dict) else None
    return status if isinstance(status, str) and status else None


def codex_recovered_lead_event(
    state: ProjectState, session: str, environ: Mapping[str, str],
) -> Event | None:
    """Return the exact lead's newest unobserved, host-completed native turn."""
    run = state.active_run
    if (not run or run.session_id != session or run.provider != "codex"
            or run.status != "recovering"
            or run.assessment.get("_retryable_lead") != run.lead_identity):
        return None
    lead_id = run.lead_identity or ""
    started_at = _instant(run.started_at)
    failed_at = _instant(run.updated_at)
    if (not _CODEX_ID.fullmatch(lead_id) or started_at is None or failed_at is None
            or failed_at < started_at):
        return None
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    if not lead or not lead.requested_tier or not lead.requested_effort:
        return None
    home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    paths = tuple(sessions.glob(f"*/*/*/*{lead_id}.jsonl"))
    if len(paths) != 1:
        return None
    path = paths[0]
    try:
        if (sessions.is_symlink() or path.is_symlink()
                or any(parent.is_symlink() for parent in path.parents if sessions in parent.parents)
                or not path.is_file() or path.stat().st_size > _MAX_TRANSCRIPT_BYTES):
            return None
        with path.open(encoding="utf-8") as stream:
            meta = json.loads(stream.readline())
            if not isinstance(meta, dict) or meta.get("type") != "session_meta":
                return None
            details = meta.get("payload") or {}
            if not isinstance(details, dict) or details.get("id") != lead_id:
                return None
            spawn = (((details.get("source") or {}).get("subagent") or {})
                     .get("thread_spawn") or {})
            if not isinstance(spawn, dict) or spawn.get("parent_thread_id") != session:
                return None
            turns: dict[str, dict] = {}
            turn_order: list[str] = []
            latest_started = ""
            for line in stream:
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                turn_id = payload.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id:
                    continue
                turn = turns.setdefault(turn_id, {})
                if record.get("type") == "turn_context":
                    turn["model"] = payload.get("model")
                    turn["effort"] = payload.get("effort")
                elif record.get("type") == "event_msg" and payload.get("type") == "task_started":
                    turn["started"] = True
                    if turn_id not in turn_order:
                        turn_order.append(turn_id)
                    latest_started = turn_id
                elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    turn["completed_at"] = _instant(record.get("timestamp"))
                    turn["message"] = payload.get("last_agent_message")
                    turn["outcome"] = _reported_status(turn["message"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    turn = turns.get(latest_started, {})
    completed_at = turn.get("completed_at")
    if (not latest_started or not turn.get("started") or completed_at is None
            or completed_at <= started_at
            or turn.get("model") != lead.requested_tier
            or turn.get("effort") != lead.requested_effort):
        return None
    terminal_turns = run.assessment.get("_terminal_turns", {})
    if f"turn_id:{latest_started}" in terminal_turns.get(lead_id, ()):
        return None
    latest_index = turn_order.index(latest_started)
    if not any((prior := turns[turn_id]).get("completed_at")
               and prior["completed_at"] >= started_at
               and prior.get("outcome") not in {None, "completed"}
               for turn_id in turn_order[:latest_index]):
        return None
    message = turn.get("message")
    if not isinstance(message, str) or len(message) > 100_000:
        return None
    if turn.get("outcome") != "completed":
        return None
    payload = {
        "provider": "codex", "session_id": session, "agent_id": lead_id,
        "turn_id": latest_started, "status": "completed",
        "model": lead.requested_tier, "model_reasoning_effort": lead.requested_effort,
        "last_assistant_message": message,
    }
    event_id = hashlib.sha256(f"codex-host-turn\0{lead_id}\0{latest_started}".encode()).hexdigest()
    return Event(event_id, "subagent_stopped", completed_at.isoformat(), payload)
