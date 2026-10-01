#!/usr/bin/env python3
"""Read-only proof that this Codex session ran the loaded Symphony hook."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from symphony import HOOK_SCHEMA_VERSION, PLUGIN_VERSION  # noqa: E402
from symphony.store import project_key  # noqa: E402


def verified_retained(record):
    """Check the old hook's retained files without executing their code."""
    runtime = record.get("runtime_root")
    if not isinstance(runtime, str) or not runtime:
        return False
    source = Path(runtime)
    base = Path(os.environ.get("SYMPHONY_RUNTIME_DIR", Path.home() / ".symphony" / "runtimes"))
    try:
        if (not source.is_absolute() or source.is_symlink() or source.parent.resolve() != base.resolve()
                or len(source.name) != 64 or any(char not in "0123456789abcdef" for char in source.name)):
            return False
        entries = list(source.rglob("*"))
        if not entries or any(path.is_symlink() for path in entries):
            return False
        files = sorted((path for path in entries if path.is_file()),
                       key=lambda path: path.relative_to(source).as_posix())
        aggregate = hashlib.sha256()
        for path in files:
            relative = path.relative_to(source).as_posix()
            aggregate.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        if aggregate.hexdigest() != source.name:
            return False
        tree = ast.parse((source / "symphony" / "__init__.py").read_text(encoding="utf-8"))
        constants = {target.id: ast.literal_eval(node.value)
                     for node in tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)
                     and target.id in {"PLUGIN_VERSION", "HOOK_SCHEMA_VERSION"}}
        return (constants.get("PLUGIN_VERSION") == record.get("plugin_version")
                and constants.get("HOOK_SCHEMA_VERSION") == record.get("hook_schema_version"))
    except (OSError, ValueError, SyntaxError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, help="Original reviewed root when executing a retained checker")
    args = parser.parse_args()
    session = os.environ.get("CODEX_SESSION_ID")
    if not session:
        print("pending verification: Codex session ID is unavailable")
        return 1

    root = Path(__file__).resolve().parents[1]
    expected_root = args.plugin_root.resolve() if args.plugin_root else root
    state_dir = Path(os.environ.get("SYMPHONY_STATE_DIR", Path.home() / ".symphony" / "state"))
    path = state_dir / f"{project_key(Path.cwd())}.v2.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        activation = document["activation"]["codex"]
    except (OSError, ValueError, KeyError, TypeError):
        print("pending verification: no readable Codex heartbeat for this project")
        return 1

    def same_path(left, right):
        try:
            return (isinstance(left, str) and
                    os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(right.resolve())))
        except (OSError, ValueError, RuntimeError):
            return False

    def matches(record):
        return (
            isinstance(record, dict)
            and record.get("session_id") == session
            and record.get("plugin_version") == PLUGIN_VERSION
            and same_path(record.get("plugin_root"), expected_root)
            and (expected_root == root or same_path(record.get("runtime_root"), root))
            and record.get("hook_schema_version") == HOOK_SCHEMA_VERSION
            and bool(record.get("observed_at"))
        )

    def matches_old(record):
        return (
            isinstance(record, dict)
            and record.get("session_id") == session
            and bool(record.get("observed_at"))
            and isinstance(record.get("plugin_root"), str)
            and not same_path(record["plugin_root"], expected_root)
            and verified_retained(record)
        )

    # Another live session can replace the provider's latest activation slot.
    # Active owners retain a complete heartbeat in session_profiles even after
    # the bounded event history has discarded their original hook event.
    sessions = activation.get("session_profiles", ()) if isinstance(activation, dict) else ()
    retained = (record for record in sessions if isinstance(record, dict))
    historical = (
        {**event["payload"], "observed_at": event.get("observed_at")}
        for event in document.get("event_history", ())
        if isinstance(event, dict) and event.get("kind") == "session_heartbeat"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("provider") == "codex"
    )
    records = ([activation] if isinstance(activation, dict) and activation.get("state") == "guarded" else [])
    records.extend(retained)
    old_records = records.copy()
    records.extend(historical)
    if any(map(matches, records)) or any(map(matches_old, old_records)):
        print("guarded: matching current-session heartbeat")
        return 0

    print("pending verification: heartbeat does not match this session and loaded plugin")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
