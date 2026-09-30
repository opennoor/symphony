"""Recover a lead turn only from native completed-turn evidence.

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

from .model import Delegation, Event, ProjectState, RunState


_CODEX_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_CLAUDE_ID = re.compile(r"[0-9a-f]{16,32}")
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


def _native_jsonl(path: Path) -> list[dict] | None:
    """Read one complete, bounded native transcript without following aliases."""
    try:
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size > _MAX_TRANSCRIPT_BYTES):
            return None
        with path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        return rows if rows and all(isinstance(row, dict) for row in rows) else None
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def _claude_native_lead_event(
    state: ProjectState, session: str, project: Path, environ: Mapping[str, str],
    *, require_missing: bool, target_prompt: str | None = None,
) -> Event | None:
    """Validate one lead terminal against Claude's native parent and child turns.

    A removed 1.5.1 plugin cache can strand its old SubagentStop command while
    the native agent and its transcript finish. The active run, exact parent
    Agent launch, child identity, pinned route, and latest native turn must all
    agree before the ordinary lifecycle reducer sees a terminal event.
    """
    run = state.active_run
    if (not run or run.provider != "claude" or run.session_id != session
            or (require_missing and (run.status != "active" or run.outcome is not None))):
        return None
    lead_id = run.lead_identity or ""
    started_at = _instant(run.started_at)
    if not _CLAUDE_ID.fullmatch(lead_id) or started_at is None:
        return None
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    if (lead is None or (require_missing and lead.state.lower() not in {"working", "pending"})
            or not lead.requested_tier or not lead.requested_effort
            or (require_missing and run.assessment.get("_terminal_turns", {}).get(lead_id))):
        return None
    home = Path(environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = home / "projects"
    if projects.is_symlink() or not projects.is_dir():
        return None
    paths = tuple(projects.glob(f"*/{session}/subagents/agent-{lead_id}.jsonl"))
    if len(paths) != 1:
        return None
    child_path = paths[0]
    root_dir = child_path.parent.parent
    if (root_dir.name != session or root_dir.is_symlink()
            or child_path.parent.is_symlink() or root_dir.parent.is_symlink()):
        return None
    meta_path = child_path.with_suffix(".meta.json")
    try:
        if (meta_path.is_symlink() or not meta_path.is_file()
                or meta_path.stat().st_size > 64 * 1024):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(meta, dict) or meta.get("spawnDepth") != 1:
        return None
    agent_type = meta.get("agentType")
    launch_id = meta.get("toolUseId")
    if (not isinstance(agent_type, str)
            or not agent_type.endswith(
                f"symphony-lead-{lead.requested_tier}-{lead.requested_effort}")
            or not isinstance(launch_id, str) or not launch_id):
        return None
    parent_rows = _native_jsonl(root_dir.with_suffix(".jsonl"))
    child_rows = _native_jsonl(child_path)
    if parent_rows is None or child_rows is None:
        return None
    launches = []
    for row in parent_rows:
        if row.get("type") != "assistant" or row.get("sessionId") != session:
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else ():
            if isinstance(item, dict) and item.get("id") == launch_id:
                launches.append((row, item))
    if len(launches) != 1:
        return None
    parent, launch = launches[0]
    launch_input = launch.get("input")
    if (launch.get("type") != "tool_use" or launch.get("name") != "Agent"
            or not isinstance(launch_input, dict)
            or launch_input.get("subagent_type") != agent_type):
        return None
    try:
        if Path(str(parent.get("cwd") or "")).resolve() != project.resolve():
            return None
    except (OSError, ValueError):
        return None
    launched_at = _instant(parent.get("timestamp"))
    if launched_at is None or launched_at < started_at:
        return None
    # A tool-result user row belongs to the current turn. A new textual user
    # prompt starts a new turn and invalidates an older completed report.
    prompt_indices = []
    for index, row in enumerate(child_rows):
        if (row.get("sessionId") != session or row.get("agentId") != lead_id
                or row.get("isSidechain") is not True):
            return None
        if row.get("type") == "user" and isinstance((row.get("message") or {}).get("content"), str):
            prompt_indices.append(index)
    if not prompt_indices:
        return None
    if target_prompt is None:
        prompt_index = len(prompt_indices) - 1
    else:
        matches = [index for index, position in enumerate(prompt_indices)
                   if child_rows[position].get("uuid") == target_prompt]
        if len(matches) != 1:
            return None
        prompt_index = matches[0]
    last_prompt = prompt_indices[prompt_index]
    prompt_id = child_rows[last_prompt].get("uuid")
    if not isinstance(prompt_id, str) or not prompt_id:
        return None
    prompt_at = _instant(child_rows[last_prompt].get("timestamp"))
    if prompt_at is None or prompt_at < launched_at:
        return None
    end = (prompt_indices[prompt_index + 1] if prompt_index + 1 < len(prompt_indices)
           else len(child_rows))
    turn = child_rows[last_prompt + 1:end]
    assistants = [row for row in turn if row.get("type") == "assistant"]
    activity = [row for row in turn if row.get("type") in {"assistant", "user"}]
    if not assistants or not activity or activity[-1] is not assistants[-1]:
        return None
    terminal = assistants[-1]
    message = terminal.get("message")
    if (not isinstance(message, dict) or message.get("stop_reason") != "end_turn"
            or any((row.get("message") or {}).get("stop_reason") == "end_turn"
                   for row in assistants[:-1])):
        return None
    completed_at = _instant(terminal.get("timestamp"))
    terminal_id = terminal.get("uuid")
    if (completed_at is None or completed_at <= prompt_at
            or not isinstance(terminal_id, str) or not terminal_id):
        return None
    if any((row.get("message") or {}).get("model") != lead.requested_tier
           or (row.get("perTurnEffort") or row.get("effort")) != lead.requested_effort
           for row in assistants):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    final_text = "\n".join(item.get("text", "") for item in content
                           if isinstance(item, dict) and item.get("type") == "text")
    final_status = _reported_status(final_text)
    if "SYMPHONY_OUTCOME:" in final_text and final_status != "completed":
        return None
    reports = [final_text] if final_status == "completed" else []
    if not reports:
        # Background agents hand their result to the parent and then end with
        # a brief goodbye. Only the current turn's successful handback counts.
        handbacks = [(item, row) for row in assistants[:-1]
                     for item in ((row.get("message") or {}).get("content") or [])
                     if isinstance(item, dict) and item.get("type") == "tool_use"
                     and item.get("name") == "SubagentHandback"]
        if len(handbacks) != 1:
            return None
        handback, _ = handbacks[0]
        handback_id = handback.get("id")
        handback_input = handback.get("input")
        report = handback_input.get("message") if isinstance(handback_input, dict) else None
        results = [item for row in turn if row.get("type") == "user"
                   for item in ((row.get("message") or {}).get("content") or [])
                   if isinstance(item, dict) and item.get("type") == "tool_result"
                   and item.get("tool_use_id") == handback_id]
        if (not isinstance(handback_id, str) or len(results) != 1
                or results[0].get("is_error") is True
                or _reported_status(report) != "completed"):
            return None
        reports = [report]
    # A later worker failure or restart supersedes this result, even when its
    # callback reached the root before the missing lead callback was recovered.
    run_identities = {item.identity for item in run.delegations if item.role != "lead"}
    for item in run.delegations if target_prompt is None else ():
        when = _instant(item.updated_at)
        if (item.identity in run_identities and when is not None and when > completed_at
                and item.state.lower() in {"failed", "interrupted", "cancelled",
                                           "canceled", "error", "terminated"}):
            return None
    for record in state.event_history if target_prompt is None else ():
        when = _instant(record.observed_at)
        if when is None or when <= completed_at:
            continue
        payload = record.payload
        if (record.kind == "delegation_updated" and payload.get("identity") in run_identities
                and payload.get("role") != "lead"
                and str(payload.get("state") or "").lower() in {
                    "failed", "interrupted", "cancelled", "canceled", "error", "terminated"}):
            return None
        if record.kind in {"lead_failed", "lead_started"} and payload.get("identity") == lead_id:
            return None
    report = reports[0]
    if len(report) > 100_000:
        return None
    payload = {
        "provider": "claude", "session_id": session, "agent_id": lead_id,
        "parent_thread_id": session, "agent_type": agent_type,
        "prompt_id": prompt_id, "status": "completed",
        "model": lead.requested_tier, "model_reasoning_effort": lead.requested_effort,
        "last_assistant_message": report,
        "_symphony_native_recovery": True,
    }
    event_id = hashlib.sha256(f"claude-host-turn\0{session}\0{lead_id}\0{terminal_id}".encode()).hexdigest()
    return Event(event_id, "subagent_stopped", completed_at.isoformat(), payload)


def claude_recovered_lead_event(
    state: ProjectState, session: str, project: Path, environ: Mapping[str, str],
) -> Event | None:
    """Recover an unobserved first terminal through the ordinary reducer."""
    return _claude_native_lead_event(
        state, session, project, environ, require_missing=True)


def _claude_native_prompt_activity(
    state: ProjectState, session: str, project: Path, environ: Mapping[str, str],
) -> tuple[str, str | None]:
    """Classify child turns without using callback arrival as native turn order."""
    run = state.active_run
    lead_id = run.lead_identity if run else None
    if (not run or run.session_id != session or not isinstance(lead_id, str)
            or not _CLAUDE_ID.fullmatch(lead_id)):
        return "absent", None
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    started_at = _instant(run.started_at)
    if not lead or not lead.requested_tier or not lead.requested_effort or started_at is None:
        return "absent", None
    home = Path(environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = home / "projects"
    if projects.is_symlink() or not projects.is_dir():
        return "absent", None
    paths = tuple(projects.glob(f"*/{session}/subagents/agent-{lead_id}.jsonl"))
    if not paths:
        return "absent", None
    if len(paths) != 1:
        return "unknown", None
    child_path = paths[0]
    root_dir = child_path.parent.parent
    meta_path = child_path.with_suffix(".meta.json")
    try:
        if (root_dir.name != session or root_dir.is_symlink()
                or root_dir.parent.is_symlink() or child_path.parent.is_symlink()
                or meta_path.is_symlink() or not meta_path.is_file()
                or meta_path.stat().st_size > 64 * 1024):
            return "unknown", None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, UnicodeError):
        return "unknown", None
    if (not isinstance(meta, dict) or meta.get("spawnDepth") != 1
            or not isinstance(meta.get("agentType"), str)
            or not meta["agentType"].endswith(
                f"symphony-lead-{lead.requested_tier}-{lead.requested_effort}")
            or not isinstance(meta.get("toolUseId"), str) or not meta["toolUseId"]):
        return "unknown", None
    parent_rows = _native_jsonl(root_dir.with_suffix(".jsonl"))
    child_rows = _native_jsonl(child_path)
    if parent_rows is None or child_rows is None:
        return "unknown", None
    launches = []
    for row in parent_rows:
        if row.get("type") != "assistant" or row.get("sessionId") != session:
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            return "unknown", None
        launches.extend((row, item) for item in content
                        if item.get("id") == meta["toolUseId"])
    if len(launches) != 1:
        return "unknown", None
    parent, launch = launches[0]
    details = launch.get("input")
    if (launch.get("type") != "tool_use" or launch.get("name") != "Agent"
            or not isinstance(details, dict)
            or details.get("subagent_type") != meta["agentType"]):
        return "unknown", None
    try:
        if Path(str(parent.get("cwd") or "")).resolve() != project.resolve():
            return "unknown", None
    except (OSError, ValueError):
        return "unknown", None
    launched_at = _instant(parent.get("timestamp"))
    if launched_at is None or launched_at < started_at:
        return "unknown", None
    prompts = []
    for row in child_rows:
        if (row.get("sessionId") != session or row.get("agentId") != lead_id
                or row.get("isSidechain") is not True):
            return "unknown", None
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if (row.get("type") == "user" and not isinstance(content, (str, list))
                or row.get("type") == "assistant" and
                (not isinstance(content, list) or any(not isinstance(item, dict)
                                                      for item in content))):
            return "unknown", None
        if (row.get("type") == "assistant" and any(
                item.get("type") == "text" and not isinstance(item.get("text"), str)
                for item in content)):
            return "unknown", None
        if row.get("type") == "user" and isinstance(content, list):
            if (not content or any(not isinstance(item, dict)
                                   or item.get("type") != "tool_result"
                                   or not isinstance(item.get("tool_use_id"), str)
                                   for item in content)):
                # A textual prompt may also use a block list. The existing
                # terminal reader cannot identify that turn, so keep Stop.
                return "unknown", None
        if row.get("type") == "user" and isinstance(content, str):
            prompt_id = row.get("uuid")
            when = _instant(row.get("timestamp"))
            if (not isinstance(prompt_id, str) or not prompt_id or when is None
                    or when < launched_at or prompts and when <= prompts[-1][1]):
                return "unknown", None
            prompts.append((prompt_id, when))
    if not prompts:
        return "unknown", None
    return ("multiple" if len(prompts) > 1 else "single"), prompts[-1][0]


def claude_completing_lead_turn(
    state: ProjectState, session: str, project: Path, environ: Mapping[str, str],
) -> tuple[str, Event | None]:
    """Prevent Stop from archiving an older recovered Claude outcome."""
    run = state.active_run
    if not run or run.status != "completing" or run.provider != "claude":
        return "none", None
    anchor = run.assessment.get("_claude_native_recovery")
    if not isinstance(anchor, str) or not anchor:
        activity, latest = _claude_native_prompt_activity(
            state, session, project, environ)
        if activity in {"absent", "single"}:
            return "none", None
        if activity != "multiple":
            return "unknown", None
        try:
            event = _claude_native_lead_event(
                state, session, project, environ, require_missing=False)
        except (AttributeError, OSError, TypeError, ValueError):
            return "unknown", None
        if event is None or event.payload["prompt_id"] != latest:
            return "unknown", None
        return "completed", event
    try:
        event = _claude_native_lead_event(
            state, session, project, environ, require_missing=False)
    except (AttributeError, OSError, TypeError, ValueError):
        return "unknown", None
    if event is None:
        return "unknown", None
    token = f"prompt_id:{event.payload['prompt_id']}"
    if token == anchor:
        return "none", None
    return "completed", event


def _claude_callback_matches_native(source: Event, native: Event, session: str) -> bool:
    identity = native.payload["agent_id"]
    payload = source.payload
    report = payload.get("last_assistant_message")
    return bool(
        source.kind == "subagent_stopped" and payload.get("provider") == "claude"
        and not payload.get("_symphony_owner_conflict")
        and (payload.get("agent_id") or payload.get("subagent_id")) == identity
        and payload.get("parent_thread_id") in {None, "", session}
        and payload.get("session_id") in {session, identity}
        and str(payload.get("status") or "completed").lower() == "completed"
        and payload.get("prompt_id") in {None, "", native.payload["prompt_id"]}
        and payload.get("agent_type") in {None, "", native.payload["agent_type"]}
        and payload.get("model") in {None, "", native.payload["model"]}
        and payload.get("model_reasoning_effort") in {
            None, "", native.payload["model_reasoning_effort"]}
        and isinstance(report, str) and _reported_status(report) == "completed"
        and native.payload["last_assistant_message"] in report
    )


def claude_current_native_lead_event(
    state: ProjectState, source: Event, session: str, project: Path,
    environ: Mapping[str, str],
) -> Event | None:
    """Attach a current native prompt to a callback missing its turn ID."""
    run = state.active_run
    if not run or run.provider != "claude" or run.session_id != session or run.status != "active":
        return None
    lead = next((item for item in run.delegations
                 if item.identity == run.lead_identity and item.role == "lead"), None)
    if not lead or lead.state.lower() not in {"working", "pending"}:
        return None
    native = _claude_native_lead_event(
        state, session, project, environ, require_missing=False)
    if (native is None or f"prompt_id:{native.payload['prompt_id']}" in
            run.assessment.get("_terminal_turns", {}).get(lead.identity, ())
            or not _claude_callback_matches_native(source, native, session)):
        return None
    return native


def claude_committed_native_terminal_replay(
    state: ProjectState, source: Event, session: str, project: Path,
    environ: Mapping[str, str],
) -> bool:
    """Match a delayed hook to the exact native terminal already accepted."""
    if (source.kind != "subagent_stopped" or source.payload.get("provider") != "claude"
            or source.payload.get("_symphony_owner_conflict")):
        return False
    identity = source.payload.get("agent_id") or source.payload.get("subagent_id")
    if not identity or str(source.payload.get("status") or "completed").lower() != "completed":
        return False
    if (source.payload.get("parent_thread_id") not in {None, "", session}
            or source.payload.get("session_id") not in {session, identity}):
        return False
    report = source.payload.get("last_assistant_message")
    if _reported_status(report) != "completed":
        return False
    current = state.active_runs.get(f"claude:{session}")
    if (current is None and state.active_run and state.active_run.provider == "claude"
            and state.active_run.session_id == session):
        current = state.active_run
    if (current and current.provider == "claude" and current.session_id == session
            and current.lead_identity == identity and current.status == "active"
            and any(item.identity == identity and item.state.lower() in {"working", "pending"}
                    for item in current.delegations)):
        # This lead has started another turn. A byte-identical old report
        # cannot identify an untagged callback from the newer native turn.
        return False
    candidates = ([current] if current else []) + list(state.recent_runs)
    # Recent runs are a bounded display archive. The atomic terminal receipt
    # is the lifetime proof for a root that may resume after that trim.
    for receipt in state.terminal_receipts:
        if (receipt.get("provider") != "claude" or receipt.get("session") != session
                or receipt.get("agent") != identity or receipt.get("lead") != identity
                or receipt.get("status") != "completed"
                or not str(receipt.get("turn") or "").startswith("prompt_id:")
                or not all(receipt.get(field) for field in (
                    "native_agent_type", "native_model", "native_effort", "run_id"))):
            continue
        candidates.append(RunState(
            str(receipt["run_id"]), "", status="completed", session_id=session,
            provider="claude", lead_identity=str(identity),
            started_at="1970-01-01T00:00:00+00:00",
            assessment={"_claude_native_recovery": receipt["turn"]},
            delegations=(Delegation(str(identity), "lead", "", "completed",
                                    str(receipt["native_model"]),
                                    str(receipt["native_effort"])),),
        ))
    for run in candidates:
        if not run or run.provider != "claude" or run.session_id != session or run.lead_identity != identity:
            continue
        anchor = run.assessment.get("_claude_native_recovery")
        if not isinstance(anchor, str) or not anchor:
            continue
        native = _claude_native_lead_event(
            ProjectState(active_run=run, event_history=state.event_history),
            session, project, environ, require_missing=False,
            target_prompt=anchor.removeprefix("prompt_id:"))
        if (native is not None and anchor == f"prompt_id:{native.payload['prompt_id']}"
                and _claude_callback_matches_native(source, native, session)):
            return True
    return False


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


def _released_failed_turn(
    state: ProjectState, run: RunState, lead: Delegation,
    turns: dict[str, dict], turn_order: list[str], latest_started: str,
    started_at: datetime,
) -> str | None:
    """Find a unique failed native turn for a pre-turn-lineage recovering run.

    Released 1.5.1 persisted the lead failure and retryable identity, but no
    native turn token. A later candidate may adopt that run only when its one
    recorded failure falls between exactly one matching native failed turn and
    the next native start. Ambiguous or delayed evidence stays unreconciled.
    """
    if ("_retryable_lead_turn" in run.assessment
            or "_terminal_turns" in run.assessment):
        return None
    failures = [event for event in state.event_history
                if event.kind == "lead_failed"
                and event.payload.get("identity") == run.lead_identity
                and event.payload.get("owner_generation", run.owner_generation)
                    == run.owner_generation
                and (when := _instant(event.observed_at)) is not None
                and when >= started_at]
    if len(failures) != 1:
        return None
    failure = failures[0]
    failed_at = _instant(failure.observed_at)
    starts = [event for event in state.event_history
              if event.kind == "lead_started"
              and event.payload.get("identity") == run.lead_identity
              and event.payload.get("owner_generation", run.owner_generation)
                  == run.owner_generation
              and (when := _instant(event.observed_at)) is not None
              and when >= started_at and failed_at is not None and when < failed_at]
    if len(starts) != 1:
        return None
    outcome = failure.payload.get("outcome")
    status = outcome.get("status") if isinstance(outcome, Mapping) else None
    if (failed_at is None or not isinstance(status, str) or not status
            or status.lower() in {"completed", "done", "success", "succeeded"}):
        return None
    latest_start = turns.get(latest_started, {}).get("started_at")
    if latest_start is None or failed_at >= latest_start:
        return None
    before_failure = [turn_id for turn_id in turn_order
                      if (began := turns[turn_id].get("started_at")) is not None
                      and began <= failed_at]
    if len(before_failure) != 1:
        return None
    turn_id = before_failure[0]
    failed = turns[turn_id]
    began = failed.get("started_at")
    next_start = turns[turn_order[1]].get("started_at") if len(turn_order) > 1 else None
    if (not failed.get("started") or failed.get("model") != lead.requested_tier
            or failed.get("effort") != lead.requested_effort
            or str(failed.get("outcome") or "").lower() != status.lower()
            or (ended := failed.get("completed_at")) is None
            or began is None or began < started_at or next_start is None
            or ended < began or ended >= next_start):
        return None
    return turn_id


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
    if failed_token is None:
        released = _released_failed_turn(
            state, run, lead, turns, turn_order, latest_started, started_at)
        if released is not None:
            failed_token = f"turn_id:{released}"
            observed_turns.add(failed_token)
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
