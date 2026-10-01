"""Provider-neutral Symphony lifecycle records."""

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    observed_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)


# Only lifecycle facts survive into durable history. A hook payload carries the
# user's prompt, the agent's final message, spawn packets and transcript paths;
# none of that belongs in a cross-project file under the user's home directory,
# and no redactor can reliably find a credential pasted into free text.
PERSISTED_EVENT_KEYS = frozenset(
    {
        "provider",
        "session_id",
        "hook_event_name",
        "agent_id",
        "agent_type",
        "identity",
        "role",
        "state",
        "status",
        "requested_tier",
        "requested_effort",
        "owner_generation",
        "run_id",
        "size",
        "complexity",
        "risk",
        "topology",
        "route",
        "outcome",
        "active_ids",
        "one_shot",
        "stop_hook_active",
        "plugin_version",
        "plugin_root",
        "runtime_root",
        "hook_schema_version",
        "last_fault",
        "profile",
        "accepted_profile",
        "accepted_route",
        "task",
        "objective",
        "rationale",
        "reason",
    }
)
_BOUNDED_TEXT_KEYS = frozenset({"task", "objective", "rationale", "reason"})
_LABEL_LIMIT = 120


def persistable(event: Event) -> Event:
    """Strip an event down to the facts Symphony is allowed to keep."""
    kept: dict[str, Any] = {}
    for key, value in event.payload.items():
        if key not in PERSISTED_EVENT_KEYS:
            continue
        if key in _BOUNDED_TEXT_KEYS and isinstance(value, str):
            value = value.splitlines()[0][:_LABEL_LIMIT] if value else value
        kept[key] = value
    return Event(event.event_id, event.kind, event.observed_at, kept)


@dataclass(frozen=True)
class Action:
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Delegation:
    identity: str
    role: str
    objective: str
    state: str
    requested_tier: str
    requested_effort: str
    updated_at: str = ""


@dataclass(frozen=True)
class RunState:
    run_id: str
    task: str
    status: str = "active"
    owner_generation: int = 1
    lead_identity: str | None = None
    assessment: Mapping[str, Any] = field(default_factory=dict)
    delegations: tuple[Delegation, ...] = ()
    outcome: Mapping[str, Any] | None = None
    started_at: str = ""
    updated_at: str = ""
    session_id: str = ""
    provider: str = ""
    # When the session that owns this run last reported. A different
    # session is not proof the owner died, so takeover needs evidence.
    owner_seen_at: str = ""
    unreconciled: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySnapshot:
    provider: str
    available_models: tuple[str, ...]
    supported_efforts: Mapping[str, tuple[str, ...]]
    tiers: Mapping[str, str]
    source: str
    provider_version: str | None
    refreshed_at: str
    matrix: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectState:
    enabled: bool = False
    configuration: Mapping[str, Any] = field(default_factory=dict)
    activation: Mapping[str, Any] = field(default_factory=dict)
    active_run: RunState | None = None
    # Canonical live runs, keyed by provider and owning root session. active_run
    # remains a compatibility view for older callers and state files.
    active_runs: Mapping[str, RunState] = field(default_factory=dict)
    recent_runs: tuple[RunState, ...] = ()
    # Accepted child terminals survive the bounded visible run archive for as
    # long as this root can resume. Never trim these receipts silently: an old
    # callback could otherwise complete a newer run that reused its child ID.
    # Each receipt contains only identities and a result hash, not message text.
    terminal_receipts: tuple[Mapping[str, str], ...] = ()
    event_history: tuple[Event, ...] = ()
    needs_reassessment: bool = False
