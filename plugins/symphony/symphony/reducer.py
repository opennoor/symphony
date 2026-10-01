"""Pure lifecycle transitions for provider-neutral Symphony events."""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace

from .model import Action, Delegation, Event, ProjectState, RunState, persistable


_ACTIVE_STATES = {"active", "created", "pending", "running", "waiting", "working"}
_EVENT_HISTORY_LIMIT = 200


def _active_identities(run: RunState) -> list[str]:
    return [item.identity for item in run.delegations if item.state in _ACTIVE_STATES]


def _archive(state: ProjectState, run: RunState, status: str, observed_at: str) -> ProjectState:
    archived = replace(run, status=status, updated_at=observed_at)
    return replace(state, active_run=None, recent_runs=(*state.recent_runs, archived)[-20:])


def _preserved_consent(recorded) -> dict:
    """Consent survives a heartbeat from any session, including a stranger's."""
    if not isinstance(recorded, Mapping):
        return {}
    accepted = recorded.get("accepted")
    accepted = dict(accepted) if isinstance(accepted, Mapping) else {}
    # A record written before consent was keyed by session keeps one flat slot.
    # Migrate it, so it stops being destroyable by the next stranger.
    owner = str(recorded.get("session_id") or "")
    if owner and owner not in accepted and recorded.get("accepted_profile"):
        accepted[owner] = {
            "profile": str(recorded.get("accepted_profile") or ""),
            "route": str(recorded.get("accepted_route") or ""),
        }
    return accepted


def _heartbeat(state: ProjectState, event: Event):
    provider = event.payload.get("provider")
    if not provider:
        return state, ()
    activation = dict(state.activation)
    previous = state.activation.get(provider, {})
    session_profiles = list(previous.get("session_profiles") or ()) if isinstance(previous, Mapping) else []
    prior_session = str(previous.get("session_id") or "") if isinstance(previous, Mapping) else ""
    if prior_session and not any(item.get("session_id") == prior_session for item in session_profiles):
        session_profiles.append({
            "session_id": prior_session,
            "plugin_version": previous.get("plugin_version"),
            "plugin_root": previous.get("plugin_root"),
            "runtime_root": previous.get("runtime_root"),
            "hook_schema_version": previous.get("hook_schema_version"),
            "observed_at": previous.get("observed_at"),
            "profile": str(previous.get("profile") or ""),
            "claude_probe_attempted": bool(previous.get("claude_probe_attempted")),
        })
    session = str(event.payload.get("session_id") or "")
    if session:
        session_profiles = [item for item in session_profiles if item.get("session_id") != session]
        session_profiles.append({
            "session_id": session,
            "plugin_version": event.payload.get("plugin_version"),
            "plugin_root": event.payload.get("plugin_root"),
            "runtime_root": event.payload.get("runtime_root"),
            "hook_schema_version": event.payload.get("hook_schema_version"),
            "observed_at": event.observed_at,
            "profile": str(event.payload.get("profile") or ""),
            "claude_probe_attempted": bool(event.payload.get("claude_probe_attempted")),
        })
    pending = list(previous.get("pending_sessions") or ()) if isinstance(previous, Mapping) else []
    if session and event.payload.get("pending_task") and session not in pending:
        pending.append(session)
    owners = {run.session_id for key, run in state.active_runs.items()
              if key.startswith(f"{provider}:")}
    if state.active_run:
        owners.add(state.active_run.session_id)
    owners.update(pending)
    session_profiles = [item for item in session_profiles
                        if item.get("session_id") in owners or item in session_profiles[-4:]]
    facts = {
        "state": "guarded",
        "session_id": event.payload.get("session_id"),
        "plugin_version": event.payload.get("plugin_version"),
        "plugin_root": event.payload.get("plugin_root"),
        "runtime_root": event.payload.get("runtime_root"),
        "hook_schema_version": event.payload.get("hook_schema_version"),
        "observed_at": event.observed_at,
        # Reported once: the next heartbeat replaces this record wholesale.
        "last_fault": event.payload.get("last_fault"),
        # Which shipped entitlement profile this session routes through.
        "profile": event.payload.get("profile"),
        "claude_probe_attempted": event.payload.get("claude_probe_attempted"),
        # Consent, keyed by session and preserved rather than replaced. One
        # slot per provider meant a second terminal's heartbeat destroyed what
        # this one had accepted, and `proceed` silently stopped holding.
        "accepted": _preserved_consent(state.activation.get(provider)),
        "session_profiles": session_profiles,
        "pending_sessions": pending,
    }
    activation[provider] = {key: value for key, value in facts.items() if value is not None}
    return replace(state, activation=activation), ()


def _route_accepted(state: ProjectState, event: Event):
    """Record that the user accepted a route their entitlement clamps."""
    provider = event.payload.get("provider")
    profile = event.payload.get("profile")
    if not provider:
        return state, ()
    activation = dict(state.activation)
    record = dict(activation.get(provider, {}))
    record["accepted_profile"] = str(profile or "")
    record["accepted_route"] = str(event.payload.get("route") or "")
    session = str(event.payload.get("session_id") or "")
    if session:
        accepted = dict(record.get("accepted") or {})
        accepted.pop(session, None)
        accepted[session] = {
            "profile": record["accepted_profile"],
            "route": record["accepted_route"],
        }
        # Bounded: a project does not need the consent history of every
        # session that ever touched it.
        owners = {run.session_id for key, run in state.active_runs.items()
                  if key.startswith(f"{provider}:")}
        owners.update(record.get("pending_sessions") or ())
        record["accepted"] = {
            owner: value for owner, value in accepted.items()
            if owner in owners or owner in list(accepted)[-4:]
        }
    activation[provider] = record
    return replace(state, activation=activation), (Action("route_acceptance_recorded"),)


def _enable(state: ProjectState, event: Event):
    if state.enabled:
        return state, ()
    return replace(state, enabled=True, needs_reassessment=True), (Action("project_enabled"),)


def _disable(state: ProjectState, event: Event):
    actions: list[Action] = []
    next_state = state
    if state.active_run:
        active = _active_identities(state.active_run)
        if active:
            actions.append(Action("stop_delegations", {"active": active}))
            stopping = replace(state.active_run, status="stopping", updated_at=event.observed_at)
            next_state = replace(state, active_run=stopping)
        else:
            actions.append(Action("archive_run", {"run_id": state.active_run.run_id}))
            next_state = _archive(state, state.active_run, "disabled", event.observed_at)
    if next_state.enabled:
        actions.append(Action("project_disabled"))
    return replace(next_state, enabled=False), tuple(actions)


def _bypass(state: ProjectState, event: Event):
    task = event.payload.get("task")
    return state, (Action("execute_bypass", {"task": task}),)


def _task_received(state: ProjectState, event: Event):
    if not state.enabled and not event.payload.get("one_shot", False):
        return state, ()
    if state.active_run:
        return state, (Action("run_already_active", {"run_id": state.active_run.run_id}),)
    run_id = str(event.payload.get("run_id") or event.event_id)
    run = RunState(
        run_id=run_id,
        task=str(event.payload.get("task") or ""),
        status="assessing",
        started_at=event.observed_at,
        updated_at=event.observed_at,
        session_id=str(event.payload.get("session_id") or ""),
    )
    return replace(state, active_run=run), (Action("request_assessment", {"run_id": run_id}),)


def _assessment_requested(state: ProjectState, event: Event):
    if not state.active_run:
        return state, ()
    run = replace(state.active_run, status="assessing", updated_at=event.observed_at)
    return replace(state, active_run=run), (Action("spawn_assessor", {"run_id": run.run_id}),)


def _assessment_accepted(state: ProjectState, event: Event):
    if not state.active_run:
        return state, ()
    lifecycle = {
        key: value
        for key, value in state.active_run.assessment.items()
        if str(key).startswith("_")
    }
    assessment = dict(event.payload)
    assessment.update(lifecycle)
    # Accepted assessment evidence supersedes unconfirmed assessor launch
    # intents. Already observed agents retain their own lifecycle records.
    pending = [item for item in assessment.get("_pending_delegations", ())
               if not isinstance(item, Mapping) or item.get("role") != "assessor"]
    if pending:
        assessment["_pending_delegations"] = pending
    else:
        assessment.pop("_pending_delegations", None)
    if state.active_run.status in {"completing", "interrupted", "recovering", "stopping"}:
        status = state.active_run.status
    elif state.active_run.lead_identity and not state.active_run.assessment.get("_fast_escalated"):
        status = "active"
    else:
        status = "assessed"
    run = replace(
        state.active_run,
        status=status,
        assessment=assessment,
        updated_at=event.observed_at,
    )
    return (
        replace(state, active_run=run, needs_reassessment=False),
        (Action("route_run", {"run_id": run.run_id}),),
    )


def _lead_started(state: ProjectState, event: Event):
    run = state.active_run
    identity = event.payload.get("identity")
    if not run or not identity:
        return state, ()

    if run.assessment.get("_fast_escalated") and identity == run.lead_identity:
        # This lead gave up the task at a terminal handoff. A same-identity
        # native restart cannot undo that decision while assessment is pending.
        return state, (Action("ignore_stale_owner", {"identity": identity}),)

    requested_generation = int(event.payload.get("owner_generation", run.owner_generation))
    if run.lead_identity is None:
        # There is no owner to protect, whatever the run's status, so the first
        # lead of a run or of a recovery registers without a replacement dance.
        # It never moves the generation backwards.
        generation = max(requested_generation, run.owner_generation)
    else:
        replacing = identity != run.lead_identity
        safe = run.status in {"interrupted", "recovering"} or bool(
            event.payload.get("safe_boundary")
        )
        proof = run.assessment.get("_codex_unavailable_proof")
        native_unavailable = (isinstance(proof, Mapping)
                              and proof.get("digest") == event.payload.get("unavailability_digest")
                              and proof.get("session_id") == run.session_id
                              and proof.get("run_id") == run.run_id
                              and proof.get("original_lead") == run.lead_identity
                              and proof.get("owner_generation") == run.owner_generation
                              and proof.get("replacement_identity") == identity)
        retryable_original = (
            run.provider == "codex"
            and run.assessment.get("_retryable_lead") == run.lead_identity
            and run.assessment.get("_lead_route_mismatch_owner") != {
                "identity": run.lead_identity,
                "generation": run.owner_generation,
            }
        )
        if replacing and ((retryable_original and not native_unavailable) or not safe
                          or requested_generation != run.owner_generation + 1):
            return state, (Action("reject_lead_replacement", {"identity": identity}),)
        if not replacing and requested_generation != run.owner_generation:
            return state, (Action("ignore_stale_owner", {"identity": identity}),)
        generation = requested_generation

    run = replace(
        run,
        lead_identity=str(identity),
        owner_generation=generation,
        status="active",
        outcome=None,
        assessment={key: value for key, value in run.assessment.items()
                    if key not in {"_retryable_lead", "_fast_escalated", "_lead_route_mismatch",
                                   "_lead_route_mismatch_owner", "_codex_unavailable_proof"}},
        updated_at=event.observed_at,
    )
    return replace(state, active_run=run), ()


def _delegation_updated(state: ProjectState, event: Event):
    run = state.active_run
    identity = event.payload.get("identity")
    if not run or not identity:
        return state, ()
    current = next((item for item in run.delegations if item.identity == identity), None)
    item = Delegation(
        identity=str(identity),
        role=str(event.payload.get("role") or (current.role if current else "worker")),
        objective=str(event.payload.get("objective") or (current.objective if current else "")),
        state=str(event.payload.get("state") or (current.state if current else "pending")),
        requested_tier=str(event.payload.get("requested_tier") or (current.requested_tier if current else "")),
        requested_effort=str(
            event.payload.get("requested_effort") or (current.requested_effort if current else "")
        ),
        updated_at=event.observed_at,
    )
    delegations = tuple(existing for existing in run.delegations if existing.identity != identity) + (item,)
    updated = replace(run, delegations=delegations, updated_at=event.observed_at)
    if updated.status == "stopping" and not _active_identities(updated):
        return _archive(state, updated, "disabled", event.observed_at), (
            Action("archive_run", {"run_id": updated.run_id}),
        )
    if item.role not in {"lead", "rejected_lead"} and item.state.lower() in {"failed", "interrupted", "cancelled", "canceled", "error", "terminated"}:
        if updated.status == "recovering" and updated.assessment.get("_retryable_lead"):
            updated = replace(updated, assessment={**updated.assessment, "_retryable_lead": ""})
        if updated.status == "completing" or updated.assessment.get("_pending_lead_completion"):
            return _lead_failed(replace(state, active_run=updated),
                                replace(event, payload={"identity": updated.lead_identity}))
    if (updated.status == "completing" and not updated.assessment.get("_batch_pending")
            and item.role not in {"consultant", "lead"}
            and _stop_block_reason(updated) is None):
        return replace(state, active_run=updated), (
            Action("permit_completion", {"run_id": updated.run_id}),
        )
    return replace(state, active_run=updated), ()


def _delegation_launch_failed(state: ProjectState, event: Event):
    if not state.active_run:
        return state, ()
    assessment = dict(state.active_run.assessment)
    pending = list(assessment.get("_pending_delegations", ()))
    for index, item in enumerate(pending):
        if (isinstance(item, Mapping) and item.get("role") == event.payload.get("role")
                and item.get("model") == event.payload.get("requested_tier")
                and item.get("effort") == event.payload.get("requested_effort")):
            pending.pop(index)
            if pending:
                assessment["_pending_delegations"] = pending
            else:
                assessment.pop("_pending_delegations", None)
            return replace(state, active_run=replace(state.active_run, assessment=assessment)), ()
    return state, ()


def _valid_outcome(outcome) -> bool:
    if not isinstance(outcome, Mapping) or not outcome:
        return False
    if "status" not in outcome:
        return True
    status = outcome["status"]
    return isinstance(status, str) and status.lower() in {"completed", "done", "success", "succeeded"}


def _lead_completed(state: ProjectState, event: Event):
    run = state.active_run
    if not run:
        return state, ()
    identity = event.payload.get("identity")
    generation = int(event.payload.get("owner_generation", run.owner_generation))
    if identity != run.lead_identity or generation != run.owner_generation:
        return state, (Action("ignore_stale_owner", {"identity": identity}),)

    if run.status == "recovering":
        return state, (Action("block_completion", {"reason": "lead_recovery_required"}),)
    outcome = event.payload.get("outcome")
    if not _valid_outcome(outcome):
        return state, (Action("block_completion", {"reason": "outcome_missing"}),)
    active = [item for item in _active_identities(run) if item != identity]
    unresolved = [item.identity for item in run.delegations if item.state == "interrupted"
                  and (item.role != "lead" or item.identity == run.lead_identity)]
    assessment = dict(run.assessment)
    assessment.pop("_retryable_lead", None)
    assessment.pop("_retryable_lead_turn", None)
    completed = replace(run, outcome=dict(outcome), assessment=assessment,
                        updated_at=event.observed_at)
    if (active or unresolved or run.assessment.get("_batch_pending")
            or run.assessment.get("_ambiguous_child_starts")
            or run.assessment.get("_pending_delegations")
            or run.assessment.get("_invalid_consultants") or run.assessment.get("_lead_route_mismatch")):
        completed = replace(completed, status="completing")
        return replace(state, active_run=completed), (Action("wait_for_delegations", {"active": active}),)
    # A completed child may receive a later native follow-up before the root
    # ends its turn. Keep its owner and outcome live until the root Stop can
    # check every later lead and worker result.
    completed = replace(completed, status="completing")
    return replace(state, active_run=completed), (
        Action("permit_completion", {"run_id": run.run_id}),
    )


def _lead_failed(state: ProjectState, event: Event):
    run = state.active_run
    if not run or event.payload.get("identity") != run.lead_identity:
        return state, ()
    assessment = dict(run.assessment)
    assessment.pop("_pending_lead_completion", None)
    if event.kind == "lead_failed" and event.payload.get("native_host_failed") is not True:
        assessment["_retryable_lead"] = run.lead_identity
        token = event.payload.get("turn_token")
        if isinstance(token, str) and token:
            assessment["_retryable_lead_turn"] = token
        else:
            assessment.pop("_retryable_lead_turn", None)
    else:
        assessment["_retryable_lead"] = ""
        assessment.pop("_retryable_lead_turn", None)
    recovering = replace(run, status="recovering", outcome=None, assessment=assessment, updated_at=event.observed_at)
    return replace(state, active_run=recovering), (
        Action("replace_lead", {"owner_generation": run.owner_generation + 1}),
    )


def _interrupt(state: ProjectState, event: Event):
    if not state.active_run:
        return state, ()
    run = replace(state.active_run, status="interrupted", updated_at=event.observed_at)
    return (
        replace(state, active_run=run),
        (Action("preserve_recovery_context", {"run_id": run.run_id}),),
    )


def _resume_reconciled(state: ProjectState, event: Event):
    run = state.active_run
    if not run:
        return state, ()
    active_ids = {str(identity) for identity in event.payload.get("active_ids", ())}
    delegations = tuple(
        item
        if item.state not in _ACTIVE_STATES or item.identity in active_ids
        else replace(item, state="interrupted", updated_at=event.observed_at)
        for item in run.delegations
    )
    if run.status in {"completing", "stopping"}:
        reconciled = replace(run, delegations=delegations, updated_at=event.observed_at)
        if run.status == "completing" and _stop_block_reason(reconciled) is None:
            return replace(state, active_run=reconciled), (
                Action("permit_completion", {"run_id": run.run_id}),
            )
        return replace(state, active_run=reconciled), ()
    if run.lead_identity and run.lead_identity in active_ids:
        resumed = replace(run, status="active", delegations=delegations, updated_at=event.observed_at)
        return replace(state, active_run=resumed), ()
    recovering = replace(run, status="recovering", delegations=delegations, updated_at=event.observed_at)
    return (
        replace(state, active_run=recovering),
        (Action("replace_lead", {"owner_generation": run.owner_generation + 1}),),
    )


def _reassess(state: ProjectState, event: Event):
    next_state = replace(state, needs_reassessment=True)
    if not state.active_run:
        return next_state, ()
    return next_state, (Action("request_assessment", {"run_id": state.active_run.run_id}),)


def _stop_block_reason(run: RunState) -> dict | None:
    """Return the payload for a stop block, or None when completion is permitted."""
    if run.assessment.get("_batch_pending"):
        return {"reason": "child lifecycle reconciliation is still in progress"}
    ambiguous = run.assessment.get("_ambiguous_child_starts", ())
    if ambiguous:
        return {"reason": "child start has no invocation ID and matches an earlier start; "
                "inspect or recover: " + ", ".join(map(str, ambiguous))}
    ambiguous_stops = run.assessment.get("_ambiguous_child_stops", ())
    if ambiguous_stops:
        return {"reason": "child terminal does not match the latest native turn; "
                "inspect or recover: " + ", ".join(map(str, ambiguous_stops))}
    active = _active_identities(run)
    if active:
        return {"active": active}
    unresolved = [item.identity for item in run.delegations if item.state == "interrupted"
                  and (item.role != "lead" or item.identity == run.lead_identity)]
    if unresolved:
        return {"reason": "interrupted work still requires reconciliation: " + ", ".join(unresolved)}
    pending = run.assessment.get("_pending_delegations", ())
    if pending:
        roles = [str(item.get("role") or "agent") for item in pending if isinstance(item, Mapping)]
        return {"reason": "waiting for host launch confirmation: " + ", ".join(roles)}
    invalid_consultants = run.assessment.get("_invalid_consultants", ())
    if invalid_consultants:
        return {
            "reason": "consultant results still require size/complexity classification: "
            + ", ".join(map(str, invalid_consultants))
        }
    mismatch = run.assessment.get("_lead_route_mismatch")
    if mismatch:
        return {"reason": mismatch}
    if not _valid_outcome(run.outcome):
        return {"reason": "lead_outcome_missing" if run.lead_identity else "lead_not_started"}
    return None


def _stop_requested(state: ProjectState, event: Event):
    run = state.active_run
    if not run:
        return state, (Action("permit_stop"),)
    if not run.delegations and not run.assessment.get("_pending_delegations"):
        # A run that never produced tracked work cannot hold the session.
        return _archive(state, run, "abandoned", event.observed_at), (
            Action("archive_run", {"run_id": run.run_id}),
            Action("permit_stop"),
        )
    waiting_for = {
        item.identity for item in run.delegations
        if item.state in _ACTIVE_STATES and item.role in {"assessor", "lead"}
    }
    if any(
        isinstance(task, Mapping)
        and task.get("type") == "subagent"
        and task.get("id") in waiting_for
        for task in (event.payload.get("background_tasks") or ())
    ):
        # Claude runs agents in the background and wakes the session with each
        # result, so ending the turn is how the root waits. Blocking it here
        # forced the root to busy-poll with shell loops, and the second stop
        # abandoned the live run, so the lead spawn was refused and the
        # assessment had to be repeated. The run stays open.
        return state, (Action("permit_stop"),)
    reason = _stop_block_reason(run)
    if reason is None:
        return _archive(state, run, "completed", event.observed_at), (
            Action("archive_run", {"run_id": run.run_id}),
            Action("permit_stop"),
        )
    if event.payload.get("stop_hook_active"):
        # Ending a host turn is not evidence that its children died. Release
        # the retry while keeping the run available for later host events.
        return state, (Action("permit_stop"),)
    return state, (Action("block_stop", reason),)


def _force_stop(state: ProjectState, event: Event):
    if not state.active_run:
        return state, (Action("permit_stop"),)
    run = state.active_run
    actions: list[Action] = []
    active = _active_identities(run)
    if active:
        actions.append(Action("stop_delegations", {"active": active}))
    actions.extend((Action("archive_run", {"run_id": run.run_id}), Action("permit_stop")))
    return _archive(state, replace(run, unreconciled=tuple(active)), "force_stopped", event.observed_at), tuple(actions)


_Handler = Callable[[ProjectState, Event], tuple[ProjectState, tuple[Action, ...]]]
_HANDLERS: dict[str, _Handler] = {
    "session_heartbeat": _heartbeat,
    "enable": _enable,
    "route_accepted": _route_accepted,
    "disable": _disable,
    "bypass": _bypass,
    "task_received": _task_received,
    "assessment_requested": _assessment_requested,
    "assessment_accepted": _assessment_accepted,
    "lead_started": _lead_started,
    "delegation_updated": _delegation_updated,
    "delegation_launch_failed": _delegation_launch_failed,
    "lead_completed": _lead_completed,
    "lead_failed": _lead_failed,
    "interrupt": _interrupt,
    "resume_reconciled": _resume_reconciled,
    "reassess": _reassess,
    "stop_requested": _stop_requested,
    "force_stop": _force_stop,
}


def reduce(state: ProjectState, event: Event) -> tuple[ProjectState, tuple[Action, ...]]:
    """Apply one canonical event without side effects."""
    if any(record.event_id == event.event_id for record in state.event_history):
        if event.kind == "stop_requested":
            return _stop_requested(state, event)
        return state, ()
    handler = _HANDLERS.get(event.kind)
    if handler is None:
        return state, ()

    next_state, actions = handler(state, event)
    if next_state == state and not actions:
        return state, ()
    history = tuple(
        deque((*next_state.event_history, persistable(event)), maxlen=_EVENT_HISTORY_LIMIT)
    )
    return replace(next_state, event_history=history), actions
