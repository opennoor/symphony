"""Atomic JSON persistence for Symphony project state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .memory import redact_secrets
from .model import Delegation, Event, ProjectState, RunState

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on platforms without fcntl
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised outside Windows
    msvcrt = None


SCHEMA_VERSION = 2
_UNMANAGED_REPORT_COUNT = 100
_UNMANAGED_REPORT_BYTES = 16 * 1024 * 1024
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_UpdateResult = TypeVar("_UpdateResult")
_SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "client_secret",
    "private_key",
    "token",
    "cookie",
    "cookies",
    "set_cookie",
    "passphrase",
    "pwd",
}


def _child_callback_payload(event: Event) -> dict[str, Any]:
    # Preserve provider fields at the first durable boundary, including fields
    # introduced by a later host version. Runtime replay flags are not inputs.
    return {key: item for key, item in event.payload.items()
            if key not in {'_symphony_owner_conflict', '_symphony_verified_alias'}}


def project_key(project: Path) -> str:
    canonical = os.path.normcase(str(Path(project).resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git_toplevel(project: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(project).resolve()), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return str(Path(output).resolve()) if output else None


def legacy_project_keys(project: Path) -> tuple[str, ...]:
    """Candidate pre-1.0 state keys.

    The pre-1.0 hook keyed project state on the sha256 of the git toplevel,
    falling back to the resolved working directory. A session started in a
    subdirectory therefore produces a different key from the one 1.0 derives,
    so both candidates are tried before concluding there is nothing to import.
    """
    candidates: list[str] = []
    toplevel = _git_toplevel(project)
    if toplevel:
        candidates.append(toplevel)
    resolved = str(Path(project).resolve())
    if resolved not in candidates:
        candidates.append(resolved)
    return tuple(hashlib.sha256(item.encode("utf-8")).hexdigest()[:24] for item in candidates)


def _event_from_dict(value: Any) -> Event:
    value = _object(value, "event")
    return Event(
        event_id=_text(value.get("event_id"), "event.event_id"),
        kind=_text(value.get("kind"), "event.kind"),
        observed_at=_text(value.get("observed_at"), "event.observed_at"),
        payload=_object(value.get("payload", {}), "event.payload"),
    )


def _delegation_from_dict(value: Any) -> Delegation:
    value = _object(value, "delegation")
    return Delegation(
        identity=_text(value.get("identity"), "delegation.identity"),
        role=_text(value.get("role"), "delegation.role"),
        objective=_text(value.get("objective"), "delegation.objective"),
        state=_text(value.get("state"), "delegation.state"),
        requested_tier=_text(value.get("requested_tier"), "delegation.requested_tier"),
        requested_effort=_text(value.get("requested_effort"), "delegation.requested_effort"),
        updated_at=_text(value.get("updated_at", ""), "delegation.updated_at"),
    )


def _receipt_from_dict(value: Any) -> dict[str, object]:
    receipt = _object(value, "terminal receipt")
    required = ("provider", "session", "agent", "run_id",
                "turn", "result", "parent", "lead")
    optional = ("status", "native_agent_type", "native_model", "native_effort",
                "native_launch_prompt_hash", "native_terminal_id", "native_report_hash")
    if 'native_fast_escalation' in receipt and type(receipt['native_fast_escalation']) is not bool:
        raise ValueError('terminal receipt.native_fast_escalation must be a boolean')
    if 'native_owner_generation' in receipt and (
            type(receipt['native_owner_generation']) is not int or receipt['native_owner_generation'] < 0):
        raise ValueError('terminal receipt.native_owner_generation must be a nonnegative integer')
    return {**{key: _text(receipt.get(key), f"terminal receipt.{key}")
               for key in required},
            **{key: _text(receipt.get(key, ""), f"terminal receipt.{key}")
               for key in optional},
            **({'native_followup_start_id': _text(receipt['native_followup_start_id'],
                    'terminal receipt.native_followup_start_id')}
               if 'native_followup_start_id' in receipt else {}),
            **({'native_owner_generation': receipt['native_owner_generation']}
               if 'native_owner_generation' in receipt else {}),
            **({'native_fast_escalation': receipt['native_fast_escalation']}
               if 'native_fast_escalation' in receipt else {})}


def _run_from_dict(value: Any) -> RunState:
    value = _object(value, "run")
    generation = value.get("owner_generation", 1)
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValueError("run.owner_generation must be an integer")
    lead_identity = value.get("lead_identity")
    if lead_identity is not None:
        lead_identity = _text(lead_identity, "run.lead_identity")
    outcome = value.get("outcome")
    if outcome is not None:
        outcome = _object(outcome, "run.outcome")
    return RunState(
        run_id=_text(value.get("run_id"), "run.run_id"),
        task=_text(value.get("task"), "run.task"),
        status=_text(value.get("status", "active"), "run.status"),
        owner_generation=generation,
        lead_identity=lead_identity,
        assessment=_object(value.get("assessment", {}), "run.assessment"),
        delegations=tuple(_delegation_from_dict(item) for item in _array(value.get("delegations", ()), "run.delegations")),
        outcome=outcome,
        started_at=_text(value.get("started_at", ""), "run.started_at"),
        updated_at=_text(value.get("updated_at", ""), "run.updated_at"),
        session_id=_text(value.get("session_id", ""), "run.session_id"),
        provider=_text(value.get("provider", ""), "run.provider"),
        owner_seen_at=_text(value.get("owner_seen_at", ""), "run.owner_seen_at"),
        unreconciled=tuple(
            _text(item, "run.unreconciled item")
            for item in _array(value.get("unreconciled", ()), "run.unreconciled")
        ),
    )


def _available_terminal_receipts(
    runs: tuple[RunState, ...], existing: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Keep lineage still present in older run records before archive trim."""
    receipts = list(existing)
    results = {(item["provider"], item["session"], item["run_id"], item["result"])
               for item in receipts if item["result"]}
    agents = {(item["provider"], item["session"], item["run_id"], item["agent"])
              for item in receipts if item["agent"] != "*"}
    turns = {(item["provider"], item["session"], item["run_id"],
              item["agent"], item["turn"]) for item in receipts if item["turn"]}
    for run in runs:
        base = {"provider": run.provider, "session": run.session_id,
                "run_id": run.run_id, "parent": "", "lead": run.lead_identity or ""}
        if not run.provider or not run.session_id:
            continue
        terminal_ids = run.assessment.get("_terminal_event_ids", ())
        recorded_turns = run.assessment.get("_terminal_turns", {})
        if not terminal_ids and not recorded_turns:
            continue
        for raw in terminal_ids:
            result = str(raw).rsplit(":", 1)[-1]
            key = (run.provider, run.session_id, run.run_id, result)
            if re.fullmatch(r"[0-9a-f]{64}", result) and key not in results:
                receipts.append({**base, "agent": "*", "turn": "", "result": result})
                results.add(key)
        for child in run.delegations:
            agent_key = (run.provider, run.session_id, run.run_id, child.identity)
            if agent_key not in agents:
                receipts.append({**base, "agent": child.identity,
                                 "turn": "", "result": ""})
                agents.add(agent_key)
            if isinstance(recorded_turns, dict):
                for raw in recorded_turns.get(child.identity, ()):
                    token = str(raw)
                    turn_key = (*agent_key, token)
                    if token and turn_key not in turns:
                        receipts.append({**base, "agent": child.identity,
                                         "turn": token, "result": ""})
                        turns.add(turn_key)
    return tuple(receipts)


def _legacy_run_key(run: RunState, activation: dict, history: tuple[Event, ...]) -> str:
    if run.session_id and run.provider in {"codex", "claude"}:
        return f"{run.provider}:{run.session_id}"
    providers = {
        provider for provider, record in activation.items()
        if isinstance(record, dict) and (
            record.get("session_id") == run.session_id
            or any(isinstance(item, dict) and item.get("session_id") == run.session_id
                   for item in record.get("session_profiles", ()))
        )
    }
    providers.update(
        str(event.payload.get("provider")) for event in history
        if event.payload.get("session_id") == run.session_id
        and event.payload.get("provider") in {"codex", "claude"}
    )
    if run.session_id and len(providers) == 1:
        return f"{providers.pop()}:{run.session_id}"
    # Ambiguous old state is retained but cannot be taken over by a different
    # provider or by a foreign root that happens to arrive next.
    return f"unbound:{run.session_id}"


def _state_to_dict(state: ProjectState) -> dict[str, Any]:
    active_runs = dict(state.active_runs)
    if state.active_run and not active_runs:
        key = _legacy_run_key(state.active_run, dict(state.activation), state.event_history)
        active_runs[key] = replace(state.active_run, provider=key.split(":", 1)[0]
                                   if not key.startswith("unbound:") else "")
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": state.enabled,
        "configuration": dict(state.configuration),
        "activation": dict(state.activation),
        "active_run": None if state.active_run is None else asdict(state.active_run),
        "active_runs": {key: asdict(run) for key, run in active_runs.items()},
        "recent_runs": [asdict(item) for item in state.recent_runs[-20:]],
        "terminal_receipts": [dict(item) for item in state.terminal_receipts],
        "event_history": [asdict(item) for item in state.event_history],
        "needs_reassessment": state.needs_reassessment,
    }


def _state_from_dict(value: Any) -> ProjectState:
    value = _object(value, "state")
    version = value.get("schema_version")
    if version not in {1, SCHEMA_VERSION}:
        raise ValueError(f"unsupported schema version {version!r}")
    enabled = value.get("enabled", False)
    needs_reassessment = value.get("needs_reassessment", False)
    if not isinstance(enabled, bool) or not isinstance(needs_reassessment, bool):
        raise ValueError("state flags must be booleans")
    active_run = value.get("active_run")
    parsed_run = None if active_run is None else _run_from_dict(active_run)
    activation = _object(value.get("activation", {}), "state.activation")
    history = tuple(
        _event_from_dict(item) for item in _array(value.get("event_history", ()), "state.event_history")
    )
    active_runs = {
        _text(key, "state.active_runs key"): _run_from_dict(run)
        for key, run in _object(value.get("active_runs", {}), "state.active_runs").items()
    }
    if parsed_run and not active_runs:
        key = _legacy_run_key(parsed_run, activation, history)
        if not key.startswith("unbound:"):
            parsed_run = replace(parsed_run, provider=key.split(":", 1)[0])
        active_runs[key] = parsed_run
    recent_runs = tuple(
        _run_from_dict(item) for item in _array(value.get("recent_runs", ()), "state.recent_runs")[-20:]
    )
    recent_runs = tuple(
        replace(run, provider=key.split(":", 1)[0])
        if not run.provider and not (key := _legacy_run_key(run, activation, history)).startswith("unbound:")
        else run
        for run in recent_runs
    )
    receipts = tuple(_receipt_from_dict(item) for item in
                     _array(value.get("terminal_receipts", ()), "state.terminal_receipts"))
    receipts = _available_terminal_receipts(
        (*recent_runs, *active_runs.values()), receipts)
    return ProjectState(
        enabled=enabled,
        configuration=_object(value.get("configuration", {}), "state.configuration"),
        activation=activation,
        active_run=parsed_run,
        active_runs=active_runs,
        recent_runs=recent_runs,
        terminal_receipts=receipts,
        event_history=history,
        needs_reassessment=needs_reassessment,
    )


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: Any, name: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _redact(value: Any, key: str = "") -> Any:
    normalized = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', key)
    normalized = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', normalized)
    normalized = re.sub(r'[^a-z0-9]+', '_', normalized.lower()).strip('_')
    words = set(normalized.split('_'))
    if (any(normalized == secret or normalized.endswith('_' + secret) for secret in _SECRET_KEYS)
            or words & {'password', 'passwd', 'secret', 'authorization', 'credential', 'credentials'}
            or normalized in {'auth', 'authentication'}
            or ('token' in words and 'value' in words)):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {item_key: _redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _local_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCAL_LOCKS_GUARD:
        # ponytail: fallback locks remain for process lifetime; replace with weak locks if Windows projects become unbounded.
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _locked(path: Path, timeout: float | None = None) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    local = _local_lock(path)
    deadline = None if timeout is None else time.monotonic() + timeout
    if timeout is None:
        local.acquire()
    elif not local.acquire(timeout=timeout):
        raise TimeoutError(f"state lock timed out: {path}")
    try:
        with path.with_name(path.name + ".lock").open("a+b") as lock_file:
            if deadline is None:
                deadline = time.monotonic() + 5
            if fcntl is not None:
                if timeout is None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(f"state lock timed out: {path}")
                            time.sleep(0.005)
            elif msvcrt is not None:
                # Win32 byte-range locks extend beyond EOF. Writing a
                # sentinel before acquiring the lock races another process's
                # first lock on a new file and can fail with PermissionError.
                while True:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"state lock timed out: {path}")
                        time.sleep(0.005)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        local.release()


def _read_owner_snapshot(path: Path, timeout: float = 5) -> str:
    """Read while coordinating with Windows' replace-existing limitation."""
    if os.name == "nt":
        # Share the scan's remaining budget across all project snapshots.
        with _locked(path, timeout=timeout):
            return path.read_text(encoding="utf-8")
    return path.read_text(encoding="utf-8")


class StateStore:
    def __init__(self, root: Path, legacy_roots: tuple[Path, ...] = ()):
        self.root = Path(root)
        self.legacy_roots = tuple(Path(item) for item in legacy_roots)

    def _path(self, project: Path) -> Path:
        return self.root / f"{project_key(project)}.v2.json"

    def _session_path(self, provider: str, session: str) -> Path:
        digest = hashlib.sha256(f"{provider}\0{session}".encode()).hexdigest()
        return self.root / f".session-{digest}.json"

    def _alias_path(self, provider: str, owner: str, child: str) -> Path:
        root_digest = hashlib.sha256(f"{provider}\0{owner}".encode()).hexdigest()
        child_digest = hashlib.sha256(f"{provider}\0{child}".encode()).hexdigest()
        return self.root / f".alias-{root_digest}-{child_digest}.json"

    def aliases_for_owner(self, provider: str, owner: str) -> tuple[str, ...]:
        """Find only aliases recorded for this root; revalidate each before use."""
        prefix = hashlib.sha256(f"{provider}\0{owner}".encode()).hexdigest()
        aliases: list[str] = []
        for path in self.root.glob(f".alias-{prefix}-*.json"):
            if path.is_symlink():
                raise ValueError("invalid child alias index")
            item = _object(json.loads(path.read_text(encoding="utf-8")), "child alias index")
            child = item.get("child")
            if (item.get("provider") != provider or item.get("owner") != owner
                    or not isinstance(child, str)
                    or path != self._alias_path(provider, owner, child)):
                raise ValueError("invalid child alias index")
            aliases.append(child)
        return tuple(aliases)

    def _register_alias(self, provider: str, owner: str, child: str) -> None:
        path = self._alias_path(provider, owner, child)
        if not path.exists():
            self._write_json(path, {"provider": provider, "owner": owner, "child": child})

    def session_record(self, provider: str, session: str) -> dict[str, Any] | None:
        """Read the root's durable owner and unresolved child events under its lock."""
        path = self._session_path(provider, session)
        if not path.exists():
            return None
        record = _object(json.loads(path.read_text(encoding="utf-8")), "session record")
        project_name = record.get("project")
        project = Path(project_name) if isinstance(project_name, str) else None
        state_name = record.get("state_name")
        state_path = self.root / state_name if isinstance(state_name, str) else None
        if (record.get("schema") != 1 or record.get("provider") != provider
                or record.get("session") != session
                or (state_name is not None and (not isinstance(state_name, str)
                                                or not re.fullmatch(r"[0-9a-f]{64}\.v2\.json", state_name)))
                or (project is not None and (not project.is_absolute()
                                             or record.get("state_name") != self._path(project).name))
                or (project is None and state_path is None and record.get("state_name") is not None)
                or (record.get("owner_session") is not None
                    and not isinstance(record.get("owner_session"), str))
                or not isinstance(record.get("migrated"), bool)
                or not isinstance(record.get("generation", 1), int)
                or record.get("generation", 1) < 1
                or not isinstance(record.get("retired_agents", []), list)
                or not isinstance(record.get("pending", []), list)
                or not isinstance(record.get("overflow", False), bool)):
            raise ValueError("invalid root session record")
        return record

    def bind_session(
        self, provider: str, session: str, state_path: Path,
        migrated: bool, project: Path | None = None, owner_session: str | None = None,
    ) -> dict[str, Any]:
        """Bind a root once; a child working directory never changes it."""
        project = Path(project).resolve() if project is not None else None
        state_path = Path(state_path)
        if (state_path.parent != self.root
                or not re.fullmatch(r"[0-9a-f]{64}\.v2\.json", state_path.name)
                or (project is not None and self._path(project) != state_path)):
            raise ValueError("invalid root session state path")
        existing = self.session_record(provider, session)
        if existing is not None:
            if existing["state_name"] not in {None, state_path.name}:
                raise ValueError("root session is already bound to another project")
            if existing["state_name"] is None:
                existing["project"] = str(project) if project else None
                existing["state_name"] = state_path.name
                existing["migrated"] = migrated
                existing["owner_session"] = owner_session or session
                if existing["owner_session"] != session:
                    self._register_alias(provider, existing["owner_session"], session)
                self._write_json(self._session_path(provider, session), existing)
            if existing["owner_session"] and existing["owner_session"] != session:
                self._register_alias(provider, existing["owner_session"], session)
            return existing
        record = {
            "schema": 1, "provider": provider, "session": session,
            "project": str(project) if project else None, "state_name": state_path.name,
            "owner_session": owner_session or session,
            "migrated": migrated, "generation": 1, "retired_agents": [],
            "pending": [], "overflow": False,
        }
        if record["owner_session"] != session:
            self._register_alias(provider, record["owner_session"], session)
        self._write_json(self._session_path(provider, session), record)
        return record

    def queue_session_event(
        self, provider: str, session: str, event: Event, *, ambiguous_owner: bool = False,
    ) -> None:
        """Retain a child hook when an old session's owner cannot be read yet."""
        record = self.session_record(provider, session)
        if record is None:
            record = {"schema": 1, "provider": provider, "session": session,
                      "project": None, "state_name": None, "migrated": True,
                      "owner_session": None,
                      "generation": 1, "retired_agents": [],
                      "pending": [], "overflow": False}
        parent = str(event.payload.get("parent_thread_id") or "")
        if parent and parent != session and record["owner_session"] is None:
            # Discovery only: the root still verifies the active run, parent,
            # session record and event under its own lock before replay.
            self._register_alias(provider, parent, session)
        pending = list(record["pending"])
        event_payload = _child_callback_payload(event)
        entry = {"event_id": event.event_id, "kind": event.kind,
                 "observed_at": event.observed_at, "payload": _redact(event_payload),
                 "generation": record["generation"], "ambiguous_owner": ambiguous_owner}
        existing = next((item for item in pending if item.get("event_id") == event.event_id), None)
        if existing is not None and ambiguous_owner and not existing.get("ambiguous_owner"):
            existing["ambiguous_owner"] = True
            record["pending"] = pending
            self._write_json(self._session_path(record["provider"], record["session"]), record)
        if existing is None:
            if len(pending) < 256 and len(json.dumps(entry)) <= 131072:
                pending.append(entry)
                record["pending"] = pending
            else:
                record["overflow"] = True
            self._write_json(self._session_path(record["provider"], record["session"]), record)

    def preserve_unmanaged_callback(
        self, provider: str, session: str, project: Path, generation: int,
        event: Event, *, create: bool = True,
    ) -> bool:
        """Preserve out-of-scope evidence before ACK; never certify completion.

        The exact callback, project, session and generation identify the record.
        Its existence also makes a crash between preservation and ACK recoverable
        after enablement changes. This is not a managed terminal receipt.
        """
        value = _redact({"schema": 1, "disposition": "outside_disabled_governance",
                        "provider": provider, "session": session,
                        "project": project_key(project), "generation": generation,
                        "event": {"event_id": event.event_id, "kind": event.kind,
                                  "observed_at": event.observed_at,
                                  "payload": _child_callback_payload(event)}})
        # Normalize tuple-valued native metadata to its durable JSON shape.
        value = json.loads(json.dumps(value, sort_keys=True))
        # Provider retries keep their callback ID and payload but acquire a new
        # local observation time. Retain that time in the original evidence;
        # bind replay to the stable identity, including the complete payload.
        identity = {**value, 'event': {key: item for key, item in value['event'].items()
                                     if key != 'observed_at'}}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        index_path, index = self._unmanaged_observations(provider, session, value['project'], generation)
        fact = index['entries'].get(digest)
        if fact and fact['event_id'] != event.event_id:
            raise ValueError('unmanaged observation event identity differs')
        if fact and (not create or fact['acknowledged']):
            return True
        paths = [self.root / folder / (digest + '.json')
                 for folder in ('unmanaged-callbacks', 'unmanaged-recovery')]
        existing = next((path for path in paths if path.exists()), None)
        if existing is not None:
            saved = json.loads(existing.read_text(encoding='utf-8'))
            if not isinstance(saved, dict) or not isinstance(saved.get('event'), dict):
                raise ValueError('invalid unmanaged callback evidence')
            saved_identity = {**saved, 'event': {key: item for key, item in saved['event'].items()
                                               if key != 'observed_at'}}
            if saved_identity != identity:
                raise ValueError('unmanaged callback evidence differs from its identity hash')
        if existing is None:
            if not create:
                return False
            try:
                self._write_json(paths[0], value)
            except OSError:
                # Separate from the managed inbox: no report-size limit, no
                # overflow flag, and no invented obligation for disabled work.
                self._write_json(paths[1], value)
        elif not create:
            return True
        if fact is None:
            index['entries'][digest] = {'event_id': event.event_id, 'acknowledged': False}
            self._write_json(index_path, index)
        return True

    def _unmanaged_observations(
        self, provider: str, session: str, project: str, generation: int,
    ) -> tuple[Path, dict[str, Any]]:
        scope = {'schema': 1, 'provider': provider, 'session': session,
                 'project': project, 'generation': generation}
        digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        path = self.root / 'unmanaged-observations' / (digest + '.json')
        if not path.exists():
            return path, {**scope, 'entries': {}}
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {*scope, 'entries'}
                or any(value[key] != expected for key, expected in scope.items())
                or type(value['schema']) is not int or type(value['generation']) is not int
                or not isinstance(value['entries'], dict)
                or any(not re.fullmatch(r'[0-9a-f]{64}', key)
                       or not isinstance(fact, dict) or set(fact) != {'event_id', 'acknowledged'}
                       or not isinstance(fact['event_id'], str)
                       or not fact['event_id'] or len(fact['event_id']) > 512
                       or type(fact['acknowledged']) is not bool
                       for key, fact in value['entries'].items())):
            raise ValueError('invalid unmanaged observation facts')
        return path, value

    def _prune_unmanaged_reports(self, index: dict[str, Any], pending: set[str]) -> None:
        """Bound acknowledged full reports; pending evidence is never pruned.

        Small content hashes survive for this root's resumable lifetime, like
        managed terminal receipts. They carry no report text or task credit.
        """
        candidates = []
        for digest, fact in index['entries'].items():
            if not fact['acknowledged'] or fact['event_id'] in pending:
                continue
            for folder in ('unmanaged-callbacks', 'unmanaged-recovery'):
                path = self.root / folder / (digest + '.json')
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                candidates.append((info.st_mtime_ns, path, info.st_size))
        retained_bytes, retained_count = 0, 0
        for _, path, size in sorted(candidates, reverse=True):
            if (retained_count >= _UNMANAGED_REPORT_COUNT
                    or retained_bytes + size > _UNMANAGED_REPORT_BYTES):
                path.unlink(missing_ok=True)
            else:
                retained_bytes += size
                retained_count += 1

    def finish_session_events(self, record: dict[str, Any], event_ids: set[str]) -> None:
        """Acknowledge only after the project transaction has committed."""
        if not event_ids:
            return
        index = None
        if record['project']:
            index_path, index = self._unmanaged_observations(
                record['provider'], record['session'], project_key(Path(record['project'])),
                record['generation'])
            changed = False
            for fact in index['entries'].values():
                if fact['event_id'] in event_ids and not fact['acknowledged']:
                    fact['acknowledged'] = True
                    changed = True
            # Commit the tiny replay facts first. If ACK fails, the unchanged
            # inbox still protects every pending full report from retention.
            if changed:
                self._write_json(index_path, index)
        record["pending"] = [item for item in record["pending"]
                             if item.get("event_id") not in event_ids]
        self._write_json(self._session_path(record["provider"], record["session"]), record)
        if index is not None:
            try:
                self._prune_unmanaged_reports(index, {item['event_id'] for item in record['pending']})
            except OSError:
                # Cleanup failure must preserve data, not turn an ordinary
                # completed observation into a new managed Stop obligation.
                pass

    def rebind_session(self, record: dict[str, Any], project: Path, retired: set[str]) -> dict[str, Any]:
        """Move a settled native session to a new root project at a task boundary."""
        project = Path(project).resolve()
        record["project"] = str(project)
        record["state_name"] = self._path(project).name
        # Keep migrated=True: old retained hooks may still write this session.
        record["generation"] = record.get("generation", 1) + 1
        record["retired_agents"] = sorted(set(record.get("retired_agents", ())) | retired)
        self._write_json(self._session_path(record["provider"], record["session"]), record)
        return record


    @contextmanager
    def session_lock(self, provider: str, session: str, timeout: float | None = None) -> Iterator[None]:
        """Serialize owner selection with updates from this root session."""
        digest = hashlib.sha256(f"{provider}\0{session}".encode()).hexdigest()
        with _locked(self.root / f".session-{digest}", timeout=timeout):
            yield

    def active_owner_paths(
        self, provider: str, session: str, parent: str = "", include_ancestry: bool = False,
    ) -> tuple[Path, ...] | None:
        """Find active root or child ancestors; None means a snapshot could not be read."""
        matches: list[Path] = []
        deadline = time.monotonic() + 5
        for path in self.root.glob("*.v2.json"):
            if path.is_symlink():
                continue
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                state = _state_from_dict(json.loads(_read_owner_snapshot(path, remaining)))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # A missing or invalid snapshot cannot prove this root has no
                # other owner. Defer instead of trusting the event's CWD.
                return None
            for run in state.active_runs.values():
                if run.provider not in {"", provider}:
                    continue
                ancestry = ({run.session_id, run.lead_identity,
                             *(item.identity for item in run.delegations)}
                            if include_ancestry else {run.session_id})
                if (session and session in ancestry) or (parent and parent in ancestry):
                    matches.append(path)
                    break
        return tuple(matches)

    def update_owned(
        self,
        path: Path,
        provider: str,
        session: str,
        transition: Callable[[ProjectState], tuple[ProjectState, _UpdateResult]],
    ) -> _UpdateResult | None:
        """Update only while the selected file still owns this exact root."""
        with _locked(path):
            if not path.is_file() or path.is_symlink():
                return None
            state = _state_from_dict(json.loads(path.read_text(encoding="utf-8")))
            run = state.active_runs.get(f"{provider}:{session}")
            if not run or run.session_id != session or run.provider not in {"", provider}:
                return None
            next_state, result = transition(state)
            self._write(path, replace(next_state, recent_runs=next_state.recent_runs[-20:]))
            return result

    def update_path(
        self, path: Path,
        transition: Callable[[ProjectState], tuple[ProjectState, _UpdateResult]],
    ) -> _UpdateResult | None:
        """Update a previously bound existing state file without guessing its CWD."""
        with _locked(path):
            if not path.is_file() or path.is_symlink():
                return None
            state = _state_from_dict(json.loads(path.read_text(encoding="utf-8")))
            next_state, result = transition(state)
            self._write(path, replace(next_state, recent_runs=next_state.recent_runs[-20:]))
            return result

    def read_path(self, path: Path) -> ProjectState | None:
        with _locked(path):
            if not path.is_file() or path.is_symlink():
                return None
            return _state_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def load(self, project: Path) -> ProjectState:
        path = self._path(project)
        with _locked(path):
            return self._load_unlocked(project, path)

    def save(self, project: Path, state: ProjectState) -> None:
        path = self._path(project)
        with _locked(path):
            self._write(path, replace(state, recent_runs=state.recent_runs[-20:]))

    def update(
        self,
        project: Path,
        transition: Callable[[ProjectState], tuple[ProjectState, _UpdateResult]],
    ) -> _UpdateResult:
        """Apply one read-reduce-write transaction under the project lock."""
        path = self._path(project)
        with _locked(path):
            state = self._load_unlocked(project, path)
            next_state, result = transition(state)
            self._write(path, replace(next_state, recent_runs=next_state.recent_runs[-20:]))
            return result

    def _load_unlocked(self, project: Path, path: Path) -> ProjectState:
        if not path.exists():
            previous = path.with_name(f"{project_key(project)}.json")
            if previous.exists():
                try:
                    raw = _object(json.loads(previous.read_text(encoding="utf-8")), "state")
                    if raw.get("schema_version") in {1, SCHEMA_VERSION}:
                        imported = _state_from_dict(raw)
                    elif raw.get("schema_version") in {None, 0}:
                        imported = self._migrated_state(raw)
                    else:
                        raise ValueError("unsupported legacy state schema")
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(f"legacy state unreadable; original preserved at {previous}") from error
                # The previous file belongs to old hooks. Never rewrite or
                # remove it: they may still be running alongside this build.
                self._write(path, imported)
                return imported
            imported = self._import_legacy(project, path)
            return imported if imported is not None else ProjectState()
        try:
            raw = _object(json.loads(path.read_text(encoding="utf-8")), "state")
            version = raw.get("schema_version")
            if version in {1, SCHEMA_VERSION}:
                loaded = _state_from_dict(raw)
                if version == 1:
                    self._write(path, loaded)
                return loaded
            if version in {None, 0}:
                return self._migrate(path, raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"state unreadable; original preserved at {path}") from error
        raise ValueError(f"unsupported state schema {version!r}; original preserved at {path}")

    def _import_legacy(self, project: Path, destination: Path) -> ProjectState | None:
        if not self.legacy_roots:
            return None
        keys = legacy_project_keys(project)
        for root in self.legacy_roots:
            for key in keys:
                source = root / "projects" / f"{key}.json"
                if not source.is_file():
                    continue
                try:
                    raw = _object(json.loads(source.read_text(encoding="utf-8")), "legacy state")
                    migrated = self._migrated_state(raw)
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                self._archive(source, "pre-1.0", sanitize=True)
                self._write(destination, migrated)
                return migrated
        return None

    def _migrate(self, path: Path, raw: dict[str, Any]) -> ProjectState:
        migrated = self._migrated_state(raw)
        self._archive(path, "pre-1.0", sanitize=True)
        self._write(path, migrated)
        return migrated

    @staticmethod
    def _migrated_state(raw: dict[str, Any]) -> ProjectState:
        enabled = raw.get("enabled", False)
        configuration = raw.get("configuration", {})
        if not isinstance(enabled, bool):
            raise ValueError("legacy enabled flag must be a boolean")
        configuration = _object(configuration, "legacy configuration")
        migrated = ProjectState(
            enabled=enabled,
            configuration=configuration,
            needs_reassessment=True,
        )
        return migrated

    @staticmethod
    def _archive(path: Path, reason: str, sanitize: bool = False) -> None:
        destination = path.with_name(f"{path.name}.{reason}-{_timestamp()}")
        if not sanitize:
            os.replace(path, destination)
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        destination.write_text(json.dumps(_redact(raw), sort_keys=True, separators=(",", ":")) + "\n")
        destination.chmod(0o600)
        path.unlink()

    @staticmethod
    def _write(path: Path, state: ProjectState) -> None:
        StateStore._write_json(path, _state_to_dict(state))

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(_redact(value), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
