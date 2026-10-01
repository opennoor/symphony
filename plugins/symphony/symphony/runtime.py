"""Fast, offline hook runtime for Symphony's canonical lifecycle."""

from dataclasses import replace
from contextlib import ExitStack
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
from .host_evidence import (
    _archived_fast_owner,
    archived_lead_followup,
    claude_archived_sendmessage_sequence, claude_sendmessage_source_hash,
    claude_committed_native_start_replay, claude_committed_native_terminal_replay, claude_completing_lead_turn,
    claude_current_native_lead_event, claude_recovered_lead_event,
    claude_substantive_launch,
    codex_completing_lead_turn, codex_recovered_lead_event,
    codex_unavailable_lead_proof,
    codex_unmanaged_pre_run_terminal,
    retained_fast_escalation_receipt,
)
from .model import Action, Delegation, Event, ProjectState, RunState, persistable
from .reducer import _ACTIVE_STATES, _stop_block_reason, _substantive_child_completed, reduce
from .routing import (
    Assessment,
    EFFORTS,
    NO_PROFILE,
    assessor_selection,
    fast_lead_selection,
    clamp_against_best,
    model_is_weaker,
    profiles_for,
    resolve_tier,
    route_for,
    route_for_recorded,
    snapshot_for,
)
from .store import StateStore, project_key


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
_ROOT_EXECUTION_TOOLS = {"Bash", "PowerShell", "Write", "Edit", "NotebookEdit"}


def _root_admission_key(provider: str, payload: Mapping) -> str:
    session = payload.get('session_id')
    if (provider != 'claude' or not isinstance(session, str) or not session
            or payload.get('agent_id') or payload.get('subagent_id')):
        return ''
    return 'claude:' + hashlib.sha256(session.encode()).hexdigest()


def _set_root_admission(state: ProjectState, key: str, payload: Mapping,
                        *, pending: bool, clear_all: bool = False) -> ProjectState:
    configuration = dict(state.configuration)
    if clear_all:
        configuration.pop('root_admission_intents', None)
    else:
        recorded = configuration.get('root_admission_intents', {})
        # Malformed present state cannot become an exemption by rewriting it.
        if not isinstance(recorded, Mapping):
            return state
        intents = dict(recorded)
        if pending:
            context = payload.get('prompt_id')
            # One fixed-size record per outstanding root, no task text or
            # historical records. Never evict an intent to admit another root.
            intents[key] = {'version': 1, 'prompt_context': (
                hashlib.sha256(context.encode()).hexdigest()
                if isinstance(context, str) and context else '')}
        else:
            intents.pop(key, None)
        if intents:
            configuration['root_admission_intents'] = intents
        else:
            configuration.pop('root_admission_intents', None)
    return replace(state, configuration=configuration)


def _root_admission_prompt(state: ProjectState, source: Event, provider: str,
                           control: tuple[str, str] | None) -> ProjectState:
    key = _root_admission_key(provider, source.payload)
    if not key or not _is_root_origin(source):
        return state
    if control is not None:
        name, argument = control
        if name == 'disable':
            return _set_root_admission(state, key, source.payload, pending=False, clear_all=True)
        if ((name == 'bypass' and argument) or (name == 'stop' and
                (argument == '--force' or not state.active_run))):
            return _set_root_admission(state, key, source.payload, pending=False)
        task = name in {'enable', 'start'} and bool(argument)
    else:
        task = state.enabled and bool(str(source.payload.get('prompt') or '').strip())
        if not state.enabled and not state.active_run:
            return _set_root_admission(state, key, source.payload, pending=False)
    recorded = state.configuration.get('root_admission_intents', {})
    existing = isinstance(recorded, Mapping) and key in recorded
    if existing and control is not None and not task:
        # A control detour does not replace the objective that authorized an
        # observed SendMessage. A new ordinary/task-bearing prompt does.
        return state
    if existing or (task and not state.active_run):
        return _set_root_admission(state, key, source.payload, pending=True)
    return state


def _observe_sendmessage_intent(state: ProjectState, source: Event, provider: str) -> ProjectState:
    key = _root_admission_key(provider, source.payload)
    if not key or source.payload.get('tool_name') != 'SendMessage':
        return state
    intents = state.configuration.get('root_admission_intents', {})
    intent = intents.get(key) if isinstance(intents, Mapping) else None
    details = source.payload.get('tool_input')
    call = source.payload.get('tool_use_id')
    context = source.payload.get('prompt_id')
    history = [run for run in state.recent_runs
               if run.provider == provider and run.session_id == source.payload.get('session_id')]
    if (not isinstance(intent, Mapping) or type(intent.get('version')) is not int or intent.get('version') != 1
            or not isinstance(details, Mapping) or not isinstance(call, str) or not call
            or not isinstance(context, str) or not context
            or hashlib.sha256(context.encode()).hexdigest() != intent.get('prompt_context')
            or not history or history[-1].status != 'completed' or state.active_run
            or details.get('to') != history[-1].lead_identity
            or not isinstance(details.get('message'), str) or not details['message'].strip()):
        return state
    marked = {**intent, 'continuation_call_hash': hashlib.sha256(call.encode()).hexdigest(),
              'continuation_message_hash': hashlib.sha256(details['message'].encode()).hexdigest()}
    return replace(state, configuration={**state.configuration,
        'root_admission_intents': {**intents, key: marked}})


def _root_admission_guard(state: ProjectState, source: Event, provider: str) -> tuple[Action, ...]:
    key = _root_admission_key(provider, source.payload)
    if not key or source.payload.get('tool_name') not in _ROOT_EXECUTION_TOOLS:
        return ()
    recorded = state.configuration.get('root_admission_intents', {})
    pending = not isinstance(recorded, Mapping) or key in recorded
    if not pending:
        return ()
    if not isinstance(recorded, Mapping):
        return (_block_tool('Symphony pending root admission state is malformed. '
                            'Use /symphony:disable to reset it before enabling a new objective.'),)
    reason = ('Symphony is awaiting managed admission for this root objective. '
              'Use Agent to launch the offered Symphony fast lead or independent assessor; '
              'wait for its accepted Start before executing commands or editing files. '
              'Read, Glob, Grep, Skill and clarification remain available. '
              'An explicit /symphony:stop with no managed run cancels this intent; '
              '/symphony:bypass <task> explicitly opts this task out.')
    return (_block_tool(reason),)


def _consume_root_admission(state: ProjectState, source: Event, provider: str,
                            launches: tuple = ()) -> ProjectState:
    run = state.active_run
    if provider != 'claude' or not run or source.kind != 'subagent_started':
        return state
    key = _root_admission_key(provider, {'session_id': run.session_id})
    recorded = state.configuration.get('root_admission_intents', {})
    if not isinstance(recorded, Mapping) or key not in recorded:
        return state
    identity = source.payload.get('agent_id')
    item = next((item for item in run.delegations if item.identity == identity), None)
    if (not item or item.role not in {'assessor', 'lead'}
            or item.state.lower() not in {'working', 'active', 'running'}
            or source.event_id not in run.assessment.get('_start_event_ids', ())
            or source.payload.get('parent_thread_id') not in (None, '', run.session_id)):
        return state
    named = _agent_label_model_effort(str(source.payload.get('agent_type') or ''), item.role)
    if (named != (item.requested_tier, item.requested_effort)
            or _observed_role(source.payload) != item.role
            or source.payload.get('model') not in (None, '', named[0])
            or source.payload.get('model_reasoning_effort') not in (None, '', named[1])):
        return state
    if item.role == 'assessor':
        queued = next((launch for launch in launches if isinstance(launch, Mapping)
                       and launch.get('role') == item.role
                       and (launch.get('model'), launch.get('effort')) == named), None)
        routes = run.assessment.get('_assessor_expected_routes', {})
        if not isinstance(routes, Mapping):
            return state
        expected = routes.get(identity) or (
            queued if queued else assessor_selection(
                _snapshot(state, provider), _boost_preference(state, provider, run.session_id)))
        if not isinstance(expected, Mapping):
            return state
        if item.requested_effort not in HIGH_EFFORTS or named != (expected.get('model'), expected.get('effort')):
            return state
    else:
        expected = run.assessment.get('_fast_route')
        if (run.lead_identity != identity or not run.assessment.get('_fast_pending')
                or run.assessment.get('_lead_route_mismatch') or not isinstance(expected, Mapping)
                or named != (expected.get('model'), expected.get('effort'))):
            return state
    return _set_root_admission(state, key, source.payload, pending=False)


def _retained_activation_command(retained: str, original: str) -> str:
    """Give Codex a short checker that verifies the pinned tree before import."""
    digest = Path(retained).name
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return ""
    code = (
        "import hashlib,pathlib,runpy,sys;"
        "r=pathlib.Path(sys.argv[1]);f=sorted(r.rglob(\"*\"),key=lambda p:p.relative_to(r).as_posix());"
        "assert r.is_dir() and not r.is_symlink() and not any(p.is_symlink() for p in f);"
        "d=hashlib.sha256(b\"\".join(p.relative_to(r).as_posix().encode()+bytes(1)"
        "+hashlib.sha256(p.read_bytes()).digest() for p in f if p.is_file())).hexdigest();"
        f"assert d==\"{digest}\";"
        "sys.argv=[str(r/\"scripts/check_activation.py\"),\"--plugin-root\",sys.argv[2]];"
        "runpy.run_path(sys.argv[0],run_name=\"__main__\")"
    )
    if os.name == "nt":
        # Windows PowerShell 5.1 strips double quotes inside native arguments.
        # Single-quoted Python literals survive its legacy argument passing.
        code = code.replace('"', "'")
    executable = str(Path(sys.executable).absolute())
    arguments = [executable, "-I", "-c", code, retained, original]
    return ("& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
            if os.name == "nt" else shlex.join(arguments))


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
    # A graceful user control is a Stop request for ownership, inbox replay,
    # and native-turn freshness as well as for the reducer. Keeping it as a
    # user_prompt until _handle_control would bypass those shared guards.
    explicit_stop = (source.kind == "user_prompt" and
                     _parse_control(str(source.payload.get("prompt") or "")) == ("stop", ""))
    if explicit_stop:
        source = replace(source, kind="stop_requested")
    expected_owner = ""

    def dispatch(state: ProjectState, event: Event) -> tuple[ProjectState, tuple[Action, ...]]:
        # Select the owning run under the store lock so parallel hooks cannot
        # observe a stale project roster.
        current_scope = _run_scope(state, event, provider)
        if current_scope is None:
            return state, ()
        if expected_owner and current_scope != (f"{provider}:{expected_owner}", expected_owner):
            return state, ()
        current_key, current_session = current_scope
        current_payload = {**event.payload, "session_id": current_session}
        current_source = replace(event, payload=current_payload)
        scoped = _scope_state(state, current_key, current_session, provider)
        next_scoped, actions = _transition(scoped, current_source, provider, current_payload, environ)
        rendered = _render_actions(actions, next_scoped, provider, event.kind)
        merged = _merge_scope(state, scoped, next_scoped, current_key, provider, current_session)
        control = _parse_control(str(event.payload.get("prompt") or "")) if event.kind == "user_prompt" else None
        if next_scoped.active_run or event.kind == "stop_requested" or (control and control[0] in {"disable", "stop"}):
            merged = _release_pending_session(merged, provider, current_session)
        return merged, rendered

    def transition(
        state: ProjectState, pending: tuple[tuple[Event, int], ...] = (), generation: int = 1,
        retired: frozenset[str] = frozenset(),
    ) -> tuple[ProjectState, tuple[tuple[Action, ...], set[str]]]:
        acknowledged: set[str] = set()
        unresolved = False
        lifecycle_source = source.kind in {"subagent_started", "subagent_stopped"}
        source_actions: tuple[Action, ...] = ()
        # The triggering lifecycle callback was timestamped before it waited
        # for this session lock. A newer callback may have entered an inbox in
        # the meantime, so sort it with the retained events, then hold archival
        # until every relevant event has been considered.
        batch = [(event, event_generation, False) for event, event_generation in pending]
        if lifecycle_source:
            batch.append((source, generation, True))
        if provider == 'claude' and session:
            retained = []
            for event, epoch, current in batch:
                identity = event.payload.get('agent_id') or event.payload.get('subagent_id')
                if (epoch == generation and identity not in retired
                        and _committed_sendmessage_source(state, event, session, generation, project, environ)):
                    if not current:
                        acknowledged.add(event.event_id)
                else:
                    retained.append((event, epoch, current))
            batch = retained
            if batch and all(epoch == generation and
                             (event.payload.get('agent_id') or event.payload.get('subagent_id')) not in retired
                             for event, epoch, _ in batch):
                try:
                    sequence = claude_archived_sendmessage_sequence(
                        state, tuple(event for event, _, _ in batch), session,
                        Path(record['project'] or project) if record else project, environ)
                except (OSError, TypeError, ValueError, AttributeError):
                    sequence = None
                if sequence is not None:
                    committed = _commit_sendmessage_sequence(state, sequence, tuple(
                        event for event, _, _ in batch), session, generation, dispatch, project, environ)
                    if committed is not None:
                        state = committed
                        acknowledged.update(event.event_id for event, _, current in batch if not current)
                        batch = []
        recoverable = _recoverable_fast_events(state, pending, provider, session, generation, retired)
        followup = None
        if session and batch and all(epoch == generation
                         and _observed_role(event.payload) in {"", "lead"} and
                         (event.payload.get("agent_id") or event.payload.get("subagent_id")) not in retired
                         for event, epoch, _ in batch):
            try:
                followup = archived_lead_followup(
                    state, tuple(event for event, _, _ in batch), provider, session,
                    Path(record["project"] or project) if record else project, environ)
            except (OSError, TypeError, ValueError, AttributeError):
                followup = None
        if followup is not None:
            archived, native = followup
            resumed = replace(archived, status="completing",
                              assessment={**archived.assessment, "_batch_pending": True})
            state = replace(state, active_run=resumed,
                            active_runs={**state.active_runs, f"{provider}:{session}": resumed},
                            recent_runs=tuple(run for run in state.recent_runs if run != archived))
            started = Event(f"{native.event_id}:followup-start", "subagent_started",
                            native.payload["_symphony_native_started_at"],
                            {**native.payload, "agent_type": native.payload.get("agent_type", "lead"),
                             "status": "working", "task": archived.task})
            if provider == "claude":
                started = replace(started, payload={**started.payload, "prompt_id":
                    native.payload.get("_symphony_root_prompt_id") or native.payload["prompt_id"]})
            state, _ = dispatch(state, started)
            recoverable.update(event.event_id for event, _, _ in batch)
            # Callback arrival can lag the native terminal. Its proven Start
            # belongs before that terminal, even when hooks arrived backwards.
            batch = [(replace(event, observed_at=started.observed_at)
                      if event.kind == "subagent_started" else event, epoch, current)
                     for event, epoch, current in batch]
        for event, event_generation, current in sorted(batch, key=lambda item: item[0].observed_at):
            if (not current and provider == 'codex' and expected_owner == session
                    and event_generation == generation
                    and (event.payload.get('agent_id') or event.payload.get('subagent_id')) not in retired):
                state, disposed = _dispose_unmanaged_pre_run_terminal(
                    state, event, session, project, environ, generation, source.observed_at,
                    tuple(item[0] for item in batch))
                if disposed:
                    acknowledged.add(event.event_id)
                    continue
            if event.event_id in recoverable:
                if followup is not None and provider == "claude" and event.kind == "subagent_stopped":
                    # Preserve the inbox ID until pending_event_id is captured;
                    # claude_current_native_lead_event below supplies the native ID.
                    event = replace(event, payload={**followup[1].payload, '_symphony_native_recovery': True})
                elif (followup is not None and event.kind == 'subagent_stopped'
                      and followup[1].payload.get('_symphony_archived_fast_escalation') is True):
                    event = replace(event, payload={**event.payload, '_symphony_native_recovery': True,
                                                   '_symphony_archived_native_event_id': followup[1].event_id,
                                                   '_symphony_archived_fast_escalation': True})
                event = replace(event, payload={**event.payload, "_symphony_owner_conflict": False})
            state = _hold_pending_batch(state, provider, session)
            pending_event_id = event.event_id
            if followup is not None and provider == 'claude' and event.kind == 'subagent_stopped':
                # ACK the inbox ID, but commit the exact proven native event.
                event = followup[1]
            if not current:
                identity = event.payload.get("agent_id") or event.payload.get("subagent_id")
                if event_generation < generation:
                    acknowledged.add(pending_event_id)
                    continue
                if event_generation != generation or identity in retired:
                    unresolved = True
                    continue
            if provider == "claude" and expected_owner:
                scoped = _scope_state(state, f"claude:{expected_owner}", expected_owner, provider)
                native = claude_current_native_lead_event(
                    scoped, event, expected_owner, project, environ)
                if native is not None:
                    event = native
            if (provider == "claude" and expected_owner
                    and _observed_role(event.payload) in {"", "lead"}
                    and claude_committed_native_start_replay(
                        state, event, expected_owner, Path(record["project"] or project), environ)):
                if not current:
                    acknowledged.add(pending_event_id)
                continue
            if (provider == "claude" and event.kind == "subagent_started" and any(
                    run.provider == provider and run.session_id == session
                    and run.status in {"completing", "completed"}
                    and run.lead_identity == (event.payload.get("agent_id") or event.payload.get("subagent_id"))
                    and event.event_id in run.assessment.get("_start_event_ids", ())
                    and any(item.endswith(":followup-start") for item in run.assessment.get("_start_event_ids", ()))
                    for run in (*state.active_runs.values(), *state.recent_runs))):
                # Claude can reuse a root prompt ID for another Agent resume.
                # The native check above must distinguish that new call from
                # a delayed Start before exact-payload replay is safe.
                if current:
                    store.queue_session_event(provider, session, event, ambiguous_owner=True)
                unresolved = True
                continue
            if (provider == "claude" and expected_owner
                    and claude_committed_native_terminal_replay(
                        state, event, expected_owner, project, environ)):
                if not current:
                    acknowledged.add(pending_event_id)
                continue
            if current:
                if (expected_owner and (_committed_child_start_replay(
                        state, event, provider, expected_owner)
                        or _committed_lead_event(state, event, provider, expected_owner))):
                    continue
                if (expected_owner and _committed_child_terminal_replay(
                        state, event, provider, expected_owner, allow_active=True)):
                    # A callback retried after archive cannot complete a
                    # newer run that reused the same child identity.
                    continue
                if (expected_owner and event.kind in {"subagent_started", "subagent_stopped"}
                        and f"{provider}:{expected_owner}" not in state.active_runs
                        and not (event.kind == "subagent_started"
                                 and _observed_role(event.payload) == "assessor")
                        and _archived_managed_child(state, event, provider, expected_owner)):
                    # A non-replay child turn after archive has no run to own
                    # it. Retain the Start as well as its terminal, so a
                    # later root task cannot silently inherit either one.
                    store.queue_session_event(provider, session, event, ambiguous_owner=True)
                    unresolved = True
                    continue
                if (expected_owner and _prior_child_terminal_conflict(
                        state, event, provider, expected_owner)):
                    store.queue_session_event(provider, session, event, ambiguous_owner=True)
                    unresolved = True
                    continue
                if (expected_owner and _run_scope(state, event, provider)
                        != (f"{provider}:{expected_owner}", expected_owner)):
                    # Retain the source before this project transaction can
                    # commit. A settled root may otherwise dispatch a child
                    # terminal into another active run in the same file.
                    store.queue_session_event(provider, session, event, ambiguous_owner=True)
                    unresolved = True
                    continue
                state, source_actions = dispatch(state, event)
                continue
            disposition = _pending_child_disposition(state, event, provider, session)
            if disposition == "stale":
                acknowledged.add(pending_event_id)
            elif disposition == "apply":
                state, _ = dispatch(state, event)
                acknowledged.add(pending_event_id)
            else:
                unresolved = True
        unresolved_reason = _unresolved_child_guidance(
            state, provider, session, tuple(event for event, _, _ in batch
                                           if event.event_id not in acknowledged)) if unresolved else ""
        if source.kind == "stop_requested" and unresolved:
            return state, (_claude_native_stop_guard(source, unresolved_reason), acknowledged)
        if (not unresolved and provider == "claude"
                and source.kind in {"stop_requested", "user_prompt", "session_heartbeat"}):
            scoped = _scope_state(state, f"claude:{session}", session, provider)
            try:
                recovered = claude_recovered_lead_event(scoped, session, project, environ)
            except Exception:
                if source.kind == "stop_requested" and scoped.active_run is not None:
                    return state, (_claude_native_stop_guard(source,
                        "Symphony could not verify the tracked lead's native Claude result. "
                        "Return to this session after its result is available."), acknowledged)
                recovered = None
            if recovered is not None:
                state = _hold_pending_batch(state, provider, session)
                state, _ = dispatch(state, recovered)
            if source.kind == "stop_requested":
                scoped = _scope_state(state, f"claude:{session}", session, provider)
                try:
                    freshness, native_turn = claude_completing_lead_turn(
                        scoped, session, project, environ)
                except Exception:
                    if scoped.active_run is not None:
                        return state, (_claude_native_stop_guard(source,
                            _claude_unknown_turn_guidance(scoped.active_run)), acknowledged)
                    freshness, native_turn = "none", None
                if freshness == "unknown":
                    return state, (_claude_native_stop_guard(source,
                        _claude_unknown_turn_guidance(scoped.active_run)), acknowledged)
                if native_turn is not None:
                    state = _hold_pending_batch(state, provider, session)
                    state, _ = dispatch(state, native_turn)
        if (not unresolved and provider == "codex"
                and source.kind in {"stop_requested", "user_prompt", "session_heartbeat"}):
            scoped = _scope_state(state, f"codex:{session}", session, provider)
            try:
                recovered = codex_recovered_lead_event(scoped, session, environ)
            except Exception:
                if source.kind == "stop_requested" and scoped.active_run is not None:
                    return state, ((Action("block_stop", {"reason":
                        "Symphony could not verify the tracked lead's native Codex result. "
                        "Return to this session after its result is available."}),), acknowledged)
                recovered = None
            if recovered is not None:
                state = _hold_pending_batch(state, provider, session)
                state, _ = dispatch(state, recovered)
            if source.kind == "stop_requested":
                scoped = _scope_state(state, f"codex:{session}", session, provider)
                try:
                    freshness, native_turn = codex_completing_lead_turn(scoped, session, environ)
                except Exception:
                    if scoped.active_run is not None:
                        return state, ((Action("block_stop", {"reason":
                            "Symphony could not verify the tracked lead's latest native Codex turn. "
                            "Return to this session after its result is available."}),), acknowledged)
                    freshness, native_turn = "none", None
                if freshness in {"running", "unknown"}:
                    reason = ("The tracked lead has a newer native turn still running. "
                              "Wait for its result before completing this run."
                              if freshness == "running" else
                              "Symphony could not verify the tracked lead's latest native turn. "
                              "Return to this session after its result is available.")
                    return state, ((Action("block_stop", {"reason": reason}),), acknowledged)
                if native_turn is not None:
                    state = _hold_pending_batch(state, provider, session)
                    state, _ = dispatch(state, native_turn)
        # A new turn delivered in this same hook can supersede an earlier
        # queued completion. Include it before releasing the archive hold.
        if source.kind != "stop_requested":
            if pending and not lifecycle_source:
                state = _hold_pending_batch(state, provider, session)
            if lifecycle_source:
                actions = source_actions
            else:
                state, actions = dispatch(state, source)
            if not unresolved:
                before_finish = state
                state = _finish_pending_batch(state, provider, session, source)
                actions = _refresh_completion_guidance(
                    actions, before_finish, state, source, provider)
        else:
            if not unresolved:
                state = _finish_pending_batch(state, provider, session, source)
            state, actions = dispatch(state, source)
        if unresolved and source.kind in {"session_heartbeat", "user_prompt"}:
            actions += (Action("inject_context", {"text": unresolved_reason}),)
        return state, (actions, acknowledged)

    session = str(source.payload.get("session_id") or "")
    if session:
        with store.session_lock(provider, session):
            record = store.session_record(provider, session)
            bound = store.root / record["state_name"] if record and record["state_name"] else None
            ambiguous_scope = False
            owners = (store.active_owner_paths(provider, session,
                                               include_ancestry=not _is_root_origin(source))
                      if bound is None or record["migrated"] else ())
            if (bound is None and owners == () and source.kind in {"subagent_started", "subagent_stopped", "pre_tool_use"}):
                parent = str(source.payload.get("parent_thread_id") or "")
                if parent:
                    owners = store.active_owner_paths(provider, session, parent, include_ancestry=True)
            if owners is not None and len(owners) == 1 and bound is None:
                origin = project if store._path(project) == owners[0] and _is_root_origin(source) else None
                owner_state = store.read_path(owners[0])
                scope = _run_scope(owner_state, source, provider) if owner_state else None
                if scope is not None:
                    record = store.bind_session(provider, session, owners[0], True, origin,
                                                scope[1])
                    bound = owners[0]
                else:
                    ambiguous_scope = True
            if (owners == () and bound is None and _is_root_origin(source)):
                record = store.bind_session(provider, session, store._path(project),
                                            bool(record and record["pending"]), project)
                bound = store._path(project)
            if (bound is not None and record is not None and bound != store._path(project)
                    and owners == () and _starts_new_root_task(source)
                    and not record["pending"] and not record["overflow"]):
                old_state = store.read_path(bound)
                if old_state is not None and _settled_for_rebind(old_state, provider, session):
                    retired = {agent for run in old_state.recent_runs
                               if run.provider == provider and run.session_id == session
                               for agent in (run.lead_identity,
                                             *(item.identity for item in run.delegations)) if agent}
                    record = store.rebind_session(record, project, retired)
                    bound = store._path(project)
            conflict = (owners is None or len(owners) > 1
                        or (bound is not None and owners and owners[0] != bound))
            if record and record["owner_session"] and record["owner_session"] != session:
                # Repair an alias record persisted by an older candidate or
                # interrupted before its discovery pointer was published.
                store._register_alias(provider, record["owner_session"], session)
            if conflict or bound is None:
                # A root session cannot safely own two active project files.
                # A real Stop must still block, and controls must explain the
                # conflict instead of silently hiding the unfinished work.
                reason = (("Symphony could not verify this root session's project owner because "
                           "a state snapshot was unavailable. Retry this turn after the other hook finishes.")
                          if owners is None else
                          ("Symphony found this root session active in multiple project states. "
                           "Inspect and reconcile those runs before completing this session.")
                          if conflict else
                          ("Symphony has not yet verified this child event's root project. "
                           "Return to the root session to reconcile it."))
                if source.kind in {"subagent_started", "subagent_stopped"}:
                    store.queue_session_event(
                        provider, session, source,
                        ambiguous_owner=ambiguous_scope or bool(owners and (len(owners) > 1 or
                            (bound is not None and owners[0] != bound))),
                    )
                    return HookResult()
                if source.kind == "pre_tool_use":
                    return render(provider, (Action("block_tool", {"reason": reason}),),
                                  str(payload.get("hook_event_name") or "PreToolUse"))
                if source.kind == "stop_requested":
                    if owners == () and bound is None and record is None:
                        actions, _ = store.update(project, lambda state: transition(state))
                        return render(provider, actions, "Stop")
                    return render(provider, _claude_native_stop_guard(source, reason),
                                  str(payload.get("hook_event_name") or "Stop"))
                if source.kind in {"user_prompt", "session_heartbeat"}:
                    return render(provider, (Action("inject_context", {"text": reason}),),
                                  str(payload.get("hook_event_name") or "UserPromptSubmit"))
                return HookResult()
            expected_owner = str(record["owner_session"] or session)
            if (source.kind in {"subagent_started", "subagent_stopped"}
                    and str(source.payload.get("agent_id") or source.payload.get("subagent_id") or "")
                    in record["retired_agents"]):
                store.queue_session_event(provider, session, source)
                return HookResult()
            if (record["owner_session"] != session
                    and source.kind in {"subagent_started", "subagent_stopped"}):
                # A child-session callback may belong to the root run, but it
                # must share the root's pending batch and project transaction.
                store.queue_session_event(provider, session, source)
                return HookResult()
            with ExitStack() as aliases:
                records = [record]
                unresolved_alias = False
                if record["owner_session"] == session:
                    roster = store.read_path(bound)
                    own_run = roster.active_runs.get(f"{provider}:{session}") if roster else None
                    ancestry = ({session, own_run.lead_identity,
                                 *(item.identity for item in own_run.delegations)}
                                if own_run else {session})
                    names = set(ancestry)
                    try:
                        for parent in sorted(name for name in ancestry if name):
                            names.update(store.aliases_for_owner(provider, parent))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        unresolved_alias = True
                    for alias in sorted(name for name in names if name and name != session):
                        try:
                            aliases.enter_context(store.session_lock(provider, alias, timeout=0.1))
                        except TimeoutError:
                            unresolved_alias = True
                            break
                        alias_record = store.session_record(provider, alias)
                        if alias_record and (alias_record["pending"] or alias_record["overflow"]):
                            if alias_record["owner_session"] not in {None, session}:
                                # The pointer is only a discovery hint. An
                                # authoritative binding to another root must
                                # not block this independent session.
                                continue
                            if any(item.get("ambiguous_owner") and not (
                                roster is not None and (
                                    _committed_child_start_replay(
                                        roster, Event(item["event_id"], item["kind"],
                                                      item["observed_at"], item["payload"]),
                                        provider, session,
                                    ) or _committed_child_terminal_replay(
                                        roster, Event(item["event_id"], item["kind"],
                                                      item["observed_at"], item["payload"]),
                                        provider, session,
                                    ) or (provider == "claude" and
                                          claude_committed_native_terminal_replay(
                                              roster, Event(item["event_id"], item["kind"],
                                                            item["observed_at"], item["payload"]),
                                              session, Path(record["project"] or project), environ))
                                )) for item in alias_record["pending"]):
                                unresolved_alias = True
                                continue
                            exact_owner = (alias_record["owner_session"] == session
                                           and alias_record["state_name"] == bound.name)
                            native_parent = (alias_record["owner_session"] is None
                                             and bool(alias_record["pending"])
                                             and all(item["payload"].get("parent_thread_id") in ancestry
                                                     for item in alias_record["pending"])
                                             and store.active_owner_paths(
                                                 provider, alias,
                                                 str(alias_record["pending"][0]["payload"].get(
                                                     "parent_thread_id") or ""),
                                                 include_ancestry=True,
                                             ) == (bound,))
                            archived_replay = (alias_record["owner_session"] is None
                                               and bool(alias_record["pending"])
                                               and roster is not None
                                               and all(
                                                   _committed_child_start_replay(
                                                       roster,
                                                       Event(item["event_id"], item["kind"],
                                                             item["observed_at"], item["payload"]),
                                                       provider, session,
                                                   ) or _committed_child_terminal_replay(
                                                       roster,
                                                       Event(item["event_id"], item["kind"],
                                                             item["observed_at"], item["payload"]),
                                                       provider, session,
                                                   ) or (provider == "claude" and
                                                         claude_committed_native_terminal_replay(
                                                             roster,
                                                             Event(item["event_id"], item["kind"],
                                                                   item["observed_at"], item["payload"]),
                                                             session, Path(record["project"] or project),
                                                             environ))
                                                   for item in alias_record["pending"]))
                            if exact_owner or native_parent or archived_replay:
                                records.append(alias_record)
                            else:
                                unresolved_alias = True
                if unresolved_alias:
                    reason = ("Symphony retained a child result whose root ownership is unresolved. "
                              "Return to this session after its lifecycle is reconciled.")
                    if source.kind in {"subagent_started", "subagent_stopped"}:
                        store.queue_session_event(provider, session, source)
                        return HookResult()
                    if source.kind == "stop_requested":
                        return render(provider, _claude_native_stop_guard(source, reason), "Stop")
                    if source.kind == "pre_tool_use":
                        return render(provider, (Action("block_tool", {"reason": reason}),), "PreToolUse")
                    if source.kind in {"user_prompt", "session_heartbeat"}:
                        return render(provider, (Action("inject_context", {"text": reason}),),
                                      str(payload.get("hook_event_name") or "UserPromptSubmit"))
                    return HookResult()
                pending = tuple(
                    (Event(item["event_id"], item["kind"], item["observed_at"],
                           {**item["payload"], "_symphony_owner_conflict":
                            bool(item.get("ambiguous_owner")),
                            "_symphony_verified_alias": source_record is not record
                            and source_record["owner_session"] == session
                            and source_record["state_name"] == bound.name}),
                     # Alias counters are local to that child session. Its
                     # verified parent/owner and active run establish the
                     # root generation; comparing raw counters drops events.
                     item["generation"] if source_record is record else record["generation"])
                    for source_record in records for item in source_record["pending"]
                )
                if any(item["overflow"] for item in records):
                    reason = ("Symphony retained more unresolved child events than it can safely replay. "
                              "Inspect this session before completion.")
                    if source.kind in {"subagent_started", "subagent_stopped"}:
                        store.queue_session_event(provider, session, source)
                        return HookResult()
                    action = ("block_stop" if source.kind == "stop_requested" else
                              "block_tool" if source.kind == "pre_tool_use" else "inject_context")
                    actions = (_claude_native_stop_guard(source, reason) if source.kind == "stop_requested"
                               else (Action(action, {"reason": reason, "text": reason}),))
                    return render(provider, actions,
                                  str(payload.get("hook_event_name") or "UserPromptSubmit"))
                if source.kind == "subagent_stopped" and not pending and not bound.is_file():
                    return HookResult()
                result = (store.update(Path(record["project"]),
                                       lambda state: transition(state, pending, record["generation"],
                                                                frozenset(record["retired_agents"])))
                          if record["project"] else
                          store.update_path(bound, lambda state: transition(state, pending, record["generation"],
                                                                            frozenset(record["retired_agents"]))))
                if result is None:
                    return render(provider, _claude_native_stop_guard(source,
                        "Symphony's bound project state is unavailable; recover this session before completion."),
                                  "Stop") if source.kind == "stop_requested" else HookResult()
                actions, acknowledged = result
                for source_record in records:
                    fresh = store.session_record(provider, source_record["session"])
                    if fresh is not None:
                        store.finish_session_events(fresh, acknowledged)
    else:
        actions, _ = store.update(project, lambda state: transition(state))
    return render(provider, actions, str(payload.get("hook_event_name") or "UserPromptSubmit"))


def _is_root_origin(source: Event) -> bool:
    """Only root-entry hooks may establish a CWD-based session binding."""
    return (source.kind in {"session_heartbeat", "user_prompt"}
            and not any(source.payload.get(field) for field in
                        ("agent_id", "subagent_id", "parent_thread_id", "agent_transcript_path")))


def _starts_new_root_task(source: Event) -> bool:
    if not _is_root_origin(source) or source.kind != "user_prompt":
        return False
    # A settled session's next root prompt (including a project-scoped
    # control) establishes the user's new project context. SessionStart alone
    # can be a resume/compaction heartbeat and cannot move it.
    return bool(str(source.payload.get("prompt") or "").strip())


def _settled_for_rebind(state: ProjectState, provider: str, session: str) -> bool:
    if f"{provider}:{session}" in state.active_runs:
        return False
    return not any(run.provider == provider and run.session_id == session and run.unreconciled
                   for run in state.recent_runs)


def _hold_pending_batch(state: ProjectState, provider: str, session: str) -> ProjectState:
    key = f"{provider}:{session}"
    run = state.active_runs.get(key)
    if not run or run.assessment.get("_batch_pending"):
        return state
    held = replace(run, assessment={**run.assessment, "_batch_pending": True})
    runs = {**state.active_runs, key: held}
    return replace(state, active_runs=runs,
                   active_run=held if state.active_run == run else state.active_run)


def _finish_pending_batch(
    state: ProjectState, provider: str, session: str, source: Event,
) -> ProjectState:
    key = f"{provider}:{session}"
    run = state.active_runs.get(key)
    if not run or not run.assessment.get("_batch_pending"):
        return state
    assessment = dict(run.assessment)
    assessment.pop("_batch_pending", None)
    settled = replace(run, assessment=assessment)
    state = replace(state, active_runs={**state.active_runs, key: settled},
                    active_run=settled if state.active_run == run else state.active_run)
    lead = next((item for item in settled.delegations
                 if item.identity == settled.lead_identity and item.role == "lead"), None)
    if (settled.status != "completing" or not settled.outcome or not lead
            or lead.state.lower() not in {"completed", "done", "success", "succeeded"}):
        return state
    scoped = _scope_state(state, key, session, provider)
    final = Event(
        f"{source.event_id}:pending-batch:{source.observed_at}", "lead_completed",
        source.observed_at,
        {"identity": settled.lead_identity, "owner_generation": settled.owner_generation,
         "outcome": settled.outcome},
    )
    next_scoped, _ = reduce(scoped, final)
    return _merge_scope(state, scoped, next_scoped, key, provider, session)


def _dispose_unmanaged_pre_run_terminal(
    state: ProjectState, event: Event, session: str, project: Path,
    environ: Mapping[str, str], generation: int, committed_at: str,
    batch: tuple[Event, ...] = (),
) -> tuple[ProjectState, bool]:
    """Settle only a proven unmanaged callback, without dispatching lifecycle."""
    payload = event.payload
    if (event.kind != 'subagent_stopped' or payload.get('provider') != 'codex'
            or payload.get('session_id') != session or payload.get('parent_thread_id') != session
            or _observed_role(payload) != 'lead' or _fast_spawn(payload)
            or type(generation) is not int or generation < 1):
        return state, False
    base = {'disposition': 'unmanaged_pre_run', 'provider': 'codex', 'session': session,
            'agent': payload.get('agent_id'), 'turn': payload.get('turn_id'), 'parent': session,
            'generation': generation, 'project': project_key(project),
            'source_event_id': event.event_id, 'source_observed_at': event.observed_at,
            'result': _terminal_result_id(event)}
    if any((item.payload.get('agent_id') == base['agent'] and item != event)
           or item.payload.get('parent_thread_id') == base['agent']
           or item.kind == 'subagent_started' and _observed_role(item.payload) == 'assessor'
           for item in batch):
        return state, False
    identifier = event.event_id + ':unmanaged-pre-run-disposition'
    witnesses = ('native_call_id', 'native_path', 'native_started_at', 'native_completed_at',
                 'first_admission_id', 'first_admission_at')
    previous = [record for record in state.event_history if record.event_id == identifier]
    if previous:
        record = previous[0]
        exact = (len(previous) == 1 and record.kind == 'unmanaged_terminal_disposed'
                 and type(record.payload.get('generation')) is int
                 and set(record.payload) == {*base, *witnesses}
                 and all(record.payload.get(key) == value for key, value in base.items())
                 and all(isinstance(record.payload.get(key), str) and record.payload[key]
                         and len(record.payload[key]) <= 512
                         for key in witnesses)
                 and record.payload['native_path'] == '/root/' + str(payload.get('task_name') or ''))
        if exact:
            try:
                instants = [datetime.fromisoformat(record.payload[key].replace('Z', '+00:00'))
                            for key in ('native_started_at', 'native_completed_at', 'first_admission_at')]
                exact = (all(instant.tzinfo is not None for instant in instants)
                         and instants[0] <= instants[1] < instants[2])
            except (TypeError, ValueError):
                exact = False
        return state, exact
    try:
        proof = codex_unmanaged_pre_run_terminal(
            state, event, session, project, environ, base['result'])
    except (OSError, AttributeError, TypeError, ValueError):
        proof = None
    if proof is None:
        return state, False
    disposition = Event(identifier, 'unmanaged_terminal_disposed', committed_at, {**base, **proof})
    if persistable(disposition) != disposition:
        return state, False
    committed, _ = reduce(state, disposition)
    return committed, True


def _unresolved_child_guidance(
    state: ProjectState, provider: str, session: str, events: tuple[Event, ...],
) -> str:
    if any('native_fast_escalation' not in receipt and 'native_followup_start_id' not in receipt
           and receipt.get('provider') == provider and receipt.get('session') == session
           and receipt.get('native_model') and receipt.get('native_effort')
           and receipt.get('agent') == receipt.get('lead')
           and any((event.payload.get('agent_id') or event.payload.get('subagent_id')) == receipt.get('agent')
                   and (provider == 'claude' or _child_turn_token(event.payload) in {'', receipt.get('turn')})
                   and re.findall(r'^SYMPHONY_FAST_DECISION: (eligible|escalate)[ \t]*\r?$',
                                  str(event.payload.get('last_assistant_message') or ''), re.MULTILINE) == ['escalate']
                   for event in events)
           for receipt in state.terminal_receipts):
        return ('Symphony retained an escalation callback whose acknowledgment proof is unavailable. '
                'An older runtime may have removed its receipt metadata, and the retained run does not '
                'supply all required anchors. Preserve the callback and reconcile it with its original '
                'native turn before completion. Finish live work before reloading onto one runtime; '
                'reloading alone does not restore missing proof.')
    history = [run for run in state.recent_runs
               if run.provider == provider and run.session_id == session]
    if history and f"{provider}:{session}" not in state.active_runs:
        run = history[-1]
        if (_archived_fast_owner(run)
                and any(event.kind == 'subagent_stopped'
                        and (event.payload.get('agent_id') or event.payload.get('subagent_id')) == run.lead_identity
                        and re.findall(r'^SYMPHONY_FAST_DECISION: (eligible|escalate)[ \t]*\r?$',
                                       str(event.payload.get('last_assistant_message') or ''), re.MULTILINE) == ['escalate']
                        for event in events)):
            return ('Symphony retained an archived fast-lead escalation report; it cannot complete work '
                    'under the old direct route. Request a fresh independent assessment for the full current '
                    'objective. The retained native turn still needs exact lineage and outcome reconciliation; '
                    'do not invent its outcome or claim completion.')
        superseded = {item.identity for item in run.delegations
                      if item.role == "lead" and item.identity != run.lead_identity}
        if any((event.payload.get("agent_id") or event.payload.get("subagent_id")) in superseded
               for event in events):
            return ("Symphony retained an unresolved child result from a superseded lead. "
                    f"The archived owner is `{run.lead_identity}`. Do not resume the superseded lead "
                    "or repeat Stop. Preserve the result and request explicit reconciliation or a "
                    "separately routed task; a weaker former lead cannot replace the accepted owner.")
    return ("Symphony retained an unresolved child result for this session. "
            "Preserve the result and inspect its original native launch and owning run before completion. "
            "Repeating Stop alone does not restore missing launch or ownership proof.")


def _committed_lead_event(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    """An exact committed lead event survives a crash before its inbox ACK."""
    identity = event.payload.get("agent_id") or event.payload.get("subagent_id")
    owned_session = (event.payload.get("session_id") == session or (
        event.payload.get("session_id") == identity and (
            event.payload.get("parent_thread_id") == session
            or event.payload.get("_symphony_verified_alias") is True)))
    if (event.payload.get("provider") != provider
            or not owned_session
            or event.payload.get("parent_thread_id") not in (
                {None, "", session} if provider == "claude" else {session})
            or not _child_turn_token(event.payload)):
        return False
    for run in (*state.active_runs.values(), *state.recent_runs):
        if (run.provider != provider or run.session_id != session
                or run.lead_identity != identity):
            continue
        if event.kind == "subagent_started":
            if event.event_id in run.assessment.get("_start_event_ids", ()):
                return True
            # A native followup may report its terminal before the Start hook.
            native_id = hashlib.sha256(
                f"codex-host-turn\0{identity}\0{event.payload.get('turn_id')}".encode()).hexdigest()
            lead = next((item for item in run.delegations if item.identity == identity), None)
            if (provider == "codex" and _observed_role(event.payload) in {"", "lead"}
                    and _child_turn_token(event.payload) in run.assessment.get("_terminal_turns", {}).get(identity, ())
                    and f"{native_id}:followup-start" in run.assessment.get("_start_event_ids", ())
                    and lead and event.payload.get("model") in {None, "", lead.requested_tier}
                    and event.payload.get("model_reasoning_effort") in {None, "", lead.requested_effort}):
                return True
        if event.kind == "subagent_stopped":
            result = _terminal_result_id(replace(event, payload={**event.payload, "session_id": session}))
            if any(item == result or item.endswith(f":{result}")
                   for item in run.assessment.get("_terminal_event_ids", ())):
                return True
    return False


def _recoverable_fast_events(
    state: ProjectState, pending: tuple[tuple[Event, int], ...], provider: str,
    session: str, generation: int, retired: frozenset[str],
) -> set[str]:
    """Reconcile the fresh Start/terminal pair retained by the old archive guard.

    The old ambiguous flag also represents real ownership conflicts. Override
    it only for one ordered, exact-root invocation with an unused identity.
    """
    if f"{provider}:{session}" in state.active_runs:
        return set()
    recovered: set[str] = set()
    for start, epoch in pending:
        identity = start.payload.get("agent_id") or start.payload.get("subagent_id")
        token = _child_turn_token(start.payload)
        if (epoch != generation or identity in retired or not token
                or not _fresh_fast_start(state, start, provider, session)
                or _run_scope(state, start, provider) != (f"{provider}:{session}", session)):
            continue
        related = [(event, epoch) for event, epoch in pending
                   if (event.payload.get("agent_id") or event.payload.get("subagent_id")) == identity
                   and event.event_id != start.event_id]
        if len(related) != 1:
            continue
        terminal, terminal_epoch = related[0]
        if (terminal.kind == "subagent_stopped" and terminal_epoch == generation
                and terminal.payload.get("provider") == provider
                and terminal.payload.get("session_id") == session
                and terminal.payload.get("parent_thread_id") == session
                and _child_turn_token(terminal.payload) == token
                and terminal.observed_at >= start.observed_at):
            recovered.update((start.event_id, terminal.event_id))
    return recovered


def _pending_child_disposition(
    state: ProjectState, event: Event, provider: str, session: str,
) -> str:
    """Replay only a child lifecycle that the current run already identifies."""
    if provider == 'codex' and event.kind in {'subagent_started', 'subagent_stopped'}:
        canonical = replace(event, payload={**event.payload, 'session_id': session})
        result = _terminal_result_id(canonical)
        token = _child_turn_token(event.payload)
        # This receipt was admitted by archived native/root proof. ACK only;
        # a subsequent assessment or owner cannot inherit its transition.
        if (token and event.payload.get('provider') == provider
                and event.payload.get('session_id') in {session, event.payload.get('agent_id')}
                and event.payload.get('parent_thread_id') == session
                and any(receipt.get('native_fast_escalation') is True
                        and receipt.get('provider') == provider and receipt.get('session') == session
                        and receipt.get('agent') == event.payload.get('agent_id')
                        and receipt.get('lead') == receipt.get('agent')
                        and receipt.get('turn') == token
                        and ((event.kind == 'subagent_stopped' and receipt.get('result') == result)
                             or (event.kind == 'subagent_started' and _observed_role(event.payload) in {'', 'lead'}
                                 and event.payload.get('model') in {None, '', receipt.get('native_model')}
                                 and event.payload.get('model_reasoning_effort') in {None, '', receipt.get('native_effort')}
                                 and receipt.get('native_followup_start_id') == hashlib.sha256(
                                     f"codex-host-turn\0{receipt['agent']}\0{event.payload.get('turn_id')}".encode()
                                 ).hexdigest() + ':followup-start'))
                        for receipt in (retained_fast_escalation_receipt(state, item)
                                        for item in state.terminal_receipts))):
            return 'stale'
    if (_committed_child_start_replay(state, event, provider, session)
            or _committed_lead_event(state, event, provider, session)):
        return "stale"
    if _pre_run_unmanaged_claude_child(state, event, provider, session):
        return "stale"
    if event.payload.get("_symphony_owner_conflict"):
        return "hold"
    if event.kind not in {"subagent_started", "subagent_stopped"}:
        return "hold"
    identity = str(event.payload.get("agent_id") or event.payload.get("subagent_id") or "")
    if not identity:
        return "hold"
    run = state.active_runs.get(f"{provider}:{session}")
    if _committed_child_terminal_replay(state, event, provider, session, allow_active=True):
        return "stale"
    if _prior_child_terminal_conflict(state, event, provider, session):
        return "hold"
    if run is None:
        return "apply" if _fresh_fast_start(state, event, provider, session) else "hold"
    if run.started_at and event.observed_at < run.started_at:
        return "stale"
    if _run_scope(state, event, provider) != (f"{provider}:{session}", session):
        # One project file can contain several roots with the same child or
        # parent ID. Never ACK an event dispatch would reject or misroute.
        return "hold"
    parent = str(event.payload.get("parent_thread_id") or "")
    known = {run.session_id, run.lead_identity,
             *(item.identity for item in run.delegations)}
    if parent and parent not in known:
        return "hold"
    if not parent and str(event.payload.get("session_id") or "") not in known:
        return "hold"
    if identity in known:
        return "apply"
    if event.kind == "subagent_started":
        role = _observed_role(event.payload)
        pending = run.assessment.get("_pending_delegations", ())
        if any(isinstance(item, Mapping) and item.get("role") == role for item in pending):
            return "apply"
    return "hold"


def _pre_run_unmanaged_claude_child(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    """An unmarked root Agent call cannot own a run opened after its callback."""
    if provider != "claude" or event.kind not in {"subagent_started", "subagent_stopped"}:
        return False
    run = state.active_runs.get(f"{provider}:{session}")
    identity = str(event.payload.get("agent_id") or event.payload.get("subagent_id") or "")
    if (not run or not run.started_at or not identity
            or event.observed_at >= run.started_at
            or str(event.payload.get("session_id") or "") != session
            or event.payload.get("parent_thread_id") or _observed_role(event.payload)
            or str(event.payload.get("agent_type") or "").strip().lower()
            not in {"", "general-purpose"}
            or event.payload.get("role") or event.payload.get("task_name")
            or event.payload.get("_symphony_child_metadata")):
        return False
    if any(identity in {item.lead_identity, *(child.identity for child in item.delegations)}
           for item in (*state.active_runs.values(), *state.recent_runs)
           if item.provider == provider):
        return False
    return not any(item.get("provider") == provider and item.get("agent") == identity
                   for item in state.terminal_receipts)


def _committed_child_terminal_replay(
    state: ProjectState, event: Event, provider: str, session: str,
    *, allow_active: bool = False,
) -> bool:
    return _prior_child_terminal_disposition(
        state, event, provider, session, allow_active=allow_active) == "replay"


def _committed_child_start_replay(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    """An exact archived start is a retry, even if it arrived after archive."""
    return (event.kind == "subagent_started"
            and f"{provider}:{session}" not in state.active_runs
            and bool(event.payload.get("turn_id") or event.payload.get("prompt_id"))
            and any(
                item.provider == provider and item.session_id == session
                and event.event_id in item.assessment.get("_start_event_ids", ())
                for item in state.recent_runs
            ))


def _fresh_fast_start(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    """Only a new child with exact root lineage can open the next fast task."""
    identity = event.payload.get("agent_id") or event.payload.get("subagent_id")
    return (event.kind == "subagent_started" and _fast_spawn(event.payload) and bool(identity)
            and event.payload.get("provider") == provider
            and event.payload.get("session_id") == session
            and event.payload.get("parent_thread_id") == session
            and not any(run.provider == provider and identity in {
                run.session_id, run.lead_identity, *(item.identity for item in run.delegations)}
                for run in (*state.active_runs.values(), *state.recent_runs))
            and not any(item.get("provider") == provider
                        and identity in {item.get("agent"), item.get("lead"), item.get("session")}
                        for item in state.terminal_receipts))


def _archived_managed_child(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    """An old managed owner or role must exist before retaining a late child."""
    archived = tuple(run for run in state.recent_runs
                     if run.provider == provider and run.session_id == session)
    receipts = tuple(item for item in state.terminal_receipts
                     if item.get("provider") == provider and item.get("session") == session)
    if not archived and not receipts:
        return False
    identity = event.payload.get("agent_id") or event.payload.get("subagent_id")
    if _fresh_fast_start(state, event, provider, session):
        return False
    return bool(_observed_role(event.payload) in ROLES
                or identity and (any(identity in {run.lead_identity,
                                                   *(item.identity for item in run.delegations)}
                                     for run in archived)
                                 or any(item.get("agent") == identity for item in receipts)))


def _prior_child_terminal_conflict(
    state: ProjectState, event: Event, provider: str, session: str,
) -> bool:
    return _prior_child_terminal_disposition(
        state, event, provider, session, allow_active=True) == "conflict"


def _prior_child_terminal_disposition(
    state: ProjectState, event: Event, provider: str, session: str,
    *, allow_active: bool,
) -> str:
    """Classify old terminal evidence before it can change a newer run."""
    if (event.kind != "subagent_stopped" or event.payload.get("_symphony_owner_conflict")
            or (not allow_active and f"{provider}:{session}" in state.active_runs)):
        return "unknown"
    identity = str(event.payload.get("agent_id") or event.payload.get("subagent_id") or "")
    if not identity:
        return "unknown"
    parent = str(event.payload.get("parent_thread_id") or "")
    if (not parent and str(event.payload.get("session_id") or "") != session
            and not event.payload.get("_symphony_verified_alias")):
        return "unknown"
    if parent != session and parent and any(
        item.provider == provider and item.session_id != session
        and parent in {item.session_id, item.lead_identity,
                       *(child.identity for child in item.delegations)}
        for item in state.active_runs.values()
    ):
        return "unknown"
    canonical = replace(event, payload={**event.payload, "session_id": session})
    result_id = _terminal_result_id(canonical)
    token = _child_turn_token(canonical.payload)
    current = state.active_runs.get(f"{provider}:{session}")
    receipts = tuple(item for item in state.terminal_receipts
                     if item["provider"] == provider and item["session"] == session
                     and item["agent"] in {identity, "*"}
                     and (current is None or item["run_id"] != current.run_id)
                     and (not parent or parent in {session, item["parent"],
                                                   item["lead"], identity}))
    if any(item["result"] == result_id for item in receipts):
        return "replay"
    owned_receipts = tuple(item for item in receipts if item["agent"] == identity)
    if owned_receipts and (not token or any(item["turn"] == token for item in owned_receipts)):
        return "conflict"
    archived = tuple(run for run in state.recent_runs if
        run.provider == provider and run.session_id == session
        and (not parent or parent in {session, run.lead_identity,
                                      *(item.identity for item in run.delegations)})
        and any(item.identity == identity for item in run.delegations))
    if any(any(item == result_id or item.endswith(f":{result_id}")
               for item in run.assessment.get("_terminal_event_ids", ()))
           for run in archived):
        return "replay"
    if archived and (not token or any(token in run.assessment.get(
            "_terminal_turns", {}).get(identity, ()) for run in archived)):
        return "conflict"
    return "unknown"


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
        if source.kind == "subagent_started" and (_observed_role(source.payload) == "assessor"
                                                   or _fast_spawn(source.payload)):
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
            text = (
                f"Symphony retained the reviewed runtime at {retained}. "
                "If the loaded plugin cache disappears, use this retained root for Symphony references. "
            )
            if provider == "codex":
                command = _retained_activation_command(retained, original)
                if command:
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
            actions += (Action("inject_context", {"text": _recovery_guidance(state, provider)}),)
    elif source.kind == "pre_tool_use":
        state = _observe_sendmessage_intent(state, source, provider)
        blocked = _root_admission_guard(state, source, provider)
        if blocked:
            return state, actions + blocked
        state, delegation_actions = _prepare_delegation(state, source, provider)
        actions += delegation_actions
    elif source.kind in {"subagent_started", "subagent_stopped"}:
        if source.kind == "subagent_started":
            state, deferred = _consume_parent_actions(state)
            actions += deferred
        launches = state.active_run.assessment.get('_pending_delegations', ()) if state.active_run else ()
        launches = tuple(launches) if isinstance(launches, (list, tuple)) else ()
        state, observed_actions = _observe_delegation(state, source, environ)
        state = _consume_root_admission(state, source, provider, launches)
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
                state.active_run
                and state.active_run.lead_identity
                and state.active_run.lead_identity == str(payload.get("agent_id") or "")
            ):
                # SubagentStart context reaches the starting agent, not the root.
                actions += (Action("inject_context", {"text": _lead_guidance(state, provider)}),)
    elif source.kind in {"post_tool_use", "post_tool_failed"}:
        state = _discard_failed_spawn(state, source, provider)
        state, parent_actions = _consume_parent_actions(state)
        actions += parent_actions
    elif source.kind in {"stop_requested", "interrupt"}:
        if (source.kind == 'stop_requested' and not state.active_run
                and source.payload.get('hook_event_name') == 'UserPromptSubmit'
                and _parse_control(str(source.payload.get('prompt') or '')) == ('stop', '')):
            key = _root_admission_key(provider, source.payload)
            if key:
                state = _set_root_admission(state, key, source.payload, pending=False)
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
    targets = ("claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5")
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
            # Three probes leave 15 seconds of the 45-second hook budget for bookkeeping.
            capture_output=True, text=True, timeout=10,
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
    state = _root_admission_prompt(state, source, provider, control)
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
        else:
            actions += (Action('inject_context', {'text':
                'The enable control has already been applied. This control-only invocation is handled '
                'at the root; report its result. It creates no managed task: do not spawn a fast lead, '
                'assessor, lead or worker for the control.'}),)
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
        return _recovery_guidance(state, provider)
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


def _committed_sendmessage_source(state: ProjectState, source: Event,
                                  session: str, generation: int, project: Path,
                                  environ: Mapping[str, str]) -> bool:
    """ACK one exact committed callback without reopening its old lifecycle."""
    identity = source.payload.get('agent_id') or source.payload.get('subagent_id')
    owned = (source.payload.get('session_id') == session or (
        source.payload.get('session_id') == identity and (
            source.payload.get('parent_thread_id') == session
            or source.payload.get('_symphony_verified_alias') is True)))
    if (source.payload.get('provider') != 'claude' or not owned
            or source.payload.get('parent_thread_id') not in {None, '', session}
            or source.kind not in {'subagent_started', 'subagent_stopped'}):
        return False
    for run in (*state.active_runs.values(), *state.recent_runs):
        if run.provider != 'claude' or run.session_id != session:
            continue
        sequences = run.assessment.get('_claude_sendmessage_sequences', ())
        if not isinstance(sequences, (list, tuple)):
            continue
        for sequence in sequences:
            if (not isinstance(sequence, Mapping) or type(sequence.get('version')) is not int
                    or sequence['version'] != 1 or type(sequence.get('owner_generation')) is not int
                    or sequence.get('owner_generation') > run.owner_generation
                    or sequence.get('lead') != identity or not isinstance(sequence.get('sources'), (list, tuple))):
                continue
            try:
                proof = claude_archived_sendmessage_sequence(state, (), session, project, environ,
                    committed_run=run, committed_anchor=sequence)
            except (OSError, TypeError, ValueError, AttributeError):
                continue
            if proof is None or proof[3]['turns'] != sequence.get('turns'):
                continue
            terminal_turns = run.assessment.get('_terminal_turns', {})
            if not isinstance(terminal_turns, Mapping):
                continue
            valid = True
            for native in proof[1]:
                token = _child_turn_token(native.payload)
                result = _terminal_result_id(native)
                receipts = [receipt for receipt in state.terminal_receipts
                            if receipt.get('provider') == 'claude' and receipt.get('session') == session
                            and receipt.get('run_id') == run.run_id and receipt.get('agent') == identity
                            and receipt.get('turn') == token]
                metadata = {'native_agent_type': native.payload.get('agent_type'),
                            'native_model': native.payload.get('model'),
                            'native_effort': native.payload.get('model_reasoning_effort'),
                            'native_followup_start_id': native.event_id + ':followup-start'}
                if len(receipts) == 1:
                    receipt = receipts[0]
                    # v1.6 drops the new Start field. Surviving route fields
                    # and any supplied Start must still agree exactly.
                    if (any(key in receipt and receipt[key] != expected
                            for key, expected in metadata.items())
                            or ('native_fast_escalation' in receipt
                                and receipt['native_fast_escalation'] is not False)
                            or (receipt.get('native_launch_prompt_hash') not in {None, ''}
                                and receipt['native_launch_prompt_hash'] != hashlib.sha256(
                                    native.payload['prompt_id'].encode()).hexdigest())):
                        valid = False
                        break
                if (native.event_id + ':followup-start' not in run.assessment.get('_start_event_ids', ())
                        or result not in run.assessment.get('_terminal_event_ids', ())
                        or token not in terminal_turns.get(identity, ())
                        or len(receipts) != 1 or receipts[0].get('result') != result
                        or receipts[0].get('parent') != session or receipts[0].get('lead') != identity
                        or receipts[0].get('status') != 'completed'):
                    valid = False
                    break
            if not valid:
                continue
            for witness in sequence['sources']:
                if (not isinstance(witness, Mapping) or witness.get('event_id') != source.event_id
                        or witness.get('kind') != source.kind or type(witness.get('generation')) is not int
                        or witness.get('generation') != generation
                        or witness.get('observed_at') != source.observed_at
                        or witness.get('hash') != claude_sendmessage_source_hash(source, session)):
                    continue
                native = next((native for native in proof[1] if native.event_id == witness.get('native_event_id')), None)
                if (native is None or witness.get('turn') != _child_turn_token(native.payload)
                        or witness.get('result') != _terminal_result_id(native)):
                    continue
                start = witness.get('native_event_id', '') + ':followup-start'
                if (start not in run.assessment.get('_start_event_ids', ())
                        or witness.get('result') not in run.assessment.get('_terminal_event_ids', ())
                        or witness.get('turn') not in run.assessment.get('_terminal_turns', {}).get(identity, ())):
                    continue
                receipts = [receipt for receipt in state.terminal_receipts
                            if receipt.get('provider') == 'claude' and receipt.get('session') == session
                            and receipt.get('run_id') == run.run_id and receipt.get('agent') == identity
                            and receipt.get('turn') == witness.get('turn')]
                if (len(receipts) == 1 and receipts[0].get('result') == witness.get('result')
                        and receipts[0].get('parent') == session and receipts[0].get('lead') == identity
                        and receipts[0].get('status') == 'completed'):
                    return True
    return False


def _commit_sendmessage_sequence(state: ProjectState, sequence: tuple, sources: tuple[Event, ...],
                                 session: str, generation: int, dispatch,
                                 project: Path, environ: Mapping[str, str]) -> ProjectState | None:
    archived, natives, mapping, anchor = sequence
    original = state
    resumed = replace(archived, status='completing',
                      assessment={**archived.assessment, '_batch_pending': True})
    state = replace(state, active_run=resumed, active_runs={**state.active_runs, f'claude:{session}': resumed},
                    recent_runs=tuple(run for run in state.recent_runs if run != archived))
    witnesses = []
    for native in natives:
        started = Event(native.event_id + ':followup-start', 'subagent_started',
                        native.payload['_symphony_native_started_at'],
                        {**native.payload, 'status': 'working', 'task': archived.task})
        state, _ = dispatch(state, started)
        state, _ = dispatch(state, native)
        run = state.active_runs.get(f'claude:{session}')
        if not run or run.run_id != archived.run_id or run.lead_identity != archived.lead_identity:
            return None
    for source in sources:
        native = natives[mapping[source.event_id]]
        witnesses.append({'event_id': source.event_id, 'kind': source.kind,
                          'observed_at': source.observed_at, 'generation': generation,
                          'hash': claude_sendmessage_source_hash(source, session),
                          'native_event_id': native.event_id, 'turn': _child_turn_token(native.payload),
                          'result': _terminal_result_id(native)})
    run = state.active_runs[f'claude:{session}']
    sequences = run.assessment.get('_claude_sendmessage_sequences', ())
    if not isinstance(sequences, (list, tuple)):
        return None
    run = replace(run, assessment={**run.assessment,
        '_claude_sendmessage_sequences': (*sequences, {**anchor, 'sources': witnesses})})
    state = replace(state, active_run=run, active_runs={**state.active_runs, f'claude:{session}': run})
    # Validate all committed receipt/start/turn anchors before any inbox ACK.
    # Assessment uses the existing open codec; no callback text is persisted.
    if not all(_committed_sendmessage_source(state, event, session, generation, project, environ)
               for event in sources):
        return None
    key = _root_admission_key('claude', {'session_id': session})
    intents = original.configuration.get('root_admission_intents', {})
    intent = intents.get(key) if isinstance(intents, Mapping) else None
    if isinstance(intent, Mapping) and any(
            intent.get('continuation_call_hash') == turn['call_hash']
            and intent.get('continuation_message_hash') == turn['message_hash'] for turn in anchor['turns']):
        state = _set_root_admission(state, key, {}, pending=False)
    return state


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


def _child_turn_kind(payload: Mapping[str, object]) -> str:
    if 'turn_id' in payload:
        return 'turn_id'
    return 'prompt_id' if payload.get('prompt_id') else 'none'


def _substantive_turn_matches(provider: str, admitted: object, observed: str,
                             admitted_kind: object, observed_kind: str) -> bool:
    # Claude prompt_id correlates hook request context until the next user
    # prompt; it does not identify a child's native invocation. Only its
    # scoped native launch proof can bridge that context change. Explicit
    # native turns and every Codex token retain exact equality.
    if admitted_kind not in (None, 'none', 'prompt_id', 'turn_id'):
        return False
    if admitted == observed and not (admitted_kind is not None and admitted_kind != observed_kind
                                    and 'turn_id' in (admitted_kind, observed_kind)):
        return True
    return (provider == 'claude' and isinstance(admitted, str)
            and admitted_kind in ('none', 'prompt_id') and observed_kind in ('none', 'prompt_id')
            and all(not token or token.startswith('prompt_id:') for token in (admitted, observed)))


def _record_substantive_child(state: ProjectState, source: Event, role: str,
                              pending: Mapping, *, successful: bool,
                              environ: Mapping[str, str] | None = None) -> ProjectState:
    run = state.active_run
    if not run or not run.lead_identity:
        return state
    contract = run.assessment.get('substantive_contract')
    if (not isinstance(contract, Mapping) or type(contract.get('version')) is not int or contract['version'] != 1
            or not isinstance(contract.get('epoch'), str) or not contract['epoch']
            or not isinstance(contract.get('accepted_at'), str)
            or source.observed_at < contract['accepted_at']):
        return state
    identity = str(source.payload.get('agent_id') or source.payload.get('subagent_id') or '')
    scope = {'epoch': contract['epoch'], 'lead': run.lead_identity,
             'owner_generation': run.owner_generation, 'run_id': run.run_id}
    recorded = run.assessment.get('_substantive_children', {})
    proofs = dict(recorded) if isinstance(recorded, Mapping) else {}
    proof = proofs.get(identity)
    token = _child_turn_token(source.payload)
    token_kind = _child_turn_kind(source.payload)
    if source.kind == 'subagent_started':
        # A duplicate Start for this invocation cannot rewrite its admitted
        # role or parent. A genuinely new turn gets its own proof epoch.
        if (not isinstance(proof, Mapping) or not _substantive_turn_matches(
                run.provider, proof.get('turn'), token, proof.get('turn_kind'), token_kind)
                or any(proof.get(key) != value for key, value in scope.items())):
            proof = {**scope, 'turn': token, 'turn_kind': token_kind, 'role': role, 'start_event_id': source.event_id,
                     'admitted_at': source.observed_at,
                     'start_parent': str(source.payload.get('parent_thread_id') or ''),
                     'successful': False}
    elif (not isinstance(proof, Mapping) or any(proof.get(key) != value for key, value in scope.items())
          or not _substantive_turn_matches(run.provider, proof.get('turn'), token, proof.get('turn_kind'), token_kind)):
        # Role/model-only launch intents cannot identify this native child.
        return state
    binding = None
    consistent = (proof.get('role') == role and _observed_role(source.payload) in {'', role}
                  and proof.get('start_parent') in ('', run.lead_identity)
                  and source.payload.get('parent_thread_id') in (None, '', run.lead_identity))
    if consistent and run.provider == 'codex' and proof.get('start_parent') == run.lead_identity:
        binding = {'parent': run.lead_identity}
    elif consistent and run.provider == 'claude' and environ is not None:
        binding = claude_substantive_launch(run, source, role, proof['admitted_at'], environ)
        if binding and any(proof.get(key) is not None and proof.get(key) != value for key, value in binding.items()):
            binding = None
    proofs[identity] = {**proof, **(binding or {}),
                        'successful': successful and binding is not None}
    assessment = {**run.assessment, '_substantive_children': proofs}
    updated = replace(run, assessment=assessment)
    if _substantive_child_completed(updated):
        assessment.pop('_substantive_child_missing', None)
    return replace(state, active_run=updated)


def _observe_delegation(
    state: ProjectState, source: Event, environ: Mapping[str, str] | None = None,
) -> tuple[ProjectState, tuple[Action, ...]]:
    identity = source.payload.get("agent_id") or source.payload.get("subagent_id")
    if not identity:
        return state, ()
    opening: tuple[Action, ...] = ()
    if not state.active_run:
        fast = _fast_spawn(source.payload)
        if source.kind != "subagent_started" or (_observed_role(source.payload) != "assessor" and not fast):
            return state, ()
        state, opening = _open_run(
            state, source, (_tool_objective({"message": source.payload.get("task")}) if fast else
                            str(source.payload.get("task") or source.payload.get("objective") or ""))
        )
        if fast and state.active_run:
            selection = fast_lead_selection(_snapshot(state, str(source.payload.get("provider") or "codex")))
            state = _mark_fast_run(state, selection, str(source.payload.get("provider") or "codex"))
            if not selection["model"]:
                opening += (Action("inject_context", {"text":
                    "The fast lead floor is unavailable. Stop this tracked child and spawn an independent assessor."}),)
        if not state.active_run:
            return state, opening
    current = next(
        (item for item in state.active_run.delegations if item.identity == str(identity)),
        None,
    )
    assessment = state.active_run.assessment
    token = _child_turn_token(source.payload)
    terminal_matches_active_start = False
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
        if source.payload.get("provider") == "codex":
            active_turns = dict(assessment.get("_active_turns", {}))
            # An accepted start without a native turn ID is still a new epoch.
            # Never inherit the prior turn's identity for a later failure.
            active_turns[str(identity)] = token
            updated["_active_turns"] = active_turns
        if current and current.state.lower() not in {"working", "pending"}:
            epochs = dict(assessment.get("_terminal_epochs", {}))
            epochs[str(identity)] = epochs.get(str(identity), 0) + 1
            updated["_terminal_epochs"] = epochs
        state = replace(state, active_run=replace(state.active_run, assessment=updated))
    if source.kind == "subagent_stopped":
        # A terminal with a different native turn from the latest accepted
        # start cannot fail that working turn or authorize replacement.
        terminal_turns = assessment.get("_terminal_turns", {})
        active_turn = assessment.get("_active_turns", {}).get(str(identity))
        terminal_matches_active_start = bool(
            source.payload.get("provider") == "codex" and token and active_turn == token)
        if (source.payload.get("provider") == "codex" and current
                and str(identity) in assessment.get("_active_turns", {})
                and token != active_turn):
            if token not in terminal_turns.get(str(identity), ()):
                updated = dict(assessment)
                ambiguous = tuple(updated.get("_ambiguous_child_stops", ()))
                if str(identity) not in ambiguous:
                    updated["_ambiguous_child_stops"] = (*ambiguous, str(identity))
                    state = replace(state, active_run=replace(state.active_run, assessment=updated))
            return state, opening
        seen = assessment.get("_terminal_event_ids", ())
        epoch = assessment.get("_terminal_epochs", {}).get(str(identity), 0)
        base_result_id = _terminal_result_id(source)
        result_id = base_result_id
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
        receipt = {
            "provider": str(source.payload.get("provider") or ""),
            "session": str(source.payload.get("session_id") or ""),
            "agent": str(identity), "run_id": state.active_run.run_id,
            "turn": token, "result": base_result_id,
            "parent": str(source.payload.get("parent_thread_id") or ""),
            "lead": str(state.active_run.lead_identity or ""),
            "status": str(source.payload.get("status") or "completed").lower(),
        }
        if source.payload.get("_symphony_native_recovery"):
            receipt["native_agent_type"] = str(source.payload.get("agent_type") or "")
            receipt["native_model"] = str(source.payload.get("model") or "")
            receipt["native_effort"] = str(source.payload.get("model_reasoning_effort") or "")
            if source.payload.get('_symphony_archived_fast_escalation') is True:
                receipt['native_fast_escalation'] = True
            followup_start = f"{source.payload.get('_symphony_archived_native_event_id', source.event_id)}:followup-start"
            if followup_start in state.active_run.assessment.get("_start_event_ids", ()):
                receipt["native_followup_start_id"] = followup_start
            if (source.payload.get("provider") == "claude"
                    and str(identity) == state.active_run.lead_identity
                    and state.active_run.assessment.get("_claude_lead_start_identity") == str(identity)):
                receipt["native_launch_prompt_hash"] = str(
                    state.active_run.assessment.get("_claude_lead_start_prompt_hash") or "")
        state = replace(state, terminal_receipts=(*state.terminal_receipts, receipt))
        assessment = dict(state.active_run.assessment)
        assessment["_terminal_event_ids"] = (*assessment.get("_terminal_event_ids", ()),
                                              result_id)
        if source.payload.get("provider") == "codex":
            active_turns = dict(assessment.get("_active_turns", {}))
            if str(identity) in active_turns and active_turns[str(identity)] == token:
                active_turns.pop(str(identity), None)
                if active_turns:
                    assessment["_active_turns"] = active_turns
                else:
                    assessment.pop("_active_turns", None)
        ambiguous = tuple(item for item in assessment.get("_ambiguous_child_starts", ())
                          if item != str(identity))
        if ambiguous:
            assessment["_ambiguous_child_starts"] = ambiguous
        else:
            assessment.pop("_ambiguous_child_starts", None)
        ambiguous_stops = tuple(item for item in assessment.get("_ambiguous_child_stops", ())
                                if item != str(identity))
        if ambiguous_stops:
            assessment["_ambiguous_child_stops"] = ambiguous_stops
        else:
            assessment.pop("_ambiguous_child_stops", None)
        if token:
            terminal_turns = dict(assessment.get("_terminal_turns", {}))
            terminal_turns[str(identity)] = (*terminal_turns.get(str(identity), ()), token)
            assessment["_terminal_turns"] = terminal_turns
            if (role := _observed_role(source.payload)) == "lead" and source.payload.get(
                    "_symphony_native_recovery") and token:
                assessment["_claude_native_recovery"] = token
        state = replace(state, active_run=replace(state.active_run, assessment=assessment))
    terminal = source.kind == "subagent_stopped"
    status = str(source.payload.get("status") or ("completed" if terminal else "working"))
    role = str(pending.get("role") or _observed_role(source.payload) or (current.role if current else "worker"))
    retryable_replacement = (
        role == "lead" and source.kind == "subagent_started"
        and state.active_run.provider == "codex"
        and state.active_run.status == "recovering"
        and state.active_run.lead_identity
        and state.active_run.lead_identity != str(identity)
        and state.active_run.assessment.get("_retryable_lead")
            == state.active_run.lead_identity
        and state.active_run.assessment.get("_lead_route_mismatch_owner") != {
            "identity": state.active_run.lead_identity,
            "generation": state.active_run.owner_generation,
        }
    )
    unavailable_proof = None
    if retryable_replacement and environ is not None and current is None:
        try:
            unavailable_proof = codex_unavailable_lead_proof(
                state, state.active_run.session_id, environ, str(identity))
        except Exception:
            # A malformed or partial native transcript cannot transfer an
            # owner; the already-created child is still tracked below.
            unavailable_proof = None
    if current and current.role == "rejected_lead":
        role = "rejected_lead"
    elif (role == "lead" and source.kind == "subagent_started"
            and state.active_run.lead_identity
            and state.active_run.lead_identity != str(identity)
            and (state.active_run.status == "completing"
                 or (state.active_run.assessment.get("_fast_escalated")
                     and not state.active_run.assessment.get("size"))
                 or (retryable_replacement and unavailable_proof is None))):
        # A Codex SubagentStart can arrive without PreToolUse after the root
        # completed its lead, or while the original lead is retryable. The
        # child already exists: track it until terminal without transferring
        # the original lead's ownership or outcome.
        role = "rejected_lead"
        opening += (
            Action("reject_lead_replacement", {"identity": str(identity)}),
        )
    if role == "lead" and (source.kind == "subagent_started" or not state.active_run.lead_identity):
        if unavailable_proof is not None:
            assessment = dict(state.active_run.assessment)
            assessment["_codex_unavailable_proof"] = unavailable_proof
            state = replace(state, active_run=replace(state.active_run, assessment=assessment))
        owner_generation = state.active_run.owner_generation
        if (
            (state.active_run.status in {"interrupted", "recovering"}
             or state.active_run.assessment.get("_fast_escalated"))
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
                {"identity": str(identity), "owner_generation": owner_generation,
                 **({"safe_boundary": True} if state.active_run.assessment.get("_fast_escalated")
                    and state.active_run.assessment.get("size") else {}),
                 **({"unavailability_digest": unavailable_proof["digest"]}
                    if unavailable_proof is not None else {})},
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
            elif state.active_run.assessment.get("_fast_pending"):
                selected = "/".join(str(state.active_run.assessment.get("_fast_route", {}).get(key) or "")
                                    for key in ("model", "effort"))
            elif (session_profile is not None and state.active_run.assessment.get("size")
                  and state.active_run.assessment.get("complexity")):
                recorded = state.active_run.assessment
                route = route_for_recorded(recorded)
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
                    route = route_for_recorded(assessment)
                    drift = _route_drift(assessment, model, effort)
                    if drift and drift["weaker"] and _accepted_route(state, provider, session) != selected:
                        expected["approval_required"] = _drift_block(provider, drift).payload["reason"]
                    clamp = _clamp_actions(state, provider, route, session, resolved_profile)
                    blocked = next((item for item in clamp if item.kind == "block_tool"), None)
                    if blocked:
                        expected["approval_required"] = blocked.payload["reason"]
                assessment["_lead_expected_route"] = expected
                state = replace(state, active_run=replace(state.active_run, assessment=assessment))
        if (source.kind == "subagent_started" and source.payload.get("provider") == "claude"
                and state.active_run and state.active_run.lead_identity == str(identity)
                and isinstance(source.payload.get("prompt_id"), str)
                and source.payload["prompt_id"]):
            assessment = dict(state.active_run.assessment)
            assessment["_claude_lead_start_identity"] = str(identity)
            assessment["_claude_lead_start_prompt_hash"] = hashlib.sha256(
                source.payload["prompt_id"].encode()).hexdigest()
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
        if status.lower() not in {"completed", "done", "success", "succeeded"}:
            actions += (Action("inject_context", {"text":
                "The independent assessor did not complete successfully. Retry assessment before selecting a lead."}),)
        elif mismatch:
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
        state = _record_substantive_child(state, source, role, pending,
                                         successful=status.lower() in {"completed", "done", "success", "succeeded"} and bool(decisions),
                                         environ=environ)
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
    elif role == 'worker' or (role == 'consultant' and not terminal):
        state = _record_substantive_child(state, source, role, pending,
                                         successful=terminal and status.lower() in {"completed", "done", "success", "succeeded"},
                                         environ=environ)
    if (role == 'lead' and terminal and source.payload.get('_symphony_sendmessage_intermediate') is True
            and source.payload.get('_symphony_native_recovery') is True):
        # Full sequence proof precedes dispatch. Native end_turn is recorded
        # as a per-turn acknowledgment, never as an intermediate task outcome.
        return state, actions
    if role == "lead" and terminal and state.active_run:
        if str(identity) != state.active_run.lead_identity:
            return state, actions
        if state.active_run.assessment.get("_fast_escalated"):
            return state, actions + (Action("inject_context", {"text":
                "The fast lead already handed off. Await the independent assessment and replacement lead."}),)
        successful = status.lower() in {"completed", "done", "success", "succeeded"}
        outcome = {"status": status}
        report = str(source.payload.get("last_assistant_message") or "")
        fast_pending = bool(state.active_run.assessment.get("_fast_pending"))
        fast_continuation = bool(not fast_pending and source.payload.get('_symphony_native_recovery')
                                 and _archived_fast_owner(state.active_run))
        continuation_decisions = [line.strip().removeprefix('SYMPHONY_FAST_DECISION:').strip()
                                  for line in report.splitlines()
                                  if line.strip().startswith('SYMPHONY_FAST_DECISION:')] if fast_continuation else []
        decisions = [line.strip().removeprefix("SYMPHONY_FAST_DECISION:").strip()
                     for line in report.splitlines()
                     if line.strip().startswith("SYMPHONY_FAST_DECISION:")] if fast_pending else []
        fast_decision = decisions[0] if len(decisions) == 1 else ""
        if fast_pending and not state.active_run.assessment.get("_fast_route", {}).get("model"):
            successful = False
        fast_route = state.active_run.assessment.get("_fast_route", {}) if fast_pending else {}
        fast_observed = next((item for item in state.active_run.delegations
                              if item.identity == str(identity)), None)
        fast_verified = bool(fast_observed and fast_route.get("model")
                             and fast_observed.requested_tier == fast_route["model"]
                             and fast_observed.requested_effort == fast_route["effort"])
        fast_disallowed = bool(state.active_run.assessment.get("_fast_disallowed"))
        descendants = [item for item in state.active_run.delegations
                       if item.identity != str(identity)]
        unsettled = [item.identity for item in descendants
                     if item.state.lower() in _ACTIVE_STATES | {"interrupted"}]
        if (fast_pending and status.lower() in {"completed", "done", "success", "succeeded", "failed"}
                and (fast_disallowed or not fast_verified)
                and not unsettled):
            state = _escalate_fast_run(state, source)
            return state, actions + (Action("inject_context", {"text":
                "The fast lead was not authorized at its native route. Its result cannot complete "
                "the task; inspect any partial work and spawn an independent assessor."}),)
        if fast_pending and not fast_verified:
            successful = False
            actions += (Action("inject_context", {"text":
                "Fast lead route could not be verified at the capable/medium floor."}),)
        if fast_pending and (unsettled or (fast_decision == "eligible" and descendants)):
            successful = False
            actions += (Action("inject_context", {"text":
                "Fast lead cannot finish while descendants remain, and direct completion "
                "is invalid after any descendant: "
                + ", ".join(item.identity for item in descendants)}),)
        if fast_pending and successful and fast_verified and fast_decision == "escalate":
            state = _escalate_fast_run(state, source)
            return state, actions + (Action("inject_context", {"text":
                "Fast lead handed off. Inspect any partial work, then spawn an independent "
                "assessor for the full task."}),)
        if fast_pending and fast_decision != "eligible":
            successful = False
            actions += (Action("inject_context", {"text":
                "Fast lead decision missing or invalid. Recover the same lead's terminal decision; "
                "an assessor can start only after an explicit escalation."}),)
        outcome_lines = [line for line in report.splitlines() if line.strip().startswith("SYMPHONY_OUTCOME:")]
        if outcome_lines:
            try:
                if len(outcome_lines) != 1:
                    raise ValueError('multiple outcome reports')
                reported = json.loads(outcome_lines[0].strip().removeprefix("SYMPHONY_OUTCOME:").strip())
                reported_status = reported.get("status") if isinstance(reported, Mapping) else None
                if not isinstance(reported_status, str) or not reported_status:
                    raise ValueError("outcome status missing")
                outcome = {"status": reported_status}
                successful = successful and reported_status.lower() in {"completed", "done", "success", "succeeded"}
            except (TypeError, ValueError):
                successful = False
                actions += (Action("inject_context", {"text":
                    "The lead ended with a malformed or duplicate SYMPHONY_OUTCOME report. Reconcile its result or retry the lead; the run remains recoverable."}),)
        elif fast_pending:
            successful = False
            actions += (Action("inject_context", {"text":
                "Fast lead completion requires an explicit SYMPHONY_OUTCOME report."}),)
        if fast_pending and len(outcome_lines) != 1:
            successful = False
        if continuation_decisions and (len(continuation_decisions) != 1
                                       or continuation_decisions[0] not in {'eligible', 'escalate'}):
            successful = False
        if fast_pending and successful and fast_decision == "eligible":
            selected = state.active_run.assessment["_fast_route"]
            accepted = dict(state.active_run.assessment)
            accepted.pop("_fast_pending", None)
            accepted.update({"size": "small", "complexity": "simple", "risk": "normal",
                             "topology": "direct", "rationale": "fast lead reported predetermined mechanical work",
                             "route": {"lead_model": selected["model"],
                                       "lead_effort": selected["effort"],
                                       "profile": _applied_profile(state, str(source.payload.get("provider") or "codex"))}})
            state = replace(state, active_run=replace(state.active_run, assessment=accepted))
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
            updated_assessment["_lead_route_mismatch_owner"] = {
                "identity": str(identity),
                "generation": state.active_run.owner_generation,
            }
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
            updated_assessment.pop("_lead_route_mismatch_owner", None)
            state = replace(state, active_run=replace(state.active_run, assessment=updated_assessment))
        if (successful and continuation_decisions == ['escalate'] and
                (len(outcome_lines) == 1 or
                 source.payload.get('_symphony_archived_fast_escalation') is True and not outcome_lines)):
            state = _escalate_fast_run(state, source, fresh_assessment=True)
            return state, actions + (Action('inject_context', {'text':
                'The archived fast lead escalated this continuation. Its old direct route cannot complete '
                'substantive work. Spawn an independent assessor for the full current objective, then the '
                'new matrix-selected lead; preserve lifecycle ownership while reassessing.'}),)
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
        if successful and not _substantive_child_completed(state.active_run):
            assessment = dict(state.active_run.assessment)
            assessment['_substantive_child_missing'] = True
            state = replace(state, active_run=replace(state.active_run, assessment=assessment))
            successful = False
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
                    **({'reason': 'substantive_child_missing'} if completion_kind == 'lead_failed'
                       and state.active_run.assessment.get('_substantive_child_missing') else {}),
                    **({"turn_token": token} if completion_kind == "lead_failed" and token else {}),
                    **({"native_host_failed": True} if completion_kind == "lead_failed"
                        and terminal_matches_active_start
                        and status.lower() in {"failed", "interrupted", "cancelled", "canceled", "error", "terminated"}
                        else {}),
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


def _mark_fast_run(state: ProjectState, selection: Mapping[str, str], provider: str) -> ProjectState:
    run = state.active_run
    if not run:
        return state
    assessment = {**run.assessment, "_fast_pending": True,
                  "_fast_route": dict(selection),
                  "_lead_expected_route": dict(selection)}
    if _boost_preference(state, provider, run.session_id) != "off" or not selection["model"]:
        assessment["_fast_disallowed"] = True
    return replace(state, active_run=replace(run, assessment=assessment))


def _escalate_fast_run(state: ProjectState, source: Event, *, fresh_assessment: bool = False) -> ProjectState:
    run = state.active_run
    if not run:
        return state
    assessment = dict(run.assessment)
    if fresh_assessment:
        for key in ('size', 'complexity', 'risk', 'rationale', 'topology', 'route',
                    'substantive_contract', '_substantive_children', '_substantive_child_missing'):
            assessment.pop(key, None)
        if source.payload.get('_symphony_archived_fast_escalation') is True:
            assessment['_archived_fast_escalation_turn'] = _child_turn_token(source.payload)
    assessment.pop("_fast_pending", None)
    assessment["_fast_escalated"] = True
    assessment.pop("_lead_expected_route", None)
    return replace(state, active_run=replace(
        run, status="assessing", assessment=assessment, outcome=None,
        updated_at=source.observed_at,
    ))


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


def _fast_spawn(payload: Mapping[str, object]) -> bool:
    if _observed_role(payload) != "lead":
        return False
    if _marker_value(payload, "SYMPHONY_FAST_ROUTE:") == "lead":
        return True
    # Codex's collaboration path may emit only a native Start, with the
    # packet unavailable to PreToolUse. The explicit host task name carries
    # the route; it is a role label, not a task-content classifier.
    return (payload.get("provider") == "codex"
            and str(payload.get("task_name") or "").startswith("symphony_lead_fast_"))


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
    fast_launch = role == "lead" and _marker_value(values, "SYMPHONY_FAST_ROUTE:") == "lead"
    if not state.active_run and role != "assessor" and not fast_launch:
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
    if role == "assessor" and state.active_run and state.active_run.assessment.get("_fast_pending"):
        return state, (_block_tool("Wait for the fast lead's terminal decision before spawning an assessor."),)
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
    if fast_launch:
        if provider == "codex" and isinstance(values, Mapping) and not str(values.get("task_name") or "").startswith("symphony_lead_fast_"):
            return state, (_block_tool("Name the Codex fast lead `symphony_lead_fast_<model>_<effort>`."),)
        if _boost_preference(state, provider, str(source.payload.get("session_id") or "")) != "off":
            return state, (_block_tool("An assessor boost is active; spawn the selected boosted assessor first."),)
        if state.active_run:
            return state, (_block_tool("A run already owns this task; continue its recorded route."),)
        selection = fast_lead_selection(_snapshot(state, provider))
        if not selection["model"]:
            return state, (_block_tool("The capable/medium fast lead floor is unavailable; spawn an independent assessor."),)
        if (model, effort) != (selection["model"], selection["effort"]):
            return state, (_block_tool(
                f"Fast lead route mismatch: expected {selection['model']}/{selection['effort']}."
            ),)
        state, actions = _open_run(state, source, objective)
        state = _mark_fast_run(state, selection, provider)
    if role == "assessor" and not state.active_run:
        state, actions = _open_run(state, source, objective)
    if role == "lead":
        if state.active_run.assessment.get("_fast_pending") and not fast_launch:
            return state, (_block_tool("Recover the fast lead's decision before another lead can start."),)
        if state.active_run.status == "completing" and state.active_run.lead_identity:
            return state, (_block_tool(
                "The tracked lead has already completed this run. Invoke the normal "
                f"`{_control_name('stop', provider)}` control to verify and archive it "
                "before starting another task."
            ),)
        if state.active_run.assessment.get("_fast_escalated") and not state.active_run.assessment.get("size"):
            return state, (_block_tool("The fast lead escalated. Wait for an independent accepted assessment before another lead starts."),)
        if state.active_run.assessment.get("_boost_assessment_pending"):
            return state, (_block_tool("A valid boosted assessor result is required before selecting the lead."),)
        assessment = _assessment_from_marker(values, "SYMPHONY_ROUTE:") if not fast_launch else None
        if assessment is None and not fast_launch:
            return state, (_block_tool("Add a valid SYMPHONY_ROUTE JSON line to the lead packet, then retry."),)
        recorded = state.active_run.assessment
        if fast_launch:
            route = None
            required_model, required_effort = model, effort
        elif recorded.get("size") and recorded.get("complexity"):
            if (
                assessment.size != recorded.get("size")
                or assessment.complexity != recorded.get("complexity")
            ):
                return state, (
                    _block_tool("Use the accepted Symphony size/complexity route for this lead."),
                )
            route = route_for_recorded(recorded)
        else:
            route = route_for(assessment)
        spawn_session = str(source.payload.get("session_id") or "")
        if not fast_launch:
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
        clamp = () if fast_launch else _clamp_actions(state, provider, route, spawn_session)
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
        if not fast_launch and not (recorded.get("size") and recorded.get("complexity")):
            state, accepted = _accept_assessment(state, source, provider, values, assessment)
            actions += accepted
    elif role in {"worker", "consultant"} and (state.active_run.assessment.get("_fast_pending")
                                                or state.active_run.assessment.get("_fast_escalated")):
        return state, (_block_tool("Wait for the fast decision and, if escalated, its assessed replacement lead before spawning descendants."),)
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
    if provider == 'claude' and fast_launch:
        launch_id = source.payload.get('tool_use_id')
        if isinstance(launch_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', launch_id):
            assessment = {**state.active_run.assessment,
                          '_claude_fast_launch_hash': hashlib.sha256(launch_id.encode()).hexdigest()}
            state = replace(state, active_run=replace(state.active_run, assessment=assessment))
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
            + f". Run `{control}` to accept the weaker route and continue this "
            "session. Model availability is checked again in a new session."
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
    route = route_for_recorded(recorded)
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
        "topology": route.execution,
        "route": route_data,
    }
    if route.execution in {'delegated', 'mixed'}:
        accepted['substantive_contract'] = {'version': 1, 'epoch': source.event_id,
                                            'accepted_at': source.observed_at}
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
    elif reason == 'substantive_child_missing':
        reason = 'resume the SAME registered lead to delegate substantive work, integrate and verify its result, then report completion'
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
        elif action.kind == 'request_substantive_work':
            rendered.append(Action('inject_context', {'text': _substantive_recovery_guidance(state.active_run, provider)}))
        elif action.kind == "block_stop":
            text = _stop_block_text(action.payload, provider, scope)
            if (state.active_run and state.active_run.assessment.get('_fast_escalated')
                    and not state.active_run.assessment.get('size')):
                text += (' Request a fresh independent assessment of the full current objective, '
                         'then launch its matrix-selected lead; the old direct route cannot complete it.')
            rendered.append(
                Action("inject_context", {"text": text})
                if prompt_originated
                else Action("block_stop", {"reason": text})
            )
        elif action.kind == 'permit_stop' and action.payload.get('reason') and not prompt_originated:
            reason = action.payload['reason']
            if isinstance(reason, Mapping):
                status = '/symphony:status' if provider == 'claude' else '$symphony:symphony status'
                reason = (f'Symphony released this repeated host Stop for {scope}; unfinished work remains open. '
                          f'Inspect `{status}` on the next turn, reconcile the tracked agents and evidence, '
                          'and confirm durable completion before reporting success.')
            rendered.append(Action('permit_stop', {'reason': reason}))
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
            rendered.append(Action("inject_context", {"text": "Symphony is enabled for this project; hooks are guarded. "
                "Enabling alone creates no managed task or child spawn; route an accompanying task only through its separate guidance."}))
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
            rendered.append(Action("inject_context", {"text": _recovery_guidance(state, provider)}))
        elif action.kind == "preserve_recovery_context":
            rendered.append(Action("inject_context", {"text": "Symphony recorded the interruption for safe reconciliation on resume."}))
        elif action.kind == "stop_delegations":
            identities = ", ".join(map(str, action.payload.get("active", ())))
            rendered.append(Action("inject_context", {"text": f"Stop these tracked Symphony agents and verify their host status: {identities}."}))
        elif action.kind == "replace_lead":
            run = state.active_run
            if (provider == "codex" and run and run.lead_identity
                    and run.assessment.get("_retryable_lead") == run.lead_identity):
                mismatch_owner = run.assessment.get("_lead_route_mismatch_owner") == {
                    "identity": run.lead_identity, "generation": run.owner_generation}
                rendered.append(Action("inject_context", {"text": (
                    "The registered lead used the wrong route. Spawn one replacement with the recorded "
                    "model and effort; the prior lead cannot change its native route."
                    if mismatch_owner else
                    "The registered lead ended a retryable turn. Continue that same lead first with "
                    "followup_task using the original task_name from spawn_agent (lowercase letters, "
                    "digits, and underscores), not a /root/ path or agent UUID. A completed host roster "
                    "entry alone does not prove the agent unavailable; do not call spawn_agent for the same "
                    "task_name. Await the original lead's new result, then finish this root turn so native Stop "
                    "can reconcile its actual outcome. A missing marker in the returned text is not permission "
                    "to spawn another lead. Only if followup_task returns an "
                    "explicit unavailable error may you spawn one replacement at the recorded route; "
                    "Symphony verifies the original spawn, exact error, and turn lineage before accepting it."
                )}))
            else:
                rendered.append(Action("inject_context", {"text":
                    "The observed lead is unavailable. Spawn one safe replacement at the recorded owner generation."}))
        elif action.kind == "route_run":
            route = state.active_run.assessment.get("route", {}) if state.active_run else {}
            model, effort = _required_lead_route(state.active_run.assessment) if state.active_run else ("", "")
            profile = route.get("profile", "") if isinstance(route, Mapping) else ""
            agent = f" as `symphony:symphony-lead-{model}-{effort}`" if provider == "claude" and model else ""
            rendered.append(Action("inject_context", {"text":
                f"Symphony accepted the assessed route. Selected {provider} lead"
                f" ({profile} profile): {model}/{effort}. Spawn only this model and effort{agent} "
                "with the accepted SYMPHONY_ROUTE marker; keep the root thin. "
                f"Accepted topology: {route.get('execution', '')}. Relay this lead contract: "
                "assign substantive implementation, diagnosis, design, review tasks, and product judgment to workers "
                "or consultants; small tasks need one worker, medium tasks need bounded worker packets, "
                "and large tasks delegate project work. The lead coordinates, reviews integration, and verifies results. "
                "Use the matrix for each child packet's own size/complexity. After verification and all children "
                "have returned, native successful assessed completion is sufficient. If supplied, a `SYMPHONY_OUTCOME:` "
                "report must be one valid JSON line; use blocked or failed when work remains."}))
        elif action.kind == "reject_lead_replacement":
            lead = state.active_run.lead_identity if state.active_run else "the registered lead"
            rendered.append(Action("inject_context", {"text":
                f"Symphony refused to register {action.payload.get('identity')} as lead: this run already "
                f"has one ({lead}). Stop the extra agent and let the registered lead finish. "
                "The extra child is tracked until it ends but cannot replace the accepted lead or outcome."}))
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


def _provider_cells(snapshot, role: str) -> list[str]:
    """Exact provider selections, including risk floors, for each matrix cell."""
    cells = []
    for size in ("small", "medium", "large"):
        for complexity in ("simple", "mixed", "complex"):
            normal, high = (
                resolve_tier(route_for(Assessment(size, complexity, risk)), snapshot)
                for risk in ("normal", "high")
            )
            def label(choice):
                return (f"symphony:symphony-{role}-{choice['lead_model']}-{choice['lead_effort']}"
                        if snapshot.provider == "claude" else f"{choice['lead_model']}/{choice['lead_effort']}")
            cell = f"{size}/{complexity} `{label(normal)}`"
            if (high["lead_model"], high["lead_effort"]) != (normal["lead_model"], normal["lead_effort"]):
                cell += f" (high risk: `{label(high)}`)"
            cells.append(cell)
    return cells


def _lead_guidance(state: ProjectState, provider: str) -> str:
    """Give the starting lead its children's exact provider spawn contract."""
    if state.active_run and (state.active_run.assessment.get('_fast_pending')
                             or _archived_fast_owner(state.active_run)):
        return (
            'You are the registered Symphony fast lead. Before any changes, decide whether the WHOLE '
            'objective consists only of predetermined mechanical steps with an expected result, bounded '
            'scope, clear requirements, low risk, available required tools, and concrete verification. '
            'A brief request or supplied command alone does not establish eligibility. Implementation, '
            'diagnosis, design, substantive review, product judgment, mixed work, or uncertainty requires '
            'escalation before any changes, including tiny features and run-and-fix objectives. '
            'Do not spawn workers, consultants, assessors, or any other descendants. '
            'If eligible, perform the mechanical steps, verify the result, and return exactly one '
            '`SYMPHONY_FAST_DECISION: eligible` line and one valid '
            '`SYMPHONY_OUTCOME: {"status":"completed"}` line. Otherwise make no changes and return '
            'exactly one `SYMPHONY_FAST_DECISION: escalate` line without a completion outcome. '
            'The root then requests independent assessment. A resumed fast turn only reconciles the same '
            'bounded mechanical task; a new or substantive objective requires fresh assessment before changes.'
        )
    snapshot = _snapshot(state, provider)
    protocol = (
        'Every worker spawn must pass explicit `model` and `reasoning_effort`, `fork_turns="none"`, '
        'and task name `symphony_worker_<model>_<effort>` using underscores for model punctuation. '
        'Put `SYMPHONY_ROLE: worker` on the first line of its bounded objective/ownership/evidence/'
        'constraints/acceptance_check/return_contract/size/complexity packet. '
        if provider == "codex" else
        'Every worker spawn uses its exact packaged agent type and a packet beginning `SYMPHONY_ROLE: worker`. '
    )
    return (
        "For assessed work, assign substantive work to workers or consultants; small tasks need one worker, "
        "medium tasks need bounded worker packets, and large tasks delegate project work. "
        "The lead coordinates, integrates, and verifies results. "
        + protocol + "Symphony worker routes by the packet's own size/complexity: "
        + "; ".join(_provider_cells(snapshot, "worker"))
        + (f". Consultants use `symphony:symphony-consultant-{snapshot.tiers['strongest']}-high`."
           if provider == "claude" else
           f". Consultants use explicit model={snapshot.tiers['strongest']}, reasoning_effort=high, "
           'fork_turns="none", SYMPHONY_ROLE: consultant, and SYMPHONY_DECISION JSON.')
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
    cells = _provider_cells(snapshot, "lead")
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
        fast = fast_lead_selection(_snapshot(state, provider))
        if (fast["model"] and _boost_preference(state, provider, session_id) == "off"
                and not (state.active_run and state.active_run.assessment.get("_fast_escalated"))):
            fast_name = f"symphony_lead_fast_{re.sub(r'[^a-z0-9]', '_', fast['model'])}_{fast['effort']}"
            spawn = (f"Use agent type `symphony:symphony-lead-{fast['model']}-{fast['effort']}` "
                     "and keep `SYMPHONY_FAST_ROUTE: lead` in its Agent prompt; the packaged type alone "
                     "does not identify a fast launch. "
                     if provider == "claude" else
                     f"Pass `model=\"{fast['model']}\"`, `reasoning_effort=\"{fast['effort']}\"`, "
                     f"`fork_turns=\"none\"`, and `task_name=\"{fast_name}\"` exactly. "
                     "The initial fast lead must keep this reserved fast name. Generic assessed-lead "
                     "names below apply only after escalation; never use them for this first spawn. ")
            return (
                "Symphony fast route: the root is a courier. Spawn one capable lead at "
                f"{fast['model']}/{fast['effort']} with `SYMPHONY_ROLE: lead` and "
                "`SYMPHONY_FAST_ROUTE: lead` on separate lines. " + spawn +
                "Relay the entire task and its acceptance checks. Before any changes, the lead must "
                "decide whether the WHOLE objective consists only of predetermined mechanical steps "
                "with an expected result, scope bounded, requirements clear, risk low, required tools "
                "(including browser or computer control when needed) available, and verification concrete. "
                "Eligible examples: run a supplied bash/git command and report its result, or read a specified "
                "browser page through known steps. Implementation, diagnosis, design, substantive review, "
                "product judgment, mixed work, or uncertainty requires escalation before any changes, "
                "even for a tiny feature. A run-and-fix request escalates as a whole. "
                "A tool name, short task, or supplied command alone does not establish eligibility. "
                "Only an eligible mechanical objective runs directly and finishes with exact lines "
                "`SYMPHONY_FAST_DECISION: eligible` and `SYMPHONY_OUTCOME: {\"status\":\"completed\"}`. "
                "If any check fails or is uncertain, make no changes and finish with exact line "
                "`SYMPHONY_FAST_DECISION: escalate`; then spawn an independent strongest/high assessor "
                "for the original task, followed by the matrix-selected lead. "
                "The fast lead cannot spawn descendants before deciding. Preserve the same run, "
                "wait for native terminal results, and use normal Stop reconciliation. "
                + ("Claude agents run in the background; end your turn after spawning and wait for the host result. "
                   if provider == "claude" else "")
                + "Only after the fast lead's native escalation result, follow this assessment and delegation contract: "
                + _assessed_guidance(task, provider, state, session_id)
            )
    return _assessed_guidance(task, provider, state, session_id)


def _assessed_guidance(task: str, provider: str, state: ProjectState | None, session_id: str) -> str:
    """The same assessor/lead packet contract for escalation and direct assessment."""
    claude = _claude_guidance(state, session_id) if provider == "claude" else ""
    codex = (
        "On Codex, use `fork_turns=\"none\"` for assessor and assessed lead, name them "
        "`symphony_<role>_<model>_<effort>`, and require the assessor's final response to contain one exact "
        "`SYMPHONY_ASSESSMENT: {\"size\":\"small|medium|large\",\"complexity\":\"simple|mixed|complex\",\"risk\":\"normal|high\","
        "\"rationale\":\"...\",\"topology\":\"...\"}` line. "
        if provider == "codex"
        else ""
    )
    if provider == "codex":
        snapshot = _snapshot(state, provider) if state else snapshot_for(provider)
        codex += ("Profile cell choices override abstract tiers. Select the assessed lead and each worker "
                  "from these exact cells; never reuse the fast lead selection: "
                  + "; ".join(_provider_cells(snapshot, "lead")) + ". ")
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
        'Relay the child spawn protocol in the lead packet: on Codex every worker uses `fork_turns="none"`, '
        'explicit model/reasoning_effort from its own cell, underscore `symphony_worker_<model>_<effort>` '
        'task name, and a packet whose first line is `SYMPHONY_ROLE: worker`. '
        "The matrix fixes execution topology; the assessor's topology is advisory. Relay this lead contract: "
        "assign substantive work to workers or consultants; small tasks need one worker, medium tasks need "
        "bounded worker packets, and large tasks delegate project work. The lead coordinates, integrates, "
        "and verifies results. Use the matrix for each child packet's own size/complexity. "
        "After verification and all children have returned, native successful assessed completion is sufficient. "
        "If supplied, a `SYMPHONY_OUTCOME:` report must be one valid JSON line; use blocked or failed when work remains. "
        "Archived followup only reconciles the same bounded task. A new or substantive objective starts a fresh "
        "assessment and delegation scope; never credit previous-task workers to it. "
        "Relay the task in full: the lead cannot see this conversation, so if the request has several parts, "
        "every part goes in the packet and the acceptance check covers all of them. "
        "The assessor packet must require the exact size/complexity/risk vocabulary above and explain that "
        "substantive small work uses one worker; medium work uses bounded worker packets. "
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


def _claude_native_stop_guard(source: Event, reason: str) -> tuple[Action, ...]:
    if (source.payload.get('provider') == 'claude'
            and source.kind == 'stop_requested' and source.payload.get('hook_event_name') == 'Stop'
            and source.payload.get('stop_hook_active') is True):
        # Release this host turn without treating unverifiable native work as
        # completed. Dispatching Stop here could archive the older outcome.
        return (Action('permit_stop', {'reason': reason +
            ' This repeated Stop releases the host turn only; the run and unresolved evidence remain open.'}),)
    return (Action('block_stop', {'reason': reason}),)


def _claude_unknown_turn_guidance(run: RunState) -> str:
    lead = run.lead_identity
    repair = (
        'If it has ended without a standalone SYMPHONY_OUTCOME marker, make at most one native '
        f'SendMessage repair request to `to: {lead}` for its actual final result and exact marker. '
        if run.assessment.get('_fast_pending') or _archived_fast_owner(run) else
        f'If its actual final result is unavailable, make at most one native SendMessage request to `to: {lead}` '
        'for that result. Ordinary assessed completion accepts successful native terminal status without '
        'an outcome marker; an archived continuation retains its stricter marker contract. ')
    return (
        "Symphony could not verify the tracked lead's latest native Claude turn. "
        f"Inspect the latest result for original lead `{lead}`. " + repair +
        "Await that same agent's response, then invoke normal `/symphony:stop` once. "
        "If this repair was already attempted or its response is still unverifiable, "
        "do not repeat SendMessage or Stop in this turn. Report the unresolved result "
        "and ask for direction while keeping this run open. Do not start a new lead "
        "or force stop."
    )


def _completion_ready_guidance(run: RunState, provider: str) -> str:
    if run.status != "completing" or _stop_block_reason(run) is not None:
        return ""
    host = provider or run.provider or "codex"
    if host == "codex":
        return (
            "The lead outcome and tracked work are reconciled. Finish this root turn "
            "now so the native Stop hook can verify and archive the run, then check "
            "durable status. Do not type a stop control as assistant prose, call "
            "list_agents, or spawn or follow up a completed lead solely because this "
            "run remains completing."
        )
    stop = _control_name("stop", host)
    return (
        "The lead outcome and tracked work are reconciled. Invoke the normal "
        f"`{stop}` control in this same session now, then check durable status. "
        "The Stop guard verifies native freshness before archiving. Do not follow up "
        "or replace a completed lead solely because this run remains completing."
    )


def _refresh_completion_guidance(
    actions: tuple[Action, ...], before: ProjectState, after: ProjectState,
    source: Event, provider: str,
) -> tuple[Action, ...]:
    """Render guidance from the committed batch, after its temporary archive hold clears."""
    if not after.active_run or not _completion_ready_guidance(after.active_run, provider):
        return actions
    if not before.active_run or _completion_ready_guidance(before.active_run, provider):
        return actions
    if source.kind == "session_heartbeat":
        old, new = _recovery_guidance(before, provider), _recovery_guidance(after, provider)
    elif source.kind == "user_prompt":
        prompt = str(source.payload.get("prompt") or "").strip()
        control = _parse_control(prompt)
        if control and control[0] in {"status", "agents"}:
            history = control[0] == "agents" and control[1] == "--all"
            session = str(source.payload.get("session_id") or "")
            old = _status(before, history, provider, session)
            new = _status(after, history, provider, session)
        elif control is None or control[0] == "start":
            old, new = _recovery_guidance(before, provider), _recovery_guidance(after, provider)
        else:
            return actions
    else:
        return actions
    return tuple(
        Action("inject_context", {**action.payload, "text": new})
        if action.kind == "inject_context" and action.payload.get("text") == old else action
        for action in actions
    )


def _substantive_recovery_guidance(run: RunState | None, provider: str) -> str:
    native = ('Use followup_task with the original underscore task_name, not its /root/ path or UUID. '
              if provider == 'codex' else
              'Use SendMessage to resume that same agent. Claude scoped credit requires a fresh worker or '
              'consultant with one native prompt and an exact Agent launch by this lead. Preserve reused '
              'child history, repair any invalid consultant decisions, then launch a fresh bounded child. ')
    return ('Resume the SAME registered lead; this assessed route needs successful substantive worker '
            'or classified consultant evidence from the current assessment and owner generation. '
            + native + 'Reconcile a verifiable current-scope worker Start, or launch a fresh bounded worker '
            'whose Start and successful terminal are observed. Integrate and verify its result, then let '
            'that lead report completion again. Preserve this run and ownership.')


def _recovery_guidance(state: ProjectState, provider: str = "") -> str:
    run = state.active_run
    if run and run.assessment.get('_substantive_child_missing'):
        return _substantive_recovery_guidance(run, provider)
    if not run:
        return "Symphony has no active run."
    if run.assessment.get("_fast_escalated") and not run.assessment.get("size"):
        assessor = assessor_selection(_snapshot(state, provider),
                                      _boost_preference(state, provider, run.session_id))
        return (f"Fast lead {run.lead_identity} escalated this run before writes. "
                f"Spawn one independent assessor at {assessor['model']}/{assessor['effort']} "
                "with `SYMPHONY_ROLE: assessor` and the full original task. Await its valid "
                "SYMPHONY_ASSESSMENT result before spawning the matrix-selected replacement lead. "
                "Preserve this run and reconcile tracked children before Stop.")
    completion = _completion_ready_guidance(run, provider)
    if completion:
        return f"Symphony run {run.run_id} remains completing. {completion}"
    records = ", ".join(f"{item.role} {item.identity} ({item.state})" for item in run.delegations)
    pending = run.assessment.get("_pending_delegations", ())
    awaiting = ", ".join(str(item.get("role") or "agent") for item in pending if isinstance(item, Mapping))
    lead = f" Lead {run.lead_identity} [{_governance(state)}]." if run.lead_identity else " Lead not yet observed."
    route_mismatch = run.assessment.get("_lead_route_mismatch_owner") == {
        "identity": run.lead_identity, "generation": run.owner_generation}
    retry = ("For this retryable Codex lead, call followup_task on the original spawn_agent "
             "task_name (lowercase letters, digits, and underscores), not its /root/ path or UUID. "
             "A completed host roster entry alone does not prove the agent unavailable; do not spawn "
             "another lead for that name. After its result, finish this root turn so native Stop can "
             "reconcile the actual outcome; a missing text marker alone never authorizes replacement. "
             "Only an explicit unavailable error from followup_task can "
             "authorize one replacement; Symphony verifies the original spawn, exact error, and native "
             "turn lineage at the new child's Start. If verification fails, the extra child stays rejected. "
             if run.provider == "codex" and run.lead_identity
             and run.assessment.get("_retryable_lead") == run.lead_identity
             and not route_mismatch else "")
    if route_mismatch:
        retry = "The registered lead used the wrong native route; spawn one replacement at the required model and effort. "
    claude_retry = (
        f"For this Claude lead, await an active background result. If its native turn has ended "
        f"without a reconciled outcome, use SendMessage with `to: {run.lead_identity}` to resume "
        "that same agent and ask it to report its observed result with one exact "
        "SYMPHONY_OUTCOME JSON line. Await its returned result before normal Stop. "
        "Do not start another task or invent a missing outcome. "
        if run.provider == "claude" and run.lead_identity and run.status in {"active", "recovering"}
        and not route_mismatch
        and any(item.identity == run.lead_identity and item.state in {"working", "pending", "failed"}
                for item in run.delegations) else ""
    )
    return (
        f"Symphony run {run.run_id} remains {run.status}.{lead} "
        f"Observed agents: {records or 'none'}. "
        + (f"Awaiting host launch confirmation: {awaiting}. " if awaiting else "")
        + retry + claude_retry + "Reconcile returned or interrupted results, preserve ownership, and continue unfinished work. "
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
        topology = route.get("execution") or assessment.get("topology")
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
        completion = _completion_ready_guidance(state.active_run, provider)
        if completion:
            lines.append(completion)
        elif state.active_run.outcome:
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


def _record_hook_decision(payload: Mapping[str, object], result: HookResult,
                          environ: Mapping[str, str]) -> None:
    """Record a bounded Stop decision in an explicitly opted-in test home."""
    destination = environ.get("SYMPHONY_HOOK_DECISIONS_DIR")
    if not destination or payload.get("hook_event_name") != "Stop":
        return
    try:
        response = json.loads(result.stdout) if result.stdout else {}
        if not isinstance(response, dict):
            response = {}
        reason = str(response.get("reason") or "")
        categories = (
            ("root ownership is unresolved", "owner_unresolved"),
            ("active in multiple project states", "owner_conflict"),
            ("state snapshot was unavailable", "owner_snapshot_unavailable"),
            ("bound project state is unavailable", "bound_state_unavailable"),
            ("unresolved child result", "pending_child"),
            ("child lifecycle reconciliation", "batch_pending"),
            ("child start has no invocation ID", "ambiguous_start"),
            ("active work remains", "active_work"),
            ("newer native turn still running", "native_running"),
            ("latest native Claude turn", "native_unknown"),
            ("latest native turn", "native_unknown"),
            ("interrupted work still requires reconciliation", "interrupted_work"),
            ("waiting for host launch confirmation", "launch_pending"),
            ("consultant results still require", "consultant_pending"),
            ("tracked lead has no reconciled outcome", "lead_outcome_missing"),
            ("assessment work has ended", "lead_not_started"),
        )
        category = next((label for phrase, label in categories if phrase in reason),
                        "other_block" if response.get("decision") == "block" else "permit")
        record = {"session_id": str(payload.get("session_id") or ""),
                  "category": category,
                  "reason_hash": hashlib.sha256(reason.encode()).hexdigest()[:12] if reason else None,
                  "observed_at": datetime.now(timezone.utc).isoformat(),
                  "plugin_version": PLUGIN_VERSION}
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{os.getpid()}-{datetime.now(timezone.utc).timestamp():.9f}.json").write_text(
            json.dumps(record), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = handle(payload, os.environ)
    except Exception as error:  # Hook failures must not block unrelated host work.
        sys.stderr.write(f"Symphony hook fault: {type(error).__name__}\n")
        _record_fault(error, os.environ)
        return 0
    _record_hook_decision(payload, result, os.environ)
    if result.stdout:
        sys.stdout.write(result.stdout)
    return 0
