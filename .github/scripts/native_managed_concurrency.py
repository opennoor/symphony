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
print("GATE_STARTED", flush=True)
deadline = time.monotonic() + 900
while not (root / "release").exists():
    if time.monotonic() > deadline:
        raise SystemExit("gate timed out")
    time.sleep(.1)
print("GATE_RELEASED", flush=True)
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


def prompt(provider, label, recover, project):
    control = "$symphony:symphony start" if provider == "codex" else "/symphony:start"
    route = '{"size":"small","complexity":"simple","risk":"normal","rationale":"disposable native CI gate","topology":"direct"}'
    recovery = (
        'The lead MUST finish its first turn after the gate with exactly '
        'SYMPHONY_OUTCOME: {"status":"blocked"}. Await that result. '
        'Then use followup_task on the SAME lead identity, asking it to return exactly '
        'SYMPHONY_OUTCOME: {"status":"completed"}; await the follow-up. '
        if recover else 'After the gate returns, return SYMPHONY_OUTCOME: {"status":"completed"}. '
    )
    codex_route = (
        "For this fixture's base profile the selected lead is gpt-6-luna at low effort. "
        "In spawn_agent pass model=gpt-6-luna, reasoning_effort=low, and fork_turns=none "
        "so the lead does not inherit this root's effort. "
        if provider == "codex" else ""
    )
    gate = str(project / "gate.py").replace("\\", "/")
    return (
        f"{control} Disposable native CI lifecycle {label}. "
        "Use Symphony and the native agent tools. Spawn one assessor that returns exactly "
        f"SYMPHONY_ASSESSMENT: {route}. Await it. Spawn one lead with "
        f"SYMPHONY_ROUTE: {route} and the exact model and effort selected by Symphony. "
        + codex_route +
        f"The lead's entire task is to run `python '{gate}' {label}` exactly once. "
        f"That absolute script path writes `{label}.ready` next to itself. Do not create "
        "another checkout or worktree. The gate prints GATE_STARTED "
        "before blocking and GATE_RELEASED only after the harness releases it. "
        "Use the native command tool to run it; report success only after observing "
        "GATE_RELEASED from that command. " + recovery +
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
                   "--output-last-message", str(output), prompt(provider, label, recover, project)]
    else:
        command = [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
                   "--permission-mode", "bypassPermissions", "--session-id", session,
                   "--output-format", "json",
                   prompt(provider, label, recover, project)]
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


def resume_claude(env, project, session, budget, deadline):
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude CLI is missing")
    completed = subprocess.run(
        [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
         "--permission-mode", "bypassPermissions", "--resume", session,
         "--output-format", "json",
         "Continue this exact Symphony session after the background lead result. "
         "Reconcile its outcome and finish. Do not start a new task."],
        cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        shell=False, timeout=max(1, min(90, deadline - time.monotonic())),
    )
    if completed.returncode:
        raise RuntimeError(f"Claude resume for session {session} exited {completed.returncode}")


def package_version(source):
    source = source.resolve()
    manifest = json.loads((source / ".codex-plugin" / "plugin.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError(f"invalid plugin version at {source}")
    recorded = re.search(r'^PLUGIN_VERSION = "([^"]+)"$',
                         (source / "symphony" / "__init__.py").read_text(), re.MULTILINE)
    if not recorded or recorded.group(1) != version:
        raise RuntimeError(f"manifest and runtime versions differ at {source}")
    return version


def marketplace(root, name, source, version):
    destination = root / name
    plugin = destination / "plugins" / "symphony"
    if any(path.is_symlink() for path in source.rglob("*")):
        raise RuntimeError(f"plugin package contains symlinks: {source}")
    shutil.copytree(source, plugin)
    manifest = {"name": name, "owner": {"name": "Symphony CI"},
                "plugins": [{"name": "symphony", "source": "./plugins/symphony",
                             "version": version}]}
    catalog = destination / ".claude-plugin" / "marketplace.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps(manifest), encoding="utf-8")
    return destination


def codex_command(env, cwd, *arguments, input_text=None, timeout=60):
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("codex CLI is missing")
    completed = subprocess.run([executable, *arguments], cwd=cwd, env=env, input=input_text,
                               capture_output=True, text=True, shell=False, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"codex {' '.join(arguments[:2])} exited {completed.returncode}")
    return completed


def claude_command(env, cwd, *arguments, timeout=60):
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude CLI is missing")
    completed = subprocess.run([executable, *arguments], cwd=cwd, env=env,
                               capture_output=True, text=True, shell=False, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"claude {' '.join(arguments[:2])} exited {completed.returncode}")
    return completed


def prepare_live_update(provider, root, old_source, candidate_source):
    old_source, candidate_source = old_source.resolve(), candidate_source.resolve()
    old_version, candidate_version = package_version(old_source), package_version(candidate_source)
    if old_version != "1.5.1" or tuple(map(int, candidate_version.split("."))) <= (1, 5, 1):
        raise RuntimeError("live update requires an actual installed 1.5.1 package and a newer candidate")
    home = root / f"{provider}-live-update-home"
    home.mkdir()
    env = {**os.environ, "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(home)}
    old_market = marketplace(root, "symphony-old", old_source, old_version)
    candidate_market = marketplace(root, "symphony-candidate", candidate_source, candidate_version)
    if provider == "codex":
        token = os.environ.get("OPENAI_API_KEY")
        if token:
            codex_command(env, root, "login", "--with-api-key", input_text=token)
        else:
            auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
            if not auth.is_file():
                raise RuntimeError("live update needs OPENAI_API_KEY or an existing Codex auth file")
            shutil.copyfile(auth, home / "auth.json")
            (home / "auth.json").chmod(0o600)
            codex_command(env, root, "login", "status")
        codex_command(env, root, "plugin", "marketplace", "add", str(old_market))
        codex_command(env, root, "plugin", "add", "symphony@symphony-old")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            source_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
            credentials = source_config / ".credentials.json"
            if not credentials.is_file():
                raise RuntimeError("Claude live update needs ANTHROPIC_API_KEY or existing auth")
            shutil.copyfile(credentials, home / ".credentials.json")
            (home / ".credentials.json").chmod(0o600)
            status = json.loads(claude_command(env, root, "auth", "status", "--json").stdout)
            if not status.get("loggedIn"):
                raise RuntimeError("disposable Claude config is not authenticated")
        claude_command(env, root, "plugin", "marketplace", "add", str(old_market))
        claude_command(env, root, "plugin", "install", "-y", "symphony@symphony-old")
    old_cache = home / "plugins" / "cache" / "symphony-old" / "symphony" / old_version
    hook = "codex.json" if provider == "codex" else "hooks.json"
    if not (old_cache / "hooks" / hook).is_file():
        raise RuntimeError(f"old 1.5.1 package was not installed in the disposable {provider} home")
    return {"provider": provider, "env": env, "home": home, "old_version": old_version,
            "candidate_version": candidate_version, "old_cache": old_cache,
            "old_source": old_market / "plugins" / "symphony",
            "candidate_market": candidate_market}


def update_while_gated(update, env, docs, sessions, projects_by_label, deadline):
    provider = update["provider"]
    old_root = update["old_cache"] if provider == "codex" else update["old_source"]
    records = {}
    for label, session in sessions.items():
        activation = docs[label].get("activation", {}).get(provider, {})
        choices = [activation, *activation.get("session_profiles", [])]
        record = next((item for item in choices if item.get("session_id") == session
                       and item.get("plugin_version") == update["old_version"]), None)
        if not record or Path(record.get("plugin_root", "")).resolve() != old_root.resolve():
            raise RuntimeError(f"{label}: active native session is not using installed 1.5.1")
        retained = Path(record.get("runtime_root", ""))
        if not retained.is_dir() or retained.parent.resolve() != Path(env["SYMPHONY_RUNTIME_DIR"]).resolve():
            raise RuntimeError(f"{label}: trusted old runtime was not retained before update")
        records[label] = record
    def change_plugin(*arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("native live update exceeded its deadline")
        command = codex_command if provider == "codex" else claude_command
        return command(env, update["home"], *arguments, timeout=min(60, remaining))

    if provider == "codex":
        change_plugin("plugin", "remove", "symphony@symphony-old")
        if old_root.exists():
            old_root.rename(update["home"] / "removed-old-cache")
        change_plugin("plugin", "marketplace", "add", str(update["candidate_market"]))
        change_plugin("plugin", "add", "symphony@symphony-candidate")
    else:
        change_plugin("plugin", "uninstall", "symphony@symphony-old")
        old_root.rename(update["home"] / "removed-old-source")
        if update["old_cache"].exists():
            update["old_cache"].rename(update["home"] / "removed-old-cache")
        change_plugin("plugin", "marketplace", "add", str(update["candidate_market"]))
        change_plugin("plugin", "install", "-y", "symphony@symphony-candidate")
    if old_root.exists():
        raise RuntimeError("old installed plugin source still exists after update")
    candidate_cache = (update["home"] / "plugins" / "cache" / "symphony-candidate"
                       / "symphony" / update["candidate_version"])
    if not (candidate_cache / "hooks" / ("codex.json" if provider == "codex" else "hooks.json")).is_file():
        raise RuntimeError("newer candidate was not installed in the disposable home")
    if provider == "codex":
        checker = candidate_cache / "scripts" / "check_activation.py"
        if not checker.is_file():
            raise RuntimeError("newer candidate checker was not installed")
        for label, session in sessions.items():
            remaining = max(1, min(30, deadline - time.monotonic()))
            completed = subprocess.run([sys.executable, "-I", str(checker)], cwd=projects_by_label[label],
                                       env={**env, "CODEX_SESSION_ID": session}, capture_output=True,
                                       text=True, timeout=remaining)
            if completed.returncode or "guarded: matching current-session heartbeat" not in completed.stdout:
                raise RuntimeError(f"{label}: newer installed checker rejected the active old session")
    return records


def check_case(provider, root, separate, timeout, budget, update=None):
    case = root / ("live-update" if update else "worktrees" if separate else "same-worktree")
    case.mkdir()
    first, second = projects(case, separate)
    for project in {first, second}:
        (project / "gate.py").write_text(GATE)
    state_dir = case / "state"
    state_dir.mkdir()
    env = {**os.environ, **(update["env"] if update else {}), "SYMPHONY_STATE_DIR": str(state_dir),
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
            if any(process.poll() not in (None, 0) for process in processes.values()):
                raise RuntimeError("native CLI failed before both managed leads reached the gate")
            if provider == "codex" and any(process.poll() is not None for process in processes.values()):
                raise RuntimeError("native CLI exited before both managed leads reached the gate")
            if all((project / f"{label}.ready").exists()
                   for label, project in (("a", first), ("b", second))):
                if provider == "codex" and not sessions["a"]:
                    sessions = {label: codex_session(logs, label) for label in processes}
                if all(path.exists() for path in paths.values()):
                    docs = {label: json.loads(path.read_text()) for label, path in paths.items()}
                    if all(docs[label].get("active_runs", {}).get(
                            f"{provider}:{session}", {}).get("lead_identity")
                            for label, session in sessions.items()):
                        break
            time.sleep(.25)
        else:
            raise RuntimeError("native lead ownership did not overlap before deadline")
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
        observed_run_ids = {label: docs[label]["active_runs"][f"{provider}:{session}"]["run_id"]
                            for label, session in sessions.items()}
        old_records = (update_while_gated(update, env, docs, sessions,
                                          {"a": first, "b": second}, deadline) if update else {})
        for project in {first, second}:
            (project / "release").touch()
        if provider == "codex":
            for label, process in processes.items():
                remaining = max(1, deadline - time.monotonic())
                if process.wait(timeout=remaining):
                    raise RuntimeError(f"{label}: native CLI exited {process.returncode}")
        else:
            resumed = set()
            while time.monotonic() < deadline:
                if any(process.poll() not in (None, 0) for process in processes.values()):
                    raise RuntimeError("native Claude CLI exited unsuccessfully")
                current = {label: json.loads(path.read_text()) for label, path in paths.items()}
                for label, process in processes.items():
                    events = current[label].get("event_history", [])
                    lead_returned = (any(event.get("kind") == "lead_completed"
                                         and event.get("payload", {}).get("identity") == observed_leads[label]
                                         for event in events)
                                     or any(run.get("session_id") == sessions[label]
                                            and run.get("status") == "completed"
                                            for run in current[label].get("recent_runs", [])))
                    if (process.poll() == 0 and label not in resumed
                            and (lead_returned or deadline - time.monotonic() < 90)):
                        resume_claude(env, first if label == "a" else second,
                                      sessions[label], budget, deadline)
                        resumed.add(label)
                if (len(resumed) == len(processes)
                        and all(any(run.get("session_id") == sessions[label]
                                and run.get("status") == "completed"
                                and (run.get("outcome") or {}).get("status") == "completed"
                                for run in current[label].get("recent_runs", []))
                                for label in processes)):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError("background Claude leads did not resume and archive completed outcomes")
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
            if matching[0].get("run_id") != observed_run_ids[label]:
                raise RuntimeError(f"{label}: active run was restarted during the native session")
            if update:
                activation = doc.get("activation", {}).get(provider, {})
                records = [activation, *activation.get("session_profiles", [])]
                if not any(item.get("session_id") == sessions[label]
                           and item.get("plugin_version") == update["old_version"]
                           and item.get("runtime_root") == old_records[label]["runtime_root"]
                           for item in records):
                    raise RuntimeError(f"{label}: old native session lost its retained runtime after update")
            if label == "a" and provider == "codex":
                events = [event for event in doc.get("event_history", [])
                          if event.get("payload", {}).get("identity") == matching[0].get("lead_identity")]
                kinds = [event.get("kind") for event in events]
                if "lead_failed" not in kinds or "lead_completed" not in kinds:
                    raise RuntimeError("recovered lead lacks durable failed and completed events")
        return {"case": "live-update" if update else
                "different-branch-worktrees" if separate else "same-worktree",
                "observed_overlap": True, "completed": ["a", "b"],
                **({"native_resumed": sorted(resumed)} if provider == "claude" else {}),
                **({"live_update": f"{update['old_version']}->{update['candidate_version']}"} if update else {})}
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


def failure_state(root, provider):
    def run_summary(run):
        return {"session_id": run.get("session_id"), "lead_id": run.get("lead_identity"),
                "status": run.get("status"),
                "outcome": (run.get("outcome") or {}).get("status")}

    cases = []
    for path in root.rglob("*.v2.json"):
        document = json.loads(path.read_text())
        cases.append({"case": path.relative_to(root).parts[0],
                      "active_runs": [run_summary(run) for run in document.get("active_runs", {}).values()],
                      "recent_runs": [run_summary(run) for run in document.get("recent_runs", [])],
                      "event_kinds": [event.get("kind") for event in document.get("event_history", [])]})
    return {"provider": provider, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--claude-budget-usd", type=float, default=3.0)
    parser.add_argument("--live-update", action="store_true")
    parser.add_argument("--old-plugin-root", type=Path)
    parser.add_argument("--candidate-plugin-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "plugins" / "symphony")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="symphony-native-managed-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        update = None
        try:
            if args.live_update:
                if not args.old_plugin_root:
                    raise RuntimeError("--live-update requires --old-plugin-root")
                old_version = package_version(args.old_plugin_root)
                candidate_version = package_version(args.candidate_plugin_root)
                if (old_version != "1.5.1"
                        or tuple(map(int, candidate_version.split("."))) <= (1, 5, 1)):
                    raise RuntimeError("live update requires 1.5.1 and a genuinely newer candidate")
                if args.provider == "codex":
                    auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
                    if not os.environ.get("OPENAI_API_KEY") and not auth.is_file():
                        raise RuntimeError("live update needs OPENAI_API_KEY or existing Codex auth")
                elif not os.environ.get("ANTHROPIC_API_KEY"):
                    source_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                    if not (source_config / ".credentials.json").is_file():
                        raise RuntimeError("Claude live update needs ANTHROPIC_API_KEY or existing auth")
            results = [check_case(args.provider, root, separate, args.timeout,
                                  args.claude_budget_usd) for separate in (False, True)]
            if args.live_update:
                update = prepare_live_update(args.provider, root, args.old_plugin_root,
                                             args.candidate_plugin_root)
                results.append(check_case(args.provider, root, False, args.timeout,
                                          args.claude_budget_usd, update))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(json.dumps({"provider": args.provider, "error": str(error),
                              "logs": str(root)}), file=sys.stderr)
            diagnostics = failure_state(root, args.provider)
            print(json.dumps(diagnostics), file=sys.stderr)
            destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"native-managed-{args.provider}-failure.json").write_text(
                    json.dumps(diagnostics, indent=2), encoding="utf-8")
            # Preserve command output as CI diagnostics before TemporaryDirectory removes it.
            for path in root.rglob("*.errors"):
                print(f"{path}: {path.read_text(errors='replace')[-4000:]}", file=sys.stderr)
            for path in root.rglob("*.output"):
                print(f"{path}: {path.read_text(errors='replace')[-2000:]}", file=sys.stderr)
            for path in root.rglob("*.stdout"):
                print(f"{path}: {path.read_text(errors='replace')[-2000:]}", file=sys.stderr)
            return 1
        finally:
            if update:
                shutil.rmtree(update["home"], ignore_errors=True)
    print(json.dumps({"provider": args.provider, "native_managed": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
