#!/usr/bin/env python3
"""Probe real mechanical eligibility and worker execution in disposable native homes.

Three plain objectives per provider; no forced role packets or concurrency gates.
Only compact evidence is exported. Raw transcripts and credentials stay temporary.
"""

import argparse
from datetime import datetime
import hashlib
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

from native_managed_concurrency import prepare_baseline_capture, projects, run_git, state_file

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
sys.path.insert(0, str(PLUGIN))
from symphony.host_evidence import _complete_native_jsonl, _native_jsonl  # noqa: E402
from symphony.routing import Assessment, fast_lead_selection, route_for, snapshot_for  # noqa: E402
from symphony.store import StateStore  # noqa: E402

CASES = {
    "command": "Run python -m unittest -q and report the result.",
    "feature": "Add an uppercase argument to greet(name, uppercase=False), preserving the default greeting. Test both modes.",
    "run-and-fix": "Run python -m unittest -q and fix any failures. Verify the corrected result.",
}
TEST = """import unittest
from greet import greet
class GreetingTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(greet('Ada'), 'Hello, Ada!')
"""
FIXTURE_INSTRUCTIONS = (
    "This is a disposable greeting fixture. Use native apply_patch, Write, or Edit for file changes. "
    "Verify the integrated result with exactly python -m unittest -q in one standalone native "
    "exec_command (Codex) or Bash (Claude) invocation. Return the full structured tool result, "
    "including exit code and output. On Codex use text(await tools.exec_command({\"cmd\":\"python -m unittest -q\"})); "
    "Do not batch verification with other commands or print only result.output.\n")


def setup_prompt(provider):
    return "$symphony:symphony enable" if provider == "codex" else "/symphony:enable"


def fast_decision_lines(text):
    return re.findall(r"^SYMPHONY_FAST_DECISION: (eligible|escalate)[ \t]*\r?$", text, re.MULTILINE)


def assert_enabled_fixture(document, provider, profile, version):
    activation = document.get("activation", {}).get(provider, {})
    require(document.get("enabled") is True, "documented enable did not enable the fixture")
    require(activation.get("state") == "guarded" and activation.get("profile") == profile
            and activation.get("plugin_version") == version,
            "enabled fixture has no matching installed build/profile heartbeat")
    require(not document.get("active_runs") and not document.get("active_run")
            and not document.get("recent_runs"), "enable unexpectedly opened a run")


def failure_diagnostics(provider, root, document):
    """Export fixed categories and counts, never native message/config contents."""
    activation = document.get("activation", {}).get(provider, {})
    counts = {event: 0 for event in ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop")}
    for path in (root / f"{provider}-hook-capture").glob("*.json"):
        try:
            event = json.loads(path.read_text()).get("event")
            if event in counts:
                counts[event] += 1
        except (OSError, ValueError, AttributeError):
            pass
    home = root / f"{provider}-baseline-home"
    native_signals = {"fast_guidance_text_present": False, "eligible_markers": 0, "escalate_markers": 0,
                      "spawn_unknown_model": False}
    native_signals.update(root_eligible_markers=0, root_escalate_markers=0,
                         bound_child_eligible_markers=0, bound_child_escalate_markers=0)
    root_launches = dict.fromkeys(('calls', 'successful_results', 'error_results', 'missing_results'), 0)
    fast_calls = dict.fromkeys(('dedicated_read', 'exec_wrapper', 'shell_exec', 'edit', 'handback', 'other'), 0)
    runs = [*document.get('active_runs', {}).values(), *document.get('recent_runs', [])]
    known_children = {child.get('identity') for run in runs for child in run.get('delegations', [])}
    fast_children = set()
    for run in runs:
        fast = run.get('assessment', {}).get('_fast_route', {})
        lead = next((child for child in run.get('delegations', []) if child.get('role') == 'lead'), None)
        if (lead and fast and lead.get('requested_tier') == fast.get('model')
                and lead.get('requested_effort') == fast.get('effort')):
            fast_children.add(lead.get('identity'))
    paths = (home / ("sessions" if provider == "codex" else "projects")).rglob("*.jsonl")
    for path in paths:
        try:
            lines = path.read_text().splitlines()
            rows = [json.loads(line) for line in lines]
            header = rows[0].get('payload', {}) if provider == 'codex' and rows else {}
            identity = header.get('id') if provider == 'codex' else next(
                (row.get('agentId') for row in rows if row.get('agentId')), None)
            source = header.get('source')
            is_root = (not (isinstance(source, dict) and source.get('subagent')) if provider == 'codex'
                       else not any(row.get('isSidechain') is True for row in rows))
            if is_root:
                counts_for_root = raw_launch_counts(provider, rows)
                for key in root_launches:
                    root_launches[key] += counts_for_root[key]
            if identity in fast_children:
                categories = raw_tool_categories(provider, rows)
                for key in fast_calls:
                    fast_calls[key] += categories[key]
            for line, row in zip(lines, rows):
                # Presence is a diagnostic, not proof that a hook delivered it.
                native_signals["fast_guidance_text_present"] |= "Symphony fast route: the root is a courier" in line
                payload = row.get("payload", {})
                native_signals["spawn_unknown_model"] |= (payload.get("type") == "function_call_output"
                    and str(payload.get("output", "")).startswith("Unknown model `"))
                text = assistant_text(provider, row)
                for decision in ("eligible", "escalate"):
                    count = fast_decision_lines(text).count(decision)
                    native_signals[decision + "_markers"] += count
                    if is_root:
                        native_signals['root_' + decision + '_markers'] += count
                    elif identity in known_children:
                        native_signals['bound_child_' + decision + '_markers'] += count
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    boosts = document.get("configuration", {}).get("assessor_boosts", {}).values()
    phases = []
    phase_path = root / 'logs' / 'phases.json'
    if phase_path.is_file():
        for phase in json.loads(phase_path.read_text()):
            if phase.get('phase') in {'enable', 'objective', 'reconcile', 'stop'}:
                phases.append({'phase': phase['phase'], 'resume': phase.get('resume') is True,
                               'returned': phase.get('returned') is True,
                               'cli_success': phase.get('cli_success') is True})
    return {"enabled": document.get("enabled") is True,
            "activation_guarded": activation.get("state") == "guarded",
            "profile": activation.get("profile") if activation.get("profile") in {"latest", "full", "opus-5-5"} else "unknown",
            "loaded_version": activation.get("plugin_version") if re.fullmatch(r"\d+\.\d+\.\d+", str(activation.get("plugin_version"))) else "unknown",
            "boost_levels": sorted({value for value in boosts if isinstance(value, str) and value in {"off", "xhigh", "max", "ultra"}}),
            "callbacks": counts, "native_signals": native_signals,
            'fast_raw_tool_categories': fast_calls, 'root_native_launches': root_launches,
            "lifecycle": lifecycle_diagnostics(provider, root, document), 'phases': phases}


def raw_tool_categories(provider, rows):
    """Count all calls, including failed/unreturned calls; publish no arguments."""
    result = dict.fromkeys(('dedicated_read', 'exec_wrapper', 'shell_exec', 'edit', 'handback', 'other'), 0)
    calls = ([row.get('payload', {}) for row in rows
              if row.get('payload', {}).get('type') in {'function_call', 'custom_tool_call'}]
             if provider == 'codex' else [block for row in rows
                 for block in row.get('message', {}).get('content', [])
                 if isinstance(block, dict) and block.get('type') == 'tool_use'])
    for call in calls:
        name = call.get('name')
        category = ('dedicated_read' if name in {'Read', 'Glob', 'Grep', 'LS', 'view_image', 'functions.view_image', 'read_file'}
                    else 'exec_wrapper' if name in {'exec', 'functions.exec'}
                    else 'shell_exec' if name in {'exec_command', 'functions.exec_command', 'Bash'}
                    else 'edit' if name in {'apply_patch', 'functions.apply_patch', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}
                    else 'handback' if name == 'SubagentHandback' else 'other')
        result[category] += 1
    return result


def raw_launch_counts(provider, rows):
    result = dict.fromkeys(('calls', 'successful_results', 'error_results', 'missing_results'), 0)
    calls, outcomes = set(), {}
    for row in rows:
        if provider == 'claude':
            for block in row.get('message', {}).get('content', []):
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('name') == 'Agent':
                    calls.add(block.get('id'))
                elif block.get('type') == 'tool_result':
                    outcomes[block.get('tool_use_id')] = block.get('is_error') is not True
        else:
            payload = row.get('payload', {})
            if payload.get('type') == 'function_call' and payload.get('name') == 'spawn_agent':
                calls.add(payload.get('call_id'))
            elif payload.get('type') == 'function_call_output':
                # Codex plain unknown-model output is a native launch failure.
                value = payload.get('output')
                outcomes[payload.get('call_id')] = isinstance(value, str) and not value.startswith('Unknown model `')
    for identity in calls - {None, ''}:
        result['calls'] += 1
        result['missing_results' if identity not in outcomes else
               'successful_results' if outcomes[identity] else 'error_results'] += 1
    return result


def lifecycle_diagnostics(provider, root, document):
    """Bounded state categories; identities, messages and arbitrary reasons stay private."""
    store = StateStore(root / 'state')
    result = []
    for run in document.get('active_runs', {}).values():
        if run.get('provider') != provider:
            continue
        assessment = run.get('assessment', {})
        known = {run.get('session_id'), run.get('lead_identity'),
                 *(child.get('identity') for child in run.get('delegations', []))} - {None, ''}
        kinds = dict.fromkeys(('subagent_started', 'subagent_stopped', 'other'), 0)
        matches = dict.fromkeys(('agent_known', 'parent_known', 'session_known', 'turn_active',
                                'turn_terminal', 'generation_current', 'ambiguous_owner'), 0)
        records = dict.fromkeys(('root', 'alias', 'missing', 'overflow'), 0)
        owner = run.get('session_id')
        for session in (owner, *store.aliases_for_owner(provider, owner)):
            record = store.session_record(provider, session)
            if record is None:
                records['missing'] += 1
                continue
            records['root' if session == owner else 'alias'] += 1
            records['overflow'] += record.get('overflow') is True
            for entry in record.get('pending', []):
                kind = entry.get('kind')
                kinds[kind if kind in kinds else 'other'] += 1
                payload = entry.get('payload', {})
                identity = payload.get('agent_id') or payload.get('subagent_id')
                token = next((field + ':' + str(payload[field]) for field in ('turn_id', 'prompt_id')
                              if payload.get(field)), '')
                matches['agent_known'] += identity in known
                matches['parent_known'] += payload.get('parent_thread_id') in known
                matches['session_known'] += payload.get('session_id') in known
                matches['turn_active'] += bool(token) and assessment.get('_active_turns', {}).get(identity) == token
                matches['turn_terminal'] += bool(token) and token in assessment.get('_terminal_turns', {}).get(identity, [])
                matches['generation_current'] += entry.get('generation') == record.get('generation')
                matches['ambiguous_owner'] += entry.get('ambiguous_owner') is True
        conditions = {key: bool(assessment.get('_' + key)) for key in (
            'batch_pending', 'ambiguous_child_starts', 'ambiguous_child_stops',
            'pending_delegations', 'invalid_consultants', 'lead_route_mismatch', 'pending_lead_completion')}
        conditions['active_children'] = any(child.get('state') in {'working', 'pending', 'running', 'active'}
                                            for child in run.get('delegations', []))
        conditions['interrupted_children'] = any(child.get('state') == 'interrupted'
                                                 for child in run.get('delegations', []))
        conditions['successful_outcome'] = (run.get('outcome') or {}).get('status') in {'completed', 'done', 'success', 'succeeded'}
        result.append({'pending_kinds': kinds, 'invocation_matches': matches, 'records': records,
                       'stop_conditions': conditions})
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_rows(provider, home, identity):
    paths = (list((home / "sessions").rglob(f"*{identity}.jsonl")) if provider == "codex"
             else list((home / "projects").glob(f"*/**/agent-{identity}.jsonl")))
    require(len(paths) == 1, "child transcript identity is missing or ambiguous")
    rows = (_complete_native_jsonl if provider == "codex" else _native_jsonl)(paths[0])
    require(rows is not None, "child transcript is incomplete")
    return rows


def worker_transcript_is_unforked(provider, rows, identity):
    """Copied ancestor calls cannot establish worker-origin writes."""
    if provider != "codex":
        return True
    headers = [row.get("payload", {}) for row in rows if row.get("type") == "session_meta"]
    return (len(headers) == 1 and headers[0].get("id") == identity
            and not headers[0].get("forked_from_id"))


def worker_launch_verified(provider, evidence, worker, rows=(), parent_identity="", parent_path=""):
    matches = 0
    for name, arguments, _, output in evidence:
        if name not in {"spawn_agent", "Agent"} or not isinstance(arguments, dict):
            continue
        packet = arguments.get("message", arguments.get("prompt", ""))
        if provider == "codex":
            if (arguments.get("fork_turns") != "none"
                    or arguments.get("model") != worker["requested_tier"]
                    or arguments.get("reasoning_effort") != worker["requested_effort"]
                    or not worker_transcript_is_unforked(provider, rows, worker["identity"])):
                continue
            header = next(row["payload"] for row in rows if row.get("type") == "session_meta")
            spawn = header.get("source", {}).get("subagent", {}).get("thread_spawn", {})
            worker_name = "symphony_worker_" + re.sub(r"\W", "_", worker["requested_tier"]) + "_" + worker["requested_effort"]
            try:
                returned = json.loads(output)
            except (TypeError, ValueError):
                continue
            # Native Codex encrypts packets and returns a task path, not UUID.
            # Bind that exact path to the child's own header and canonical parent.
            if (isinstance(returned, dict) and returned.get("task_name") == header.get("agent_path")
                    and arguments.get("task_name") == worker_name
                    and parent_path and header.get("agent_path") == parent_path + "/" + worker_name
                    and parent_identity and spawn.get("parent_thread_id") == parent_identity):
                matches += 1
        elif (isinstance(packet, str) and packet.splitlines()[:1] == ["SYMPHONY_ROLE: worker"]
              and worker["identity"] in output):
            matches += 1
    return matches == 1


def assistant_text(provider, row):
    message = row.get("payload", {}) if provider == "codex" else row.get("message", {})
    if message.get("role") != "assistant":
        return ""
    content = message.get("content", [])
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "") if block.get("type") in {"text", "output_text"}
        else block.get("input", {}).get("message", "")
        for block in content if isinstance(block, dict) and (
            block.get("type") in {"text", "output_text"} or block.get("name") == "SubagentHandback"))


def tool_evidence(provider, rows):
    """Match native call/result IDs, retaining arguments only within this process."""
    calls, results = {}, {}
    for row in rows:
        if provider == "codex":
            item = row.get("payload", {})
            if item.get("type") in {"function_call", "custom_tool_call"}:
                arguments = item.get("arguments", item.get("input", ""))
                if item.get("type") == "function_call" and isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        arguments = None
                calls[item.get("call_id")] = (item.get("name", ""), arguments, row.get("timestamp"))
            elif item.get("type") in {"function_call_output", "custom_tool_call_output"}:
                value = item.get("output", "")
                output = value if isinstance(value, str) else json.dumps(value)
                results[item.get("call_id")] = output if output and not re.search(
                    r'Error:|"isError"\s*:\s*true|exit code [1-9]|"exit_code"\s*:\s*[1-9]', output) else None
        else:
            content = row.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls[block.get("id")] = (block.get("name", ""), block.get("input", {}),
                                              row.get("timestamp"))
                elif block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = (json.dumps(block.get("content", ""))
                                                        if not block.get("is_error", False) else None)
    return [(name, arguments, timestamp, results[identity]) for identity, (name, arguments, timestamp) in calls.items()
            if identity and results.get(identity)]


JSON_STRING = r'"(?:[^"\\]|\\.)*"'


def composed_call(source, method):
    """Recognize only a single literal native tool invocation, never evaluate JS."""
    if not isinstance(source, str):
        return None
    source = re.sub(r'^\s*// @exec:[^\n]*\n', '', source).strip()
    argument = JSON_STRING if method == "apply_patch" else r'\{[^{}]*\}'
    call = rf'tools\.{method}\((?P<argument>{argument})\)'
    for wrapper in (rf'text\(await {call}\);?',
                    rf'const (?P<result>\w+) = await {call};\s*text\((?P=result)\);?'):
        match = re.fullmatch(wrapper, source, re.DOTALL)
        if match:
            value = match['argument']
            if method == "exec_command":
                value = re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', value)
            try:
                return json.loads(value)
            except ValueError:
                return None
    if method == "apply_patch":
        match = re.fullmatch(rf'const (?P<variable>\w+) = (?P<patch>{JSON_STRING});\s*'
                             r'text\(await tools\.apply_patch\((?P=variable)\)\);?', source, re.DOTALL)
        if match:
            return json.loads(match['patch'])
    return None


def fixture_edit(provider, evidence, project):
    # This proves supported native edits to this fixture, not arbitrary shell writes.
    target = project / "greet.py"
    for name, arguments, _, output in evidence:
        if provider == "claude" and name in {"Write", "Edit", "MultiEdit"} and isinstance(arguments, dict):
            path = Path(arguments.get("file_path", ""))
            if (path if path.is_absolute() else project / path).resolve() == target.resolve():
                return True
        patch = (arguments if name == "apply_patch" else composed_call(arguments, "apply_patch")
                 if provider == "codex" and name in {"exec", "functions.exec"} else None)
        positive_result = "Success." in output if name == "apply_patch" else False
        if name in {"exec", "functions.exec"}:
            try:
                blocks = json.loads(output)
                positive_result = (isinstance(blocks, list) and len(blocks) == 2
                    and blocks[0].get("text", "").startswith("Script completed")
                    and blocks[1].get("text") == "{}" and "Failed" not in output)
            except (ValueError, AttributeError):
                pass
        if positive_result and isinstance(patch, str) and patch.startswith("*** Begin Patch\n") and patch.rstrip().endswith("*** End Patch"):
            paths = re.findall(r'^\*\*\* (?:Update|Add) File: (.+)$', patch, re.MULTILINE)
            if any((Path(path) if Path(path).is_absolute() else project / path).resolve() == target.resolve() for path in paths):
                return True
    return False


def fast_turn_has_only_escalation(provider, rows, evidence):
    """Every raw tool call counts; even a failed command can modify a file."""
    if provider == "codex":
        return not any(row.get("payload", {}).get("type") in {"function_call", "custom_tool_call"} for row in rows)
    calls = [block for row in rows for block in row.get("message", {}).get("content", [])
             if isinstance(block, dict) and block.get("type") == "tool_use"]
    if not calls:
        return True
    return (len(calls) == 1 and calls[0].get("name") == "SubagentHandback"
            and "SYMPHONY_FAST_DECISION: escalate" in calls[0].get("input", {}).get("message", "")
            and len(evidence) == 1 and evidence[0][0] == "SubagentHandback")


def observed_composed_unittest(source, output):
    """Captured literal verification forms; unknown JavaScript stays unverified."""
    if not isinstance(source, str):
        return False
    try:
        blocks = json.loads(output)
        if (not isinstance(blocks, list) or not blocks
                or blocks[0].get("type") != "input_text"
                or not blocks[0].get("text", "").startswith("Script completed")):
            return False
        def arguments(literal):
            return json.loads(re.sub(r'([{,]\s*)([A-Za-z_]\w*)(\s*:)', r'\1"\2"\3', literal))
        def passed(stdout):
            return (isinstance(stdout, str) and re.match(
                r'^-{3,}\r?\nRan [1-9]\d* tests? in \d+(?:\.\d+)?s\r?\n\r?\nOK(?:\r?\n|$)', stdout) is not None
                and not re.search(r'FAILED|Traceback|fatal:|error:', stdout, re.IGNORECASE))
        structured = re.fullmatch(
            r'\s*const (?P<result>\w+)\s*=\s*await tools\.exec_command\((?P<args>\{[^{}]*\})\);'
            r'\s*text\(JSON\.stringify\((?P=result)\)\);?\s*', source, re.DOTALL)
        if structured and len(blocks) == 2 and blocks[1].get('type') == 'input_text':
            result = json.loads(blocks[1].get('text', ''))
            return (arguments(structured['args']).get('cmd') == 'python -m unittest -q'
                    and isinstance(result, dict) and result.get('exit_code') == 0
                    and passed(result.get('output')))
        single = re.fullmatch(
            r'\s*const (?P<result>\w+)\s*=\s*await tools\.exec_command\((?P<args>\{[^{}]*\})\);'
            r'\s*text\((?P=result)\.output\);?\s*', source, re.DOTALL)
        if single:
            details = arguments(single['args'])
            return (details.get("cmd") == "python -m unittest -q && git diff -- greet.py"
                    and len(blocks) == 2 and blocks[1].get("type") == "input_text"
                    and passed(blocks[1].get("text")))
        batch = re.fullmatch(
            r'\s*const results\s*=\s*await Promise\.all\(\[tools\.exec_command\((?P<first>\{[^{}]*\})\),'
            r'\s*tools\.exec_command\((?P<second>\{[^{}]*\})\)\]\);\s*'
            r'for \(let i=0;i<results\.length;i\+\+\)\{text\(JSON\.stringify\(\{i,'
            r'output:results\[i\]\.output,exit_code:results\[i\]\.exit_code\}\)\);\}\s*', source, re.DOTALL)
        if batch and len(blocks) == 3:
            if (arguments(batch['first']).get('cmd') != 'git diff -- greet.py test_greet.py'
                    or arguments(batch['second']).get('cmd') != 'python -m unittest -q'):
                return False
            results = [json.loads(block['text']) for block in blocks[1:] if block.get('type') == 'input_text']
            return (len(results) == 2 and all(isinstance(result, dict)
                    and type(result.get('i')) is int and result['i'] == index
                    and type(result.get('exit_code')) is int and result['exit_code'] == 0
                    for index, result in enumerate(results)) and passed(results[1].get('output')))
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    return False


def unittest_verified(evidence):
    for name, arguments, _, output in evidence:
        if name in {"exec", "functions.exec"} and observed_composed_unittest(arguments, output):
            return True
        details = arguments if name in {"Bash", "exec_command"} else composed_call(arguments, "exec_command") if name in {"exec", "functions.exec"} else None
        if not isinstance(details, dict):
            continue
        try:
            tokens = shlex.split(details.get("command", details.get("cmd", "")))
        except ValueError:
            continue
        if (len(tokens) >= 3 and re.fullmatch(r'python(?:3|\.exe)?', tokens[0])
                and tokens[1:3] == ["-m", "unittest"] and all(token in {"-q", "-v"} for token in tokens[3:])
                and "Ran " in output and "OK" in output and "FAILED" not in output):
            return True
    return False


def check_case(provider, root, candidate, case, timeout, budget, requested_profile=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SYMPHONY_") and key not in {"CODEX_SESSION_ID", "CLAUDECODE"}}
    env.update(prepare_baseline_capture(provider, root, candidate))
    project, _ = projects(root, False)
    greeting = project / "greet.py"
    greeting.write_text("def greet(name):\n    return f'" + ("hello" if case == "run-and-fix" else "Hello") + ", {name}!'\n")
    (project / "test_greet.py").write_text(TEST)
    (project / "AGENTS.md").write_text(FIXTURE_INSTRUCTIONS)
    run_git("add", ".", cwd=project)
    run_git("commit", "-m", "greeting fixture", cwd=project)
    initial_hash = fingerprint(greeting)
    state_dir = root / "state"
    profile = requested_profile or ("latest" if provider == "codex" else "opus-5-5")
    fast = fast_lead_selection(snapshot_for(provider, profile))
    require(bool(fast["model"]), "semantic probe profile has no capable/medium fast route")
    env.update(SYMPHONY_STATE_DIR=str(state_dir), SYMPHONY_PROFILE=profile,
               SYMPHONY_RUNTIME_DIR=str(root / "runtimes"), SYMPHONY_HOOK_DECISIONS_DIR=str(root / "hook-decisions"))
    executable = shutil.which(provider)
    require(bool(executable), "native CLI executable is unavailable")
    session = str(uuid.uuid4())
    prompt = CASES[case]
    logs = root / "logs"
    logs.mkdir()
    phases = []
    deadline = time.monotonic() + timeout
    last_unchanged_ns = time.time_ns()
    first_change_after_ns = None

    def run(prompt, resume=False, phase='objective'):
        nonlocal last_unchanged_ns, first_change_after_ns
        phases.append({'phase': phase, 'resume': resume, 'returned': False, 'cli_success': False})
        (logs / 'phases.json').write_text(json.dumps(phases))
        if provider == "codex":
            command = [executable, "exec", "--dangerously-bypass-hook-trust",
                       "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                       "--model", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
                       "-c", "features.multi_agent=true", "-C", str(project), prompt]
        else:
            command = [executable, "--print", "--model", "haiku", "--max-budget-usd", str(budget),
                       "--permission-mode", "bypassPermissions", "--output-format", "json",
                       "--resume" if resume else "--session-id", session, prompt]
        with (logs / "stdout").open("a", encoding="utf-8") as output, (logs / "stderr").open("a", encoding="utf-8") as errors:
            process = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=errors)
            while process.poll() is None:
                if fingerprint(greeting) == initial_hash:
                    last_unchanged_ns = time.time_ns()
                elif first_change_after_ns is None:
                    first_change_after_ns = last_unchanged_ns
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise RuntimeError("native semantic case exceeded its time limit")
                time.sleep(.1)
        phases[-1].update(returned=True, cli_success=process.returncode == 0)
        (logs / 'phases.json').write_text(json.dumps(phases))
        require(process.returncode == 0, "native semantic CLI failed")

    run(setup_prompt(provider), phase='enable')
    state_path = state_file(state_dir, project)
    require(state_path.is_file(), "installed enable hook did not create durable state")
    version = json.loads((candidate / ".codex-plugin" / "plugin.json").read_text())["version"]
    assert_enabled_fixture(json.loads(state_path.read_text()), provider, profile, version)
    run(prompt, provider == "claude")
    if provider == "codex":
        matches = re.findall(r"^session id: ([0-9a-f-]+)$", (logs / "stderr").read_text(), re.MULTILINE)
        require(len(matches) == 2 and matches[0] != matches[1], "native enable/objective sessions are missing or reused")
        session = matches[-1]
    require(state_path.is_file(), "installed native hooks did not create durable state")
    document = json.loads(state_path.read_text())
    owner = f"{provider}:{session}"
    active = lambda doc: doc.get("active_runs", {}).get(owner)
    if provider == "claude" and active(document):
        # Background native results may need a same-session root turn to reconcile.
        run("Continue this Symphony session after its background results; reconcile the existing work and finish.", True, 'reconcile')
        document = json.loads(state_path.read_text())
        if (active(document) or {}).get("status") == "completing":
            run("/symphony:stop", True, 'stop')
            document = json.loads(state_path.read_text())
    require(active(document) is None, "native Stop did not archive the run")
    runs = [item for item in document.get("recent_runs", []) if item.get("session_id") == session]
    require(len(runs) == 1 and runs[0]["status"] == "completed", "native run did not complete exactly once")
    run_state = runs[0]
    store = StateStore(state_dir)
    record = store.session_record(provider, session)
    require(record is not None and not record.get("pending") and not record.get("overflow"),
            "native callbacks remain unresolved")
    for alias in store.aliases_for_owner(provider, session):
        child_record = store.session_record(provider, alias)
        require(child_record is not None and not child_record.get("pending") and not child_record.get("overflow"),
                "bound child callbacks remain unresolved")
    home = Path(env["CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"])
    children = run_state.get("delegations", [])
    child_rows = {item["identity"]: native_rows(provider, home, item["identity"]) for item in children}
    decisions = []
    for identity, rows in child_rows.items():
        for row in rows:
            for decision in fast_decision_lines(assistant_text(provider, row)):
                decisions.append((identity, decision, row.get("timestamp")))
    expected = "eligible" if case == "command" else "escalate"
    require(len(decisions) == 1 and decisions[0][1] == expected, "native fast lead made the wrong semantic decision")
    fast_identity, _, terminal_time = decisions[0]
    observed_fast = next(item for item in children if item["identity"] == fast_identity)
    require((observed_fast["requested_tier"], observed_fast["requested_effort"]) == (fast["model"], fast["effort"]),
            "native fast lead disagrees with the selected capable/medium route")
    fast_evidence = tool_evidence(provider, child_rows[fast_identity])
    workers = [item for item in children if item["role"] == "worker"]
    assessors = [item for item in children if item["role"] == "assessor"]
    if case == "command":
        require(not workers and not assessors and fingerprint(greeting) == initial_hash,
                "mechanical objective changed the fixture or used assessed delegation")
        require(unittest_verified(fast_evidence),
                "mechanical command has no successful native tool result")
    else:
        require(len(assessors) == 1 and workers, "substantive objective lacks an independent assessor or worker")
        require(assessors[0]["identity"] != fast_identity and run_state["lead_identity"] != fast_identity,
                "assessed execution reused the unassessed fast lead")
        assessment = run_state["assessment"]
        assessed_leads = [item for item in children if item["identity"] != fast_identity and item["role"] in {"lead", "rejected_lead"}]
        require(len(assessed_leads) == 1 and assessed_leads[0]["identity"] == run_state["lead_identity"],
                "plain substantive objective used an extra or misrouted assessed lead")
        selected = assessment["route"]
        require((assessed_leads[0]["requested_tier"], assessed_leads[0]["requested_effort"]) == (selected["lead_model"], selected["lead_effort"]),
                "observed assessed lead differs from the matrix profile route")
        execution = route_for(Assessment(assessment["size"], assessment["complexity"], assessment["risk"])).execution
        require(assessment["topology"] == execution == assessment["route"]["execution"],
                "native assessed topology disagrees with the matrix")
        require(fast_turn_has_only_escalation(provider, child_rows[fast_identity], fast_evidence),
                "substantive fast turn performed tools; before-change proof is unverified")
        terminal_ns = int(datetime.fromisoformat(terminal_time.replace("Z", "+00:00")).timestamp() * 1e9)
        require(first_change_after_ns is not None and first_change_after_ns > terminal_ns,
                "fixture was not observed unchanged after fast escalation")
        lead_evidence = tool_evidence(provider, child_rows[run_state["lead_identity"]])
        parent_path = ""
        if provider == "codex":
            lead_rows = child_rows[run_state["lead_identity"]]
            require(worker_transcript_is_unforked(provider, lead_rows, run_state["lead_identity"]),
                    "lead transcript contains inherited or foreign calls; integration proof is unverified")
            parent_path = next(row["payload"]["agent_path"] for row in lead_rows if row.get("type") == "session_meta")
        for worker in workers:
            require(worker_transcript_is_unforked(provider, child_rows[worker["identity"]], worker["identity"]),
                    "worker transcript contains inherited or foreign calls; write provenance is unverified")
            require(worker_launch_verified(provider, lead_evidence, worker,
                                           child_rows[worker["identity"]], run_state["lead_identity"], parent_path),
                    "worker native launch/result is not bound to the canonical assessed lead")
        require(any(fixture_edit(provider, tool_evidence(provider, child_rows[item["identity"]]), project) for item in workers),
                "fixture edit has no successful worker-origin native tool evidence")
        require(unittest_verified(lead_evidence),
                "lead integration has no successful native verification call")
    oracle = "from greet import greet; assert greet('Ada') == 'Hello, Ada!'"
    if case == "feature":
        oracle += "; assert greet('Ada', uppercase=True) == 'HELLO, ADA!'"
    result = subprocess.run([sys.executable, "-c", oracle], cwd=project, capture_output=True)
    require(result.returncode == 0, "final greeting fixture failed its external acceptance check")
    return {"case": case, "fast_decision": expected, "fast_model": observed_fast["requested_tier"],
            "fast_effort": observed_fast["requested_effort"],
            "assessment": {key: run_state["assessment"].get(key) for key in ("size", "complexity", "topology")},
            "workers": len(workers), "worker_edit_verified": case != "command", "pending_callbacks": 0,
            "worker_packet_role_marker_observed": provider == "claude" and case != "command",
            "status": "completed", "fixture_sha256": fingerprint(greeting)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--profile", choices=("latest", "full", "opus-5-5"),
                        help="Explicit shipped profile; Codex latest requires an observed compatible spawn roster")
    parser.add_argument("--candidate-plugin-root", type=Path, default=PLUGIN)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per plain objective")
    parser.add_argument("--budget", type=float, default=6)
    parser.add_argument("--case", choices=tuple(CASES))
    parser.add_argument("--private-failure-dir", type=Path,
                        help="Local debugging only: preserve raw logs/state/transcripts, never authentication files")
    args = parser.parse_args()
    receipt = {"provider": args.provider, "native_routing": [], "browser_evidence": "instruction contract only"}
    try:
        for case in ((args.case,) if args.case else CASES):
            with tempfile.TemporaryDirectory(prefix="symphony-native-routing-") as scratch:
                try:
                    receipt["native_routing"].append(check_case(args.provider, Path(scratch),
                        args.candidate_plugin_root.resolve(), case, args.timeout, args.budget, args.profile))
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    if args.private_failure_dir:
                        destination = args.private_failure_dir / f"{args.provider}-{case}-{uuid.uuid4().hex[:8]}"
                        destination.mkdir(parents=True, mode=0o700)
                        native_home = Path(scratch) / f"{args.provider}-baseline-home"
                        for source in (Path(scratch) / "logs", Path(scratch) / "state",
                                       native_home / ("sessions" if args.provider == "codex" else "projects")):
                            if source.exists():
                                shutil.copytree(source, destination / source.name)
                        receipt["private_failure_path"] = str(destination)
                    for path in (Path(scratch) / "state").glob("*.v2.json"):
                        document = json.loads(path.read_text())
                        receipt["fixture_diagnostics"] = failure_diagnostics(args.provider, Path(scratch), document)
                        runs = [*document.get("active_runs", {}).values(), *document.get("recent_runs", [])]
                        receipt["observed"] = [{"status": run.get("status"), "outcome": run.get("outcome"),
                            "recovery_reasons": [key for key in run.get("assessment", {}) if key.startswith("_retryable") or key == "_pending_lead_completion"],
                            "children": [{"role": child["role"], "state": child["state"]} for child in run.get("delegations", [])]}
                            for run in runs]
                    raise
        print(json.dumps(receipt))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        receipt["failure"] = {"case": case, "type": type(error).__name__, "message": str(error)[:200]}
        destination = os.environ.get("SYMPHONY_NATIVE_DIAGNOSTICS_DIR")
        if destination:
            path = Path(destination)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{args.provider}-routing.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
