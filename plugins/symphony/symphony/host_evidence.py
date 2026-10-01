"""Recover a lead turn only from native completed-turn evidence.

Some Codex followup turns deliver user SubagentStop hooks but omit a plugin's
SubagentStop command. The root's next hook can reconcile the same recorded
lead without trusting an assistant's prose alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
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


def _complete_native_jsonl(path: Path) -> list[dict] | None:
    """Read one bounded newline-terminated transcript as a single snapshot."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(_MAX_TRANSCRIPT_BYTES + 1)
        if not data or len(data) > _MAX_TRANSCRIPT_BYTES or not data.endswith(b"\n"):
            return None
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines()]
        if not rows or any(not isinstance(row, dict)
                           or not isinstance(row.get("payload"), dict) for row in rows):
            return None
        return rows
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
    for index, row in enumerate(parent_rows):
        if row.get("type") != "assistant" or row.get("sessionId") != session:
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for item in content if isinstance(content, list) else ():
            if isinstance(item, dict) and item.get("id") == launch_id:
                launches.append((index, row, item))
    if len(launches) != 1:
        return None
    parent_index, parent, launch = launches[0]
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
    # Claude's SubagentStop prompt_id identifies the root prompt that launched
    # Agent, while the child transcript uses a separate prompt uuid. Keep both
    # native identities; a callback cannot establish this relation by itself.
    root_prompts = []
    for row in parent_rows[:parent_index]:
        if row.get("type") != "user" or row.get("sessionId") != session:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        textual = isinstance(content, str) or (isinstance(content, list) and content
                    and all(isinstance(item, dict) and item.get("type") == "text"
                            and isinstance(item.get("text"), str) for item in content))
        when = _instant(row.get("timestamp"))
        if textual and when is not None and when <= launched_at:
            prompt_id = row.get("uuid")
            if not isinstance(prompt_id, str) or not prompt_id:
                return None
            root_prompts.append(prompt_id)
    root_prompt_id = root_prompts[-1] if root_prompts else None
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
        "_symphony_native_started_at": prompt_at.isoformat(),
    }
    if root_prompt_id:
        payload["_symphony_root_prompt_id"] = root_prompt_id
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


def _claude_callback_matches_native(
    source: Event, native: Event, session: str, *, allow_conflict: bool = False,
) -> bool:
    identity = native.payload["agent_id"]
    payload = source.payload
    report = payload.get("last_assistant_message")
    return bool(
        source.kind == "subagent_stopped" and payload.get("provider") == "claude"
        and (not payload.get("_symphony_owner_conflict") or allow_conflict)
        and (payload.get("agent_id") or payload.get("subagent_id")) == identity
        and payload.get("parent_thread_id") in {None, "", session}
        and payload.get("session_id") in {session, identity}
        and str(payload.get("status") or "completed").lower() == "completed"
        and payload.get("prompt_id") in {
            None, "", native.payload["prompt_id"],
            native.payload.get("_symphony_root_prompt_id")}
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
    if source.kind != "subagent_stopped" or source.payload.get("provider") != "claude":
        return False
    identity = source.payload.get("agent_id") or source.payload.get("subagent_id")
    if not identity or str(source.payload.get("status") or "completed").lower() != "completed":
        return False
    if (source.payload.get("parent_thread_id") not in {None, "", session}
            or source.payload.get("session_id") not in {session, identity}):
        return False
    if (source.payload.get("_symphony_owner_conflict")
            and source.payload.get("session_id") != session
            and source.payload.get("parent_thread_id") != session
            and source.payload.get("_symphony_verified_alias") is not True):
        # A child-session alias that could belong to another live root is not
        # owned by this archive merely because the agent ID and prompt match.
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
            assessment={"_claude_native_recovery": receipt["turn"],
                        "_start_event_ids": (receipt.get("native_followup_start_id", ""),),
                        "_claude_lead_start_identity": str(identity),
                        "_claude_lead_start_prompt_hash": receipt.get(
                            "native_launch_prompt_hash", "")},
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
        if native is None or anchor != f"prompt_id:{native.payload['prompt_id']}":
            continue
        followup_callback = False
        followup = f"{native.event_id}:followup-start" in run.assessment.get("_start_event_ids", ())
        promptless_root = not source.payload.get("prompt_id") and source.payload.get("session_id") == session
        if followup or promptless_root:
            # Standard Claude hooks omit prompt_id. Bind these retries to the
            # committed native turn and latest successful root resume instead.
            native = _claude_root_followup(run, native, project, environ, replay=True,
                                          allow_original_launch=not followup)
            if native is None:
                continue
            followup_callback = followup and source.payload.get("session_id") == session
        root_prompt = native.payload.get("_symphony_root_prompt_id")
        root_callback = bool(root_prompt and source.payload.get("prompt_id") == root_prompt)
        launch_owner = (source.payload.get("session_id") == session
                        or source.payload.get("parent_thread_id") == session
                        or source.payload.get("_symphony_verified_alias") is True)
        launch_hash = (run.assessment.get("_claude_lead_start_prompt_hash")
                       if run.assessment.get("_claude_lead_start_identity") == identity
                       else None)
        launch_callback = bool(
            launch_owner and isinstance(launch_hash, str) and len(launch_hash) == 64
            and isinstance(source.payload.get("prompt_id"), str)
            and hashlib.sha256(source.payload["prompt_id"].encode()).hexdigest() == launch_hash
        )
        if (root_callback or launch_callback or followup_callback
                or promptless_root):
            # A later native child prompt may be an unfinished new turn. The
            # parent hook prompt identifies the launch, not which child turn ended.
            try:
                activity, latest = _claude_native_prompt_activity(
                    ProjectState(active_run=run), session, project, environ)
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            if activity not in {"single", "multiple"} or latest != native.payload["prompt_id"]:
                continue
        callback = source
        if launch_callback and not root_callback:
            # The hook's launch prompt can differ from both native transcript
            # prompts. It is accepted only from this run's own lead Start.
            callback = replace(source, payload={**source.payload,
                                                "prompt_id": native.payload["prompt_id"]})
        if _claude_callback_matches_native(
                callback, native, session,
                allow_conflict=root_callback or launch_callback or followup_callback):
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


def codex_unavailable_lead_proof(
    state: ProjectState, session: str, environ: Mapping[str, str], replacement_identity: str,
) -> dict[str, object] | None:
    """Bind an unavailable response to the original retryable native lead.

    Codex 0.158 returns a plain function_call_output string, not an error flag.
    Only its exact, paired host output can authorize a different lead; model
    prose, roster absence, and a failure for another task name cannot.
    """
    run = state.active_run
    if not run or run.provider != "codex" or run.session_id != session:
        return None
    lead_id = run.lead_identity or ""
    started_at = _instant(run.started_at)
    if (run.status != "recovering" or run.assessment.get("_retryable_lead") != lead_id
            or not _CODEX_ID.fullmatch(lead_id) or started_at is None
            or not isinstance(replacement_identity, str) or not replacement_identity
            or replacement_identity == lead_id):
        return None
    lead = next((item for item in run.delegations
                 if item.identity == lead_id and item.role == "lead"), None)
    if lead is None or not lead.requested_tier or not lead.requested_effort:
        return None
    sessions = Path(environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    if sessions.is_symlink() or not sessions.is_dir():
        return None
    child_paths = tuple(sessions.glob(f"*/*/*/*{lead_id}.jsonl"))
    paths = tuple(sessions.glob(f"*/*/*/*{session}.jsonl"))
    if len(paths) != 1 or len(child_paths) != 1:
        return None
    path, child_path = paths[0], child_paths[0]
    if (path.is_symlink() or child_path.is_symlink()
            or any(parent.is_symlink() for candidate in (path, child_path)
                   for parent in candidate.parents if sessions in parent.parents)):
        return None
    # Read both files once. A second child read used only for completeness can
    # observe a newer native turn while the parsed lineage remains stale.
    rows = _complete_native_jsonl(path)
    child_rows = _complete_native_jsonl(child_path)
    if (not rows or rows[0].get("type") != "session_meta"
            or rows[0]["payload"].get("id") != session
            or not child_rows or child_rows[0].get("type") != "session_meta"
            or child_rows[0]["payload"].get("id") != lead_id):
        return None
    source = child_rows[0]["payload"].get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if not isinstance(spawn, dict) or spawn.get("parent_thread_id") != session:
        return None
    turns: dict[str, dict] = {}
    turn_order: list[str] = []
    latest_started = ""
    observed_turn_ids: set[str] = set()
    for row in child_rows[1:]:
        payload = row["payload"]
        when = _instant(row.get("timestamp"))
        if when is None:
            return None
        turn_id = payload.get("turn_id")
        if turn_id is not None:
            if not isinstance(turn_id, str) or not turn_id:
                return None
            observed_turn_ids.add(turn_id)
        if row.get("type") == "turn_context":
            if not turn_id:
                return None
            turn = turns.setdefault(turn_id, {})
            if "model" in turn or "effort" in turn:
                return None
            turn["model"], turn["effort"] = payload.get("model"), payload.get("effort")
        elif row.get("type") == "event_msg" and payload.get("type") == "task_started":
            if not turn_id or turn_id in turn_order:
                return None
            turn = turns.setdefault(turn_id, {})
            turn["started"], turn["started_at"] = True, when
            turn_order.append(turn_id)
            latest_started = turn_id
        elif row.get("type") == "event_msg" and payload.get("type") == "task_complete":
            if not turn_id:
                return None
            turn = turns.setdefault(turn_id, {})
            if "completed_at" in turn:
                return None
            turn["completed_at"] = when
            turn["outcome"] = _reported_status(payload.get("last_agent_message"))
    failed_token = run.assessment.get("_retryable_lead_turn")
    if not isinstance(failed_token, str) or not failed_token.startswith("turn_id:"):
        return None
    failed_turn = failed_token.removeprefix("turn_id:")
    terminal_turns = run.assessment.get("_terminal_turns", {})
    failed = turns.get(failed_turn, {})
    if (latest_started != failed_turn or not turn_order
            or observed_turn_ids != set(turn_order)
            or failed_token not in terminal_turns.get(lead_id, ())
            or not failed.get("started") or failed.get("outcome") == "completed"
            or failed.get("started_at") is None or failed.get("completed_at") is None
            or failed["started_at"] < started_at
            or failed["completed_at"] < failed["started_at"]):
        return None
    for index, turn_id in enumerate(turn_order[:-1]):
        earlier = turns[turn_id]
        next_start = turns[turn_order[index + 1]].get("started_at")
        if (not earlier.get("started") or earlier.get("started_at") is None
                or earlier.get("completed_at") is None or next_start is None
                or earlier.get("model") != lead.requested_tier
                or earlier.get("effort") != lead.requested_effort
                or earlier["completed_at"] < earlier["started_at"]
                or earlier["completed_at"] >= next_start):
            return None
    if (failed.get("model") != lead.requested_tier
            or failed.get("effort") != lead.requested_effort):
        return None

    calls: dict[str, tuple[str, dict, datetime]] = {}
    outputs: dict[str, tuple[object, datetime]] = {}
    activity: list[tuple[str, str, datetime]] = []
    for row in rows[1:]:
        payload = row.get("payload")
        when = _instant(row.get("timestamp"))
        if not isinstance(payload, dict) or when is None:
            return None
        kind = payload.get("type")
        if row.get("type") == "response_item" and kind == "function_call":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                return None
            if payload.get("name") not in {"spawn_agent", "followup_task"}:
                continue
            try:
                args = json.loads(payload.get("arguments"))
            except (TypeError, ValueError):
                return None
            if not isinstance(args, dict):
                return None
            calls[call_id] = (str(payload["name"]), args, when)
        elif row.get("type") == "response_item" and kind == "function_call_output":
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                if call_id in outputs:
                    return None
                outputs[call_id] = (payload.get("output"), when)
        elif row.get("type") == "event_msg" and kind == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "SubAgentActivity":
                call_id, agent_id = item.get("id"), item.get("agent_thread_id")
                if isinstance(call_id, str) and isinstance(agent_id, str):
                    activity.append((call_id, agent_id, when))

    linked = [(call_id, args, when) for call_id, (name, args, when) in calls.items()
              if name == "spawn_agent" and any(
                  item_call == call_id and agent_id == lead_id
                  for item_call, agent_id, _ in activity)]
    if len(linked) != 1:
        return None
    spawn_id, spawn_args, spawn_at = linked[0]
    task_name = spawn_args.get("task_name")
    if (not isinstance(task_name, str) or not re.fullmatch(r"[a-z0-9_]{1,128}", task_name)
            or spawn_args.get("model") != lead.requested_tier
            or spawn_args.get("reasoning_effort") != lead.requested_effort
            or spawn_args.get("fork_turns") != "none"
            or not isinstance(spawn_args.get("message"), str)
            or spawn_at < started_at):
        return None
    matched = [(call_id, when, outputs.get(call_id))
               for call_id, (name, args, when) in calls.items()
               if name == "followup_task" and args.get("target") == task_name]
    if not matched:
        return None
    call_id, called_at, response = matched[-1]
    expected = f"live agent path `/root/{task_name}` not found"
    if (response is None or response[0] != expected
            or spawn_at >= failed.get("started_at", started_at)
            or called_at <= failed["completed_at"]
            or response[1] < called_at
            or any(agent_id == lead_id and when > response[1]
                   for _, agent_id, when in activity)):
        return None
    # Another invocation for that task after the exact error makes it unclear
    # whether the host restored the original child before this replacement.
    if any(name == "followup_task" and args.get("target") == task_name
           and when > called_at for name, args, when in calls.values()):
        return None
    digest = hashlib.sha256(
        f"{session}\0{run.run_id}\0{run.owner_generation}\0{lead_id}\0"
        f"{failed_turn}\0{spawn_id}\0{call_id}\0{replacement_identity}".encode()
    ).hexdigest()
    return {"digest": digest, "session_id": session, "run_id": run.run_id,
            "original_lead": lead_id, "owner_generation": run.owner_generation,
            "replacement_identity": replacement_identity}


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
        "_symphony_native_started_at": began.isoformat(),
    }
    event_id = hashlib.sha256(f"codex-host-turn\0{lead_id}\0{latest_started}".encode()).hexdigest()
    return "completed", Event(event_id, "subagent_stopped", completed_at.isoformat(), payload)


def _codex_root_followup(
    run: RunState, native: Event, project: Path, environ: Mapping[str, str],
) -> bool:
    """Bind a successful root followup to the original spawn and new turn."""
    sessions = Path(environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    paths = tuple(sessions.glob(f"*/*/*/*{run.session_id}.jsonl"))
    if sessions.is_symlink() or len(paths) != 1:
        return False
    path = paths[0]
    if any(parent.is_symlink() for parent in path.parents if sessions in parent.parents):
        return False
    rows = _complete_native_jsonl(path)
    if (not rows or rows[0].get("type") != "session_meta"
            or rows[0]["payload"].get("id") != run.session_id
            or not isinstance(rows[0]["payload"].get("cwd"), str)
            or not rows[0]["payload"]["cwd"].strip()
            or not Path(rows[0]["payload"]["cwd"]).is_absolute()
            or Path(rows[0]["payload"]["cwd"]).resolve() != project.resolve()):
        return False
    calls, outputs, activity = {}, {}, []
    for row in rows[1:]:
        payload = row["payload"]
        when = _instant(row.get("timestamp"))
        if when is None:
            return False
        if row.get("type") == "response_item" and payload.get("type") == "function_call":
            if payload.get("name") not in {"spawn_agent", "followup_task"}:
                continue
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                return False
            try:
                args = json.loads(payload.get("arguments"))
            except (TypeError, ValueError):
                return False
            if not isinstance(args, dict):
                return False
            calls[call_id] = (payload["name"], args, when)
        elif row.get("type") == "response_item" and payload.get("type") == "function_call_output":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in outputs:
                return False
            outputs[call_id] = (payload.get("output"), when)
        elif row.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if (isinstance(item, dict) and item.get("type") == "SubAgentActivity"
                    and item.get("agent_thread_id") == run.lead_identity):
                if payload.get("thread_id") != run.session_id:
                    return False
                activity.append((item, when))
    spawns = [(call_id, args, when) for call_id, (name, args, when) in calls.items()
              if name == "spawn_agent" and any(item.get("id") == call_id
                    and item.get("kind") == "started" for item, _ in activity)]
    if len(spawns) != 1:
        return False
    spawn_id, spawn, spawned_at = spawns[0]
    task = spawn.get("task_name")
    starts = [(item, when) for item, when in activity
              if item.get("id") == spawn_id and item.get("kind") == "started"]
    if (not isinstance(task, str) or not re.fullmatch(r"[a-z0-9_]{1,128}", task)
            or len(starts) != 1 or starts[0][0].get("agent_path") != f"/root/{task}"
            or spawn.get("model") != native.payload["model"]
            or spawn.get("reasoning_effort") != native.payload["model_reasoning_effort"]):
        return False
    followups = [(call_id, when) for call_id, (name, args, when) in calls.items()
                 if name == "followup_task" and args.get("target") in {task, f"/root/{task}"}]
    if not followups:
        return False
    archived = _instant(run.updated_at)
    followups = [(call_id, when) for call_id, when in followups if archived and when > archived]
    if len(followups) != 1:
        return False
    call_id, called_at = followups[0]
    response = outputs.get(call_id)
    deliveries = [when for item, when in activity if item.get("id") == call_id
                  and item.get("kind") == "interacted" and item.get("agent_path") == f"/root/{task}"]
    began = _instant(native.payload.get("_symphony_native_started_at"))
    completed = _instant(native.observed_at)
    archived = _instant(run.updated_at)
    return bool(response and response[0] == "" and len(deliveries) == 1
                and began and completed and archived
                and spawned_at <= starts[0][1] < archived < called_at <= deliveries[0] <= began
                and deliveries[0] <= response[1])


def _claude_root_followup(
    run: RunState, native: Event, project: Path, environ: Mapping[str, str], *, replay: bool = False,
    allow_original_launch: bool = False,
) -> Event | None:
    """Claude resumes are proven by an exact Agent resume and tool result."""
    projects = Path(environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    paths = tuple(projects.glob(f"*/{run.session_id}.jsonl"))
    if projects.is_symlink() or len(paths) != 1 or paths[0].parent.is_symlink():
        return None
    rows = _native_jsonl(paths[0])
    if not rows:
        return None
    calls, results = [], {}
    root_prompt = None
    for row in rows:
        if row.get("sessionId") != run.session_id:
            return None
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "user" and isinstance(content, str):
            root_prompt = row.get("uuid")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                return None
            if row.get("type") == "assistant" and item.get("type") == "tool_use":
                details = item.get("input")
                if (item.get("name") == "Agent" and isinstance(details, dict)
                        and details.get("resume") == run.lead_identity):
                    if not isinstance(item.get("id"), str) or not item["id"]:
                        return None
                    calls.append((row, item, root_prompt))
            elif row.get("type") == "user" and item.get("type") == "tool_result":
                result_id = item.get("tool_use_id")
                if not isinstance(result_id, str) or not result_id:
                    return None
                results.setdefault(result_id, []).append((row, item))
    archived = _instant(run.updated_at)
    if not replay:
        calls = [(row, call, prompt) for row, call, prompt in calls
                 if archived and _instant(row.get("timestamp"))
                 and _instant(row["timestamp"]) > archived]
    if not calls and replay and allow_original_launch:
        return native
    if not calls or (not replay and len(calls) != 1):
        return None
    row, call, root_prompt = calls[-1]
    if sum(item.get("id") == call.get("id") for _, item, _ in calls) != 1:
        return None
    matching = results.get(call.get("id"), ())
    began = _instant(native.payload.get("_symphony_native_started_at"))
    archived = _instant(run.updated_at)
    called_at = _instant(row.get("timestamp"))
    if (len(matching) != 1 or not began or (not replay and not archived) or not called_at
            or not called_at <= began or (not replay and called_at <= archived)
            or not isinstance(row.get("cwd"), str) or not row["cwd"].strip()
            or not Path(row["cwd"]).is_absolute()
            or Path(row["cwd"]).resolve() != project.resolve()
            or call["input"].get("subagent_type") not in {None, native.payload["agent_type"]}):
        return None
    result_row, result = matching[0]
    returned_at = _instant(result_row.get("timestamp"))
    # Background Agent results acknowledge dispatch before the child finishes.
    # Completion is established independently by the exact native child turn.
    if result.get("is_error") is True or returned_at is None or returned_at < called_at:
        return None
    return replace(native, payload={**native.payload, "_symphony_root_prompt_id": root_prompt})


def archived_lead_followup(
    state: ProjectState, events: tuple[Event, ...], provider: str, session: str,
    project: Path, environ: Mapping[str, str],
) -> tuple[RunState, Event] | None:
    """A root's explicit native followup continues its latest completed run.

    An outcome alone cannot reopen ownership. Require the original lineage,
    a successful root followup, and a completed new native turn at the pinned
    route; every retained callback must agree with that turn.
    """
    if (not any(event.kind == "subagent_stopped" for event in events)
            or f"{provider}:{session}" in state.active_runs):
        return None
    history = [run for run in state.recent_runs
               if run.provider == provider and run.session_id == session]
    if not history:
        return None
    run = history[-1]
    if (run.status != "completed" or run.unreconciled or not run.lead_identity
            or any(item.state.lower() in {"working", "pending", "interrupted"}
                   for item in run.delegations)
            or any(run.lead_identity in {other.lead_identity, *(item.identity for item in other.delegations)}
                   for other in state.active_runs.values() if other.provider == provider)):
        return None
    scoped = replace(state, active_run=replace(run, status="completing"))
    if provider == "codex":
        _, native = codex_completing_lead_turn(scoped, session, environ)
        if (native is None or native.payload.get("status") != "completed"
                or not _codex_root_followup(run, native, project, environ)):
            return None
    elif provider == "claude":
        _, native = claude_completing_lead_turn(scoped, session, project, environ)
        if native is None:
            return None
        native = _claude_root_followup(run, native, project, environ)
        if native is None:
            return None
    else:
        return None
    for event in events:
        payload = event.payload
        if (payload.get("provider") != provider or payload.get("session_id") != session
                or (payload.get("agent_id") or payload.get("subagent_id")) != run.lead_identity
                or event.kind not in {"subagent_started", "subagent_stopped"}):
            return None
        if provider == "codex":
            if (payload.get("parent_thread_id") != session
                    or payload.get("turn_id") != native.payload["turn_id"]
                    or payload.get("model") not in {None, "", native.payload["model"]}
                    or payload.get("model_reasoning_effort") not in {
                        None, "", native.payload["model_reasoning_effort"]}
                    or event.kind == "subagent_stopped" and (
                        str(payload.get("status") or "completed").lower() != "completed"
                        or payload.get("last_assistant_message") != native.payload["last_assistant_message"])):
                return None
        elif event.kind == "subagent_stopped":
            if (payload.get("prompt_id") not in {None, "", native.payload["prompt_id"],
                                                        native.payload.get("_symphony_root_prompt_id")}
                    or not _claude_callback_matches_native(event, native, session, allow_conflict=True)):
                return None
        elif (payload.get("parent_thread_id") not in {None, "", session}
              or payload.get("prompt_id") not in {None, "", native.payload["prompt_id"],
                                                  native.payload.get("_symphony_root_prompt_id")}):
            return None
    return run, native


def claude_committed_native_start_replay(
    state: ProjectState, source: Event, session: str, project: Path, environ: Mapping[str, str],
) -> bool:
    """Recognize a delayed Start only for an already accepted native followup."""
    payload = source.payload
    if (source.kind != "subagent_started" or payload.get("provider") != "claude"
            or payload.get("session_id") != session
            or payload.get("parent_thread_id") not in {None, "", session}):
        return False
    identity = payload.get("agent_id") or payload.get("subagent_id")
    for run in (*state.active_runs.values(), *state.recent_runs):
        if (run.provider != "claude" or run.session_id != session or run.lead_identity != identity
                or run.status not in {"completing", "completed"}):
            continue
        native = _claude_native_lead_event(
            replace(state, active_run=run), session, project, environ, require_missing=False)
        if (native is None or run.assessment.get("_claude_native_recovery") !=
                f"prompt_id:{native.payload['prompt_id']}"
                or f"{native.event_id}:followup-start" not in run.assessment.get("_start_event_ids", ())):
            continue
        native = _claude_root_followup(run, native, project, environ, replay=True)
        if (native is not None and payload.get("prompt_id") in {
                None, "", native.payload["prompt_id"], native.payload.get("_symphony_root_prompt_id")}
                and payload.get("agent_type") in {None, "", native.payload["agent_type"]}
                and payload.get("model") in {None, "", native.payload["model"]}
                and payload.get("model_reasoning_effort") in {None, "", native.payload["model_reasoning_effort"]}):
            return True
    return False
