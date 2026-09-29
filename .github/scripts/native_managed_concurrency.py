#!/usr/bin/env python3
"""Exercise concurrent Symphony runs through installed native Codex or Claude CLIs.

Run after the CI job has installed and authenticated the selected provider's
plugin. All Git checkouts, state, runtime pins, and gate files are temporary.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


GATE = '''import pathlib, sys, time
root = pathlib.Path(__file__).resolve().parent
name = sys.argv[1]
(root / (name + ".ready")).touch()
deadline = time.monotonic() + 900
while not (root / "release").exists():
    if time.monotonic() > deadline:
        raise SystemExit("gate timed out")
    time.sleep(.1)
'''


def run_git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def projects(root, separate):
    primary = root / "primary"
    primary.mkdir()
    run_git("init", "-b", "first", cwd=primary)
    run_git("config", "user.email", "ci@example.invalid", cwd=primary)
    run_git("config", "user.name", "Symphony CI", cwd=primary)
    (primary / "README.md").write_text("Disposable Symphony native concurrency check.\n")
    run_git("add", "README.md", cwd=primary)
    run_git("commit", "-m", "fixture", cwd=primary)
    if not separate:
        return primary, primary
    second = root / "second"
    run_git("worktree", "add", "-b", "second", str(second), cwd=primary)
    for project, expected in ((primary, "first"), (second, "second")):
        actual = subprocess.run(["git", "branch", "--show-current"], cwd=project,
                                check=True, capture_output=True, text=True).stdout.strip()
        if actual != expected:
            raise RuntimeError(f"expected branch {expected}, got {actual}")
    return primary, second


def state_file(state_dir, project):
    from hashlib import sha256
    key = sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()
    return state_dir / (key + ".v2.json")


def prompt(provider, label, recover):
    control = "$symphony:symphony start" if provider == "codex" else "/symphony:start"
    route = '{"size":"small","complexity":"simple","risk":"normal","rationale":"disposable native CI gate","topology":"direct"}'
    recovery = (
        'The lead MUST finish its first turn after the gate with exactly '
        'SYMPHONY_OUTCOME: {"status":"blocked"}. Await that result. '
        'Then use followup_task on the SAME lead identity, asking it to return exactly '
        'SYMPHONY_OUTCOME: {"status":"completed"}; await the follow-up. '
        if recover else 'After the gate returns, return SYMPHONY_OUTCOME: {"status":"completed"}. '
    )
    return (
        f"{control} Disposable native CI lifecycle {label}. "
        "Use Symphony and the native agent tools. Spawn one assessor that returns exactly "
        f"SYMPHONY_ASSESSMENT: {route}. Await it. Spawn one lead with "
        f"SYMPHONY_ROUTE: {route} and the exact model and effort selected by Symphony. "
        f"The lead's entire task is to run `python gate.py {label}` once from this "
        "disposable checkout, then report its outcome. " + recovery +
        "Await all children and finish briefly. Do not edit files or inspect other projects."
    )


def launch(provider, project, label, recover, env, log_dir, budget, session):
    executable = shutil.which(provider)
    if not executable:
        raise RuntimeError(f"{provider} CLI is missing")
    output = log_dir / f"{label}.output"
    errors = log_dir / f"{label}.errors"
    if provider == "codex":
        command = [executable, "exec", "--dangerously-bypass-hook-trust",
                   "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                   "--model", "gpt-6-luna", "-C", str(project),
                   "--output-last-message", str(output), prompt(provider, label, recover)]
    else:
        command = [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
                   "--permission-mode", "bypassPermissions", "--session-id", session,
                   "--output-format", "json",
                   prompt(provider, label, recover)]
    stdout = (log_dir / f"{label}.stdout").open("w", encoding="utf-8")
    stderr = errors.open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                               stdout=stdout, stderr=stderr, shell=False)
    stdout.close()
    stderr.close()
    return process


def codex_session(logs, label):
    banner = (logs / f"{label}.errors").read_text(errors="replace")
    match = re.search(r"^session id: ([0-9a-f-]+)$", banner, re.MULTILINE)
    if not match:
        raise RuntimeError(f"{label}: native Codex session ID missing from CLI banner")
    return match.group(1)


def check_case(provider, root, separate, timeout, budget):
    case = root / ("worktrees" if separate else "same-worktree")
    case.mkdir()
    first, second = projects(case, separate)
    for project in {first, second}:
        (project / "gate.py").write_text(GATE)
    state_dir = case / "state"
    state_dir.mkdir()
    env = {**os.environ, "SYMPHONY_STATE_DIR": str(state_dir),
           "SYMPHONY_RUNTIME_DIR": str(case / "retained runtimes"),
           "SYMPHONY_PROFILE": "base" if provider == "codex" else "sonnet"}
    if provider == "claude":
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    logs = case / "logs"
    logs.mkdir()
    processes = {}
    sessions = {}
    try:
        for label, project in (("a", first), ("b", second)):
            # Keep gate files outside Git; each checkout sees only its own gate.
            (project / "release").unlink(missing_ok=True)
            sessions[label] = str(uuid.uuid4()) if provider == "claude" else ""
            processes[label] = launch(provider, project, label, label == "a" and provider == "codex", env, logs,
                                      budget, sessions[label])
        deadline = time.monotonic() + timeout
        paths = {label: state_file(state_dir, project) for label, project in (("a", first), ("b", second))}
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes.values()):
                raise RuntimeError("native CLI exited before both managed leads reached the gate")
            if all((project / f"{label}.ready").exists()
                   for label, project in (("a", first), ("b", second))):
                break
            time.sleep(.25)
        else:
            raise RuntimeError("native leads did not overlap before deadline")
        if provider == "codex":
            sessions = {label: codex_session(logs, label) for label in processes}
        docs = {label: json.loads(path.read_text()) for label, path in paths.items()}
        observed_leads = {}
        if separate:
            for label, doc in docs.items():
                owned = [run for key, run in doc.get("active_runs", {}).items()
                         if key.startswith(provider + ":")]
                if (len(owned) != 1 or owned[0].get("session_id") != sessions[label]
                        or not owned[0].get("lead_identity")):
                    raise RuntimeError(f"{label}: expected one owned lead in its worktree")
                observed_leads[label] = owned[0]["lead_identity"]
            if paths["a"] == paths["b"]:
                raise RuntimeError("different worktrees shared a state key")
        else:
            owned = [run for key, run in docs["a"].get("active_runs", {}).items()
                     if key.startswith(provider + ":")]
            if len(owned) != 2 or len({run.get("session_id") for run in owned}) != 2:
                raise RuntimeError("same worktree did not preserve two independent owner sessions")
            if len({run.get("lead_identity") for run in owned}) != 2:
                raise RuntimeError("same worktree did not preserve two independent leads")
            by_session = {run.get("session_id"): run for run in owned}
            if set(by_session) != set(sessions.values()):
                raise RuntimeError("same worktree owner sessions differ from native CLI sessions")
            observed_leads = {label: by_session[session]["lead_identity"]
                              for label, session in sessions.items()}
        for project in {first, second}:
            (project / "release").touch()
        for label, process in processes.items():
            remaining = max(1, deadline - time.monotonic())
            if process.wait(timeout=remaining):
                raise RuntimeError(f"{label}: native CLI exited {process.returncode}")
        final_docs = {label: json.loads(path.read_text()) for label, path in paths.items()}
        for label, doc in final_docs.items():
            runs = [run for run in doc.get("recent_runs", []) if run.get("provider") == provider]
            matching = [run for run in runs if run.get("session_id") == sessions[label]]
            if len(matching) != 1 or matching[0].get("status") != "completed":
                raise RuntimeError(f"{label}: missing durable completed run")
            if (matching[0].get("outcome") or {}).get("status") != "completed":
                raise RuntimeError(f"{label}: missing durable completed outcome")
            if matching[0].get("lead_identity") != observed_leads[label]:
                raise RuntimeError(f"{label}: completed run changed lead identity")
            if label == "a" and provider == "codex":
                events = [event for event in doc.get("event_history", [])
                          if event.get("payload", {}).get("identity") == matching[0].get("lead_identity")]
                kinds = [event.get("kind") for event in events]
                if "lead_failed" not in kinds or "lead_completed" not in kinds:
                    raise RuntimeError("recovered lead lacks durable failed and completed events")
        return {"case": "different-branch-worktrees" if separate else "same-worktree",
                "observed_overlap": True, "completed": ["a", "b"]}
    finally:
        for project in {first, second}:
            (project / "release").touch()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--claude-budget-usd", type=float, default=3.0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symphony-native-managed-") as temporary:
        root = Path(temporary)
        try:
            results = [check_case(args.provider, root, separate, args.timeout,
                                  args.claude_budget_usd) for separate in (False, True)]
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(json.dumps({"provider": args.provider, "error": str(error),
                              "logs": str(root)}), file=sys.stderr)
            for path in root.rglob("*.v2.json"):
                document = json.loads(path.read_text())
                def summary(run):
                    task = str(run.get("task") or "")
                    return {"session_id": run.get("session_id"), "status": run.get("status"),
                            "outcome_status": (run.get("outcome") or {}).get("status"),
                            "lead_identity": run.get("lead_identity"),
                            "task_matches": [label for label in ("a", "b")
                                             if f"lifecycle {label}" in task]}
                print(json.dumps({"state_file": str(path),
                                  "active_runs": [summary(run) for run in document.get("active_runs", {}).values()],
                                  "recent_runs": [summary(run) for run in document.get("recent_runs", [])],
                                  "event_kinds": [event.get("kind") for event in document.get("event_history", [])]}),
                      file=sys.stderr)
            # Preserve command output as CI diagnostics before TemporaryDirectory removes it.
            for path in root.rglob("*.errors"):
                print(f"{path}: {path.read_text(errors='replace')[-4000:]}", file=sys.stderr)
            for path in root.rglob("*.output"):
                print(f"{path}: {path.read_text(errors='replace')[-2000:]}", file=sys.stderr)
            for path in root.rglob("*.stdout"):
                print(f"{path}: {path.read_text(errors='replace')[-2000:]}", file=sys.stderr)
            return 1
    print(json.dumps({"provider": args.provider, "native_managed": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
