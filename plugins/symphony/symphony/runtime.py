"""Fast, offline hook runtime for Symphony's canonical lifecycle."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
from typing import Mapping

from . import HOOK_SCHEMA_VERSION, PLUGIN_VERSION
from .adapters import HookResult, detect_provider, event_from_payload, render
from .model import Action, Delegation, Event, ProjectState
from .reducer import reduce
from .routing import (
    Assessment,
    EFFORTS,
    NO_PROFILE,
    assessor_selection,
    clamp_against_best,
    model_is_weaker,
    profiles_for,
    resolve_tier,
    route_for,
    snapshot_for,
)
from .store import StateStore


CONTROLS = {
    "agents",
    "boost",
    "bypass",
    "disable",
    "enable",
    "help",
    "proceed",
    "reassess",
    "start",
    "status",
    "stop",
    "version",
}
ROLES = {"assessor", "consultant", "lead", "worker"}
HIGH_EFFORTS = {"high", "xhigh", "max", "ultra"}


def handle(payload: dict, environ: Mapping[str, str] = os.environ) -> HookResult:
    if environ.get("SYMPHONY_CLAUDE_PROBE"):
        return HookResult()
    provider = str(environ.get("SYMPHONY_PROVIDER") or detect_provider(payload))
    project = Path(payload.get("cwd") or os.getcwd()).resolve()
    legacy_roots = tuple(
        Path(environ[name])
        for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA")
        if environ.get(name)
    )
    store = StateStore(
        Path(environ.get("SYMPHONY_STATE_DIR", Path.home() / ".symphony" / "state")),
        legacy_roots,
    )
    source = event_from_payload(provider, payload)

    def transition(state: ProjectState) -> tuple[ProjectState, tuple[Action, ...]]:
        # Select the owning run under the store lock so parallel hooks cannot
        # observe a stale project roster.
        current_scope = _run_scope(state, source, provider)
        if current_scope is None:
            return state, ()
        current_key, current_session = current_scope
        current_payload = {**payload, "session_id": current_session}
        current_source = replace(source, payload={**source.payload, "session_id": current_session})
        scoped = _scope_state(state, current_key, current_session, provider)
        next_scoped, actions = _transition(scoped, current_source, provider, current_payload, environ)
        rendered = _render_actions(actions, next_scoped, provider, source.kind)
        merged = _merge_scope(state, scoped, next_scoped, current_key, provider, current_session)
        control = _parse_control(str(payload.get("prompt") or "")) if source.kind == "user_prompt" else None
        if next_scoped.active_run or source.kind == "stop_requested" or (control and control[0] in {"disable", "stop"}):
            merged = _release_pending_session(merged, provider, current_session)
        return merged, rendered

    actions = store.update(project, transition)
    return render(provider, actions, str(payload.get("hook_event_name") or "UserPromptSubmit"))


def _run_scope(state: ProjectState, source: Event, provider: str) -> tuple[str, str] | None:
    """Find the owning root without ever borrowing another session's run."""
    session = str(source.payload.get("session_id") or "")
    key = f"{provider}:{session}" if session else ""
    parent = str(source.payload.get("parent_thread_id") or "")
    agent = str(source.payload.get("agent_id") or source.payload.get("subagent_id") or "")
    if source.kind in {"subagent_started", "subagent_stopped"}:
        session_matches = [
            (owned_key, run.session_id or session) for owned_key, run in state.active_runs.items()
            if owned_key.startswith(f"{provider}:")
            and session in {run.session_id, run.lead_identity,
                            *(item.identity for item in run.delegations)}
        ] if session else []
        if parent:
            matches = [
                (owned_key, run.session_id or parent) for owned_key, run in state.active_runs.items()
                if owned_key.startswith(f"{provider}:")
                and parent in {run.session_id, run.lead_identity,
                               *(item.identity for item in run.delegations)}
            ]
            if len(matches) == 1:
                if ((key in state.active_runs and key != matches[0][0])
                        or (session_matches and all(owner != matches[0][0]
                                                    for owner, _ in session_matches))):
                    return None
                return matches[0]
            if matches:
                owned = [match for match in matches if match in session_matches]
                if len(owned) == 1 and (key not in state.active_runs or owned[0][0] == key):
                    return owned[0]
                return None
            if session_matches:
                return None
            if source.kind == "subagent_started":
                return f"{provider}:{parent}", parent
            return None
        if len(session_matches) == 1:
            return session_matches[0]
        if session_matches:
            return None
        if source.kind == "subagent_started" and session and _observed_role(source.payload) == "assessor":
            return key, session
        if agent:
            matches = [
                (key, run.session_id or session) for key, run in state.active_runs.items()
                if key.startswith(f"{provider}:")
                and any(item.identity == agent for item in run.delegations)
            ]
            if len(matches) == 1:
                return matches[0]
            if matches:
                return None
    if source.kind in {"pre_tool_use", "post_tool_use", "post_tool_failed"} and session:
        matches = [
            (key, run.session_id) for key, run in state.active_runs.items()
            if key.startswith(f"{provider}:")
            and session in {run.lead_identity, *(item.identity for item in run.delegations)}
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    if not session:
        return None
    if key in state.active_runs:
        return key, session
    if source.kind in {"subagent_started", "subagent_stopped"}:
        # A child with no known parent/run cannot claim an arbitrary project
        # run. An assessor may open a new run under its reported session.
        if source.kind == "subagent_started" and _observed_role(source.payload) == "assessor":
            return key, session
        return None
    return key, session


def _scope_state(state: ProjectState, key: str, session: str, provider: str) -> ProjectState:
    run = state.active_runs.get(key)
    if run and not run.provider:
        run = replace(run, provider=provider, session_id=session)
    return replace(state, active_run=run)


def _merge_scope(
    original: ProjectState, before: ProjectState, after: ProjectState,
    key: str, provider: str, session: str,
) -> ProjectState:
    runs = dict(original.active_runs)
    if before.active_run:
        runs.pop(key, None)
    run = None
    if after.active_run:
        run = replace(after.active_run, session_id=session, provider=provider)
        runs[f"{provider}:{session}"] = run
    history = original.recent_runs
    if before.active_run and not after.active_run and after.recent_runs:
        history = (*history, after.recent_runs[-1])[-20:]
    # Keep the old scalar view useful for readers of a single-run state. It is
    # never used to select the run for a hook.
    latest = run or next(reversed(tuple(runs.values())), None)
    return replace(
        after, active_run=latest, active_runs=runs, recent_runs=history,
    )


def _release_pending_session(state: ProjectState, provider: str, session: str) -> ProjectState:
    activation = dict(state.activation)
    record = dict(activation.get(provider) or {})
    pending = list(record.get("pending_sessions") or ())
    if session not in pending:
        return state
    record["pending_sessions"] = [item for item in pending if item != session]
    activation[provider] = record
    return replace(state, activation=activation)


def _transition(
    state: ProjectState,
    source: Event,
    provider: str,
    payload: Mapping[str, object],
    environ: Mapping[str, str],
) -> tuple[ProjectState, tuple[Action, ...]]:
    actions: tuple[Action, ...] = ()
    if source.kind not in {"session_heartbeat", "user_prompt"}:
        state = _activate_session_profile(state, provider, str(payload.get("session_id") or ""))

    if source.kind in {"session_heartbeat", "user_prompt"}:
        probe_claude = _should_probe_claude(state, source, provider, environ)
        previous = _session_profile_record(state, "claude", str(payload.get("session_id") or ""))
        prompt = str(payload.get("prompt") or "").strip()
        control = _parse_control(prompt) if source.kind == "user_prompt" else None
        pending_task = bool(source.kind == "user_prompt" and not state.active_run and (
            (control is None and state.enabled and prompt)
            or (control and control[0] in {"start", "enable"} and control[1])
        ))
        heartbeat = Event(
            source.event_id + ":" + source.observed_at + ":heartbeat",
            "session_heartbeat",
            source.observed_at,
            {
                "provider": provider,
                "session_id": payload.get("session_id"),
                "plugin_version": PLUGIN_VERSION,
                "plugin_root": environ.get(
                    "SYMPHONY_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1])
                ),
                "runtime_root": environ.get("SYMPHONY_PINNED_RUNTIME"),
                "hook_schema_version": HOOK_SCHEMA_VERSION,
                "last_fault": _drain_fault(environ),
                "profile": _entitlement_profile(
                    state, provider, str(payload.get("session_id") or ""), environ,
                    probe_claude=probe_claude,
                ),
                "claude_probe_attempted": probe_claude or (
                    previous.get("claude_probe_attempted", False)
                    if previous.get("plugin_version") == PLUGIN_VERSION else False
                ),
                "pending_task": pending_task,
            },
        )
        state, heartbeat_actions = reduce(state, heartbeat)
        actions += heartbeat_actions
        retained = environ.get("SYMPHONY_PINNED_RUNTIME")
        original = str(heartbeat.payload["plugin_root"])
        if retained and (source.kind == "session_heartbeat" or not Path(original).is_dir()):
            executable = "python.exe" if os.name == "nt" else "python3"
            text = (
                f"Symphony retained the reviewed runtime at {retained}. "
                "If the loaded plugin cache disappears, use this retained root for Symphony references. "
            )
            bootstrap = environ.get("SYMPHONY_BOOTSTRAP_CODE")
            if provider == "codex" and bootstrap:
                arguments = [executable, "-I", "-c", bootstrap, original, provider, "--check-activation"]
                command = ("& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
                           if os.name == "nt" else shlex.join(arguments))
                text += f"Check activation through the verified launcher: {command}"
            actions += (Action("inject_context", {"text": text}),)

    # Every event from the owning session is evidence it is still alive, which
    # is what stops a second terminal from declaring this run abandoned.
    owner_session = str(payload.get("session_id") or "")
    if state.active_run and owner_session and state.active_run.session_id == owner_session:
        state = replace(
            state, active_run=replace(state.active_run, owner_seen_at=source.observed_at)
        )

    if source.kind == "user_prompt":
        state, deferred = _consume_parent_actions(state)
        actions += deferred
        state, prompt_actions = _handle_prompt(state, source, provider, environ)
        actions += prompt_actions
    elif source.kind == "session_heartbeat":
        state, resume_actions = _reconcile_session(state, source, payload)
        actions += resume_actions
        if state.active_run:
            actions += (Action("inject_context", {"text": _recovery_guidance(state)}),)
    elif source.kind == "pre_tool_use":
        state, delegation_actions = _prepare_delegation(state, source, provider)
        actions += delegation_actions
    elif source.kind in {"subagent_started", "subagent_stopped"}:
        if source.kind == "subagent_started":
            state, deferred = _consume_parent_actions(state)
            actions += deferred
        state, observed_actions = _observe_delegation(state, source)
        if source.kind == "subagent_stopped":
            # Neither host accepts injected context on a subagent-stop result,
            # so corrective guidance waits for the next event that does. Render
            # before deferring: the deferral keeps only presentable kinds, so
            # raw lifecycle decisions were being dropped a second time here,
            # behind the renderer rather than in front of it.
            state = _defer_parent_actions(
                state, _render_actions(observed_actions, state, provider, source.kind)
            )
        else:
            actions += observed_actions
            if (
                provider == "claude"
                and state.active_run
                and state.active_run.lead_identity
                and state.active_run.lead_identity == str(payload.get("agent_id") or "")
            ):
                # SubagentStart context reaches the starting agent, not the root.
                actions += (Action("inject_context", {"text": _claude_lead_guidance(state)}),)
    elif source.kind in {"post_tool_use", "post_tool_failed"}:
        state = _discard_failed_spawn(state, source, provider)
        state, parent_actions = _consume_parent_actions(state)
        actions += parent_actions
    elif source.kind in {"stop_requested", "interrupt"}:
        if source.kind == "stop_requested":
            state, reconciliation_actions = _reconcile_session(state, source, payload)
            actions += reconciliation_actions
        state, lifecycle_actions = reduce(state, source)
        actions += lifecycle_actions

    return state, actions


def _carried_acceptance(
    state: ProjectState, provider: str, session_id: str, key: str = "profile"
) -> str:
    """An accepted clamp survives the rest of its own session and no longer.

    Held per session. The activation record has one slot per provider, so a
    heartbeat from a second terminal used to overwrite this session's consent
    with an empty string, and `proceed` silently stopped holding.
    """
    recorded = state.activation.get(provider, {})
    if not isinstance(recorded, Mapping) or not session_id:
        return ""
    entry = (recorded.get("accepted") or {}).get(session_id)
    if isinstance(entry, Mapping):
        return str(entry.get(key) or "")
    # Records written before acceptance was keyed by session.
    legacy = "accepted_profile" if key == "profile" else "accepted_route"
    if recorded.get("session_id") == session_id:
        return str(recorded.get(legacy) or "")
    return ""


def _session_profile_record(state: ProjectState, provider: str, session_id: str) -> Mapping:
    recorded = state.activation.get(provider, {})
    if not session_id or not isinstance(recorded, Mapping):
        return {}
    if recorded.get("session_id") == session_id:
        return recorded
    return next((item for item in reversed(recorded.get("session_profiles", ()))
                 if isinstance(item, Mapping) and item.get("session_id") == session_id), {})


def _activate_session_profile(state: ProjectState, provider: str, session_id: str) -> ProjectState:
    """Restore this session's route before hooks that do not emit a heartbeat."""
    current = state.activation.get(provider, {})
    if not session_id or current.get("session_id") == session_id:
        return state
    record = _session_profile_record(state, provider, session_id)
    valid = record.get("plugin_version") == PLUGIN_VERSION
    accepted = dict(current.get("accepted") or {})
    previous_session = str(current.get("session_id") or "")
    if previous_session and previous_session not in accepted and current.get("accepted_profile"):
        accepted[previous_session] = {
            "profile": str(current.get("accepted_profile") or ""),
            "route": str(current.get("accepted_route") or ""),
        }
    activation = dict(state.activation)
    activation[provider] = {
        **current,
        "session_id": session_id,
        "plugin_version": PLUGIN_VERSION,
        "profile": record.get("profile", "") if valid else "",
        "claude_probe_attempted": bool(record.get("claude_probe_attempted")) if valid else False,
        "accepted": accepted,
        "accepted_profile": "",
        "accepted_route": "",
    }
    return replace(state, activation=activation)


def _should_probe_claude(
    state: ProjectState, source: Event, provider: str, environ: Mapping[str, str]
) -> bool:
    session_id = str(source.payload.get("session_id") or "")
    if (provider != "claude" or source.kind != "user_prompt"
            or (state.active_run and state.active_run.session_id != session_id)
            or environ.get("SYMPHONY_CLAUDE_PROBE")):
        return False
    if environ.get("SYMPHONY_PROFILE") or environ.get("SYMPHONY_CLAUDE_AVAILABLE_MODELS"):
        return False
    recorded = _session_profile_record(state, "claude", session_id)
    if recorded.get("plugin_version") == PLUGIN_VERSION and recorded.get("claude_probe_attempted"):
        return False
    control = _parse_control(str(source.payload.get("prompt") or "").strip())
    return (control is None and (state.enabled or state.active_run is not None)) or bool(
        control and control[0] in {"start", "enable"} and control[1]
    )


def _entitlement_profile(
    state: ProjectState, provider: str, session_id: str, environ: Mapping[str, str],
    *, probe_claude: bool = False,
) -> str:
    """Which shipped profile this account can run, probed once per session/version.

    Entitlement does not change within a session, so the stored answer is
    reused until the session or plugin version changes. A probe that yields
    nothing returns the empty string, which routes through the conservative floor.
    A readable roster with no matching shipped profile is unavailable.
    """
    try:
        profiles = profiles_for(provider)
    except (OSError, ValueError, KeyError, TypeError):
        return NO_PROFILE
    pinned = environ.get("SYMPHONY_PROFILE")
    if pinned:
        # An explicit pin skips probing entirely: useful when a host's private
        # caches are unreadable, and what keeps tests off the developer's box.
        return pinned
    recorded = _session_profile_record(state, provider, session_id)
    if (recorded.get("plugin_version") == PLUGIN_VERSION
            and recorded.get("profile")):
        return str(recorded["profile"])
    entitled = _entitlement(provider, environ, probe_claude=probe_claude)
    if entitled is None:
        return ""
    for profile in profiles:
        required_all = {str(item) for item in profile.get("requires_all", ())}
        required_any = {str(item) for item in profile.get("requires_any", ())}
        selected = {str(choice["model"]) for choice in profile.get("matrix", {}).values()}
        if required_all and not required_all <= entitled:
            continue
        if required_any and not required_any & entitled:
            continue
        if not selected <= entitled:
            continue
        return str(profile["id"])
    return NO_PROFILE


def _entitlement(provider: str, environ: Mapping[str, str], *, probe_claude: bool = False) -> set[str] | None:
    """What the account grants, read without touching any credential file."""
    if provider == "codex":
        return _codex_entitlement(environ)
    return _claude_entitlement(environ, probe=probe_claude)


def _codex_entitlement(environ: Mapping[str, str]) -> set[str] | None:
    home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        roster = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        str(item.get("slug"))
        for item in roster.get("models", ())
        if isinstance(item, Mapping) and item.get("visibility") == "list" and item.get("slug")
    }


def _claude_entitlement(environ: Mapping[str, str], *, probe: bool = False) -> set[str] | None:
    # CI's API key and the user's Claude Code login can have different access.
    models = environ.get("SYMPHONY_CLAUDE_AVAILABLE_MODELS", "")
    explicit = {model.strip() for model in models.split(",") if model.strip()}
    if explicit or not probe:
        return explicit or None
    targets = ("claude-sonnet-5", "claude-opus-5-5")
    available = set()
    for model in targets:
        if model not in available and _claude_accepts(model):
            available.add(model)
    return available or None


def _claude_accepts(model: str) -> bool:
    """Ask this Claude Code login, then verify the model that actually served."""
    try:
        executable = shutil.which("claude")
        if not executable:
            return False
        completed = subprocess.run(
            [executable, "--settings", '{"disableAllHooks":true}', "--print", "--no-session-persistence",
             "--tools", "", "--disallowedTools", "mcp__*",
             "--system-prompt", "Reply ok.", "--model", model,
             "--max-budget-usd", "0.25", "--output-format", "json", "Reply ok."],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "SYMPHONY_CLAUDE_PROBE": "1"},
        )
        if completed.returncode:
            return False
        result = json.loads(completed.stdout)
        return isinstance(result, dict) and not result.get("is_error") and set(result.get("modelUsage", {})) == {model}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return False


# Bookkeeping the host has no use for. Everything else must render.
INTERNAL_ACTIONS = frozenset(
    {"archive_run", "permit_completion", "spawn_assessor", "permit_stop", "run_abandoned"}
)


def _control_name(name: str, provider: str) -> str:
    """How a user types a Symphony control on this host."""
    return f"/symphony:{name}" if provider == "claude" else f"$symphony:symphony {name}"


def _reconcile_session(
    state: ProjectState, source: Event, payload: Mapping[str, object]
) -> tuple[ProjectState, tuple[Action, ...]]:
    """Reconcile tracked work against the session the host is reporting now."""
    run = state.active_run
    if not run:
        return state, ()
    active_ids = payload.get("active_agent_ids", payload.get("active_ids"))
    session_id = str(payload.get("session_id") or "")
    # A host snapshot for another run is not evidence about this run. Hosts
    # that do not send a run id remain supported; when they do, require an
    # exact match before changing durable lifecycle state.
    observed_run_id = payload.get("run_id")
    if observed_run_id is not None and str(observed_run_id) != run.run_id:
        return state, ()
    if run.session_id and session_id != run.session_id:
        return state, ()
    if isinstance(active_ids, list):
        # A malformed roster is incomplete evidence, not an empty roster.
        if any(not isinstance(item, str) or not item for item in active_ids):
            return state, ()
        observed = list(active_ids)
    else:
        return state, ()
    state, actions = reduce(
        state, _derived(state, source, "resume_reconciled", {"active_ids": observed}, "resume")
    )
    return state, actions


def _handle_prompt(
    state: ProjectState, source: Event, provider: str, environ: Mapping[str, str]
) -> tuple[ProjectState, tuple[Action, ...]]:
    prompt = str(source.payload.get("prompt") or "").strip()
    control = _parse_control(prompt)
    if control is None:
        # Enablement decides whether a NEW run opens, never whether a live one
        # is mentioned. Staying silent during a one-shot run left the root free
        # to do the work itself, in parallel with the lead it was never told
        # about, at whatever model the session happened to be set to.
        if not state.enabled and not state.active_run:
            return state, ()
        return state, (Action("inject_context", {"text": _task_guidance(state, prompt, provider, str(source.payload.get("session_id") or ""))}),)

    name, argument = control
    if name == "boost":
        session_id = str(source.payload.get("session_id") or "")
        levels = ("xhigh", "max", "ultra") if provider == "codex" else ("xhigh", "max")
        requested = argument or levels[-1]
        if requested not in {*levels, "off", "reset"}:
            return state, (Action("inject_context", {"text": f"Use Symphony boost [{'|'.join(levels)}|off]; reset restores high effort."}),)
        if not session_id:
            return state, (Action("inject_context", {"text": "Symphony boost requires an observed session identity."}),)
        if requested not in {"off", "reset"}:
            selected = assessor_selection(_snapshot(state, provider), requested)
            if not selected["effort"]:
                return state, (Action("inject_context", {"text": f"Assessor effort {requested} is unsupported for {selected['model'] or 'this account'}. Preference unchanged; choose a supported native level or off."}),)
        configuration = dict(state.configuration)
        preferences = dict(configuration.get("assessor_boosts", {}))
        key = f"{provider}:{session_id}"
        if requested in {"off", "reset"}:
            preferences.pop(key, None)
        else:
            preferences[key] = requested
        configuration["assessor_boosts"] = preferences
        state = replace(state, configuration=configuration)
        return state, (Action("inject_context", {"text": _boost_status(state, provider, session_id)}),)
    if name == "help":
        return state, (Action("inject_context", {"text": _help(provider)}),)
    if name == "version":
        return state, (Action("inject_context", {"text": _version_text(environ)}),)
    if name == "status":
        return state, (Action("inject_context", {"text": _status(
            state, False, provider, str(source.payload.get("session_id") or "")
        )}),)
    if name == "agents":
        return state, (Action("inject_context", {"text": _status(
            state, argument == "--all", provider, str(source.payload.get("session_id") or "")
        )}),)
    if name == "enable":
        next_state, actions = reduce(state, _derived(state, source, "enable"))
        if argument:
            actions += (
                Action("inject_context", {"text": _task_guidance(next_state, argument, provider, str(source.payload.get("session_id") or ""))}),
            )
        return next_state, actions
    if name == "start":
        if not argument:
            return state, (Action("inject_context", {"text": "Symphony start requires a task."}),)
        return state, (Action("inject_context", {"text": _task_guidance(state, argument, provider, str(source.payload.get("session_id") or ""))}),)
    if name == "proceed":
        profile = _applied_profile(state, provider)
        standing = _standing_route(state, provider)
        if not standing and profile == profiles_for(provider)[0]["id"]:
            # Fully entitled with nothing standing: no weaker route to consent to.
            return state, (
                Action("inject_context", {"text": "Symphony has no clamped route to accept."}),
            )
        return reduce(
            state,
            _derived(
                state,
                source,
                "route_accepted",
                {
                    "provider": provider,
                    "profile": profile,
                    "route": standing,
                    "session_id": str(source.payload.get("session_id") or ""),
                },
            ),
        )
    if name == "bypass":
        if not argument:
            return state, (Action("inject_context", {"text": "Symphony bypass requires a task."}),)
        return reduce(state, _derived(state, source, "bypass", {"task": argument}))
    if name == "disable":
        return reduce(state, _derived(state, source, "disable"))
    if name == "reassess":
        return reduce(
            state, _derived(state, source, "reassess", {"reason": argument or "explicit request"})
        )
    if name == "stop":
        kind = "force_stop" if argument == "--force" else "stop_requested"
        return reduce(state, _derived(state, source, kind))
    return state, (Action("inject_context", {"text": f"Unknown Symphony control: {name}. Use {_native_help(provider)}."}),)


def _task_guidance(state: ProjectState, task: str, provider: str, session_id: str = "") -> str:
    """Guidance for substantive work: recover an active run, or open a new one."""
    if _applied_profile(state, provider) == NO_PROFILE:
        return "Symphony has no launchable route in this account's available model roster."
    if state.active_run:
        return _recovery_guidance(state)
    return _assessment_guidance(task, provider, state, session_id)


def _parse_control(prompt: str) -> tuple[str, str] | None:
    # Controls occupy the prompt's first line. Mentions in bug reports, quoted
    # text and fenced examples cannot issue lifecycle commands.
    prompt = prompt.strip()
    if prompt == "enable":
        return "enable", ""
    marker = "$symphony:symphony"
    if prompt == marker or prompt.startswith(marker + " ") or prompt.startswith(marker + "\n"):
        tail = prompt[len(marker):].strip()
        if not tail:
            return "help", ""
        name, _, argument = tail.partition(" ")
        argument = argument.strip()
        if name not in CONTROLS:
            return "start", tail
    elif prompt.startswith("/symphony:"):
        name, _, argument = prompt.removeprefix("/symphony:").partition(" ")
        argument = argument.strip()
    elif prompt.startswith("SYMPHONY_CONTROL:"):
        lines = prompt.splitlines()
        name, _, argument = lines[0].removeprefix("SYMPHONY_CONTROL:").strip().partition(" ")
        argument = argument.strip()
        if not argument and len(lines) == 2 and lines[1].startswith("ARGUMENTS:"):
            argument = lines[1].removeprefix("ARGUMENTS:").strip()
        elif len(lines) > 1:
            return None
    else:
        return None
    # Task controls carry prose; simple controls accept only documented flags.
    flags = {"stop": {"", "--force"}, "agents": {"", "--all"}}
    if name in {"disable", "help", "proceed", "status", "version"} and argument:
        return None
    if name in flags and argument not in flags[name]:
        return None
    return name or "unknown", argument


def _derived(
    state: ProjectState,
    source: Event,
    kind: str,
    payload: dict | None = None,
    suffix: str = "control",
) -> Event:
    """Derive a lifecycle event, keeping repeated controls distinguishable.

    Host payloads carry no timestamp, so two identical prompts in one session
    hash to the same source id. Without this guard the second control is
    silently dropped as a replay, which also swallows the force-stop escape.
    """
    candidate = f"{source.event_id}:{suffix}:{kind}"
    if source.kind == "user_prompt" and any(item.event_id == candidate for item in state.event_history):
        candidate = f"{candidate}:{source.observed_at}"
    return Event(candidate, kind, source.observed_at, payload or {})


def _terminal_result_id(source: Event) -> str:
    """Identify a child result across hook retries without merging new turns."""
    payload = source.payload
    fields = ("provider", "session_id", "agent_id", "subagent_id", "turn_id",
              "prompt_id", "status", "last_assistant_message", "agent_transcript_path",
              "agent_type", "task_name", "role", "model", "model_reasoning_effort")
    stable = {key: payload[key] for key in fields if key in payload}
    stable["observed_role"] = _observed_role(payload)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def _child_turn_token(payload: Mapping[str, object]) -> str:
    for field in ("turn_id", "prompt_id"):
        if payload.get(field):
            return f"{field}:{payload[field]}"
    return ""


def _observe_delegation(state: ProjectState, source: Event) -> tuple[ProjectState, tuple[Action, ...]]:
    identity = source.payload.get("agent_id") or source.payload.get("subagent_id")
    if not identity:
        return state, ()
    opening: tuple[Action, ...] = ()
    if not state.active_run:
        if source.kind != "subagent_started" or _observed_role(source.payload) != "assessor":
            return state, ()
        state, opening = _open_run(
            state, source, str(source.payload.get("task") or source.payload.get("objective") or "")
        )
        if not state.active_run:
            return state, opening
    current = next(
        (item for item in state.active_run.delegations if item.identity == str(identity)),
        None,
    )
    assessment = state.active_run.assessment
    token = _child_turn_token(source.payload)
    if source.kind == "subagent_started":
        if source.event_id in assessment.get("_start_event_ids", ()):
            if not token and current and current.state.lower() not in {"working", "pending", "interrupted"}:
                # A host without invocation IDs cannot distinguish a replay
                # from a fresh byte-identical start. Preserve unfinished work.
                updated = dict(assessment)
                updated["_ambiguous_child_starts"] = (*updated.get("_ambiguous_child_starts", ()),
                                                       str(identity))
                delegations = tuple(replace(item, state="interrupted", updated_at=source.observed_at)
                                    if item.identity == str(identity) else item
                                    for item in state.active_run.delegations)
                run = replace(state.active_run, assessment=updated, delegations=delegations)
                if current.role == "lead":
                    run = replace(run, outcome=None, status="recovering")
                state = replace(state, active_run=run)
            return state, opening
        terminal_turns = assessment.get("_terminal_turns", {})
        if current and token and token in terminal_turns.get(str(identity), ()):
            return state, opening
        updated = dict(assessment)
        updated["_start_event_ids"] = (*assessment.get("_start_event_ids", ()), source.event_id)
        if current and current.state.lower() not in {"working", "pending"}:
            epochs = dict(assessment.get("_terminal_epochs", {}))
            epochs[str(identity)] = epochs.get(str(identity), 0) + 1
            updated["_terminal_epochs"] = epochs
        state = replace(state, active_run=replace(state.active_run, assessment=updated))
    if source.kind == "subagent_stopped":
        seen = assessment.get("_terminal_event_ids", ())
        epoch = assessment.get("_terminal_epochs", {}).get(str(identity), 0)
        result_id = _terminal_result_id(source)
        if not token and epoch:
            result_id = f"{epoch}:{result_id}"
        if (result_id in seen or source.event_id in seen or
                (not seen and any(record.event_id == f"{source.event_id}:delegation:delegation_updated"
                                  for record in state.event_history))):
            return state, opening
        if not token and epoch:
            source = replace(source, event_id=f"{source.event_id}:terminal-epoch:{epoch}")
    pending: Mapping[str, object] = {}
    if current is None:
        state, pending = _consume_pending_delegation(state, source.payload)
    if (
        source.payload.get("provider") == "claude"
        and current is None
        and not pending
        and not _observed_role(source.payload)
    ):
        # Every Symphony spawn on Claude passes PreToolUse with a role marker,
        # so an agent that arrives with neither is the host's or another
        # plugin's. Recording those as workers buried the real delegations
        # under dozens of anonymous entries.
        return state, opening
    if source.kind == "subagent_stopped":
        assessment = dict(state.active_run.assessment)
        assessment["_terminal_event_ids"] = (*assessment.get("_terminal_event_ids", ()),
                                              result_id)
        ambiguous = tuple(item for item in assessment.get("_ambiguous_child_starts", ())
                          if item != str(identity))
        if ambiguous:
            assessment["_ambiguous_child_starts"] = ambiguous
        else:
            assessment.pop("_ambiguous_child_starts", None)
        if token:
            terminal_turns = dict(assessment.get("_terminal_turns", {}))
            terminal_turns[str(identity)] = (*terminal_turns.get(str(identity), ()), token)
            assessment["_terminal_turns"] = terminal_turns
        state = replace(state, active_run=replace(state.active_run, assessment=assessment))
    terminal = source.kind == "subagent_stopped"
    status = str(source.payload.get("status") or ("completed" if terminal else "working"))
    role = str(pending.get("role") or _observed_role(source.payload) or (current.role if current else "worker"))
    if role == "lead" and (source.kind == "subagent_started" or not state.active_run.lead_identity):
        owner_generation = state.active_run.owner_generation
        if (
            state.active_run.status in {"interrupted", "recovering"}
            and state.active_run.lead_identity
            and state.active_run.lead_identity != str(identity)
        ):
            owner_generation += 1
        state, lead_actions = reduce(
            state,
            _derived(
                state,
                source,
                "lead_started",
                {"identity": str(identity), "owner_generation": owner_generation},
                "lead",
            ),
        )
        actions = opening + lead_actions
        if state.active_run:
            deferred = state.active_run.assessment.get("_pending_lead_completion")
            if (isinstance(deferred, Mapping)
                    and (deferred.get("identity") != state.active_run.lead_identity
                         or deferred.get("owner_generation") != state.active_run.owner_generation)):
                assessment = dict(state.active_run.assessment)
                assessment.pop("_pending_lead_completion", None)
                state = replace(state, active_run=replace(state.active_run, assessment=assessment))
        if current is None and state.active_run and state.active_run.lead_identity == str(identity):
            provider = str(source.payload.get("provider") or "codex")
            activation = state.activation.get(provider, {})
            session = str(source.payload.get("session_id") or "")
            profiles = activation.get("session_profiles", ()) if isinstance(activation, Mapping) else ()
            session_profile = next(
                (str(item.get("profile") or "") for item in reversed(profiles)
                 if isinstance(item, Mapping) and item.get("session_id") == session),
                None,
            )
            resolved_profile = None
            if pending.get("role") == "lead" and pending.get("model") and pending.get("effort"):
                selected = f"{pending['model']}/{pending['effort']}"
            elif (session_profile is not None and state.active_run.assessment.get("size")
                  and state.active_run.assessment.get("complexity")):
                recorded = state.active_run.assessment
                route = route_for(Assessment(
                    str(recorded["size"]), str(recorded["complexity"]),
                    str(recorded.get("risk", "normal")),
                ))
                resolved = resolve_tier(route, snapshot_for(provider, session_profile or None))
                selected = f"{resolved['lead_model']}/{resolved['lead_effort']}"
                resolved_profile = session_profile
            elif isinstance(activation, Mapping) and activation.get("session_id") == source.payload.get("session_id"):
                selected = _standing_route(state, provider)
                resolved_profile = _applied_profile(state, provider)
            else:
                selected = "/".join(_required_lead_route(state.active_run.assessment))
            model, _, effort = selected.partition("/")
            if model and effort:
                assessment = dict(state.active_run.assessment)
                expected = {
                    "identity": str(identity), "model": model, "effort": effort,
                }
                if assessment.get("_boost_assessment_pending"):
                    expected["approval_required"] = "A valid boosted assessor result is required before selecting the lead."
                if resolved_profile is not None and assessment.get("size"):
                    route = route_for(Assessment(
                        str(assessment["size"]), str(assessment["complexity"]),
                        str(assessment.get("risk", "normal")),
                    ))
                    drift = _route_drift(assessment, model, effort)
                    if drift and drift["weaker"] and _accepted_route(state, provider, session) != selected:
                        expected["approval_required"] = _drift_block(provider, drift).payload["reason"]
                    clamp = _clamp_actions(state, provider, route, session, resolved_profile)
                    blocked = next((item for item in clamp if item.kind == "block_tool"), None)
                    if blocked:
                        expected["approval_required"] = blocked.payload["reason"]
                assessment["_lead_expected_route"] = expected
                state = replace(state, active_run=replace(state.active_run, assessment=assessment))
    else:
        actions = opening
    update = {
        "identity": str(identity),
        "role": role,
        "objective": str(
            pending.get("objective")
            or source.payload.get("task")
            or source.payload.get("objective")
            or ""
        ),
        "state": status,
    }
    child_metadata = source.payload.get("_symphony_child_metadata", ())
    named_model, named_effort = _agent_label_model_effort(
        str(source.payload.get("agent_type") or source.payload.get("task_name") or ""), role
    )
    model = pending.get("model") or named_model or (
        source.payload.get("model")
        if "model" in child_metadata or not current or not current.requested_tier
        else current.requested_tier
    )
    effort = pending.get("effort") or named_effort or (
        source.payload.get("model_reasoning_effort")
        if "model_reasoning_effort" in child_metadata or not current or not current.requested_effort
        else current.requested_effort
    )
    if role == "assessor":
        # Native child configuration outranks a requested label or queued spawn.
        if "model" in child_metadata:
            model = source.payload.get("model")
        if "model_reasoning_effort" in child_metadata:
            effort = source.payload.get("model_reasoning_effort")
        if not current and state.active_run:
            session = str(source.payload.get("session_id") or "")
            provider = str(source.payload.get("provider") or detect_provider(source.payload))
            requested = _boost_preference(state, provider, session)
            queued = pending.get("assessor_selection")
            remaining = [item.get("assessor_selection") for item in state.active_run.assessment.get("_pending_delegations", ())
                         if isinstance(item, Mapping) and item.get("role") == "assessor" and item.get("assessor_selection")
                         and item["assessor_selection"].get("requested_effort") != "high"]
            if not queued and len(remaining) == 1:
                queued = remaining[0]
            if requested != "off" or queued or remaining:
                recorded = dict(state.active_run.assessment)
                expected = dict(recorded.get("_assessor_expected_routes", {}))
                expected[str(identity)] = queued or ({"model": "unreconciled launch", "effort": "unknown"}
                                                   if len(remaining) > 1 else assessor_selection(_snapshot(state, provider), requested))
                recorded["_assessor_expected_routes"] = expected
                if expected[str(identity)].get("requested_effort") != "high":
                    recorded["_boost_assessment_pending"] = True
                state = replace(state, active_run=replace(state.active_run, assessment=recorded))
    if model:
        update["requested_tier"] = str(model)
    if effort:
        update["requested_effort"] = str(effort)
    state, delegation_actions = reduce(
        state, _derived(state, source, "delegation_updated", update, "delegation")
    )
    actions += delegation_actions
    if role == "assessor" and terminal and state.active_run:
        observed_assessor = next(
            (
                item
                for item in state.active_run.delegations
                if item.identity == str(identity)
            ),
            None,
        )
        expected = state.active_run.assessment.get("_assessor_expected_routes", {}).get(str(identity), {})
        mismatch = bool(expected and observed_assessor and (
            observed_assessor.requested_tier != expected.get("model")
            or observed_assessor.requested_effort != expected.get("effort")
        ))
        if mismatch:
            actions += (Action("inject_context", {"text":
                f"Assessor boost was not observed at {expected['model']}/{expected['effort']}. "
                "Retry with the effective boosted route before selecting a lead."}),)
        elif not observed_assessor or (not expected and observed_assessor.requested_effort not in HIGH_EFFORTS):
            actions += (
                Action(
                    "inject_context",
                    {
                        "text": "The assessor result was not produced at high effort or above. Retry the assessment with an explicitly strong/high assessor before selecting a lead."
                    },
                ),
            )
        else:
            assessment = _assessment_from_marker(
                source.payload.get("last_assistant_message", ""), "SYMPHONY_ASSESSMENT:"
            )
            if assessment is None:
                actions += (
                    Action(
                        "inject_context",
                        {
                            "text": "The assessor finished without a valid SYMPHONY_ASSESSMENT line. Retry the assessment before selecting a lead."
                        },
                    ),
                )
            else:
                recorded = dict(state.active_run.assessment)
                recorded.pop("_boost_assessment_pending", None)
                state = replace(state, active_run=replace(state.active_run, assessment=recorded))
                state, assessment_actions = _accept_assessment(
                    state,
                    source,
                    str(source.payload.get("provider") or "codex"),
                    {},
                    assessment,
                )
                actions += assessment_actions
    if role == "consultant" and terminal and state.active_run:
        decisions = _decision_markers(source.payload.get("last_assistant_message", ""))
        state = _set_invalid_consultant(state, str(identity), not decisions)
        if not decisions:
            actions += (
                Action(
                    "inject_context",
                    {
                        "text": "The consultant result is not actionable until every decision has a valid SYMPHONY_DECISION size/complexity line. Retry that consultant before completing the lead."
                    },
                ),
            )
        elif not state.active_run.assessment.get("_invalid_consultants"):
            pending = state.active_run.assessment.get("_pending_lead_completion")
            if isinstance(pending, Mapping):
                assessment = dict(state.active_run.assessment)
                assessment.pop("_pending_lead_completion", None)
                state = replace(state, active_run=replace(state.active_run, assessment=assessment))
                lead = next(
                    (item for item in state.active_run.delegations
                     if item.identity == pending.get("identity")),
                    None,
                )
                if (lead and lead.state.lower() in {"completed", "done", "success", "succeeded"}
                        and pending.get("identity") == state.active_run.lead_identity
                        and pending.get("owner_generation") == state.active_run.owner_generation):
                    state, completion_actions = reduce(
                        state,
                        _derived(state, source, "lead_completed", dict(pending), "deferred-lead-completion"),
                    )
                    actions += completion_actions
    if role == "lead" and terminal and state.active_run:
        if str(identity) != state.active_run.lead_identity:
            return state, actions
        successful = status.lower() in {"completed", "done", "success", "succeeded"}
        outcome = {"status": status}
        report = str(source.payload.get("last_assistant_message") or "")
        outcome_lines = [line for line in report.splitlines() if line.strip().startswith("SYMPHONY_OUTCOME:")]
        if outcome_lines:
            try:
                reported = json.loads(outcome_lines[-1].strip().removeprefix("SYMPHONY_OUTCOME:").strip())
                reported_status = reported.get("status") if isinstance(reported, Mapping) else None
                if not isinstance(reported_status, str) or not reported_status:
                    raise ValueError("outcome status missing")
                outcome = {"status": reported_status}
                successful = successful and reported_status.lower() in {"completed", "done", "success", "succeeded"}
            except (TypeError, ValueError):
                successful = False
                actions += (Action("inject_context", {"text":
                    "The lead ended with a malformed SYMPHONY_OUTCOME report. Reconcile its result or retry the lead; the run remains recoverable."}),)
        assessment = state.active_run.assessment
        if not successful and str(identity) == state.active_run.lead_identity and assessment.get("_pending_lead_completion"):
            assessment = dict(assessment)
            assessment.pop("_pending_lead_completion", None)
            state = replace(state, active_run=replace(state.active_run, assessment=assessment))
        if successful and not (assessment.get("size") and assessment.get("complexity")):
            actions += (
                Action(
                    "inject_context",
                    {
                        "text": "Lead completion is waiting for an accepted assessment. Reconcile the assessor result before retrying the lead."
                    },
                ),
            )
            return state, actions
        expected = assessment.get("_lead_expected_route", {})
        if (isinstance(expected, Mapping) and expected.get("identity") == str(identity)
                and expected.get("model") and expected.get("effort")):
            required_model = str(expected.get("model") or "")
            required_effort = str(expected.get("effort") or "")
        else:
            required_model, required_effort = _required_lead_route(assessment)
        observed_lead = next(
            (
                item
                for item in state.active_run.delegations
                if item.identity == str(identity)
            ),
            None,
        )
        approval_required = (
            str(expected.get("approval_required") or "")
            if isinstance(expected, Mapping) and expected.get("identity") == str(identity)
            else ""
        )
        if (
            successful
            and state.active_run.lead_identity == str(identity)
            and observed_lead
            and (
                approval_required
                or (required_model and observed_lead.requested_tier != required_model)
                or (required_effort and observed_lead.requested_effort != required_effort)
            )
        ):
            observed = f"{observed_lead.requested_tier}/{observed_lead.requested_effort}"
            mismatch = f"lead route mismatch: expected {required_model}/{required_effort}; observed {observed}"
            if approval_required:
                mismatch += f". {approval_required}"
            updated_assessment = dict(state.active_run.assessment)
            updated_assessment["_lead_route_mismatch"] = mismatch
            updated_assessment.pop("_pending_lead_completion", None)
            state = replace(
                state, active_run=replace(state.active_run, assessment=updated_assessment)
            )
            state, recovery_actions = reduce(
                state,
                _derived(
                    state,
                    source,
                    "lead_failed",
                    {"identity": str(identity)},
                    "lead-route-recovery",
                ),
            )
            actions += recovery_actions
            actions += (
                Action(
                    "inject_context",
                    {
                        "text": f"Lead completion rejected: {mismatch}. Replace or retry the lead with the recorded route."
                    },
                ),
            )
            return state, actions
        if successful and state.active_run.lead_identity == str(identity) and assessment.get("_lead_route_mismatch"):
            updated_assessment = dict(assessment)
            updated_assessment.pop("_lead_route_mismatch", None)
            state = replace(state, active_run=replace(state.active_run, assessment=updated_assessment))
        # 1.5.0 persisted lead failures without the retry marker. Its latest
        # recovery event still distinguishes a failed lead from a later failed
        # worker, whose old lead result must stay invalid.
        run_identities = {item.identity for item in state.active_run.delegations}
        legacy_recovery = next((record for record in reversed(state.event_history)
                                if record.observed_at >= state.active_run.started_at
                                and record.payload.get("identity") in run_identities
                                and (record.kind == "lead_failed" or
                                     (record.kind == "delegation_updated"
                                      and record.payload.get("role") != "lead"
                                      and str(record.payload.get("state") or "").lower() in {
                                          "failed", "interrupted", "cancelled", "canceled", "error", "terminated"
                                      }))), None)
        if (successful and state.active_run.status == "recovering"
                and ((current and current.state == "interrupted")
                     or assessment.get("_retryable_lead") == str(identity)
                     or ("_retryable_lead" not in assessment and legacy_recovery
                         and legacy_recovery.kind == "lead_failed"
                         and legacy_recovery.payload.get("identity") == str(identity)))):
            state, recovery_actions = reduce(
                state, _derived(state, source, "lead_started", {
                    "identity": str(identity), "owner_generation": state.active_run.owner_generation,
                }, "lead-followup"),
            )
            actions += recovery_actions
        invalid_consultants = state.active_run.assessment.get("_invalid_consultants", ())
        if successful and invalid_consultants:
            assessment = dict(state.active_run.assessment)
            assessment["_pending_lead_completion"] = {
                "identity": str(identity),
                "owner_generation": state.active_run.owner_generation,
                "outcome": outcome,
            }
            state = replace(state, active_run=replace(state.active_run, assessment=assessment))
            actions += (
                Action(
                    "inject_context",
                    {
                        "text": "Lead completion is waiting for classified consultant results: "
                        + ", ".join(map(str, invalid_consultants))
                    },
                ),
            )
            return state, actions
        completion_kind = "lead_completed" if successful else "lead_failed"
        # Only the lifecycle fact is recorded. The lead's prose belongs to the
        # host transcript, not to Symphony's durable state.
        state, completion_actions = reduce(
            state,
            _derived(
                state,
                source,
                completion_kind,
                {
                    "identity": str(identity),
                    "owner_generation": state.active_run.owner_generation,
                    "outcome": outcome,
                },
                "lead-completion",
            ),
        )
        actions += completion_actions
    return state, actions


def _open_run(
    state: ProjectState, source: Event, objective: str
) -> tuple[ProjectState, tuple[Action, ...]]:
    """Begin a run at the observed assessor spawn.

    A prompt alone opens nothing, so a session whose root never spawns an
    assessor has no tracked work and cannot be held open. Claude reports the
    spawn before launch and Codex only once the child starts, so whichever
    event arrives first opens the run.
    """
    state, opened = reduce(
        state,
        _derived(
            state,
            source,
            "task_received",
            {
                "task": objective,
                "one_shot": True,
                "session_id": str(source.payload.get("session_id") or ""),
            },
            "assessor-open",
        ),
    )
    # The assessor is already being spawned; asking for one would loop.
    return state, tuple(item for item in opened if item.kind != "request_assessment")


def _observed_role(payload: Mapping[str, object]) -> str:
    for value in payload.values():
        if not isinstance(value, str):
            continue
        marker = _marker_value(value, "SYMPHONY_ROLE:")
        if marker in ROLES:
            return marker
    explicit = str(payload.get("role") or "").strip().lower()
    if explicit in ROLES:
        return explicit
    for key, value in payload.items():
        if not isinstance(value, str):
            continue
        if key not in {"agent_type", "task_name"}:
            continue
        if value.strip().lower() in ROLES:
            return value.strip().lower()
        match = re.search(r"(?:^|[_:/-])symphony[_-](assessor|consultant|lead|worker)(?:[_:/-]|$)", value.lower())
        if match:
            return match.group(1)
    return ""


def _prepare_delegation(
    state: ProjectState, source: Event, provider: str
) -> tuple[ProjectState, tuple[Action, ...]]:
    tool_name = str(source.payload.get("tool_name") or source.payload.get("tool") or "").lower()
    if "agent" not in tool_name:
        return state, ()
    values = source.payload.get("tool_input") or source.payload.get("input") or {}
    role = _marker_value(values, "SYMPHONY_ROLE:")
    if role not in ROLES:
        if not state.active_run and not state.enabled:
            # Nothing is being governed, so this spawn is not Symphony's to judge.
            return state, ()
        return state, (_block_tool("Add exactly one SYMPHONY_ROLE: assessor|lead|worker|consultant line, then retry the spawn."),)
    try:
        profiles_for(provider)
    except (OSError, ValueError, KeyError, TypeError):
        return state, (_block_tool("Symphony capability profiles are invalid; repair the shipped profiles before spawning."),)
    if _applied_profile(state, provider) == NO_PROFILE:
        return state, (_block_tool("Symphony has no launchable route in this account's available model roster."),)
    if not state.active_run and role != "assessor":
        return state, (
            _block_tool(
                "Spawn the Symphony assessor first; a run begins when the assessor starts."
            ),
        )
    model, effort = _requested_model_effort(values, provider, role)
    if not model or not effort:
        if provider == "claude":
            reason = (
                f"Use a Symphony agent type named symphony-{role}-<model>-<effort>; "
                "generic Claude agents cannot pin effort."
            )
        else:
            reason = f"Spawn the Symphony {role} with an explicit model and effort, then retry."
        return state, (_block_tool(reason),)
    if provider == "claude":
        override = str(values.get("model") or "").strip() if isinstance(values, dict) else ""
        if override and override != model:
            return state, (
                _block_tool(
                    f"Remove the Claude model override or use `{model}` so the packaged Symphony role remains observable."
                ),
            )
    if role == "assessor" and effort not in HIGH_EFFORTS:
        return state, (_block_tool("The Symphony assessor requires a strong model at high effort or above."),)
    if role == "assessor":
        session_id = str(source.payload.get("session_id") or "")
        requested = _boost_preference(state, provider, session_id)
        snapshot = _snapshot(state, provider)
        selected = assessor_selection(snapshot, requested)
        if model in snapshot.supported_efforts and effort not in snapshot.supported_efforts[model]:
            return state, (_block_tool(f"Assessor effort {effort} is unsupported for {model} in this account profile."),)
        if requested != "off" and (model, effort) != (selected["model"], selected["effort"]):
            return state, (_block_tool(
                f"Assessor boost route mismatch: requested {selected['requested_model']}/{selected['requested_effort']}; "
                f"effective {selected['model']}/{selected['effort']}. Spawn that assessor with explicit model and effort."
            ),)

    actions: tuple[Action, ...] = ()
    objective = _tool_objective(values)
    if role == "assessor" and not state.active_run:
        state, actions = _open_run(state, source, objective)
    if role == "lead":
        if state.active_run.assessment.get("_boost_assessment_pending"):
            return state, (_block_tool("A valid boosted assessor result is required before selecting the lead."),)
        assessment = _assessment_from_marker(values, "SYMPHONY_ROUTE:")
        if assessment is None:
            return state, (_block_tool("Add a valid SYMPHONY_ROUTE JSON line to the lead packet, then retry."),)
        recorded = state.active_run.assessment
        if recorded.get("size") and recorded.get("complexity"):
            if (
                assessment.size != recorded.get("size")
                or assessment.complexity != recorded.get("complexity")
            ):
                return state, (
                    _block_tool("Use the accepted Symphony size/complexity route for this lead."),
                )
            route = route_for(
                Assessment(
                    str(recorded["size"]),
                    str(recorded["complexity"]),
                    str(recorded.get("risk", "normal")),
                    str(recorded.get("rationale", "")),
                    str(recorded.get("topology", "")),
                )
            )
        else:
            route = route_for(assessment)
        spawn_session = str(source.payload.get("session_id") or "")
        snapshot = _snapshot(state, provider)
        resolved = resolve_tier(route, snapshot)
        required_model = str(resolved["lead_model"])
        required_effort = str(resolved["lead_effort"])
        drift = _route_drift(recorded, required_model, required_effort)
        if drift and drift["weaker"]:
            if _accepted_route(state, provider, spawn_session) != f"{required_model}/{required_effort}":
                return state, (_drift_block(provider, drift),)
        elif drift:
            actions += (_drift_notice(drift),)
        clamp = _clamp_actions(state, provider, route, spawn_session)
        if any(item.kind == "block_tool" for item in clamp):
            return state, clamp
        actions += clamp
        if model != required_model or effort != required_effort:
            return state, (
                _block_tool(
                    f"Lead route mismatch: expected {required_model}/{required_effort}; "
                    f"observed {model}/{effort}. Spawn the selected lead with the expected route, then retry."
                ),
            )
        if not (recorded.get("size") and recorded.get("complexity")):
            state, accepted = _accept_assessment(state, source, provider, values, assessment)
            actions += accepted
    elif role in {"worker", "consultant"} and not state.active_run.lead_identity:
        return state, (_block_tool(f"Register the selected lead before spawning a Symphony {role}."),)
    if role == "consultant" and not _decision_markers(values):
        return state, (_block_tool("Add SYMPHONY_DECISION JSON with decision-local size and complexity, then retry."),)

    launch_selection = None
    if role == "assessor":
        # Freeze the accepted launch even when boost is off; a later control
        # changes subsequent assessors, not an already queued native spawn.
        launch_selection = dict(selected, model=model, effort=effort)
    state = _queue_pending_delegation(state, role, objective, model, effort, launch_selection)
    return state, actions


def _version_text(environ: Mapping[str, str]) -> str:
    """Which build is executing, and whether a newer one is waiting.

    Installed and running are different questions. The hook command registered
    at session start already has the plugin root expanded into it, so a session
    keeps loading the build it started with even after an update writes a newer
    one alongside.
    """
    root = environ.get("SYMPHONY_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    text = (
        f"Symphony {PLUGIN_VERSION} is running this hook, hook schema "
        f"{HOOK_SCHEMA_VERSION}, loaded from {root}."
    )
    retained = environ.get("SYMPHONY_PINNED_RUNTIME")
    if retained:
        text += f" Executing its verified retained runtime at {retained}."
    waiting = _newer_build_on_disk(root)
    if waiting:
        text += (
            f" Version {waiting} is installed alongside it and will load on restart."
            " Until then this session keeps the build it started with."
        )
    return text


def _version_parts(name: str) -> tuple[int, ...] | None:
    """A three-part version read as numbers, so 0.10.1 outranks 0.9.0."""
    pieces = name.split(".")
    if len(pieces) != 3:
        return None
    try:
        return tuple(int(piece) for piece in pieces)
    except ValueError:
        return None


def _newer_build_on_disk(root: str) -> str:
    """A build installed beside the running one, which a restart would load.

    Both hosts cache a plugin under a version-named directory, so the sibling
    names are the installed versions. Any other layout reports nothing, which
    is why a git checkout stays quiet.
    """
    running = _version_parts(Path(root).name)
    if running is None:
        return ""
    try:
        siblings = [entry.name for entry in Path(root).parent.iterdir() if entry.is_dir()]
    except OSError:
        return ""
    newer = [
        (parsed, name)
        for name in siblings
        if (parsed := _version_parts(name)) is not None and parsed > running
    ]
    return max(newer)[1] if newer else ""


def _applied_profile(state: ProjectState, provider: str) -> str:
    activation = state.activation.get(provider, {})
    profile = activation.get("profile") if isinstance(activation, Mapping) else None
    return str(profile) if profile else ""


def _accepted_profile(state: ProjectState, provider: str, session_id: str) -> str:
    return _carried_acceptance(state, provider, session_id, "profile")


def _accepted_route(state: ProjectState, provider: str, session_id: str) -> str:
    return _carried_acceptance(state, provider, session_id, "route")


def _snapshot(state: ProjectState, provider: str):
    """The capability snapshot for the profile this session probed."""
    return snapshot_for(provider, _applied_profile(state, provider) or None)


def _clamp_actions(
    state: ProjectState, provider: str, route, session_id: str = "", profile_id: str | None = None
) -> tuple[Action, ...] | None:
    """Gate a tier clamp, disclose an effort clamp, stay silent otherwise.

    A weaker model doing the work is the degradation that must not pass
    unnoticed, so it waits for the user. A reduced effort on the same model is
    the dimension the matrix already trades away under risk, so it is announced
    and the run continues.
    """
    profile = _applied_profile(state, provider) if profile_id is None else profile_id
    clamp = clamp_against_best(provider, route, profile or None)
    if clamp["tier_clamped"]:
        if _accepted_profile(state, provider, session_id) == profile and profile:
            return ()
        control = _control_name("proceed", provider)
        reason = (
            f"Your plan routes this work to {clamp['actual_model']} instead of the "
            f"matrix-selected {clamp['intended_model']}"
            + (" and no entitlement could be read" if not profile else "")
            + f". Run `{control}` to accept the weaker route for this session, or "
            "upgrade the plan and start a new session."
        )
        return (_block_tool(reason),)
    if clamp["effort_clamped"]:
        return (
            Action(
                "inject_context",
                {
                    "text": f"Symphony reduced effort to {clamp['actual_effort']} from "
                    f"{clamp['intended_effort']}: {clamp['actual_model']} does not offer it."
                },
            ),
        )
    return ()


def _required_lead_route(recorded: Mapping[str, object]) -> tuple[str, str]:
    """The model and effort this assessment resolved to when it was accepted.

    No longer an enforcement pin. A run is held to its tier, so that a model
    disappearing from the map cannot leave the run naming a spawn nobody can
    make. These two values survive only as the baseline that says whether
    re-resolving the same tier has weakened the route.
    """
    route = recorded.get("route", {})
    if not isinstance(route, Mapping):
        return "", ""
    return str(route.get("lead_model") or ""), str(route.get("lead_effort") or "")


def _route_drift(
    recorded: Mapping[str, object],
    model: str,
    effort: str,
) -> dict[str, str] | None:
    """How a standing assessment's route has moved since it was accepted.

    Only one move needs the user's agreement: the work was sized once, and the
    route that sizing chose would now run with less model capability or effort.
    A reassessment writes its own baseline.
    """
    stored_model, stored_effort = _required_lead_route(recorded)
    if not stored_model or (stored_model == model and stored_effort == effort):
        return None
    gone = model_is_weaker(model, stored_model) or (
        effort in EFFORTS and stored_effort in EFFORTS
        and EFFORTS.index(effort) < EFFORTS.index(stored_effort)
    )
    return {
        "stored_model": stored_model,
        "stored_effort": stored_effort,
        "model": model,
        "effort": effort,
        "weaker": "yes" if gone else "",
    }


def _drift_block(provider: str, drift: Mapping[str, str]) -> Action:
    control = _control_name("proceed", provider)
    return _block_tool(
        f"The route this assessment accepted was {drift['stored_model']} at "
        f"{drift['stored_effort']} effort; the same work would now run on "
        f"{drift['model']} at {drift['effort']} effort. Run `{control}` to "
        "accept the weaker route, or reassess so the sizing matches what you can buy."
    )


def _drift_notice(drift: Mapping[str, str]) -> Action:
    return Action(
        "inject_context",
        {
            "text": f"Symphony re-resolved this route to {drift['model']} at "
            f"{drift['effort']} effort; the assessment accepted {drift['stored_model']} at "
            f"{drift['stored_effort']}. The tier the matrix chose is unchanged."
        },
    )


def _standing_route(state: ProjectState, provider: str) -> str:
    """What the standing assessment resolves to right now, if one stands."""
    run = state.active_run
    recorded = run.assessment if run else None
    if not isinstance(recorded, Mapping) or not recorded.get("size"):
        return ""
    route = route_for(
        Assessment(
            str(recorded["size"]),
            str(recorded["complexity"]),
            str(recorded.get("risk", "normal")),
        )
    )
    resolved = resolve_tier(route, _snapshot(state, provider))
    return f"{resolved['lead_model']}/{resolved['lead_effort']}"


def _accept_assessment(
    state: ProjectState,
    source: Event,
    provider: str,
    values: object,
    assessment: Assessment,
) -> tuple[ProjectState, tuple[Action, ...]]:
    route = route_for(assessment)
    route_data = {
        "lead_tier": route.lead_tier,
        "lead_effort": route.lead_effort,
        "execution": route.execution,
        "consultation": route.consultation,
        "independent_review": route.independent_review,
    }
    route_data.update(resolve_tier(route, _snapshot(state, provider)))
    # Which entitlement priced this route, so a later move can be read as a
    # downgrade or an upgrade rather than merely a difference.
    route_data["profile"] = _applied_profile(state, provider)
    accepted = {
        "size": assessment.size,
        "complexity": assessment.complexity,
        "risk": assessment.risk,
        "rationale": assessment.rationale,
        "topology": assessment.topology or route.execution,
        "route": route_data,
    }
    return reduce(state, _derived(state, source, "assessment_accepted", accepted, "assessment"))


def _assessment_from_marker(values: object, marker: str) -> Assessment | None:
    line = _marker_value(values, marker)
    if not line:
        return None
    try:
        raw = json.loads(line)
        assessment = Assessment(
            str(raw["size"]),
            str(raw["complexity"]),
            str(raw.get("risk", "normal")),
            str(raw.get("rationale", "")),
            str(raw.get("topology", "")),
        )
        route_for(assessment)
        return assessment
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _decision_markers(values: object) -> tuple[Mapping[str, object], ...]:
    texts = [str(values)]
    if isinstance(values, dict):
        texts = [str(value) for value in values.values() if isinstance(value, str)]
    marker = "SYMPHONY_DECISION:"
    lines = [
        item.strip()[len(marker) :].strip()
        for text in texts
        for item in text.splitlines()
        if item.strip().startswith(marker)
    ]
    decisions = []
    for line in lines:
        try:
            raw = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return ()
        if not isinstance(raw, dict):
            return ()
        if raw.get("size") not in {"small", "medium", "large"}:
            return ()
        if raw.get("complexity") not in {"simple", "mixed", "complex"}:
            return ()
        decisions.append(raw)
    return tuple(decisions)


def _marker_value(values: object, marker: str) -> str:
    texts = [str(values)]
    if isinstance(values, dict):
        texts = [str(value) for value in values.values() if isinstance(value, str)]
    return next(
        (
            item.strip()[len(marker) :].strip()
            for text in texts
            for item in text.splitlines()
            if item.strip().startswith(marker)
        ),
        "",
    )


def _requested_model_effort(values: object, provider: str, role: str) -> tuple[str, str]:
    if not isinstance(values, dict):
        return "", ""
    if provider == "claude":
        return _agent_label_model_effort(str(values.get("subagent_type") or ""), role)
    model = str(values.get("model") or "").strip()
    effort = str(
        values.get("reasoning_effort")
        or values.get("model_reasoning_effort")
        or values.get("effort")
        or ""
    ).strip()
    return model, effort


def _agent_label_model_effort(label: str, role: str) -> tuple[str, str]:
    agent_type = label.split(":")[-1]
    prefix = f"symphony-{role}-"
    if not agent_type.startswith(prefix):
        return "", ""
    setting = agent_type[len(prefix) :]
    model, separator, effort = setting.rpartition("-")
    return (
        (model, effort)
        if separator and effort in {"low", "medium", "high", "xhigh", "max", "ultra"}
        else ("", "")
    )


def _tool_objective(values: object) -> str:
    if not isinstance(values, dict):
        return ""
    text = str(values.get("message") or values.get("prompt") or values.get("task") or "")
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("SYMPHONY_")
    ).strip()


def _block_tool(reason: str) -> Action:
    return Action("block_tool", {"reason": reason})


def _discard_failed_spawn(state: ProjectState, source: Event, provider: str) -> ProjectState:
    if not state.active_run or "agent" not in str(source.payload.get("tool_name") or "").lower():
        return state
    response = source.payload.get("tool_response") or source.payload.get("tool_result") or {}
    failed = source.kind == "post_tool_failed" or (isinstance(response, Mapping) and (
        response.get("is_error") is True or response.get("status") in {"failed", "error", "rejected"}
    ))
    if not failed:
        return state
    values = source.payload.get("tool_input") or source.payload.get("input") or {}
    role = _marker_value(values, "SYMPHONY_ROLE:")
    model, effort = _requested_model_effort(values, provider, role)
    state, _ = reduce(state, Event(source.event_id + ":delegation_launch_failed", "delegation_launch_failed", source.observed_at, {
        "role": role, "requested_tier": model, "requested_effort": effort,
    }))
    return state


def _queue_pending_delegation(
    state: ProjectState,
    role: str,
    objective: str,
    model: str,
    effort: str,
    assessor_route: Mapping | None = None,
) -> ProjectState:
    if not state.active_run:
        return state
    assessment = dict(state.active_run.assessment)
    pending = list(assessment.get("_pending_delegations", ()))
    pending.append(
        {
            "role": role,
            "objective": objective,
            "model": model,
            "effort": effort,
        }
    )
    if assessor_route:
        pending[-1]["assessor_selection"] = dict(assessor_route)
        if assessor_route.get("requested_effort") != "high":
            assessment["_boost_assessment_pending"] = True
    # ponytail: bound unmatched host events; add ID correlation only if a provider exposes it.
    assessment["_pending_delegations"] = pending[-32:]
    return replace(state, active_run=replace(state.active_run, assessment=assessment))


def _consume_pending_delegation(
    state: ProjectState,
    payload: Mapping[str, object],
) -> tuple[ProjectState, Mapping[str, object]]:
    if not state.active_run:
        return state, {}
    assessment = dict(state.active_run.assessment)
    pending = list(assessment.get("_pending_delegations", ()))
    if not pending:
        return state, {}
    observed_role = _observed_role(payload)
    observed_model, observed_effort = _agent_label_model_effort(
        str(payload.get("agent_type") or ""), observed_role
    )
    matching = [
        index
        for index, item in enumerate(pending)
        if isinstance(item, Mapping)
        and item.get("role") == observed_role
        and (
            not observed_model
            or (
                item.get("model") == observed_model
                and item.get("effort") == observed_effort
            )
        )
    ]
    if matching:
        item = pending.pop(matching[0])
    elif len(pending) == 1 and not observed_role and payload.get("provider") != "claude":
        # Claude always names the packaged agent type, so an unnamed start
        # there is somebody else's agent and must not claim this spawn.
        item = pending.pop(0)
    else:
        return state, {}
    if pending:
        assessment["_pending_delegations"] = pending
    else:
        assessment.pop("_pending_delegations", None)
    state = replace(state, active_run=replace(state.active_run, assessment=assessment))
    return state, item if isinstance(item, Mapping) else {}


def _set_invalid_consultant(
    state: ProjectState, identity: str, invalid: bool
) -> ProjectState:
    run = state.active_run
    if not run:
        return state
    assessment = dict(run.assessment)
    identities = set(map(str, assessment.get("_invalid_consultants", ())))
    identities.discard(identity)
    objective = next(
        (item.objective for item in run.delegations if item.identity == identity), ""
    )
    if objective:
        # A retry answers the same question under a new agent id. Without this
        # the first attempt's marker blocks completion for the rest of the run.
        identities -= {
            item.identity
            for item in run.delegations
            if item.objective == objective and item.identity != identity
        }
    if invalid:
        identities.add(identity)
    if identities:
        assessment["_invalid_consultants"] = sorted(identities)
    else:
        assessment.pop("_invalid_consultants", None)
    return replace(state, active_run=replace(run, assessment=assessment))


def _defer_parent_actions(
    state: ProjectState, actions: tuple[Action, ...]
) -> ProjectState:
    if not state.active_run:
        return state
    visible = [
        {"kind": action.kind, "payload": dict(action.payload)}
        for action in actions
        if action.kind == "inject_context"
    ]
    if not visible:
        return state
    assessment = dict(state.active_run.assessment)
    pending = list(assessment.get("_pending_parent_actions", ()))
    assessment["_pending_parent_actions"] = (pending + visible)[-16:]
    return replace(state, active_run=replace(state.active_run, assessment=assessment))


def _consume_parent_actions(
    state: ProjectState,
) -> tuple[ProjectState, tuple[Action, ...]]:
    if not state.active_run:
        return state, ()
    assessment = dict(state.active_run.assessment)
    pending = assessment.pop("_pending_parent_actions", ())
    actions = tuple(
        Action(str(item.get("kind") or ""), dict(item.get("payload") or {}))
        for item in pending
        if isinstance(item, Mapping) and item.get("kind")
    )
    return (
        replace(state, active_run=replace(state.active_run, assessment=assessment)),
        actions,
    )


def _stop_block_text(
    payload: Mapping[str, object], provider: str, scope: str = "this project"
) -> str:
    active = ", ".join(map(str, payload.get("active", ())))
    reason = payload.get("reason") or f"active work remains: {active}"
    if reason == "lead_outcome_missing":
        reason = "the tracked lead has no reconciled outcome; inspect its ended or interrupted result and recover or retry it"
    elif reason == "lead_not_started":
        reason = "assessment work has ended; reconcile its result and launch the selected lead"
    force = "/symphony:stop --force" if provider == "claude" else "$symphony:symphony stop --force"
    return (
        f"Symphony stop is blocked for {scope}: {reason}. Let the tracked agents finish, "
        f"return to this run on the next turn, or run `{force}` to end the run and "
        "record what was not reconciled. Keep protocol markers out of the final answer, "
        "and report completion only after durable status confirms it."
    )


def _render_actions(
    actions: tuple[Action, ...],
    state: ProjectState,
    provider: str,
    source_kind: str = "",
    scope: str = "this project",
) -> tuple[Action, ...]:
    # A stop control arrives as a prompt, where blocking would reject the user's
    # own message; only a real Stop event may answer with a block decision.
    prompt_originated = source_kind == "user_prompt"
    rendered: list[Action] = []
    for action in actions:
        if action.kind in {"inject_context", "block_tool"}:
            rendered.append(action)
        elif action.kind == "block_stop":
            text = _stop_block_text(action.payload, provider, scope)
            rendered.append(
                Action("inject_context", {"text": text})
                if prompt_originated
                else Action("block_stop", {"reason": text})
            )
        elif action.kind == "permit_stop" and prompt_originated:
            rendered.append(
                Action(
                    "inject_context",
                    {"text": f"Symphony has no active work in {scope} to stop."},
                )
            )
        elif action.kind == "run_abandoned":
            # Stop schemas accept only a decision object; abandonment is
            # durable state, not user-facing Stop context.
            continue
        elif action.kind == "route_acceptance_recorded":
            rendered.append(
                Action(
                    "inject_context",
                    {"text": "Symphony accepted the clamped route for this session."},
                )
            )
        elif action.kind == "project_enabled":
            rendered.append(Action("inject_context", {"text": "Symphony is enabled for this project; hooks are guarded."}))
        elif action.kind == "project_disabled":
            rendered.append(Action("inject_context", {"text": "Symphony is disabled for future tasks in this project."}))
        elif action.kind == "request_assessment":
            task = state.active_run.task if state.active_run else "the task"
            rendered.append(Action("inject_context", {"text": _assessment_guidance(task, provider, state)}))
        elif action.kind == "execute_bypass":
            rendered.append(
                Action(
                    "inject_context",
                    {"text": f"Execute outside Symphony without changing project state: {action.payload.get('task', '')}"},
                )
            )
        elif action.kind == "run_already_active":
            rendered.append(Action("inject_context", {"text": _recovery_guidance(state)}))
        elif action.kind == "preserve_recovery_context":
            rendered.append(Action("inject_context", {"text": "Symphony recorded the interruption for safe reconciliation on resume."}))
        elif action.kind == "stop_delegations":
            identities = ", ".join(map(str, action.payload.get("active", ())))
            rendered.append(Action("inject_context", {"text": f"Stop these tracked Symphony agents and verify their host status: {identities}."}))
        elif action.kind == "replace_lead":
            rendered.append(Action("inject_context", {"text": "The observed lead is unavailable. Spawn one safe replacement at the recorded owner generation."}))
        elif action.kind == "route_run":
            route = state.active_run.assessment.get("route", {}) if state.active_run else {}
            model, effort = _required_lead_route(state.active_run.assessment) if state.active_run else ("", "")
            profile = route.get("profile", "") if isinstance(route, Mapping) else ""
            agent = f" as `symphony:symphony-lead-{model}-{effort}`" if provider == "claude" and model else ""
            rendered.append(Action("inject_context", {"text":
                f"Symphony accepted the assessed route. Selected {provider} lead"
                f" ({profile} profile): {model}/{effort}. Spawn only this model and effort{agent} "
                "with the accepted SYMPHONY_ROUTE marker; keep the root thin."}))
        elif action.kind == "reject_lead_replacement":
            lead = state.active_run.lead_identity if state.active_run else "the registered lead"
            rendered.append(Action("inject_context", {"text":
                f"Symphony refused to register {action.payload.get('identity')} as lead: this run already "
                f"has one ({lead}). Stop the extra agent and let the registered lead finish. Symphony is "
                "not tracking the extra agent's work, so anything it does will go unreconciled."}))
        elif action.kind == "ignore_stale_owner":
            rendered.append(Action("inject_context", {"text":
                f"Symphony ignored a lifecycle report from {action.payload.get('identity')}, which is not "
                "the registered lead of this run. The run is unchanged."}))
        elif action.kind == "wait_for_delegations":
            identities = ", ".join(map(str, action.payload.get("active", ())))
            reason = f"active agents: {identities}" if identities else "pending launches or unreconciled results"
            rendered.append(Action("inject_context", {"text":
                f"The lead reported completion while Symphony still tracks {reason}. "
                "Reconcile this work before completing the run."}))
        elif action.kind == "block_completion":
            text = (
                "Symphony invalidated the earlier lead outcome after later work failed. Register a recovered lead and integrate the result before reporting completion."
                if action.payload.get("reason") == "lead_recovery_required"
                else "Symphony refused this completion because the lead returned no valid outcome. Reconcile the outcome before completing."
            )
            rendered.append(Action("inject_context", {"text": text}))
        elif action.kind not in INTERNAL_ACTIONS:
            # A decision the reducer made must never die on the way out. Seven
            # of them did, which is how two leads ran at once with nobody told.
            rendered.append(Action("inject_context", {"text":
                f"Symphony made a lifecycle decision it has no message for ({action.kind}). "
                "This is a plugin defect; no action is required of you."}))
    return tuple(rendered)


def _claude_cells(snapshot, role: str) -> list[str]:
    """The packaged agent type the matrix selects for each cell."""
    cells = []
    for size in ("small", "medium", "large"):
        for complexity in ("simple", "mixed", "complex"):
            normal, high = (
                resolve_tier(route_for(Assessment(size, complexity, risk)), snapshot)
                for risk in ("normal", "high")
            )
            cell = f"{size}/{complexity} `symphony:symphony-{role}-{normal['lead_model']}-{normal['lead_effort']}`"
            if (high["lead_model"], high["lead_effort"]) != (normal["lead_model"], normal["lead_effort"]):
                cell += f" (high risk: `symphony:symphony-{role}-{high['lead_model']}-{high['lead_effort']}`)"
            cells.append(cell)
    return cells


def _claude_lead_guidance(state: ProjectState) -> str:
    """What a Claude lead needs at start and cannot derive: its children's types."""
    snapshot = _snapshot(state, "claude")
    return (
        "Symphony worker agent types by the packet's own size/complexity: "
        + "; ".join(_claude_cells(snapshot, "worker"))
        + f". Consultants use `symphony:symphony-consultant-{snapshot.tiers['strongest']}-high`."
    )


def _claude_guidance(state: ProjectState | None, session_id: str = "") -> str:
    """Name the exact agent types, how to wait, and where user-facing skills run.

    The root is the cheapest model in the session. Left to derive a packaged
    agent type from the matrix it guessed, and without being told how Claude
    delivers background results it busy-polled the agent's output file.
    """
    snapshot = _snapshot(state, "claude") if state else snapshot_for("claude")
    requested = _boost_preference(state, "claude", session_id) if state else "off"
    assessor = assessor_selection(snapshot, requested)
    cells = _claude_cells(snapshot, "lead")
    access = ""
    if state and state.activation.get("claude", {}).get("claude_probe_attempted"):
        profile = _applied_profile(state, "claude")
        access = ("Claude Code model check selected the Opus profile. " if profile == "opus" else
                  "Claude Code could not verify both Sonnet and Opus; this task uses the Sonnet fallback. ")
    return (
        access +
        f"On Claude Code, spawn the assessor as `symphony:symphony-assessor-{assessor['model']}-{assessor['effort']}` "
        "and the lead by its assessed cell: " + "; ".join(cells) + ". "
        "Agents run in the background: after a spawn, end your turn and Claude Code wakes you with the "
        "agent's result. Never wait by polling with Bash, sleep, Monitor, or by reading the agent's output "
        "file, and never repeat an assessment that is still running. "
        "User-facing steps stay at the root, because agents cannot ask the user anything: when the request "
        "needs requirements or design clarification and a brainstorming skill is available (for example "
        "`superpowers:brainstorming`), run it with the user before the assessor and relay the agreed design "
        "in full; after the lead returns, run any user-facing finishing skill (for example "
        "`superpowers:finishing-a-development-branch`) at the root. "
    )


def _assessment_guidance(task: str, provider: str = "", state: ProjectState | None = None, session_id: str = "") -> str:
    if state is not None and provider:
        session_id = session_id or str(state.activation.get(provider, {}).get("session_id") or "")
    claude = _claude_guidance(state, session_id) if provider == "claude" else ""
    codex = (
        "On Codex, use `fork_turns=\"none\"` for assessor and lead, name them "
        "`symphony_<role>_<model>_<effort>`, and require the assessor's final response to contain one exact "
        "`SYMPHONY_ASSESSMENT: {\"size\":\"...\",\"complexity\":\"...\",\"risk\":\"...\","
        "\"rationale\":\"...\",\"topology\":\"...\"}` line. "
        if provider == "codex"
        else ""
    )
    boost = ""
    if state is not None and provider:
        session_id = session_id or str(state.activation.get(provider, {}).get("session_id") or "")
        requested = _boost_preference(state, provider, session_id)
        selected = assessor_selection(_snapshot(state, provider), requested)
        model, effort = selected["model"], selected["effort"]
        spawn = (f"Use agent type `symphony:symphony-assessor-{model}-{effort}`. " if provider == "claude"
                 else f"Pass `model=\"{model}\"`, `reasoning_effort=\"{effort}\"`, and `fork_turns=\"none\"`. ")
        boost = _boost_status(state, provider, session_id) + " " + spawn
    return (
        "Symphony owns execution topology. Keep the root thin. Spawn the selected assessor with explicit model "
        "and effort and put `SYMPHONY_ROLE: assessor` on its own line. Then select the lead mechanically from the "
        "nine-cell matrix; the assessor must not become the lead. Spawn the lead with explicit model and effort, "
        "put `SYMPHONY_ROLE: lead` on its own line, and include one exact line in its task: "
        "SYMPHONY_ROUTE: {\"size\":\"small|medium|large\",\"complexity\":\"simple|mixed|complex\","
        "\"risk\":\"normal|high\",\"rationale\":\"...\",\"topology\":\"...\"}. "
        "Every later worker or consultant spawn needs its matching SYMPHONY_ROLE line and explicit model/effort; "
        "consultants also need SYMPHONY_DECISION JSON with decision-local size and complexity. "
        "Relay the task in full: the lead cannot see this conversation, so if the request has several parts, "
        "every part goes in the packet and the acceptance check covers all of them. "
        f"{boost}{codex}{claude}Task: {task}"
    )


def _boost_preference(state: ProjectState, provider: str, session_id: str) -> str:
    requested = str(state.configuration.get("assessor_boosts", {}).get(f"{provider}:{session_id}", "off"))
    levels = {"xhigh", "max", "ultra"} if provider == "codex" else {"xhigh", "max"}
    return requested if requested in levels else "off"


def _boost_status(state: ProjectState, provider: str, session_id: str) -> str:
    requested = _boost_preference(state, provider, session_id)
    record = _session_profile_record(state, provider, session_id)
    snapshot = snapshot_for(provider, str(record.get("profile") or "") or None)
    selected = assessor_selection(snapshot, requested)
    text = (f"Assessor boost: {requested}; requested {selected['requested_model'] or 'unavailable'}/{selected['requested_effort']}; "
            f"effective {selected['model'] or 'unavailable'}/{selected['effort'] or 'unsupported'}. "
            f"Scope: current project, {provider} session {session_id or 'unverified'}, assessors only; applies to the next spawn.")
    if selected['effort'] != selected['requested_effort']:
        text += " Requested effort is unsupported by the current account profile; it cannot launch."
    return text


def _governance(state: ProjectState) -> str:
    """Whether the next prompt will be governed, which is what a lead label answers.

    A run started with `start` governs one task. A user who believes the
    project is enabled cannot tell the difference from the run itself, and
    every prompt after the first quietly runs at the session's own model.
    """
    return "enabled" if state.enabled else "transactional"


def _recovery_guidance(state: ProjectState) -> str:
    run = state.active_run
    if not run:
        return "Symphony has no active run."
    records = ", ".join(f"{item.role} {item.identity} ({item.state})" for item in run.delegations)
    pending = run.assessment.get("_pending_delegations", ())
    awaiting = ", ".join(str(item.get("role") or "agent") for item in pending if isinstance(item, Mapping))
    lead = f" Lead {run.lead_identity} [{_governance(state)}]." if run.lead_identity else " Lead not yet observed."
    return (
        f"Symphony run {run.run_id} remains {run.status}.{lead} "
        f"Observed agents: {records or 'none'}. "
        + (f"Awaiting host launch confirmation: {awaiting}. " if awaiting else "")
        + "Reconcile returned or interrupted results, preserve ownership, and continue unfinished work. "
        "An ended host turn does not complete this run; report completion only when durable status confirms it."
    )


def compact_delegations(state: ProjectState, limit: int = 5) -> tuple[Delegation, ...]:
    if limit <= 0:
        return ()
    delegations = state.active_run.delegations if state.active_run else ()
    rank = {"failed": 0, "terminated": 0, "working": 1, "running": 1, "active": 1, "waiting": 2, "pending": 2}
    ordered = sorted(delegations, key=lambda item: (rank.get(item.state, 3), item.updated_at), reverse=False)
    active = [item for item in ordered if rank.get(item.state, 3) < 3]
    completed = sorted((item for item in ordered if rank.get(item.state, 3) == 3), key=lambda item: item.updated_at, reverse=True)
    return tuple((active + completed)[:limit])


def format_delegation(item: Delegation) -> str:
    label = item.role
    if item.requested_tier and item.requested_effort:
        label += f" [{item.requested_tier}/{item.requested_effort}]"
    elif item.requested_tier:
        label += f" [{item.requested_tier}]"
    elif item.requested_effort:
        label += f" [effort={item.requested_effort}]"
    result = f"- {item.state}: {label} — {item.identity}"
    if item.objective:
        result += f" — {item.objective}"
    return result


def _status(
    state: ProjectState,
    include_history: bool,
    provider: str = "",
    session_id: str = "",
    scope: str = "this project",
) -> str:
    activation = state.activation.get(provider, {}) if provider else next(iter(state.activation.values()), {})
    activation_session = str(activation.get("session_id") or "") if isinstance(activation, Mapping) else ""
    guarded = bool(activation.get("state") == "guarded") if isinstance(activation, Mapping) else False
    if session_id and activation_session and activation_session != session_id:
        guarded = False
    lines = [
        f"Symphony ({scope}): {'enabled' if state.enabled else 'disabled'}",
        f"Hooks: {'guarded' if guarded else 'pending verification'}",
    ]
    if provider:
        lines.append(_boost_status(state, provider, session_id))
    if provider == "claude" and activation.get("claude_probe_attempted"):
        lines.append(f"Claude model check: {activation.get('profile') or 'unverified (Sonnet fallback)'}")
    if not state.active_run:
        lines.append("Run (this session): none")
    if session_id and activation_session and activation_session != session_id:
        lines.append(
            f"Historical heartbeat from session {activation_session}; current session "
            "has not been verified."
        )
    if activation.get("last_fault"):
        lines.append(f"Last hook fault: {activation['last_fault']}")
    runs = ([state.active_run] if state.active_run else []) + (list(state.recent_runs) if include_history else [])
    if state.active_run:
        lines.append(f"Run: {state.active_run.run_id} ({state.active_run.status})")
        assessment = state.active_run.assessment
        if assessment.get("size") and assessment.get("complexity"):
            lines.append(f"Assessment: {assessment['size']}/{assessment['complexity']}")
        route = assessment.get("route", {})
        if not isinstance(route, Mapping):
            route = {}
        topology = assessment.get("topology") or route.get("execution")
        if topology:
            lines.append(f"Topology: {topology}")
        model = route.get("lead_model") or route.get("lead_tier")
        effort = route.get("lead_effort")
        if model:
            lines.append(f"Lead route: {model}{f'/{effort}' if effort else ''}")
        if state.active_run.lead_identity:
            lines.append(f"Lead: {state.active_run.lead_identity} [{_governance(state)}]")
        pending = assessment.get("_pending_delegations", ())
        if pending:
            roles = ", ".join(str(item.get("role") or "agent") for item in pending if isinstance(item, Mapping))
            lines.append(f"Awaiting host launch confirmation: {roles}")
        if state.active_run.outcome:
            lines.append("Lead outcome recorded; tracked work still requires reconciliation.")
        if not state.enabled:
            control = _control_name("enable", provider) if provider else "enable"
            lines.append(
                f"This run is transactional: it governs this task only, and the next prompt is "
                f"ungoverned. Run `{control}` to govern every prompt in this project."
            )
    # Archived unresolved work must remain visible without --all.
    unresolved = next(
        (
            run
            for run in reversed(state.recent_runs)
            if run.status in {"abandoned", "force_stopped"} and run.unreconciled
            and (include_history or (run.session_id == session_id and run.provider == provider))
        ),
        None,
    )
    if unresolved:
        lines.append(
            f"{unresolved.status.replace('_', ' ').capitalize()} run {unresolved.run_id}: never reconciled "
            + ", ".join(unresolved.unreconciled)
        )
    records = [item for run in runs if run for item in run.delegations]
    if include_history:
        visible = records
        others = [run for key, run in state.active_runs.items()
                  if run is not state.active_run
                  and key != f"{provider}:{session_id}"]
        if others:
            lines.append("Other active sessions:")
            for run in others:
                lines.append(f"- run {run.run_id} ({run.provider or 'unbound'}/{run.session_id}, {run.status})")
                lines.extend(format_delegation(item) for item in run.delegations)
        if state.recent_runs:
            lines.append("Historical runs:")
            lines.extend(
                f"- historical run {run.run_id} ({run.status})"
                for run in state.recent_runs
            )
    else:
        visible = list(compact_delegations(state))
    lines.extend(format_delegation(item) for item in visible)
    return "\n".join(lines)


def _native_help(provider: str) -> str:
    return "/symphony:help" if provider == "claude" else "$symphony:symphony help"


def _help(provider: str) -> str:
    if provider == "claude":
        return "Symphony controls: /symphony:enable, /symphony:start, /symphony:bypass, /symphony:disable, /symphony:status, /symphony:agents, /symphony:boost [max|ultra|off], /symphony:reassess, /symphony:proceed, /symphony:stop, /symphony:version, /symphony:help."
    return "Symphony controls: $symphony:symphony enable|start|bypass|disable|status|agents|boost [max|ultra|off]|reassess|proceed|stop|version|help."


def _fault_log(environ: Mapping[str, str]) -> Path:
    return Path(
        environ.get("SYMPHONY_STATE_DIR", Path.home() / ".symphony" / "state")
    ).parent / "faults.log"


def _drain_fault(environ: Mapping[str, str]) -> str | None:
    """Consume any recorded hook fault so the next status can report it once."""
    marker = _fault_log(environ)
    try:
        lines = [line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()]
        marker.unlink()
    except OSError:
        return None
    return lines[-1] if lines else None


def _record_fault(error: BaseException, environ: Mapping[str, str]) -> None:
    """Leave a durable trace of a hook fault outside the store that may have failed."""
    try:
        marker = _fault_log(environ)
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()} {type(error).__name__}\n")
        marker.chmod(0o600)
    except OSError:
        # A fault we cannot even record must still not block the host.
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = handle(payload, os.environ)
    except Exception as error:  # Hook failures must not block unrelated host work.
        sys.stderr.write(f"Symphony hook fault: {type(error).__name__}\n")
        _record_fault(error, os.environ)
        return 0
    if result.stdout:
        sys.stdout.write(result.stdout)
    return 0
