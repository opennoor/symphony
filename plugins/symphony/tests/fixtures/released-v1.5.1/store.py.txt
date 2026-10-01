"""Atomic JSON persistence for Symphony project state."""

from __future__ import annotations

import hashlib
import json
import os
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
}


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
    return ProjectState(
        enabled=enabled,
        configuration=_object(value.get("configuration", {}), "state.configuration"),
        activation=activation,
        active_run=parsed_run,
        active_runs=active_runs,
        recent_runs=recent_runs,
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
    if key.lower().replace("-", "_") in _SECRET_KEYS:
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
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _local_lock(path), path.with_name(path.name + ".lock").open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            deadline = time.monotonic() + 5
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"state lock timed out: {path}")
                    time.sleep(0.02)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


class StateStore:
    def __init__(self, root: Path, legacy_roots: tuple[Path, ...] = ()):
        self.root = Path(root)
        self.legacy_roots = tuple(Path(item) for item in legacy_roots)

    def _path(self, project: Path) -> Path:
        return self.root / f"{project_key(project)}.v2.json"

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
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(_redact(_state_to_dict(state)), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
