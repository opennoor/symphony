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

from .model import Event, ProjectState, RunState


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


def _native_lead_turns(
    state: ProjectState, session: str, environ: Mapping[str, str],
) -> tuple[RunState, dict[str, dict], list[str], str, datetime] | None:
    """Read native turns only for this run's pinned lead and root parent."""
    run = state.active_run
    if not run or run.session_id != session or run.provider != "codex":
        return None
    lead_id = run.lead_identity or ""
    started_at = _instant(run.started_at)
    if not _CODEX_ID.fullmatch(lead_id) or started_at is None:
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
                    turn["started_at"] = _instant(record.get("timestamp"))
                    if turn_id not in turn_order:
                        turn_order.append(turn_id)
                    latest_started = turn_id
                elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
                    turn["completed_at"] = _instant(record.get("timestamp"))
                    turn["message"] = payload.get("last_agent_message")
                    turn["outcome"] = _reported_status(turn["message"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None
    return run, turns, turn_order, latest_started, started_at


def codex_recovered_lead_event(
    state: ProjectState, session: str, environ: Mapping[str, str],
) -> Event | None:
    """Return the exact lead's newest unobserved, host-completed recovery turn."""
    run = state.active_run
    if (not run or run.status != "recovering"
            or run.assessment.get("_retryable_lead") != run.lead_identity):
        return None
    observed = _native_lead_turns(state, session, environ)
    if observed is None:
        return None
    run, turns, turn_order, latest_started, started_at = observed
    failed_at = _instant(run.updated_at)
    if failed_at is None or failed_at < started_at:
        return None
    lead_id = run.lead_identity or ""
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    if lead is None:
        return None
    turn = turns.get(latest_started, {})
    completed_at = turn.get("completed_at")
    if (not latest_started or not turn.get("started") or completed_at is None
            or completed_at <= started_at
            or turn.get("model") != lead.requested_tier
            or turn.get("effort") != lead.requested_effort):
        return None
    terminal_turns = run.assessment.get("_terminal_turns", {})
    observed_turns = set(terminal_turns.get(lead_id, ()))
    if f"turn_id:{latest_started}" in observed_turns:
        return None
    latest_index = turn_order.index(latest_started)
    # The reducer pins the exact native turn that caused lead recovery. An
    # unrelated older failure cannot authorize a completion that predates a
    # later rejected callback; a markerless rejected turn is still valid
    # evidence once its terminal callback was durably processed.
    failed_token = run.assessment.get("_retryable_lead_turn")
    if not isinstance(failed_token, str) or not failed_token.startswith("turn_id:"):
        return None
    failed_turn = failed_token.removeprefix("turn_id:")
    if (failed_token not in observed_turns or failed_turn not in turn_order
            or turn_order.index(failed_turn) >= latest_index):
        return None
    failed = turns[failed_turn]
    if (not failed.get("completed_at") or failed["completed_at"] < started_at
            or failed.get("outcome") == "completed"):
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


def codex_completing_lead_turn(
    state: ProjectState, session: str, environ: Mapping[str, str],
) -> tuple[str, Event | None]:
    """Check for a newer native lead turn before root Stop archives success.

    An accepted terminal turn anchors transcript order. A later turn cannot
    inherit that earlier outcome, even if its plugin SubagentStop is delayed.
    """
    run = state.active_run
    if not run or run.status != "completing" or run.provider != "codex":
        return "none", None
    lead_id = run.lead_identity or ""
    anchored = tuple(run.assessment.get("_terminal_turns", {}).get(lead_id, ()))
    if not anchored or not _CODEX_ID.fullmatch(lead_id):
        return "none", None
    observed = _native_lead_turns(state, session, environ)
    if observed is None:
        return "unknown", None
    run, turns, turn_order, latest_started, started_at = observed
    anchors = [token.removeprefix("turn_id:") for token in anchored
               if isinstance(token, str) and token.startswith("turn_id:")]
    if not anchors or not latest_started or not any(token in turn_order for token in anchors):
        return "unknown", None
    if latest_started in anchors:
        return "none", None
    if turn_order.index(latest_started) <= max(turn_order.index(token)
                                               for token in anchors if token in turn_order):
        return "unknown", None
    turn = turns.get(latest_started, {})
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    began = turn.get("started_at")
    if (not turn.get("started") or began is None or began <= started_at
            or lead is None or turn.get("model") != lead.requested_tier
            or turn.get("effort") != lead.requested_effort):
        return "unknown", None
    completed_at = turn.get("completed_at")
    if completed_at is None:
        return "running", None
    if completed_at < began:
        return "unknown", None
    message = turn.get("message")
    if not isinstance(message, str) or len(message) > 100_000:
        return "unknown", None
    outcome = _reported_status(message)
    status = "completed" if outcome == "completed" else "blocked"
    payload = {
        "provider": "codex", "session_id": session, "agent_id": lead_id,
        "parent_thread_id": session, "turn_id": latest_started, "status": status,
        "model": lead.requested_tier, "model_reasoning_effort": lead.requested_effort,
        "last_assistant_message": message,
    }
    event_id = hashlib.sha256(f"codex-host-turn\0{lead_id}\0{latest_started}".encode()).hexdigest()
    return "completed", Event(event_id, "subagent_stopped", completed_at.isoformat(), payload)
