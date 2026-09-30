#!/usr/bin/env python3
"""Exercise concurrent Symphony runs through installed native Codex or Claude CLIs.

Run after the CI job has installed and authenticated the selected provider's
plugin. All Git checkouts, state, runtime pins, and gate files are temporary.
"""

import argparse
import ast
from collections import Counter
from dataclasses import replace
from datetime import datetime
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


OBSERVER_SNAPSHOT_LOCK_RETRIES = 0


# The isolated-worktree harness still uses a shell gate to prove child cwd.
# Managed concurrency below uses the native SubagentStart hook gate instead.
GATE = '''import pathlib, sys, time
root = pathlib.Path(__file__).resolve().parent
name = sys.argv[1]
with (root / (name + ".ready")).open("x"):
    pass
print("GATE_STARTED", flush=True)
deadline = time.monotonic() + 900
while not (root / "release").exists():
    if time.monotonic() > deadline:
        raise SystemExit("gate timed out")
    time.sleep(.1)
(root / (name + ".released")).touch()
print("GATE_RELEASED", flush=True)
'''


CODEX_HOOK_CAPTURE = '''import hashlib, json, os, pathlib, re, sys, time, uuid
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
provider = sys.argv[3]
record = {"invocation_id": invocation, "event": event,
          "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
          "turn_id": payload.get("turn_id"), "parent_id": payload.get("parent_id"),
          "prompt_id_hash": hashlib.sha256(str(payload.get("prompt_id") or "").encode()).hexdigest()[:12]
              if payload.get("prompt_id") else None,
          "message_hash": hashlib.sha256(str(payload.get("last_assistant_message") or "").encode()).hexdigest()[:12]
              if payload.get("last_assistant_message") else None,
          "native_home": os.environ.get("CODEX_HOME" if provider == "codex"
                                        else "CLAUDE_CONFIG_DIR"),
          "cwd": payload.get("cwd"), "payload_keys": sorted(payload),
          "started_ns": time.time_ns()}
gate_dir = pathlib.Path(os.environ["SYMPHONY_NATIVE_GATE_DIR"]) if os.environ.get(
    "SYMPHONY_NATIVE_GATE_DIR") else None
env_label = os.environ.get("SYMPHONY_NATIVE_GATE_LABEL")
gate_label = None
if gate_dir and event == "SessionStart" and env_label in {"a", "b"}:
    gate_dir.mkdir(parents=True, exist_ok=True)
    root_file = gate_dir / (env_label + ".root.json")
    root_session = payload.get("session_id")
    if not isinstance(root_session, str) or not root_session:
        raise SystemExit("native root SessionStart lacked a session ID")
    if root_file.exists():
        if json.loads(root_file.read_text()).get("session_id") != root_session:
            raise SystemExit("native gate root session changed")
    else:
        temporary = gate_dir / (env_label + "." + invocation + ".tmp")
        temporary.write_text(json.dumps({"session_id": root_session}))
        temporary.replace(root_file)
if gate_dir and event == "SubagentStart":
    owners = [label for label in ("a", "b")
              if (gate_dir / (label + ".root.json")).is_file()
              and json.loads((gate_dir / (label + ".root.json")).read_text()).get(
                  "session_id") == payload.get("session_id")]
    if len(owners) > 1:
        raise SystemExit("native child matches multiple root gate sessions")
    gate_label = owners[0] if owners else None
agent_type = payload.get("agent_type")
is_lead_start = (event == "SubagentStart" and provider == "claude"
                 and isinstance(agent_type, str) and re.fullmatch(
                     r"symphony:symphony-lead-[a-z0-9-]{1,96}", agent_type) is not None)
if (gate_dir and gate_label in {"a", "b"} and event == "SubagentStart"
        and provider == "codex" and agent_type == "default"):
    # Codex 0.159 reports both assessor and lead as agent_type=default.
    # This fixture awaits one assessor before launching one lead, so hold
    # the second distinct native child and verify its durable lead ID below.
    gate_dir.mkdir(parents=True, exist_ok=True)
    first = gate_dir / (gate_label + ".first.json")
    try:
        with first.open("x") as stream:
            json.dump({"session_id": payload.get("session_id"),
                       "agent_id": payload.get("agent_id")}, stream)
    except FileExistsError:
        original = json.loads(first.read_text())
        if original.get("session_id") != payload.get("session_id"):
            raise SystemExit("native gate root session changed")
        is_lead_start = original.get("agent_id") != payload.get("agent_id")
if gate_dir and gate_label in {"a", "b"} and is_lead_start:
    record["native_gate_label"] = gate_label
    record["native_gate_env_label"] = env_label if env_label in {"a", "b"} else None
    record["native_gate_session_id"] = payload.get("session_id")
    record["agent_type"] = payload["agent_type"]
    gate_dir.mkdir(parents=True, exist_ok=True)
    ready = gate_dir / (gate_label + ".ready")
    temporary = gate_dir / (gate_label + "." + invocation + ".tmp")
    temporary.write_text(json.dumps({
        "session_id": payload.get("session_id"), "agent_id": payload.get("agent_id"),
        "started_ns": record["started_ns"]}))
    temporary.replace(ready)
if event == "PreToolUse":
    tool = str(payload.get("tool_name") or "")
    record["tool_name"] = tool if tool in {"Agent", "Task"} else "other"
    record["tool_use_id_hash"] = (hashlib.sha256(str(payload["tool_use_id"]).encode()).hexdigest()[:12]
                                  if payload.get("tool_use_id") else None)
    details = payload.get("tool_input")
    if record["tool_name"] in {"Agent", "Task"} and isinstance(details, dict):
        agent_type = str(details.get("subagent_type") or "")
        record["agent_type"] = (agent_type if re.fullmatch(
            r"symphony:symphony-[a-z0-9-]{1,96}", agent_type) else None)
        record["isolation"] = details.get("isolation") in {"worktree"}
        record["background"] = details.get("run_in_background") is True
if event == "UserPromptSubmit":
    prompt = str(payload.get("prompt") or "").strip()
    record["control"] = ("stop" if prompt == "$symphony:symphony stop" else
                         "status" if prompt == "$symphony:symphony status" else
                         "other")
if event == "Stop":
    background = payload.get("background_tasks")
    record["background_tasks_type"] = type(background).__name__
    record["background_tasks_count"] = (len(background) if isinstance(
        background, (dict, list)) else None)
    values = background.values() if isinstance(background, dict) else (
        background if isinstance(background, list) else ())
    record["background_task_states"] = sorted({
        state for item in values if isinstance(item, dict)
        if isinstance((state := item.get("status")), str)
        and state in {"pending", "running", "completed", "failed", "cancelled", "canceled"}})
    if (provider == "codex" and gate_dir and env_label == "a"
            and os.environ.get("SYMPHONY_NATIVE_HOLD_OLD_STOP") == "1"
            and (gate_dir / "a.root.json").is_file()
            and json.loads((gate_dir / "a.root.json").read_text()).get("session_id")
            == payload.get("session_id")):
        ready = gate_dir / "a.stop-ready.json"
        try:
            with (gate_dir / "a.stop-claim").open("x"):
                pass
            temporary = gate_dir / ("a.stop-" + invocation + ".tmp")
            temporary.write_text(json.dumps({"session_id": payload.get("session_id"),
                                             "invocation_id": invocation,
                                             "started_ns": record["started_ns"]}))
            temporary.replace(ready)
            record["native_stop_hold_label"] = "a"
        except FileExistsError:
            pass
(destination / (invocation + "-entry.json")).write_text(json.dumps(record))
if record.get("native_stop_hold_label"):
    deadline = time.monotonic() + 600
    while not (gate_dir / "a.stop-release").is_file():
        if time.monotonic() >= deadline:
            raise SystemExit("native old root Stop hold timed out")
        time.sleep(.1)
if record.get("native_gate_label"):
    deadline = time.monotonic() + 600
    while not (gate_dir / "release").is_file():
        if time.monotonic() >= deadline:
            raise SystemExit("native lead start gate timed out")
        time.sleep(.1)
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


def codex_fixture_roles():
    profiles = json.loads((Path(__file__).resolve().parents[2] / "plugins" / "symphony" /
                           "profiles.json").read_text(encoding="utf-8"))
    base = next(item for item in profiles["providers"]["codex"]["profiles"]
                if item["id"] == "base")
    lead = base["matrix"]["small/simple"]
    assessor = {"model": base["tiers"]["strongest"], "effort": "high"}
    for role in (assessor, lead):
        if role["effort"] not in base["efforts"][role["model"]]:
            raise RuntimeError("native fixture route is unsupported by the packaged base profile")
    def with_name(role_name, route):
        model_slug = re.sub(r"[^a-z0-9]+", "_", route["model"]).strip("_")
        return {**route, "task_name": f"symphony_{role_name}_{model_slug}_{route['effort']}"}
    return with_name("assessor", assessor), with_name("lead", lead)


def prompt(provider, label, recover, project, *, defer_recovery=False):
    assessor_role, lead_role = codex_fixture_roles() if provider == "codex" else ({}, {})
    control = "$symphony:symphony start" if provider == "codex" else "/symphony:start"
    route = '{"size":"small","complexity":"simple","risk":"normal","rationale":"disposable native CI gate","topology":"direct"}'
    recovery = (
        'The lead MUST finish its first turn after its native start hook is released, '
        'followed by the exact line '
        'SYMPHONY_OUTCOME: {"status":"blocked"}. Await that result. '
        'Then use followup_task on the SAME lead identity, passing the exact task_name '
        'used in spawn_agent as target (lowercase letters, digits, and underscores; '
        'do not pass a /root/ path or agent UUID), asking it to report the gate '
        'release from its existing native start without starting another task, '
        'followed by the exact line '
        'SYMPHONY_OUTCOME: {"status":"completed"}; await the follow-up. '
        if recover else 'After the native start hook releases, return SYMPHONY_OUTCOME: {"status":"completed"}. '
    )
    if provider == "codex":
        if recover and defer_recovery:
            recovery = ('Await the lead\'s deliberately blocked first turn. Do not call '
                        'followup_task or spawn another lead in this old runtime. End this '
                        'root turn with the original run recovering; the harness will '
                        'resume THIS SAME root session and lead under the candidate. ')
        elif recover:
            recovery = ('Await the lead\'s deliberately blocked first turn. Use followup_task '
                        'with its original task_name as target on the SAME lead: report the '
                        'existing native start release, then SYMPHONY_OUTCOME: '
                        '{"status":"completed"}. Await that follow-up. ')
        else:
            recovery = 'Await the lead\'s completed native start report. '
    claude_isolation = (
        "Use Agent run_in_background=true for the lead, and omit isolation=worktree; "
        "the fixture checkout is already disposable and its shared state must observe this lead. "
        if provider == "claude" else ""
    )
    lead_task = (
        "FIRST-TURN CONTRACT: There is no gate command or file to find or run. "
        "After your native start hook releases, your only action is to reply with "
        "exactly these two literal lines (no Markdown):\nGATE_RELEASED\n"
        + ('SYMPHONY_OUTCOME: {"status":"blocked"}' if recover else
           'SYMPHONY_OUTCOME: {"status":"completed"}') + "\n"
        + ("This recovery fixture is deliberately blocked even after the native hook releases; "
           "the root will resume this same lead for completion.\n" if recover else "") +
        "The test-only native SubagentStart hook may briefly hold your first turn. "
        "GATE_RELEASED is a report line, not an operation. "
        "Do not inspect files, run tools, delegate, or create a worktree."
    )
    lead_relay = (
        "Pass LEAD_SPAWN_PACKET verbatim as spawn_agent arguments, including message. "
        "The child has no root context; do not expand, paraphrase, or omit its contract. "
        "Never replace the lead or reinterpret GATE_RELEASED as a command.\n"
        "LEAD_SPAWN_PACKET: " + json.dumps({
            "task_name": lead_role["task_name"],
            "model": lead_role["model"],
            "reasoning_effort": lead_role["effort"],
            "fork_turns": "none",
            "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {route}\n" + lead_task,
        }) + "\n"
        if provider == "codex" else
        "Pass LEAD_TASK_TEXT verbatim as the Agent prompt; do not summarize it. "
        "LEAD_TASK_TEXT: " + json.dumps(
            f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {route}\n" + lead_task) + "\n"
    )
    assessor_relay = ("Pass ASSESSOR_SPAWN_PACKET verbatim to spawn_agent before the lead packet. "
                      "Both task_name values use only lowercase letters, digits, and underscores. "
                      "ASSESSOR_SPAWN_PACKET: " + json.dumps({
                          "task_name": assessor_role["task_name"],
                          "model": assessor_role["model"],
                          "reasoning_effort": assessor_role["effort"],
                          "fork_turns": "none",
                          "message": f"SYMPHONY_ROLE: assessor\nReturn exactly SYMPHONY_ASSESSMENT: {route}",
                      }) + "\n" if provider == "codex" else "")
    codex_spawn_note = (
        "Do not call followup_task until spawn_agent has returned success for the exact lead task_name. "
        "If a spawn name is rejected, retry the exact underscore name from its packet. "
        if provider == "codex" else "")
    return (
        f"{control} Disposable native callback report {label}. "
        "Use Symphony and the native agent tools. Spawn one assessor that returns exactly "
        f"SYMPHONY_ASSESSMENT: {route}. Await it. Spawn one lead with "
        f"SYMPHONY_ROUTE: {route} and the exact model and effort selected by Symphony. "
        + claude_isolation + assessor_relay + lead_relay + recovery + codex_spawn_note +
        "Await all children, then finish briefly so the native Stop hook can finalize "
        "the run. A completing run awaits that hook: do not poll for completed before "
        "ending the turn, print status/stop controls as prose, or ask a completed lead "
        "for extra confirmation. Only the external harness releases the test-only native "
        "start hook; neither root nor child controls that gate. "
        "The harness verifies durable completion after Stop. "
        "Never force stop. Do not edit files or inspect other projects."
    )


def launch(provider, project, label, recover, env, log_dir, budget, session,
           *, defer_recovery=False):
    executable = shutil.which(provider)
    if not executable:
        raise RuntimeError(f"{provider} CLI is missing")
    output = log_dir / f"{label}.output"
    errors = log_dir / f"{label}.errors"
    if provider == "codex":
        _, lead_role = codex_fixture_roles()
        command = [executable, "exec", "--dangerously-bypass-hook-trust",
                   "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                   "--model", lead_role["model"], "-C", str(project),
                   "--output-last-message", str(output),
                   prompt(provider, label, recover, project,
                          defer_recovery=defer_recovery)]
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


def resume_claude(env, project, session, budget, deadline, logs, label, *, finalize=False):
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError("claude CLI is missing")
    receipt = {"session_id": session, "started_ns": time.time_ns(),
               "phase": "finalize" if finalize else "reconcile",
               "status": "started"}
    receipt_path = logs / f"{label}.resume.phase.json"
    receipt_path.write_text(json.dumps(receipt))
    try:
        completed = subprocess.run(
            [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
             "--permission-mode", "bypassPermissions", "--resume", session,
             "--output-format", "json",
             ("The original lead has reconciled and this same Symphony run is completing. "
              "Invoke the normal /symphony:stop control now, then check durable status. "
              "Do not start another task or agent." if finalize else
              "Continue this exact Symphony session after the background lead result. "
              "Follow the injected Symphony recovery guidance, reconcile the original "
              "agent's outcome, and finish. Do not start a new task.")],
            cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            shell=False, timeout=max(1, min(90, deadline - time.monotonic())),
        )
    except subprocess.TimeoutExpired:
        receipt.update(status="timed_out", finished_ns=time.time_ns())
        receipt_path.write_text(json.dumps(receipt))
        raise
    receipt.update(status="returned", finished_ns=time.time_ns(),
                   returncode=completed.returncode)
    receipt_path.write_text(json.dumps(receipt))
    if completed.returncode:
        raise RuntimeError(f"Claude resume for session {session} exited {completed.returncode}")


def claude_finalization_ready(document, session, run_id, lead_id, state_dir):
    """Wake the same root only after its original lead and inbox reconcile."""
    owned = document.get("active_runs", {}).get(f"claude:{session}")
    if not owned or owned.get("status") != "completing":
        return False
    if (owned.get("run_id") != run_id or owned.get("lead_identity") != lead_id
            or (owned.get("outcome") or {}).get("status") != "completed"
            or any(item.get("role") == "lead" and item.get("identity") != lead_id
                   for item in owned.get("delegations", []))):
        raise RuntimeError("candidate changed the original Claude run")
    if any(item.get("state") in {"working", "pending", "running", "active"}
           for item in owned.get("delegations", [])):
        return False
    return not any((record := read_state_snapshot(path)).get("pending") or record.get("overflow")
                   for path in state_dir.glob(".session-*.json"))


def codex_resume_phase(env, session, logs):
    """Capture bounded state and native tool metadata while the root is still live."""
    phase = {"root_process_alive_at_90s": True}
    if os.name == "nt":
        # Python's normal read handles can interfere with an atomic replace
        # by a concurrent Windows hook. The final post-process artifact still
        # captures the full state without adding a diagnostic writer race.
        phase["state_sample"] = "omitted_live_windows"
        return phase
    try:
        matches = []
        for path in Path(env["SYMPHONY_STATE_DIR"]).glob("*.v2.json"):
            document = json.loads(path.read_text())
            run = document.get("active_runs", {}).get(f"codex:{session}")
            if run:
                matches.append(run)
        phase["active_owner_count"] = len(matches)
        if len(matches) == 1:
            run = matches[0]
            status = run.get("status")
            outcome = (run.get("outcome") or {}).get("status")
            phase["run_status"] = status if status in {
                "active", "completing", "recovering", "interrupted", "stopping"} else "unknown"
            phase["run_outcome"] = outcome if outcome in {
                "completed", "done", "success", "succeeded", "blocked", "failed"} else "missing_or_invalid"
            phase["lead_identity_present"] = bool(run.get("lead_identity"))
            phase["pending_callback_count"] = sum(
                len(item.get("pending", [])) for item in (
                    json.loads(path.read_text()) for path in
                    Path(env["SYMPHONY_STATE_DIR"]).glob(".session-*.json"))
                if item.get("session") == session)
            if run.get("lead_identity"):
                host = codex_host_trace(Path(env["CODEX_HOME"]), session,
                                        run["lead_identity"], logs / "a.resume.errors")
                phase["last_root_function_call"] = host["last_root_function_call"]
                phase["root_tool_calls_tail"] = host["root_tool_calls_tail"][-3:]
                phase["guidance_mentions"] = host["guidance_observed"][-3:]
                phase["manual_control_attempts"] = host["manual_controls"][-3:]
    except (OSError, ValueError, TypeError, KeyError) as error:
        phase["snapshot_error_type"] = type(error).__name__
    return phase


def resume_codex(env, project, session, logs, deadline, already_completed=False,
                 direct_stop=False, *, lead_id, lead_task_name):
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
        "$symphony:symphony stop"
        if direct_stop else
        f"Resume the original Symphony run in this session. Its original lead identity "
        f"is {lead_id!r}, with spawn task_name {lead_task_name!r}. Preserve that identity "
        "and the original run. Let the candidate SessionStart reconcile its completed "
        "native host result first. If the injected status says this run is completing "
        "with its original lead done, make no agent calls: finish this root turn so "
        "native Stop archives it. Only if the run is still recovering because gate "
        "markers or exit code are missing from its report, use followup_task "
        f"with target {lead_task_name!r}, without a /root/ prefix or UUID, and await "
        "that SAME lead. Ask it to report only evidence from its existing command "
        "result with its outcome; never invent missing evidence. Do not spawn a replacement "
        "or another assessor, even if the old lead is absent from list_agents. If the "
        "host cannot resume that lead or its evidence is unavailable, report the "
        "limitation and end the turn without changing ownership. Do not rerun the gate "
        "or force stop. Once reconciled, finish the root turn so the native Stop hook "
        "can finalize the original run; do not print stop/status controls as prose "
        "or poll a completing run before ending the turn.")
    command = [executable, "exec", "resume", "--dangerously-bypass-hook-trust",
               "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
               "--model", codex_fixture_roles()[1]["model"], session, prompt]
    status_file = logs / "a.resume.status.json"
    started = time.monotonic()
    budget = max(1, deadline - started - 15)
    timed_out = False
    process = None
    try:
        with (logs / "a.resume.stdout").open("w", encoding="utf-8") as stdout, \
                (logs / "a.resume.errors").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, shell=False)
            try:
                process.wait(timeout=min(90, budget))
            except subprocess.TimeoutExpired:
                if budget > 90:
                    (logs / "a.resume.phase90.json").write_text(json.dumps(
                        codex_resume_phase(env, session, logs)))
                try:
                    process.wait(timeout=max(0.01, budget - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    if timed_out:
        status_file.write_text(json.dumps({"timed_out": True,
                                           "allotted_seconds": round(budget, 2),
                                           "elapsed_seconds": round(time.monotonic() - started, 2),
                                           "mode": "status-version" if already_completed else
                                                   "direct-stop" if direct_stop else "reconcile"}))
        raise RuntimeError("Codex same-session resume timed out") from None
    banner = re.search(r"^session id: ([0-9a-f-]+)$",
                       (logs / "a.resume.errors").read_text(errors="replace"), re.MULTILINE)
    status_file.write_text(json.dumps({"mode": "status-version" if already_completed else
                                              "direct-stop" if direct_stop else "reconcile",
                                       "allotted_seconds": round(budget, 2),
                                       "elapsed_seconds": round(time.monotonic() - started, 2),
                                       "exit_code": process.returncode,
                                       "banner_present": banner is not None,
                                       "same_session": bool(banner and banner.group(1) == session)}))
    if process.returncode:
        raise RuntimeError(f"Codex resume for session {session} exited {process.returncode}")
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


def install_hook_capture(provider, root, home):
    """Add an independent native callback clock to a disposable CLI home."""
    capture = root / f"{provider}-hook-capture"
    capture.mkdir(exist_ok=True)
    script = root / f"capture_{provider}_hook.py"
    script.write_text(CODEX_HOOK_CAPTURE)
    if provider == "codex":
        if any(character.isspace() for character in str(script)):
            raise RuntimeError("native Codex hook capture requires a temporary path without spaces")
        hooks = {"hooks": {}}
        for event in ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop"):
            command = f"python3 -I {script.as_posix()} {event} {capture.as_posix()} codex"
            windows = f"python.exe -I {script.as_posix()} {event} {capture.as_posix()} codex"
            hooks["hooks"][event] = [{"hooks": [{"type": "command", "command": command,
                                                  "command_windows": windows,
                                                  "timeout": 600 if event in {"SubagentStart", "Stop"}
                                                  else 10}]}]
        (home / "hooks.json").write_text(json.dumps(hooks))
    else:
        settings_path = home / "settings.json"
        settings = json.loads(settings_path.read_text())
        hooks = settings.setdefault("hooks", {})
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse",
                      "SubagentStart", "SubagentStop", "Stop"):
            command = (f'python -I "{script.as_posix()}" {event} '
                       f'"{capture.as_posix()}" claude')
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command,
                                                             "timeout": 600 if event == "SubagentStart"
                                                             else 10}]})
        settings_path.write_text(json.dumps(settings))


def prepare_baseline_capture(provider, root, candidate_source):
    """Install the candidate and capture hooks in a fresh native CLI home."""
    candidate_source = candidate_source.resolve()
    version = package_version(candidate_source)
    home = root / f"{provider}-baseline-home"
    home.mkdir()
    env = {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(home)}
    if provider == "claude":
        # Claude --print otherwise may begin a turn before a newly installed
        # plugin has finished its background load.
        env["CLAUDE_CODE_SYNC_PLUGIN_INSTALL"] = "1"
    native_env = {**os.environ, **env}
    market = marketplace(root, "symphony-baseline", candidate_source, version)
    if provider == "codex":
        token = os.environ.get("OPENAI_API_KEY")
        if token:
            codex_command(native_env, root, "login", "--with-api-key", input_text=token)
        else:
            auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
            if not auth.is_file():
                raise RuntimeError("Codex baseline capture needs OPENAI_API_KEY or existing auth")
            shutil.copyfile(auth, home / "auth.json")
            (home / "auth.json").chmod(0o600)
            codex_command(native_env, root, "login", "status")
        codex_command(native_env, root, "plugin", "marketplace", "add", str(market))
        codex_command(native_env, root, "plugin", "add", "symphony@symphony-baseline")
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            source_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
            credentials = source_config / ".credentials.json"
            if not credentials.is_file():
                raise RuntimeError("Claude baseline capture needs ANTHROPIC_API_KEY or existing auth")
            shutil.copyfile(credentials, home / ".credentials.json")
            (home / ".credentials.json").chmod(0o600)
            status = json.loads(claude_command(native_env, root, "auth", "status", "--json").stdout)
            if not status.get("loggedIn"):
                raise RuntimeError("disposable Claude baseline config is not authenticated")
        claude_command(native_env, root, "plugin", "marketplace", "add", str(market))
        claude_command(native_env, root, "plugin", "install", "-y", "symphony@symphony-baseline")
    install_hook_capture(provider, root, home)
    return env


def prepare_live_update(provider, root, old_source, candidate_source):
    old_source, candidate_source = old_source.resolve(), candidate_source.resolve()
    old_version, candidate_version = package_version(old_source), package_version(candidate_source)
    if old_version != "1.5.1" or tuple(map(int, candidate_version.split("."))) <= (1, 5, 1):
        raise RuntimeError("live update requires an actual installed 1.5.1 package and a newer candidate")
    home = root / f"{provider}-live-update-home"
    home.mkdir()
    env = {**os.environ, "CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(home)}
    if provider == "claude":
        env["CLAUDE_CODE_SYNC_PLUGIN_INSTALL"] = "1"
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
    install_hook_capture(provider, root, home)
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
        update["old_source_removed_ns"] = time.time_ns()
        change_plugin("plugin", "marketplace", "add", str(update["candidate_market"]))
        change_plugin("plugin", "add", "symphony@symphony-candidate")
    else:
        change_plugin("plugin", "uninstall", "symphony@symphony-old")
        old_root.rename(update["home"] / "removed-old-source")
        if update["old_cache"].exists():
            update["old_cache"].rename(update["home"] / "removed-old-cache")
        update["old_source_removed_ns"] = time.time_ns()
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


def retained_profile(records, session, version, retained_root, source_root):
    """Accept a source profile or its exact retained-runtime callback.

    Released 1.5.1 writes runtime_root while the source cache is present. After
    removal, a callback launched from the reviewed snapshot can instead write
    that same snapshot as plugin_root without a runtime_root field.
    """
    retained = Path(retained_root).resolve()
    source = Path(source_root).resolve()
    if not retained.is_dir():
        return False
    for record in records:
        if record.get("session_id") != session or record.get("plugin_version") != version:
            continue
        runtime = record.get("runtime_root")
        plugin = record.get("plugin_root")
        if (runtime and plugin and Path(runtime).resolve() == retained
                and Path(plugin).resolve() == source):
            return True
        if not runtime and plugin and Path(plugin).resolve() == retained:
            return True
    return False


def literal_runtime_constants(root):
    tree = ast.parse((root / "symphony" / "__init__.py").read_text(encoding="utf-8"))
    return {target.id: ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and target.id in {"PLUGIN_VERSION", "HOOK_SCHEMA_VERSION"}}


def reviewed_snapshot_spec(source):
    """Recompute the file set and digest embedded by generate_hooks.bootstrap."""
    paths = [*source.glob("symphony/*.py"), *source.glob("agents/*.md"),
             *source.glob("commands/*.md"), *source.glob("skills/**/*.md"),
             source / "profiles.json", source / "model-policy.json",
             source / "scripts/symphony_hook.py", source / "scripts/check_activation.py"]
    expected = sorted(path.relative_to(source).as_posix() for path in paths)
    aggregate = sha256()
    for relative in expected:
        aggregate.update(relative.encode() + b"\0" + sha256((source / relative).read_bytes()).digest())
    return expected, aggregate.hexdigest(), literal_runtime_constants(source)


def verified_candidate_retained(root, runtime_base, expected, digest, version, schema):
    """Verify candidate bytes without executing a retained snapshot."""
    try:
        if (not root.is_absolute() or root.is_symlink() or
                root.parent.resolve() != runtime_base.resolve() or root.name != digest):
            return False
        entries = list(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            return False
        files = sorted(path.relative_to(root).as_posix() for path in entries if path.is_file())
        if files != expected:
            return False
        aggregate = sha256()
        for relative in expected:
            aggregate.update(relative.encode() + b"\0" + sha256((root / relative).read_bytes()).digest())
        if aggregate.hexdigest() != digest:
            return False
        constants = literal_runtime_constants(root)
        return (constants.get("PLUGIN_VERSION") == version and
                constants.get("HOOK_SCHEMA_VERSION") == schema)
    except (OSError, ValueError, SyntaxError, TypeError):
        return False


def candidate_retained_profile(records, session, version, source, runtime_base):
    files, digest, constants = reviewed_snapshot_spec(source)
    retained = runtime_base / digest
    schema = constants.get("HOOK_SCHEMA_VERSION")
    if (constants.get("PLUGIN_VERSION") != version or
            not isinstance(schema, int) or
            not verified_candidate_retained(retained, runtime_base, files, digest, version, schema)):
        return False
    return any(item.get("session_id") == session
               and item.get("plugin_version") == version
               and item.get("hook_schema_version") == schema
               and item.get("observed_at")
               and (retained_profile((item,), session, version, retained, source)
                    or (not item.get("runtime_root") and item.get("plugin_root")
                        and Path(item["plugin_root"]).resolve() == source.resolve()))
               for item in records)


def observer_locked_read(reader, timeout=5):
    """Let the test observer yield to native Windows state writers."""
    global OBSERVER_SNAPSHOT_LOCK_RETRIES
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        try:
            return reader()
        except TimeoutError:
            attempts += 1
            OBSERVER_SNAPSHOT_LOCK_RETRIES += 1
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"native state observer remained locked after {attempts} short reads") from None
            time.sleep(.02)


def read_state_snapshot(path):
    if os.name == "nt":
        # The reducer replaces state atomically; Windows readers must share its
        # project lock instead of briefly holding an incompatible plain handle.
        repository = str(Path(__file__).resolve().parents[2])
        if repository not in sys.path:
            sys.path.insert(0, repository)
        from plugins.symphony.symphony.store import _locked, _read_owner_snapshot
        if path.name.startswith(".session-") and path.suffix == ".json":
            # Session ACKs use the stem lock, unlike project state writes.
            def session_read():
                with _locked(path.with_suffix(""), timeout=.1):
                    return path.read_text(encoding="utf-8")
            return json.loads(observer_locked_read(session_read))
        return json.loads(observer_locked_read(lambda: _read_owner_snapshot(path)))
    return json.loads(path.read_text())


def wait_for_completed_docs(paths, provider, sessions, run_ids, deadline,
                            read_document=read_state_snapshot):
    """Allow an already-delivered native Stop hook to commit after CLI exit."""
    limit = min(deadline, time.monotonic() + 10)
    latest = None
    state_dir = next(iter(paths.values())).parent
    while True:
        try:
            latest = {label: read_document(path) for label, path in paths.items()}
            inboxes = [read_document(path) for path in state_dir.glob(".session-*.json")]
        except (OSError, TimeoutError, ValueError):
            latest = None
            inboxes = []
        if latest and all(any(run.get("provider") == provider
                              and run.get("session_id") == sessions[label]
                              and run.get("run_id") == run_ids[label]
                              and run.get("status") == "completed"
                              and (run.get("outcome") or {}).get("status") == "completed"
                              for run in latest[label].get("recent_runs", []))
                          for label in paths) and not any(
                              record.get("pending") or record.get("overflow") for record in inboxes):
            return latest
        if time.monotonic() >= limit:
            if latest is None:
                raise RuntimeError("native final state remained unreadable after Stop")
            if any(record.get("pending") or record.get("overflow") for record in inboxes):
                raise RuntimeError("native callbacks remained unacknowledged after Stop")
            return latest
        time.sleep(.1)


def event_counts(document):
    return dict(sorted(Counter(event.get("kind") for event in
                               document.get("event_history", [])).items()))


def require_recovered_lead_events(document, lead_id):
    """Transport metadata cannot substitute for ordered durable child outcomes."""
    kinds = [event.get("kind") for event in document.get("event_history", [])
             if event.get("payload", {}).get("identity") == lead_id]
    if ("lead_failed" not in kinds or "lead_completed" not in kinds
            or kinds.index("lead_failed") >= kinds.index("lead_completed")):
        raise RuntimeError("recovered lead lacks ordered durable failed and completed events")


def require_case_recovery_events(document, lead_id, provider, update, label,
                                 pre_resume_state=None):
    if provider == "codex" and update and label == "a" and pre_resume_state == "recovering":
        require_recovered_lead_events(document, lead_id)
    elif provider == "codex" and update and label == "a" and pre_resume_state != "completed":
        raise RuntimeError("Codex update lacks an original recovering or completed run")


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


def require_codex_old_lead_terminal(pre_resume_state, captured_stops, lead_turns):
    """A completed original run needs one turn; recovery needs a later same-ID turn."""
    completed = [turn for turn in lead_turns if turn.get("completed")
                 and turn.get("reported_outcome") == "completed"]
    if pre_resume_state == "completed":
        if not completed:
            raise RuntimeError("archived original lead lacks a native completed turn")
        return "original_completed"
    if pre_resume_state != "recovering":
        raise RuntimeError("old original lead has no supported resume state")
    terminal_hashes = {sha256(str(record["turn_id"]).encode()).hexdigest()[:12]
                       for record in captured_stops if record.get("turn_id")}
    if len(terminal_hashes) < 2:
        raise RuntimeError("native host did not deliver a repeated same-lead terminal")
    if (len(lead_turns) < 2 or lead_turns[0].get("reported_outcome") != "blocked"
            or not all(turn.get("completed") and turn.get("turn_hash") in terminal_hashes
                       for turn in lead_turns)):
        raise RuntimeError("native original lead lacks a completed blocked-to-followup lineage")
    latest = lead_turns[-1].get("reported_outcome")
    if latest == "completed":
        return "same_id_recovered"
    if latest in {"blocked", "other", "missing"}:
        # Old 1.5.1 may leave the same lead blocked or deliver a markerless
        # follow-up. Only candidate's later exact outcome may finish it.
        return "same_id_unreconciled"
    raise RuntimeError("native original lead follow-up has no recoverable outcome")


def require_codex_unreconciled_recovery(previous_turns, resumed_turns, terminal_hashes):
    """An unreconciled latest old turn needs a newer hooked completed turn."""
    previous = previous_turns[-1]
    latest = resumed_turns[-1]
    if (len(resumed_turns) <= len(previous_turns)
            or latest.get("turn_hash") == previous.get("turn_hash")
            or not latest.get("completed")
            or latest.get("reported_outcome") != "completed"
            or latest.get("turn_hash") not in terminal_hashes
            or timestamp_ns(latest.get("started_at"))
            <= timestamp_ns(previous.get("completed_at"))
            or timestamp_ns(latest.get("completed_at"))
            < timestamp_ns(latest.get("started_at"))):
        raise RuntimeError("candidate did not complete a new original-lead turn")


def verified_old_codex_wait(document, session, run_id, lead_id, capture, host):
    """Prove an old CLI is only waiting after its original lead's completed turn."""
    if codex_pre_resume_state(document, session, run_id, lead_id) != "recovering":
        raise RuntimeError("old Codex root is not recovering its original lead")
    run = document["active_runs"][f"codex:{session}"]
    if any(item.get("state") != "completed"
           for item in run.get("delegations", []) if item.get("identity") != lead_id):
        raise RuntimeError("old Codex root still owns another active child")
    stops = [record for record in capture.get("records", [])
             if record.get("event") == "SubagentStop"
             and record.get("session_id") == session
             and record.get("agent_id") == lead_id
             and record.get("exit_marker_written")
             and record.get("script_exit_marker_code") == 0]
    turn_hashes = {sha256(str(record.get("turn_id")).encode()).hexdigest()[:12]
                   for record in stops if record.get("turn_id")}
    turns = host.get("lead_turns", [])
    if (len(turn_hashes) < 2 or not turns
            or not turns[-1].get("completed")
            or turns[-1].get("reported_outcome") != "completed"
            or turns[-1].get("turn_hash") not in turn_hashes):
        raise RuntimeError("old Codex lead lacks a hooked latest completed native turn")
    last_call = host.get("last_root_function_call") or {}
    calls = host.get("root_tool_calls_tail") or []
    if (last_call.get("name") != "wait_agent" or last_call.get("return_recorded")
            or not calls or calls[-1].get("name") != "wait_agent"
            or not calls[-1].get("call_hash")
            or (calls[-1].get("wait_timeout_ms") or 0) < 600000):
        raise RuntimeError("old Codex root is not in a long native wait")
    return {"reason": "old_runtime_wait_after_hooked_completed_turn",
            "latest_turn_hash": turns[-1]["turn_hash"],
            "wait_call_hash": calls[-1].get("call_hash")}


def wait_for_old_codex_root(process, root, update, state_path, session, run_id,
                            lead_id, error_log, deadline):
    """Let the old CLI exit, or end only a proven stale native wait."""
    waiting_since = None
    waiting_identity = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 0)
        if os.name == "nt":
            # Terminating an npm .cmd wrapper does not establish that its
            # native Codex descendant exited; never overlap a candidate root.
            return process.wait(timeout=remaining), None
        try:
            return process.wait(timeout=min(5, remaining)), None
        except subprocess.TimeoutExpired:
            try:
                proof = verified_old_codex_wait(
                    read_state_snapshot(state_path), session, run_id, lead_id,
                    codex_hook_capture_summary(root),
                    codex_host_trace(update["home"], session, lead_id, error_log,
                                     strict=True))
            except (OSError, TimeoutError, ValueError, RuntimeError):
                waiting_since = None
                waiting_identity = None
                continue
            identity = (proof["latest_turn_hash"], proof["wait_call_hash"])
            if identity != waiting_identity:
                waiting_since = time.monotonic()
                waiting_identity = identity
                continue
            if time.monotonic() - waiting_since < 15:
                continue
            # The released 1.5.1 hook cannot be changed after install. End
            # only its stuck CLI wait; candidate resume keeps root/run/lead IDs.
            process.terminate()
            exit_code = process.wait(timeout=5)
            proof["old_cli_exit_code"] = exit_code
            return exit_code, proof


def native_lead_registered(document, provider, session, run_id, lead_id):
    run = document.get("active_runs", {}).get(f"{provider}:{session}") or {}
    return run.get("run_id") == run_id and run.get("lead_identity") == lead_id


def registration_cli_exited(provider, processes):
    # Claude --print can exit successfully while background agents still run.
    return any((code := process.poll()) is not None
               and (provider == "codex" or code != 0)
               for process in processes.values())


def require_inflight_update_leads(documents, provider, sessions, run_ids, lead_ids):
    for label, session in sessions.items():
        run = documents[label].get("active_runs", {}).get(f"{provider}:{session}") or {}
        if (not native_lead_registered(documents[label], provider, session,
                                       run_ids[label], lead_ids[label])
                or run.get("status") != "active"
                or not any(item.get("identity") == lead_ids[label]
                           and item.get("role") == "lead" and item.get("state") == "working"
                           for item in run.get("delegations", []))):
            raise RuntimeError(f"{label}: native lead finished before in-flight update began")


def timestamp_ns(value):
    if not isinstance(value, str):
        raise RuntimeError("native or durable completion lacks a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RuntimeError("native or durable completion has an invalid timestamp") from None
    if parsed.tzinfo is None:
        raise RuntimeError("native or durable completion timestamp lacks a timezone")
    return int(parsed.timestamp() * 1_000_000_000)


def require_post_removal_lead_completion(document, provider, session, lead_id,
                                         native_trace, removed_ns):
    """Prove an original child turn spans old-source removal and later reconciles."""
    if not isinstance(removed_ns, int) or removed_ns <= 0:
        raise RuntimeError("old plugin removal boundary is missing")
    events = [event for event in document.get("event_history", ())
              if event.get("kind") == "lead_completed"
              and event.get("payload", {}).get("identity") == lead_id]
    if not events or not any(timestamp_ns(event.get("observed_at")) > removed_ns
                             for event in events):
        raise RuntimeError("original lead lacked durable completion after old plugin removal")
    turns = native_trace.get("lead_turns" if provider == "codex" else "child_turns", ())
    if provider == "codex":
        straddled = any(turn.get("completed") is True
                        and timestamp_ns(turn.get("started_at")) < removed_ns
                        < timestamp_ns(turn.get("completed_at"))
                        for turn in turns if turn.get("completed") is True)
        completed_after = any(turn.get("reported_outcome") == "completed"
                              and turn.get("completed") is True
                              and timestamp_ns(turn.get("completed_at")) > removed_ns
                              for turn in turns if turn.get("reported_outcome") == "completed")
    elif provider == "claude":
        if native_trace.get("child_prompt_shape_supported") is not True:
            raise RuntimeError("native Claude child prompt shape is unsupported")
        terminal = [turn for turn in turns
                    if turn.get("marker") == "completed"
                    and turn.get("last_stop_reason") == "end_turn"
                    and timestamp_ns(turn.get("last_assistant_at")) > removed_ns]
        completed_after = bool(terminal)
        straddled = any(
            timestamp_ns(turn.get("child_prompt_at")) < removed_ns
            < timestamp_ns(turn.get("last_assistant_at"))
            and (turn.get("last_stop_reason") == "end_turn"
                 or turn.get("last_stop_reason") is None and any(
                     timestamp_ns(final.get("child_prompt_at"))
                     > timestamp_ns(turn.get("last_assistant_at"))
                     and timestamp_ns(final.get("last_assistant_at"))
                     >= timestamp_ns(final.get("child_prompt_at"))
                     for final in terminal))
            for turn in turns if turn.get("marker") == "completed"
            and turn.get("last_stop_reason") in {None, "end_turn"})
    else:
        raise RuntimeError("unsupported native provider")
    if not straddled or not completed_after:
        raise RuntimeError("original native lead turn did not span and complete after old plugin removal")
    return {"session_id": session, "lead_id": lead_id,
            "durable_completion_after_removal": True,
            "native_turn_spanned_removal": True}


def codex_host_trace(home, session, lead_id, error_log, *, strict=False):
    """Extract only tool and turn metadata from disposable Codex JSONL."""
    _, lead_role = codex_fixture_roles()
    def fingerprint(value):
        return sha256(str(value).encode()).hexdigest()[:12] if value else None

    def records(path):
        if not path:
            if strict:
                raise ValueError("native Codex transcript is missing")
            return
        with path.open(errors="replace") as stream:
            for line in stream:
                if strict and not line.endswith("\n"):
                    raise ValueError("native Codex transcript ends with a partial row")
                try:
                    yield json.loads(line)
                except ValueError:
                    if strict:
                        raise ValueError("native Codex transcript has an invalid row") from None
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
    guidance_observed = []
    manual_controls = []
    for record in records(root_file):
        payload = record.get("payload") or {}
        # A phrase in the native transcript is only a mention. It may be a
        # quoted file/tool result rather than injected hook context.
        rendered = json.dumps(payload, default=str)
        provenance = {
            "row_type": record.get("type") if isinstance(record.get("type"), str) and record.get("type") in
                        {"event_msg", "response_item", "turn_context", "session_meta"} else None,
            "payload_type": payload.get("type") if isinstance(payload.get("type"), str) and payload.get("type") in
                            {"message", "function_call", "function_call_output",
                             "task_started", "task_complete"} else None,
            "payload_role": payload.get("role") if isinstance(payload.get("role"), str) and payload.get("role") in
                            {"system", "developer", "user", "assistant", "tool"} else None,
        }
        if len(guidance_observed) < 8:
            if "The lead outcome and tracked work are reconciled" in rendered:
                guidance_observed.append({"at": record.get("timestamp"),
                                          "kind": "candidate_normal_stop_mention",
                                          **provenance})
            elif "The observed lead is unavailable" in rendered:
                guidance_observed.append({"at": record.get("timestamp"),
                                          "kind": "old_recovering_mention",
                                          **provenance})
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
            if name in {"exec", "exec_command"} and len(manual_controls) < 8:
                arguments = str(payload.get("arguments") or "")
                if "symphony" in arguments and any(
                    control in arguments for control in ("status", "stop", "version")):
                    manual_controls.append({
                        "at": record.get("timestamp"),
                        "runtime": "old" if "symphony-old" in arguments else
                                   "candidate" if "symphony-candidate" in arguments else
                                   "unknown",
                        "attempted_control": "stop" if "symphony stop" in arguments else
                                   "status" if "symphony status" in arguments else
                                   "version" if "symphony version" in arguments else "other",
                    })
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
                                   and re.fullmatch(r"[a-z0-9_]{1,128}", task_name) else None,
                                   "invalid_task_name_shape": {
                                       "length": len(task_name),
                                       "hash": fingerprint(task_name),
                                       "hyphen": "-" in task_name,
                                       "slash": "/" in task_name,
                                       "dot": "." in task_name,
                                       "uppercase": any(char.isupper() for char in task_name),
                                   } if isinstance(task_name, str) and not re.fullmatch(
                                       r"[a-z0-9_]{1,128}", task_name) else None,
                                   "argument_keys": sorted(key for key in arguments
                                       if re.fullmatch(r"[A-Za-z0-9_]{1,64}", key))[:16],
                                   "task_text_fields": {field: {
                                       "type": type(arguments[field]).__name__,
                                       "chars": len(arguments[field]) if isinstance(arguments[field], str) else None,
                                   } for field in ("message", "prompt", "task") if field in arguments},
                                   # Native messages can be encrypted at rest. This is
                                   # transport metadata, not proof of child semantics.
                                   "lead_packet_metadata_matches":
                                   isinstance(arguments.get("message"), str) and
                                   bool(arguments["message"].strip()) and
                                   task_name == lead_role["task_name"] and
                                   arguments.get("model") == lead_role["model"] and
                                   arguments.get("reasoning_effort") == lead_role["effort"] and
                                   arguments.get("fork_turns") == "none"}
            if call_id and name == "followup_task":
                target = arguments.get("target")
                calls[call_id] = {"call_hash": fingerprint(call_id),
                                  "called_at": record.get("timestamp"),
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
                turn = turns.setdefault(turn_id, {"turn_hash": fingerprint(turn_id)})
                turn["started"] = True
                turn["started_at"] = record.get("timestamp")
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
                turn["completed_at"] = record.get("timestamp")
                turn["reported_outcome"] = "missing"
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
            "guidance_observed": guidance_observed,
            "manual_controls": manual_controls,
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


def check_case(provider, root, separate, timeout, budget, update=None,
               direct_stop_resume=False, baseline_env=None):
    case = root / ("live-update" if update else "worktrees" if separate else "same-worktree")
    case.mkdir()
    first, second = projects(case, separate)
    gate_dir = case / "native-gate"
    gate_dir.mkdir()
    state_dir = case / "state"
    state_dir.mkdir()
    candidate_version = (update["candidate_version"] if update else
                         package_version(Path(__file__).resolve().parents[2] / "plugins/symphony"))
    env = {**os.environ, **(update["env"] if update else baseline_env or {}),
           "SYMPHONY_STATE_DIR": str(state_dir),
           "SYMPHONY_RUNTIME_DIR": str(case / "retained runtimes"),
           "SYMPHONY_HOOK_DECISIONS_DIR": str(case / "hook decisions"),
           "SYMPHONY_PROFILE": "base" if provider == "codex" else "sonnet-5-5"}
    if provider == "claude":
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    logs = case / "logs"
    logs.mkdir()
    processes = {}
    sessions = {}
    old_root_interruption = None
    pre_resume_state = None
    try:
        for label, project in (("a", first), ("b", second)):
            sessions[label] = str(uuid.uuid4()) if provider == "claude" else ""
            launch_env = {**env, "SYMPHONY_NATIVE_GATE_DIR": str(gate_dir),
                          "SYMPHONY_NATIVE_GATE_LABEL": label}
            processes[label] = launch(provider, project, label, label == "a" and provider == "codex", launch_env, logs,
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
            if all((gate_dir / f"{label}.ready").exists() for label in processes):
                if provider == "codex" and not sessions["a"]:
                    sessions = {label: codex_session(logs, label) for label in processes}
                for label in processes:
                    gate_record = json.loads((gate_dir / f"{label}.ready").read_text())
                    if gate_record.get("session_id") != sessions[label]:
                        raise RuntimeError(f"{label}: native lead gate belongs to another root session")
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
                            f"{provider}:{session}", {}).get("run_id")
                            for label, session in sessions.items()):
                        break
            time.sleep(.25)
        else:
            gate_timeout = {}
            for label, project in (("a", first), ("b", second)):
                path = paths[label]
                try:
                    document = json.loads(path.read_text()) if path.is_file() else {}
                except (OSError, ValueError):
                    document = {}
                owned = document.get("active_runs", {}).get(f"{provider}:{sessions[label]}")
                archived = [run for run in document.get("recent_runs", [])
                            if run.get("session_id") == sessions[label]]
                gate_timeout[label] = {
                    "ready": (gate_dir / f"{label}.ready").exists(),
                    "state_exists": path.is_file(),
                    "owner_status": owned.get("status") if owned else "missing",
                    "lead_present": bool(owned and owned.get("lead_identity")),
                    "archived_count": len(archived),
                    "cli_exit_code": processes[label].poll(),
                }
            (case / "gate_timeout.json").write_text(json.dumps(gate_timeout))
            raise RuntimeError("native lead starts did not overlap before deadline")
        observed_leads = {label: json.loads((gate_dir / f"{label}.ready").read_text())["agent_id"]
                          for label in sessions}
        if len(set(observed_leads.values())) != 2:
            raise RuntimeError("native start gate reused one lead identity across roots")
        if separate:
            for label, doc in docs.items():
                owned = [run for key, run in doc.get("active_runs", {}).items()
                         if key.startswith(provider + ":")]
                if len(owned) != 1 or owned[0].get("session_id") != sessions[label]:
                    raise RuntimeError(f"{label}: expected one owned run in its worktree")
            if paths["a"] == paths["b"]:
                raise RuntimeError("different worktrees shared a state key")
        else:
            owned = [run for key, run in docs["a"].get("active_runs", {}).items()
                     if key.startswith(provider + ":")]
            if len(owned) != 2 or len({run.get("session_id") for run in owned}) != 2:
                raise RuntimeError("same worktree did not preserve two independent owner sessions")
            by_session = {run.get("session_id"): run for run in owned}
            if set(by_session) != set(sessions.values()):
                raise RuntimeError("same worktree owner sessions differ from native CLI sessions")
        observed_run_ids = {label: docs[label]["active_runs"][f"{provider}:{session}"]["run_id"]
                            for label, session in sessions.items()}
        if provider == "codex":
            for label in sessions:
                first_start = json.loads((gate_dir / f"{label}.first.json").read_text())
                own_run = docs[label]["active_runs"][f"codex:{sessions[label]}"]
                assessors = {item.get("identity") for item in own_run.get("delegations", [])
                             if item.get("role") == "assessor"}
                if (first_start.get("session_id") != sessions[label]
                        or first_start.get("agent_id") not in assessors):
                    raise RuntimeError(f"{label}: first native child was not this run's assessor")
                host = codex_host_trace(Path(env.get("CODEX_HOME", Path.home() / ".codex")),
                                        sessions[label], observed_leads[label], logs / f"{label}.errors")
                if not any(item["same_lead_id"] and item["lead_packet_metadata_matches"]
                           for item in host["spawn_calls"]):
                    raise RuntimeError(f"{label}: native original lead spawn lacks required transport metadata")
        # Native hosts may invoke the test hook before the package's Start
        # hook. Release only that barrier, then require the exact product
        # registration before a live update can begin.
        gate_release_before_update = not all(
            native_lead_registered(docs[label], provider, sessions[label],
                                   observed_run_ids[label], observed_leads[label])
            for label in sessions)
        if gate_release_before_update:
            (gate_dir / "release").touch()
        while not all(native_lead_registered(docs[label], provider, sessions[label],
                                             observed_run_ids[label], observed_leads[label])
                      for label in sessions):
            if time.monotonic() >= deadline:
                raise RuntimeError("native lead Start never registered after gate release")
            if registration_cli_exited(provider, processes):
                raise RuntimeError("native CLI exited before both lead Starts registered")
            time.sleep(.1)
            docs = {label: read_state_snapshot(path) for label, path in paths.items()}
        if update:
            require_inflight_update_leads(docs, provider, sessions,
                                          observed_run_ids, observed_leads)
        gate_evidence = {label: {
            "ready": (gate_dir / f"{label}.ready").is_file(),
            "start_matches_original_lead": json.loads((gate_dir / f"{label}.ready").read_text())
                                           ["agent_id"] == observed_leads[label],
            "owner_active": bool(docs[label]["active_runs"].get(f"{provider}:{session}")),
            "lead_matches_original": docs[label]["active_runs"][f"{provider}:{session}"]
                                     ["lead_identity"] == observed_leads[label],
            "registered_before_release": not gate_release_before_update,
        } for label, session in sessions.items()}
        if not all(item["start_matches_original_lead"] for item in gate_evidence.values()):
            raise RuntimeError("native gate lead identity differs from durable original lead")
        old_records = {}
        if update:
            snapshot_file = case / "update_event_counts.json"
            snapshot = {"pre_update": {label: event_counts(docs[label]) for label in sessions},
                        "gate_release_before_update": gate_release_before_update,
                        "original_owners": {label: {"session_id": sessions[label],
                                                    "run_id": observed_run_ids[label],
                                                    "lead_id": observed_leads[label]}
                                            for label in sessions}}
            snapshot_file.write_text(json.dumps(snapshot))
            old_records = update_while_gated(update, env, docs, sessions,
                                             {"a": first, "b": second}, deadline)
            snapshot["old_source_removed_ns"] = update["old_source_removed_ns"]
            snapshot["post_update"] = {label: event_counts(json.loads(paths[label].read_text()))
                                       for label in sessions}
            snapshot_file.write_text(json.dumps(snapshot))
        if not gate_release_before_update:
            (gate_dir / "release").touch()
        if provider == "codex":
            for label, process in processes.items():
                remaining = max(1, deadline - time.monotonic())
                if update and label == "a":
                    exit_code, old_root_interruption = wait_for_old_codex_root(
                        process, root, update, paths[label], sessions[label],
                        observed_run_ids[label], observed_leads[label],
                        logs / f"{label}.errors", deadline)
                    if old_root_interruption:
                        (case / "old_root_interruption.json").write_text(
                            json.dumps(old_root_interruption))
                else:
                    exit_code = process.wait(timeout=remaining)
                if exit_code and not (label == "a" and old_root_interruption):
                    raise RuntimeError(f"{label}: native CLI exited {exit_code}")
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
                if not retained_profile(old_profiles, sessions["a"],
                                            update["old_version"],
                                            old_records["a"]["runtime_root"],
                                            old_records["a"]["plugin_root"]):
                    raise RuntimeError("old retained runtime was lost before native candidate resume")
                captured_stops = [record for record in codex_hook_capture_summary(root)["records"]
                                  if record.get("event") == "SubagentStop"
                                  and record.get("session_id") == sessions["a"]
                                  and record.get("agent_id") == observed_leads["a"]
                                  and record.get("exit_marker_written")]
                host = codex_host_trace(update["home"], sessions["a"], observed_leads["a"],
                                        logs / "a.errors")
                pre_resume_spawn_calls = {call["call_hash"] for call in host["spawn_calls"]}
                terminal_branch = require_codex_old_lead_terminal(
                    pre_resume_state, captured_stops, host["lead_turns"])
                snapshot["pre_candidate_resume"] = event_counts(before_resume)
                snapshot["pre_candidate_resume_run_state"] = pre_resume_state
                snapshot["pre_candidate_terminal_branch"] = terminal_branch
                snapshot["pre_candidate_recovery_probe"] = codex_recovery_probe(
                    before_resume, sessions["a"], update["home"])
                snapshot_file.write_text(json.dumps(snapshot))
                lead_task_name = next((item["task_name"] for item in host["spawn_calls"]
                                       if item["same_lead_id"] and item.get("task_name")), None)
                if not lead_task_name:
                    raise RuntimeError("native host lacks the original lead's spawn task_name")
                resume_codex(env, first, sessions["a"], logs, deadline,
                             already_completed=pre_resume_state == "completed",
                             direct_stop=direct_stop_resume, lead_id=observed_leads["a"],
                             lead_task_name=lead_task_name)
                starts = [record for record in codex_hook_capture_summary(root)["records"]
                          if record.get("event") == "SessionStart"
                          and record.get("session_id") == sessions["a"]
                          and record.get("exit_marker_written")]
                if len(starts) < 2:
                    raise RuntimeError("candidate resume lacked a second native SessionStart")
                resumed_host = codex_host_trace(
                    update["home"], sessions["a"], observed_leads["a"],
                    logs / "a.resume.errors", strict=True)
                if any(call["call_hash"] not in pre_resume_spawn_calls
                       for call in resumed_host["spawn_calls"]):
                    # A rejected native child may be absent from the durable
                    # lead set; the native launch attempt still fails this gate.
                    raise RuntimeError("candidate resume attempted an extra native spawn")
                if direct_stop_resume and pre_resume_state != "completed":
                    controls = [record for record in codex_hook_capture_summary(root)["records"]
                                if record.get("event") == "UserPromptSubmit"
                                and record.get("session_id") == sessions["a"]
                                and record.get("control") == "stop"
                                and record.get("started_ns", 0) > starts[-1]["started_ns"]
                                and record.get("exit_marker_written")]
                    if not controls:
                        raise RuntimeError("candidate resume did not deliver a native normal Stop control")
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
            finalized = set()
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
                            if not retained_profile(records, sessions[label],
                                                        update["old_version"],
                                                        old_records[label]["runtime_root"],
                                                        old_records[label]["plugin_root"]):
                                raise RuntimeError(f"{label}: old retained activation was lost before resume")
                            snapshot.setdefault("pre_candidate_resume", {})[label] = {
                                "old_product_lead_completed": old_lead_completed,
                                "native_lead_stop_captured": True,
                                "candidate_recovery_probe": claude_recovery_probe(
                                    current[label], sessions[label],
                                    first if label == "a" else second, update["home"]),
                                "events": event_counts(current[label]),
                            }
                            snapshot_file.write_text(json.dumps(snapshot))
                        resume_claude(env, first if label == "a" else second,
                                      sessions[label], budget, deadline, logs, label)
                        resumed.add(label)
                    elif update and label in resumed and label not in finalized:
                        if claude_finalization_ready(
                                current[label], sessions[label], observed_run_ids[label],
                                observed_leads[label], state_dir):
                            resume_claude(env, first if label == "a" else second,
                                          sessions[label], budget, deadline, logs,
                                          f"{label}.finalize", finalize=True)
                            finalized.add(label)
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
        final_docs = (wait_for_completed_docs(paths, provider, sessions, observed_run_ids, deadline)
                      if provider == "codex" else
                      {label: json.loads(path.read_text()) for label, path in paths.items()})
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
            record = read_state_snapshot(record_path)
            if record.get("pending") or record.get("overflow"):
                raise RuntimeError("native session retained unresolved child callbacks after completion")
        post_removal_evidence = {}
        for label, doc in final_docs.items():
            released = gate_dir / "release"
            terminals = [event for event in doc.get("event_history", ())
                         if event.get("kind") == "lead_completed"
                         and event.get("payload", {}).get("identity") == observed_leads[label]]
            # ponytail: allow 1s filesystem clock granularity; use host monotonic receipts if subsecond ordering matters.
            if not released.is_file():
                raise RuntimeError(f"{label}: native lead start gate was never released")
            if not terminals:
                raise RuntimeError(f"{label}: original lead has no durable completed terminal")
            if (min(datetime.fromisoformat(event["observed_at"]).timestamp()
                    for event in terminals) < released.stat().st_mtime - 1):
                raise RuntimeError(f"{label}: lead completed before the gate released")
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
            if update:
                native_trace = (codex_host_trace(update["home"], sessions[label],
                                                 observed_leads[label], logs / f"{label}.errors",
                                                 strict=True)
                                if provider == "codex" else
                                claude_host_trace(update["home"], sessions[label],
                                                  observed_leads[label]))
                post_removal_evidence[label] = require_post_removal_lead_completion(
                    doc, provider, sessions[label], observed_leads[label], native_trace,
                    update["old_source_removed_ns"])
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
                    if label == "a":
                        matching = candidate_retained_profile(
                            records, sessions[label], expected, update["candidate_cache"],
                            Path(env["SYMPHONY_RUNTIME_DIR"]))
                    else:
                        matching = [item for item in records
                                    if retained_profile((item,), sessions[label], expected,
                                                        old_records[label]["runtime_root"],
                                                        old_records[label]["plugin_root"])]
                    if not matching:
                        raise RuntimeError(f"{label}: native session lacks expected {expected} heartbeat")
                elif not any(item.get("session_id") == sessions[label]
                             and item.get("plugin_version") == update["candidate_version"]
                             and Path(item.get("plugin_root", "")).resolve()
                             == update["candidate_source"].resolve()
                             for item in records):
                    raise RuntimeError(f"{label}: same-session Claude resume did not load the candidate")
            require_case_recovery_events(doc, observed_leads[label], provider, update,
                                         label, pre_resume_state)
        capture = codex_hook_capture_summary(root, provider)
        for label, session in sessions.items():
            gate_starts = [record for record in capture["records"]
                           if record.get("event") == "SubagentStart"
                           and record.get("native_gate_label") == label
                           and record.get("session_id") == session
                           and record.get("agent_id") == observed_leads[label]]
            if (len(gate_starts) != 1 or not gate_starts[0]["exit_marker_written"]
                    or (gate_starts[0].get("finished_ns") or 0)
                    < (gate_dir / "release").stat().st_mtime_ns):
                raise RuntimeError(f"{label}: native lead start gate did not release in order")
        claude_continuation = {}
        if update and provider == "claude":
            for label, session in sessions.items():
                trace = claude_host_trace(update["home"], session, observed_leads[label])
                messages = trace.get("root_message_calls", [])
                if any(not call["to_matches_original_lead"] for call in messages):
                    raise RuntimeError(f"{label}: native SendMessage targeted another lead")
                # A completed native callback may arrive before or during the
                # candidate wake. Record which continuation actually occurred.
                claude_continuation[label] = {
                    "original_lead_message_count": len(messages),
                    "original_lead_message_accepted": any(
                        any(not result["is_error"] for result in call["results"])
                        for call in messages),
                    "original_lead_completed_native_turns": sum(
                        turn["marker"] == "completed" for turn in trace.get("child_turns", [])),
                    "finalize_wake": label in finalized,
                }
                if messages and not claude_continuation[label]["original_lead_message_accepted"]:
                    raise RuntimeError(f"{label}: native SendMessage was not accepted")
                if not any(record.get("event") == "Stop"
                           and record.get("session_id") == session
                           and record.get("exit_marker_written")
                           and record.get("script_exit_marker_code") == 0
                           for record in capture["records"]):
                    raise RuntimeError(f"{label}: native root Stop was not delivered")
        return {"case": "live-update" if update else
                "different-branch-worktrees" if separate else "same-worktree",
                "observed_overlap": True, "completed": ["a", "b"],
                "candidate_version": candidate_version, "pending_callbacks": 0,
                "gate_version": update["old_version"] if update else candidate_version,
                "gate_release_before_update": gate_release_before_update,
                "gate_evidence": gate_evidence,
                **({"old_source_removed_ns": update["old_source_removed_ns"],
                    "post_removal_original_completions": post_removal_evidence}
                   if update else {}),
                "original_owners": {label: {"session_id": sessions[label],
                                             "run_id": observed_run_ids[label],
                                             "lead_id": observed_leads[label]}
                                    for label in sessions},
                **({"old_product_lead_completed_before_resume": {
                    label: snapshot["pre_candidate_resume"][label]["old_product_lead_completed"]
                    for label in sessions}}
                   if update and provider == "claude" else {}),
                **({"candidate_recovery_probe_before_resume": {
                    label: snapshot["pre_candidate_resume"][label]["candidate_recovery_probe"]
                    for label in sessions}}
                   if update and provider == "claude" else {}),
                **({"native_resumed": sorted(resumed), "native_finalize_wakes": sorted(finalized)}
                   if provider == "claude" else
                   {"native_resumed": ["a"]} if update and provider == "codex" else {}),
                **({"native_claude_continuation": claude_continuation}
                   if update and provider == "claude" else {}),
                **({"native_resume_mode": "status-version" if pre_resume_state == "completed"
                    else "direct-stop" if direct_stop_resume else "reconcile"}
                   if update and provider == "codex" else {}),
                **({"pre_candidate_resume_state": pre_resume_state}
                   if update and provider == "codex" else {}),
                **({"old_root_interruption": old_root_interruption}
                   if update and provider == "codex" else {}),
                "native_hook_capture": {
                    "configured": capture["configured"],
                    "records": [record for record in capture["records"]
                                if record.get("session_id") in set(sessions.values())],
                },
                **({"live_update": f"{update['old_version']}->{update['candidate_version']}"} if update else {})}
    finally:
        (gate_dir / "release").touch()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def check_codex_mixed_live_update(root, timeout, budget, update,
                                  direct_stop_resume=False):
    """Keep one released root in flight while a second root loads the candidate.

    Released 1.5.1 can reject a second simultaneous Windows root before it
    delegates. Candidate parallelism is exercised by the two baseline cases;
    this case tests continuity across the cache swap without depending on that
    immutable pre-update checker.
    """
    case = root / "live-update"
    case.mkdir()
    first, second = projects(case, False)
    gate_dir = case / "native-gate"
    gate_dir.mkdir()
    state_dir = case / "state"
    state_dir.mkdir()
    logs = case / "logs"
    logs.mkdir()
    env = {**os.environ, **update["env"], "SYMPHONY_STATE_DIR": str(state_dir),
           "SYMPHONY_RUNTIME_DIR": str(case / "retained runtimes"),
           "SYMPHONY_HOOK_DECISIONS_DIR": str(case / "hook decisions"),
           "SYMPHONY_PROFILE": "base"}
    state_path = state_file(state_dir, first)
    deadline = time.monotonic() + timeout
    processes = {}
    sessions = {}
    leads = {}
    runs = {}
    interruption = None
    try:
        processes["a"] = launch("codex", first, "a", True,
                                {**env, "SYMPHONY_NATIVE_GATE_DIR": str(gate_dir),
                                 "SYMPHONY_NATIVE_GATE_LABEL": "a",
                                 "SYMPHONY_NATIVE_HOLD_OLD_STOP": "1"}, logs, budget, "",
                                defer_recovery=True)
        while time.monotonic() < deadline:
            if processes["a"].poll() is not None:
                raise RuntimeError("old native CLI exited before original lead gate")
            if (gate_dir / "a.ready").is_file() and state_path.is_file():
                sessions["a"] = codex_session(logs, "a")
                ready = json.loads((gate_dir / "a.ready").read_text())
                if ready.get("session_id") != sessions["a"]:
                    raise RuntimeError("old lead gate belongs to another root")
                document = read_state_snapshot(state_path)
                run = document.get("active_runs", {}).get(f"codex:{sessions['a']}") or {}
                if run.get("run_id"):
                    runs["a"] = run["run_id"]
                    leads["a"] = ready["agent_id"]
                    break
            time.sleep(.1)
        else:
            raise RuntimeError("old original lead did not reach native start gate")
        first_start = json.loads((gate_dir / "a.first.json").read_text())
        assessors = {item.get("identity") for item in run.get("delegations", [])
                     if item.get("role") == "assessor"}
        if first_start.get("session_id") != sessions["a"] or first_start.get("agent_id") not in assessors:
            raise RuntimeError("old first native child was not the original assessor")
        # The independent capture hook can run before the product Start hook.
        # Release it only when necessary, then require the exact old owner.
        released_before_registration = not native_lead_registered(
            document, "codex", sessions["a"], runs["a"], leads["a"])
        if released_before_registration:
            (gate_dir / "release").touch()
        while time.monotonic() < deadline:
            document = read_state_snapshot(state_path)
            if native_lead_registered(document, "codex", sessions["a"], runs["a"], leads["a"]):
                break
            if processes["a"].poll() is not None:
                raise RuntimeError("old root exited before original lead registration")
            time.sleep(.1)
        else:
            raise RuntimeError("old original lead Start never registered")
        require_inflight_update_leads({"a": document}, "codex", sessions, runs, leads)
        activation = document.get("activation", {}).get("codex", {})
        profiles = [activation, *activation.get("session_profiles", [])]
        if not any(item.get("session_id") == sessions["a"]
                   and item.get("plugin_version") == update["old_version"]
                   for item in profiles):
            raise RuntimeError("old A did not use its guarded 1.5.1 runtime")
        snapshot = {"original_owners": {"a": {"session_id": sessions["a"],
                                               "run_id": runs["a"], "lead_id": leads["a"]}},
                    "gate_release_before_update": released_before_registration,
                    "pre_update": {"a": event_counts(document)}}
        snapshot_file = case / "update_event_counts.json"
        snapshot_file.write_text(json.dumps(snapshot))
        old_records = update_while_gated(update, env, {"a": document}, sessions,
                                         {"a": first}, deadline)
        snapshot["old_source_removed_ns"] = update["old_source_removed_ns"]
        snapshot_file.write_text(json.dumps(snapshot))
        if not released_before_registration:
            (gate_dir / "release").touch()

        # Hold the old root's first native Stop callback after its blocked
        # turn. Its host stays alive with the original lead while B registers.
        while True:
            current = read_state_snapshot(state_path)
            old_run = current.get("active_runs", {}).get(f"codex:{sessions['a']}")
            if (old_run and old_run.get("run_id") == runs["a"]
                    and old_run.get("lead_identity") == leads["a"]
                    and old_run.get("status") == "recovering"
                    and (gate_dir / "a.stop-ready.json").is_file()):
                held = json.loads((gate_dir / "a.stop-ready.json").read_text())
                if held.get("session_id") != sessions["a"] or not held.get("invocation_id"):
                    raise RuntimeError("held old Stop belongs to another native root")
                break
            if not old_run:
                raise RuntimeError("old original run archived before candidate B launch")
            if time.monotonic() >= deadline:
                raise RuntimeError("old original lead did not become recovering before B launch")
            time.sleep(.1)
        processes["b"] = launch("codex", second, "b", False,
                                {**env, "SYMPHONY_NATIVE_GATE_DIR": str(gate_dir),
                                 "SYMPHONY_NATIVE_GATE_LABEL": "b"}, logs, budget, "")
        while time.monotonic() < deadline:
            if (gate_dir / "b.ready").is_file():
                sessions["b"] = codex_session(logs, "b")
                ready = json.loads((gate_dir / "b.ready").read_text())
                if ready.get("session_id") != sessions["b"]:
                    raise RuntimeError("candidate lead gate belongs to another root")
                leads["b"] = ready["agent_id"]
                current = read_state_snapshot(state_path)
                owned = current.get("active_runs", {}).get(f"codex:{sessions['b']}")
                archived = [item for item in current.get("recent_runs", [])
                            if item.get("session_id") == sessions["b"]]
                candidate_run = owned or (archived[0] if len(archived) == 1 else None)
                if candidate_run and candidate_run.get("lead_identity") == leads["b"]:
                    old_run = current.get("active_runs", {}).get(f"codex:{sessions['a']}")
                    if (not old_run or old_run.get("run_id") != runs["a"]
                            or old_run.get("lead_identity") != leads["a"]
                            or old_run.get("status") != "recovering"):
                        raise RuntimeError("candidate B lead did not overlap recovering original A")
                    runs["b"] = candidate_run["run_id"]
                    break
            if processes["b"].poll() is not None:
                raise RuntimeError("candidate B exited before managed lead registration")
            time.sleep(.1)
        else:
            raise RuntimeError("candidate B did not register its native original lead")
        if sessions["a"] == sessions["b"] or leads["a"] == leads["b"] or runs["a"] == runs["b"]:
            raise RuntimeError("old and candidate roots reused an owner identity")
        activation = current.get("activation", {}).get("codex", {})
        profiles = [activation, *activation.get("session_profiles", [])]
        if not candidate_retained_profile(profiles, sessions["b"],
                                          update["candidate_version"],
                                          update["candidate_cache"],
                                          Path(env["SYMPHONY_RUNTIME_DIR"])):
            raise RuntimeError("candidate B did not use its guarded installed runtime")
        snapshot["original_owners"]["b"] = {"session_id": sessions["b"],
                                               "run_id": runs["b"], "lead_id": leads["b"]}
        snapshot["candidate_b_native_start_ns"] = json.loads(
            (gate_dir / "b.ready").read_text())["started_ns"]
        old_host_at_overlap = codex_host_trace(
            update["home"], sessions["a"], leads["a"], logs / "a.errors", strict=True)
        old_turns_at_overlap = old_host_at_overlap["lead_turns"]
        if (len(old_turns_at_overlap) != 1
                or not old_turns_at_overlap[0].get("completed")
                or old_turns_at_overlap[0].get("reported_outcome") != "blocked"
                or old_host_at_overlap["followup_calls"]
                or len(old_host_at_overlap["spawn_calls"]) != 2
                or processes["a"].poll() is not None):
            raise RuntimeError("old root advanced beyond its blocked turn during Stop hold")
        snapshot["old_host_at_b_registration"] = {
            "original_turn_hash": old_turns_at_overlap[0]["turn_hash"],
            "followup_count": 0, "old_cli_alive": True}
        snapshot["post_update"] = {"a": event_counts(current), "b": event_counts(current)}
        snapshot_file.write_text(json.dumps(snapshot))

        (gate_dir / "a.stop-release").touch()
        stop_release_ns = (gate_dir / "a.stop-release").stat().st_mtime_ns
        exit_code, interruption = wait_for_old_codex_root(
            processes["a"], root, update, state_path, sessions["a"], runs["a"],
            leads["a"], logs / "a.errors", deadline)
        if interruption:
            (case / "old_root_interruption.json").write_text(json.dumps(interruption))
        if exit_code and not interruption:
            raise RuntimeError(f"old A native CLI exited {exit_code}")
        if processes["b"].wait(timeout=max(1, deadline - time.monotonic())):
            raise RuntimeError("candidate B native CLI exited unsuccessfully")
        before_resume = read_state_snapshot(state_path)
        pre_resume_state = codex_pre_resume_state(
            before_resume, sessions["a"], runs["a"], leads["a"])
        old_activation = before_resume.get("activation", {}).get("codex", {})
        old_profiles = [old_activation, *old_activation.get("session_profiles", [])]
        if not retained_profile(old_profiles, sessions["a"], update["old_version"],
                                old_records["a"]["runtime_root"],
                                old_records["a"]["plugin_root"]):
            raise RuntimeError("old retained runtime was lost before candidate resume")
        host = codex_host_trace(update["home"], sessions["a"], leads["a"],
                                logs / "a.errors", strict=True)
        if (host["lead_turns"][0]["turn_hash"]
                != snapshot["old_host_at_b_registration"]["original_turn_hash"]
                or any(timestamp_ns(turn["started_at"]) < stop_release_ns
                       for turn in host["lead_turns"][1:])
                or any(timestamp_ns(call["called_at"]) < stop_release_ns
                       for call in host["followup_calls"])):
            raise RuntimeError("old root advanced before its held Stop released")
        stops = [item for item in codex_hook_capture_summary(root)["records"]
                 if item.get("event") == "SubagentStop"
                 and item.get("session_id") == sessions["a"]
                 and item.get("agent_id") == leads["a"] and item.get("exit_marker_written")]
        branch = require_codex_old_lead_terminal(pre_resume_state, stops, host["lead_turns"])
        original_spawn_calls = {item["call_hash"] for item in host["spawn_calls"]}
        lead_task_name = next((item["task_name"] for item in host["spawn_calls"]
                               if item["same_lead_id"] and item.get("task_name")), None)
        if not lead_task_name:
            raise RuntimeError("old lead lacks native task name for same-session resume")
        snapshot["pre_candidate_resume_run_state"] = pre_resume_state
        snapshot["pre_candidate_terminal_branch"] = branch
        snapshot["pre_candidate_recovery_probe"] = codex_recovery_probe(
            before_resume, sessions["a"], update["home"])
        snapshot_file.write_text(json.dumps(snapshot))
        resume_codex(env, first, sessions["a"], logs, deadline,
                     already_completed=pre_resume_state == "completed",
                     direct_stop=direct_stop_resume, lead_id=leads["a"],
                     lead_task_name=lead_task_name)
        after_resume = read_state_snapshot(state_path)
        resumed_activation = after_resume.get("activation", {}).get("codex", {})
        resumed_profiles = [resumed_activation,
                            *resumed_activation.get("session_profiles", [])]
        if not candidate_retained_profile(resumed_profiles, sessions["a"],
                                          update["candidate_version"],
                                          update["candidate_cache"],
                                          Path(env["SYMPHONY_RUNTIME_DIR"])):
            raise RuntimeError("original A resume did not activate the candidate runtime")
        starts = [item for item in codex_hook_capture_summary(root)["records"]
                  if item.get("event") == "SessionStart"
                  and item.get("session_id") == sessions["a"]
                  and item.get("exit_marker_written")]
        if len(starts) < 2:
            raise RuntimeError("original A lacked candidate native SessionStart")
        resumed_host = codex_host_trace(update["home"], sessions["a"], leads["a"],
                                        logs / "a.resume.errors", strict=True)
        if any(item["call_hash"] not in original_spawn_calls
               for item in resumed_host["spawn_calls"]):
            raise RuntimeError("candidate A resume attempted an extra native spawn")
        if branch == "same_id_unreconciled":
            terminal_hashes = {sha256(str(item["turn_id"]).encode()).hexdigest()[:12]
                               for item in codex_hook_capture_summary(root)["records"]
                               if item.get("event") == "SubagentStop"
                               and item.get("session_id") == sessions["a"]
                               and item.get("agent_id") == leads["a"]
                               and item.get("exit_marker_written")
                               and item.get("script_exit_marker_code") == 0
                               and item.get("turn_id")}
            require_codex_unreconciled_recovery(host["lead_turns"],
                                               resumed_host["lead_turns"], terminal_hashes)
        final = wait_for_completed_docs({"a": state_path, "b": state_path},
                                        "codex", sessions, runs, deadline)
        document = final["a"]
        for label in ("a", "b"):
            matching = [item for item in document.get("recent_runs", [])
                        if item.get("session_id") == sessions[label]]
            if (len(matching) != 1 or matching[0].get("status") != "completed"
                    or matching[0].get("run_id") != runs[label]
                    or (matching[0].get("outcome") or {}).get("status") != "completed"
                    or matching[0].get("lead_identity") != leads[label]):
                raise RuntimeError(f"{label}: original managed run did not archive completed")
            durable_leads = {item.get("identity") for item in matching[0].get("delegations", [])
                             if item.get("role") == "lead"}
            if durable_leads != {leads[label]}:
                raise RuntimeError(f"{label}: managed run changed its lead set")
            if not any(item.get("kind") == "lead_completed"
                       and item.get("payload", {}).get("identity") == leads[label]
                       for item in document.get("event_history", [])):
                raise RuntimeError(f"{label}: original lead lacks durable completion")
        if any(item.get("session_id") in set(sessions.values())
               for item in document.get("active_runs", {}).values()):
            raise RuntimeError("mixed update left an original run active")
        require_case_recovery_events(document, leads["a"], "codex", update,
                                     "a", pre_resume_state)
        for record_path in state_dir.glob(".session-*.json"):
            pending = read_state_snapshot(record_path)
            if pending.get("pending") or pending.get("overflow"):
                raise RuntimeError("mixed update retained unresolved child callbacks")
        proof = require_post_removal_lead_completion(
            document, "codex", sessions["a"], leads["a"],
            codex_host_trace(update["home"], sessions["a"], leads["a"],
                             logs / "a.errors", strict=True), update["old_source_removed_ns"])
        for label in ("a", "b"):
            host = codex_host_trace(update["home"], sessions[label], leads[label],
                                    logs / f"{label}.errors", strict=True)
            original_calls = [call for call in host["spawn_calls"]
                              if call["same_lead_id"]]
            if (len(original_calls) != 1
                    or not original_calls[0]["lead_packet_metadata_matches"]):
                (case / "spawn_probe.json").write_text(json.dumps({
                    "label": label, "session_id": sessions[label],
                    "lead_id": leads[label], "spawn_calls": host["spawn_calls"][:8]}))
                raise RuntimeError(f"{label}: original native lead spawn lacks transport metadata")
        candidate_start = snapshot["candidate_b_native_start_ns"]
        if candidate_start <= update["old_source_removed_ns"]:
            raise RuntimeError("candidate B started before old source removal")
        capture = codex_hook_capture_summary(root)
        held_stops = [item for item in capture["records"]
                      if item.get("invocation_id") == held["invocation_id"]
                      and item.get("event") == "Stop"
                      and item.get("session_id") == sessions["a"]]
        if (len(held_stops) != 1 or not held_stops[0].get("exit_marker_written")
                or held_stops[0].get("script_exit_marker_code") != 0
                or held_stops[0].get("finished_ns", 0)
                < (gate_dir / "a.stop-release").stat().st_mtime_ns):
            raise RuntimeError("held original root Stop did not exit after release")
        for label in ("a", "b"):
            gate_starts = [item for item in capture["records"]
                           if item.get("event") == "SubagentStart"
                           and item.get("native_gate_label") == label
                           and item.get("session_id") == sessions[label]
                           and item.get("agent_id") == leads[label]]
            if (len(gate_starts) != 1 or not gate_starts[0]["exit_marker_written"]
                    or gate_starts[0].get("finished_ns", 0)
                    < (gate_dir / "release").stat().st_mtime_ns):
                raise RuntimeError(f"{label}: native lead start gate did not release in order")
            if not any(item.get("event") == "Stop" and item.get("session_id") == sessions[label]
                       and item.get("exit_marker_written")
                       and item.get("script_exit_marker_code") == 0
                       for item in capture["records"]):
                raise RuntimeError(f"{label}: native root Stop was not captured")
        return {"case": "live-update", "observed_overlap": True,
                "completed": ["a", "b"], "pending_callbacks": 0,
                "live_update": f"{update['old_version']}->{update['candidate_version']}",
                "gate_version": {"a": update["old_version"],
                                 "b": update["candidate_version"]},
                "candidate_version": update["candidate_version"],
                "gate_release_before_update": released_before_registration,
                "old_recovering_run_overlapped_candidate_registration": True,
                "old_stop_held_through_candidate_registration": True,
                "old_source_removed_ns": update["old_source_removed_ns"],
                "post_removal_original_completions": {"a": proof},
                "native_spawn_metadata_checked": ["a", "b"],
                "original_owners": snapshot["original_owners"],
                "pre_candidate_resume_state": pre_resume_state,
                "old_root_interruption": interruption,
                "native_resumed": ["a"],
                "native_hook_capture": {"configured": capture["configured"],
                                        "records": [item for item in capture["records"]
                                                    if item.get("session_id") in set(sessions.values())]}}
    finally:
        (gate_dir / "release").touch()
        (gate_dir / "a.stop-release").touch()
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
        if (entry.get("event") not in {"SessionStart", "UserPromptSubmit",
                                       "PreToolUse", "SubagentStart", "SubagentStop", "Stop"}
                or entry.get("event") == "PreToolUse"
                and entry.get("tool_name") not in {"Agent", "Task"}):
            continue
        records.append({key: entry.get(key) for key in
                        ("invocation_id", "event", "session_id", "agent_id", "turn_id",
                         "parent_id", "prompt_id_hash", "message_hash", "cwd",
                         "started_ns", "payload_keys", "control", "tool_name",
                         "tool_use_id_hash", "agent_type", "isolation", "background",
                         "native_gate_label", "native_gate_session_id",
                         "background_tasks_type", "background_tasks_count",
                         "background_task_states")}
                       | {"exit_marker_written": marker.get("invocation_id") == entry.get("invocation_id"),
                          "finished_ns": marker.get("finished_ns"),
                          "native_home_matches_expected": (
                              Path(entry["native_home"]).resolve()
                              in {(root / f"{provider}-live-update-home").resolve(),
                                  (root / f"{provider}-baseline-home").resolve()})
                          if entry.get("native_home") else False,
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

_CODEX_NATIVE_REJECT = {
    402: "run_scope", 406: "lead_identity_or_start", 410: "lead_route",
    414: "native_home", 417: "child_transcript_path",
    423: "child_path_trust", 427: "session_meta", 430: "child_identity",
    434: "parent_identity", 463: "transcript_parse",
}
_CODEX_RECOVERY_REJECT = {
    536: "run_not_retryable", 539: "native_transcript_unavailable",
    543: "failure_time_missing", 548: "lead_record_missing",
    555: "latest_turn_route_or_time", 559: "latest_turn_already_observed",
    573: "failed_turn_anchor_missing", 577: "failed_turn_order",
    581: "failed_native_turn_invalid", 584: "latest_message_invalid",
    586: "latest_outcome_invalid",
}
_RELEASED_FAILED_REJECT = {
    481: "new_lineage_present", 490: "failure_count",
    501: "start_count", 506: "failure_outcome",
    509: "observed_failure_after_new_start", 514: "native_turn_count_before_failure",
    525: "native_failed_turn_mismatch",
}
_CODEX_COMPLETION_RESULT = {
    607: "not_completing", 611: "no_terminal_anchor", 614: "native_transcript_unavailable",
    619: "terminal_anchor_missing", 621: "latest_is_accepted_terminal",
    624: "native_turn_order_conflict", 632: "native_turn_route_or_time",
    635: "newer_turn_running", 637: "newer_turn_time",
    640: "newer_turn_message", 650: "newer_turn_completed",
}


def codex_recovery_probe(document, session, home):
    """Classify why a released recovering lead's exact native result was not adopted."""
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    try:
        from symphony import host_evidence
        from symphony.store import _state_from_dict
        state = _state_from_dict(document)
        run = state.active_runs.get(f"codex:{session}")
        if run is None:
            return {"result": "no_active_run"}
        state = replace(state, active_run=run)
        captured = {"active_status": run.status,
                    "retryable_matches_lead":
                    run.assessment.get("_retryable_lead") == run.lead_identity,
                    "lineage_fields_present": any(key in run.assessment for key in
                                                  ("_retryable_lead_turn", "_terminal_turns")),
                    "native_home_present": (home / "sessions").is_dir()}

        def trace(frame, event, value):
            if event != "return":
                return trace
            line = frame.f_lineno
            local = frame.f_locals
            if frame.f_code is host_evidence._native_lead_turns.__code__:
                captured["native_stage"] = (
                    "accepted" if value is not None else
                    _CODEX_NATIVE_REJECT.get(line, "unknown"))
                captured["native_source_line"] = line
                captured["child_transcript_path_count"] = len(local.get("paths", ()))
                if value is not None:
                    _, turns, order, latest, _ = value
                    captured["native_turn_count"] = len(order)
                    captured["latest_native_turn_complete"] = bool(
                        turns.get(latest, {}).get("completed_at"))
                    outcome = turns.get(latest, {}).get("outcome")
                    captured["latest_native_outcome"] = (
                        outcome if outcome in {"completed", "blocked", "failed", "abandoned"}
                        else "missing_or_invalid")
            elif frame.f_code is host_evidence._released_failed_turn.__code__:
                captured["released_failure_stage"] = (
                    "accepted" if value is not None else
                    _RELEASED_FAILED_REJECT.get(line, "unknown"))
                captured["released_failure_source_line"] = line
                for name, field in (("failures", "failure_event_count"),
                                    ("starts", "lead_start_count"),
                                    ("before_failure", "native_turns_before_failure")):
                    if name in local:
                        captured[field] = len(local[name])
                if local.get("failed_at") and local.get("latest_start"):
                    captured["observed_failure_before_latest_start"] = (
                        local["failed_at"] < local["latest_start"])
                if local.get("failed_at") and local.get("ended"):
                    captured["native_failure_completed_before_observation"] = (
                        local["ended"] <= local["failed_at"])
                if local.get("next_start") and local.get("ended"):
                    captured["native_failure_completed_before_next_start"] = (
                        local["ended"] < local["next_start"])
            elif frame.f_code is host_evidence.codex_recovered_lead_event.__code__:
                captured["result"] = "accepted" if value is not None else "rejected"
                captured["stage"] = (
                    "accepted" if value is not None else
                    _CODEX_RECOVERY_REJECT.get(line, "unknown"))
                captured["source_line"] = line
            return trace

        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            host_evidence.codex_recovered_lead_event(
                state, session, {"CODEX_HOME": str(home)})
        finally:
            sys.settrace(previous)
        return captured
    except Exception as error:
        return {"result": "probe_error", "error_type": type(error).__name__}


def codex_completion_probe(document, session, home):
    """Classify why an original completing run did not finalize at native Stop."""
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    try:
        from symphony import host_evidence
        from symphony.store import _state_from_dict
        state = _state_from_dict(document)
        run = state.active_runs.get(f"codex:{session}")
        if run is None:
            return {"result": "no_active_run"}
        state = replace(state, active_run=run)
        lead_id = run.lead_identity or ""
        anchors = tuple(run.assessment.get("_terminal_turns", {}).get(lead_id, ()))
        captured = {"native_home_present": (home / "sessions").is_dir(),
                    "terminal_anchor_count": len(anchors)}
        def trace(frame, event, value):
            native = frame.f_code is host_evidence._native_lead_turns.__code__
            completion = frame.f_code is host_evidence.codex_completing_lead_turn.__code__
            if not native and not completion:
                return None
            if event == "return":
                if native:
                    captured["native_stage"] = (
                        "accepted" if value is not None else
                        _CODEX_NATIVE_REJECT.get(frame.f_lineno, "unknown"))
                    captured["native_source_line"] = frame.f_lineno
                    local = frame.f_locals
                    captured["child_transcript_path_count"] = len(local.get("paths", ()))
                    if value is not None:
                        _, turns, order, latest, _ = value
                        captured["native_turn_count"] = len(order)
                        captured["latest_turn_is_accepted_terminal"] = (
                            f"turn_id:{latest}" in anchors)
                        captured["latest_turn_completed"] = bool(
                            turns.get(latest, {}).get("completed_at"))
                else:
                    captured["completion_stage"] = _CODEX_COMPLETION_RESULT.get(
                        frame.f_lineno, "unknown")
                    captured["completion_source_line"] = frame.f_lineno
                    captured["freshness"] = value[0] if isinstance(value, tuple) else "unknown"
            return trace
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            host_evidence.codex_completing_lead_turn(
                state, session, {"CODEX_HOME": str(home)})
        finally:
            sys.settrace(previous)
        return captured
    except Exception as error:
        return {"result": "probe_error", "error_type": type(error).__name__}


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


def claude_child_user_kind(content):
    if isinstance(content, str):
        return "prompt"
    if isinstance(content, list) and content and all(isinstance(item, dict) for item in content):
        kinds = {item.get("type") for item in content}
        if kinds == {"text"} and all(isinstance(item.get("text"), str) for item in content):
            return "prompt"
        if kinds == {"tool_result"}:
            return "tool_result"
    return "unsupported"


def claude_host_trace(home, session, lead_id):
    """Join native Agent calls and child turns without exporting transcript text."""
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    from symphony.host_evidence import _native_jsonl

    def digest(value):
        return sha256(str(value).encode()).hexdigest()[:12] if value else None

    paths = tuple((home / "projects").glob(f"*/{session}"))
    if len(paths) != 1:
        return {"native_session_path_count": len(paths)}
    directory = paths[0]
    parent = _native_jsonl(directory.with_suffix(".jsonl"))
    child_path = directory / "subagents" / f"agent-{lead_id}.jsonl"
    child = _native_jsonl(child_path)
    meta_path = child_path.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.stat().st_size <= 65536 else {}
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    result = {"root_transcript_readable": parent is not None,
              "child_transcript_readable": child is not None,
              "child_prompt_shape_supported": child is not None,
              "meta_launch_id_hash": digest(meta.get("toolUseId")),
              "meta_agent_type": meta.get("agentType") if re.fullmatch(
                  r"symphony:symphony-[a-z0-9-]{1,96}", str(meta.get("agentType") or ""))
                  else None,
              "root_agent_calls": [], "root_message_calls": [], "child_turns": []}
    if parent is not None:
        prompt = None
        results = {}
        for row in parent:
            message = row.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if row.get("type") == "user" and isinstance(content, str):
                prompt = {"root_prompt_id_hash": digest(row.get("uuid")),
                          "root_prompt_at": row.get("timestamp")}
            if row.get("type") == "user" and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        results.setdefault(digest(item.get("tool_use_id")), []).append(
                            {"at": row.get("timestamp"),
                             "is_error": item.get("is_error") is True})
            if row.get("type") != "assistant" or not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                details = item.get("input") or {}
                if not isinstance(details, dict):
                    details = {}
                if item.get("name") == "SendMessage":
                    result["root_message_calls"].append({
                        "tool_use_id_hash": digest(item.get("id")),
                        "called_at": row.get("timestamp"),
                        **(prompt or {}),
                        "to_matches_original_lead": details.get("to") == lead_id,
                        "to_hash": digest(details.get("to")),
                        "requests_outcome_marker": "SYMPHONY_OUTCOME" in str(
                            details.get("message") or ""),
                    })
                    continue
                if item.get("name") != "Agent":
                    continue
                agent_type = details.get("subagent_type")
                result["root_agent_calls"].append({
                    "tool_use_id_hash": digest(item.get("id")),
                    "matches_lead_meta": item.get("id") == meta.get("toolUseId"),
                    "root_assistant_id_hash": digest(row.get("uuid")),
                    "called_at": row.get("timestamp"),
                    **(prompt or {}),
                    "agent_type": agent_type if isinstance(agent_type, str) and re.fullmatch(
                        r"symphony:symphony-[a-z0-9-]{1,96}", agent_type) else None,
                    "resume_id_hash": digest(details.get("resume")),
                    "run_in_background": details.get("run_in_background") is True,
                    "isolation_worktree": details.get("isolation") == "worktree",
                })
        for call in result["root_agent_calls"]:
            call["results"] = results.get(call["tool_use_id_hash"], [])
        for call in result["root_message_calls"]:
            call["results"] = results.get(call["tool_use_id_hash"], [])
    if child is not None:
        turn = None
        for row in child:
            message = row.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if row.get("type") == "user":
                kind = claude_child_user_kind(content)
                if kind == "unsupported":
                    result["child_prompt_shape_supported"] = False
                    turn = None
                elif kind == "prompt":
                    turn = {"child_prompt_id_hash": digest(row.get("uuid")),
                            "child_prompt_at": row.get("timestamp"),
                            "last_assistant_at": None,
                            "last_assistant_id_hash": None,
                            "last_stop_reason": None,
                            "marker": None,
                            "handback_count": 0}
                    result["child_turns"].append(turn)
            elif row.get("type") == "assistant" and turn is not None:
                turn["last_assistant_at"] = row.get("timestamp")
                turn["last_assistant_id_hash"] = digest(row.get("uuid"))
                turn["last_stop_reason"] = message.get("stop_reason")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            match = re.findall(r"^SYMPHONY_OUTCOME:\s*(\{[^\n]*\})",
                                               str(item.get("text") or ""), re.MULTILINE)
                            if len(match) == 1:
                                try:
                                    decoded = json.loads(match[0])
                                    marker = decoded.get("status") if isinstance(decoded, dict) else None
                                    turn["marker"] = (marker if isinstance(marker, str)
                                                      and marker in {
                                                          "completed", "blocked", "failed",
                                                          "abandoned"} else "invalid")
                                    if turn["marker"] == "invalid":
                                        turn["marker_status_shape"] = (
                                            marker if isinstance(marker, str) and re.fullmatch(
                                                r"[a-z_]{1,32}", marker) else
                                            type(marker).__name__)
                                except (ValueError, AttributeError):
                                    turn["marker"] = "invalid"
                                    turn["marker_status_shape"] = "invalid_json"
                        if item.get("type") == "tool_use" and item.get("name") == "SubagentHandback":
                            turn["handback_count"] += 1
    return result


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
                "observed_at": event.get("observed_at"),
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
                "observed_at": item.get("observed_at"),
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
        root_stop_events = [event_summary(event, root_sessions)
                            for event in document.get("event_history", [])
                            if event.get("kind") == "stop_requested"]
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
        completion_probes = {}
        if provider == "codex":
            native_home = (root / "codex-live-update-home" if case == "live-update" else
                           root / "codex-baseline-home" if (root / "codex-baseline-home").is_dir() else
                           Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
            for label, session in cli_sessions.items():
                active = document.get("active_runs", {}).get(f"codex:{session}")
                if active and active.get("status") == "completing":
                    completion_probes[label] = codex_completion_probe(
                        document, session, native_home)
        hook_decisions = []
        for decision_path in (case_root / "hook decisions").glob("*.json"):
            try:
                decision = json.loads(decision_path.read_text())
            except (OSError, ValueError):
                continue
            if decision.get("session_id") in root_sessions:
                hook_decisions.append(decision)
        cases.append({"case": case, "labels": labels, "cli_session_ids": cli_sessions,
                      "activation_profiles": [activation_summary(item) for item in profiles
                                              if isinstance(item, dict) and item.get("session_id")],
                      "active_runs": [run_summary(run) for run in document.get("active_runs", {}).values()],
                      "recent_runs": [run_summary(run) for run in document.get("recent_runs", [])],
                      "event_kinds": [event.get("kind") for event in document.get("event_history", [])],
                      "lead_lifecycle_events": relevant_events,
                      "root_stop_events": root_stop_events,
                      "hook_decisions": sorted(hook_decisions,
                                               key=lambda item: item.get("observed_at", "")),
                      "session_records": session_records,
                      "candidate_recovery_probes": recovery_probes,
                      "codex_completion_probes": completion_probes,
                      "event_counts": event_counts(document),
                      "native_resume_phases": {label: json.loads(phase.read_text())
                                               for label in labels
                                               if (phase := case_root / "logs" /
                                                   f"{label}.resume.phase.json").is_file()},
                      "old_root_interruption": json.loads(interruption.read_text())
                      if (interruption := case_root / "old_root_interruption.json").is_file()
                      else None,
                      "update_event_counts": snapshots})
    host = {}
    if provider == "codex":
        for case in cases:
            case_root = root / case["case"]
            home = (root / "codex-live-update-home" if case["case"] == "live-update" else
                    root / "codex-baseline-home" if (root / "codex-baseline-home").is_dir() else
                    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
            runs = [*case["active_runs"], *case["recent_runs"]]
            for label, session in case["cli_session_ids"].items():
                final_lead = next((run.get("lead_id") for run in runs
                                   if run.get("session_id") == session), None)
                owner = ((case.get("update_event_counts") or {}).get("original_owners")
                         or {}).get(label) or {}
                lead = owner.get("lead_id") or final_lead
                trace = codex_host_trace(
                    home, session, lead, case_root / "logs" / f"{label}.errors")
                if final_lead and final_lead != lead:
                    replacement = codex_host_trace(
                        home, session, final_lead, case_root / "logs" / f"{label}.errors")
                    trace["replacement_lead_turns"] = replacement["lead_turns"]
                    trace["replacement_parent_matches_root"] = replacement["lead_parent_matches_root"]
                host[f"{case['case']}:{label}"] = trace
    elif provider == "claude":
        for case in cases:
            home = (root / "claude-live-update-home" if case["case"] == "live-update" else
                    root / "claude-baseline-home")
            runs = [*case["active_runs"], *case["recent_runs"]]
            for label, session in case["cli_session_ids"].items():
                lead = next((run.get("lead_id") for run in runs
                             if run.get("session_id") == session), None)
                if lead:
                    host[f"{case['case']}:{label}"] = claude_host_trace(home, session, lead)
    resume_status_file = root / "live-update" / "logs" / "a.resume.status.json"
    resume_status = json.loads(resume_status_file.read_text()) if resume_status_file.is_file() else None
    phase_file = root / "live-update" / "logs" / "a.resume.phase90.json"
    resume_phase = json.loads(phase_file.read_text()) if phase_file.is_file() else None
    gate_timeouts = {}
    for snapshot in root.glob("*/gate_timeout.json"):
        try:
            gate_timeouts[snapshot.parent.name] = json.loads(snapshot.read_text())
        except (OSError, ValueError):
            gate_timeouts[snapshot.parent.name] = {"snapshot_unreadable": True}
    spawn_probes = {}
    for path in root.glob("*/spawn_probe.json"):
        try:
            spawn_probes[path.parent.name] = json.loads(path.read_text())
        except (OSError, ValueError):
            spawn_probes[path.parent.name] = {"snapshot_unreadable": True}
    return {"provider": provider, "cases": cases, "native_host_trace": host,
            "native_resume_status": resume_status,
            "native_resume_phase90": resume_phase,
            "gate_timeouts": gate_timeouts, "native_spawn_probes": spawn_probes,
            "native_hook_capture": codex_hook_capture_summary(root, provider)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--claude-budget-usd", type=float, default=3.0)
    parser.add_argument("--live-update", action="store_true")
    parser.add_argument("--only-live-update", action="store_true",
                        help="run the bounded native upgrade case without baseline cases")
    parser.add_argument("--only-worktrees", action="store_true",
                        help="run only the two independent worktree owners")
    parser.add_argument("--direct-stop-resume", action="store_true",
                        help="probe an exact normal Stop control on Codex live-update resume")
    parser.add_argument("--old-plugin-root", type=Path)
    parser.add_argument("--candidate-plugin-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "plugins" / "symphony")
    args = parser.parse_args()
    if args.only_live_update and not args.live_update:
        parser.error("--only-live-update requires --live-update")
    if args.only_live_update and args.only_worktrees:
        parser.error("--only-live-update and --only-worktrees cannot be combined")
    if args.direct_stop_resume and (args.provider != "codex" or not args.live_update):
        parser.error("--direct-stop-resume requires a Codex live-update run")
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
            cases = (() if args.only_live_update else
                     (True,) if args.only_worktrees else (False, True))
            baseline_env = (prepare_baseline_capture(args.provider, root,
                            args.candidate_plugin_root) if cases else None)
            results = [check_case(args.provider, root, separate, args.timeout,
                                  args.claude_budget_usd,
                                  baseline_env=baseline_env) for separate in cases]
            if args.live_update:
                update = prepare_live_update(args.provider, root, args.old_plugin_root,
                                             args.candidate_plugin_root)
                results.append(
                    check_codex_mixed_live_update(
                        root, args.timeout, args.claude_budget_usd, update,
                        direct_stop_resume=args.direct_stop_resume)
                    if args.provider == "codex" else
                    check_case(args.provider, root, False, args.timeout,
                               args.claude_budget_usd, update,
                               direct_stop_resume=args.direct_stop_resume))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(json.dumps({"provider": args.provider, "error": str(error),
                              "logs": str(root)}), file=sys.stderr)
            diagnostics = failure_state(root, args.provider)
            diagnostics["failure"] = {"type": type(error).__name__, "message": str(error)[:300]}
            diagnostics["observer_snapshot_lock_retries"] = OBSERVER_SNAPSHOT_LOCK_RETRIES
            destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"native-managed-{args.provider}-failure.json").write_text(
                    json.dumps(diagnostics, indent=2), encoding="utf-8")
            print(json.dumps({"provider": args.provider, "failure": diagnostics["failure"],
                              "case_count": len(diagnostics.get("cases", [])),
                              "diagnostics_dir": destination}), file=sys.stderr)
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
    receipt = {"provider": args.provider, "native_managed": results,
               "observer_snapshot_lock_retries": OBSERVER_SNAPSHOT_LOCK_RETRIES}
    destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"native-managed-{args.provider}-receipt.json").write_text(
            json.dumps(receipt, indent=2), encoding="utf-8")
    # Full hook timelines belong in the artifact. A large single stdout write
    # can fail on CI's nonblocking pipe after every native check has passed.
    print(json.dumps({"provider": args.provider, "native_managed": [
        {key: value for key, value in result.items()
         if key not in ("native_hook_capture", "native_host_trace")}
        for result in results]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
