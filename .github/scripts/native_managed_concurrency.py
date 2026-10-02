#!/usr/bin/env python3
"""Exercise concurrent Symphony runs through installed native Codex or Claude CLIs.

Run after the CI job has installed and authenticated the selected provider's
plugin. All Git checkouts, state, runtime pins, and gate files are temporary.
"""

import argparse
import ast
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
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
def native_metadata_probe(payload):
    facts = dict.fromkeys(("transcript_readable", "own_header_present", "own_header_identity_matches",
                          "native_parent_matches_root", "callback_turn_context_present",
                          "native_model_present", "native_effort_present",
                          "assessor_model_matches_fixture", "assessor_effort_matches_fixture",
                          "declared_assessed_lead_matches"), False)
    facts["native_task_role"] = "unknown"
    transcript = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not isinstance(transcript, str):
        return facts
    try:
        rows, size = [], 0
        with pathlib.Path(transcript).open("rb") as stream:
            for _ in range(64):
                line = stream.readline(65537)
                if not line:
                    break
                size += len(line)
                if len(line) > 65536 or size > 1024 * 1024 or not line.endswith(b"\\n"):
                    return facts
                row = json.loads(line.decode("utf-8"))
                if isinstance(row, dict):
                    rows.append(row)
        facts["transcript_readable"] = True
        header = next((row.get("payload", {}) for row in rows if row.get("type") == "session_meta"), {})
        facts["own_header_present"] = bool(header)
        facts["own_header_identity_matches"] = bool(header.get("id") and header.get("id") == payload.get("agent_id"))
        if not facts["own_header_identity_matches"]:
            return facts
        spawn = header
        for key in ("source", "subagent", "thread_spawn"):
            spawn = spawn.get(key, {}) if isinstance(spawn, dict) else {}
        facts["native_parent_matches_root"] = bool(isinstance(spawn, dict) and
            spawn.get("parent_thread_id") == payload.get("session_id"))
        match = re.match(r"symphony_(assessor|lead|worker|consultant)_", str(header.get("agent_path") or "").rsplit("/", 1)[-1])
        facts["native_task_role"] = match.group(1) if match else "unknown"
        facts["declared_assessed_lead_matches"] = bool(os.environ.get("SYMPHONY_NATIVE_GATE_LEAD_NAME") and
            str(header.get("agent_path") or "").rsplit("/", 1)[-1] == os.environ["SYMPHONY_NATIVE_GATE_LEAD_NAME"])
        turns = [row.get("payload", {}) for row in rows if row.get("type") == "turn_context"
                 and isinstance(row.get("payload"), dict) and payload.get("turn_id")
                 and row["payload"].get("turn_id") == payload["turn_id"]]
        facts["callback_turn_context_present"] = len(turns) == 1
        if len(turns) == 1:
            turn = turns[0]
            facts["native_model_present"] = isinstance(turn.get("model"), str) and bool(turn["model"])
            facts["native_effort_present"] = isinstance(turn.get("effort"), str) and bool(turn["effort"])
            facts["assessor_model_matches_fixture"] = bool(os.environ.get("SYMPHONY_NATIVE_ASSESSOR_MODEL") and
                turn.get("model") == os.environ["SYMPHONY_NATIVE_ASSESSOR_MODEL"])
            facts["assessor_effort_matches_fixture"] = bool(os.environ.get("SYMPHONY_NATIVE_ASSESSOR_EFFORT") and
                turn.get("effort") == os.environ["SYMPHONY_NATIVE_ASSESSOR_EFFORT"])
    except (OSError, ValueError, UnicodeError, TypeError, AttributeError):
        pass
    return facts

def admission_probe(payload):
    facts = dict.fromkeys(("state_readable", "owner_run_present", "child_tracked", "child_start_recorded",
                          "accepted_assessed_lead_route", "canonical_lead_consistent"), False)
    facts["tracked_role"] = "unknown"
    try:
        key = hashlib.sha256(os.path.normcase(str(pathlib.Path(payload["cwd"]).resolve())).encode()).hexdigest()
        path = pathlib.Path(os.environ["SYMPHONY_STATE_DIR"]) / (key + ".v2.json")
        document = json.loads(path.read_text(encoding="utf-8"))
        facts["state_readable"] = True
        run = document.get("active_runs", {}).get(provider + ":" + payload["session_id"])
        facts["owner_run_present"] = isinstance(run, dict)
        if isinstance(run, dict):
            assessment = run.get("assessment") or {}
            route = assessment.get("route") or {}
            expected = "symphony:symphony-lead-" + str(route.get("lead_model") or "") + "-" + str(route.get("lead_effort") or "")
            facts["accepted_assessed_lead_route"] = bool(provider == "claude" and route
                and assessment.get("_fast_pending") is not True
                and payload.get("agent_type") == expected)
            facts["canonical_lead_consistent"] = run.get("lead_identity") in {None, "", payload.get("agent_id")}
            child = next((item for item in run.get("delegations", [])
                          if item.get("identity") == payload.get("agent_id")), None)
            facts["child_tracked"] = child is not None
            role = (child or {}).get("role")
            facts["tracked_role"] = role if role in {"assessor", "lead", "worker", "consultant"} else "unknown"
        facts["child_start_recorded"] = any(event.get("kind") in {"delegation_updated", "lead_started"}
            and event.get("payload", {}).get("identity") == payload.get("agent_id")
            and (event.get("kind") == "lead_started" or event.get("payload", {}).get("state") == "working")
            for event in document.get("event_history", []))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return facts

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
if provider == "codex" and event in {"SubagentStart", "SubagentStop"}:
    record["native_metadata"] = native_metadata_probe(payload)
    # Observer snapshots only: callback clock ordering can precede the
    # product hook. These facts never authorize registration or gate release.
    record["admission_at_capture"] = admission_probe(payload)
if provider == "claude" and event in {"SubagentStart", "SubagentStop"}:
    record["admission_at_capture"] = admission_probe(payload)
if event == "SubagentStop":
    message = payload.get("last_assistant_message")
    markers = re.findall(r'^SYMPHONY_OUTCOME:\\s*(\\{[^\\n]*\\})', message, re.MULTILINE) if isinstance(message, str) else []
    try:
        outcome = json.loads(markers[-1]).get("status") if markers else None
    except (ValueError, AttributeError):
        outcome = None
    record["reported_outcome"] = outcome if isinstance(outcome, str) and outcome in {
        "completed", "blocked", "failed", "abandoned"} else "missing"
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
if gate_dir and gate_label in {"a", "b"} and is_lead_start:
    # Resume callbacks remain in the observer timeline, but only the first
    # root-bound packaged lead Start can define the overlap barrier.
    identity = payload.get("agent_id")
    parent = payload.get("parent_thread_id")
    if (not isinstance(identity, str) or not identity or identity == payload.get("session_id")
            or parent not in {None, "", payload.get("session_id")}
            or record["admission_at_capture"]["accepted_assessed_lead_route"] is not True
            or record["admission_at_capture"]["canonical_lead_consistent"] is not True):
        is_lead_start = False
    else:
        try:
            with (gate_dir / (gate_label + ".lead.json")).open("x") as stream:
                json.dump({"session_id": payload.get("session_id"), "agent_id": identity}, stream)
        except FileExistsError:
            frozen = json.loads((gate_dir / (gate_label + ".lead.json")).read_text())
            if frozen.get("session_id") != payload.get("session_id"):
                raise SystemExit("native gate root session changed")
            is_lead_start = False
if (gate_dir and gate_label in {"a", "b"} and event == "SubagentStart"
        and provider == "codex" and agent_type == "default"):
    # Codex 0.159 reports both assessor and lead as agent_type=default.
    # A valid fast attempt may precede the assessor. Gate only the declared
    # assessed task's own native header; ordinal/default labels are ambiguous.
    gate_dir.mkdir(parents=True, exist_ok=True)
    first = gate_dir / (gate_label + ".first.json")
    facts = record["native_metadata"]
    own = facts["own_header_identity_matches"] and facts["native_parent_matches_root"]
    if own and facts["native_task_role"] == "assessor":
        try:
            with first.open("x") as stream:
                json.dump({"session_id": payload.get("session_id"), "agent_id": payload.get("agent_id")}, stream)
        except FileExistsError:
            original = json.loads(first.read_text())
            if original.get("session_id") != payload.get("session_id"):
                raise SystemExit("native gate root session changed")
    if own and facts["declared_assessed_lead_matches"]:
        try:
            with (gate_dir / (gate_label + ".lead.json")).open("x") as stream:
                json.dump({"session_id": payload.get("session_id"), "agent_id": payload.get("agent_id")}, stream)
            is_lead_start = True
        except FileExistsError:
            frozen = json.loads((gate_dir / (gate_label + ".lead.json")).read_text())
            if frozen.get("session_id") != payload.get("session_id"):
                raise SystemExit("native gate root session changed")
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
    status_path = os.environ.get("SYMPHONY_NATIVE_TASK_STATUS_FILE")
    expected_status = os.environ.get("SYMPHONY_NATIVE_TASK_STATUS_WAIT")
    if provider == "codex" and status_path and expected_status:
        try:
            record["native_task_wait"] = pathlib.Path(status_path).read_text() == expected_status
        except OSError:
            record["native_task_wait"] = False
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
        try:
            with (gate_dir / "a.stop-claim").open("x"):
                pass
            record["native_stop_hold_label"] = "a"
        except FileExistsError:
            pass
(destination / (invocation + "-entry.json")).write_text(json.dumps(record))
if record.get("native_stop_hold_label"):
    temporary = gate_dir / ("a.stop-" + invocation + ".tmp")
    temporary.write_text(json.dumps({"session_id": payload.get("session_id"),
                                     "invocation_id": invocation,
                                     "started_ns": record["started_ns"]}))
    temporary.replace(gate_dir / "a.stop-ready.json")
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


def codex_fixture_roles(profile_id="base"):
    profiles = json.loads((Path(__file__).resolve().parents[2] / "plugins" / "symphony" /
                           "profiles.json").read_text(encoding="utf-8"))
    profile = next(item for item in profiles["providers"]["codex"]["profiles"]
                   if item["id"] == profile_id)
    route = "small/simple" if profile_id == "base" else "small/complex"
    lead = profile["matrix"][route]
    assessor = {"model": profile["tiers"]["strongest"], "effort": "high"}
    for role in (assessor, lead):
        if role["effort"] not in profile["efforts"][role["model"]]:
            raise RuntimeError("native fixture route is unsupported by the packaged profile")
    def with_name(role_name, route):
        model_slug = re.sub(r"[^a-z0-9]+", "_", route["model"]).strip("_")
        return {**route, "task_name": f"symphony_{role_name}_{model_slug}_{route['effort']}"}
    return with_name("assessor", assessor), with_name("lead", lead)


def prompt(provider, label, recover, project, *, defer_recovery=False,
           codex_profile="base", released_direct_version=None):
    if released_direct_version not in {None, '1.5.1', '1.6.0'}:
        raise ValueError('direct original fixture requires a reviewed released baseline')
    assessor_role, lead_role = codex_fixture_roles(codex_profile) if provider == "codex" else ({}, {})
    control = "$symphony:symphony start" if provider == "codex" else "/symphony:start"
    route = ('{"size":"small","complexity":"complex","risk":"normal",'
             '"rationale":"disposable native CI upgrade gate","topology":"delegated"}'
             if provider == "codex" and codex_profile != "base" else
             '{"size":"small","complexity":"simple","risk":"normal",'
             '"rationale":"disposable native CI gate","topology":"delegated"}')
    if released_direct_version:
        route = route.replace('"topology":"delegated"', '"topology":"direct"')
    profiles = json.loads((Path(__file__).resolve().parents[2] / 'plugins/symphony/profiles.json').read_text(encoding='utf-8'))
    profile_id = codex_profile if provider == 'codex' else 'sonnet-5-5'
    profile = next(item for item in profiles['providers'][provider]['profiles'] if item['id'] == profile_id)
    worker = profile['matrix']['small/simple']
    worker_text = 'SYMPHONY_ROLE: worker\n' + json.dumps({
        'objective': 'Return the literal callback report GATE_RELEASED.',
        'ownership': 'Only this bounded literal report under the owning lead.',
        'evidence': 'The owning lead has already returned from its native start hook.',
        'constraints': 'No applicable capability phase. Do not inspect files, execute shell commands, edit files, create worktrees or delegate.',
        'acceptance_check': 'The native worker finishes successfully with exactly GATE_RELEASED.',
        'return_contract': 'Return exactly GATE_RELEASED without Markdown.',
        'size': 'small', 'complexity': 'simple'})
    if provider == 'codex':
        worker_packet = {'task_name': 'symphony_worker_' + re.sub(r'\W', '_', worker['model']) + '_' + worker['effort'],
                         'model': worker['model'], 'reasoning_effort': worker['effort'], 'fork_turns': 'none',
                         'message': worker_text}
    else:
        worker_packet = {'subagent_type': f"symphony:symphony-worker-{worker['model']}-{worker['effort']}",
                         'run_in_background': False, 'prompt': worker_text}
    review_needed = provider == 'codex' and codex_profile != 'base' and not released_direct_version
    review_packet = ({'task_name': lead_role['task_name'].replace('symphony_lead_', 'symphony_consultant_', 1) + '_review',
                      'model': lead_role['model'], 'reasoning_effort': lead_role['effort'], 'fork_turns': 'none',
                      'message': 'SYMPHONY_ROLE: consultant\nSYMPHONY_DECISION: {"size":"small","complexity":"simple"}\n' + json.dumps({
                          'objective': 'Independently check the prior literal GATE_RELEASED worker result.',
                          'ownership': 'Review only; make no changes.',
                          'evidence': 'The lead verified the first worker returned exactly GATE_RELEASED.',
                          'constraints': 'Do not inspect files, execute commands, edit, or delegate.',
                          'acceptance_check': 'The supplied literal report is exactly GATE_RELEASED.',
                          'return_contract': 'Return SYMPHONY_DECISION: {"size":"small","complexity":"simple"} and SYMPHONY_REVIEW: passed on separate lines if the check passes.',
                          'size': 'small', 'complexity': 'simple'})} if review_needed else None)
    review_contract = (
        'This small/complex route also requires an independent check. After the substantive worker returns, '
        'spawn REVIEW_SPAWN_PACKET verbatim as a separate consultant; await its classified native '
        'SYMPHONY_REVIEW: passed report before completing. Preserve both child results on followup. '
        'REVIEW_SPAWN_PACKET: ' + json.dumps(review_packet) + '\n' if review_needed else '')
    worker_contract = (
        'After your native start hook releases, YOU as the canonical lead must spawn exactly one bounded '
        'worker using WORKER_SPAWN_PACKET verbatim as native spawn arguments; the root never substitutes '
        'for this launch. Await its successful native result and verify its literal GATE_RELEASED report. '
        'Relay its return_contract unchanged: the worker final report, or SubagentHandback message if '
        'background, is exactly GATE_RELEASED, with no outcome marker, preface, Markdown or explanation. '
        'This literal callback report performs no capability phase. '
        + review_contract +
        'Then return your own specified GATE_RELEASED/outcome lines. Use only the exact requested '
        'completed or blocked enum; never substitute successful. Do not search for a callback command '
        'or gate file. Preserve the successful worker result for same-task followup; do not spawn duplicate '
        'work on routine completion reconciliation. WORKER_SPAWN_PACKET: ' + json.dumps(worker_packet) + '\n')
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
        ("FIRST-TURN CONTRACT: The test-only native hook holds the unfinished task while "
         "NATIVE_STATUS.txt says WAIT. Do not edit that file. On the first turn "
         "report exactly GATE_RELEASED and SYMPHONY_OUTCOME: {\"status\":\"blocked\"}, each "
         "on its own line. On a later same-agent followup, read the file again: only READY permits "
         "SYMPHONY_OUTCOME: {\"status\":\"completed\"}. Do not create a worktree."
         if provider == "codex" and recover and defer_recovery else
         "FIRST-TURN CONTRACT: There is no gate command or file to find or run. "
         "After your native start hook releases" + ("" if released_direct_version else " and your worker report is verified") + ", reply with "
         "exactly these two literal lines (no Markdown):\nGATE_RELEASED\n"
         + ('SYMPHONY_OUTCOME: {"status":"blocked"}' if recover else
            'SYMPHONY_OUTCOME: {"status":"completed"}') + "\n"
         + ("This recovery fixture is deliberately blocked even after the native hook releases; "
            "the root will resume this same lead for completion.\n" if recover else "") +
         "The test-only native SubagentStart hook may briefly hold your first turn. "
         "GATE_RELEASED is a report line, not an operation. "
         "Do not inspect files, execute shell commands, edit files or create a worktree.")
    )
    lead_task += ('\nThis original released-runtime task is a bounded direct callback report. '
                  'The original lead owns its execution; no worker report is required. '
                  'Preserve this same task and identity on completion followup.'
                  if released_direct_version else '\n' + worker_contract)
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
           *, defer_recovery=False, released_direct_version=None):
    executable = shutil.which(provider)
    if not executable:
        raise RuntimeError(f"{provider} CLI is missing")
    output = log_dir / f"{label}.output"
    errors = log_dir / f"{label}.errors"
    if provider == "codex":
        codex_profile = env.get("SYMPHONY_PROFILE", "base")
        _, lead_role = codex_fixture_roles(codex_profile)
        env = {**env, 'SYMPHONY_NATIVE_GATE_LEAD_NAME': lead_role['task_name']}
        command = [executable, "exec", "--dangerously-bypass-hook-trust",
                   "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                   "--model", lead_role["model"], "-C", str(project),
                   "--output-last-message", str(output),
                   prompt(provider, label, recover, project,
                          defer_recovery=defer_recovery,
                          codex_profile=codex_profile, released_direct_version=released_direct_version)]
    else:
        command = [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
                   "--permission-mode", "bypassPermissions", "--session-id", session,
                   "--output-format", "json",
                   prompt(provider, label, recover, project, released_direct_version=released_direct_version)]
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


def claude_original_owner_archived(document, session, run_id, lead_id):
    """Completion of another run is not a reason to skip the original wake."""
    if any(run.get('session_id') == session for run in document.get('active_runs', {}).values()):
        return False
    matches = [run for run in document.get('recent_runs', ())
               if run.get('provider') == 'claude' and run.get('session_id') == session
               and run.get('run_id') == run_id]
    return bool(len(matches) == 1 and matches[0].get('status') == 'completed'
        and matches[0].get('lead_identity') == lead_id
        and (matches[0].get('outcome') or {}).get('status') == 'completed'
        and not matches[0].get('unreconciled')
        and all(item.get('state', '').lower() not in {'working', 'pending', 'interrupted'}
                for item in matches[0].get('delegations', ())))


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
                 direct_stop=False, *, lead_id, lead_task_name,
                 native_status_nonce=None):
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
        "that SAME lead. "
        + (f"Ask it to read NATIVE_STATUS.txt in this project now. Only the exact "
           f"line READY {native_status_nonce} permits its completed outcome; if the file "
           "still says WAIT, report blocked. Never edit the file or invent evidence. "
           if native_status_nonce else
           "Ask it to report only evidence from its existing command result with its "
           "outcome; never invent missing evidence. ")
        + "After that same-ID result, "
        "finish this root turn immediately so native Stop checks and reconciles it, even if "
        "the returned text lacks a marker. Do not spawn a replacement "
        "or another assessor, even if the old lead is absent from list_agents. If the "
        "host cannot resume that lead or its evidence is unavailable, report the "
        "limitation and end the turn without changing ownership. Do not rerun the gate "
        "or force stop. Once reconciled, finish the root turn so the native Stop hook "
        "can finalize the original run; do not print stop/status controls as prose "
        "or poll a completing run before ending the turn.")
    command = [executable, "exec", "resume", "--dangerously-bypass-hook-trust",
               "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
               "--model", codex_fixture_roles(env.get("SYMPHONY_PROFILE", "base"))[1]["model"],
               session, prompt]
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


def require_live_update_versions(old_version, candidate_version):
    """Require a newer candidate; CI uses 1.6.0 and explicit legacy probes may use 1.5.1."""
    if not isinstance(old_version, str) or old_version not in {"1.5.1", "1.6.0"}:
        raise RuntimeError("live update requires an actual released 1.5.1 or 1.6.0 package")
    if (not isinstance(candidate_version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", candidate_version)
            or tuple(map(int, candidate_version.split(".")))
               <= tuple(map(int, old_version.split(".")))):
        raise RuntimeError(f"live update requires a candidate genuinely newer than {old_version}")


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


def install_hook_capture(provider, root, home, *, private_children=False, candidate_source=None):
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
    if provider == 'claude' and private_children:
        # Diagnostic-only snapshots preserve hook-time handback normalization.
        # Product hook commands and callback admission remain untouched.
        private = root / 'private-child-hooks'
        private.mkdir(mode=0o700, exist_ok=True)
        candidate = (Path(candidate_source).resolve() if candidate_source is not None else
                     Path(__file__).resolve().parents[2] / 'plugins' / 'symphony')
        anchor = 'provider = sys.argv[3]\n'
        extra = (
            "if provider == 'claude' and event in {'SubagentStart', 'SubagentStop'}:\n"
            "    private_capture = {'raw': payload, 'canonical': None}\n"
            "    try:\n"
            f"        sys.path.insert(0, {str(candidate)!r})\n"
            "        from symphony.adapters import event_from_payload\n"
            "        private_event = event_from_payload('claude', payload)\n"
            "        private_capture['canonical'] = {'event_id': private_event.event_id, 'kind': private_event.kind, 'payload': private_event.payload}\n"
            "    except (OSError, ValueError, TypeError, AttributeError, ImportError):\n"
            "        pass\n"
            f"    private_file = pathlib.Path({str(private)!r}) / (invocation + '.json')\n"
            "    descriptor = os.open(private_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
            "    with os.fdopen(descriptor, 'w', encoding='utf-8') as private_stream:\n"
            "        json.dump(private_capture, private_stream)\n")
        script.write_text(CODEX_HOOK_CAPTURE.replace(anchor, anchor + extra), encoding='utf-8')


def prepare_baseline_capture(provider, root, candidate_source, *, capture_child_sources=False):
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
    install_hook_capture(provider, root, home, private_children=capture_child_sources,
                         candidate_source=candidate_source)
    return env


def prepare_live_update(provider, root, old_source, candidate_source):
    old_source, candidate_source = old_source.resolve(), candidate_source.resolve()
    old_version, candidate_version = package_version(old_source), package_version(candidate_source)
    require_live_update_versions(old_version, candidate_version)
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
    install_hook_capture(provider, root, home, private_children=True,
                         candidate_source=candidate_source)
    old_cache = home / "plugins" / "cache" / "symphony-old" / "symphony" / old_version
    hook = "codex.json" if provider == "codex" else "hooks.json"
    if not (old_cache / "hooks" / hook).is_file():
        raise RuntimeError(f"old {old_version} package was not installed in the disposable {provider} home")
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
            raise RuntimeError(f"{label}: active native session is not using installed {update['old_version']}")
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
    """Require every old turn's terminal; candidate may finish a blocked first turn."""
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
    if (not lead_turns or lead_turns[0].get("reported_outcome") != "blocked"
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


def held_codex_stop_capture(root, session, invocation, deadline):
    """Wait for the exact native Stop capture after its readiness is published."""
    while time.monotonic() < deadline:
        matches = [item for item in codex_hook_capture_summary(root)["records"]
                   if item.get("event") == "Stop"
                   and item.get("session_id") == session
                   and item.get("invocation_id") == invocation
                   and item.get("native_stop_hold_label") == "a"]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            break
        time.sleep(.05)
    raise RuntimeError("held old Stop capture did not publish its exact invocation")


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
                                         native_trace, removed_ns, *, require_native_span=True):
    """Prove durable completion, and report whether the native turn crossed removal."""
    if provider != "claude" and not require_native_span:
        raise RuntimeError("native span relaxation is Claude-only")
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
        original_terminals = [turn for turn in turns
                              if turn.get("marker") == "completed"
                              and turn.get("last_stop_reason") == "end_turn"
                              and timestamp_ns(turn.get("child_prompt_at")) < removed_ns
                              and timestamp_ns(turn.get("last_assistant_at"))
                              >= timestamp_ns(turn.get("child_prompt_at"))]
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
    if ((require_native_span and (not straddled or not completed_after))
            or (not require_native_span and provider == "claude"
                and not (original_terminals or (straddled and completed_after)))):
        raise RuntimeError("original native lead turn did not span and complete after old plugin removal")
    return {"session_id": session, "lead_id": lead_id,
            "durable_completion_after_removal": True,
            "native_turn_spanned_removal": bool(straddled and completed_after)}


def require_any_native_lead_span(evidence):
    if not any(item["native_turn_spanned_removal"] for item in evidence.values()):
        raise RuntimeError("no original native lead turn spanned old plugin removal")


def codex_host_trace(home, session, lead_id, error_log, *, strict=False,
                     codex_profile="base"):
    """Extract only tool and turn metadata from disposable Codex JSONL."""
    _, lead_role = codex_fixture_roles(codex_profile)
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
    child_tools = {}
    child_tool_count = 0
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
        elif record.get("type") == "response_item" and payload.get("type") == "function_call":
            call_id = payload.get("call_id")
            child_tool_count += 1
            if call_id and len(child_tools) < 24:
                arguments = str(payload.get("arguments") or "")
                name = payload.get("name")
                child_tools[call_id] = {
                    "name": name if isinstance(name, str) and
                    re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) else None,
                    "call_hash": fingerprint(call_id),
                    "called_at": record.get("timestamp"),
                    "status_file_mentioned": "NATIVE_STATUS.txt" in arguments,
                    "arguments_hash": fingerprint(arguments),
                    "return_recorded": False,
                }
        elif (record.get("type") == "response_item"
              and payload.get("type") == "function_call_output"):
            tool = child_tools.get(payload.get("call_id"))
            if tool is not None:
                output = str(payload.get("output") or "")
                tool["return_recorded"] = True
                tool["returned_at"] = record.get("timestamp")
                tool["wait_token_seen"] = bool(re.search(r"\bWAIT [0-9a-f]{32}\b", output))
                tool["ready_token_seen"] = bool(re.search(r"\bREADY [0-9a-f]{32}\b", output))
                tool["output_hash"] = fingerprint(output)
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
            "lead_tool_calls": list(child_tools.values()),
            "lead_tool_call_count": child_tool_count,
            "lead_tool_calls_truncated": child_tool_count > len(child_tools),
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
    if provider == 'codex':
        assessor, _ = codex_fixture_roles(env['SYMPHONY_PROFILE'])
        env['SYMPHONY_NATIVE_ASSESSOR_MODEL'] = assessor['model']
        env['SYMPHONY_NATIVE_ASSESSOR_EFFORT'] = assessor['effort']
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
                                      budget, sessions[label], released_direct_version=update['old_version'] if update else None)
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
                    docs = {label: read_state_snapshot(path) for label, path in paths.items()}
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
                    document = read_state_snapshot(path) if path.is_file() else {}
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
                if (host.get('lead_parent_matches_root') is not True
                        or not any(item["same_lead_id"] and item["lead_packet_metadata_matches"]
                           for item in host["spawn_calls"])):
                    raise RuntimeError(f"{label}: native original lead spawn lacks required transport metadata")
                require_codex_gate_lead(codex_hook_capture_summary(root)["records"],
                                        label, sessions[label], observed_leads[label])
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
            snapshot["post_update"] = {label: event_counts(read_state_snapshot(paths[label]))
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
                before_resume = read_state_snapshot(paths["a"])
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
                snapshot["post_candidate_resume"] = event_counts(read_state_snapshot(paths["a"]))
                snapshot_file.write_text(json.dumps(snapshot))
        else:
            resumed = set()
            finalized = set()
            while time.monotonic() < deadline:
                if any(process.poll() not in (None, 0) for process in processes.values()):
                    raise RuntimeError("native Claude CLI exited unsuccessfully")
                current = {label: read_state_snapshot(path) for label, path in paths.items()}
                for label, process in processes.items():
                    if not update and claude_original_owner_archived(
                            current[label], sessions[label], observed_run_ids[label], observed_leads[label]):
                        # A successful archive needs no extra SendMessage wake.
                        # Explicit continuation is exercised by its own case.
                        continue
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
                records_clear = all(not (record.get('pending') or record.get('overflow'))
                                    for record in (read_state_snapshot(path)
                                                   for path in state_dir.glob('.session-*.json')))
                if ((len(resumed) == len(processes) if update else True)
                        and all(process.poll() == 0 for process in processes.values())
                        and records_clear
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
                      {label: read_state_snapshot(path) for label, path in paths.items()})
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
                require_released_direct_fixture(completed_run, update['old_version'])
            else:
                require_literal_worker(provider, completed_run, Path(env['CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR']),
                                       first if label == 'a' else second, codex_hook_capture_summary(root, provider)['records'])
            if update:
                native_trace = (codex_host_trace(update["home"], sessions[label],
                                                 observed_leads[label], logs / f"{label}.errors",
                                                 strict=True)
                                if provider == "codex" else
                                claude_host_trace(update["home"], sessions[label],
                                                  observed_leads[label]))
                post_removal_evidence[label] = require_post_removal_lead_completion(
                    doc, provider, sessions[label], observed_leads[label], native_trace,
                    update["old_source_removed_ns"],
                    require_native_span=provider != "claude")
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
        if update and provider == "claude":
            # Claude's Start hook must release before the managed lead can
            # register. A short original turn may finish during uninstall;
            # both durable completions must follow removal, while at least one
            # native turn must prove work crossed that boundary.
            require_any_native_lead_span(post_removal_evidence)
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


def old_stop_hold_conditions(host, old_cli_alive, old_version):
    """Publish each existing hold predicate without changing its acceptance."""
    turns = host['lead_turns']
    return {'native_lead_turn_count': len(turns),
            'first_native_turn_completed': bool(turns and turns[0].get('completed')),
            'first_native_outcome_blocked': bool(turns and turns[0].get('reported_outcome') == 'blocked'),
            'native_followup_count': len(host['followup_calls']),
            'native_root_spawn_count': len(host['spawn_calls']),
            'native_root_has_two_spawns': len(host['spawn_calls']) == 2,
            'old_cli_alive': old_cli_alive, 'old_version': old_version}


def ensure_native_gate_release(path):
    """Cleanup may unblock a missing gate, but must preserve a prior release boundary."""
    try:
        path.touch(exist_ok=False)
    except FileExistsError:
        pass


def require_old_stop_hold(host, alive, old_version, run, home, project, canonical):
    """A held old turn may include a separately proven original fast escalation."""
    from native_routing_smoke import native_rows, worker_transcript_is_unforked
    turns = host['lead_turns']
    if (len(turns) != 1 or not turns[0].get('completed')
            or turns[0].get('reported_outcome') != 'blocked'
            or host['followup_calls'] or not alive):
        raise RuntimeError('old root advanced beyond its blocked turn during Stop hold')
    require_assessed_lead_set(run, home, project, canonical,
                              released_old_version=old_version)
    leads = [child for child in run.get('delegations', []) if child.get('role') == 'lead']
    assessors = [child for child in run.get('delegations', []) if child.get('role') == 'assessor']
    if len(assessors) != 1 or assessors[0].get('state') != 'completed':
        raise RuntimeError('old hold lacks its one completed original assessor')
    expected = [*leads, assessors[0]]
    root_rows = native_rows('codex', home, run['session_id'])
    if not worker_transcript_is_unforked('codex', root_rows, run['session_id']):
        raise RuntimeError('old hold root transcript has ambiguous ownership')
    calls = [row for row in root_rows if row.get('payload', {}).get('type') == 'function_call'
             and row['payload'].get('name') == 'spawn_agent']
    if len(calls) != len(expected) or len(host['spawn_calls']) != len(expected):
        raise RuntimeError('old hold root has extra or missing native launches')
    remaining = {child['identity']: child for child in expected}
    if len(remaining) != len(expected):
        raise RuntimeError('old hold has duplicate child identities')
    seen_calls, launches, completions = set(), {}, {}
    root_header = root_rows[0]['payload']
    # Native exec roots omit agent_path; their direct children have /root/<task>.
    root_path = root_header.get('agent_path', '/root')
    if (root_header.get('source') != 'exec' or root_path != '/root'
            or not isinstance(root_header.get('cwd'), str) or not Path(root_header['cwd']).is_absolute()
            or Path(root_header['cwd']).resolve() != project.resolve()):
        raise RuntimeError('old hold root has no exact native exec ownership')
    for row in calls:
        payload = row['payload']
        call_id = payload.get('call_id')
        if not isinstance(call_id, str) or not call_id or call_id in seen_calls:
            raise RuntimeError('old hold has duplicate or missing launch call IDs')
        seen_calls.add(call_id)
        outputs = [item for item in root_rows if item.get('payload', {}).get('call_id') == call_id
                   and item['payload'].get('type') == 'function_call_output']
        if len(outputs) != 1:
            raise RuntimeError('old hold launch has no unique native result')
        try:
            arguments = json.loads(payload['arguments'])
            result = json.loads(outputs[0]['payload']['output'])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError('old hold launch has malformed native call/result') from None
        if not isinstance(arguments, dict) or not isinstance(result, dict) or set(result) != {'task_name'}:
            raise RuntimeError('old hold launch has malformed native call/result')
        matches = []
        for identity, child in remaining.items():
            rows = native_rows('codex', home, identity)
            if not worker_transcript_is_unforked('codex', rows, identity):
                continue
            header = rows[0]['payload']
            role_name = ('symphony_lead_fast' if child['role'] == 'lead' and identity != canonical
                         else 'symphony_' + child['role'])
            name = role_name + '_' + re.sub(r'\W', '_', child['requested_tier']) + '_' + child['requested_effort']
            parent = header.get('source', {}).get('subagent', {}).get('thread_spawn', {})
            if (arguments.get('task_name') == name and arguments.get('fork_turns') == 'none'
                    and arguments.get('model') == child['requested_tier']
                    and arguments.get('reasoning_effort') == child['requested_effort']
                    and result.get('task_name') == header.get('agent_path') == root_path + '/' + name
                    and parent.get('parent_thread_id') == run['session_id']
                    and isinstance(header.get('cwd'), str) and Path(header['cwd']).is_absolute()
                    and Path(header['cwd']).resolve() == project.resolve()):
                matches.append((identity, rows))
        if len(matches) != 1:
            raise RuntimeError('old hold launch/result does not bind one exact original child')
        identity, rows = matches[0]
        child = remaining.pop(identity)
        starts = [item for item in rows if item.get('payload', {}).get('type') == 'task_started']
        ends = [item for item in rows if item.get('payload', {}).get('type') == 'task_complete']
        contexts = [item['payload'] for item in rows if item.get('type') == 'turn_context']
        failed = any(item.get('payload', {}).get('type') in
                     {'task_failed', 'turn_aborted', 'task_interrupted', 'error'} for item in rows)
        if len(starts) != 1 or len(ends) != 1 or len(contexts) != 1 or failed:
            raise RuntimeError('old hold child has no unique successful native turn')
        turn = starts[0]['payload'].get('turn_id')
        if (not isinstance(turn, str) or not turn or ends[0]['payload'].get('turn_id') != turn
                or contexts[0].get('turn_id') != turn
                or contexts[0].get('model') != child['requested_tier']
                or contexts[0].get('effort') != child['requested_effort']):
            raise RuntimeError('old hold child has conflicting native turn/route')
        try:
            launch = timestamp_ns(row.get('timestamp'))
            result_at = timestamp_ns(outputs[0].get('timestamp'))
            start = timestamp_ns(starts[0].get('timestamp'))
            end = timestamp_ns(ends[0].get('timestamp'))
        except (ValueError, TypeError):
            raise RuntimeError('old hold child has invalid native chronology') from None
        if not launch <= result_at <= end or not launch <= start <= end:
            raise RuntimeError('old hold child has conflicting native chronology')
        launches[identity], completions[identity] = launch, end
    assessor = assessors[0]['identity']
    fast = [child['identity'] for child in leads if child['identity'] != canonical]
    if (remaining or not completions[assessor] < launches[canonical]
            or fast and not completions[fast[0]] < launches[assessor]):
        raise RuntimeError('old hold launch order is not fast, assessor, assessed lead')


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
    native_status = first / "NATIVE_STATUS.txt"
    status_nonce = uuid.uuid4().hex
    native_status.write_text(f"WAIT {status_nonce}\n")
    gate_dir = case / "native-gate"
    gate_dir.mkdir()
    state_dir = case / "state"
    state_dir.mkdir()
    logs = case / "logs"
    logs.mkdir()
    env = {**os.environ, **update["env"], "SYMPHONY_STATE_DIR": str(state_dir),
           "SYMPHONY_RUNTIME_DIR": str(case / "retained runtimes"),
           "SYMPHONY_HOOK_DECISIONS_DIR": str(case / "hook decisions"),
           "SYMPHONY_PROFILE": "full"}
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
                                 "SYMPHONY_NATIVE_TASK_STATUS_FILE": str(native_status),
                                 "SYMPHONY_NATIVE_TASK_STATUS_WAIT": f"WAIT {status_nonce}\n",
                                 "SYMPHONY_NATIVE_HOLD_OLD_STOP": "1"}, logs, budget, "",
                                defer_recovery=True, released_direct_version=update['old_version'])
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
            raise RuntimeError(f"old A did not use its guarded {update['old_version']} runtime")
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
                old_turns = codex_host_trace(
                    update["home"], sessions["a"], leads["a"], logs / "a.errors",
                    codex_profile="full"
                ).get("lead_turns", [])
                held = json.loads((gate_dir / "a.stop-ready.json").read_text())
                if held.get("session_id") != sessions["a"] or not held.get("invocation_id"):
                    raise RuntimeError("held old Stop belongs to another native root")
                held_stop = held_codex_stop_capture(
                    root, sessions["a"], held.get("invocation_id"), deadline)
                if (native_status.read_text() != f"WAIT {status_nonce}\n" or not old_turns
                        or old_turns[0].get("reported_outcome") != "blocked"
                        or held_stop.get("native_task_wait") is not True):
                    raise RuntimeError("old original lead did not report its unfinished WAIT task")
                break
            if not old_run:
                old_turns = codex_host_trace(
                    update["home"], sessions["a"], leads["a"], logs / "a.errors",
                    codex_profile="full"
                ).get("lead_turns", [])
                if old_turns and old_turns[0].get("reported_outcome") == "completed":
                    raise RuntimeError("old original lead ignored the unfinished WAIT task before candidate B")
                raise RuntimeError("old original run archived before candidate B launch")
            if time.monotonic() >= deadline:
                raise RuntimeError("old original lead did not become recovering before B launch")
            time.sleep(.1)
        processes["b"] = launch("codex", second, "b", False,
                                {**env, "SYMPHONY_PROFILE": "latest",
                                 "SYMPHONY_NATIVE_GATE_DIR": str(gate_dir),
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
        if native_status.read_text() != f"WAIT {status_nonce}\n":
            raise RuntimeError("old original task status changed before native overlap")
        native_status.write_text(f"READY {status_nonce}\n")
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
            update["home"], sessions["a"], leads["a"], logs / "a.errors",
            strict=True, codex_profile="full")
        old_turns_at_overlap = old_host_at_overlap["lead_turns"]
        snapshot['old_stop_hold_conditions'] = old_stop_hold_conditions(
            old_host_at_overlap, processes['a'].poll() is None, update['old_version'])
        snapshot_file.write_text(json.dumps(snapshot), encoding='utf-8')
        old_run_at_overlap = current.get('active_runs', {}).get(f"codex:{sessions['a']}", {})
        if old_run_at_overlap.get('run_id') != runs['a']:
            raise RuntimeError('old hold original run ownership changed')
        require_old_stop_hold(old_host_at_overlap, processes['a'].poll() is None,
                              update['old_version'], old_run_at_overlap, update['home'], first, leads['a'])
        snapshot['old_stop_hold_conditions']['native_root_launch_set_verified'] = True
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
                                logs / "a.errors", strict=True,
                                codex_profile="full")
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
                     lead_task_name=lead_task_name, native_status_nonce=status_nonce)
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
                                        logs / "a.resume.errors", strict=True,
                                        codex_profile="full")
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
            require_assessed_lead_set(matching[0], update['home'], first, leads[label],
                                      released_old_version=update['old_version'] if label == 'a' else None,
                                      document=document)
            if label == 'a':
                require_released_direct_fixture(matching[0], update['old_version'])
            else:
                require_literal_worker('codex', matching[0], update['home'])
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
                             logs / "a.errors", strict=True, codex_profile="full"),
            update["old_source_removed_ns"])
        for label in ("a", "b"):
            host = codex_host_trace(update["home"], sessions[label], leads[label],
                                    logs / f"{label}.errors", strict=True,
                                    codex_profile="full" if label == "a" else "latest")
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
        ensure_native_gate_release(gate_dir / "release")
        ensure_native_gate_release(gate_dir / "a.stop-release")
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def require_released_direct_fixture(run, old_version):
    """Only the explicitly installed old owner's direct task has no new worker mandate."""
    assessment = run.get('assessment', {})
    route = assessment.get('route', {})
    if (old_version not in {'1.5.1', '1.6.0'} or not isinstance(route, dict)
            or assessment.get('size') != 'small' or route.get('execution') != 'direct'
            or 'substantive_contract' in assessment):
        raise RuntimeError('released original fixture lost its legacy direct contract')


def require_assessed_lead_set(run, home, project, canonical, *, released_old_version=None, document=None):
    """Only one independently identified, completed fast escalation may precede the assessed owner."""
    from native_routing_smoke import native_rows, fast_native_identity, worker_transcript_is_unforked, fast_decision_lines
    leads = [child for child in run.get('delegations', []) if child.get('role') == 'lead']
    extras = [child for child in leads if child.get('identity') != canonical]
    if (run.get('lead_identity') != canonical or sum(child.get('identity') == canonical for child in leads) != 1
            or len(extras) > 1):
        raise RuntimeError('managed run changed its assessed lead set')
    canonical_child = next(child for child in leads if child['identity'] == canonical)
    canonical_rows = native_rows('codex', home, canonical)
    header = canonical_rows[0].get('payload', {}) if canonical_rows and canonical_rows[0].get('type') == 'session_meta' else {}
    expected = 'symphony_lead_' + re.sub(r'\W', '_', canonical_child['requested_tier']) + '_' + canonical_child['requested_effort']
    spawn = header.get('source', {}).get('subagent', {}).get('thread_spawn', {})
    if (header.get('id') != canonical or spawn.get('parent_thread_id') != run.get('session_id')
            or not isinstance(header.get('agent_path'), str) or header['agent_path'].rsplit('/', 1)[-1] != expected
            or not isinstance(header.get('cwd'), str) or not Path(header['cwd']).is_absolute()
            or Path(header['cwd']).resolve() != project.resolve()):
        raise RuntimeError('managed assessed lead has no exact native ownership')
    if not extras:
        return
    child = extras[0]
    rows = native_rows('codex', home, child['identity'])
    completed = [row.get('payload', {}).get('last_agent_message') for row in rows
                 if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_complete']
    starts = [row for row in rows if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_started']
    contexts = [row.get('payload', {}) for row in rows if row.get('type') == 'turn_context']
    terminal_rows = [row for row in rows if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_complete']
    canonical_starts = [row for row in canonical_rows if row.get('type') == 'event_msg'
                        and row.get('payload', {}).get('type') == 'task_started']
    failed = any(row.get('type') == 'event_msg' and row.get('payload', {}).get('type') in
                 {'turn_aborted', 'task_failed', 'task_interrupted', 'error'} for row in rows)
    assessment = run.get('assessment', {})
    contract = assessment.get('substantive_contract')
    route = assessment.get('route')
    scoped_candidate = ('_fast_escalated' not in assessment and isinstance(contract, dict)
        and type(contract.get('version')) is int and contract['version'] == 1
        and isinstance(contract.get('epoch'), str) and bool(contract['epoch'])
        and isinstance(route, dict) and route.get('execution') in {'delegated', 'mixed'})
    escalated = (assessment.get('_fast_escalated') is True
                 or released_old_version == '1.6.0' and '_fast_escalated' not in assessment
                 or scoped_candidate)
    if (child.get('state') != 'completed' or not escalated
            or len(starts) != 1 or failed or not fast_native_identity('codex', rows, child['identity'], run)
            or not worker_transcript_is_unforked('codex', rows, child['identity'])
            or len(completed) != 1 or not isinstance(completed[0], str)
            or fast_decision_lines(completed[0]) != ['escalate']):
        raise RuntimeError('managed run has an unverified extra lead')
    turn = starts[0].get('payload', {}).get('turn_id')
    try:
        chronological = (timestamp_ns(starts[0].get('timestamp')) <= timestamp_ns(terminal_rows[0].get('timestamp'))
                         < min(timestamp_ns(row.get('timestamp')) for row in canonical_starts))
    except (ValueError, TypeError):
        chronological = False
    if (not isinstance(turn, str) or not turn or len(contexts) != 1
            or contexts[0].get('turn_id') != turn or terminal_rows[0].get('payload', {}).get('turn_id') != turn
            or contexts[0].get('model') != child['requested_tier'] or contexts[0].get('effort') != child['requested_effort']
            or not chronological):
        raise RuntimeError('managed fast history lacks exact preceding native turn proof')
    if scoped_candidate:
        token = 'turn_id:' + turn
        receipts = [item for item in (document or {}).get('terminal_receipts', ())
                    if item.get('provider') == 'codex' and item.get('session') == run.get('session_id')
                    and item.get('agent') == child['identity'] and item.get('turn') == token]
        anchors = [item for item in (document or {}).get('event_history', ())
                   if item.get('event_id') == contract['epoch'] + ':assessment:assessment_accepted']
        receipt = receipts[0] if len(receipts) == 1 else {}
        anchor = anchors[0] if len(anchors) == 1 else {}
        result = receipt.get('result')
        if (receipt.get('run_id') != run.get('run_id') or receipt.get('lead') != child['identity']
                or receipt.get('parent') != run.get('session_id')
                or receipt.get('status') not in {'completed', 'done', 'success', 'succeeded'}
                or not isinstance(result, str) or re.fullmatch(r'[0-9a-f]{64}', result) is None
                or result not in assessment.get('_terminal_event_ids', ())
                or token not in assessment.get('_terminal_turns', {}).get(child['identity'], ())
                or anchor.get('kind') != 'assessment_accepted'
                or anchor.get('observed_at') != contract.get('accepted_at')
                or anchor.get('payload', {}).get('route') != route
                or route.get('lead_model') != canonical_child['requested_tier']
                or route.get('lead_effort') != canonical_child['requested_effort']):
            raise RuntimeError('managed candidate fast history lacks exact durable receipt/assessment scope')
        try:
            scoped_order = (timestamp_ns(terminal_rows[0].get('timestamp'))
                < timestamp_ns(contract.get('accepted_at'))
                < min(timestamp_ns(row.get('timestamp')) for row in canonical_starts))
        except (ValueError, TypeError):
            scoped_order = False
        if not scoped_order:
            raise RuntimeError('managed candidate fast history does not precede accepted assessment/owner')


def codex_literal_worker_probe(run, home):
    """Fixed native worker proof components, without exporting report text."""
    from native_routing_smoke import native_rows, tool_evidence, worker_launch_verified, worker_transcript_is_unforked
    workers = [item for item in run.get('delegations', ()) if item.get('role') == 'worker']
    result = {'worker_count': len(workers), 'completed_workers': sum(item.get('state') == 'completed' for item in workers),
              'workers': []}
    for ordinal, worker in enumerate(workers[:12]):
        facts = {'ordinal': ordinal, 'native_reader_available': False}
        try:
            rows = native_rows('codex', home, worker['identity'])
            lead_rows = native_rows('codex', home, run['lead_identity'])
            starts = [row for row in rows if row.get('type') == 'event_msg'
                      and row.get('payload', {}).get('type') == 'task_started']
            contexts = [row for row in rows if row.get('type') == 'turn_context']
            terminals = [row for row in rows if row.get('type') == 'event_msg'
                         and row.get('payload', {}).get('type') == 'task_complete']
            header = rows[0].get('payload', {}) if rows and rows[0].get('type') == 'session_meta' else {}
            parent = header.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
            parent_path = lead_rows[0].get('payload', {}).get('agent_path', '') if lead_rows else ''
            handbacks = [arguments for name, arguments, _, _ in tool_evidence('codex', rows)
                         if name.rsplit('.', 1)[-1] == 'send_message' and isinstance(arguments, dict)
                         and arguments.get('target') == parent_path]
            facts.update(native_reader_available=True,
                successful_parent_handback_count=len(handbacks),
                literal_parent_handback_count=sum(item.get('message') == 'GATE_RELEASED' for item in handbacks),
                worker_unforked=worker_transcript_is_unforked('codex', rows, worker['identity']),
                lead_unforked=worker_transcript_is_unforked('codex', lead_rows, run['lead_identity']),
                native_parent_matches_lead=parent == run['lead_identity'],
                exact_native_launch_verified=worker_launch_verified('codex', tool_evidence('codex', lead_rows),
                    worker, rows, run['lead_identity'], parent_path),
                start_count=len(starts), context_count=len(contexts), complete_count=len(terminals),
                failed_event_count=sum(row.get('type') == 'event_msg' and row.get('payload', {}).get('type') in
                    {'turn_aborted', 'task_failed', 'task_interrupted', 'error'} for row in rows))
            if len(starts) == len(contexts) == len(terminals) == 1:
                token = starts[0]['payload'].get('turn_id')
                message = terminals[0]['payload'].get('last_agent_message')
                context = contexts[0]['payload']
                facts.update(bound_nonempty_turn=isinstance(token, str) and bool(token)
                    and token == context.get('turn_id') == terminals[0]['payload'].get('turn_id'),
                    model_matches=context.get('model') == worker.get('requested_tier'),
                    effort_matches=context.get('effort') == worker.get('requested_effort'),
                    exact_literal_report=message == 'GATE_RELEASED',
                    literal_line_present=isinstance(message, str) and 'GATE_RELEASED' in message.splitlines(),
                    outcome_marker_count=sum(line.strip().startswith('SYMPHONY_OUTCOME:')
                        for line in message.splitlines()) if isinstance(message, str) else 0)
                try:
                    facts['start_before_completion'] = timestamp_ns(starts[0].get('timestamp')) <= timestamp_ns(terminals[0].get('timestamp'))
                except (ValueError, TypeError):
                    facts['start_before_completion'] = False
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
            facts['error_type'] = type(error).__name__
        result['workers'].append(facts)
    return result


def require_literal_worker(provider, run, home, project=None, captures=()):
    """Prove the fixture's one literal child under its canonical lead, including legacy runs."""
    from native_routing_smoke import native_rows, tool_evidence, worker_launch_verified, assistant_text, worker_transcript_is_unforked
    workers = [child for child in run.get('delegations', []) if child.get('role') == 'worker']
    if len(workers) != 1 or workers[0].get('state') != 'completed':
        raise RuntimeError('literal fixture lacks exactly one successful worker')
    worker = workers[0]
    lead_rows = native_rows(provider, home, run['lead_identity'])
    rows = native_rows(provider, home, worker['identity'])
    if (not worker_transcript_is_unforked(provider, rows, worker['identity'])
            or not worker_transcript_is_unforked(provider, lead_rows, run['lead_identity'])):
        raise RuntimeError('literal worker has ambiguous native history')
    parent_path = lead_rows[0].get('payload', {}).get('agent_path', '') if provider == 'codex' else ''
    if not worker_launch_verified(provider, tool_evidence(provider, lead_rows), worker, rows,
                                  run['lead_identity'], parent_path):
        raise RuntimeError('literal worker launch is not bound to the canonical lead')
    if provider == 'codex':
        reports = [row.get('payload', {}).get('last_agent_message') for row in rows
                   if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_complete']
        starts = [row.get('payload', {}) for row in rows if row.get('type') == 'event_msg'
                  and row.get('payload', {}).get('type') == 'task_started']
        contexts = [row.get('payload', {}) for row in rows if row.get('type') == 'turn_context']
        terminals = [row.get('payload', {}) for row in rows if row.get('type') == 'event_msg'
                     and row.get('payload', {}).get('type') == 'task_complete']
        literal = (len(reports) == len(starts) == len(contexts) == 1 and reports[0] == 'GATE_RELEASED'
                   and isinstance(starts[0].get('turn_id'), str) and bool(starts[0]['turn_id'])
                   and starts[0]['turn_id'] == contexts[0].get('turn_id') == terminals[0].get('turn_id')
                   and contexts[0].get('model') == worker['requested_tier']
                   and contexts[0].get('effort') == worker['requested_effort']
                   and not any(row.get('type') == 'event_msg' and row.get('payload', {}).get('type') in
                               {'turn_aborted', 'task_failed', 'task_interrupted', 'error'} for row in rows))
        try:
            started = next(row['timestamp'] for row in rows if row.get('type') == 'event_msg'
                           and row.get('payload', {}).get('type') == 'task_started')
            finished = next(row['timestamp'] for row in rows if row.get('type') == 'event_msg'
                            and row.get('payload', {}).get('type') == 'task_complete')
            literal = literal and timestamp_ns(started) <= timestamp_ns(finished)
        except (KeyError, ValueError, TypeError, StopIteration):
            literal = False
    else:
        # The native meta→Agent link is required independently of callback labels.
        plugin = Path(__file__).resolve().parents[2] / 'plugins' / 'symphony'
        if str(plugin) not in sys.path:
            sys.path.insert(0, str(plugin))
        from symphony.host_evidence import claude_substantive_launch, _claude_historical_worker_terminal
        from symphony.store import _run_from_dict
        from symphony.model import Event
        latest = rows[-1].get('timestamp') if rows else ''
        if project is None or not project.is_absolute():
            raise RuntimeError('literal Claude worker has no bound fixture project')
        proof = run.get('assessment', {}).get('_substantive_children', {}).get(worker['identity'], {})
        admitted = proof.get('admitted_at')
        if not isinstance(admitted, str) or not admitted:
            starts = [record for record in captures if record.get('event') == 'SubagentStart'
                      and record.get('session_id') == run['session_id'] and record.get('agent_id') == worker['identity']]
            if len(starts) != 1 or type(starts[0].get('started_ns')) is not int:
                raise RuntimeError('legacy literal Claude worker has no exact captured Start')
            admitted = datetime.fromtimestamp(starts[0]['started_ns'] / 1e9, timezone.utc).isoformat()
        scoped = replace(_run_from_dict(run), assessment={'substantive_contract': {'accepted_at': run['started_at']}})
        source = Event('fixture-native-worker', 'subagent_stopped', latest, {'provider': 'claude',
            'session_id': run['session_id'], 'agent_id': worker['identity'], 'cwd': str(project)})
        binding = claude_substantive_launch(scoped, source, 'worker', admitted, {'CLAUDE_CONFIG_DIR': str(home)})
        if not binding:
            raise RuntimeError('literal Claude worker lacks exact native lead parent proof')
        assistants = [row for row in rows if row.get('type') == 'assistant']
        activity = [row for row in rows if row.get('type') in {'assistant', 'user'}]
        terminal = assistants[-1] if assistants else {}
        final = assistant_text(provider, terminal)
        prompts = [index for index, row in enumerate(rows) if row.get('type') == 'user'
                   and isinstance(row.get('message', {}).get('content'), str)]
        closed = (len(prompts) == 1 and _claude_historical_worker_terminal(
            rows, prompts[0], worker['identity'], run['session_id'], worker['requested_tier'],
            worker['requested_effort'], parent_rows=lead_rows, parent=run['lead_identity'],
            launch_hash=binding['launch_hash']) is not None)
        literal = closed and final == 'GATE_RELEASED'
        if closed and not literal and not any(line.strip().startswith('SYMPHONY_OUTCOME:') for line in final.splitlines()):
            handbacks = [(block, row) for row in assistants[:-1] for block in row.get('message', {}).get('content', [])
                         if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'SubagentHandback']
            if len(handbacks) == 1:
                call, call_row = handbacks[0]
                results = [(block, row) for row in rows if row.get('type') == 'user'
                           for block in row.get('message', {}).get('content', []) if isinstance(block, dict)
                           and block.get('type') == 'tool_result' and block.get('tool_use_id') == call.get('id')]
                if (isinstance(call.get('id'), str) and call['id'] and len(results) == 1
                        and results[0][0].get('is_error') is not True
                        and call.get('input', {}).get('message') == 'GATE_RELEASED'):
                    try:
                        literal = (timestamp_ns(call_row.get('timestamp')) <= timestamp_ns(results[0][1].get('timestamp'))
                                   <= timestamp_ns(terminal.get('timestamp')))
                    except (ValueError, TypeError):
                        literal = False
    if not literal:
        raise RuntimeError('literal worker has no exact successful native report')


def claude_literal_worker_probe(run, home):
    """Fixed report-shape facts; native report prose stays private."""
    from native_routing_smoke import native_rows, assistant_text, native_call_inventory
    from symphony.host_evidence import claude_native_parent_completion
    facts = []
    workers = [child for child in run.get('delegations', ()) if child.get('role') == 'worker']
    for ordinal, worker in enumerate(workers[:12]):
        item = {'ordinal': ordinal, 'native_reader_available': False}
        try:
            rows = native_rows('claude', home, worker['identity'])
            assistants = [row for row in rows if row.get('type') == 'assistant']
            activity = [row for row in rows if row.get('type') in {'assistant', 'user'}]
            terminal = assistants[-1] if assistants else {}
            report = assistant_text('claude', terminal)
            calls, results = native_call_inventory('claude', rows)
            lead_rows = native_rows('claude', home, run['lead_identity']) if run.get('lead_identity') else []
            handbacks = [call for call in calls if call[1] == 'SubagentHandback']
            proof = run.get('assessment', {}).get('_substantive_children', {}).get(worker['identity'], {})
            launch_hash = proof.get('launch_hash', '')
            launches = [block for row in lead_rows if row.get('type') == 'assistant'
                for block in row.get('message', {}).get('content', [])
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Agent'
                and isinstance(block.get('id'), str)
                and sha256(block['id'].encode()).hexdigest() == launch_hash]
            bound_results = [block for row in lead_rows if row.get('type') == 'user'
                for block in row.get('message', {}).get('content', [])
                if isinstance(block, dict) and block.get('type') == 'tool_result'
                and any(block.get('tool_use_id') == launch.get('id') for launch in launches)]
            parent_deliveries = [item for row in lead_rows if row.get('type') == 'user'
                for item in row.get('message', {}).get('content', [])
                if isinstance(item, dict) and item.get('type') == 'tool_result'
                and isinstance(item.get('content'), list)
                and any(isinstance(block, dict) and isinstance(block.get('text'), str)
                    and block['text'].startswith('agentId: ' + worker['identity'] + ' ')
                    for block in item['content'])]
            item.update(native_reader_available=True, assistant_count=len(assistants),
                native_bound_launch_count=len(launches),
                native_bound_background_launch_count=sum(launch.get('input', {}).get('run_in_background') is True
                                                          for launch in launches),
                native_bound_result_shapes=[{'content_type': type(delivery.get('content')).__name__,
                    'block_count': len(delivery['content']) if isinstance(delivery.get('content'), list) else None,
                    'is_error': delivery.get('is_error') is True,
                    'child_footer_present': 'agentId: ' + worker['identity'] in json.dumps(delivery.get('content')),
                    'literal_report_present': 'GATE_RELEASED' in json.dumps(delivery.get('content')),
                    'async_ack_present': 'Async agent launched successfully' in json.dumps(delivery.get('content'))}
                    for delivery in bound_results[:4]],
                native_parent_result_with_child_footer_count=len(parent_deliveries),
                native_parent_result_success_count=sum(delivery.get('is_error') is not True
                                                       for delivery in parent_deliveries),
                native_parent_result_exact_report_count=sum(bool(delivery['content'])
                    and delivery['content'][0] == {'type': 'text', 'text': report}
                    for delivery in parent_deliveries),
                native_parent_completion_verified=claude_native_parent_completion(
                    lead_rows, terminal, report, run.get('session_id', ''), run.get('lead_identity', ''), worker['identity'],
                    prompt=next((row for row in rows if row.get('type') == 'user'
                        and isinstance(row.get('message', {}).get('content'), str)), None), launch_hash=launch_hash),
                native_parent_notification_count=sum(isinstance(row.get('origin'), dict)
                    and row['origin'].get('kind') == 'task-notification' for row in lead_rows),
                final_activity_is_terminal=bool(activity) and activity[-1] is terminal,
                  terminal_is_end_turn=terminal.get('message', {}).get('stop_reason') == 'end_turn',
                  terminal_stop_reason_present='stop_reason' in terminal.get('message', {}),
                  terminal_stop_reason_null=terminal.get('message', {}).get('stop_reason') is None,
                terminal_has_uuid=bool(terminal.get('uuid')),
                prior_end_turn_count=sum(row.get('message', {}).get('stop_reason') == 'end_turn' for row in assistants[:-1]),
                api_error_count=sum(row.get('isApiErrorMessage') is True for row in assistants),
                exact_literal_final=report == 'GATE_RELEASED',
                literal_line_present='GATE_RELEASED' in report.splitlines(),
                outcome_marker_count=sum(line.strip().startswith('SYMPHONY_OUTCOME:') for line in report.splitlines()),
                handback_count=len(handbacks),
                exact_literal_handbacks=sum(call[2].get('message') == 'GATE_RELEASED' for call in handbacks
                                            if isinstance(call[2], dict)),
                handback_result_counts=[len(results.get(call[0], ())) for call in handbacks],
                handback_error_counts=[sum(result[3] for result in results.get(call[0], ())) for call in handbacks])
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
            item['error_type'] = type(error).__name__
        facts.append(item)
    return {'worker_count': len(workers), 'workers': facts}


def require_codex_gate_lead(records, label, session_id, lead_id):
    """The second-child scheduling heuristic cannot prove native ownership."""
    starts = [item for item in records if item.get('event') == 'SubagentStart'
              and item.get('native_gate_label') == label
              and item.get('session_id') == session_id and item.get('agent_id') == lead_id]
    if len(starts) != 1:
        raise RuntimeError(f'{label}: native gate has no unique canonical lead Start')
    facts = starts[0].get('native_metadata') or {}
    if (facts.get('own_header_identity_matches') is not True
            or facts.get('native_parent_matches_root') is not True
            or facts.get('declared_assessed_lead_matches') is not True
            or facts.get('native_task_role') != 'lead'):
        raise RuntimeError(f'{label}: native gate child lacks canonical lead metadata')


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
                         "background_task_states", "reported_outcome", "native_task_wait",
                         "native_stop_hold_label", "native_metadata", "admission_at_capture")}
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


def claude_callback_disposition_probe(root, run, document, session_records):
    """Observe exact private callback identities without replaying or ACKing them."""
    from native_routing_smoke import Event, _terminal_result_id, _child_turn_token
    assessment = run.get('assessment') or {}
    proofs = assessment.get('_substantive_children') or {}
    known = {item.get('identity') for item in run.get('delegations', [])}
    output = []
    for path in sorted((root / 'private-child-hooks').glob('*.json')):
        try:
            capture = json.loads(path.read_text(encoding='utf-8'))
            snapshot, raw = capture.get('canonical'), capture.get('raw')
            if not isinstance(snapshot, dict) or not isinstance(raw, dict):
                continue
            payload, event_id = snapshot.get('payload'), snapshot.get('event_id')
            if (not isinstance(payload, dict) or payload.get('provider') != 'claude'
                    or snapshot.get('kind') != 'subagent_stopped'
                    or raw.get('hook_event_name') != 'SubagentStop'
                    or set(payload) - set(raw) - {'provider', 'last_assistant_message'}
                    or set(raw) - set(payload)
                    or any(payload.get(key) != value for key, value in raw.items()
                           if key not in {'provider', 'last_assistant_message'})
                    or event_id != sha256(json.dumps(payload, sort_keys=True,
                        separators=(',', ':'), default=str).encode()).hexdigest()):
                continue
            identity = payload.get('agent_id') or payload.get('subagent_id')
            if identity not in known or payload.get('session_id') not in {run.get('session_id'), identity}:
                continue
            proof = proofs.get(identity) or {}
            source = Event(event_id, 'subagent_stopped', '', payload)
            # Observation only, with the same exact retained roster/root boundary.
            scoped = replace(source, payload={**payload, 'session_id': run['session_id']})
            result, token = _terminal_result_id(scoped), _child_turn_token(payload)
            history = [item for item in document.get('event_history', [])
                if str(item.get('event_id') or '').startswith(event_id + ':')]
            receipts = [item for item in document.get('terminal_receipts', [])
                if item.get('provider') == 'claude' and item.get('session') == run['session_id']
                and item.get('run_id') == run.get('run_id') and item.get('agent') == identity]
            role = proof.get('role')
            status = payload.get('status')
            output.append({'ordinal': len(output),
                'immutable_start_proof_present': bool(proof),
                'immutable_start_role': role if role in {'worker', 'consultant', 'lead'} else 'unknown',
                'canonical_id_equals_admitted_start': event_id == proof.get('start_event_id'),
                'canonical_id_in_start_ids': event_id in assessment.get('_start_event_ids', ()),
                'canonical_id_in_terminal_ids': event_id in assessment.get('_terminal_event_ids', ()),
                'scoped_result_in_terminal_ids': result in assessment.get('_terminal_event_ids', ()),
                'terminal_token_recorded': token in assessment.get('_terminal_turns', {}).get(identity, ()),
                'derived_history_count': len(history),
                'derived_terminal_history_count': sum(item.get('kind') == 'delegation_updated'
                    and item.get('payload', {}).get('state') in {'completed', 'failed'} for item in history),
                'pending_canonical_id_count': sum(item.get('event_id') == event_id
                    for record in session_records for item in record.get('pending', ())),
                'scoped_receipt_count': len(receipts),
                'exact_turn_receipt_count': sum(item.get('turn') == token for item in receipts),
                'exact_result_receipt_count': sum(item.get('result') == result for item in receipts),
                'supplied_status': status if status in {'completed', 'failed', 'blocked', 'done', 'success', 'succeeded'} else 'other_or_absent',
                'batch_pending': assessment.get('_batch_pending') is True})
            if len(output) >= 24:
                break
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            continue
    return output


def failure_state(root, provider):
    from native_routing_smoke import claude_child_binding_probe
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

    def run_summary(run, home):
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
                'native_literal_workers': (codex_literal_worker_probe(run, home) if provider == 'codex'
                                         else claude_literal_worker_probe(run, home)),
                'native_child_binding': claude_child_binding_probe(home, run, document)
                    if provider == 'claude' else None,
                'native_callback_disposition': claude_callback_disposition_probe(root, run, document, raw_session_records)
                    if provider == 'claude' else None,
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
            document = read_state_snapshot(path)
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
            if label not in cli_sessions:
                gate_root = case_root / 'native-gate' / f'{label}.root.json'
                try:
                    gate_session = json.loads(gate_root.read_text()).get('session_id')
                except (OSError, ValueError, AttributeError):
                    gate_session = None
                if (isinstance(gate_session, str) and gate_session and any(
                        run.get('provider') == provider and run.get('session_id') == gate_session
                        for run in (*document.get('active_runs', {}).values(), *document.get('recent_runs', [])))):
                    cli_sessions[label] = gate_session
        if case == 'case' and not cli_sessions:
            owners = {run.get('session_id') for run in (
                *document.get('active_runs', {}).values(), *document.get('recent_runs', ()))
                if run.get('provider') == provider and isinstance(run.get('session_id'), str)
                and run['session_id']}
            if len(owners) == 1:
                cli_sessions['a'] = owners.pop()
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
        raw_session_records = []
        for record_path in (case_root / "state").glob(".session-*.json"):
            try:
                record = read_state_snapshot(record_path)
            except (OSError, ValueError):
                continue
            if record.get("state_name") in {None, path.name}:
                raw_session_records.append(record)
                session_records.append(session_record_summary(record, root_sessions))
        native_home = root / f'{provider}-{"live-update" if case == "live-update" else "baseline"}-home'
        if provider == 'claude' and case == 'case' and (root / 'claude-home').is_dir():
            native_home = root / 'claude-home'
        recovery_probes = {}
        if provider == "claude":
            from native_claude_followup import sequence_rejection_probe
            from symphony import host_evidence
            from symphony.model import Event
            from symphony.store import _state_from_dict
            for label, session in cli_sessions.items():
                project = case_root / ("second" if label == "b" and
                                       (case_root / "second").exists() else "primary")
                if f"claude:{session}" in document.get("active_runs", {}):
                    recovery_probes[label] = claude_recovery_probe(
                        document, session, project, native_home)
                pending = [Event(item["event_id"], item["kind"], item["observed_at"], item["payload"])
                           for record in raw_session_records if record.get("owner_session") == session
                           for item in record.get("pending", ())]
                if pending:
                    recovery_probes.setdefault(label, {})["archived_sequence"] = sequence_rejection_probe(
                        _state_from_dict(document), pending, session, project,
                        {"CLAUDE_CONFIG_DIR": str(native_home)}, host_evidence, mixed=True)
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
                      "active_runs": [run_summary(run, native_home) for run in document.get("active_runs", {}).values()],
                      "recent_runs": [run_summary(run, native_home) for run in document.get("recent_runs", [])],
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
                    home, session, lead, case_root / "logs" / f"{label}.errors",
                    codex_profile=("full" if label == "a" else "latest")
                    if case["case"] == "live-update" else "base")
                if final_lead and final_lead != lead:
                    replacement = codex_host_trace(
                        home, session, final_lead, case_root / "logs" / f"{label}.errors",
                        codex_profile=("full" if label == "a" else "latest")
                        if case["case"] == "live-update" else "base")
                    trace["replacement_lead_turns"] = replacement["lead_turns"]
                    trace["replacement_parent_matches_root"] = replacement["lead_parent_matches_root"]
                host[f"{case['case']}:{label}"] = trace
    elif provider == "claude":
        for case in cases:
            home = (root / "claude-live-update-home" if case["case"] == "live-update" else
                    root / "claude-home" if case["case"] == "case" and (root / "claude-home").is_dir() else
                    root / "claude-baseline-home")
            runs = [*case["active_runs"], *case["recent_runs"]]
            for label, session in case["cli_session_ids"].items():
                lead = next((run.get("lead_id") for run in runs
                             if run.get("session_id") == session), None)
                if lead:
                    host[f"{case['case']}:{label}"] = claude_host_trace(home, session, lead)
            for run in runs:
                if run.get('session_id') not in case['cli_session_ids'].values() and run.get('lead_id'):
                    host[f"{case['case']}:durable-owner-{len(host)}"] = claude_host_trace(
                        home, run['session_id'], run['lead_id'])
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
    sequence_facts = {}
    for sequence_probe in (*root.glob('sendmessage-proof*.json'), *root.glob('sendmessage-required.json')):
        try:
            sequence_facts[sequence_probe.stem] = json.loads(sequence_probe.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            sequence_facts[sequence_probe.stem] = {'snapshot_unreadable': True}
    return {"provider": provider, "cases": cases, "native_host_trace": host,
            "sendmessage_proof": sequence_facts,
            "native_resume_status": resume_status,
            "native_resume_phase90": resume_phase,
            "gate_timeouts": gate_timeouts, "native_spawn_probes": spawn_probes,
            "native_hook_capture": codex_hook_capture_summary(root, provider)}


def preserve_private_native_failure(root, destination, provider):
    """Retain only local fixture evidence, excluding native credentials/config."""
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    target = destination / f'{provider}-{uuid.uuid4().hex[:12]}'
    target.mkdir(mode=0o700)
    private = root / 'private-child-hooks'
    if provider == 'claude' and private.is_dir():
        for path in private.glob('*.json'):
            if path.is_file() and not path.is_symlink():
                (target / 'private-child-hooks').mkdir(exist_ok=True)
                shutil.copyfile(path, target / 'private-child-hooks' / path.name)
    for case in ('same-worktree', 'worktrees', 'live-update'):
        source = root / case
        if not source.is_dir():
            continue
        for child in ('logs', 'state', 'native-gate', 'hook decisions'):
            if (source / child).is_dir():
                shutil.copytree(source / child, target / case / child)
        for name in ('update_event_counts.json', 'spawn_probe.json', 'old_root_interruption.json'):
            if (source / name).is_file():
                (target / case).mkdir(exist_ok=True)
                shutil.copyfile(source / name, target / case / name)
    for name in (f'{provider}-baseline-home', f'{provider}-live-update-home'):
        source = root / name
        for child in ('sessions', 'projects'):
            if (source / child).is_dir():
                for path in (source / child).rglob('*'):
                    if (not path.is_file() or path.is_symlink()
                            or not (path.suffix == '.jsonl' or path.name.endswith('.meta.json'))):
                        continue
                    copied = target / name / path.relative_to(source)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, copied)
    capture = root / f'{provider}-hook-capture'
    if capture.is_dir():
        shutil.copytree(capture, target / capture.name)
    # The optional directory is local-only and never part of CI's receipt glob.
    for path in target.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return target


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
    parser.add_argument('--only-sendmessage-followups', action='store_true',
                        help='run one fresh same-worktree baseline and explicit archived followups only')
    parser.add_argument('--claude-sendmessage-followups', action='store_true',
                        help='prove ordered archived same-lead sends and private copied-state replay')
    parser.add_argument("--direct-stop-resume", action="store_true",
                        help="probe an exact normal Stop control on Codex live-update resume")
    parser.add_argument("--old-plugin-root", type=Path,
                        help="actual released 1.6.0 package (CI baseline), or explicit legacy 1.5.1 probe")
    parser.add_argument('--keep-failure-dir', type=Path,
                        help='private local evidence directory; never upload its raw native transcripts')
    parser.add_argument("--candidate-plugin-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "plugins" / "symphony")
    args = parser.parse_args()
    attempt = uuid.uuid4().hex[:12]
    if args.only_live_update and not args.live_update:
        parser.error("--only-live-update requires --live-update")
    if args.only_live_update and args.only_worktrees:
        parser.error("--only-live-update and --only-worktrees cannot be combined")
    if args.only_sendmessage_followups:
        args.claude_sendmessage_followups = True
    if args.only_sendmessage_followups and args.live_update:
        parser.error('--only-sendmessage-followups cannot include a live update')
    if args.claude_sendmessage_followups and (args.provider != 'claude' or args.only_live_update or args.only_worktrees):
        parser.error('--claude-sendmessage-followups requires the Claude baseline same-worktree case')
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
                require_live_update_versions(old_version, candidate_version)
                if args.provider == "codex":
                    auth = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
                    if not os.environ.get("OPENAI_API_KEY") and not auth.is_file():
                        raise RuntimeError("live update needs OPENAI_API_KEY or existing Codex auth")
                elif not os.environ.get("ANTHROPIC_API_KEY"):
                    source_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
                    if not (source_config / ".credentials.json").is_file():
                        raise RuntimeError("Claude live update needs ANTHROPIC_API_KEY or existing auth")
            cases = (() if args.only_live_update else
                     (True,) if args.only_worktrees else (False,) if args.only_sendmessage_followups else (False, True))
            baseline_env = (prepare_baseline_capture(args.provider, root,
                            args.candidate_plugin_root, capture_child_sources=True) if cases else None)
            results = [check_case(args.provider, root, separate, args.timeout,
                                  args.claude_budget_usd,
                                  baseline_env=baseline_env) for separate in cases]
            if args.claude_sendmessage_followups:
                from native_claude_followup import check_native_sendmessage_pair
                results.extend(check_native_sendmessage_pair(root, args.candidate_plugin_root, baseline_env,
                                                            results[0], args.timeout, args.claude_budget_usd))
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
            if args.keep_failure_dir:
                preserve_private_native_failure(root, args.keep_failure_dir, args.provider)
            print(json.dumps({"provider": args.provider, "error": str(error),
                              "logs": str(root)}), file=sys.stderr)
            diagnostics = failure_state(root, args.provider)
            diagnostics["failure"] = {"type": type(error).__name__, "message": str(error)[:300]}
            diagnostics["observer_snapshot_lock_retries"] = OBSERVER_SNAPSHOT_LOCK_RETRIES
            destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"native-managed-{args.provider}-failure-{attempt}.json").write_text(
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
        (directory / f"native-managed-{args.provider}-receipt-{attempt}.json").write_text(
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
