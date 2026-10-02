#!/usr/bin/env python3
"""Probe real mechanical eligibility and worker execution in disposable native homes.

Three plain objectives per provider; no forced role packets or concurrency gates.
Only compact evidence is exported. Raw transcripts and credentials stay temporary.
"""

import argparse
from dataclasses import replace
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

from native_managed_concurrency import prepare_baseline_capture, projects, run_git, state_file, read_state_snapshot

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
sys.path.insert(0, str(PLUGIN))
from symphony.host_evidence import _complete_native_jsonl, _native_jsonl  # noqa: E402
from symphony import host_evidence  # noqa: E402
from symphony.routing import Assessment, fast_lead_selection, route_for, snapshot_for  # noqa: E402
from symphony.store import StateStore, _state_from_dict, _run_from_dict  # noqa: E402
from symphony.model import Event, ProjectState  # noqa: E402
from symphony.adapters import event_from_payload  # noqa: E402
from symphony.runtime import _terminal_result_id, _child_turn_token, _child_turn_kind, _substantive_turn_matches, _observed_role, _retained_activation_command  # noqa: E402

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
FIXTURE_COMMAND_INSTRUCTIONS = (
    "The root delegates and waits; relay these execution instructions to the selected fast lead. "
    "The fast lead runs the requested test command in one standalone native exec_command (Codex) or Bash (Claude) call. "
    "Use exactly python -m unittest -q, with no test-file arguments, discovery arguments, shell "
    "operators, pipelines, or output filters. Relay this exact command unchanged to the fast lead. "
    "Return the full structured result including exit code and output. On Codex use "
    "text(await tools.exec_command({\"cmd\":\"python -m unittest -q\"})); do not print only result.output.\n")
FIXTURE_INSTRUCTIONS = (
    "This is a disposable greeting fixture. Use native apply_patch, Write, or Edit for file changes. "
    "Relay this fixture acceptance contract unchanged to the canonical assessed lead. Workers own and make "
    "the implementation edits; delegate them before editing project files. After ALL workers "
    "return successfully, the canonical lead itself must run exactly python -m unittest -q; a worker's "
    "test run or git diff does not fulfill this lead integration check. Use one standalone native "
    "exec_command (Codex) or Bash (Claude) invocation. Return the full structured tool result, "
    "including exit code and output. On Codex use text(await tools.exec_command({\"cmd\":\"python -m unittest -q\"})); "
    "Do not batch verification with other commands or print only result.output. "
    'If supplying an outcome, use one valid JSON line: SYMPHONY_OUTCOME: {"status":"completed"}. '
    'Use failed or blocked when appropriate. Never emit SYMPHONY_OUTCOME: completed or duplicate a marker.\n')


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
            event = json.loads(path.read_text(encoding="utf-8")).get("event")
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
    root_calls = dict(fast_calls)
    runs = [*document.get('active_runs', {}).values(), *document.get('recent_runs', [])]
    known_children = {child.get('identity') for run in runs for child in run.get('delegations', [])}
    fast_provenance = dict.fromkeys(('matching_native_candidates', 'unforked_candidates',
                                    'forked_or_foreign_candidates', 'header_count'), 0)
    fast_representations = []
    fast_identity_components = []
    fast_terminal_turns = []
    fast_command_witnesses = []
    paths = (home / ("sessions" if provider == "codex" else "projects")).rglob("*.jsonl")
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
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
                categories = raw_tool_categories(provider, rows)
                for key in root_calls:
                    root_calls[key] += categories[key]
            fast_candidate = any(fast_native_identity(provider, rows, identity, run, home, root / 'primary')
                                 for run in runs)
            if (provider == 'codex' and identity in known_children
                    and any(fast_decision_lines(assistant_text(provider, row)) for row in rows)):
                for run in runs:
                    if (len(fast_identity_components) < 12 and run.get('provider') == provider
                            and any(child.get('identity') == identity for child in run.get('delegations', []))):
                        fast_identity_components.append(codex_fast_identity_probe(rows, identity, run))
            if fast_candidate:
                fast_provenance['matching_native_candidates'] += 1
                fast_provenance['header_count'] += sum(row.get('type') == 'session_meta' for row in rows)
                verified = worker_transcript_is_unforked(provider, rows, identity)
                fast_provenance['unforked_candidates' if verified else 'forked_or_foreign_candidates'] += 1
                fast_representations.append(decision_representations(provider, rows, identity))
                fast_command_witnesses.append(command_witness_probe(provider, rows, root / 'primary'))
                if provider == 'codex':
                    bound_runs = [run for run in runs if fast_native_identity(provider, rows, identity, run, home, root / 'primary')]
                    if len(bound_runs) == 1:
                        fast_terminal_turns.append(codex_fast_terminal_probe(document, bound_runs[0],
                                                  rows, identity, home, root / 'primary'))
                categories = raw_tool_categories(provider, rows)
                if verified:
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
        for phase in json.loads(phase_path.read_text(encoding="utf-8")):
            if phase.get('phase') in {'enable', 'objective', 'reconcile', 'stop'}:
                  phases.append({'phase': phase['phase'], 'resume': phase.get('resume') is True,
                               'returned': phase.get('returned') is True,
                                 'cli_success': phase.get('cli_success') is True})
    archived_probes = []
    if provider == 'claude':
        from native_claude_followup import sequence_rejection_probe
        for run in runs:
            session = run.get('session_id')
            if run.get('provider') != provider or not isinstance(session, str) or not session:
                continue
            pending = []
            for path in (root / 'state').glob('.session-*.json'):
                try:
                    record = read_state_snapshot(path)
                    if record.get('owner_session') == session:
                        pending.extend(Event(item['event_id'], item['kind'], item['observed_at'], item['payload'])
                                       for item in record.get('pending', ()))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if pending:
                archived_probes.append(sequence_rejection_probe(_state_from_dict(document), pending, session,
                    root / 'primary', {'CLAUDE_CONFIG_DIR': str(home)}, host_evidence, mixed=True))
    return {"enabled": document.get("enabled") is True,
            "activation_guarded": activation.get("state") == "guarded",
            "profile": activation.get("profile") if activation.get("profile") in {"latest", "full", "opus-5-5"} else "unknown",
            "loaded_version": activation.get("plugin_version") if re.fullmatch(r"\d+\.\d+\.\d+", str(activation.get("plugin_version"))) else "unknown",
            "boost_levels": sorted({value for value in boosts if isinstance(value, str) and value in {"off", "xhigh", "max", "ultra"}}),
            "callbacks": counts, "native_signals": native_signals,
            'fast_raw_tool_categories': fast_calls, 'fast_native_provenance': fast_provenance,
            'fast_decision_representations': fast_representations,
            **({'fast_identity_components': fast_identity_components} if provider == 'codex' else {}),
            'fast_terminal_turns': fast_terminal_turns,
            'fast_command_witnesses': fast_command_witnesses,
            'assessed_completion': [assessed_completion_probe(provider, home, run, document) for run in runs
                                    if run.get('provider') == provider and run.get('assessment', {}).get('size')],
            'root_native_launches': root_launches,
            'root_native_tool_categories': root_calls,
            'lead_integration': [lead_integration_probe(provider, run, home, root / 'primary')
                for run in runs if run.get('provider') == provider and run.get('assessment', {}).get('size')],
            'assessed_first_authority': [assessed_first_probe(provider, document, run, home, root / 'primary')
                for run in runs if run.get('provider') == provider and run.get('assessment', {}).get('size')],
              **({'claude_phases': claude_phase_diagnostics(root, home)} if provider == 'claude' else {}),
              **({'claude_archived_sequence': archived_probes} if provider == 'claude' else {}),
            'claude_completion': [claude_completion_probe(document, run.get('session_id'), root / 'primary', home)
                for run in document.get('active_runs', {}).values()
                if provider == 'claude' and run.get('provider') == provider and run.get('status') == 'completing'],
            "lifecycle": lifecycle_diagnostics(provider, root, document), 'phases': phases}


def claude_completion_probe(document, session, project, home):
    """Trace completing freshness using fixed facts, never native IDs or text."""
    state = _state_from_dict(document)
    run = state.active_runs.get(f'claude:{session}')
    archived = False
    if run is None:
        run = next((item for item in reversed(state.recent_runs)
                    if item.provider == 'claude' and item.session_id == session), None)
        archived = run is not None
    if run is None:
        return {'freshness': 'no_active_run'}
    # Read-only replay of freshness for a successfully archived receipt. This
    # never dispatches lifecycle or restores the run in its durable store.
    state = replace(state, active_run=replace(run, status='completing') if archived else run)
    captured = {'has_recovery_anchor': bool(run.assessment.get('_claude_native_recovery')),
                'from_completed_archive': archived}
    readers = {host_evidence._claude_native_lead_event.__code__: 'native_reader',
               host_evidence._claude_native_prompt_activity.__code__: 'prompt_activity'}
    def trace(frame, event, value):
        kind = readers.get(frame.f_code)
        if kind is None:
            return None
        if event == 'return':
            values = frame.f_locals
            facts = {'source_line': frame.f_lineno,
                     'result': value[0] if kind == 'prompt_activity' else 'accepted' if value else 'rejected'}
            launched, started = values.get('launched_at'), values.get('started_at')
            if isinstance(launched, datetime) and isinstance(started, datetime):
                facts['launch_minus_run_start_ms'] = round((launched - started).total_seconds() * 1000, 3)
                facts['launch_before_run_start'] = launched < started
            meta, launch = values.get('meta'), values.get('launch')
            if isinstance(meta, dict) and isinstance(launch, dict):
                facts['root_launch_id_matches_meta'] = launch.get('id') == meta.get('toolUseId')
                facts['accepted_fast_launch_anchor_matches'] = host_evidence._claude_observed_fast_launch(
                    run, str(meta.get('toolUseId') or ''),
                    values.get('root_prompt_id', values.get('root_prompt')))
                launch_hash = run.assessment.get('_claude_fast_launch_hash')
                prompt_hash = run.assessment.get('_claude_fast_root_prompt_hash')
                prompt = values.get('root_prompt_id', values.get('root_prompt'))
                fast = run.assessment.get('_fast_route')
                fast = fast if isinstance(fast, dict) else {}
                route = run.assessment.get('route', {})
                route = route if isinstance(route, dict) else {}
                lead = next((item for item in run.delegations
                             if item.role == 'lead' and item.identity == run.lead_identity), None)
                valid_hash = lambda value: isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None
                prior_prompts = values.get('root_prompts', ())
                facts.update(launch_hash_present='_claude_fast_launch_hash' in run.assessment,
                    launch_hash_valid=valid_hash(launch_hash),
                    launch_hash_matches_native_meta=valid_hash(launch_hash) and isinstance(meta.get('toolUseId'), str)
                        and hashlib.sha256(meta['toolUseId'].encode()).hexdigest() == launch_hash,
                    root_prompt_hash_present='_claude_fast_root_prompt_hash' in run.assessment,
                    root_prompt_hash_valid=valid_hash(prompt_hash),
                    root_prompt_hash_matches_selected_native_prompt=valid_hash(prompt_hash) and isinstance(prompt, str)
                        and hashlib.sha256(prompt.encode()).hexdigest() == prompt_hash,
                    prior_native_root_prompt_count=len(prior_prompts),
                    root_prompt_hash_matches_any_prior_native_prompt=valid_hash(prompt_hash) and any(
                        isinstance(item, str) and hashlib.sha256(item.encode()).hexdigest() == prompt_hash
                        for item in prior_prompts),
                    selected_native_root_prompt_available=isinstance(prompt, str),
                    genuine_fast_owner=host_evidence._archived_fast_owner(run), provider_matches=run.provider == 'claude',
                    fast_metadata_present=bool(fast), prior_escalated=bool(run.assessment.get('_fast_escalated')),
                    direct_topology=run.assessment.get('topology') in {None, '', 'direct'}, canonical_lead_present=lead is not None,
                    lead_route_matches_fast=lead is not None and (lead.requested_tier, lead.requested_effort) ==
                        (fast.get('model'), fast.get('effort')),
                    accepted_route_matches_fast=route.get('lead_model', fast.get('model')) == fast.get('model')
                        and route.get('lead_effort', fast.get('effort')) == fast.get('effort'))
            parent = values.get('parent')
            if isinstance(parent, dict):
                facts['root_cwd_match'] = Path(str(parent.get('cwd') or '')).resolve() == project.resolve()
            prompts = [row for row in values.get('child_rows', ()) if row.get('type') == 'user'
                       and isinstance(row.get('message', {}).get('content'), str)]
            first = host_evidence._instant(prompts[0].get('timestamp')) if prompts else None
            if first and isinstance(started, datetime):
                facts['first_child_prompt_before_run_start'] = first < started
            facts['stage'] = ('accepted' if facts['result'] in {'accepted', 'single', 'multiple'} else
                'child_before_hook' if facts.get('first_child_prompt_before_run_start') else
                'launch_before_hook' if facts.get('launch_before_run_start')
                    and not facts.get('accepted_fast_launch_anchor_matches') else
                'terminal' if 'terminal' in values else 'child_turn' if 'prompt_indices' in values or 'prompts' in values else
                'root_launch' if 'launches' in values else 'metadata' if 'meta' in values else
                'paths' if 'paths' in values else 'state')
            captured[kind] = facts
        return trace
    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        captured['freshness'] = host_evidence.claude_completing_lead_turn(
            state, session, project, {'CLAUDE_CONFIG_DIR': str(home)})[0]
    except (OSError, TypeError, ValueError, AttributeError) as error:
        captured['freshness'] = 'probe_error'
        captured['error_type'] = type(error).__name__
    finally:
        sys.settrace(previous)
    return captured


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
    for run in (*document.get('active_runs', {}).values(), *document.get('recent_runs', [])):
        if run.get('provider') != provider:
            continue
        assessment = run.get('assessment', {})
        known = {run.get('session_id'), run.get('lead_identity'),
                 *(child.get('identity') for child in run.get('delegations', []))} - {None, ''}
        kinds = dict.fromkeys(('subagent_started', 'subagent_stopped', 'other'), 0)
        matches = dict.fromkeys(('agent_known', 'parent_known', 'session_known', 'turn_active',
                                'turn_terminal', 'generation_current', 'ambiguous_owner'), 0)
        records = dict.fromkeys(('root', 'alias', 'missing', 'overflow'), 0)
        pending_terminals = []
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
                if provider == 'codex' and kind == 'subagent_stopped' and len(pending_terminals) < 12:
                    pending_terminals.append(codex_pending_terminal_probe(root, document, run, record, entry))
        conditions = {key: bool(assessment.get('_' + key)) for key in (
            'batch_pending', 'ambiguous_child_starts', 'ambiguous_child_stops',
            'pending_delegations', 'invalid_consultants', 'lead_route_mismatch', 'pending_lead_completion')}
        conditions['active_children'] = any(child.get('state') in {'working', 'pending', 'running', 'active'}
                                            for child in run.get('delegations', []))
        conditions['interrupted_children'] = any(child.get('state') == 'interrupted'
                                                 for child in run.get('delegations', []))
        conditions['successful_outcome'] = (run.get('outcome') or {}).get('status') in {'completed', 'done', 'success', 'succeeded'}
        result.append({'archived_owner': run not in document.get('active_runs', {}).values(),
                       'pending_kinds': kinds, 'invocation_matches': matches, 'records': records,
                       'stop_conditions': conditions,
                       **({'pending_terminals': pending_terminals,
                           'pending_terminal_details_truncated': kinds['subagent_stopped'] > len(pending_terminals)}
                          if provider == 'codex' else {})})
    return result


def codex_pending_terminal_probe(root, document, current, record, entry):
    """Read an original inbox event against retained evidence, without ACK or dispatch."""
    payload = entry.get('payload', {})
    identity = payload.get('agent_id') or payload.get('subagent_id')
    owner, lead = current.get('session_id'), current.get('lead_identity')
    token = _child_turn_token(payload)
    event = Event(entry.get('event_id', ''), entry.get('kind', ''), entry.get('observed_at', ''), payload)
    observed, started = host_evidence._instant(event.observed_at), host_evidence._instant(current.get('started_at'))
    known = {child.get('identity') for child in current.get('delegations', [])} - {None, ''}
    classify = lambda value: ('missing' if not value else 'root' if value == owner else
        'canonical_lead' if value == lead else 'known_child' if value in known else 'other')
    bound = (payload.get('provider') == record.get('provider') == 'codex'
        and record.get('owner_session') == owner
        and record.get('state_name') == StateStore(root / 'state')._path(root / 'primary').name
        and entry.get('generation') == record.get('generation')
        and payload.get('session_id') in {owner, record.get('session')}
        and (record.get('session') == owner or record.get('session') == identity))
    raw_result = _terminal_result_id(event)
    scoped_result = _terminal_result_id(replace(event, payload={**payload, 'session_id': owner})) if bound else None
    facts = {'source': {'event_id_present': isinstance(event.event_id, str) and bool(event.event_id),
        'observed_at_valid': observed is not None, 'event_before_current_run': bool(observed and started and observed < started),
        'provider_matches': payload.get('provider') == 'codex', 'agent_present': isinstance(identity, str) and bool(identity),
        'parent_category': classify(payload.get('parent_thread_id')),
        'session_category': 'bound_alias' if bound and payload.get('session_id') != owner else classify(payload.get('session_id')),
        'generation_current': entry.get('generation') == record.get('generation'),
        'ambiguous_owner': entry.get('ambiguous_owner') is True, 'turn_kind': _child_turn_kind(payload),
        'turn_present': bool(token), 'root_normalization_bound': bound}, 'runs': {}, 'receipts': {}, 'native': {}}
    runs = [*document.get('active_runs', {}).values(), *document.get('recent_runs', [])]
    compatible = document.get('active_run')
    if isinstance(compatible, dict) and compatible not in runs:
        runs.append(compatible)
    counts = dict.fromkeys(('retained', 'provider_root', 'foreign_root_agent',
        'current_canonical', 'current_superseded', 'current_delegation',
        'prior_canonical', 'prior_superseded', 'prior_delegation',
        'current_active_turn', 'current_terminal_turn', 'prior_active_turn', 'prior_terminal_turn',
        'current_native_start_anchor', 'prior_native_start_anchor',
        'current_substantive_start_anchor', 'prior_substantive_start_anchor',
        'current_terminal_result', 'prior_terminal_result'), 0)
    for run in runs:
        counts['retained'] += 1
        children = [child for child in run.get('delegations', []) if child.get('identity') == identity]
        if run.get('provider') != 'codex' or run.get('session_id') != owner:
            counts['foreign_root_agent'] += bool(children) or run.get('lead_identity') == identity
            continue
        counts['provider_root'] += 1
        scope = 'current' if run.get('run_id') == current.get('run_id') else 'prior'
        counts[scope + '_canonical'] += run.get('lead_identity') == identity
        counts[scope + '_superseded'] += any(child.get('role') in {'lead', 'rejected_lead'}
            and run.get('lead_identity') != identity for child in children)
        counts[scope + '_delegation'] += bool(children)
        assessment = run.get('assessment', {})
        counts[scope + '_active_turn'] += bool(token) and assessment.get('_active_turns', {}).get(identity) == token
        counts[scope + '_terminal_turn'] += bool(token) and token in assessment.get('_terminal_turns', {}).get(identity, [])
        native_start = (hashlib.sha256(('codex-host-turn\0' + identity + '\0' + str(payload.get('turn_id'))).encode()).hexdigest()
                        if isinstance(identity, str) and payload.get('turn_id') else None)
        counts[scope + '_native_start_anchor'] += bool(native_start) and any(value in assessment.get('_start_event_ids', ())
            for value in (native_start, native_start + ':followup-start'))
        proof = assessment.get('_substantive_children', {}).get(identity, {})
        counts[scope + '_substantive_start_anchor'] += (bool(proof.get('start_event_id'))
            and proof.get('turn') == token and proof['start_event_id'] in assessment.get('_start_event_ids', ()))
        counts[scope + '_terminal_result'] += any(value in assessment.get('_terminal_event_ids', ())
            for value in (event.event_id, raw_result, scoped_result) if value)
    facts['runs'] = counts
    receipt_counts = dict.fromkeys(('all', 'provider', 'provider_root', 'agent', 'turn',
        'raw_result', 'scoped_result', 'parent', 'canonical_lead', 'same_run', 'prior_run',
        'same_run_result_parent_raw', 'same_run_result_parent_scoped',
        'prior_run_result_parent_raw', 'prior_run_result_parent_scoped',
        'same_run_all_fields_raw', 'same_run_all_fields_scoped',
        'prior_run_all_fields_raw', 'prior_run_all_fields_scoped', 'retained_canonical_lead'), 0)
    retained_ids = {run.get('run_id') for run in runs if run.get('provider') == 'codex' and run.get('session_id') == owner}
    for receipt in document.get('terminal_receipts', []):
        receipt_counts['all'] += 1
        if receipt.get('provider') != payload.get('provider'):
            continue
        receipt_counts['provider'] += 1
        if receipt.get('session') != owner:
            continue
        receipt_counts['provider_root'] += 1
        if receipt.get('agent') != identity:
            continue
        receipt_counts['agent'] += 1
        if receipt.get('turn') != token:
            continue
        receipt_counts['turn'] += 1
        raw, scoped = receipt.get('result') == raw_result, scoped_result is not None and receipt.get('result') == scoped_result
        parent = receipt.get('parent') == str(payload.get('parent_thread_id') or '')
        receipt_counts['raw_result'] += raw
        receipt_counts['scoped_result'] += scoped
        receipt_counts['parent'] += parent
        receipt_counts['canonical_lead'] += receipt.get('lead') == lead
        scope = ('same_run' if receipt.get('run_id') == current.get('run_id') else
                 'prior_run' if receipt.get('run_id') in retained_ids else None)
        if scope:
            receipt_counts[scope] += 1
            receipt_counts[scope + '_result_parent_raw'] += raw and parent
            receipt_counts[scope + '_result_parent_scoped'] += scoped and parent
            retained_lead = any(run.get('provider') == 'codex' and run.get('session_id') == owner
                and run.get('run_id') == receipt.get('run_id') and run.get('lead_identity') == receipt.get('lead') for run in runs)
            receipt_counts['retained_canonical_lead'] += retained_lead
            receipt_counts[scope + '_all_fields_raw'] += raw and parent and retained_lead
            receipt_counts[scope + '_all_fields_scoped'] += scoped and parent and retained_lead
    facts['receipts'] = receipt_counts
    history = document.get('event_history', [])
    facts['history'] = {'source_event_exact': sum(item.get('event_id') == event.event_id for item in history),
        'derived_terminal_exact': sum(item.get('event_id') == event.event_id + ':delegation:delegation_updated'
            or isinstance(item.get('event_id'), str) and re.fullmatch(re.escape(event.event_id)
                + r':terminal-epoch:\d+:delegation:delegation_updated', item['event_id']) is not None for item in history)}
    native = facts['native']
    native['source'] = 'identity_unavailable'
    if not isinstance(identity, str) or not host_evidence._CODEX_ID.fullmatch(identity):
        return facts
    try:
        rows = native_rows('codex', root / 'codex-baseline-home', identity)
    except (RuntimeError, OSError, ValueError):
        native['source'] = 'missing_or_ambiguous'
        return facts
    headers = [row.get('payload', {}) for row in rows if row.get('type') == 'session_meta']
    first = rows[0].get('payload', {}) if rows and rows[0].get('type') == 'session_meta' else {}
    parent = first.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
    cwd = first.get('cwd')
    native.update(source='available', header_count=len(headers), own_first_header=first.get('id') == identity,
        unforked=worker_transcript_is_unforked('codex', rows, identity),
        parent_category=classify(parent), parent_matches_callback=parent == payload.get('parent_thread_id'),
        cwd_absolute=isinstance(cwd, str) and Path(cwd).is_absolute(),
        cwd_matches_fixture=isinstance(cwd, str) and Path(cwd).is_absolute()
            and Path(cwd).resolve() == (root / 'primary').resolve())
    basename = str(first.get('agent_path') or '').rsplit('/', 1)[-1]
    native['task_role'] = next((role for role, prefix in (
        ('fast_lead', 'symphony_lead_fast_'), ('assessor', 'symphony_assessor_'),
        ('lead', 'symphony_lead_'), ('worker', 'symphony_worker_'), ('consultant', 'symphony_consultant_'))
        if basename.startswith(prefix)), 'other')
    if not native['own_first_header'] or not native['unforked']:
        return facts
    turn_id = payload.get('turn_id')
    selected = [(row, row.get('payload', {})) for row in rows if turn_id
                and row.get('payload', {}).get('turn_id') == turn_id]
    starts = [row for row, value in selected if row.get('type') == 'event_msg' and value.get('type') == 'task_started']
    contexts = [value for row, value in selected if row.get('type') == 'turn_context']
    completions = [row for row, value in selected if row.get('type') == 'event_msg' and value.get('type') == 'task_complete']
    failures = [row for row, value in selected if row.get('type') == 'event_msg'
                and value.get('type') in {'error', 'task_failed', 'turn_aborted', 'task_interrupted'}]
    native.update(start_count=len(starts), context_count=len(contexts), completed_count=len(completions),
        failed_count=len(failures), unique_completed_turn=len(starts) == len(contexts) == len(completions) == 1 and not failures,
        report_matches_callback=len(completions) == 1
            and completions[0]['payload'].get('last_agent_message') == payload.get('last_assistant_message'),
        context_model_matches_callback=len(contexts) == 1 and contexts[0].get('model') == payload.get('model'),
        context_effort_matches_callback=len(contexts) == 1 and contexts[0].get('effort') == payload.get('model_reasoning_effort'))
    all_starts = [row.get('payload', {}).get('turn_id') for row in rows if row.get('type') == 'event_msg'
                  and row.get('payload', {}).get('type') == 'task_started']
    latest = all_starts[-1] if all_starts else None
    native['newer_started_turn'] = bool(latest and turn_id and latest != turn_id)
    native['latest_turn_unfinished'] = bool(latest and not any(row.get('type') == 'event_msg'
        and row.get('payload', {}).get('type') == 'task_complete'
        and row.get('payload', {}).get('turn_id') == latest for row in rows))
    native['retained_child_route_matches'] = sum(len(contexts) == 1
        and contexts[0].get('model') == child.get('requested_tier')
        and contexts[0].get('effort') == child.get('requested_effort')
        for run in runs if run.get('provider') == 'codex' and run.get('session_id') == owner
        for child in run.get('delegations', []) if child.get('identity') == identity)
    if len(starts) == len(completions) == 1:
        began, completed = host_evidence._instant(starts[0].get('timestamp')), host_evidence._instant(completions[0].get('timestamp'))
        native['chronology_valid'] = bool(began and completed and began <= completed)
        native['completion_before_callback'] = bool(completed and observed and completed <= observed)
    return facts


def claude_root_rows(home, session):
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', session):
        return None
    paths = list((home / 'projects').glob(f'*/{session}.jsonl'))
    return _native_jsonl(paths[0]) if len(paths) == 1 else None


def claude_phase_diagnostics(root, home):
    """Fixed per-CLI-phase observations; private stdout/native text is never exported."""
    path = root / 'logs' / 'phases.json'
    if not path.is_file():
        return []
    phases = json.loads(path.read_text(encoding='utf-8'))
    output = []
    for phase in phases[:8]:
        if phase.get('phase') not in {'enable', 'objective', 'reconcile', 'stop'}:
            continue
        facts = {'phase': phase['phase'], 'returned': phase.get('returned') is True,
                 'cli_success': phase.get('cli_success') is True, 'stdout_range_available': False,
                 'result_objects': 0, 'result_error': False, 'budget_exceeded': False,
                 'native_range_available': False, 'root_final_end_turns': 0,
                 'root_tools': dict.fromkeys(('Agent', 'Bash', 'Read', 'Write', 'Edit', 'Skill', 'other'), 0)}
        begin, end = phase.get('stdout_begin'), phase.get('stdout_end')
        try:
            stdout = root / 'logs' / 'stdout'
            if type(begin) is int and type(end) is int and 0 <= begin <= end <= stdout.stat().st_size and end - begin <= 2 * 1024 * 1024:
                with stdout.open('rb') as stream:
                    stream.seek(begin)
                    text = stream.read(end - begin).decode('utf-8')
                facts['stdout_range_available'] = True
                for line in text.splitlines():
                    try:
                        result = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(result, dict) and result.get('type') == 'result':
                        facts['result_objects'] += 1
                        facts['result_error'] |= result.get('is_error') is True
                        facts['budget_exceeded'] |= result.get('subtype') == 'error_max_budget_usd'
            rows = claude_root_rows(home, phase.get('root_session'))
            first, last = phase.get('root_rows_before'), phase.get('root_rows_after')
            if rows is not None and type(first) is int and type(last) is int and 0 <= first <= last <= len(rows):
                scoped = [row for row in rows[first:last] if row.get('type') in {'assistant', 'user'}]
                if all(row.get('sessionId') == phase.get('root_session') and not row.get('agentId')
                       and row.get('isSidechain') is not True for row in scoped):
                    facts['native_range_available'] = True
                    for row in scoped:
                        message = row.get('message', {})
                        facts['root_final_end_turns'] += row.get('type') == 'assistant' and message.get('stop_reason') == 'end_turn'
                        for block in message.get('content', []) if isinstance(message.get('content'), list) else ():
                            if isinstance(block, dict) and block.get('type') == 'tool_use':
                                name = block.get('name')
                                facts['root_tools'][name if name in facts['root_tools'] else 'other'] += 1
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            facts['source_error'] = True
        output.append(facts)
    return output


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


def claude_fast_launch_identity(home, project, identity, run, fast):
    """Match the accepted fast launch to one native root Agent call."""
    session = run.get('session_id')
    pinned = run.get('assessment', {}).get('_claude_fast_launch_hash')
    if (home is None or project is None or run.get('provider') != 'claude'
            or not isinstance(pinned, str) or not re.fullmatch(r'[0-9a-f]{64}', pinned)
            or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value)
                   for value in (session, identity))):
        return False
    paths = list((home / 'projects').glob(f'*/{session}/subagents/agent-{identity}.jsonl'))
    if len(paths) != 1:
        return False
    meta_path = paths[0].with_suffix('.meta.json')
    try:
        if meta_path.is_symlink() or meta_path.stat().st_size > 64 * 1024:
            return False
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        launch_id = meta.get('toolUseId')
        agent_type = meta.get('agentType')
        if (type(meta.get('spawnDepth')) is not int or meta['spawnDepth'] != 1
                or not isinstance(launch_id, str) or not launch_id
                or hashlib.sha256(launch_id.encode()).hexdigest() != pinned
                or not isinstance(agent_type, str)
                or not agent_type.endswith(f"symphony-lead-{fast['model']}-{fast['effort']}")):
            return False
        parent = _native_jsonl(paths[0].parent.parent.with_suffix('.jsonl'))
        if parent is None:
            return False
        launches = [(row, item) for row in parent
                    for item in row.get('message', {}).get('content', [])
                    if isinstance(item, dict) and item.get('id') == launch_id]
        if len(launches) != 1:
            return False
        row, launch = launches[0]
        values = launch.get('input')
        cwd = row.get('cwd')
        return (row.get('type') == 'assistant' and row.get('sessionId') == session
                and row.get('isSidechain') is not True and not row.get('agentId')
                and isinstance(cwd, str) and Path(cwd).is_absolute()
                and Path(cwd).resolve() == project.resolve()
                and launch.get('type') == 'tool_use' and launch.get('name') == 'Agent'
                and isinstance(values, dict) and values.get('subagent_type') == agent_type
                and ('model' not in values or values['model'] == fast['model']))
    except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
        return False


def codex_fast_identity_probe(rows, identity, run):
    """Explain pre-filter identity failures without using them as admission proof."""
    children = [child for child in run.get('delegations', []) if child.get('identity') == identity]
    child = children[0] if children else {}
    fast = run.get('assessment', {}).get('_fast_route', {})
    header = rows[0].get('payload', {}) if rows and rows[0].get('type') == 'session_meta' else {}
    spawn = header.get('source', {}).get('subagent', {}).get('thread_spawn', {})
    model, effort = fast.get('model'), fast.get('effort')
    route_present = isinstance(model, str) and bool(model) and isinstance(effort, str) and bool(effort)
    expected = 'symphony_lead_fast_' + re.sub(r'\W', '_', model) + '_' + effort if route_present else None
    path, nested_path = header.get('agent_path'), spawn.get('agent_path')
    return {'known_child_count': len(children), 'lead_role': child.get('role') == 'lead',
        'fast_route_present': route_present,
        'model_matches_fast': route_present and child.get('requested_tier') == model,
        'effort_matches_fast': route_present and child.get('requested_effort') == effort,
        'own_first_header': bool(header) and header.get('id') == identity,
        'header_count': sum(row.get('type') == 'session_meta' for row in rows),
        'unforked': worker_transcript_is_unforked('codex', rows, identity),
        'top_level_path_present': isinstance(path, str) and bool(path),
        'top_level_basename_exact': isinstance(path, str) and path.rsplit('/', 1)[-1] == expected,
        'top_level_fast_prefix': isinstance(path, str) and path.rsplit('/', 1)[-1].startswith('symphony_lead_fast_'),
        'nested_path_present': isinstance(nested_path, str) and bool(nested_path),
        'nested_basename_exact': isinstance(nested_path, str) and nested_path.rsplit('/', 1)[-1] == expected,
        'root_parent_matches': bool(run.get('session_id')) and spawn.get('parent_thread_id') == run['session_id']}


def fast_native_identity(provider, rows, identity, run, home=None, project=None):
    """A shared model/effort pair cannot identify the fast child."""
    child = next((item for item in run.get('delegations', []) if item.get('identity') == identity), None)
    fast = run.get('assessment', {}).get('_fast_route', {})
    if (not child or child.get('role') != 'lead' or not fast.get('model') or not fast.get('effort')
            or (child.get('requested_tier'), child.get('requested_effort')) != (fast['model'], fast['effort'])):
        return False
    if provider == 'codex':
        # Use the first own native header; copied parent headers never select a
        # fast identity. The separate unforked gate controls usable evidence.
        if not rows or rows[0].get('type') != 'session_meta':
            return False
        header = rows[0].get('payload', {})
        spawn = header.get('source', {}).get('subagent', {}).get('thread_spawn', {})
        expected = 'symphony_lead_fast_' + re.sub(r'\W', '_', fast['model']) + '_' + fast['effort']
        return (header.get('id') == identity and isinstance(header.get('agent_path'), str)
                and re.fullmatch(re.escape('/root/' + expected) + r'(?:_[a-z0-9_]+)?', header['agent_path']) is not None
                and spawn.get('agent_path', header['agent_path']) == header['agent_path']
                and spawn.get('parent_thread_id') == run.get('session_id'))
    return (claude_fast_launch_identity(home, project, identity, run, fast)
            and all(row.get('agentId') == identity and row.get('sessionId') == run.get('session_id')
                    and row.get('isSidechain') is True for row in rows))


def decision_representations(provider, rows, identity):
    """Count native representation ambiguity without exporting IDs or text."""
    result = dict.fromkeys(('marker_lines', 'marker_records', 'nonempty_message_ids',
                          'own_turn_bound_records', 'equivalent_same_id_records',
                          'conflicting_same_id_records', 'missing_message_ids'), 0)
    result['phases'] = dict.fromkeys(('final_answer', 'commentary', 'end_turn', 'tool_use', 'other'), 0)
    own = worker_transcript_is_unforked(provider, rows, identity)
    current_turn = ''
    seen = {}
    marker_turns = set()
    for row in rows:
        payload = row.get('payload', {}) if provider == 'codex' else row.get('message', {})
        if provider == 'codex' and (row.get('type') == 'turn_context' or
                row.get('type') == 'event_msg' and payload.get('type') == 'task_started'):
            current_turn = payload.get('turn_id') if isinstance(payload.get('turn_id'), str) else ''
        text = assistant_text(provider, row)
        decisions = fast_decision_lines(text)
        if not decisions:
            continue
        result['marker_records'] += 1
        result['marker_lines'] += len(decisions)
        phase = payload.get('phase') if provider == 'codex' else payload.get('stop_reason')
        result['phases'][phase if phase in result['phases'] else 'other'] += 1
        message_id = payload.get('id') if provider == 'codex' else row.get('uuid')
        nonempty = isinstance(message_id, str) and bool(message_id)
        result['nonempty_message_ids' if nonempty else 'missing_message_ids'] += 1
        bound = own and bool(current_turn) if provider == 'codex' else (
            row.get('agentId') == identity and row.get('isSidechain') is True)
        result['own_turn_bound_records'] += bound
        if bound and current_turn:
            marker_turns.add(current_turn)
        if not nonempty or not bound:
            continue
        key = (identity, current_turn, message_id)
        content = (json.dumps(payload.get('content'), sort_keys=True), phase)
        if key in seen:
            result['equivalent_same_id_records' if seen[key] == content else 'conflicting_same_id_records'] += 1
        else:
            seen[key] = content
    result['distinct_marker_turns'] = len(marker_turns)
    return result


def codex_command_turns_verified(document, run_values, rows, identity, home, project):
    """Verify one command turn, or its single proven same-task native followup."""
    if (run_values.get('provider') != 'codex' or run_values.get('status') != 'completed'
            or run_values.get('lead_identity') != identity
            or run_values.get('outcome', {}).get('status') != 'completed'
            or not fast_native_identity('codex', rows, identity, run_values)
            or not worker_transcript_is_unforked('codex', rows, identity)):
        return None
    run = _run_from_dict(run_values)
    native = host_evidence._native_lead_turns(ProjectState(active_run=run), run.session_id,
                                            {'CODEX_HOME': str(home)})
    if native is None:
        return None
    _, turns, order, latest, _ = native
    if len(order) not in {1, 2} or set(turns) != set(order) or latest != order[-1]:
        return None
    raw = {token: {'start': [], 'context': [], 'complete': [], 'markers': []} for token in order}
    current = ''
    for row in rows:
        payload = row.get('payload', {})
        if row.get('type') == 'event_msg' and payload.get('type') in {
                'turn_aborted', 'task_failed', 'task_interrupted', 'error'}:
            return None
        kind = ('context' if row.get('type') == 'turn_context' else
                'start' if row.get('type') == 'event_msg' and payload.get('type') == 'task_started' else
                'complete' if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete' else '')
        if kind:
            token = payload.get('turn_id')
            if not isinstance(token, str) or not token or token not in raw:
                return None
            raw[token][kind].append(row)
            if kind in {'start', 'context'}:
                current = token
        text = assistant_text('codex', row)
        if any(line.strip().startswith('SYMPHONY_FAST_DECISION:') for line in text.splitlines()):
            if (current not in raw or payload.get('phase') != 'final_answer'
                    or not isinstance(payload.get('id'), str) or not payload['id']
                    or payload.get('turn_id', current) != current):
                return None
            raw[current]['markers'].append(row)
    previous = None
    last_timestamp = None
    marker_ids = set()
    for token in order:
        records, turn = raw[token], turns[token]
        if (any(len(records[kind]) != 1 for kind in ('start', 'context', 'complete', 'markers'))
                or turn.get('failed') or not turn.get('started') or turn.get('completed_at') is None
                or turn.get('model') != run.assessment.get('_fast_route', {}).get('model')
                or turn.get('effort') != run.assessment.get('_fast_route', {}).get('effort')):
            return None
        began, completed = turn.get('started_at'), turn.get('completed_at')
        context_at = host_evidence._instant(records['context'][0].get('timestamp'))
        marker_at = host_evidence._instant(records['markers'][0].get('timestamp'))
        if (not began or not context_at or not marker_at or not began <= context_at <= marker_at <= completed
                or previous and not previous < began):
            return None
        report = turn.get('message')
        marker_id = records['markers'][0]['payload']['id']
        if marker_id in marker_ids:
            return None
        marker_ids.add(marker_id)
        if (not isinstance(report, str) or fast_decision_lines(report) != ['eligible']
                or sum(line.strip().startswith('SYMPHONY_FAST_DECISION:') for line in report.splitlines()) != 1
                or assistant_text('codex', records['markers'][0]) != report):
            return None
        # Durable receipts are already accepted callback evidence. A synthetic
        # minimal event cannot reproduce their result hash's extra fields.
        receipts = [item for item in document.get('terminal_receipts', ())
                    if item.get('provider') == 'codex' and item.get('session') == run.session_id
                    and item.get('agent') == identity and item.get('turn') == 'turn_id:' + token]
        if (len(receipts) != 1 or receipts[0].get('run_id') != run.run_id
                or receipts[0].get('lead') != identity or receipts[0].get('parent') != run.session_id
                or receipts[0].get('status') not in {'completed', 'done', 'success', 'succeeded'}
                or not isinstance(receipts[0].get('result'), str)
                or re.fullmatch(r'[0-9a-f]{64}', receipts[0]['result']) is None):
            return None
        if previous:
            native_event = Event('command-proof', 'subagent_stopped', completed.isoformat(), {
                'model': turn['model'], 'model_reasoning_effort': turn['effort'],
                '_symphony_native_started_at': began.isoformat()})
            if not host_evidence._codex_root_followup(replace(run, updated_at=previous.isoformat()),
                native_event, project, {'CODEX_HOME': str(home)}):
                return None
        previous = completed
        last_timestamp = records['markers'][0]['timestamp']
    return identity, 'eligible', last_timestamp


def codex_fast_terminal_probe(document, run_values, rows, identity, home, project):
    """Read canonical turn/report evidence for diagnostics only, never acceptance."""
    result = {'native_reader_available': False, 'started_turns': 0, 'completed_turns': 0,
              'failed_turns': 0, 'newer_unfinished_turn': False, 'turns': [],
              'extra_turns_with_exact_root_followup': 0}
    run = replace(_run_from_dict(run_values), lead_identity=identity)
    native = host_evidence._native_lead_turns(ProjectState(active_run=run), run.session_id,
                                             {'CODEX_HOME': str(home)})
    if native is None:
        return result
    _, turns, order, latest, _ = native
    result['native_reader_available'] = True
    result['started_turns'] = sum(turn.get('started') is True for turn in turns.values())
    result['completed_turns'] = sum(turn.get('completed_at') is not None for turn in turns.values())
    result['failed_turns'] = sum(turn.get('failed') is True for turn in turns.values())
    result['newer_unfinished_turn'] = bool(latest and not turns.get(latest, {}).get('completed_at'))
    current = ''
    messages, reports = {}, {}
    for row in rows:
        payload = row.get('payload', {})
        if (row.get('type') == 'turn_context' or row.get('type') == 'event_msg'
                and payload.get('type') == 'task_started'):
            current = payload.get('turn_id', '')
        text = assistant_text('codex', row)
        if fast_decision_lines(text):
            messages.setdefault(current, []).append(text)
        if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
            reports[payload.get('turn_id')] = reports.get(payload.get('turn_id'), 0) + 1
    previous = None
    for ordinal, token in enumerate(order[:12]):
        turn = turns[token]
        message = turn.get('message')
        decisions = fast_decision_lines(message) if isinstance(message, str) else []
        bound = [receipt for receipt in document.get('terminal_receipts', [])
                 if receipt.get('provider') == 'codex' and receipt.get('session') == run.session_id
                 and receipt.get('run_id') == run.run_id and receipt.get('agent') == identity
                 and receipt.get('turn') == 'turn_id:' + token]
        payload = {'provider': 'codex', 'session_id': run.session_id, 'agent_id': identity,
                   'turn_id': token, 'status': 'completed', 'model': turn.get('model'),
                   'model_reasoning_effort': turn.get('effort'), 'last_assistant_message': message}
        event = Event('diagnostic', 'subagent_stopped',
                      turn['completed_at'].isoformat() if turn.get('completed_at') else '', payload)
        result['turns'].append({'ordinal': ordinal, 'report_count': reports.get(token, 0),
            'marker_count': len(decisions), 'decision': decisions[0] if len(decisions) == 1 else 'ambiguous_or_absent',
            'matching_marker_records': sum(text == message for text in messages.get(token, [])),
            'marker_records': len(messages.get(token, [])), 'receipt_turn_matches': bool(bound),
            'canonical_event_result_matches_receipt': any(receipt.get('result') == _terminal_result_id(event) for receipt in bound),
            'model_matches': turn.get('model') == run.assessment.get('_fast_route', {}).get('model'),
            'effort_matches': turn.get('effort') == run.assessment.get('_fast_route', {}).get('effort')})
        if previous and turn.get('started_at') and turn.get('completed_at'):
            followup = replace(event, payload={**payload, '_symphony_native_started_at': turn['started_at'].isoformat()})
            result['extra_turns_with_exact_root_followup'] += host_evidence._codex_root_followup(
                replace(run, updated_at=previous.isoformat()), followup, project, {'CODEX_HOME': str(home)})
        if turn.get('completed_at'):
            previous = turn['completed_at']
    return result


def bash_unittest_shape(command, project=None):
    """A finite literal Bash grammar; never execute or expand its arguments."""
    facts = dict.fromkeys(('bound_cwd_prefix_present', 'bound_cwd_matches', 'stderr_merge_present',
                          'interpreter_family_matches', 'absolute_interpreter', 'discover_arguments',
                          'specific_test_arguments', 'unsupported_shell_operators', 'finite_bash_shape_supported'), False)
    if not isinstance(command, str):
        return facts
    source = command.strip()
    redirected = re.sub(r'\s+2>&1$', '', source)
    facts['stderr_merge_present'] = redirected != source
    source = redirected
    cd = re.match(r'''^cd\s+(?P<cwd>'[^']*'|"[^"$`]*"|[^\s;&|<>`$]+)\s+&&\s+''', source)
    if cd:
        facts['bound_cwd_prefix_present'] = True
        try:
            cwd = shlex.split(cd['cwd'])[0]
            facts['bound_cwd_matches'] = (project is not None and Path(cwd).is_absolute()
                                         and Path(cwd).resolve() == Path(project).resolve())
        except (OSError, ValueError, IndexError):
            pass
        source = source[cd.end():]
    facts['unsupported_shell_operators'] = bool(re.search(r'[;&|<>`$\r\n]', source))
    try:
        tokens = shlex.split(source)
    except ValueError:
        return facts
    if tokens:
        facts['interpreter_family_matches'] = re.fullmatch(r'python(?:3|\.exe)?', tokens[0]) is not None
        facts['absolute_interpreter'] = Path(tokens[0]).is_absolute()
    if len(tokens) >= 3 and tokens[1:3] == ['-m', 'unittest']:
        facts['discover_arguments'] = 'discover' in tokens[3:]
        facts['specific_test_arguments'] = any(token not in {'-q', '-v', 'discover'} for token in tokens[3:])
    literal = re.fullmatch(r'python(?:3|\.exe)?\s+-m\s+unittest(?:\s+-(?:q|v))*', source)
    facts['finite_bash_shape_supported'] = (literal is not None and not facts['unsupported_shell_operators']
        and not re.search(r'[$`\r\n]', command)
        and (not cd or facts['bound_cwd_matches']))
    return facts


def command_witness_probe(provider, rows, project=None):
    """Categorize direct native unittest calls/results without exporting arguments or output."""
    result = dict.fromkeys(('direct_shell_calls', 'exact_supported_unittest_calls', 'matched_results',
                           'missing_results', 'error_results', 'ran_tests_present', 'ok_present',
                           'failed_present', 'explicit_exit_zero', 'explicit_exit_nonzero', 'exit_unreported',
                           'all_direct_matched_results', 'all_direct_missing_results', 'all_direct_error_results',
                           'all_direct_ran_tests_present', 'all_direct_ok_present', 'all_direct_failed_present',
                           'argv_parse_failures', 'unittest_token_sequence_present', 'duplicate_call_ids'), 0)
    calls, outputs = {}, {}
    bash_shapes = {key: 0 for key in bash_unittest_shape('')}
    direct_calls = []
    for row in rows:
        value = row.get('payload', {}) if provider == 'codex' else row.get('message', {})
        blocks = [value] if provider == 'codex' else value.get('content', [])
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get('type') in {'tool_use', 'function_call'} and block.get('name') in {'Bash', 'exec_command'}:
                result['direct_shell_calls'] += 1
                call_id = block.get('id', block.get('call_id'))
                result['duplicate_call_ids'] += bool(call_id and call_id in direct_calls)
                direct_calls.append(call_id)
                args = block.get('input', block.get('arguments'))
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                    tokens = shlex.split(args.get('command', args.get('cmd', '')))
                except (AttributeError, TypeError, ValueError):
                    result['argv_parse_failures'] += 1
                    continue
                result['unittest_token_sequence_present'] += any(tokens[index:index + 2] == ['-m', 'unittest']
                                                               for index in range(len(tokens) - 1))
                shape = bash_unittest_shape(args.get('command', ''), project) if provider == 'claude' else None
                if shape:
                    for key, value in shape.items():
                        bash_shapes[key] += value
                if (shape and shape['finite_bash_shape_supported'] or provider != 'claude'
                        and len(tokens) >= 3 and re.fullmatch(r'python(?:3|\.exe)?', tokens[0])
                        and tokens[1:3] == ['-m', 'unittest'] and all(token in {'-q', '-v'} for token in tokens[3:])):
                    result['exact_supported_unittest_calls'] += 1
                    calls[block.get('id', block.get('call_id'))] = True
            elif block.get('type') in {'tool_result', 'function_call_output'}:
                outputs[block.get('tool_use_id', block.get('call_id'))] = block
    for identity in direct_calls:
        output = outputs.get(identity) if identity else None
        if output is None:
            result['all_direct_missing_results'] += 1
            continue
        result['all_direct_matched_results'] += 1
        result['all_direct_error_results'] += output.get('is_error') is True or output.get('isError') is True
        value = output.get('content', output.get('output', ''))
        text = value if isinstance(value, str) else json.dumps(value)
        result['all_direct_ran_tests_present'] += bool(re.search(r'Ran [1-9]\d* tests?', text))
        result['all_direct_ok_present'] += 'OK' in text
        result['all_direct_failed_present'] += 'FAILED' in text
    for identity in calls:
        output = outputs.get(identity) if identity else None
        if output is None:
            result['missing_results'] += 1
            continue
        result['matched_results'] += 1
        result['error_results'] += output.get('is_error') is True or output.get('isError') is True
        value = output.get('content', output.get('output', ''))
        text = value if isinstance(value, str) else json.dumps(value)
        result['ran_tests_present'] += bool(re.search(r'Ran [1-9]\d* tests?', text))
        result['ok_present'] += 'OK' in text
        result['failed_present'] += 'FAILED' in text
        try:
            structured = json.loads(text)
            code = structured.get('exit_code') if isinstance(structured, dict) else None
        except ValueError:
            code = None
        result['explicit_exit_zero' if type(code) is int and code == 0 else
               'explicit_exit_nonzero' if type(code) is int else 'exit_unreported'] += 1
    if provider == 'claude':
        result['bash_shapes'] = bash_shapes
    return result


def install_private_child_capture(root, candidate=PLUGIN):
    """Keep original callback payloads private for exact diagnostic replay."""
    directory = root / 'private-child-hooks'
    directory.mkdir(mode=0o700)
    script = root / 'capture_claude_hook.py'
    anchor = 'provider = sys.argv[3]\n'
    source = script.read_text(encoding='utf-8')
    require(source.count(anchor) == 1, 'private callback capture has no unique insertion point')
    extra = (
        "if provider == 'claude' and event in {'SubagentStart', 'SubagentStop'}:\n"
        "    private_capture = {'raw': payload, 'canonical': None}\n"
        "    try:\n"
        f"        sys.path.insert(0, {str(candidate.resolve())!r})\n"
        "        from symphony.adapters import event_from_payload\n"
        "        private_event = event_from_payload('claude', payload)\n"
        "        private_capture['canonical'] = {'event_id': private_event.event_id, 'kind': private_event.kind, 'payload': private_event.payload}\n"
        "    except (OSError, ValueError, TypeError, AttributeError, ImportError):\n"
        "        pass\n"
        f"    private_file = pathlib.Path({str(directory)!r}) / (invocation + '.json')\n"
        "    descriptor = os.open(private_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
        "    with os.fdopen(descriptor, 'w', encoding='utf-8') as private_stream:\n"
        "        json.dump(private_capture, private_stream)\n")
    script.write_text(source.replace(anchor, anchor + extra), encoding='utf-8')


def claude_child_binding_probe(home, run_dict, document):
    """Trace binding only when exact admitted callback sources remain available."""
    try:
        run = _run_from_dict(run_dict)
    except (ValueError, TypeError):
        return [{'source_stage': 'run_unavailable'}]
    proofs = run.assessment.get('_substantive_children', {})
    history = document.get('event_history', ()) if isinstance(document, dict) else ()
    receipts = document.get('terminal_receipts', ()) if isinstance(document, dict) else ()
    sources = {}
    captures = []
    for path in (home.parent / 'private-child-hooks').glob('*.json'):
        try:
            captured = json.loads(path.read_text(encoding='utf-8'))
            snapshot = captured.get('canonical') if isinstance(captured, dict) else None
            payload = captured.get('raw') if isinstance(captured, dict) and 'raw' in captured else captured
            if not isinstance(payload, dict) or payload.get('hook_event_name') not in {'SubagentStart', 'SubagentStop'}:
                continue
            if isinstance(captured, dict) and 'raw' in captured:
                canonical = snapshot.get('payload') if isinstance(snapshot, dict) else None
                expected_kind = 'subagent_started' if payload['hook_event_name'] == 'SubagentStart' else 'subagent_stopped'
                if (not isinstance(canonical, dict) or snapshot.get('kind') != expected_kind
                        or canonical.get('provider') != 'claude'
                        or set(canonical) - set(payload) - {'provider', 'last_assistant_message'}
                        or set(payload) - set(canonical)
                        or any(canonical.get(key) != value for key, value in payload.items()
                               if key not in {'provider', 'last_assistant_message'})
                        or snapshot.get('event_id') != hashlib.sha256(json.dumps(
                            canonical, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()):
                    captures.append((payload, None, False))
                    continue
                # Reuse the immutable hook-time normalization, including its
                # handback report. Never re-read the child's later transcript.
                event = Event(snapshot['event_id'], expected_kind, '', canonical)
            else:
                # Older private fixtures contain raw callbacks only. Their
                # reconstruction still needs an exact durable source ID.
                event = replace(event_from_payload('claude', payload), observed_at='')
            # The observer clock is not the product's callback admission time.
            # Require the exact canonical source ID in durable derived history.
            matches = [record for record in history if record.get('kind') == 'delegation_updated'
                       and (record.get('event_id') == event.event_id + ':delegation:delegation_updated'
                            or re.fullmatch(re.escape(event.event_id) + r':terminal-epoch:\d+:delegation:delegation_updated',
                                            record.get('event_id', '')) is not None)]
            admitted = len(matches) == 1 and host_evidence._instant(matches[0].get('observed_at')) is not None
            if admitted:
                event = replace(event, observed_at=matches[0]['observed_at'])
                sources[event.event_id] = event
            captures.append((payload, event, admitted))
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
            continue
    results = []
    for ordinal, (identity, proof) in enumerate(proofs.items() if isinstance(proofs, dict) else ()):
        if ordinal >= 12 or not isinstance(proof, dict):
            continue
        facts = {'ordinal': ordinal, 'source_stage': 'source_unavailable'}
        start = sources.get(proof.get('start_event_id'))
        if (start is None or start.kind != 'subagent_started' or start.observed_at != proof.get('admitted_at')
                or (start.payload.get('agent_id') or start.payload.get('subagent_id')) != identity
                or start.payload.get('session_id') != run.session_id
                or _child_turn_token(start.payload) != proof.get('turn')):
            facts['source_stage'] = 'admitted_start_unavailable'
            results.append(facts)
            continue
        filters = dict.fromkeys(('captured_stops_for_child', 'canonical_snapshot_unavailable', 'canonical_source_available',
            'canonical_id_history_matches', 'root_session', 'child_alias_session', 'other_session',
            'proof_turn_matches', 'timestamp_order_matches', 'receipt_owner_matches', 'receipt_turn_matches',
            'raw_result_hash_matches', 'scoped_result_hash_matches', 'duplicate_exact_sources'), 0)
        candidates = []
        for payload, event, admitted in captures:
            if (payload.get('hook_event_name') != 'SubagentStop'
                    or (payload.get('agent_id') or payload.get('subagent_id')) != identity):
                continue
            filters['captured_stops_for_child'] += 1
            if event is None:
                filters['canonical_snapshot_unavailable'] += 1
                continue
            filters['canonical_source_available'] += 1
            if not admitted:
                continue
            filters['canonical_id_history_matches'] += 1
            session = event.payload.get('session_id')
            filters['root_session' if session == run.session_id else
                    'child_alias_session' if session == identity else 'other_session'] += 1
            terminal_token = _child_turn_token(event.payload)
            if session not in {run.session_id, identity}:
                continue
            filters['proof_turn_matches'] += terminal_token == proof.get('turn')
            if event.observed_at < start.observed_at:
                continue
            filters['timestamp_order_matches'] += 1
            owner_receipts = [receipt for receipt in receipts if receipt.get('provider') == 'claude'
                and receipt.get('session') == run.session_id and receipt.get('run_id') == run.run_id
                and receipt.get('agent') == identity]
            if not owner_receipts:
                continue
            filters['receipt_owner_matches'] += 1
            turn_receipts = [receipt for receipt in owner_receipts if receipt.get('turn') == terminal_token]
            if len(turn_receipts) != 1:
                continue
            filters['receipt_turn_matches'] += 1
            filters['raw_result_hash_matches'] += turn_receipts[0].get('result') == _terminal_result_id(event)
            # Dispatch keeps the original event ID but scopes its payload to
            # the admitted root before hashing a terminal result. Mirror that
            # only after exact durable source and receipt-owner checks.
            scoped = replace(event, payload={**event.payload, 'session_id': run.session_id})
            if turn_receipts[0].get('result') != _terminal_result_id(scoped):
                continue
            filters['scoped_result_hash_matches'] += 1
            if scoped in candidates:
                filters['duplicate_exact_sources'] += 1
            else:
                candidates.append(scoped)
        facts['terminal_source_filters'] = filters
        if len(candidates) != 1:
            facts['source_stage'] = 'terminal_source_unavailable' if not candidates else 'ambiguous_terminal_sources'
            results.append(facts)
            continue
        terminal = candidates[0]
        role = proof.get('role')
        facts.update(source_stage='exact_start_and_terminal',
            start_token_kind=_child_turn_kind(start.payload),
            terminal_token_kind=_child_turn_kind(terminal.payload),
            hook_tokens_equal=proof.get('turn') == _child_turn_token(terminal.payload),
            scoped_token_compatible=_substantive_turn_matches(run.provider, proof.get('turn'),
                _child_turn_token(terminal.payload), proof.get('turn_kind'), _child_turn_kind(terminal.payload)),
            immutable_role_matches=_observed_role(terminal.payload) in {'', role}
                and _observed_role(start.payload) in {'', role},
            start_parent_consistent=proof.get('start_parent') in {'', run.lead_identity},
            terminal_parent_consistent=terminal.payload.get('parent_thread_id') in {None, '', run.lead_identity},
            source_start_parent_matches=proof.get('start_parent') == str(start.payload.get('parent_thread_id') or ''),
            current_scope=proof.get('run_id') == run.run_id and proof.get('lead') == run.lead_identity
                and proof.get('owner_generation') == run.owner_generation
                and proof.get('epoch') == run.assessment.get('substantive_contract', {}).get('epoch'),
            source_provider_matches=start.payload.get('provider') == terminal.payload.get('provider') == run.provider,
            source_session_matches=start.payload.get('session_id') == terminal.payload.get('session_id') == run.session_id)
        def trace(frame, event, value):
            if frame.f_code is not host_evidence._claude_substantive_launch.__code__:
                return None
            if event == 'return':
                values = frame.f_locals
                facts.update(source_line=frame.f_lineno, binding_reader_accepted=value is not None,
                    stage='accepted' if value else 'prompt_identity_timing' if 'prompt' in values else
                    'child_prompts' if 'prompts' in values else 'cwd' if 'source_cwd' in values else
                    'native_launch' if 'launches' in values else 'metadata' if 'metadata' in values else
                    'paths' if 'paths' in values else 'input')
                child, lead = values.get('child'), values.get('lead')
                child_meta, lead_meta = values.get('child_meta'), values.get('lead_meta')
                if isinstance(child_meta, dict) and isinstance(lead_meta, dict):
                    facts.update(child_depth_two=type(child_meta.get('spawnDepth')) is int and child_meta['spawnDepth'] == 2,
                        lead_depth_one=type(lead_meta.get('spawnDepth')) is int and lead_meta['spawnDepth'] == 1,
                        child_type_matches=child is not None and isinstance(child_meta.get('agentType'), str)
                            and child_meta['agentType'].endswith(f'symphony-{role}-{child.requested_tier}-{child.requested_effort}'),
                        lead_type_matches=lead is not None and isinstance(lead_meta.get('agentType'), str)
                            and lead_meta['agentType'].endswith(f'symphony-lead-{lead.requested_tier}-{lead.requested_effort}'))
                if 'metadata' in values:
                    facts['metadata_read_count'] = len(values['metadata'])
                if 'paths' in values:
                    facts['matching_native_child_paths'] = len(values['paths'])
                if 'launches' in values:
                    facts['matching_native_launch_count'] = len(values['launches'])
                parent, launch = values.get('parent'), values.get('launch')
                if isinstance(parent, dict) and isinstance(launch, dict):
                    inputs = launch.get('input')
                    inputs = inputs if isinstance(inputs, dict) else {}
                    native_cwd, source_cwd = parent.get('cwd'), terminal.payload.get('cwd')
                    facts.update(parent_session_matches=parent.get('sessionId') == run.session_id,
                        parent_agent_matches=parent.get('agentId') == run.lead_identity,
                        parent_sidechain=parent.get('isSidechain') is True,
                        exact_agent_call=launch.get('type') == 'tool_use' and launch.get('name') == 'Agent',
                        explicit_model_present='model' in inputs,
                        explicit_model_matches=child is not None and ('model' not in inputs or inputs['model'] == child.requested_tier),
                        subagent_type_matches=inputs.get('subagent_type') == values.get('child_type'),
                        source_cwd_absolute=isinstance(source_cwd, str) and bool(source_cwd) and Path(source_cwd).is_absolute(),
                        native_cwd_absolute=isinstance(native_cwd, str) and bool(native_cwd) and Path(native_cwd).is_absolute(),
                        cwd_matches=isinstance(source_cwd, str) and isinstance(native_cwd, str)
                            and Path(source_cwd).resolve() == Path(native_cwd).resolve())
                for label, left, right in (('launch_minus_accepted_ms', 'launched', 'accepted'),
                                           ('launch_minus_admitted_ms', 'launched', 'admitted'),
                                           ('prompt_minus_launch_ms', 'when', 'launched'),
                                           ('prompt_minus_callback_ms', 'when', 'observed')):
                    if isinstance(values.get(left), datetime) and isinstance(values.get(right), datetime):
                        facts[label] = round((values[left] - values[right]).total_seconds() * 1000, 3)
                if 'prompts' in values:
                    facts['textual_prompt_count'] = len(values['prompts'])
                if 'child_rows' in values:
                    facts['native_user_row_count'] = sum(row.get('type') == 'user' for row in values['child_rows'])
                    unsupported = 0
                    for row in values['child_rows']:
                        if row.get('type') != 'user':
                            continue
                        message = row.get('message')
                        content = message.get('content') if isinstance(message, dict) else None
                        textual = isinstance(content, str) or isinstance(content, list) and bool(content) and all(
                            isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)
                            for item in content)
                        tool_result = isinstance(content, list) and bool(content) and any(
                            isinstance(item, dict) and item.get('type') == 'tool_result' for item in content) and all(
                            isinstance(item, dict) and (item.get('type') == 'tool_result' or item.get('type') == 'text'
                                and isinstance(item.get('text'), str)) for item in content)
                        unsupported += not textual and not tool_result
                    facts['unsupported_native_user_rows'] = unsupported
                prompt = values.get('prompt')
                if isinstance(prompt, dict):
                    facts.update(prompt_session_matches=prompt.get('sessionId') == run.session_id,
                        prompt_agent_matches=prompt.get('agentId') == identity,
                        prompt_sidechain=prompt.get('isSidechain') is True,
                        prompt_uuid_nonempty=isinstance(prompt.get('uuid'), str) and bool(prompt['uuid']))
            return trace
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            host_evidence.claude_substantive_launch(run, terminal, role, proof['admitted_at'],
                                                   {'CLAUDE_CONFIG_DIR': str(home)})
        except (OSError, ValueError, TypeError, AttributeError) as error:
            facts['stage'] = 'probe_error'
            facts['error_type'] = type(error).__name__
        finally:
            sys.settrace(previous)
        results.append(facts)
    return results


def assessed_completion_probe(provider, home, run, document=None):
    """Separate admitted child proof from supplied lead-result categories."""
    assessment = run.get('assessment', {})
    contract = assessment.get('substantive_contract')
    proofs = assessment.get('_substantive_children', {})
    counts = dict.fromkeys(('worker_starts', 'consultant_starts', 'parent_bound', 'successful', 'current_scope'), 0)
    for proof in proofs.values() if isinstance(proofs, dict) else ():
        if not isinstance(proof, dict):
            continue
        for role in ('worker', 'consultant'):
            counts[role + '_starts'] += proof.get('role') == role
        counts['parent_bound'] += proof.get('parent') == run.get('lead_identity')
        counts['successful'] += proof.get('successful') is True
        counts['current_scope'] += (isinstance(contract, dict) and proof.get('epoch') == contract.get('epoch')
            and proof.get('run_id') == run.get('run_id') and proof.get('lead') == run.get('lead_identity')
            and proof.get('owner_generation') == run.get('owner_generation'))
    outcome_statuses = dict.fromkeys(('completed', 'failed', 'blocked', 'malformed', 'other'), 0)
    marker_count = 0
    latest_model_matches = False
    latest_effort_matches = False
    native_prompt_count = 0
    try:
        rows = native_rows(provider, home, run.get('lead_identity'))
        assistant_rows = [row for row in rows if assistant_text(provider, row)]
        native_prompt_count = sum(row.get('type') == 'user' and (
            isinstance(row.get('message', {}).get('content'), str) or
            isinstance(row.get('message', {}).get('content'), list) and bool(row['message']['content'])
            and all(isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
                    for block in row['message']['content'])) for row in rows) if provider == 'claude' else sum(
            row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_started' for row in rows)
        for row in assistant_rows:
            for line in assistant_text(provider, row).splitlines():
                line = line.strip()
                if not line.startswith('SYMPHONY_OUTCOME:'):
                    continue
                marker_count += 1
                try:
                    value = json.loads(line.partition(':')[2])
                    status = value.get('status') if isinstance(value, dict) else 'malformed'
                    category = status if status in {'completed', 'failed', 'blocked'} else 'other'
                except (ValueError, TypeError):
                    category = 'malformed'
                outcome_statuses[category] += 1
        child = next((item for item in run.get('delegations', []) if item.get('identity') == run.get('lead_identity')), {})
        latest_model_matches = bool(assistant_rows and provider == 'claude' and
            assistant_rows[-1].get('message', {}).get('model') == child.get('requested_tier'))
        if provider == 'claude':
            paths = tuple((home / 'projects').glob(f'*/{run.get("session_id")}/subagents/agent-{run.get("lead_identity")}.meta.json'))
            if len(paths) == 1:
                label = json.loads(paths[0].read_text(encoding='utf-8')).get('agentType')
                latest_effort_matches = isinstance(label, str) and label.endswith(
                    f'symphony-lead-{child.get("requested_tier")}-{child.get("requested_effort")}')
        else:
            contexts = [row.get('payload', {}) for row in rows if row.get('type') == 'turn_context']
            latest_model_matches = bool(contexts and contexts[-1].get('model') == child.get('requested_tier'))
            latest_effort_matches = bool(contexts and contexts[-1].get('effort') == child.get('requested_effort'))
    except (OSError, ValueError, RuntimeError, TypeError):
        pass
    return {'claude_child_binding': claude_child_binding_probe(home, run, document) if provider == 'claude' else [],
            'contract_present': 'substantive_contract' in assessment,
            'contract_version_one': isinstance(contract, dict) and type(contract.get('version')) is int and contract['version'] == 1,
            'child_proof_counts': counts, 'retryable_lead_present': bool(assessment.get('_retryable_lead')),
            'substantive_child_missing': assessment.get('_substantive_child_missing') is True,
            'route_mismatch_present': bool(assessment.get('_lead_route_mismatch')),
            'supplied_outcome_markers': marker_count, 'supplied_outcome_categories': outcome_statuses,
            'native_prompt_or_started_turn_count': native_prompt_count,
            'latest_native_assistant_model_matches': latest_model_matches,
            'latest_native_effort_label_matches': latest_effort_matches}


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
            launched_name = arguments.get("task_name")
            try:
                returned = json.loads(output)
            except (TypeError, ValueError):
                continue
            # Native Codex encrypts packets and returns a task path, not UUID.
            # Bind that exact path to the child's own header and canonical parent.
            if (isinstance(returned, dict) and returned.get("task_name") == header.get("agent_path")
                      and isinstance(launched_name, str)
                      and re.fullmatch(re.escape(worker_name) + r"(?:_[a-z0-9_]+)?", launched_name)
                      and parent_path and header.get("agent_path") == parent_path + "/" + launched_name
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
JS_STRING = r'''(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''


def composed_call(source, method, *, output_only=False, serialized=False):
    """Recognize only a single literal native tool invocation, never evaluate JS."""
    if not isinstance(source, str):
        return None
    source = re.sub(r'^\s*// @exec:[^\n]*\n', '', source).strip()
    argument = JS_STRING if method == "apply_patch" else rf'''\{{(?:{JS_STRING}|[^{{}}"'])*\}}'''
    call = rf'tools\.{method}\((?P<argument>{argument})\)'
    wrappers = [rf'text\(await {call}\);?',
                rf'const (?P<result>\w+)\s*=\s*await {call};\s*text\((?P=result)\);?']
    if serialized:
        wrappers.append(rf'const (?P<result>\w+)\s*=\s*await {call};\s*text\(JSON\.stringify\((?P=result)\)\);?')
    if output_only:
        wrappers.append(rf'const (?P<result>\w+)\s*=\s*await {call};\s*text\((?P=result)\.output\);?')
    for wrapper in wrappers:
        match = re.fullmatch(wrapper, source, re.DOTALL)
        if match:
            value = match['argument']
            if method == "exec_command":
                value = re.sub(JS_STRING + r'|([{,]\s*)([A-Za-z_]\w*)(\s*:)',
                    lambda match: match[0] if match[1] is None else f'{match[1]}"{match[2]}"{match[3]}', value)
            try:
                def json_string(match):
                    literal = match[0]
                    if literal.startswith("'"):
                        body = re.sub(r'''\\.|"''', lambda part: "'" if part[0] == r"\'"
                                      else r'\"' if part[0] == '"' else part[0], literal[1:-1])
                        return json.dumps(json.loads('"' + body + '"'))
                    return literal
                value = re.sub(JS_STRING, json_string, value)
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
        if provider == 'codex' and name in {'exec_command', 'exec', 'functions.exec'} and not patch:
            details = arguments if name == 'exec_command' else composed_call(arguments, 'exec_command', output_only=True)
            if not isinstance(details, dict):
                continue
            shell = re.fullmatch(r"apply_patch <<'(?P<delimiter>[A-Z_]+)'\n(?P<patch>\*\*\* Begin Patch\n.*\n\*\*\* End Patch)\n(?P=delimiter)",
                                 details.get('cmd', ''), re.DOTALL)
            if (not shell or shell['delimiter'] in shell['patch'].splitlines()
                    or Path(details.get('workdir', project)).resolve() != project.resolve()):
                continue
            try:
                native = json.loads(output)
                if name in {'exec', 'functions.exec'}:
                    if (not isinstance(native, list) or len(native) != 2
                            or not native[0].get('text', '').startswith('Script completed')
                            or native[1].get('type') != 'input_text'):
                        continue
                    native = native[1]['text']
                    try:
                        native = json.loads(native)
                    except ValueError:
                        pass
                positive_result = ((isinstance(native, dict) and type(native.get('exit_code')) is int
                                    and native['exit_code'] == 0 and 'Success. Updated the following files:' in native.get('output', ''))
                                   or (isinstance(native, str) and native.startswith('Exit code: 0\n')
                                       and 'Success. Updated the following files:' in native))
                patch = shell['patch']
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if positive_result and isinstance(patch, str) and patch.startswith("*** Begin Patch\n") and patch.rstrip().endswith("*** End Patch"):
            paths = re.findall(r'^\*\*\* (?:Update|Add) File: (.+)$', patch, re.MULTILINE)
            if any((Path(path) if Path(path).is_absolute() else project / path).resolve() == target.resolve() for path in paths):
                return True
    return False


def fast_turn_has_only_escalation(provider, rows, evidence, identity=''):
    """Every raw tool call counts; even a failed command can modify a file."""
    if provider == "codex":
        return (worker_transcript_is_unforked(provider, rows, identity)
                and not any(row.get("payload", {}).get("type") in {"function_call", "custom_tool_call"} for row in rows))
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


def unittest_verified(evidence, project=None):
    for name, arguments, _, output in evidence:
        if name in {"exec", "functions.exec"} and observed_composed_unittest(arguments, output):
            return True
        details = arguments if name in {"Bash", "exec_command"} else composed_call(arguments, "exec_command") if name in {"exec", "functions.exec"} else None
        if not isinstance(details, dict):
            continue
        if name == 'Bash':
            if not bash_unittest_shape(details.get('command'), project)['finite_bash_shape_supported']:
                continue
            try:
                result = json.loads(output)
            except (TypeError, ValueError):
                result = output
            if isinstance(result, list) and all(isinstance(block, dict) and block.get('type') == 'text'
                                              and isinstance(block.get('text'), str) for block in result):
                result = '\n'.join(block['text'] for block in result)
            if (isinstance(result, str) and re.search(r'(?m)^Ran [1-9]\d* tests? in \d+(?:\.\d+)?s\r?$', result)
                    and re.search(r'(?m)^OK\r?$', result) and 'FAILED' not in result):
                return True
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


def native_cli_command(provider, executable, project, session, prompt, budget, resume=False):
    if provider == 'codex':
        return [executable, 'exec', *(['resume'] if resume else []), '--dangerously-bypass-hook-trust',
                '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                '--model', 'gpt-6-luna', '-c', 'model_reasoning_effort="low"',
                '-c', 'features.multi_agent=true', *([session] if resume else ['-C', str(project)]), prompt]
    return [executable, '--print', '--model', 'haiku', '--max-budget-usd', str(budget),
            '--permission-mode', 'bypassPermissions', '--output-format', 'json',
            '--resume' if resume else '--session-id', session, prompt]


def check_case(provider, root, candidate, case, timeout, budget, requested_profile=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SYMPHONY_") and key not in {"CODEX_SESSION_ID", "CLAUDECODE"}}
    env.update(prepare_baseline_capture(provider, root, candidate))
    if provider == 'claude':
        install_private_child_capture(root, candidate)
    project, _ = projects(root, False)
    greeting = project / "greet.py"
    greeting.write_text("def greet(name):\n    return f'" + ("hello" if case == "run-and-fix" else "Hello") + ", {name}!'\n")
    (project / "test_greet.py").write_text(TEST)
    instructions = FIXTURE_COMMAND_INSTRUCTIONS if case == 'command' else FIXTURE_INSTRUCTIONS
    (project / "AGENTS.md").write_text(instructions)
    (project / "CLAUDE.md").write_text(instructions)
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
    prompt = CASES[case] + (' This is the required fast-eligibility escalation probe: first relay the complete '
        'request to the offered fast lead. That lead must use NO tools and make NO changes before returning '
        'SYMPHONY_FAST_DECISION: escalate. Only then request independent assessment and bounded delegation.'
        if case == 'feature' else ' This is the required assessor-first probe: request independent assessment '
        'before launching any lead or worker. Then launch the selected canonical assessed lead and let that '
        'lead delegate the fix to a bounded worker.' if case == 'run-and-fix' else '')
    if case != 'command':
        prompt += (' Relay the fixture integration check unchanged: after every worker returns successfully, '
            'the canonical assessed lead itself must run exactly python -m unittest -q in a standalone native '
            'tool call and return the full successful result. A worker test or git diff is insufficient.')
    logs = root / "logs"
    logs.mkdir()
    phases = []
    deadline = time.monotonic() + timeout
    last_unchanged_ns = time.time_ns()
    first_change_after_ns = None

    def run(prompt, resume=False, phase='objective'):
        nonlocal last_unchanged_ns, first_change_after_ns
        phases.append({'phase': phase, 'resume': resume, 'returned': False, 'cli_success': False})
        if provider == 'claude':
            home = Path(env['CLAUDE_CONFIG_DIR'])
            rows = claude_root_rows(home, session)
            phases[-1].update(root_session=session, root_rows_before=len(rows) if rows is not None else
                0 if not list((home / 'projects').glob(f'*/{session}.jsonl')) else None)
        (logs / 'phases.json').write_text(json.dumps(phases))
        command = native_cli_command(provider, executable, project, session, prompt, budget, resume)
        with (logs / "stdout").open("a", encoding="utf-8") as output, (logs / "stderr").open("a", encoding="utf-8") as errors:
            if provider == 'claude':
                phases[-1]['stdout_begin'] = output.tell()
                (logs / 'phases.json').write_text(json.dumps(phases), encoding='utf-8')
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
        if provider == 'claude':
            rows = claude_root_rows(Path(env['CLAUDE_CONFIG_DIR']), session)
            phases[-1].update(stdout_end=(logs / 'stdout').stat().st_size,
                             root_rows_after=len(rows) if rows is not None else None)
        (logs / 'phases.json').write_text(json.dumps(phases))
        require(process.returncode == 0, "native semantic CLI failed")

    run(setup_prompt(provider), phase='enable')
    state_path = state_file(state_dir, project)
    require(state_path.is_file(), "installed enable hook did not create durable state")
    version = json.loads((candidate / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    assert_enabled_fixture(read_state_snapshot(state_path), provider, profile, version)
    run(prompt, provider == "claude")
    if provider == "codex":
        matches = re.findall(r"^session id: ([0-9a-f-]+)$", (logs / "stderr").read_text(encoding="utf-8"), re.MULTILINE)
        require(len(matches) == 2 and matches[0] != matches[1], "native enable/objective sessions are missing or reused")
        session = matches[-1]
    require(state_path.is_file(), "installed native hooks did not create durable state")
    document = read_state_snapshot(state_path)
    owner = f"{provider}:{session}"
    active = lambda doc: doc.get("active_runs", {}).get(owner)
    if provider == "claude" and active(document):
        # Background native results may need a same-session root turn to reconcile.
        run("Continue this Symphony session after its background results; reconcile the existing work and finish.", True, 'reconcile')
        document = read_state_snapshot(state_path)
        if (active(document) or {}).get("status") == "completing":
            run("/symphony:stop", True, 'stop')
            document = read_state_snapshot(state_path)
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
    receipt = verify_case_run(provider, case, document, run_state, home, project, greeting,
                              initial_hash, first_change_after_ns, fast)
    if case == 'command':
        original = run_state
        original_receipts = tuple(document.get('terminal_receipts', ()))
        # A new user objective is distinct from continuation of the old child.
        # The first archive and empty inbox have already been verified above.
        run('New wholly mechanical Symphony objective: run python -m unittest -q again and report the result. '
            'The preceding objective is archived. Use a fresh fast lead for this new objective, '
            'without resuming the preceding child.', True, 'objective')
        document = read_state_snapshot(state_path)
        if provider == 'claude' and active(document):
            run('Reconcile only the new mechanical objective and finish after its background result.', True, 'reconcile')
            document = read_state_snapshot(state_path)
            if (active(document) or {}).get('status') == 'completing':
                run('/symphony:stop', True, 'stop')
                document = read_state_snapshot(state_path)
        require(active(document) is None, 'second mechanical objective did not archive')
        fresh = fresh_command_run_verified(document, original, provider, session, original_receipts,
                                           store, greeting, initial_hash)
        second = verify_case_run(provider, case, document, fresh, home, project, greeting,
                                 initial_hash, first_change_after_ns, fast)
        receipt['fresh_objectives_same_root'] = 2
        receipt['second_fresh_objective'] = second
    return receipt


def fresh_command_run_verified(document, original, provider, session, original_receipts,
                               store, greeting, initial_hash):
    runs = [item for item in document.get('recent_runs', [])
            if item.get('provider') == provider and item.get('session_id') == session]
    require(len(runs) == 2 and sum(item == original for item in runs) == 1,
            'second mechanical objective changed or reused the first archived run')
    fresh = next(item for item in runs if item != original)
    old_ids = {item.get('identity') for item in original.get('delegations', [])}
    new_ids = {item.get('identity') for item in fresh.get('delegations', [])}
    require(fresh.get('run_id') != original.get('run_id') and fresh.get('status') == 'completed'
            and fresh.get('outcome', {}).get('status') == 'completed'
            and old_ids and new_ids and not old_ids.intersection(new_ids)
            and host_evidence._instant(fresh.get('started_at')) is not None
            and host_evidence._instant(original.get('updated_at')) is not None
            and host_evidence._instant(fresh['started_at']) > host_evidence._instant(original['updated_at']),
            'second mechanical objective has no fresh postarchive owner and identity')
    require(all(receipt in document.get('terminal_receipts', ()) for receipt in original_receipts),
            'second objective changed the first terminal receipts')
    old_receipts = [item for item in original_receipts if item.get('run_id') == original['run_id']
                    and item.get('provider') == provider and item.get('session') == session]
    new_receipts = [item for item in document.get('terminal_receipts', ()) if item.get('run_id') == fresh['run_id']
                    and item.get('provider') == provider and item.get('session') == session]
    require(old_receipts and new_receipts and all(item.get('agent') in new_ids for item in new_receipts)
            and not {item.get('result') for item in old_receipts}.intersection(
                item.get('result') for item in new_receipts), 'new objective inherited terminal receipt evidence')
    for identity in (session, *store.aliases_for_owner(provider, session)):
        record = store.session_record(provider, identity)
        require(record is not None and not record.get('pending') and not record.get('overflow'),
                'second mechanical objective retained unresolved callbacks')
    require(fingerprint(greeting) == initial_hash, 'second mechanical objective changed the fixture')
    return fresh


def native_call_inventory(provider, rows):
    """Retain every native attempt and unique paired result; never overwrite IDs."""
    calls, results = [], {}
    for ordinal, row in enumerate(rows):
        when = host_evidence._instant(row.get('timestamp'))
        if provider == 'codex':
            payload = row.get('payload', {})
            if payload.get('type') in {'function_call', 'custom_tool_call'}:
                args = payload.get('arguments', payload.get('input'))
                if payload.get('type') == 'function_call' and isinstance(args, str):
                    args = json.loads(args)
                calls.append((payload.get('call_id'), payload.get('name'), args, when, ordinal, row))
            elif payload.get('type') in {'function_call_output', 'custom_tool_call_output'}:
                output = payload.get('output')
                failed = (payload.get('is_error') is True or payload.get('isError') is True
                          or isinstance(output, str) and re.search(
                              r'Error:|Unknown model `|"isError"\s*:\s*true|exit code [1-9]|"exit_code"\s*:\s*[1-9]', output) is not None)
                results.setdefault(payload.get('call_id'), []).append((output, when, ordinal, failed))
        else:
            message = row.get('message', {})
            content = message.get('content') if isinstance(message, dict) else None
            for item in content if isinstance(content, list) else ():
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'tool_use':
                    calls.append((item.get('id'), item.get('name'), item.get('input'), when, ordinal, row))
                elif item.get('type') == 'tool_result':
                    results.setdefault(item.get('tool_use_id'), []).append(
                        (item.get('content'), when, ordinal, item.get('is_error') is True))
    ids = [item[0] for item in calls]
    require(all(isinstance(value, str) and value for value in ids) and len(set(ids)) == len(ids)
            and all(item[3] is not None for item in calls)
            and all(left[3] <= right[3] for left, right in zip(calls, calls[1:])), 'native root call inventory is malformed or duplicated')
    return calls, results


def structured_root_discovery(name, args, *, provider='', document=None, session='', project=None):
    if name in {'Read', 'Glob', 'Grep', 'read_file', 'AskUserQuestion', 'request_user_input'}:
        return isinstance(args, dict)
    if name == 'Skill' and isinstance(args, dict):
        return args.get('skill') in {'symphony', 'symphony:symphony'}
    if provider == 'codex' and name in {'exec', 'functions.exec'} and isinstance(args, str):
        # Reading the tool catalogue cannot inspect or alter the project.
        metadata = (r'const (?P<result>\w+)\s*=\s*ALL_TOOLS\.filter\(x\s*=>\s*'
                    r'/[a-z|]+/i?\.test\(x\.name\s*\+\s*" "\s*\+\s*x\.description\)\);\s*'
                    r'text\((?P=result)\);?')
        if re.fullmatch(metadata, args.strip()):
            return True
    if provider != 'codex' or not isinstance(document, dict) or project is None:
        return False
    details = (args if name == 'exec_command' else composed_call(args, 'exec_command', serialized=True, output_only=True)
               if name in {'exec', 'functions.exec'} else None)
    if (not isinstance(details, dict) or not isinstance(details.get('cmd'), str)
            or set(details) - {'cmd', 'workdir', 'yield_time_ms', 'max_output_tokens'}
            or 'workdir' in details and (not isinstance(details['workdir'], str)
                or Path(details['workdir']).resolve() != project.resolve())):
        return False
    activation = document.get('activation', {}).get('codex', {})
    profiles = [activation, *activation.get('session_profiles', ())]
    roots = {(item.get('runtime_root'), item.get('plugin_root')) for item in profiles
             if isinstance(item, dict) and item.get('session_id') == session}
    if len(roots) != 1:
        return False
    retained, original = next(iter(roots))
    if not isinstance(retained, str) or not isinstance(original, str):
        return False
    # The exact generated checker verifies the retained tree before importing it.
    checker = _retained_activation_command(retained, original)
    if checker and details['cmd'] == checker:
        return True
    try:
        if any(value in details['cmd'] for value in ('$','`','\n','\r')):
            return False
        wrapped = re.fullmatch(
            r'"(?P<shell>[^"]+)" -Command "Get-Content -LiteralPath \'(?P<path>[^\'"]+)\'"',
            details['cmd'], re.IGNORECASE)
        if wrapped:
            installed = shutil.which('pwsh.exe') or shutil.which('pwsh')
            if not installed or Path(wrapped['shell']).resolve() != Path(installed).resolve():
                return False
            argv = ['Get-Content', '-LiteralPath', wrapped['path']]
        else:
            lexer = shlex.shlex(details['cmd'], posix=True, punctuation_chars=';&|<>')
            lexer.whitespace_split = True
            argv = list(lexer)
        allowed = {(project / 'AGENTS.md').resolve()}
        allowed.update((Path(root) / 'skills/symphony' / relative).resolve()
                       for root in (original, retained) for relative in (
                           'SKILL.md', 'references/role-contracts.md',
                           'references/provider-activation.md', 'references/capability-routing.md'))
        allowed.update((Path(root) / relative).resolve() for root in (original, retained)
                       for relative in ('profiles.json', 'model-policy.json',
                           '.codex-plugin/migrated-command-skills/source-command-help/SKILL.md'))
        paths = (argv[2:] if len(argv) >= 3 and argv[:2] == ['cat', '--'] else
                 argv[1:] if len(argv) >= 2 and argv[0] == 'cat' else
                 argv[3:] if len(argv) == 4 and argv[:2] == ['sed', '-n']
                    and re.fullmatch(r'[1-9]\d*(?:,[1-9]\d*)?p', argv[2]) else
                 argv[2:] if len(argv) == 3 and argv[:2] == ['Get-Content', '-LiteralPath'] else [])
        return bool(paths) and all((project / value).resolve() in allowed for value in paths)
    except (ValueError, OSError):
        return False


def assessed_first_verified(provider, document, run, child_rows, home, project, profile):
    """Prove assessor-first authority; absent fast markers alone prove nothing."""
    try:
        session = run['session_id']
        assessors = [item for item in run['delegations'] if item['role'] == 'assessor']
        if len(assessors) != 1 or assessors[0]['state'] != 'completed':
            return False
        assessor = assessors[0]
        identity = assessor['identity']
        expected = snapshot_for(provider, profile).tiers['strongest']
        if (assessor['requested_tier'], assessor['requested_effort']) != (expected, 'high'):
            return False
        contract = run['assessment'].get('substantive_contract')
        if (not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != 1
                or not isinstance(contract.get('epoch'), str) or not contract['epoch']):
            return False
        accepted = host_evidence._instant(contract.get('accepted_at'))
        history = document.get('event_history', ())
        admissions = [item for item in history if item.get('event_id') == contract['epoch'] + ':assessment:assessment_accepted'
                      and item.get('kind') == 'assessment_accepted' and item.get('observed_at') == contract['accepted_at']]
        terminals = [item for item in history if item.get('kind') == 'delegation_updated'
                     and item.get('payload', {}).get('identity') == identity
                     and item.get('payload', {}).get('state') == 'completed']
        if (accepted is None or len(admissions) != 1 or not terminals
                or any(item.get('payload', {}).get('role') not in {None, 'assessor'} for item in terminals)
                or any(admissions[0].get('payload', {}).get(key) != run['assessment'].get(key)
                       for key in ('size', 'complexity', 'risk'))):
            return False
        rows = child_rows[identity]
        lead_rows = child_rows[run['lead_identity']]
        if identity == run['lead_identity']:
            return False
        retried = set()
        if provider == 'codex':
            paths = list((home / 'sessions').rglob('*' + session + '.jsonl'))
            if len(paths) != 1:
                return False
            root = _complete_native_jsonl(paths[0])
            if (not root or root[0].get('type') != 'session_meta' or root[0]['payload'].get('id') != session
                    or root[0]['payload'].get('source') != 'exec' or root[0]['payload'].get('forked_from_id')
                    or not Path(root[0]['payload'].get('cwd', '')).is_absolute()
                    or Path(root[0]['payload'].get('cwd', '')).resolve() != project.resolve()
                    or not worker_transcript_is_unforked(provider, rows, identity)
                    or not worker_transcript_is_unforked(provider, lead_rows, run['lead_identity'])):
                return False
            if rows[0].get('type') != 'session_meta' or lead_rows[0].get('type') != 'session_meta':
                return False
            lead_header = lead_rows[0]['payload']
            if (lead_header.get('id') != run['lead_identity']
                    or lead_header.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id') != session
                    or not Path(lead_header.get('cwd', '')).is_absolute()
                    or Path(lead_header['cwd']).resolve() != project.resolve()):
                return False
            header = rows[0]['payload']
            spawn = header.get('source', {}).get('subagent', {}).get('thread_spawn', {})
            task = 'symphony_assessor_' + re.sub(r'\W', '_', expected) + '_high'
            path = header.get('agent_path', spawn.get('agent_path'))
            if (header.get('id') != identity or spawn.get('parent_thread_id') != session
                    or path != '/root/' + task or header.get('agent_path', path) != path
                    or spawn.get('agent_path', path) != path
                    or not Path(header.get('cwd', '')).is_absolute()
                    or Path(header['cwd']).resolve() != project.resolve()):
                return False
            calls, results = native_call_inventory(provider, root)
            launches = [item for item in calls if item[1] == 'spawn_agent']
            if not launches:
                return False
            call = launches[0]
            args = call[2]
            paired = results.get(call[0], ())
            if (not isinstance(args, dict) or args.get('task_name') != task or args.get('model') != expected
                    or args.get('reasoning_effort') != 'high' or args.get('fork_turns') != 'none'
                    or len(paired) != 1):
                return False
            result, returned, result_index, _ = paired[0]
            output = json.loads(result) if isinstance(result, str) else None
            activity = [(index, row) for index, row in enumerate(root) if row.get('type') == 'event_msg'
                and row.get('payload', {}).get('type') == 'item_completed'
                and row['payload'].get('item', {}).get('id') == call[0]]
            if (not isinstance(output, dict) or output.get('task_name') != path or len(activity) != 1
                    or activity[0][1]['payload'].get('thread_id') != session
                    or activity[0][1]['payload']['item'].get('type') != 'SubAgentActivity'
                    or activity[0][1]['payload']['item'].get('kind') != 'started'
                    or activity[0][1]['payload']['item'].get('agent_thread_id') != identity
                    or activity[0][1]['payload']['item'].get('agent_path') != path):
                return False
            starts = [row for row in rows if row.get('type') == 'event_msg' and row['payload'].get('type') == 'task_started']
            ends = [row for row in rows if row.get('type') == 'event_msg' and row['payload'].get('type') == 'task_complete']
            contexts = [row for row in rows if row.get('type') == 'turn_context']
            if (len(starts) != 1 or len(ends) != 1 or len(contexts) != 1
                    or not isinstance(starts[0]['payload'].get('turn_id'), str) or not starts[0]['payload']['turn_id']
                    or ends[0]['payload'].get('turn_id') != starts[0]['payload']['turn_id']
                    or contexts[0]['payload'].get('turn_id') != starts[0]['payload']['turn_id']
                    or contexts[0]['payload'].get('model') != expected or contexts[0]['payload'].get('effort') != 'high'
                    or any(row.get('type') == 'event_msg' and row['payload'].get('type') in
                           {'task_failed', 'turn_aborted', 'task_interrupted', 'error'} for row in rows)):
                return False
            began = host_evidence._instant(starts[0].get('timestamp'))
            completed = host_evidence._instant(ends[0].get('timestamp'))
            reports = [(ends[0]['payload'].get('last_agent_message'), completed)]
            reports.extend((assistant_text(provider, row), host_evidence._instant(row.get('timestamp')))
                           for row in rows if row.get('type') == 'response_item'
                           and row['payload'].get('phase') == 'final_answer')
            lead_starts = [row for row in lead_rows if row.get('type') == 'event_msg'
                           and row['payload'].get('type') == 'task_started']
        else:
            paths = list((home / 'projects').glob(f'*/{session}.jsonl'))
            if len(paths) != 1:
                return False
            root = _native_jsonl(paths[0])
            if root is None:
                return False
            calls, results = native_call_inventory(provider, root)
            launches = [item for item in calls if item[1] == 'Agent']
            if not launches:
                return False
            meta = json.loads((paths[0].with_suffix('') / 'subagents' / f'agent-{identity}.meta.json').read_text())
            bound = [item for item in launches if item[0] == meta.get('toolUseId')]
            if len(bound) != 1:
                return False
            call = bound[0]
            args, launch_row = call[2], call[5]
            label = f'symphony:symphony-assessor-{expected}-high'
            paired = results.get(call[0], ())
            if (not isinstance(args, dict) or args.get('subagent_type') != label
                    or args.get('model', expected) != expected or not isinstance(args.get('prompt'), str)
                    or not args['prompt'].strip() or meta.get('toolUseId') != call[0]
                    or meta.get('agentType') != label or type(meta.get('spawnDepth')) is not int or meta['spawnDepth'] != 1
                    or len(paired) != 1 or paired[0][3]
                    or launch_row.get('sessionId') != session or launch_row.get('agentId')
                    or launch_row.get('isSidechain') is True or not Path(launch_row.get('cwd', '')).is_absolute()
                    or Path(launch_row['cwd']).resolve() != project.resolve()):
                return False
            # A rejected launch of this same assessor may be retried. It must
            # have failed before spawning any child; unknown prior work remains
            # a failure of the assessor-first proof.
            for earlier in launches:
                if earlier[4] >= call[4]:
                    continue
                values, earlier_row = earlier[2], earlier[5]
                response = results.get(earlier[0], ())
                if (not isinstance(values, dict) or values.get('subagent_type') != label
                        or values.get('model', expected) != expected
                        or values.get('prompt') != args['prompt']
                        or len(response) != 1 or response[0][3] is not True
                        or not earlier[3] <= response[0][1] < call[3]
                        or not earlier[4] < response[0][2] < call[4]
                        or earlier_row.get('sessionId') != session or earlier_row.get('agentId')
                        or earlier_row.get('isSidechain') is True):
                    return False
                retried.add(earlier[0])
            if retried:
                metadata_paths = list((paths[0].with_suffix('') / 'subagents').glob('*.meta.json'))
                if len(metadata_paths) > 128:
                    return False
                for metadata_path in metadata_paths:
                    if metadata_path.is_symlink() or metadata_path.stat().st_size > 64 * 1024:
                        return False
                    if json.loads(metadata_path.read_text()).get('toolUseId') in retried:
                        return False
            result, returned, result_index, _ = paired[0]
            # The child metadata binds this exact native launch, including
            # background ACKs whose text does not contain the child UUID.
            activity = [row for row in rows if row.get('type') in {'user', 'assistant'}]
            if any(row.get('sessionId') != session or row.get('agentId') != identity
                   or row.get('isSidechain') is not True or row.get('isApiErrorMessage') is True for row in activity):
                return False
            prompts = [row for row in activity if row['type'] == 'user' and isinstance(row.get('message', {}).get('content'), str)]
            assistants = [row for row in activity if row['type'] == 'assistant']
            if (len(prompts) != 1 or prompts[0]['message']['content'] != args['prompt']
                    or not prompts[0].get('uuid') or not assistants
                    or activity[-1] is not assistants[-1] or assistants[-1]['message'].get('stop_reason') != 'end_turn'
                    or any(row['message'].get('model') != expected
                           or row.get('effort', 'high') != 'high' for row in assistants)):
                return False
            began = host_evidence._instant(prompts[0].get('timestamp'))
            completed = host_evidence._instant(assistants[-1].get('timestamp'))
            reports = [(assistant_text(provider, row), host_evidence._instant(row.get('timestamp')))
                       for row in assistants]
            lead_activity = [row for row in lead_rows if row.get('type') in {'user', 'assistant'}]
            lead_meta = json.loads((paths[0].with_suffix('') / 'subagents' / f'agent-{run["lead_identity"]}.meta.json').read_text())
            lead = next(item for item in run['delegations'] if item['identity'] == run['lead_identity'])
            if (not lead_activity or any(row.get('sessionId') != session or row.get('agentId') != run['lead_identity']
                                         or row.get('isSidechain') is not True for row in lead_activity)
                    or type(lead_meta.get('spawnDepth')) is not int or lead_meta['spawnDepth'] != 1
                    or lead_meta.get('agentType') != f'symphony:symphony-lead-{lead["requested_tier"]}-{lead["requested_effort"]}'):
                return False
            lead_starts = [row for row in lead_activity if row.get('type') == 'user'
                           and isinstance(row.get('message', {}).get('content'), str)]
        if (began is None or completed is None or returned is None or not lead_starts
                or call[3] > began or call[3] > returned or call[4] >= result_index
                or began > completed or accepted < began
                or accepted >= host_evidence._instant(lead_starts[0].get('timestamp'))):
            return False
        # No prior launch can vanish just because it failed or had no result.
        # Unknown execution/write wrappers before admission stay unverified.
        if any(item != call and item[0] not in retried and item[3] <= accepted and not (
                    structured_root_discovery(item[1], item[2], provider=provider,
                                              document=document, session=session, project=project)
                    or provider == 'codex' and item[1] == 'wait_agent' and isinstance(item[2], dict))
               for item in calls):
            return False
        available = False
        for report, reported_at in reports:
            marker_lines = [line.strip() for line in report.splitlines()
                            if line.strip().startswith('SYMPHONY_ASSESSMENT:')]
            if not marker_lines:
                continue
            if len(marker_lines) != 1 or reported_at is None or not began <= reported_at <= completed:
                return False
            packet = json.loads(marker_lines[0].partition(':')[2])
            if not isinstance(packet, dict) or any(packet.get(key) != run['assessment'].get(key)
                                                   for key in ('size', 'complexity', 'risk')):
                return False
            available |= reported_at <= accepted
        return available
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        return False


def assessed_first_probe(provider, document, run, home, project):
    """Expose exact proof stage, never native arguments, identities or paths."""
    facts = {'accepted': False, 'source_line': None, 'root_call_count': 0, 'root_launch_count': 0}
    def trace(frame, event, value):
        if frame.f_code is structured_root_discovery.__code__:
            if event == 'return' and value is False and frame.f_locals.get('name') in {
                    'exec', 'functions.exec', 'exec_command'}:
                details = frame.f_locals.get('details')
                argv = frame.f_locals.get('argv', ())
                paths = frame.f_locals.get('paths', ())
                source = frame.f_locals.get('args')
                rejected = facts.setdefault('root_discovery_rejections', [])
                if len(rejected) < 16:
                    rejected.append({'source_line': frame.f_lineno,
                        'literal_call_parsed': isinstance(details, dict),
                        'exec_command_mentions': min(16, source.count('tools.exec_command('))
                            if isinstance(source, str) else 0,
                        'command_family': argv[0] if argv and argv[0] in {
                            'cat', 'sed', 'Get-Content', 'python', 'python3', 'rg', 'pwd'} else 'other',
                        'bootstrap_target_count': len(paths),
                        'control_operator_present': any(token in {';', '&&', '||', '|', '>', '<'} for token in argv)})
            return trace
        if frame.f_code is not assessed_first_verified.__code__:
            return None
        if event == 'return':
            facts.update(accepted=value is True, source_line=frame.f_lineno)
            facts['admission_count'] = len(frame.f_locals.get('admissions', ()))
            facts['assessor_terminal_count'] = len(frame.f_locals.get('terminals', ()))
            facts['failed_same_assessor_retries'] = len(frame.f_locals.get('retried', ()))
            calls = frame.f_locals.get('calls', ())
            if isinstance(calls, (tuple, list)):
                facts['root_call_count'] = len(calls)
                facts['root_launch_count'] = sum(item[1] in {'Agent', 'spawn_agent'} for item in calls)
                unsupported = [item for item in calls if item != frame.f_locals.get('call')
                    and frame.f_locals.get('accepted') is not None and item[3] <= frame.f_locals['accepted']
                    and not (structured_root_discovery(item[1], item[2], provider=provider,
                                                      document=document, session=run['session_id'], project=project)
                             or provider == 'codex' and item[1] == 'wait_agent' and isinstance(item[2], dict))]
                facts['pre_admission_unsupported_calls'] = len(unsupported)
                facts['unsupported_tool_kinds'] = {}
                for item in unsupported:
                    name = item[1] if item[1] in {'exec', 'functions.exec', 'exec_command', 'wait_agent',
                        'list_agents', 'send_message', 'followup_task', 'Agent', 'spawn_agent', 'Bash'} else 'other'
                    facts['unsupported_tool_kinds'][name] = facts['unsupported_tool_kinds'].get(name, 0) + 1
            args = frame.f_locals.get('args', {})
            call = frame.f_locals.get('call')
            paired = frame.f_locals.get('paired', ())
            expected = frame.f_locals.get('expected')
            meta = frame.f_locals.get('meta', {})
            if isinstance(args, dict) and call:
                facts.update(first_type_matches=args.get('subagent_type') == frame.f_locals.get('label')
                             if provider == 'claude' else args.get('task_name') == frame.f_locals.get('task'),
                    first_explicit_model_present='model' in args,
                    first_model_matches=args.get('model', expected) == expected,
                    first_effort_matches=args.get('reasoning_effort') == 'high' if provider == 'codex' else True,
                    first_fork_none=args.get('fork_turns') == 'none' if provider == 'codex' else True,
                    first_result_count=len(paired), first_result_failed=bool(paired) and paired[0][3],
                    first_meta_call_matches=meta.get('toolUseId') == call[0] if provider == 'claude' else True,
                    first_meta_type_matches=meta.get('agentType') == frame.f_locals.get('label') if provider == 'claude' else True)
                if provider == 'codex':
                    values = frame.f_locals
                    identity, path = values.get('identity'), values.get('path')
                    header, output, activity = values.get('header', {}), values.get('output'), values.get('activity', ())
                    bound = (header.get('id') == identity and isinstance(path, str)
                        and header.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id') == run['session_id']
                        and isinstance(output, dict) and output.get('task_name') == path and len(activity) == 1
                        and activity[0][1].get('payload', {}).get('thread_id') == run['session_id']
                        and activity[0][1].get('payload', {}).get('item', {}).get('type') == 'SubAgentActivity'
                        and activity[0][1]['payload']['item'].get('kind') == 'started'
                        and activity[0][1].get('payload', {}).get('item', {}).get('agent_thread_id') == identity
                        and activity[0][1]['payload']['item'].get('agent_path') == path)
                    facts['rejected_assessor_controls'] = []
                    for item in unsupported[:16]:
                        if item[1] not in {'send_message', 'followup_task'}:
                            continue
                        details = item[2] if isinstance(item[2], dict) else {}
                        replies = values.get('results', {}).get(item[0], ())
                        native_delivery = [row for row in values.get('root', ())
                            if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'item_completed'
                            and row['payload'].get('thread_id') == run['session_id']
                            and row['payload'].get('item', {}).get('id') == item[0]
                            and row['payload']['item'].get('type') == 'SubAgentActivity']
                        delivered = (len(native_delivery) == 1
                            and native_delivery[0]['payload']['item'].get('agent_thread_id') == identity
                            and native_delivery[0]['payload']['item'].get('agent_path') == path
                            and native_delivery[0]['payload']['item'].get('kind') in {'interacted', 'started'})
                        message, initial = _observable_probe_text(details.get('message')), _observable_probe_text(args.get('message'))
                        final_times = [when for _, when in values.get('reports', ()) if when is not None]
                        facts['rejected_assessor_controls'].append({
                            'tool': item[1], 'native_assessor_binding': bound,
                            'recipient_uuid_matches': bound and details.get('target') == identity,
                            'recipient_path_matches': bound and details.get('target') == path,
                            'after_spawn_call': item[3] >= call[3] and item[4] > call[4],
                            'after_spawn_result': len(paired) == 1 and paired[0][1] is not None
                                and item[3] >= paired[0][1] and item[4] > paired[0][2],
                            'before_assessor_final': item[3] < min(final_times) if final_times else None,
                            'before_admission': item[3] < values['accepted'] if values.get('accepted') else None,
                            'result_count': len(replies), 'native_delivery_matches': delivered,
                            'unique_successful_delivery': len(replies) == 1 and not replies[0][3] and delivered
                                and replies[0][1] is not None and replies[0][1] >= item[3] and replies[0][2] > item[4],
                            'message_observable': message is not None,
                            'same_as_initial_packet': message == initial if message is not None and initial is not None else None,
                            'interrupt': details.get('interrupt') if type(details.get('interrupt')) is bool else None})
        return trace
    previous = sys.gettrace()
    try:
        children = {item['identity']: native_rows(provider, home, item['identity']) for item in run['delegations']}
        sys.settrace(trace)
        assessed_first_verified(provider, document, run, children, home, project,
                                run['assessment']['route']['profile'])
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        facts['source_unavailable'] = True
    finally:
        sys.settrace(previous)
    return facts


def lead_verifies_after_worker_returns(provider, lead_rows, workers, child_rows, project, run=None):
    """A worker's test output cannot stand in for the lead's later integration call."""
    try:
        calls, results = native_call_inventory(provider, lead_rows)
        returned = []
        for worker in workers:
            matches = []
            for call_id, name, args, called, index, _ in calls:
                if name != ('spawn_agent' if provider == 'codex' else 'Agent'):
                    continue
                paired = results.get(call_id, ())
                if len(paired) != 1 or paired[0][3] or paired[0][1] is None or paired[0][2] <= index:
                    continue
                output, when, _, _ = paired[0]
                if provider == 'codex':
                    header = child_rows[worker['identity']][0]['payload']
                    try:
                        launched = json.loads(output) if isinstance(output, str) else None
                    except ValueError:
                        continue  # A rejected launch is not the later successful child's launch.
                    exact = isinstance(launched, dict) and launched.get('task_name') == header.get('agent_path')
                else:
                    proof = (run or {}).get('assessment', {}).get('_substantive_children', {}).get(worker['identity'], {})
                    scope = {'run_id': (run or {}).get('run_id'), 'lead': (run or {}).get('lead_identity'),
                             'owner_generation': (run or {}).get('owner_generation'),
                             'epoch': (run or {}).get('assessment', {}).get('substantive_contract', {}).get('epoch')}
                    exact = (proof.get('successful') is True and proof.get('role') == 'worker'
                             and proof.get('parent') == scope['lead'] and all(scope.values())
                             and all(proof.get(key) == value for key, value in scope.items())
                             and proof.get('launch_hash') == hashlib.sha256(call_id.encode()).hexdigest())
                if exact:
                    rows = child_rows[worker['identity']]
                    if provider == 'codex':
                        terminals = [row for row in rows if row.get('type') == 'event_msg'
                                     and row.get('payload', {}).get('type') == 'task_complete']
                        if len(terminals) != 1 or any(row.get('type') == 'event_msg'
                            and row.get('payload', {}).get('type') in {
                                'task_failed', 'turn_aborted', 'task_interrupted', 'error'} for row in rows):
                            return False
                        completed = host_evidence._instant(terminals[0].get('timestamp'))
                    else:
                        activity = [row for row in rows if row.get('type') in {'user', 'assistant'}]
                        if (not activity or activity[-1].get('type') != 'assistant'
                                or activity[-1].get('message', {}).get('stop_reason') != 'end_turn'
                                or any(row.get('isApiErrorMessage') is True for row in activity)):
                            return False
                        completed = host_evidence._instant(activity[-1].get('timestamp'))
                    child_calls, child_results = native_call_inventory(provider, rows)
                    handbacks = [item for item in child_calls if item[1] == 'SubagentHandback'] if provider == 'claude' else []
                    if provider == 'codex' and child_calls and child_calls[-1][1] == 'send_message' and run:
                        candidate = child_calls[-1]
                        args = candidate[2]
                        parent = rows[0]['payload'].get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
                        if (parent and parent == run.get('lead_identity') and isinstance(args, dict) and args.get('target')
                                and args.get('target') in {parent, lead_rows[0].get('payload', {}).get('agent_path')}):
                            handbacks = [candidate]
                    if handbacks:
                        if len(handbacks) != 1 or child_calls[-1] != handbacks[0]:
                            return False
                        handback = handbacks[0]
                        response = child_results.get(handback[0], ())
                        if (len(response) != 1 or response[0][3] or response[0][1] is None
                                or not isinstance(handback[2], dict) or not isinstance(handback[2].get('message'), str)
                                or not handback[2]['message'] or response[0][2] <= handback[4]
                                or response[0][1] < handback[3] or completed is None or response[0][1] > completed):
                            return False
                        for prior in child_calls[:-1]:
                            prior_result = child_results.get(prior[0], ())
                            if (len(prior_result) != 1 or prior_result[0][1] is None
                                    or not prior[3] <= prior_result[0][1] <= handback[3]
                                    or not prior[4] < prior_result[0][2] < handback[4]):
                                return False
                        # Earlier diagnostic failures may be repaired; all
                        # their results must be settled before successful handoff.
                        # Native handoff can deliver the finished report before
                        # the child's final courtesy terminal is written.
                        completed = response[0][1]
                    if completed is None:
                        return False
                    matches.append(max(when, completed))
            if len(matches) != 1:
                return False
            returned.extend(matches)
        if not returned:
            return False
        boundary = max(returned)
        for call_id, _, _, called, index, row in calls:
            paired = results.get(call_id, ())
            if (called <= boundary or len(paired) != 1 or paired[0][3]
                    or paired[0][1] is None or paired[0][1] < called or paired[0][2] <= index):
                continue
            if unittest_verified(tool_evidence(provider, [row, lead_rows[paired[0][2]]]), project):
                return True
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
        pass
    return False


def _observable_probe_text(value):
    """Native encrypted message envelopes are unavailable, never plaintext proof."""
    return value if isinstance(value, str) and not re.fullmatch(r'gAAAA[A-Za-z0-9_=-]*', value) else None


def _lead_delivery_probe(provider, run, home, project, lead):
    """Observe exact packet/context presence; none of these facts grant authority."""
    from symphony.runtime import _LEAD_VERIFICATION_CONTRACT
    clauses = (
        "After ALL workers return successfully, the canonical lead itself must run exactly python -m unittest -q; "
        "a worker's test run or git diff does not fulfill this lead integration check.",
        "after every worker returns successfully, the canonical assessed lead itself must run exactly python -m unittest -q "
        "in a standalone native tool call and return the full successful result. A worker test or git diff is insufficient.")
    present = lambda text: any(clause in text for clause in clauses) if text is not None else None
    facts = {'admitted_task_acceptance_present': present(_observable_probe_text(run.get('task'))),
             'root_native_available': False, 'launch_binding': None, 'launch_prompt_observable': None,
             'launch_matches_child_prompt': None, 'fixture_acceptance_in_launch': None,
             'start_context_observable': False, 'shared_verification_in_start': None, 'resume_deliveries': None}
    contexts = []
    for row in lead:
        payload = row.get('payload', {}) if provider == 'codex' else row.get('message', {})
        if not isinstance(payload, dict) or not (payload.get('role') in {'developer', 'system'} or row.get('type') == 'system'):
            continue
        content = payload.get('content', ())
        text = '\n'.join(block['text'] for block in content if isinstance(block, dict) and isinstance(block.get('text'), str)) if isinstance(content, list) else content
        if isinstance(text, str) and 'Symphony worker routes by the packet\'s own size/complexity:' in text:
            contexts.append(text)
    if contexts:
        facts.update(start_context_observable=True,
                     shared_verification_in_start=any(_LEAD_VERIFICATION_CONTRACT.strip() in text for text in contexts))
    try:
        session, identity = run['session_id'], run['lead_identity']
        paths = (list((home / 'sessions').rglob('*' + session + '.jsonl')) if provider == 'codex'
                 else list((home / 'projects').glob(f'*/{session}.jsonl')))
        if len(paths) != 1:
            return facts
        root = (_complete_native_jsonl if provider == 'codex' else _native_jsonl)(paths[0])
        if root is None:
            return facts
        facts['root_native_available'] = True
        calls, results = native_call_inventory(provider, root)
        canonical_path = None
        if provider == 'claude':
            meta = json.loads((paths[0].with_suffix('') / 'subagents' / f'agent-{identity}.meta.json').read_text())
            launches = [item for item in calls if item[1] == 'Agent' and item[0] == meta.get('toolUseId')]
            prompts = [row for row in lead if row.get('type') == 'user'
                       and isinstance(row.get('message', {}).get('content'), str)]
            child_prompt = prompts[0]['message']['content'] if prompts else None
            child_bound = bool(prompts and prompts[0].get('sessionId') == session
                               and prompts[0].get('agentId') == identity and prompts[0].get('isSidechain') is True
                               and meta.get('spawnDepth') == 1)
        else:
            header = lead[0].get('payload', {}) if lead and lead[0].get('type') == 'session_meta' else {}
            root_header = root[0].get('payload', {}) if root and root[0].get('type') == 'session_meta' else {}
            canonical_path = header.get('agent_path')
            launch_ids = [row['payload']['item']['id'] for row in root if row.get('type') == 'event_msg'
                and row.get('payload', {}).get('type') == 'item_completed'
                and row['payload'].get('thread_id') == session
                and row['payload'].get('item', {}).get('type') == 'SubAgentActivity'
                and row['payload']['item'].get('kind') == 'started'
                and row['payload']['item'].get('agent_thread_id') == identity
                and row['payload']['item'].get('agent_path') == canonical_path]
            launches = [item for item in calls if item[1] == 'spawn_agent' and item[0] in launch_ids]
            child_bound = (len(launch_ids) == 1 and header.get('id') == identity
                and root_header.get('id') == session and root_header.get('source') == 'exec'
                and worker_transcript_is_unforked(provider, root, session)
                and header.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id') == session
                and worker_transcript_is_unforked(provider, lead, identity))
            # Codex can omit the encrypted spawn prompt from visible child rows.
            prompts = [row['payload'].get('message') for row in lead if row.get('type') == 'event_msg'
                       and row.get('payload', {}).get('type') == 'user_message']
            child_prompt = _observable_probe_text(prompts[0]) if prompts else None
        facts['launch_binding'] = False
        if len(launches) == 1 and isinstance(launches[0][2], dict):
            call = launches[0]
            args = call[2]
            paired = results.get(call[0], ())
            if provider == 'claude':
                scope = (call[5].get('sessionId') == session and not call[5].get('agentId')
                    and call[5].get('isSidechain') is not True and args.get('subagent_type') == meta.get('agentType')
                    and isinstance(call[5].get('cwd'), str) and Path(call[5]['cwd']).is_absolute())
            else:
                output = json.loads(paired[0][0]) if len(paired) == 1 and isinstance(paired[0][0], str) else None
                scope = isinstance(output, dict) and output.get('task_name') == canonical_path
            facts['launch_binding'] = bool(child_bound and scope and len(paired) == 1 and not paired[0][3]
                and paired[0][1] is not None and paired[0][1] >= call[3] and paired[0][2] > call[4]
                and Path(call[5].get('cwd', '') if provider == 'claude' else header.get('cwd', '')).is_absolute()
                and Path(call[5].get('cwd', '') if provider == 'claude' else header.get('cwd', '')).resolve() == project.resolve())
            prompt = _observable_probe_text(args.get('prompt' if provider == 'claude' else 'message'))
            facts.update(launch_prompt_observable=prompt is not None, fixture_acceptance_in_launch=present(prompt),
                         launch_matches_child_prompt=prompt == child_prompt if prompt is not None and child_prompt is not None else None)
        facts['resume_deliveries'] = []
        for call in calls:
            if call[1] not in ({'SendMessage'} if provider == 'claude' else {'send_message', 'followup_task'}) or not isinstance(call[2], dict):
                continue
            target = call[2].get('to' if provider == 'claude' else 'target')
            if target != identity and not (canonical_path and target == canonical_path):
                continue
            prompt = _observable_probe_text(call[2].get('message'))
            paired = results.get(call[0], ())
            facts['resume_deliveries'].append({'message_observable': prompt is not None,
                'fixture_acceptance_present': present(prompt), 'unique_nonerror_result': len(paired) == 1 and not paired[0][3]})
            if len(facts['resume_deliveries']) == 16:
                break
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        facts['source_incomplete'] = True
    return facts


def lead_integration_probe(provider, run, home, project):
    """Inspect the same verifier; export only counts, categories and its exit line."""
    facts = {'accepted': False}
    code = lead_verifies_after_worker_returns.__code__
    def trace(frame, kind, result):
        if frame.f_code != code:
            return None
        if kind == 'return':
            values = frame.f_locals
            facts.update(accepted=result is True, source_line=frame.f_lineno,
                         returned_workers=len(values.get('returned', ())),
                         worker_matches=len(values.get('matches', ())),
                         integration_boundary_present=values.get('boundary') is not None)
        elif kind == 'exception':
            facts.update(exception_type=result[0].__name__, exception_line=frame.f_lineno)
        return trace
    previous = sys.gettrace()
    try:
        children = {item['identity']: native_rows(provider, home, item['identity']) for item in run['delegations']}
        lead = children[run['lead_identity']]
        workers = [item for item in run['delegations'] if item['role'] == 'worker']
        facts['delivery'] = _lead_delivery_probe(provider, run, home, project, lead)
        facts['commands'] = command_witness_probe(provider, lead, project)
        sys.settrace(trace)
        lead_verifies_after_worker_returns(provider, lead, workers, children, project, run)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError) as error:
        facts['error_type'] = type(error).__name__
    finally:
        sys.settrace(previous)
    return facts


def verify_case_run(provider, case, document, run_state, home, project, greeting,
                    initial_hash, first_change_after_ns, fast):
    """Verify one archived objective; this never creates or resumes a native turn."""
    session = run_state['session_id']
    children = run_state.get("delegations", [])
    child_rows = {item["identity"]: native_rows(provider, home, item["identity"]) for item in children}
    decisions = []
    for identity, rows in child_rows.items():
        if not fast_native_identity(provider, rows, identity, run_state, home, project):
            continue
        require(worker_transcript_is_unforked(provider, rows, identity),
                "fast transcript contains inherited or foreign rows; decision provenance is unverified")
        for row in rows:
            for decision in fast_decision_lines(assistant_text(provider, row)):
                decisions.append((identity, decision, row.get("timestamp")))
    expected = "eligible" if case == "command" else "escalate"
    if provider == 'codex' and case == 'command':
        candidates = [identity for identity, rows in child_rows.items()
                      if fast_native_identity(provider, rows, identity, run_state, home, project)]
        require(len(candidates) == 1, 'mechanical command has no unique native fast owner')
        canonical = codex_command_turns_verified(document, run_state, child_rows[candidates[0]],
                                                candidates[0], home, project)
        require(canonical is not None, 'mechanical command lacks exact canonical turn/receipt/followup proof')
        decisions = [canonical]
    assessed_first = case in {'feature', 'run-and-fix'} and not decisions
    if assessed_first:
        require(assessed_first_verified(provider, document, run_state, child_rows, home, project,
                                       run_state['assessment']['route']['profile']),
                'substantive objective lacks exact assessor-first authority')
        fast_identity, terminal_time, observed_fast, fast_evidence = None, None, {}, []
        routing_path = 'assessed_first'
    else:
        require(len(decisions) == 1 and decisions[0][1] == expected, 'native fast lead made the wrong semantic decision')
        fast_identity, _, terminal_time = decisions[0]
        observed_fast = next(item for item in children if item['identity'] == fast_identity)
        require((observed_fast['requested_tier'], observed_fast['requested_effort']) == (fast['model'], fast['effort']),
                'native fast lead disagrees with the selected capable/medium route')
        fast_evidence = tool_evidence(provider, child_rows[fast_identity])
        routing_path = 'fast_direct' if case == 'command' else 'fast_escalated'
    workers = [item for item in children if item["role"] == "worker"]
    assessors = [item for item in children if item["role"] == "assessor"]
    if case == "command":
        require(not workers and not assessors and fingerprint(greeting) == initial_hash,
                "mechanical objective changed the fixture or used assessed delegation")
        require(unittest_verified(fast_evidence, project),
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
        if not assessed_first:
            require(fast_turn_has_only_escalation(provider, child_rows[fast_identity], fast_evidence, fast_identity),
                    'substantive fast turn performed tools; before-change proof is unverified')
            boundary = host_evidence._instant(terminal_time)
        else:
            boundary = host_evidence._instant(assessment['substantive_contract']['accepted_at'])
        require(boundary is not None and first_change_after_ns is not None
                and first_change_after_ns > int(boundary.timestamp() * 1e9),
                'fixture was not observed unchanged after its routing boundary')
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
        require(lead_verifies_after_worker_returns(provider, child_rows[run_state['lead_identity']],
                                                  workers, child_rows, project, run_state),
                "lead integration has no successful native verification call after worker returns")
    oracle = "from greet import greet; assert greet('Ada') == 'Hello, Ada!'"
    if case == "feature":
        oracle += "; assert greet('Ada', uppercase=True) == 'HELLO, ADA!'"
    result = subprocess.run([sys.executable, "-c", oracle], cwd=project, capture_output=True)
    require(result.returncode == 0, "final greeting fixture failed its external acceptance check")
    return {"case": case, "routing_path": routing_path,
            **({"fast_decision": expected, "fast_model": observed_fast["requested_tier"],
                "fast_effort": observed_fast["requested_effort"]} if not assessed_first else {}),
            "assessment": {key: run_state["assessment"].get(key) for key in ("size", "complexity", "topology")},
            "workers": len(workers), "worker_edit_verified": case != "command", "pending_callbacks": 0,
            "worker_packet_role_marker_observed": provider == "claude" and case != "command",
            **({'verified_fast_command_turns': sum(row.get('type') == 'event_msg' and
                row.get('payload', {}).get('type') == 'task_started' for row in child_rows[fast_identity])}
               if provider == 'codex' and case == 'command' else {}),
            **({'claude_completion': claude_completion_probe(document, session, project, home)}
               if provider == 'claude' else {}),
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
            with tempfile.TemporaryDirectory(prefix="symphony-native-routing-", ignore_cleanup_errors=True) as scratch:
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
                        document = json.loads(path.read_text(encoding="utf-8"))
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
