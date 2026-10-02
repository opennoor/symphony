#!/usr/bin/env python3
"""Check Claude's native background Agent worktree hooks in disposable state.

The capture hook stores lifecycle identity and classified Agent failure facts,
without raw tool text or paths. This is a separate
regression from the shared-checkout native managed concurrency harness.
"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import native_managed_concurrency as native


PROFILE = "sonnet-5-5"
MODEL = "claude-sonnet-5-5"
LEAD_AGENT = f"symphony:symphony-lead-{MODEL}-low"


CAPTURE = '''import hashlib, json, os, pathlib, re, sys, uuid
try:
    payload = json.load(sys.stdin)
    record = {key: payload.get(key) for key in
              ("hook_event_name", "session_id", "cwd", "agent_id", "agent_type",
               "parent_id", "parent_agent_id", "parent_session_id")}
    if payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_name") == "Agent":
        tool = payload.get("tool_input") or {}
        record["tool_name"] = "Agent"
        record["tool_input"] = {key: tool.get(key) for key in
                                ("subagent_type", "isolation", "run_in_background")}
    elif payload.get("hook_event_name") == "PreToolUse":
        raise SystemExit(0)
    if payload.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure"):
        if payload.get("tool_name") != "Agent":
            raise SystemExit(0)
        response = payload.get("tool_response") or payload.get("tool_result") or {}
        failed = (payload.get("hook_event_name") == "PostToolUseFailure"
                  or payload.get("is_error") is True
                  or isinstance(response, dict) and (response.get("is_error") is True
                      or response.get("status") in ("failed", "error", "rejected")))
        result = payload.get("error") or response or ""
        if isinstance(result, dict):
            result = result.get("error") or result.get("message") or result.get("content") or ""
        if isinstance(result, list):
            result = " ".join(str(item.get("text", "")) for item in result if isinstance(item, dict))
        result = str(result)
        record["tool_failure_hash"] = hashlib.sha256(result.encode()).hexdigest()[:12] if result else None
        if failed:
            lowered = result.lower()
            match = re.search(
                r"Refusing to use (.+?) as an isolation worktree:\\s*"
                r"git resolves its working tree to ([^\\r\\n]+)", result, re.I)
            if match:
                pinned = match.group(1).strip().replace("\\\\", "/").rstrip("/")
                resolved = match.group(2).strip()
                note = "(a core.worktree redirect, or a checkout discovered above it)"
                if note in resolved:
                    resolved = resolved.split(note, 1)[0].strip().rstrip(",.")
                if "(" not in resolved or re.search(r"\\([^)]*\\)[\\\\/]", resolved):
                    resolved = resolved.replace("\\\\", "/").rstrip("/")
                    pinned_path = pathlib.Path(pinned)
                    resolved_path = pathlib.Path(resolved)
                    try:
                        same_file = os.path.samefile(pinned_path, resolved_path)
                    except (OSError, ValueError):
                        same_file = None
                    try:
                        pinned_real = os.path.realpath(pinned)
                        resolved_real = os.path.realpath(resolved)
                        pinned_exists = pinned_path.exists()
                        resolved_exists = resolved_path.exists()
                    except (OSError, ValueError):
                        pinned_real = resolved_real = None
                        pinned_exists = resolved_exists = None
                    record["worktree_path_relation"] = {
                        "exact_match": pinned == resolved,
                        "casefold_match": pinned.casefold() == resolved.casefold(),
                        "realpath_casefold_match": (
                            pinned_real.casefold() == resolved_real.casefold()
                            if pinned_real is not None and resolved_real is not None else None),
                        "same_file": same_file,
                        "pinned_exists": pinned_exists,
                        "resolved_exists": resolved_exists,
                        "drive_letter_case_differs": (
                            len(pinned) > 1 and len(resolved) > 1
                            and pinned[1] == resolved[1] == ":"
                            and pinned[0] != resolved[0]
                            and pinned[0].casefold() == resolved[0].casefold()),
                    }
                record["native_error_shape"] = "git-worktree-resolves-elsewhere"
            elif "Refusing to use" in result and "git could not be run to resolve it" in result:
                record["native_error_shape"] = "git-worktree-identity-unavailable"
            else:
                record["native_error_shape"] = "unclassified-agent-failure"
            reasons = {
                "not-a-repo": r"not a git repository|outside repository",
                "invalid-ref": r"invalid reference|unknown revision|bad revision|not a valid object name|reference is not a tree|ambiguous argument",
                "long-path": r"filename too long|path too long|longpaths?|error 206|error code 206",
                "permission": r"permission denied|access (?:is )?denied|operation not permitted|requires elevation",
                "directory-exists": r"already exists|already used by worktree|already checked out|file exists",
                "index-lock": r"index\\.lock|another git process|unable to create[^\\n]*\\.lock",
                "git-missing": r"git: command not found|git[^\\n]*(?:not found|not recognized)|cannot find[^\\n]*git",
                "worktree-path-mismatch": r"git resolves its working tree|work-tree-elsewhere|write outside the worktree",
                "git-cannot-resolve": r"git could not be run to resolve|git identity could not be verified",
            }
            record["tool_failure_reason"] = next(
                (name for name, pattern in reasons.items() if re.search(pattern, lowered)), "unknown")
            record["tool_failure_flags"] = sorted(name for name, pattern in {
                "worktree": r"worktree", "git": r"\\bgit\\b", "isolation": r"isolat",
                "unsupported": r"unsupported|not supported", "missing": r"not found|does not exist|missing",
                "permission": r"permission|access denied", "dirty": r"uncommitted|dirty",
                "path": r"directory|path|folder", "collision": r"already exists|conflict",
                "launch": r"spawn|launch|start", "failed": r"fail|error|reject",
            }.items() if re.search(pattern, lowered))
            record["tool_failure_unknown"] = record["tool_failure_reason"] == "unknown"
    destination = pathlib.Path(os.environ["SYMPHONY_CAPTURE_DIR"])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / (str(uuid.uuid4()) + ".json")).write_text(json.dumps(record))
except (KeyError, TypeError, ValueError, OSError):
    pass
'''


def capture_records(directory):
    records = []
    for path in directory.glob("*.json"):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    return records


GIT_FAILURE_REASONS = {
    "not-a-repo": r"not a git repository|outside repository",
    "invalid-ref": r"invalid reference|unknown revision|bad revision|not a valid object name|reference is not a tree|ambiguous argument",
    "long-path": r"filename too long|path too long|longpaths?|error 206|error code 206",
    "permission": r"permission denied|access (?:is )?denied|operation not permitted|requires elevation",
    "directory-exists": r"already exists|already used by worktree|already checked out|file exists",
    "index-lock": r"index\.lock|another git process|unable to create[^\n]*\.lock",
    "git-missing": r"git: command not found|git[^\n]*(?:not found|not recognized)|cannot find[^\n]*git",
}


def git_failure_reason(message):
    lowered = message.lower()
    return next((name for name, pattern in GIT_FAILURE_REASONS.items()
                 if re.search(pattern, lowered)), "unknown")


def git_worktree_preflight(root, project):
    """Check Git worktree support in the same disposable repository as Claude."""
    target = root / "git-preflight-worktree"
    receipt = {"attempted": True, "success": False, "stages": []}
    commands = (("add", ["git", "worktree", "add", "--detach", str(target), "HEAD"]),
                ("remove", ["git", "worktree", "remove", "--force", str(target)]))
    for stage, command in commands:
        try:
            result = subprocess.run(command, cwd=project, capture_output=True, text=True,
                                    timeout=20, check=False)
            detail = (result.stderr or "") + (result.stdout or "")
            succeeded = result.returncode == 0
            reason = None if succeeded else git_failure_reason(detail)
        except subprocess.TimeoutExpired:
            detail, succeeded, reason = "timeout", False, "timeout"
        except FileNotFoundError:
            detail, succeeded, reason = "git executable missing", False, "git-missing"
        except OSError as error:
            detail, succeeded, reason = str(error), False, git_failure_reason(str(error))
        receipt["stages"].append({"stage": stage, "success": succeeded,
                                  "reason": reason,
                                  "detail_hash": sha256(detail.encode()).hexdigest()[:12]
                                  if not succeeded else None})
        if not succeeded:
            break
    receipt["success"] = len(receipt["stages"]) == 2 and all(
        stage["success"] for stage in receipt["stages"])
    (root / "git-worktree-preflight.json").write_text(json.dumps(receipt))
    if not receipt["success"]:
        failed = receipt["stages"][-1]
        raise RuntimeError(f"git worktree preflight {failed['stage']} failed: {failed['reason']}")
    return receipt


def scratch_claude(root, package):
    home = root / "claude-home"
    home.mkdir()
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        source = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        credentials = source / ".credentials.json"
        if not credentials.is_file():
            raise RuntimeError("native Claude check needs ANTHROPIC_API_KEY or existing auth")
        shutil.copyfile(credentials, home / ".credentials.json")
        (home / ".credentials.json").chmod(0o600)
        status = json.loads(native.claude_command(env, root, "auth", "status", "--json").stdout)
        if not status.get("loggedIn"):
            raise RuntimeError("disposable Claude config is not authenticated")
    version = native.package_version(package)
    market = native.marketplace(root, "symphony-isolated", package, version)
    native.claude_command(env, root, "plugin", "marketplace", "add", str(market))
    native.claude_command(env, root, "plugin", "install", "-y", "symphony@symphony-isolated")
    if not (home / "plugins" / "cache" / "symphony-isolated" / "symphony"
            / version / "hooks" / "hooks.json").is_file():
        raise RuntimeError("candidate Claude plugin missing from disposable config")
    capture = root / "capture_hook.py"
    capture.write_text(CAPTURE)
    command = f'python -I "{str(capture).replace(chr(92), "/")}"'
    settings_file = home / "settings.json"
    settings = json.loads(settings_file.read_text())
    settings["hooks"] = {
        "PreToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": command}]}],
        "PostToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": command}]}],
        "PostToolUseFailure": [{"matcher": "Agent", "hooks": [{"type": "command", "command": command}]}],
        "SubagentStart": [{"hooks": [{"type": "command", "command": command}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": command}]}],
    }
    settings_file.write_text(json.dumps(settings))
    native.install_hook_capture('claude', root, home, private_children=True,
                                candidate_source=package)
    return env, home


def verify_lead_model(env, root):
    result = native.claude_command(
        env, root, "--settings", '{"disableAllHooks":true}', "--print",
        "--no-session-persistence", "--tools", "", "--disallowedTools", "mcp__*",
        "--agent", LEAD_AGENT, "--max-budget-usd", "0.25", "--output-format", "json",
        "Reply only OK. Do not use tools.", timeout=30,
    )
    report = json.loads(result.stdout)
    usage = report.get("modelUsage") if isinstance(report, dict) else None
    if not isinstance(usage, dict) or report.get("is_error") or set(usage) != {MODEL}:
        raise RuntimeError("packaged isolated lead did not run on the exact Sonnet 5.5 model")
    return {"model": MODEL, "evidence": "native packaged lead accepted; exact modelUsage"}


def prompt(project):
    route = ('{"size":"small","complexity":"simple","risk":"normal",'
             '"rationale":"disposable native worktree hook check","topology":"delegated"}')
    gate = str(project / "gate.py").replace("\\", "/")
    gate_command = shlex.join([str(Path(sys.executable)).replace("\\", "/"), gate, "isolated"])
    worker_packet = {'subagent_type': f'symphony:symphony-worker-{MODEL}-low',
                     'prompt': 'SYMPHONY_ROLE: worker\n' + json.dumps({
                         'objective': 'Run the isolated fixture gate once and return GATE_RELEASED.',
                         'ownership': 'Only the supplied gate command in the owning isolated worktree.',
                         'constraints': 'Run exactly ' + gate_command + '. Do not edit files or delegate.',
                         'acceptance_check': 'The command exits zero after printing GATE_RELEASED.',
                         'return_contract': 'Return exactly GATE_RELEASED on successful completion.',
                         'size': 'small', 'complexity': 'simple'})}
    lead_packet = {"subagent_type": LEAD_AGENT,
                   "description": "Check isolated Symphony lead gate",
                   "run_in_background": True,
                   "isolation": "worktree",
                   "prompt": (f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {route}\n"
                              'Spawn exactly one worker using WORKER_SPAWN_PACKET unchanged. '
                              'Await its successful GATE_RELEASED result, verify it, '
                              'then return exactly SYMPHONY_OUTCOME: {"status":"completed"}. '
                              'WORKER_SPAWN_PACKET: ' + json.dumps(worker_packet))}
    return (
        "/symphony:start Disposable native Claude isolated Agent hook check. "
        "Spawn one Symphony assessor and await its assessment: SYMPHONY_ASSESSMENT: "
        + route + ". Spawn one Symphony lead with SYMPHONY_ROUTE: " + route +
        " using native Agent. Pass the following exact Agent tool arguments. "
        "LEAD_AGENT_PACKET: " + json.dumps(lead_packet) +
        ". The isolation=worktree field is required. "
        "If Agent rejects that packet, report the error without retrying without isolation. "
        "Await the background lead and finish after its result. Do not make other "
        "worktrees or edit the fixture."
    )


def run_case(root, package, timeout, budget):
    # projects() creates its own primary directory inside the case directory.
    case = root / "case"
    case.mkdir()
    project, _ = native.projects(case, False)
    # Windows tempfile may use an 8.3 alias; Claude compares its isolation
    # worktree path with Git's expanded path even when both name one directory.
    project = project.resolve(strict=True)
    preflight = git_worktree_preflight(root, project)
    (project / "gate.py").write_text(native.GATE)
    state = case / "state"
    state.mkdir()
    runtime = case / "retained runtimes"
    capture = root / "captured-hooks"
    capture.mkdir()
    env, home = scratch_claude(root, package)
    env.update({"SYMPHONY_STATE_DIR": str(state), "SYMPHONY_RUNTIME_DIR": str(runtime),
                "SYMPHONY_PROFILE": PROFILE, "SYMPHONY_CAPTURE_DIR": str(capture)})
    model_check = verify_lead_model(env, root)
    session = str(uuid.uuid4())
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude CLI is missing")
    logs = root / "logs"
    logs.mkdir()
    stdout = (logs / "root.stdout").open("w", encoding="utf-8")
    stderr = (logs / "root.errors").open("w", encoding="utf-8")
    command = [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
               "--permission-mode", "bypassPermissions", "--session-id", session,
               "--output-format", "json", prompt(project)]
    process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                               stdout=stdout, stderr=stderr, shell=False)
    stdout.close()
    stderr.close()
    deadline = time.monotonic() + timeout
    state_path = native.state_file(state, project)
    try:
        while time.monotonic() < deadline:
            records = capture_records(capture)
            lead_starts = [item for item in records if item.get("hook_event_name") == "SubagentStart"
                           and item.get("agent_type") == LEAD_AGENT]
            isolated_starts = [item for item in lead_starts
                               if item.get("cwd")
                               and Path(item["cwd"]).resolve() != project.resolve()
                               and ".claude/worktrees/" in
                               str(item["cwd"]).replace("\\", "/")]
            isolated_calls = [item for item in records if item.get("tool_name") == "Agent"
                              and (item.get("tool_input") or {}).get("subagent_type") == LEAD_AGENT
                              and (item.get("tool_input") or {}).get("isolation") == "worktree"
                              and (item.get("tool_input") or {}).get("run_in_background") is True]
            if (project / "isolated.ready").exists() and isolated_starts and isolated_calls:
                break
            if process.poll() not in (None, 0):
                raise RuntimeError(f"native Claude root exited {process.returncode} before isolated lead gate")
            time.sleep(.25)
        else:
            raise RuntimeError("isolated background lead did not reach the fixture gate")
        lead_start = isolated_starts[-1]
        lead_id = lead_start.get("agent_id")
        if not lead_id:
            raise RuntimeError("SubagentStart hook did not contain lead agent_id")
        if not state_path.is_file():
            raise RuntimeError("isolated lead did not register in its root project")
        before_release = native.read_state_snapshot(state_path)
        original_run = before_release.get("active_runs", {}).get(f"claude:{session}")
        if (not original_run or original_run.get("lead_identity") != lead_id
                or original_run.get("status") != "active"):
            raise RuntimeError("isolated lead was not owned by the original active run")
        original_run_id = original_run.get("run_id")
        (project / "release").touch()
        resumed = False
        while time.monotonic() < deadline:
            records = capture_records(capture)
            stopped = any(item.get("hook_event_name") == "SubagentStop"
                          and item.get("agent_id") == lead_id
                          and item.get("cwd") == lead_start["cwd"] for item in records)
            if stopped and process.poll() == 0 and not resumed:
                native.resume_claude(env, project, session, budget, deadline,
                                     logs, "isolated")
                resumed = True
            if resumed and state_path.exists():
                document = native.read_state_snapshot(state_path)
                matching = [run for run in document.get("recent_runs", [])
                            if run.get("session_id") == session]
                if (len(matching) == 1 and matching[0].get("status") == "completed"
                        and matching[0].get("lead_identity") == lead_id
                        and matching[0].get("run_id") == original_run_id
                        and (matching[0].get("outcome") or {}).get("status") == "completed"):
                    version = native.package_version(package)
                    activation = document.get("activation", {}).get("claude", {})
                    profiles = [activation, *activation.get("session_profiles", [])]
                    if not any(item.get("session_id") == session
                               and item.get("plugin_version") == version for item in profiles):
                        raise RuntimeError("isolated root used another Symphony plugin version")
                    for record_path in state.glob(".session-*.json"):
                        record = native.read_state_snapshot(record_path)
                        if record.get("pending") or record.get("overflow"):
                            raise RuntimeError("isolated root retained unresolved callbacks")
                    return {"provider": "claude", "case": "isolated-worktree",
                            "session_id": session, "lead_id": lead_id,
                            "run_id": original_run_id,
                            "candidate_version": version,
                            "pending_callbacks": 0,
                            "isolated_child_cwd_different": True,
                            "isolation_requested": True,
                            "git_worktree_preflight": preflight,
                            "native_model_check": model_check,
                            "captured_hook_events": sorted({item.get("hook_event_name") for item in records}),
                            "durable_parent_outcome": "completed"}
            if process.poll() not in (None, 0):
                raise RuntimeError(f"native Claude root exited {process.returncode}")
            time.sleep(.25)
        raise RuntimeError("isolated lead hooks did not reconcile a durable parent outcome")
    finally:
        (project / "release").touch()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # main() collects fixed failure facts before its TemporaryDirectory
        # removes the private native home and all other disposable evidence.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-plugin-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "plugins" / "symphony")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--claude-budget-usd", type=float, default=3.0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symphony-claude-isolated-",
                                     ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        try:
            result = run_case(root, args.candidate_plugin_root, args.timeout,
                              args.claude_budget_usd)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            preflight_path = root / "git-worktree-preflight.json"
            evidence = {"provider": "claude", "case": "isolated-worktree",
                        "error": str(error),
                        "git_worktree_preflight": json.loads(preflight_path.read_text())
                        if preflight_path.is_file() else {"attempted": False},
                        "hooks": capture_records(root / "captured-hooks"),
                        "state": native.failure_state(root, "claude")}
            destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "native-claude-isolated-worktree-failure.json").write_text(
                    json.dumps(evidence, indent=2))
            print(json.dumps(evidence), file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
