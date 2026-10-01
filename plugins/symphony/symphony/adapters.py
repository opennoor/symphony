"""Translate provider hook payloads to and from Symphony's core model."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .model import Action, Event


EVENT_KINDS = {
    "SessionStart": "session_heartbeat",
    "UserPromptSubmit": "user_prompt",
    "PreToolUse": "pre_tool_use",
    "SubagentStart": "subagent_started",
    "SubagentStop": "subagent_stopped",
    "PostToolUse": "post_tool_use",
    "PostToolUseFailure": "post_tool_failed",
    "Stop": "stop_requested",
    "Interrupt": "interrupt",
}


@dataclass(frozen=True)
class HookResult:
    stdout: str = ""


def detect_provider(payload: dict[str, Any]) -> str:
    explicit = payload.get("provider")
    if explicit in {"codex", "claude"}:
        return explicit
    if "turn_id" in payload or "model" in payload:
        return "codex"
    return "claude"


def event_from_payload(provider: str, payload: dict[str, Any]) -> Event:
    name = payload.get("hook_event_name", "")
    kind = EVENT_KINDS.get(name, "unknown")
    canonical = dict(payload)
    if provider == "codex" and name in {"SubagentStart", "SubagentStop"}:
        child_metadata = _codex_subagent_metadata(payload)
        canonical.update(child_metadata)
        canonical["_symphony_child_metadata"] = tuple(child_metadata)
    if provider == "claude" and name == "SubagentStop":
        # A background agent delivers its report through a SubagentHandback
        # call and then says a one-line goodbye, which is all the host puts in
        # last_assistant_message. The result markers live in the report.
        report = _claude_handback_report(payload)
        if report:
            canonical["last_assistant_message"] = (
                report + "\n" + str(payload.get("last_assistant_message") or "")
            )
    canonical["provider"] = provider
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return Event(
        event_id=hashlib.sha256(raw.encode()).hexdigest(),
        kind=kind,
        observed_at=datetime.now(timezone.utc).isoformat(),
        payload=canonical,
    )


def _codex_subagent_metadata(payload: dict[str, Any]) -> dict[str, str]:
    transcript = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not transcript:
        return {}
    found: dict[str, str] = {}
    header_seen = False
    forked = False
    own_turn = True
    callback_turn = str(payload.get("turn_id") or "")
    try:
        with Path(str(transcript)).open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= 64:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("payload", {}), dict):
                    continue
                record_payload = record.get("payload", {})
                if record.get("type") == "session_meta":
                    # A fork contains copied ancestor headers and turns after
                    # its own first header. They are not this child's evidence.
                    if header_seen:
                        forked = True
                        own_turn = False
                        continue
                    header_seen = True
                    identity = record_payload.get("id")
                    if identity and payload.get("agent_id") and identity != payload["agent_id"]:
                        return {}
                    forked = bool(record_payload.get("forked_from_id"))
                    own_turn = not forked
                    spawn = record_payload
                    for key in ("source", "subagent", "thread_spawn"):
                        spawn = spawn.get(key, {}) if isinstance(spawn, dict) else {}
                    if not isinstance(spawn, dict):
                        spawn = {}
                    agent_path = record_payload.get("agent_path") or spawn.get("agent_path")
                    if agent_path:
                        found["task_name"] = str(agent_path).rsplit("/", 1)[-1]
                    parent_thread_id = spawn.get("parent_thread_id")
                    if parent_thread_id:
                        found["parent_thread_id"] = str(parent_thread_id)
                elif record.get("type") == "turn_context":
                    if forked:
                        own_turn = bool(callback_turn and record_payload.get("turn_id") == callback_turn)
                    elif callback_turn and record_payload.get("turn_id"):
                        own_turn = record_payload["turn_id"] == callback_turn
                    if not own_turn:
                        continue
                    if record_payload.get("model"):
                        found["model"] = str(record_payload["model"])
                    if record_payload.get("effort"):
                        found["model_reasoning_effort"] = str(record_payload["effort"])
                elif (record.get("type") == "event_msg"
                      and record_payload.get("type") == "user_message" and own_turn):
                    message = record_payload.get("message")
                    if isinstance(message, str) and "SYMPHONY_FAST_ROUTE: lead" in message:
                        found["task"] = message
                elif (record.get("type") == "response_item"
                      and record_payload.get("type") == "message"
                      and record_payload.get("role") == "user" and own_turn):
                    content = record_payload.get("content")
                    if isinstance(content, list):
                        message = "\n".join(str(item.get("text")) for item in content
                                            if isinstance(item, dict) and item.get("type") == "input_text"
                                            and isinstance(item.get("text"), str))
                        if "SYMPHONY_FAST_ROUTE: lead" in message:
                            found["task"] = message
                if {"task_name", "model_reasoning_effort", "task"} <= found.keys():
                    break
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return found
    return found


def _claude_handback_report(payload: dict[str, Any]) -> str:
    """The last report a Claude subagent handed back to its caller, if any."""
    transcript = payload.get("agent_transcript_path")
    if not transcript:
        return ""
    report = ""
    try:
        with Path(str(transcript)).open(encoding="utf-8") as handle:
            for line in handle:
                if "SubagentHandback" not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message") if isinstance(record, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                for item in content if isinstance(content, list) else ():
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") == "SubagentHandback"
                    ):
                        values = item.get("input")
                        if isinstance(values, dict):
                            report = str(values.get("message") or report)
    except (OSError, TypeError, ValueError, AttributeError):
        return report
    return report


def render(
    provider: str,
    actions: tuple[Action, ...],
    hook_event_name: str = "UserPromptSubmit",
) -> HookResult:
    context = "\n".join(
        str(action.payload.get("text", ""))
        for action in actions
        if action.kind == "inject_context" and action.payload.get("text")
    )
    block = next((action for action in actions if action.kind in {"block_stop", "block_tool"}), None)
    if block:
        reason = block.payload.get("reason") or _active_reason(block.payload.get("active", ()))
        if block.kind == "block_tool" and provider == "claude":
            return HookResult(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": hook_event_name,
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
        return HookResult(json.dumps({"decision": "block", "reason": reason}))
    if provider in {'claude', 'codex'} and hook_event_name == 'Stop':
        notice = next((action.payload.get('reason') for action in actions
                       if action.kind == 'permit_stop' and action.payload.get('reason')), None)
        if notice:
            # additionalContext would continue Claude's Stop loop. The common
            # systemMessage field displays a warning without requesting a turn.
            return HookResult(json.dumps({'systemMessage': notice}))
    if context:
        return HookResult(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": hook_event_name,
                        "additionalContext": context,
                    }
                }
            )
        )
    return HookResult()


def _active_reason(active: Any) -> str:
    identities = ", ".join(map(str, active))
    return f"Symphony is still tracking active work: {identities}." if identities else "Symphony work remains active."
