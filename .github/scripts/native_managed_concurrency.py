#!/usr/bin/env python3
"""Exercise concurrent Symphony runs through installed native Codex or Claude CLIs.

Run after the CI job has installed and authenticated the selected provider's
plugin. All Git checkouts, state, runtime pins, and gate files are temporary.
"""

import argparse
from collections import Counter
from dataclasses import replace
from hashlib import sha256
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

CODEX_HOOK_CAPTURE = '''import json, pathlib, sys, time, uuid
destination = pathlib.Path(sys.argv[2])
destination.mkdir(parents=True, exist_ok=True)
invocation = str(uuid.uuid4())
try:
    payload = json.load(sys.stdin)
except (ValueError, OSError):
    payload = {}
if not isinstance(payload, dict):
    payload = {}
event = sys.argv[1]
record = {"invocation_id": invocation, "event": event,
          "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
          "turn_id": payload.get("turn_id"), "parent_id": payload.get("parent_id"),
          "cwd": payload.get("cwd"), "payload_keys": sorted(payload),
          "started_ns": time.time_ns()}
(destination / (invocation + "-entry.json")).write_text(json.dumps(record))
(destination / (invocation + "-exit.json")).write_text(json.dumps({"invocation_id": invocation,
                                                                    "exit_code": 0,
                                                                    "finished_ns": time.time_ns()}))
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
    key = sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()
    return state_dir / (key + ".v2.json")


def prompt(provider, label, recover, project):
    control = "$symphony:symphony start" if provider == "codex" else "/symphony:start"
    route = '{"size":"small","complexity":"simple","risk":"normal","rationale":"disposable native CI gate","topology":"direct"}'
    recovery = (
        'The lead MUST finish its first turn after the gate with exactly '
        'SYMPHONY_OUTCOME: {"status":"blocked"}. Await that result. '
        'Then use followup_task on the SAME lead identity, passing the exact task_name '
        'used in spawn_agent as target (lowercase letters, digits, and underscores; '
        'do not pass a /root/ path or agent UUID), asking it to return exactly '
        'SYMPHONY_OUTCOME: {"status":"completed"}; await the follow-up. '
        if recover else 'After the gate returns, return SYMPHONY_OUTCOME: {"status":"completed"}. '
    )
    codex_route = (
        "For this fixture's base profile the selected lead is gpt-6-luna at low effort. "
        "In spawn_agent pass model=gpt-6-luna, reasoning_effort=low, and fork_turns=none "
        "so the lead does not inherit this root's effort. "
        if provider == "codex" else ""
    )
    claude_isolation = (
        "Use Agent run_in_background=true for the lead, and omit isolation=worktree; "
        "the fixture checkout is already disposable and its shared state must observe this lead. "
        if provider == "claude" else ""
    )
    gate = str(project / "gate.py").replace("\\", "/")
    return (
        f"{control} Disposable native CI lifecycle {label}. "
        "Use Symphony and the native agent tools. Spawn one assessor that returns exactly "
        f"SYMPHONY_ASSESSMENT: {route}. Await it. Spawn one lead with "
        f"SYMPHONY_ROUTE: {route} and the exact model and effort selected by Symphony. "
        + codex_route + claude_isolation +
        f"The lead's entire task is to run `python '{gate}' {label}` exactly once. "
        f"That absolute script path writes `{label}.ready` next to itself. Do not create "
        "another checkout or worktree. The gate prints GATE_STARTED "
        "before blocking and GATE_RELEASED only after the harness releases it. "
        "Use the native command tool to run it; report success only after observing "
        "GATE_RELEASED from that command. " + recovery +
        "Await all children. Once their outcomes are reconciled, check Symphony status; "
        f"if the original run remains completing, invoke the normal "
        f"{'$symphony:symphony stop' if provider == 'codex' else '/symphony:stop'} "
        "control in this same session and check durable status again. Never force stop. "
        "Then finish briefly. Do not edit files or inspect other projects."
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


def resume_codex(env, project, session, logs, deadline, already_completed=False):
    """Resume the original native root so newly installed candidate hooks load."""
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("codex CLI is missing")
    prompt = (
        "Resume this exact native Symphony root session after the original run completed. "
        "Run $symphony:symphony status and $symphony:symphony version to check the installed "
        "candidate. Report only the status and version. Do not start or stop work, delegate, "
        "call followup_task, or rerun the gate."
        if already_completed else
        "Resume the original Symphony run in this session. Reconcile its registered lead "
        "from the native host result; if a retry is needed, call followup_task with the "
        "exact original spawn_agent task_name, without a /root/ prefix or UUID, and await "
        "the same lead. Preserve the original run and gate evidence. When the lead and "
        "all tracked work are reconciled, invoke the normal $symphony:symphony stop "
        "control and check durable status. Do not force stop or rerun the gate.")
    command = [executable, "exec", "resume", "--dangerously-bypass-hook-trust",
               "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
               "--model", "gpt-6-luna", session, prompt]
    status_file = logs / "a.resume.status.json"
    try:
        with (logs / "a.resume.stdout").open("w", encoding="utf-8") as stdout, \
                (logs / "a.resume.errors").open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, shell=False,
                                       timeout=max(1, min(90, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        status_file.write_text(json.dumps({"timed_out": True,
                                           "mode": "status-version" if already_completed else "reconcile"}))
        raise RuntimeError("Codex same-session resume timed out") from None
    banner = re.search(r"^session id: ([0-9a-f-]+)$",
                       (logs / "a.resume.errors").read_text(errors="replace"), re.MULTILINE)
    status_file.write_text(json.dumps({"mode": "status-version" if already_completed else "reconcile",
                                       "exit_code": completed.returncode,
                                       "banner_present": banner is not None,
                                       "same_session": bool(banner and banner.group(1) == session)}))
    if completed.returncode:
        raise RuntimeError(f"Codex resume for session {session} exited {completed.returncode}")
    if not banner or banner.group(1) != session:
        raise RuntimeError("Codex resume changed the original root session ID")


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
        capture = root / "codex-hook-capture"
        capture.mkdir()
        script = root / "capture_codex_hook.py"
        script.write_text(CODEX_HOOK_CAPTURE)
        if any(character.isspace() for character in str(script)):
            raise RuntimeError("native Codex hook capture requires a temporary path without spaces")
        hooks = {"hooks": {}}
        for event in ("SessionStart", "SubagentStart", "SubagentStop", "Stop"):
            command = f"python3 -I {script.as_posix()} {event} {capture.as_posix()}"
            windows = f"python.exe -I {script.as_posix()} {event} {capture.as_posix()}"
            hooks["hooks"][event] = [{"hooks": [{"type": "command", "command": command,
                                                  "command_windows": windows, "timeout": 10}]}]
        (home / "hooks.json").write_text(json.dumps(hooks))
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
        capture = root / "claude-hook-capture"
        capture.mkdir()
        script = root / "capture_claude_hook.py"
        script.write_text(CODEX_HOOK_CAPTURE)
        settings_path = home / "settings.json"
        settings = json.loads(settings_path.read_text())
        hooks = settings.setdefault("hooks", {})
        for event in ("SessionStart", "SubagentStart", "SubagentStop", "Stop"):
            command = (f'python -I "{script.as_posix()}" {event} '
                       f'"{capture.as_posix()}"')
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command}]})
        settings_path.write_text(json.dumps(settings))
    old_cache = home / "plugins" / "cache" / "symphony-old" / "symphony" / old_version
    hook = "codex.json" if provider == "codex" else "hooks.json"
    if not (old_cache / "hooks" / hook).is_file():
        raise RuntimeError(f"old 1.5.1 package was not installed in the disposable {provider} home")
    return {"provider": provider, "env": env, "home": home, "old_version": old_version,
            "candidate_version": candidate_version, "old_cache": old_cache,
            "candidate_cache": home / "plugins" / "cache" / "symphony-candidate" /
                               "symphony" / candidate_version,
            "hook_capture_dir": root / "codex-hook-capture" if provider == "codex" else None,
            "old_source": old_market / "plugins" / "symphony",
            "candidate_source": candidate_market / "plugins" / "symphony",
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


def event_counts(document):
    return dict(sorted(Counter(event.get("kind") for event in
                               document.get("event_history", [])).items()))


def codex_pre_resume_state(document, session, run_id, lead_id):
    """Accept only the original recovering run or its durable completed archive."""
    key = f"codex:{session}"
    active = document.get("active_runs", {})
    owned = active.get(key)
    active_for_session = [run for run in active.values() if run.get("session_id") == session]
    archived = [run for run in document.get("recent_runs", [])
                if run.get("session_id") == session]

    def original(run):
        return (run.get("provider") == "codex"
                and run.get("session_id") == session
                and run.get("run_id") == run_id
                and run.get("lead_identity") == lead_id)
    if (owned and len(active_for_session) == 1 and not archived
            and original(owned) and owned.get("status") == "recovering"):
        return "recovering"
    if (not owned and not active_for_session and len(archived) == 1
            and original(archived[0]) and archived[0].get("status") == "completed"
            and (archived[0].get("outcome") or {}).get("status") == "completed"):
        return "completed"
    raise RuntimeError("old native run is neither the original recovering lead nor a durable completed archive")


def codex_host_trace(home, session, lead_id, error_log):
    """Extract only tool and turn metadata from disposable Codex JSONL."""
    def fingerprint(value):
        return sha256(str(value).encode()).hexdigest()[:12] if value else None

    def records(path):
        if not path:
            return
        for line in path.open(errors="replace"):
            try:
                yield json.loads(line)
            except ValueError:
                continue

    def output_summary(payload):
        value = payload.get("output")
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = value
        empty = value is None or value == "" or value == [] or value == {}
        shape = ("null" if value is None else
                 "empty_string" if value == "" else
                 "json_object" if isinstance(value, str) and isinstance(parsed, dict) else
                 "json_array" if isinstance(value, str) and isinstance(parsed, list) else
                 "text" if isinstance(value, str) else type(value).__name__)
        details = {"payload_keys": sorted(key for key in payload
                                           if re.fullmatch(r"[A-Za-z0-9_]{1,64}", key))[:16],
                   "output_field_present": "output" in payload,
                   "output_shape": shape, "output_empty": empty,
                   "output_json_keys": sorted(key for key in parsed
                                              if re.fullmatch(r"[A-Za-z0-9_]{1,64}", key))[:16]
                   if isinstance(parsed, dict) else []}
        return parsed, details

    def agent_roster(parsed):
        agents = parsed.get("agents") if isinstance(parsed, dict) else None
        if not isinstance(agents, list):
            return None
        roster = []
        for agent in agents[:16]:
            if not isinstance(agent, dict):
                continue
            name = agent.get("agent_name") or agent.get("task_name") or agent.get("name")
            status = agent.get("agent_status") or agent.get("status")
            roster.append({"name": name if isinstance(name, str) and
                           re.fullmatch(r"[A-Za-z0-9_/-]{1,128}", name) else None,
                           "status": status if isinstance(status, str) and
                           re.fullmatch(r"[A-Za-z0-9_ -]{1,64}", status) else None})
        return {"count": len(agents), "entries": roster,
                "truncated": len(agents) > 16}

    session_dir = home / "sessions"
    root_file = next(session_dir.rglob(f"*{session}.jsonl"), None) if session_dir.exists() else None
    child_file = next(session_dir.rglob(f"*{lead_id}.jsonl"), None) if lead_id and session_dir.exists() else None
    calls = {}
    spawns = {}
    roster_call_ids = set()
    outputs = {}
    activities = []
    last_roster = None
    root_turns = {}
    last_call = None
    root_call_history = []
    for record in records(root_file):
        payload = record.get("payload") or {}
        if record.get("type") == "event_msg" and payload.get("type") == "task_started":
            turn_id = payload.get("turn_id")
            if turn_id:
                root_turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})["started"] = True
        elif record.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            if turn_id:
                turn = root_turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})
                turn["model"] = payload.get("model")
                turn["effort"] = payload.get("effort")
        elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
            turn_id = payload.get("turn_id")
            if turn_id:
                root_turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})["completed"] = True
        if record.get("type") == "response_item" and payload.get("type") == "function_call":
            name = payload.get("name")
            call_id = payload.get("call_id")
            wait_timeout_ms = None
            if isinstance(name, str) and "wait" in name.lower():
                try:
                    wait_arguments = json.loads(payload.get("arguments") or "{}")
                except (TypeError, ValueError):
                    wait_arguments = {}
                value = (wait_arguments.get("timeout_ms", wait_arguments.get("timeoutMs"))
                         if isinstance(wait_arguments, dict) else None)
                if isinstance(value, int) and 0 <= value <= 3600000:
                    wait_timeout_ms = value
            root_call_history.append({
                "name": name if isinstance(name, str) and
                re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) else None,
                "call_hash": fingerprint(call_id),
                "called_at": record.get("timestamp"),
                "wait_timeout_ms": wait_timeout_ms,
                "returned": False,
            })
            last_call = {"name": name if isinstance(name, str)
                         and re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) else None,
                         "call_hash": fingerprint(call_id), "call_id": call_id}
            if name == "list_agents" and call_id:
                roster_call_ids.add(call_id)
            if name not in {"followup_task", "spawn_agent"}:
                continue
            try:
                arguments = json.loads(payload.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            call_id = payload.get("call_id")
            if call_id and name == "spawn_agent":
                task_name = arguments.get("task_name")
                spawns[call_id] = {"call_hash": fingerprint(call_id),
                                   "task_name": task_name if isinstance(task_name, str)
                                   and re.fullmatch(r"[a-z0-9_]{1,128}", task_name) else None}
            if call_id and name == "followup_task":
                target = arguments.get("target")
                calls[call_id] = {"call_hash": fingerprint(call_id),
                                  "target": target if isinstance(target, str)
                                  and re.fullmatch(r"[A-Za-z0-9_/-]{1,128}", target) else None}
        elif record.get("type") == "response_item" and payload.get("type") == "function_call_output":
            parsed, shape = output_summary(payload)
            output = payload.get("output")
            output_text = output if isinstance(output, str) else json.dumps(output, default=str)
            error = (payload.get("is_error") is True or payload.get("isError") is True
                     or isinstance(parsed, dict) and (
                         parsed.get("is_error") is True or parsed.get("isError") is True
                         or (isinstance(parsed.get("status"), str) and
                             parsed["status"] in {"failed", "error", "rejected"})
                         or bool(parsed.get("error")))
                     or '"isError":true' in output_text or '"is_error":true' in output_text)
            kind = ("invalid_name" if "agent_name must use only lowercase" in output_text else
                    "unknown_agent" if "agent" in output_text.lower() and any(
                        term in output_text.lower() for term in ("not found", "does not exist", "unknown")) else
                    "tool_error" if error else
                    "empty_output" if shape["output_empty"] else "returned")
            call_id = payload.get("call_id")
            for call in reversed(root_call_history):
                if call_id and call["call_hash"] == fingerprint(call_id):
                    call["returned"] = True
                    call["returned_at"] = record.get("timestamp")
                    break
            outputs[call_id] = {"kind": kind,
                                "hash": fingerprint(output_text) if not shape["output_empty"] else None,
                                "error_detected": bool(error), "shape": shape}
            if call_id in roster_call_ids:
                # Keep only the last parseable roster; never retain tool prose.
                roster = agent_roster(parsed)
                if roster is not None:
                    last_roster = roster
        elif record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item") or {}
            if item.get("type") == "SubAgentActivity" and item.get("agent_thread_id") == lead_id:
                activities.append({"call_hash": fingerprint(item.get("id")),
                                   "kind": item.get("kind"),
                                   "agent_thread_id": item.get("agent_thread_id")})
    spawned_names = {item["task_name"] for item in spawns.values() if item.get("task_name")}
    lead_spawn_names = {item["task_name"] for item in spawns.values()
                        if item.get("task_name") and any(
                            activity["call_hash"] == item["call_hash"]
                            and activity["agent_thread_id"] == lead_id for activity in activities)}
    followups = [{**details, "target_matches_spawn_name": details["target"] in spawned_names,
                  "target_matches_lead_spawn_name": details["target"] in lead_spawn_names
                  if lead_spawn_names else None,
                  "tool_return_recorded": call_id in outputs,
                  "tool_return_kind": (outputs.get(call_id) or {}).get("kind"),
                  "tool_return_hash": (outputs.get(call_id) or {}).get("hash"),
                  "tool_return_error_detected": (outputs.get(call_id) or {}).get("error_detected"),
                  "tool_return_shape": (outputs.get(call_id) or {}).get("shape"),
                  "activity_kinds": [item["kind"] for item in activities
                                     if item["call_hash"] == details["call_hash"]],
                  "same_lead_id": any(item["call_hash"] == details["call_hash"]
                                      and item["agent_thread_id"] == lead_id
                                      for item in activities)}
                 for call_id, details in calls.items()]
    spawn_calls = [{**details, "tool_return_kind": (outputs.get(call_id) or {}).get("kind"),
                    "tool_return_shape": (outputs.get(call_id) or {}).get("shape"),
                    "same_lead_id": any(item["call_hash"] == details["call_hash"]
                                        and item["agent_thread_id"] == lead_id
                                        for item in activities)}
                   for call_id, details in spawns.items()]
    if last_call:
        last_call = {"name": last_call["name"], "call_hash": last_call["call_hash"],
                     "return_recorded": last_call["call_id"] in outputs}
    turns = {}
    child_parent = None
    for record in records(child_file):
        payload = record.get("payload") or {}
        if record.get("type") == "session_meta":
            spawn = ((payload.get("source") or {}).get("subagent") or {}).get("thread_spawn") or {}
            child_parent = spawn.get("parent_thread_id")
        elif record.get("type") == "event_msg" and payload.get("type") == "task_started":
            turn_id = payload.get("turn_id")
            if turn_id:
                turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})["started"] = True
        elif record.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            if turn_id:
                turn = turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})
                turn["model"] = payload.get("model")
                turn["effort"] = payload.get("effort")
        elif record.get("type") == "event_msg" and payload.get("type") == "task_complete":
            turn_id = payload.get("turn_id")
            if turn_id:
                turn = turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})
                turn["completed"] = True
                message = str(payload.get("last_agent_message") or "")
                markers = re.findall(r"^SYMPHONY_OUTCOME:\s*(\{[^\n]*\})", message, re.MULTILINE)
                if markers:
                    try:
                        status = json.loads(markers[-1]).get("status")
                    except (ValueError, AttributeError):
                        status = None
                    turn["reported_outcome"] = status if status in {
                        "completed", "blocked", "failed", "abandoned"} else "other"
    log = error_log.read_text(errors="replace") if error_log.is_file() else ""
    return {"root_jsonl_present": root_file is not None,
            "lead_jsonl_present": child_file is not None,
            "lead_parent_matches_root": child_parent == session if child_file else None,
            "followup_calls": followups,
            "spawn_calls": spawn_calls,
            "last_list_agents_roster": last_roster,
            "root_turns_tail": list(root_turns.values())[-3:],
            "root_tool_calls_tail": root_call_history[-12:],
            "last_root_function_call": last_call,
            "lead_activity": activities,
            "lead_turns": list(turns.values()),
            "root_stop_hook": {
                "invocation_count": len(re.findall(r"(?m)^hook: Stop(?: Started)?\r?$", log)),
                "completed_count": len(re.findall(r"hook: Stop Completed", log)),
                "failed_count": len(re.findall(r"hook: Stop Failed", log))},
            "subagent_stop_hook": {
                "invocation_count": len(re.findall(r"(?m)^hook: SubagentStop(?: Started)?\r?$", log)),
                "completed_count": len(re.findall(r"hook: SubagentStop Completed", log)),
                "failed_count": len(re.findall(r"hook: SubagentStop Failed", log))}}


def check_case(provider, root, separate, timeout, budget, update=None):
    case = root / ("live-update" if update else "worktrees" if separate else "same-worktree")
    case.mkdir()
    first, second = projects(case, separate)
    for project in {first, second}:
        (project / "gate.py").write_text(GATE)
    state_dir = case / "state"
    state_dir.mkdir()
    candidate_version = (update["candidate_version"] if update else
                         package_version(Path(__file__).resolve().parents[2] / "plugins/symphony"))
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
            exited = {label: code for label, process in processes.items()
                      if (code := process.poll()) is not None
                      and (provider == "codex" or code != 0)}
            if exited:
                exits = ",".join(f"{label}={code}" for label, code in sorted(exited.items()))
                raise RuntimeError(f"native CLI exited before both managed leads reached the gate: {exits}")
            if all((project / f"{label}.ready").exists()
                   for label, project in (("a", first), ("b", second))):
                if provider == "codex" and not sessions["a"]:
                    sessions = {label: codex_session(logs, label) for label in processes}
                if all(path.exists() for path in paths.values()):
                    docs = {label: json.loads(path.read_text()) for label, path in paths.items()}
                    expected_gate_version = update["old_version"] if update else candidate_version
                    for label, session in sessions.items():
                        activation = docs[label].get("activation", {}).get(provider, {})
                        profiles = [activation, *activation.get("session_profiles", [])]
                        if not any(item.get("session_id") == session
                                   and item.get("plugin_version") == expected_gate_version
                                   for item in profiles):
                            raise RuntimeError(
                                f"{label}: native lead used another Symphony plugin version at gate")
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
        old_records = {}
        if update:
            snapshot_file = case / "update_event_counts.json"
            snapshot = {"pre_update": {label: event_counts(docs[label]) for label in sessions}}
            snapshot_file.write_text(json.dumps(snapshot))
            old_records = update_while_gated(update, env, docs, sessions,
                                             {"a": first, "b": second}, deadline)
            snapshot["post_update"] = {label: event_counts(json.loads(paths[label].read_text()))
                                       for label in sessions}
            snapshot_file.write_text(json.dumps(snapshot))
        for project in {first, second}:
            (project / "release").touch()
        if provider == "codex":
            for label, process in processes.items():
                remaining = max(1, deadline - time.monotonic())
                if process.wait(timeout=remaining):
                    raise RuntimeError(f"{label}: native CLI exited {process.returncode}")
            if update:
                before_resume = json.loads(paths["a"].read_text())
                pre_resume_state = codex_pre_resume_state(
                    before_resume, sessions["a"], observed_run_ids["a"], observed_leads["a"])
                pre_resume_run_ids = {run.get("run_id") for run in [
                    *before_resume.get("active_runs", {}).values(),
                    *before_resume.get("recent_runs", [])]}
                pre_resume_lead_ids = {item.get("identity") for run in [
                    *before_resume.get("active_runs", {}).values(),
                    *before_resume.get("recent_runs", [])]
                    for item in (run.get("delegations") or []) if item.get("role") == "lead"}
                old_activation = before_resume.get("activation", {}).get("codex", {})
                old_profiles = [old_activation, *old_activation.get("session_profiles", [])]
                if not any(item.get("session_id") == sessions["a"]
                           and item.get("plugin_version") == update["old_version"]
                           and item.get("runtime_root") == old_records["a"]["runtime_root"]
                           for item in old_profiles):
                    raise RuntimeError("old retained runtime was lost before native candidate resume")
                captured_stops = [record for record in codex_hook_capture_summary(root)["records"]
                                  if record.get("event") == "SubagentStop"
                                  and record.get("session_id") == sessions["a"]
                                  and record.get("agent_id") == observed_leads["a"]
                                  and record.get("exit_marker_written")]
                if len({record.get("turn_id") for record in captured_stops}) < 2:
                    raise RuntimeError("native host did not deliver a repeated same-lead terminal")
                host = codex_host_trace(update["home"], sessions["a"], observed_leads["a"],
                                        logs / "a.errors")
                if not any(turn.get("reported_outcome") == "completed"
                           for turn in host["lead_turns"][1:]):
                    raise RuntimeError("native lead transcript lacks a completed follow-up turn")
                snapshot["pre_candidate_resume"] = event_counts(before_resume)
                snapshot["pre_candidate_resume_run_state"] = pre_resume_state
                snapshot_file.write_text(json.dumps(snapshot))
                resume_codex(env, first, sessions["a"], logs, deadline,
                             already_completed=pre_resume_state == "completed")
                starts = [record for record in codex_hook_capture_summary(root)["records"]
                          if record.get("event") == "SessionStart"
                          and record.get("session_id") == sessions["a"]
                          and record.get("exit_marker_written")]
                if len(starts) < 2:
                    raise RuntimeError("candidate resume lacked a second native SessionStart")
                checker = update["candidate_cache"] / "scripts" / "check_activation.py"
                checked = subprocess.run([sys.executable, "-I", str(checker)], cwd=first,
                                         env={**env, "CODEX_SESSION_ID": sessions["a"]},
                                         capture_output=True, text=True,
                                         timeout=max(1, min(30, deadline - time.monotonic())))
                if checked.returncode or "guarded: matching current-session heartbeat" not in checked.stdout:
                    raise RuntimeError("candidate checker rejected the resumed native root session")
                snapshot["post_candidate_resume"] = event_counts(json.loads(paths["a"].read_text()))
                snapshot_file.write_text(json.dumps(snapshot))
        else:
            resumed = set()
            while time.monotonic() < deadline:
                if any(process.poll() not in (None, 0) for process in processes.values()):
                    raise RuntimeError("native Claude CLI exited unsuccessfully")
                current = {label: json.loads(path.read_text()) for label, path in paths.items()}
                for label, process in processes.items():
                    events = current[label].get("event_history", [])
                    old_lead_completed = any(event.get("kind") == "lead_completed"
                                             and event.get("payload", {}).get("identity") == observed_leads[label]
                                             for event in events)
                    lead_returned = (old_lead_completed
                                     or any(run.get("session_id") == sessions[label]
                                            and run.get("status") == "completed"
                                            for run in current[label].get("recent_runs", [])))
                    native_lead_stopped = (captured_claude_lead_stop(root, sessions[label],
                                                                     observed_leads[label])
                                           if update else False)
                    if (process.poll() == 0 and label not in resumed
                            and (native_lead_stopped if update else
                                 lead_returned or deadline - time.monotonic() < 90)):
                        if update:
                            activation = current[label].get("activation", {}).get(provider, {})
                            records = [activation, *activation.get("session_profiles", [])]
                            if not any(item.get("session_id") == sessions[label]
                                       and item.get("plugin_version") == update["old_version"]
                                       and item.get("runtime_root") == old_records[label]["runtime_root"]
                                       for item in records):
                                raise RuntimeError(f"{label}: old retained activation was lost before resume")
                            snapshot.setdefault("pre_candidate_resume", {})[label] = {
                                "old_product_lead_completed": old_lead_completed,
                                "native_lead_stop_captured": True,
                                "events": event_counts(current[label]),
                            }
                            snapshot_file.write_text(json.dumps(snapshot))
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
        if provider == "codex" and update:
            final_a = final_docs["a"]
            final_run_ids = {run.get("run_id") for run in [
                *final_a.get("active_runs", {}).values(), *final_a.get("recent_runs", [])]}
            if final_run_ids != pre_resume_run_ids:
                raise RuntimeError("candidate resume changed the durable run set")
            final_lead_ids = {item.get("identity") for run in [
                *final_a.get("active_runs", {}).values(), *final_a.get("recent_runs", [])]
                for item in (run.get("delegations") or []) if item.get("role") == "lead"}
            if final_lead_ids != pre_resume_lead_ids:
                raise RuntimeError("candidate resume changed the lead set")
            if any(run.get("session_id") == sessions["a"]
                   for run in final_a.get("active_runs", {}).values()):
                raise RuntimeError("candidate resume left the original run active")
        # This case owns its disposable state directory exclusively, including
        # child-session aliases whose owner pointer was never populated.
        for record_path in state_dir.glob(".session-*.json"):
            record = json.loads(record_path.read_text())
            if record.get("pending") or record.get("overflow"):
                raise RuntimeError("native session retained unresolved child callbacks after completion")
        for label, doc in final_docs.items():
            runs = [run for run in doc.get("recent_runs", []) if run.get("provider") == provider]
            matching = [run for run in runs if run.get("session_id") == sessions[label]]
            if len(matching) != 1 or matching[0].get("status") != "completed":
                raise RuntimeError(f"{label}: missing durable completed run")
            completed_run = matching[0]
            if (completed_run.get("outcome") or {}).get("status") != "completed":
                raise RuntimeError(f"{label}: missing durable completed outcome")
            if completed_run.get("lead_identity") != observed_leads[label]:
                raise RuntimeError(f"{label}: completed run changed lead identity")
            if completed_run.get("run_id") != observed_run_ids[label]:
                raise RuntimeError(f"{label}: active run was restarted during the native session")
            if not update:
                activation = doc.get("activation", {}).get(provider, {})
                profiles = [activation, *activation.get("session_profiles", [])]
                if not any(item.get("session_id") == sessions[label]
                           and item.get("plugin_version") == candidate_version
                           for item in profiles):
                    raise RuntimeError(f"{label}: completed under another Symphony plugin version")
            if update:
                activation = doc.get("activation", {}).get(provider, {})
                records = [activation, *activation.get("session_profiles", [])]
                if not Path(old_records[label]["runtime_root"]).is_dir():
                    raise RuntimeError(f"{label}: old retained runtime disappeared after update")
                if provider == "codex":
                    expected = update["candidate_version"] if label == "a" else update["old_version"]
                    matching = [item for item in records if item.get("session_id") == sessions[label]
                                and item.get("plugin_version") == expected]
                    if label == "a":
                        matching = [item for item in matching
                                    if item.get("plugin_root") and
                                    Path(item["plugin_root"]).resolve() == update["candidate_cache"].resolve()]
                    else:
                        matching = [item for item in matching
                                    if item.get("runtime_root") == old_records[label]["runtime_root"]]
                    if not matching:
                        raise RuntimeError(f"{label}: native session lacks expected {expected} heartbeat")
                elif not any(item.get("session_id") == sessions[label]
                             and item.get("plugin_version") == update["candidate_version"]
                             and Path(item.get("plugin_root", "")).resolve()
                             == update["candidate_source"].resolve()
                             for item in records):
                    raise RuntimeError(f"{label}: same-session Claude resume did not load the candidate")
            if label == "a" and provider == "codex":
                events = [event for event in doc.get("event_history", [])
                          if event.get("payload", {}).get("identity") == completed_run.get("lead_identity")]
                kinds = [event.get("kind") for event in events]
                if "lead_failed" not in kinds or "lead_completed" not in kinds:
                    raise RuntimeError("recovered lead lacks durable failed and completed events")
        return {"case": "live-update" if update else
                "different-branch-worktrees" if separate else "same-worktree",
                "observed_overlap": True, "completed": ["a", "b"],
                "candidate_version": candidate_version, "pending_callbacks": 0,
                **({"native_resumed": sorted(resumed)} if provider == "claude" else
                   {"native_resumed": ["a"]} if update and provider == "codex" else {}),
                **({"native_resume_mode": "status-version" if pre_resume_state == "completed"
                    else "reconcile"} if update and provider == "codex" else {}),
                **({"native_hook_capture": codex_hook_capture_summary(root, provider)}
                   if update else {}),
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


def codex_hook_capture_summary(root, provider="codex"):
    """Read independent user hooks without collecting native transcripts or payload bodies."""
    directory = root / f"{provider}-hook-capture"
    if not directory.is_dir():
        return {"configured": False, "records": []}
    records = []
    for path in directory.glob("*-entry.json"):
        try:
            entry = json.loads(path.read_text())
            exit_path = path.with_name(path.name.replace("-entry.json", "-exit.json"))
            marker = json.loads(exit_path.read_text()) if exit_path.is_file() else {}
        except (OSError, ValueError):
            continue
        if entry.get("event") not in {"SessionStart", "SubagentStart", "SubagentStop", "Stop"}:
            continue
        records.append({key: entry.get(key) for key in
                        ("invocation_id", "event", "session_id", "agent_id", "turn_id",
                         "parent_id", "cwd", "started_ns", "payload_keys")}
                       | {"exit_marker_written": marker.get("invocation_id") == entry.get("invocation_id"),
                          "script_exit_marker_code": marker.get("exit_code")})
    records.sort(key=lambda record: record.get("started_ns") or 0)
    terminals = Counter()
    for record in records:
        if record["event"] == "SubagentStop":
            identity = (record.get("session_id"), record.get("agent_id"))
            terminals[identity] += 1
            record["terminal_index_for_agent"] = terminals[identity]
    return {"configured": True, "records": records,
            "event_counts": dict(Counter(record["event"] for record in records))}


def captured_claude_lead_stop(root, session_id, lead_id):
    """Require an independent completed host callback for this original lead."""
    return any(record.get("event") == "SubagentStop"
               and record.get("session_id") == session_id
               and record.get("agent_id") == lead_id
               and record.get("exit_marker_written")
               and record.get("script_exit_marker_code") == 0
               for record in codex_hook_capture_summary(root, "claude")["records"])


_CLAUDE_RECOVERY_REJECT = {
    78: "run_scope", 82: "lead_identity", 88: "lead_route_or_state",
    92: "native_home", 95: "child_transcript_path", 100: "child_path_ancestry",
    105: "child_meta_read", 108: "child_meta_parse", 110: "child_meta_shape",
    117: "child_meta_launch", 121: "native_transcript_read", 132: "parent_launch_count",
    138: "parent_launch_shape", 141: "parent_cwd", 143: "parent_cwd_resolve",
    146: "parent_launch_time", 153: "child_identity", 157: "child_prompt_missing",
    164: "target_prompt", 169: "prompt_uuid", 172: "prompt_chronology",
    179: "latest_activity", 185: "latest_terminal", 190: "terminal_chronology",
    194: "model_effort", 197: "terminal_content", 202: "final_marker_conflict",
    212: "handback_count", 224: "handback_receipt", 234: "later_worker_failure",
    244: "later_worker_failure_event", 246: "later_lead_restart",
    249: "report_length",
}


def claude_recovery_probe(document, session, project, home):
    """Classify the candidate's exact rejection stage without retaining transcript text."""
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    try:
        from symphony import host_evidence
        from symphony.store import _state_from_dict
        state = _state_from_dict(document)
        run = state.active_runs.get(f"claude:{session}")
        if run is None:
            return {"result": "no_active_run"}
        state = replace(state, active_run=run)
        captured = {}

        def trace(frame, event, value):
            if frame.f_code is not host_evidence._claude_native_lead_event.__code__:
                return None
            if event == "return":
                captured["line"] = frame.f_lineno
                captured["accepted"] = value is not None
                values = frame.f_locals
                if frame.f_lineno == 95:
                    captured["child_path_count"] = len(values.get("paths", ()))
                if frame.f_lineno == 194:
                    lead = values.get("lead")
                    assistants = values.get("assistants", ())
                    if lead is not None:
                        captured["requested_model"] = (
                            lead.requested_tier if re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,80}", lead.requested_tier) else "other")
                        captured["requested_effort"] = (
                            lead.requested_effort if re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,40}", lead.requested_effort) else "other")
                    captured["assistant_models"] = sorted({
                        item for row in assistants
                        if (item := str((row.get("message") or {}).get("model")))
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item)})[:8]
                    captured["assistant_efforts"] = sorted({
                        item for row in assistants
                        if (item := str(row.get("perTurnEffort") or row.get("effort")))
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", item)})[:8]
                if frame.f_lineno in {179, 185}:
                    activity = values.get("activity", ())
                    assistants = values.get("assistants", ())
                    captured["latest_activity_is_assistant"] = bool(
                        activity and assistants and activity[-1] is assistants[-1])
                    terminal = values.get("terminal")
                    if isinstance(terminal, dict):
                        reason = (terminal.get("message") or {}).get("stop_reason")
                        captured["latest_stop_reason"] = (
                            reason if reason in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}
                            else "other")
                if frame.f_lineno in {202, 212, 224}:
                    status = values.get("final_status")
                    captured["final_marker_status"] = (
                        status if status in {"completed", "blocked", "failed", "abandoned"}
                        else "missing_or_invalid")
                    captured["handback_count"] = len(values.get("handbacks", ()))
                    captured["handback_result_count"] = len(values.get("results", ()))
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            event = host_evidence._claude_native_lead_event(
                state, session, project, {"CLAUDE_CONFIG_DIR": str(home)},
                require_missing=True)
        finally:
            sys.settrace(previous)
        line = captured.pop("line", None)
        accepted = captured.pop("accepted", False)
        return {"result": "accepted" if accepted and event is not None else "rejected",
                "stage": "accepted" if accepted else _CLAUDE_RECOVERY_REJECT.get(line, "unknown"),
                "source_line": line, **captured}
    except Exception as error:
        return {"result": "probe_error", "error_type": type(error).__name__}


def failure_state(root, provider):
    def short_hash(value):
        return sha256(str(value).encode()).hexdigest()[:12] if value else None

    def outcome_marker(payload):
        message = payload.get("last_assistant_message")
        if not isinstance(message, str):
            return None
        markers = re.findall(r"^SYMPHONY_OUTCOME:\s*(\{[^\n]*\})", message, re.MULTILINE)
        if len(markers) != 1:
            return None
        try:
            status = json.loads(markers[0]).get("status")
        except (ValueError, AttributeError):
            return "invalid"
        return status if status in {"completed", "blocked", "failed", "abandoned", "released"} else "other"

    def observed_role(payload):
        roles = {"assessor", "consultant", "lead", "worker"}
        for value in payload.values():
            if isinstance(value, str):
                for line in value.splitlines():
                    line = line.strip()
                    if line.startswith("SYMPHONY_ROLE:"):
                        role = line.removeprefix("SYMPHONY_ROLE:").strip()
                        if role in roles:
                            return role
        role = str(payload.get("role") or "").strip().lower()
        if role in roles:
            return role
        for key in ("agent_type", "task_name"):
            value = payload.get(key)
            if isinstance(value, str):
                if value.strip().lower() in roles:
                    return value.strip().lower()
                match = re.search(r"(?:^|[_:/-])symphony[_-](assessor|consultant|lead|worker)(?:[_:/-]|$)",
                                  value.lower())
                if match:
                    return match.group(1)
        return ""

    def terminal_result_prefix(payload):
        fields = ("provider", "session_id", "agent_id", "subagent_id", "turn_id",
                  "prompt_id", "status", "last_assistant_message", "agent_transcript_path",
                  "agent_type", "task_name", "role", "model", "model_reasoning_effort")
        stable = {key: payload[key] for key in fields if key in payload}
        stable["observed_role"] = observed_role(payload)
        return sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def run_summary(run):
        assessment = run.get("assessment") or {}
        terminal_turns = assessment.get("_terminal_turns") or {}
        return {"session_id": run.get("session_id"), "lead_id": run.get("lead_identity"),
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "outcome": (run.get("outcome") or {}).get("status"),
                "terminal_result_prefixes": [str(item).split(":")[-1][:12]
                                             for item in assessment.get("_terminal_event_ids", ())],
                "terminal_turn_hashes": {identity: [short_hash(token) for token in tokens]
                                         for identity, tokens in terminal_turns.items()},
                "lead_delegations": [{"identity": item.get("identity"), "state": item.get("state"),
                                      "requested_tier": item.get("requested_tier"),
                                      "requested_effort": item.get("requested_effort")}
                                     for item in run.get("delegations", [])
                                     if item.get("role") == "lead"]}

    def event_summary(event, root_sessions):
        payload = event.get("payload") or {}
        invocation = next((payload[key] for key in ("invocation_id", "tool_use_id", "call_id", "turn_id")
                           if payload.get(key)), None)
        source_id = str(event.get("event_id") or "").split(":", 1)[0]
        return {"kind": event.get("kind"), "identity": payload.get("identity"),
                "event_id_hash": short_hash(event.get("event_id")),
                "session_id": payload.get("session_id"),
                "parent_matches_root": payload.get("parent_thread_id") in root_sessions
                if payload.get("parent_thread_id") else None,
                "turn_id_hash": short_hash(payload.get("turn_id")),
                "prompt_id_hash": short_hash(payload.get("prompt_id")),
                "role": payload.get("role"), "delegation_state": payload.get("state"),
                "outcome": (payload.get("outcome") or {}).get("status"),
                "owner_generation": payload.get("owner_generation"),
                "native_invocation_id_present": invocation is not None,
                "native_invocation_id_hash": short_hash(invocation),
                "source_event_hash": short_hash(source_id)}

    def session_record_summary(record, root_sessions):
        owner = record.get("owner_session")
        pending = []
        for item in record.get("pending", ()):
            payload = item.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            token = next((f"{key}:{payload[key]}" for key in ("turn_id", "prompt_id")
                          if payload.get(key)), None)
            event_id = str(item.get("event_id") or "")
            parent = payload.get("parent_thread_id")
            pending.append({
                "kind": item.get("kind"), "event_id_hash": short_hash(event_id),
                "event_id_prefix": event_id[:12] if re.fullmatch(r"[0-9a-f]{64}", event_id) else None,
                "terminal_result_prefix": terminal_result_prefix(payload)
                if item.get("kind") == "subagent_stopped" else None,
                "session_id": payload.get("session_id"),
                "agent_id": payload.get("agent_id") or payload.get("subagent_id"),
                "parent_id": parent if parent in root_sessions else None,
                "parent_matches_record_owner": parent == owner if parent else None,
                "turn_id_present": bool(payload.get("turn_id")),
                "turn_id_hash": short_hash(payload.get("turn_id")),
                "prompt_id_present": bool(payload.get("prompt_id")),
                "prompt_id_hash": short_hash(payload.get("prompt_id")),
                "terminal_turn_hash": short_hash(token),
                "outcome_marker_status": outcome_marker(payload),
                "ambiguous_owner": bool(item.get("ambiguous_owner")),
                "generation": item.get("generation"),
            })
        return {"session_id": record.get("session"), "owner_session_id": owner,
                "owner_matches_root": owner in root_sessions, "migrated": record.get("migrated"),
                "overflow": bool(record.get("overflow")), "pending_count": len(pending),
                "pending": pending}

    def activation_summary(record):
        plugin_root = record.get("plugin_root")
        runtime_root = record.get("runtime_root")
        try:
            relative_plugin = str(Path(plugin_root).relative_to(root)) if plugin_root else None
        except ValueError:
            relative_plugin = None
        return {"session_id": record.get("session_id"),
                "plugin_version": record.get("plugin_version"),
                "guarded_state": record.get("state"),
                "plugin_root_basename": Path(plugin_root).name if plugin_root else None,
                "plugin_root_relative_to_scratch": relative_plugin,
                "plugin_root_present": Path(plugin_root).exists() if plugin_root else False,
                "runtime_root_present": Path(runtime_root).is_dir() if runtime_root else False,
                "runtime_digest": Path(runtime_root).name if runtime_root else None,
                "observed_at_present": bool(record.get("observed_at"))}

    cases = []
    for path in root.rglob("*.v2.json"):
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        case = path.relative_to(root).parts[0]
        case_root = root / case
        labels = [label for label, project in (("a", case_root / "primary"),
                                                ("b", case_root / "second" if
                                                 (case_root / "second").exists() else
                                                 case_root / "primary"))
                  if state_file(case_root / "state", project) == path]
        cli_sessions = {}
        for label in labels:
            log = case_root / "logs" / f"{label}.{'errors' if provider == 'codex' else 'stdout'}"
            if log.is_file():
                pattern = (r"^session id: ([0-9a-f-]+)$" if provider == "codex" else
                           r'"session_id"\s*:\s*"([0-9a-f-]+)"')
                match = re.search(pattern, log.read_text(errors="replace"), re.MULTILINE)
                if match:
                    cli_sessions[label] = match.group(1)
        activation = document.get("activation", {}).get(provider, {})
        profiles = ([activation, *activation.get("session_profiles", [])]
                    if isinstance(activation, dict) else [])
        snapshot_file = case_root / "update_event_counts.json"
        snapshots = json.loads(snapshot_file.read_text()) if snapshot_file.is_file() else None
        root_sessions = set(cli_sessions.values())
        relevant_events = [event_summary(event, root_sessions)
                           for event in document.get("event_history", [])
                           if event.get("kind") in {"delegation_updated", "lead_started",
                                                    "lead_failed", "lead_completed"}]
        session_records = []
        for record_path in (case_root / "state").glob(".session-*.json"):
            try:
                record = json.loads(record_path.read_text())
            except (OSError, ValueError):
                continue
            if record.get("state_name") in {None, path.name}:
                session_records.append(session_record_summary(record, root_sessions))
        recovery_probes = {}
        if provider == "claude" and case == "live-update":
            native_home = root / "claude-live-update-home"
            for label, session in cli_sessions.items():
                if f"claude:{session}" in document.get("active_runs", {}):
                    project = case_root / ("second" if label == "b" and
                                           (case_root / "second").exists() else "primary")
                    recovery_probes[label] = claude_recovery_probe(
                        document, session, project, native_home)
        cases.append({"case": case, "labels": labels, "cli_session_ids": cli_sessions,
                      "activation_profiles": [activation_summary(item) for item in profiles
                                              if isinstance(item, dict) and item.get("session_id")],
                      "active_runs": [run_summary(run) for run in document.get("active_runs", {}).values()],
                      "recent_runs": [run_summary(run) for run in document.get("recent_runs", [])],
                      "event_kinds": [event.get("kind") for event in document.get("event_history", [])],
                      "lead_lifecycle_events": relevant_events,
                      "session_records": session_records,
                      "candidate_recovery_probes": recovery_probes,
                      "event_counts": event_counts(document),
                      "update_event_counts": snapshots})
    host = {}
    if provider == "codex":
        for case in cases:
            case_root = root / case["case"]
            home = (root / "codex-live-update-home" if case["case"] == "live-update" else
                    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
            runs = [*case["active_runs"], *case["recent_runs"]]
            for label, session in case["cli_session_ids"].items():
                lead = next((run.get("lead_id") for run in runs
                             if run.get("session_id") == session), None)
                host[f"{case['case']}:{label}"] = codex_host_trace(
                    home, session, lead, case_root / "logs" / f"{label}.errors")
    resume_status_file = root / "live-update" / "logs" / "a.resume.status.json"
    resume_status = json.loads(resume_status_file.read_text()) if resume_status_file.is_file() else None
    return {"provider": provider, "cases": cases, "native_host_trace": host,
            "native_resume_status": resume_status,
            "native_hook_capture": codex_hook_capture_summary(root, provider)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--claude-budget-usd", type=float, default=3.0)
    parser.add_argument("--live-update", action="store_true")
    parser.add_argument("--only-live-update", action="store_true",
                        help="run the bounded native upgrade case without baseline cases")
    parser.add_argument("--old-plugin-root", type=Path)
    parser.add_argument("--candidate-plugin-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "plugins" / "symphony")
    args = parser.parse_args()
    if args.only_live_update and not args.live_update:
        parser.error("--only-live-update requires --live-update")
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
            results = ([] if args.only_live_update else
                       [check_case(args.provider, root, separate, args.timeout,
                                   args.claude_budget_usd) for separate in (False, True)])
            if args.live_update:
                update = prepare_live_update(args.provider, root, args.old_plugin_root,
                                             args.candidate_plugin_root)
                results.append(check_case(args.provider, root, False, args.timeout,
                                          args.claude_budget_usd, update))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(json.dumps({"provider": args.provider, "error": str(error),
                              "logs": str(root)}), file=sys.stderr)
            diagnostics = failure_state(root, args.provider)
            diagnostics["failure"] = {"type": type(error).__name__, "message": str(error)[:300]}
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
