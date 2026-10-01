"""The native upgrade receipt accepts only the reviewed retained candidate."""

import importlib.util
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.symphony.symphony.store import _locked


REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "symphony"
HARNESS = REPO / ".github" / "scripts" / "native_managed_concurrency.py"
sys.path.insert(0, str(HARNESS.parent))
SPEC = importlib.util.spec_from_file_location("native_managed_concurrency", HARNESS)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class CandidateRetainedProfileTests(unittest.TestCase):
    def test_live_update_versions_keep_default_release_and_explicit_160_strict(self):
        for old, candidate in (("1.5.1", "1.6.0"), ("1.5.1", "1.7.0"),
                               ("1.6.0", "1.7.0")):
            with self.subTest(old=old, candidate=candidate):
                native.require_live_update_versions(old, candidate)
        for old, candidate in (("1.5.1", "1.5.1"), ("1.5.1", "1.5.0"),
                               ("1.6.0", "1.6.0"), ("1.6.0", "1.5.1"),
                               ("1.7.0", "1.8.0"), ("1.5.0", "1.7.0"),
                               (None, "1.7.0"), ([], "1.7.0"),
                               ("1.6.0", "1.7"), ("1.6.0", "1.7.0-dev"),
                               ("1.6.0", None)):
            with self.subTest(old=old, candidate=candidate), self.assertRaises(RuntimeError):
                native.require_live_update_versions(old, candidate)

    def test_live_update_main_accepts_both_released_roots_with_default_candidate(self):
        for old_version in ("1.5.1", "1.6.0"):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory() as temporary:
                old_root = Path(temporary) / old_version
                argv = [str(HARNESS), "--provider", "codex", "--live-update",
                        "--only-live-update", "--old-plugin-root", str(old_root)]
                update = {"home": Path(temporary) / "disposable-home"}
                stdout = io.StringIO()
                with patch.object(native, "package_version", side_effect=[old_version, "1.7.0"]) as versions, \
                     patch.object(native, "prepare_live_update", return_value=update) as prepare, \
                     patch.object(native, "prepare_baseline_capture") as baseline, \
                     patch.object(native, "check_codex_mixed_live_update", return_value={"completed": ["a", "b"]}) as check, \
                     patch.object(sys, "argv", argv), patch.object(sys, "stdout", stdout), \
                     patch.dict(native.os.environ, {"OPENAI_API_KEY": "fixture-auth", "SYMPHONY_NATIVE_DIAGNOSTICS_DIR": ""}):
                    self.assertEqual(native.main(), 0)
                self.assertEqual([call.args[0] for call in versions.call_args_list], [old_root, PLUGIN])
                self.assertEqual(prepare.call_args.args[2:], (old_root, PLUGIN))
                baseline.assert_not_called()
                self.assertIs(check.call_args.args[3], update)

    def test_live_update_install_preflight_rejects_same_or_newer_old_before_mutation(self):
        for old, candidate in (("1.5.1", "1.5.1"), ("1.6.0", "1.6.0"),
                               ("1.6.0", "1.5.1"), ("1.7.0", "1.7.0")):
            with self.subTest(old=old, candidate=candidate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with patch.object(native, "package_version", side_effect=[old, candidate]), \
                     patch.object(native, "marketplace") as market, \
                     patch.object(native, "codex_command") as command, \
                     self.assertRaises(RuntimeError):
                    native.prepare_live_update("codex", root, root / "old", root / "candidate")
                market.assert_not_called()
                command.assert_not_called()
                self.assertFalse((root / "codex-live-update-home").exists())

    def test_literal_claude_worker_uses_actual_admitted_start_or_exact_legacy_clock(self):
        from dataclasses import asdict
        from plugins.symphony.tests.test_assessed_contract import AssessedContractTests
        fixture = AssessedContractTests()
        fixture.begin('claude')
        self.addCleanup(fixture.doCleanups)
        fixture.worker()
        run = asdict(fixture.state.active_run)
        home = Path(fixture.environ['CLAUDE_CONFIG_DIR'])
        directory = home / 'projects' / '-fixture' / 'root' / 'subagents'
        parent = directory / 'agent-lead.jsonl'
        rows = [json.loads(line) for line in parent.read_text(encoding='utf-8').splitlines()]
        block = rows[0]['message']['content'][0]
        block['input']['prompt'] = 'SYMPHONY_ROLE: worker\nReturn GATE_RELEASED.'
        rows.append({'type': 'user', 'sessionId': 'root', 'agentId': 'lead', 'isSidechain': True,
                     'timestamp': '2026-10-01T14:00:04+00:00', 'message': {'content': [
                     {'type': 'tool_result', 'tool_use_id': block['id'], 'content': 'worker', 'is_error': False}]}})
        parent.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        child = directory / 'agent-worker.jsonl'
        with child.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'assistant', 'sessionId': 'root', 'agentId': 'worker', 'isSidechain': True,
                'uuid': 'native-terminal', 'timestamp': '2026-10-01T14:00:04+00:00',
                'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': 'GATE_RELEASED'}]}}) + '\n')
        native.require_literal_worker('claude', run, home, fixture.project)
        legacy = {**run, 'assessment': {}}
        with self.assertRaises(RuntimeError):
            native.require_literal_worker('claude', legacy, home, fixture.project)
        stamp = int(datetime.fromisoformat(run['assessment']['_substantive_children']['worker']['admitted_at']).timestamp() * 1e9)
        clock = {'event': 'SubagentStart', 'session_id': 'root', 'agent_id': 'worker', 'started_ns': stamp}
        native.require_literal_worker('claude', legacy, home, fixture.project, [clock])
        original = child.read_text(encoding='utf-8')
        history = [json.loads(line) for line in original.splitlines()]
        history[-1]['message']['stop_reason'] = None
        later = {**history[-1], 'uuid': 'later', 'timestamp': '2026-10-01T14:00:05+00:00',
                 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'different result'}]}}
        child.write_text(''.join(json.dumps(row) + '\n' for row in [*history, later]), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            native.require_literal_worker('claude', run, home, fixture.project)
        child.write_text(original, encoding='utf-8')
        prompt = json.loads(original.splitlines()[0])
        handback = {**history[-1], 'uuid': 'handback', 'timestamp': '2026-10-01T14:00:04+00:00',
                    'message': {'role': 'assistant', 'stop_reason': 'tool_use', 'content': [
                    {'type': 'tool_use', 'id': 'handback-id', 'name': 'SubagentHandback', 'input': {'message': 'GATE_RELEASED'}}]}}
        delivered = {**prompt, 'timestamp': '2026-10-01T14:00:05+00:00', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'handback-id', 'content': 'accepted', 'is_error': False}]}}
        goodbye = {**later, 'timestamp': '2026-10-01T14:00:06+00:00',
                   'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done.'}]}}
        child.write_text(''.join(json.dumps(row) + '\n' for row in (prompt, handback, delivered, goodbye)), encoding='utf-8')
        native.require_literal_worker('claude', run, home, fixture.project)
        delivered['message']['content'][0]['is_error'] = True
        child.write_text(''.join(json.dumps(row) + '\n' for row in (prompt, handback, delivered, goodbye)), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            native.require_literal_worker('claude', run, home, fixture.project)
        child.write_text(original, encoding='utf-8')
        for captures in ([clock, clock], [{**clock, 'session_id': 'foreign'}]):
            with self.assertRaises(RuntimeError):
                native.require_literal_worker('claude', legacy, home, fixture.project, captures)
        rows[0]['message']['content'][0]['input']['model'] = 'foreign'
        parent.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            native.require_literal_worker('claude', run, home, fixture.project)

    def test_literal_worker_acceptance_requires_exact_one_report_and_canonical_native_parent(self):
        import native_routing_smoke as smoke
        lead_path = '/root/symphony_lead_model_medium'
        worker_path = lead_path + '/symphony_worker_model_medium'
        worker = {'identity': 'worker', 'role': 'worker', 'state': 'completed',
                  'requested_tier': 'model', 'requested_effort': 'medium'}
        run = {'lead_identity': 'lead', 'delegations': [worker]}
        lead_rows = [{'type': 'session_meta', 'payload': {'id': 'lead', 'agent_path': lead_path}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'launch',
             'name': 'spawn_agent', 'arguments': json.dumps({'task_name': 'symphony_worker_model_medium',
             'model': 'model', 'reasoning_effort': 'medium', 'fork_turns': 'none', 'message': 'SYMPHONY_ROLE: worker\nfixture'})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'launch',
             'output': json.dumps({'task_name': worker_path})}}]
        rows = [{'type': 'session_meta', 'payload': {'id': 'worker', 'agent_path': worker_path,
                 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'lead'}}}}},
                {'type': 'turn_context', 'payload': {'turn_id': 'worker-turn', 'model': 'model', 'effort': 'medium'}},
                {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:01+00:00', 'payload': {'type': 'task_started', 'turn_id': 'worker-turn'}},
                {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:02+00:00', 'payload': {'type': 'task_complete', 'turn_id': 'worker-turn', 'last_agent_message': 'GATE_RELEASED'}}]

        def check(values, child_rows):
            with patch.object(smoke, 'native_rows', side_effect=[lead_rows, child_rows]):
                native.require_literal_worker('codex', values, Path('/native'))

        check(run, rows)
        for values, child_rows in (({**run, 'delegations': [worker, worker]}, rows),
                                  ({**run, 'delegations': [{**worker, 'state': 'failed'}]}, rows),
                                  (run, [*rows[:-1], {'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': 'claimed'}}]),
                                  (run, [{**rows[0], 'payload': {**rows[0]['payload'], 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}, *rows[1:]]),
                                  (run, [rows[0], {**rows[1], 'payload': {**rows[1]['payload'], 'model': 'foreign'}}, *rows[2:]]),
                                  (run, [*rows, {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'future'}}]),
                                  (run, [*rows[:-1], {**rows[-1], 'timestamp': '2026-10-01T00:00:00+00:00'}]),
                                  (run, [*rows, {'type': 'event_msg', 'payload': {'type': 'error', 'turn_id': 'worker-turn'}}])):
            with self.assertRaises(RuntimeError):
                check(values, child_rows)

    def test_assessed_lead_set_excludes_only_exact_completed_native_fast_escalation(self):
        import native_routing_smoke as smoke
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            lead = {'identity': 'lead', 'role': 'lead', 'state': 'completed', 'requested_tier': 'model', 'requested_effort': 'medium'}
            fast = {**lead, 'identity': 'fast'}
            run = {'lead_identity': 'lead', 'session_id': 'root', 'delegations': [lead, fast],
                   'assessment': {'_fast_escalated': True, '_fast_route': {'model': 'model', 'effort': 'medium'}}}
            def header(identity, name, parent='root'):
                return {'type': 'session_meta', 'payload': {'id': identity, 'cwd': str(project),
                    'agent_path': '/root/' + name, 'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent}}}}}
            canonical = [header('lead', 'symphony_lead_model_medium'), {'type': 'event_msg',
                         'timestamp': '2026-10-01T00:00:03+00:00', 'payload': {'type': 'task_started', 'turn_id': 'assessed'}}]
            rows = [header('fast', 'symphony_lead_fast_model_medium'),
                    {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'model': 'model', 'effort': 'medium'}},
                    {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:01+00:00', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
                    {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:02+00:00', 'payload': {'type': 'task_complete', 'turn_id': 'turn',
                     'last_agent_message': 'SYMPHONY_FAST_DECISION: escalate'}}]
            def check(values, fast_rows):
                with patch.object(smoke, 'native_rows', side_effect=[canonical, fast_rows]):
                    native.require_assessed_lead_set(values, project, project, 'lead')
            check(run, rows)
            for values, other in (({**run, 'delegations': [lead, fast, {**fast, 'identity': 'extra'}]}, rows),
                                  ({**run, 'delegations': [lead, {**fast, 'state': 'failed'}]}, rows),
                                  (run, [header('fast', 'symphony_lead_model_medium'), *rows[1:]]),
                                  (run, [header('fast', 'symphony_lead_fast_model_medium', 'foreign'), *rows[1:]]),
                                  (run, rows + [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'future'}}]),
                                  (run, [*rows[:-1], {**rows[-1], 'payload': {**rows[-1]['payload'], 'turn_id': 'foreign'}}]),
                                  (run, [*rows[:-1], {**rows[-1], 'timestamp': '2026-10-01T00:00:04+00:00'}])):
                with self.assertRaises(RuntimeError):
                    check(values, other)

    def test_second_child_schedule_requires_exact_native_canonical_lead_proof(self):
        record = {'event': 'SubagentStart', 'native_gate_label': 'a', 'session_id': 'root',
                  'agent_id': 'lead', 'native_metadata': {'own_header_identity_matches': True,
                  'native_parent_matches_root': True, 'native_task_role': 'lead', 'declared_assessed_lead_matches': True}}
        native.require_codex_gate_lead([record], 'a', 'root', 'lead')
        for field, value in (('own_header_identity_matches', False),
                             ('native_parent_matches_root', False), ('native_task_role', 'worker'),
                             ('declared_assessed_lead_matches', False)):
            with self.subTest(field=field):
                bad = {**record, 'native_metadata': {**record['native_metadata'], field: value}}
                with self.assertRaises(RuntimeError):
                    native.require_codex_gate_lead([bad], 'a', 'root', 'lead')
        for rows in ([], [record, record], [{**record, 'session_id': 'foreign'}],
                     [{**record, 'agent_id': 'worker'}]):
            with self.assertRaises(RuntimeError):
                native.require_codex_gate_lead(rows, 'a', 'root', 'lead')

    def test_assessed_fixture_lead_owns_one_literal_worker_and_keeps_recovery(self):
        from plugins.symphony.symphony.routing import Assessment, route_for
        profiles = json.loads((PLUGIN / 'profiles.json').read_text(encoding='utf-8'))['providers']
        for provider, profile_id in (('codex', 'base'), ('codex', 'full'), ('codex', 'latest'),
                                     ('claude', 'sonnet-5-5')):
            for recover in (False, True):
                with self.subTest(provider=provider, profile=profile_id, recover=recover):
                    root = native.prompt(provider, 'a', recover, Path('/tmp/fixture'), codex_profile=profile_id)
                    if provider == 'codex':
                        lead = json.loads(re.search(r'^LEAD_SPAWN_PACKET: (.+)$', root, re.MULTILINE)[1])['message']
                    else:
                        lead = json.loads(re.search(r'LEAD_TASK_TEXT: (.+)$', root, re.MULTILINE)[1])
                    route = json.loads(re.search(r'^SYMPHONY_ROUTE: (.+)$', lead, re.MULTILINE)[1])
                    self.assertEqual(route['topology'], route_for(Assessment(
                        route['size'], route['complexity'], risk=route['risk'])).execution)
                    packet = json.loads(re.search(r'WORKER_SPAWN_PACKET: (.+)$', lead, re.MULTILINE)[1])
                    profile = next(p for p in profiles[provider]['profiles'] if p['id'] == profile_id)
                    selected = profile['matrix']['small/simple']
                    if provider == 'codex':
                        self.assertEqual((packet['model'], packet['reasoning_effort']),
                                         (selected['model'], selected['effort']))
                        self.assertEqual(packet['fork_turns'], 'none')
                        self.assertEqual(packet['task_name'], 'symphony_worker_' +
                                         re.sub(r'\W', '_', selected['model']) + '_' + selected['effort'])
                    else:
                        self.assertEqual(packet['subagent_type'],
                                         f"symphony:symphony-worker-{selected['model']}-{selected['effort']}")
                    message = packet.get('message', packet.get('prompt'))
                    self.assertTrue(message.startswith('SYMPHONY_ROLE: worker\n'))
                    own = json.loads(message.split('\n', 1)[1])
                    self.assertEqual((own['size'], own['complexity']), ('small', 'simple'))
                    self.assertEqual(own['return_contract'], 'Return exactly GATE_RELEASED without Markdown.')
                    self.assertIn('YOU as the canonical lead must spawn exactly one', lead)
                    self.assertIn('Await its successful native result and verify', lead)
                    self.assertIn('do not spawn duplicate work', lead)
                    self.assertNotIn('Do not delegate or create a worktree.', lead)
                    self.assertIn('the root never substitutes', lead)
                    self.assertIn('Only the external harness releases', root)
                    if recover:
                        self.assertIn('SYMPHONY_OUTCOME: {"status":"blocked"}', lead)
                        self.assertIn('SAME lead', root)

    def test_assessor_capture_exports_metadata_and_admission_facts_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'project'
            project.mkdir()
            state = root / 'state'
            state.mkdir()
            script = root / 'capture.py'
            script.write_text(native.CODEX_HOOK_CAPTURE, encoding='utf-8')
            transcript = root / 'child.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'assessor-a',
                'agent_path': '/root/symphony_assessor_gpt_6_luna_high',
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root-a'}}}}}
            turn = {'type': 'turn_context', 'payload': {'turn_id': 'turn-a',
                                                     'model': 'gpt-6-luna', 'effort': 'high'}}
            secret = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'PRIVATE_AUTH_SENTINEL'}]}}
            payload = {'cwd': str(project), 'session_id': 'root-a', 'agent_id': 'assessor-a',
                       'agent_type': 'default', 'turn_id': 'turn-a', 'agent_transcript_path': str(transcript)}
            path = native.state_file(state, project)
            env = {**os.environ, 'SYMPHONY_STATE_DIR': str(state),
                   'SYMPHONY_NATIVE_ASSESSOR_MODEL': 'gpt-6-luna', 'SYMPHONY_NATIVE_ASSESSOR_EFFORT': 'high'}
            for admitted, rows in ((False, [header, turn, secret]), (True, [header, turn, secret]),
                                    (False, [header, secret])):
                transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                run = {'delegations': [{'identity': 'assessor-a', 'role': 'assessor'}]}
                event = {'kind': 'delegation_updated', 'payload': {'identity': 'assessor-a', 'state': 'working'}}
                path.write_text(json.dumps({'active_runs': {'codex:root-a': run} if admitted else {},
                                            'event_history': [event] if admitted else []}), encoding='utf-8')
                result = subprocess.run([sys.executable, '-I', str(script), 'SubagentStart',
                                         str(root / 'codex-hook-capture'), 'codex'],
                                        input=json.dumps(payload), env=env, capture_output=True,
                                        text=True, encoding='utf-8', timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                records = native.codex_hook_capture_summary(root)['records']
                capture = records[-1]
                self.assertTrue(capture['native_metadata']['own_header_identity_matches'])
                self.assertTrue(capture['native_metadata']['native_parent_matches_root'])
                self.assertEqual(capture['native_metadata']['native_task_role'], 'assessor')
                self.assertEqual(capture['native_metadata']['callback_turn_context_present'], len(rows) == 3)
                self.assertEqual(capture['admission_at_capture']['owner_run_present'], admitted)
                self.assertEqual(capture['admission_at_capture']['child_start_recorded'], admitted)
                self.assertNotIn('PRIVATE_AUTH_SENTINEL', json.dumps(records))
                self.assertNotIn('symphony_assessor_gpt_6_luna_high', json.dumps(capture['native_metadata']))
    def test_codex_native_spawn_packets_follow_packaged_base_matrix(self):
        base = next(item for item in json.loads((PLUGIN / "profiles.json").read_text())
                    ["providers"]["codex"]["profiles"] if item["id"] == "base")
        assessor, lead = native.codex_fixture_roles()
        self.assertEqual((lead["model"], lead["effort"]),
                         (base["matrix"]["small/simple"]["model"],
                          base["matrix"]["small/simple"]["effort"]))
        self.assertEqual((assessor["model"], assessor["effort"]),
                         (base["tiers"]["strongest"], "high"))
        prompt = native.prompt("codex", "a", True, Path("/tmp/native-project"))
        for marker, selected in (("ASSESSOR_SPAWN_PACKET", assessor),
                                 ("LEAD_SPAWN_PACKET", lead)):
            packet = json.loads(re.search(marker + r": (\{[^\n]+\})", prompt).group(1))
            self.assertEqual(packet["task_name"], selected["task_name"])
            self.assertEqual(packet["model"], selected["model"])
            self.assertEqual(packet["reasoning_effort"], selected["effort"])
            self.assertRegex(packet["task_name"], r"^[a-z0-9_]+$")
        self.assertIn("Do not call followup_task until spawn_agent has returned success", prompt)
        deferred = native.prompt("codex", "a", True, Path("/tmp/native-project"),
                                 defer_recovery=True)
        self.assertIn("End this root turn with the original run recovering", deferred)
        self.assertIn("Do not call followup_task or spawn another lead", deferred)
        self.assertNotIn("Use followup_task with its original task_name", deferred)
        deferred_packet = json.loads(re.search(
            r"LEAD_SPAWN_PACKET: (\{[^\n]+\})", deferred).group(1))
        self.assertIn("native hook holds the unfinished task", deferred_packet["message"])
        self.assertIn("WAIT", deferred_packet["message"])
        self.assertIn("only READY permits", deferred_packet["message"])

    def test_mixed_codex_upgrade_uses_packaged_full_route(self):
        profiles = json.loads((PLUGIN / "profiles.json").read_text())["providers"]["codex"]["profiles"]
        project = Path("/tmp/native-project")
        for label, profile_id in (("a", "full"), ("b", "latest")):
            with self.subTest(profile=profile_id):
                profile = next(item for item in profiles if item["id"] == profile_id)
                assessor, lead = native.codex_fixture_roles(profile_id)
                self.assertEqual((lead["model"], lead["effort"]),
                                 (profile["matrix"]["small/complex"]["model"],
                                  profile["matrix"]["small/complex"]["effort"]))
                self.assertEqual((assessor["model"], assessor["effort"]),
                                 (profile["tiers"]["strongest"], "high"))
                fixture_prompt = native.prompt("codex", label, label == "a", project,
                                               defer_recovery=label == "a",
                                               codex_profile=profile_id)
                self.assertIn('"size":"small","complexity":"complex"', fixture_prompt)
                for marker, role in (("ASSESSOR_SPAWN_PACKET", assessor),
                                     ("LEAD_SPAWN_PACKET", lead)):
                    packet = json.loads(re.search(marker + r": (\{[^\n]+\})", fixture_prompt).group(1))
                    self.assertEqual((packet["task_name"], packet["model"],
                                      packet["reasoning_effort"]),
                                     (role["task_name"], role["model"], role["effort"]))
                with tempfile.TemporaryDirectory() as temporary, \
                        patch.object(native.shutil, "which", return_value="codex"), \
                        patch.object(native.subprocess, "Popen") as popen:
                    native.launch("codex", project, label, label == "a",
                                  {"SYMPHONY_PROFILE": profile_id}, Path(temporary), 3, "",
                                  defer_recovery=label == "a")
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], lead["model"])
                self.assertIn(lead["task_name"], command[-1])

    def test_windows_native_observer_retries_only_bounded_lock_contention(self):
        before = native.OBSERVER_SNAPSHOT_LOCK_RETRIES
        reader = Mock(side_effect=[TimeoutError("writer owns lock"), '{"active_runs": {}}'])
        with patch.object(native.time, "sleep"):
            self.assertEqual(native.observer_locked_read(reader), '{"active_runs": {}}')
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(native.OBSERVER_SNAPSHOT_LOCK_RETRIES, before + 1)
        with self.assertRaisesRegex(TimeoutError, "observer remained locked"):
            native.observer_locked_read(Mock(side_effect=TimeoutError("writer owns lock")),
                                        timeout=0)
        with self.assertRaises(ValueError):
            native.observer_locked_read(Mock(side_effect=ValueError("invalid state")))

    def test_codex_native_hook_gate_holds_declared_assessed_task_not_fast_or_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "capture.py"
            script.write_text(native.CODEX_HOOK_CAPTURE)
            gate, capture = root / "gate", root / "codex-hook-capture"
            env = {**os.environ, 'SYMPHONY_NATIVE_GATE_DIR': str(gate),
                   'SYMPHONY_NATIVE_GATE_LABEL': 'a', 'SYMPHONY_NATIVE_GATE_LEAD_NAME': 'symphony_lead_fixture'}

            def payload(identity, task):
                path = root / (identity + '.jsonl')
                path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': identity,
                    'agent_path': '/root/' + task, 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root-a'}}}}}) + '\n')
                return {'session_id': 'root-a', 'agent_id': identity, 'agent_type': 'default',
                        'agent_transcript_path': str(path)}
            root_start = subprocess.run(
                [sys.executable, "-I", str(script), "SessionStart", str(capture), "codex"],
                input=json.dumps({"session_id": "root-a"}),
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(root_start.returncode, 0)
            self.assertEqual(json.loads((gate / "a.root.json").read_text())["session_id"], "root-a")
            for identity, task in (('fast', 'symphony_lead_fast_model_medium'), ('foreign', 'symphony_lead_other')):
                result = subprocess.run([sys.executable, '-I', str(script), 'SubagentStart', str(capture), 'codex'],
                    input=json.dumps(payload(identity, task)), env=env, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0)
                self.assertFalse((gate / 'a.first.json').exists())
                self.assertFalse((gate / 'a.ready').exists())
            first = subprocess.run(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "codex"],
                input=json.dumps(payload('assessor-a', 'symphony_assessor_fixture')),
                env=env,
                text=True, capture_output=True, timeout=5)
            self.assertEqual(first.returncode, 0)
            self.assertFalse((gate / "a.ready").exists())
            self.assertEqual(json.loads((gate / "a.first.json").read_text())["agent_id"],
                             "assessor-a")
            process = subprocess.Popen(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "codex"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                env=env)
            try:
                process.stdin.write(json.dumps(payload('lead-a', 'symphony_lead_fixture')))
                process.stdin.close()
                deadline = time.monotonic() + 5
                while not (gate / "a.ready").is_file():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertIsNone(process.poll())
                self.assertEqual(json.loads((gate / "a.ready").read_text())["agent_id"], "lead-a")
                frozen = (gate / 'a.ready').read_bytes()
                for later in ('worker-a', 'lead-a'):
                    replay = subprocess.run(
                        [sys.executable, '-I', str(script), 'SubagentStart', str(capture), 'codex'],
                        input=json.dumps(payload(later, 'symphony_lead_fixture' if later == 'lead-a' else 'symphony_worker_fixture')),
                        env=env, text=True, capture_output=True, timeout=5)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                    self.assertEqual((gate / 'a.ready').read_bytes(), frozen)
                    self.assertEqual(json.loads((gate / 'a.lead.json').read_text())['agent_id'], 'lead-a')
                self.assertIsNone(process.poll(), 'later callbacks must not release the original barrier')
                (gate / "release").touch()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                (gate / "release").touch()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_codex_old_root_stop_hold_is_one_shot_and_session_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "capture.py"
            script.write_text(native.CODEX_HOOK_CAPTURE)
            gate, capture = root / "gate", root / "codex-hook-capture"
            env = {**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                   "SYMPHONY_NATIVE_GATE_LABEL": "a",
                   "SYMPHONY_NATIVE_HOLD_OLD_STOP": "1"}
            command = [sys.executable, "-I", str(script)]
            started = subprocess.run(
                [*command, "SessionStart", str(capture), "codex"],
                input=json.dumps({"session_id": "root-a"}), env=env,
                text=True, capture_output=True, timeout=5)
            self.assertEqual(started.returncode, 0)
            foreign = subprocess.run(
                [*command, "Stop", str(capture), "codex"],
                input=json.dumps({"session_id": "root-b"}), env=env,
                text=True, capture_output=True, timeout=5)
            self.assertEqual(foreign.returncode, 0)
            self.assertFalse((gate / "a.stop-ready.json").exists())
            process = subprocess.Popen(
                [*command, "Stop", str(capture), "codex"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, text=True)
            try:
                process.stdin.write(json.dumps({"session_id": "root-a"}))
                process.stdin.close()
                deadline = time.monotonic() + 5
                while not (gate / "a.stop-ready.json").is_file():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                held = json.loads((gate / "a.stop-ready.json").read_text())
                self.assertEqual(held["session_id"], "root-a")
                self.assertIsNone(process.poll())
                second = subprocess.run(
                    [*command, "Stop", str(capture), "codex"],
                    input=json.dumps({"session_id": "root-a"}), env=env,
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(second.returncode, 0)
                self.assertIsNone(process.poll())
                (gate / "a.stop-release").touch()
                self.assertEqual(process.wait(timeout=5), 0)
                markers = native.codex_hook_capture_summary(root)["records"]
                held_record = [item for item in markers
                               if item["invocation_id"] == held["invocation_id"]]
                self.assertEqual(len(held_record), 1)
                self.assertTrue(held_record[0]["exit_marker_written"])
                self.assertGreaterEqual(held_record[0]["finished_ns"],
                                        (gate / "a.stop-release").stat().st_mtime_ns)
            finally:
                (gate / "a.stop-release").touch()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_native_hook_gate_holds_distinct_leads_until_external_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "capture.py"
            script.write_text(native.CODEX_HOOK_CAPTURE)
            gate, capture = root / "gate", root / "capture"
            assessor = subprocess.run(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "claude"],
                input=json.dumps({"session_id": "root-a", "agent_id": "assessor",
                                  "agent_type": "symphony:symphony-assessor-claude-sonnet-5-high"}),
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(assessor.returncode, 0)
            self.assertFalse(gate.exists())
            for label in ("a", "b"):
                started = subprocess.run(
                    [sys.executable, "-I", str(script), "SessionStart", str(capture), "claude"],
                    input=json.dumps({"session_id": f"root-{label}"}),
                    env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                         "SYMPHONY_NATIVE_GATE_LABEL": label},
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(started.returncode, 0)
            foreign = subprocess.run(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "claude"],
                input=json.dumps({"session_id": "foreign-root", "agent_id": "foreign-lead",
                                  "agent_type": "symphony:symphony-lead-claude-sonnet-5-low"}),
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(foreign.returncode, 0)
            self.assertFalse((gate / "a.ready").exists())
            self.assertFalse((gate / "b.ready").exists())
            processes = {}
            try:
                for label in ("a", "b"):
                    processes[label] = subprocess.Popen(
                        [sys.executable, "-I", str(script), "SubagentStart", str(capture), "claude"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True,
                        env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                             "SYMPHONY_NATIVE_GATE_LABEL": "b" if label == "a" else "a"})
                    processes[label].stdin.write(json.dumps({
                        "session_id": f"root-{label}", "agent_id": f"lead-{label}",
                        "agent_type": "symphony:symphony-lead-claude-sonnet-5-low"}))
                    processes[label].stdin.close()
                deadline = time.monotonic() + 5
                while not all((gate / f"{label}.ready").is_file() for label in processes):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                for label, process in processes.items():
                    self.assertIsNone(process.poll())
                    self.assertEqual(json.loads((gate / f"{label}.ready").read_text())
                                     ["session_id"], f"root-{label}")
                starts = [json.loads(path.read_text()) for path in capture.glob("*-entry.json")]
                self.assertEqual({(record["session_id"], record["native_gate_label"],
                                   record["native_gate_env_label"])
                                  for record in starts if record.get("native_gate_label")},
                                 {("root-a", "a", "b"), ("root-b", "b", "a")})
                (gate / "release").touch()
                for process in processes.values():
                    self.assertEqual(process.wait(timeout=5), 0)
            finally:
                (gate / "release").touch()
                for process in processes.values():
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    process.stderr.close()

    def test_claude_second_wake_requires_original_reconciled_run_and_empty_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            run = {"run_id": "original", "lead_identity": "lead-1", "status": "completing",
                   "outcome": {"status": "completed"},
                   "delegations": [{"role": "lead", "identity": "lead-1", "state": "completed"}]}
            document = {"active_runs": {"claude:root-a": run}}
            ready = lambda doc: native.claude_finalization_ready(
                doc, "root-a", "original", "lead-1", state_dir)
            self.assertTrue(ready(document))
            self.assertFalse(ready({"active_runs": {"claude:root-a":
                                    {**run, "status": "active"}}}))
            self.assertFalse(ready({"active_runs": {"claude:root-a":
                                    {**run, "delegations": [{"role": "lead", "identity": "lead-1",
                                                           "state": "working"}]}}}))
            pending = state_dir / ".session-child.json"
            pending.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                           "overflow": False}))
            self.assertFalse(ready(document))
            pending.write_text(json.dumps({"pending": [], "overflow": False}))
            self.assertTrue(ready(document))
            for changed in ({**run, "run_id": "new-run"},
                            {**run, "lead_identity": "new-lead"},
                            {**run, "delegations": [*run["delegations"],
                                                      {"role": "lead", "identity": "replacement",
                                                       "state": "completed"}]}):
                with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                    ready({"active_runs": {"claude:root-a": changed}})

    def test_failure_keeps_full_artifact_without_flooding_ci_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = {"provider": "claude", "cases": [{"native_trace": "x" * 100000}]}
            stderr = io.StringIO()
            with patch.object(native, "prepare_baseline_capture",
                              side_effect=RuntimeError("native case failed")), \
                 patch.object(native, "failure_state", return_value=report), \
                 patch.object(sys, "argv", ["native_managed_concurrency.py", "--provider", "claude"]), \
                 patch.object(sys, "stderr", stderr), \
                 patch.dict(native.os.environ, {"SYMPHONY_NATIVE_DIAGNOSTICS_DIR": temporary}):
                self.assertEqual(native.main(), 1)
            saved = json.loads((Path(temporary) / "native-managed-claude-failure.json").read_text())
            self.assertEqual(len(saved["cases"][0]["native_trace"]), 100000)
            self.assertLess(len(stderr.getvalue()), 1000)

    def test_success_keeps_full_hook_trace_in_artifact_without_flooding_ci_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = {"case": "same-worktree", "completed": ["a", "b"],
                      "native_hook_capture": {"records": ["x" * 100000]}}
            stdout = io.StringIO()
            with patch.object(native, "prepare_baseline_capture", return_value={}), \
                 patch.object(native, "check_case", return_value=result), \
                 patch.object(sys, "argv", ["native_managed_concurrency.py", "--provider", "claude"]), \
                 patch.object(sys, "stdout", stdout), \
                 patch.dict(native.os.environ, {"SYMPHONY_NATIVE_DIAGNOSTICS_DIR": temporary}):
                self.assertEqual(native.main(), 0)
            saved = json.loads((Path(temporary) / "native-managed-claude-receipt.json").read_text())
            self.assertEqual(len(saved["native_managed"][0]["native_hook_capture"]["records"][0]),
                             100000)
            self.assertEqual(json.loads(stdout.getvalue())["native_managed"][0]["completed"],
                             ["a", "b"])
            self.assertLess(len(stdout.getvalue()), 1000)

    def test_opaque_transport_cannot_replace_durable_recovery_events(self):
        def document(kinds, identity="original"):
            return {"event_history": [{"kind": kind, "payload": {"identity": identity}}
                                      for kind in kinds]}
        native.require_recovered_lead_events(document(["lead_failed", "lead_completed"]), "original")
        native.require_recovered_lead_events(
            document(["lead_failed", "lead_failed", "lead_completed", "lead_completed"]), "original")
        for kinds in ([], ["lead_completed"], ["lead_failed"], ["lead_completed", "lead_failed"]):
            with self.subTest(kinds=kinds), self.assertRaises(RuntimeError):
                native.require_recovered_lead_events(document(kinds), "original")
        with self.assertRaises(RuntimeError):
            native.require_recovered_lead_events(document(["lead_failed", "lead_completed"], "replacement"),
                                                  "original")

    def test_recovery_events_required_only_for_codex_update_original_lead(self):
        plain_completion = {"event_history": [
            {"kind": "lead_completed", "payload": {"identity": "original"}}]}
        for provider, update, label in (("codex", None, "a"),
                                        ("codex", {"old_version": "1.5.1"}, "b"),
                                        ("claude", {"old_version": "1.5.1"}, "a")):
            with self.subTest(provider=provider, update=bool(update), label=label):
                native.require_case_recovery_events(
                    plain_completion, "original", provider, update, label)
        native.require_case_recovery_events(
            plain_completion, "original", "codex", {"old_version": "1.5.1"},
            "a", "completed")
        with self.assertRaisesRegex(RuntimeError, "ordered durable failed and completed"):
            native.require_case_recovery_events(
                plain_completion, "original", "codex", {"old_version": "1.5.1"},
                "a", "recovering")
        native.require_case_recovery_events(
            {"event_history": [
                {"kind": "lead_failed", "payload": {"identity": "original"}},
                {"kind": "lead_completed", "payload": {"identity": "original"}}]},
            "original", "codex", {"old_version": "1.5.1"}, "a", "recovering")
        with self.assertRaisesRegex(RuntimeError, "original recovering or completed"):
            native.require_case_recovery_events(
                plain_completion, "original", "codex", {"old_version": "1.5.1"},
                "a", None)

    def test_old_codex_wait_interruption_requires_latest_hooked_completion(self):
        from hashlib import sha256
        run = {"provider": "codex", "session_id": "root-a", "run_id": "run-a",
               "lead_identity": "lead-a", "status": "recovering",
               "delegations": [{"identity": "assessor-a", "state": "completed"}]}
        document = {"active_runs": {"codex:root-a": run}, "recent_runs": []}
        captured = {"records": [
            {"event": "SubagentStop", "session_id": "root-a", "agent_id": "lead-a",
             "turn_id": turn, "exit_marker_written": True, "script_exit_marker_code": 0}
            for turn in ("blocked-turn", "completed-turn")]}
        terminal = sha256(b"completed-turn").hexdigest()[:12]
        host = {"lead_turns": [{"turn_hash": terminal, "completed": True,
                                "reported_outcome": "completed"}],
                "last_root_function_call": {"name": "wait_agent", "return_recorded": False},
                "root_tool_calls_tail": [{"name": "wait_agent", "wait_timeout_ms": 3600000,
                                          "call_hash": "wait-a"}]}
        proof = native.verified_old_codex_wait(
            document, "root-a", "run-a", "lead-a", captured, host)
        self.assertEqual("old_runtime_wait_after_hooked_completed_turn", proof["reason"])
        for changed_doc, changed_capture, changed_host in (
            ({"active_runs": {"codex:root-a": {**run, "lead_identity": "other"}},
              "recent_runs": []}, captured, host),
            (document, {"records": captured["records"][:1]}, host),
            (document, captured, {**host, "lead_turns": [
                {"turn_hash": terminal, "completed": False, "reported_outcome": "completed"}]}),
            (document, captured, {**host, "lead_turns": [
                {"turn_hash": "foreign-turn", "completed": True,
                 "reported_outcome": "completed"}]}),
            (document, captured, {**host, "last_root_function_call":
                                   {"name": "wait_agent", "return_recorded": True}}),
            ({"active_runs": {"codex:root-a": {**run, "delegations": [
                {"identity": "worker-a", "state": "working"}]}}, "recent_runs": []},
             captured, host),
        ):
            with self.subTest(changed_doc=changed_doc, changed_capture=changed_capture,
                              changed_host=changed_host), self.assertRaises(RuntimeError):
                native.verified_old_codex_wait(
                    changed_doc, "root-a", "run-a", "lead-a", changed_capture, changed_host)

    def test_codex_update_preserves_original_completed_or_recovering_run(self):
        original = {"provider": "codex", "session_id": "root-a", "run_id": "run-a",
                    "lead_identity": "lead-a", "status": "recovering"}
        recovering = {"active_runs": {"codex:root-a": original}, "recent_runs": []}
        self.assertEqual("recovering", native.codex_pre_resume_state(
            recovering, "root-a", "run-a", "lead-a"))
        completed = {"active_runs": {}, "recent_runs": [
            {**original, "status": "completed", "outcome": {"status": "completed"}}]}
        self.assertEqual("completed", native.codex_pre_resume_state(
            completed, "root-a", "run-a", "lead-a"))
        for changed in ({**completed, "recent_runs": [
                            {**completed["recent_runs"][0], "lead_identity": "replacement"}]},
                        {**completed, "recent_runs": [
                            {**completed["recent_runs"][0], "run_id": "new-run"}]},
                        {**completed, "active_runs": {"codex:root-b": original}}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                native.codex_pre_resume_state(changed, "root-a", "run-a", "lead-a")

    def test_codex_old_terminal_branches_require_native_proof(self):
        first = {"turn_id": "first"}
        second = {"turn_id": "second"}
        blocked = {"turn_hash": native.sha256(b"first").hexdigest()[:12],
                   "completed": True, "reported_outcome": "blocked"}
        completed = {"turn_hash": native.sha256(b"second").hexdigest()[:12],
                     "completed": True, "reported_outcome": "completed"}
        markerless = {**completed, "reported_outcome": "other"}
        self.assertEqual("original_completed", native.require_codex_old_lead_terminal(
            "completed", [first], [completed]))
        self.assertEqual("same_id_recovered", native.require_codex_old_lead_terminal(
            "recovering", [first, second], [blocked, completed]))
        self.assertEqual("same_id_unreconciled", native.require_codex_old_lead_terminal(
            "recovering", [first], [blocked]))
        self.assertEqual("same_id_unreconciled", native.require_codex_old_lead_terminal(
            "recovering", [first, second], [blocked, markerless]))
        self.assertEqual("same_id_unreconciled", native.require_codex_old_lead_terminal(
            "recovering", [first, second], [blocked, {**markerless,
                                                    "reported_outcome": "missing"}]))
        self.assertEqual("same_id_unreconciled", native.require_codex_old_lead_terminal(
            "recovering", [first, second], [blocked, {**blocked,
                                                    "turn_hash": completed["turn_hash"]}]))
        for state, stops, turns in (
            ("completed", [first], [blocked]),
            ("completed", [first], [{**completed, "completed": False}]),
            ("recovering", [first], [blocked, completed]),
            ("recovering", [], [blocked]),
            ("recovering", [first, second], [blocked, {**markerless,
                                                    "turn_hash": "foreign"}]),
            ("recovering", [first, second], [blocked, {**markerless,
                                                    "completed": False}]),
            ("unknown", [first, second], [blocked, completed]),
        ):
            with self.subTest(state=state, stops=stops, turns=turns), self.assertRaises(RuntimeError):
                native.require_codex_old_lead_terminal(state, stops, turns)

    def test_held_codex_stop_waits_for_exact_capture_entry(self):
        receipt = {"event": "Stop", "session_id": "root-a",
                   "invocation_id": "held-1", "native_stop_hold_label": "a",
                   "native_task_wait": True}
        with patch.object(native, "codex_hook_capture_summary", side_effect=[
                {"records": []}, {"records": [{**receipt, "session_id": "root-b"}]},
                {"records": [receipt]}]) as capture, patch.object(native.time, "sleep"):
            self.assertEqual(receipt, native.held_codex_stop_capture(
                Path("/tmp/native"), "root-a", "held-1", native.time.monotonic() + 1))
        self.assertEqual(3, capture.call_count)

    def test_native_stop_sidecar_records_only_outcome_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "codex-hook-capture"
            for message in ('SYMPHONY_OUTCOME: {"status":"completed"}',
                            'SYMPHONY_OUTCOME: {"status":[]}',
                            'SYMPHONY_OUTCOME: {"status":{}}',
                            'SYMPHONY_OUTCOME: {broken}',
                            "Completed without a marker"):
                payload = {"session_id": "root-a", "agent_id": "lead-a",
                           "turn_id": message[:4],
                           "last_assistant_message": message}
                subprocess.run(
                    [sys.executable, "-c", native.CODEX_HOOK_CAPTURE,
                     "SubagentStop", str(destination), "codex"],
                    input=json.dumps(payload), text=True, check=True,
                    capture_output=True,
                )
            outcomes = {record["reported_outcome"] for record in
                        native.codex_hook_capture_summary(root)["records"]}
            self.assertEqual({"completed", "missing"}, outcomes)
            self.assertNotIn('SYMPHONY_OUTCOME: {"status":"completed"}', json.dumps(
                native.codex_hook_capture_summary(root)))

    def test_native_markerless_parse_requires_new_latest_hooked_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            sessions = home / "sessions"
            sessions.mkdir()
            (sessions / "root-a.jsonl").write_text("{}\n")
            child_rows = [
                {"type": "event_msg", "timestamp": "2026-09-30T00:00:01+00:00",
                 "payload": {"type": "task_started", "turn_id": "blocked"}},
                {"type": "event_msg", "timestamp": "2026-09-30T00:00:02+00:00",
                 "payload": {"type": "task_complete", "turn_id": "blocked",
                             "last_agent_message": 'SYMPHONY_OUTCOME: {"status":"blocked"}'}},
                {"type": "event_msg", "timestamp": "2026-09-30T00:00:03+00:00",
                 "payload": {"type": "task_started", "turn_id": "markerless"}},
                {"type": "event_msg", "timestamp": "2026-09-30T00:00:04+00:00",
                 "payload": {"type": "task_complete", "turn_id": "markerless",
                             "last_agent_message": "The work is done."}},
            ]
            child = sessions / "lead-a.jsonl"
            child.write_text("".join(json.dumps(row) + "\n" for row in child_rows))
            turns = native.codex_host_trace(home, "root-a", "lead-a",
                                            home / "errors", strict=True)["lead_turns"]
            self.assertEqual([turn["reported_outcome"] for turn in turns],
                             ["blocked", "missing"])
            stops = [{"turn_id": "blocked"}, {"turn_id": "markerless"}]
            self.assertEqual("same_id_unreconciled", native.require_codex_old_lead_terminal(
                "recovering", stops, turns))
            stale = {"turn_hash": native.sha256(b"completed-middle").hexdigest()[:12],
                     "completed": True, "completed_at": "2026-09-30T00:00:03+00:00",
                     "started_at": "2026-09-30T00:00:02+00:00",
                     "reported_outcome": "completed"}
            with self.assertRaisesRegex(RuntimeError, "new original-lead turn"):
                native.require_codex_unreconciled_recovery(
                    [turns[0], stale, turns[1]], [turns[0], stale, turns[1]],
                    {stale["turn_hash"]})
            latest = {"turn_hash": native.sha256(b"candidate-latest").hexdigest()[:12],
                      "completed": True, "completed_at": "2026-09-30T00:00:06+00:00",
                      "started_at": "2026-09-30T00:00:05+00:00",
                      "reported_outcome": "completed"}
            native.require_codex_unreconciled_recovery(turns, [*turns, latest],
                                                     {latest["turn_hash"]})
            for changed, hashes in (({**latest, "reported_outcome": "missing"},
                                     {latest["turn_hash"]}),
                                    (latest, set()),
                                    ({**latest, "started_at": turns[1]["completed_at"]},
                                     {latest["turn_hash"]}),
                                    ({**latest, "completed_at": "2026-09-30T00:00:03+00:00"},
                                     {latest["turn_hash"]})):
                with self.subTest(changed=changed, hashes=hashes), \
                        self.assertRaisesRegex(RuntimeError, "new original-lead turn"):
                    native.require_codex_unreconciled_recovery(
                        turns, [*turns, changed], hashes)

    def test_native_gate_can_release_before_product_start_but_update_requires_working_leads(self):
        sessions = {"a": "root-a", "b": "root-b"}
        run_ids = {"a": "run-a", "b": "run-b"}
        leads = {"a": "lead-a", "b": "lead-b"}
        runs = {label: {"provider": "codex", "session_id": sessions[label],
                        "run_id": run_ids[label], "lead_identity": leads[label],
                        "status": "active", "delegations": [
                            {"role": "lead", "identity": leads[label], "state": "working"}]}
                for label in sessions}
        docs = {label: {"active_runs": {f"codex:{sessions[label]}": runs[label]}}
                for label in sessions}
        assessing = {"active_runs": {"codex:root-b":
                     {**runs["b"], "lead_identity": None, "status": "assessing",
                      "delegations": []}}}
        self.assertFalse(native.native_lead_registered(
            assessing, "codex", "root-b", "run-b", "lead-b"))
        with self.assertRaisesRegex(RuntimeError, "before in-flight update"):
            native.require_inflight_update_leads(
                {**docs, "b": assessing}, "codex", sessions, run_ids, leads)
        native.require_inflight_update_leads(docs, "codex", sessions, run_ids, leads)
        completed_b = {"active_runs": {"codex:root-b": {**runs["b"], "status": "completing",
                       "delegations": [{"role": "lead", "identity": "lead-b",
                                        "state": "completed"}]}}}
        with self.assertRaisesRegex(RuntimeError, "before in-flight update"):
            native.require_inflight_update_leads(
                {**docs, "b": completed_b}, "codex", sessions, run_ids, leads)
        with self.assertRaisesRegex(RuntimeError, "before in-flight update"):
            native.require_inflight_update_leads(
                docs, "codex", sessions, run_ids, {**leads, "b": "foreign-lead"})

    def test_registration_wait_allows_only_successful_claude_background_exit(self):
        processes = {"a": Mock(), "b": Mock()}
        processes["a"].poll.return_value = 0
        processes["b"].poll.return_value = None
        self.assertFalse(native.registration_cli_exited("claude", processes))
        self.assertTrue(native.registration_cli_exited("codex", processes))
        processes["a"].poll.return_value = 1
        self.assertTrue(native.registration_cli_exited("claude", processes))

    def test_live_update_requires_original_native_turn_to_span_source_removal(self):
        before = "2026-09-30T12:00:00+00:00"
        removed = native.timestamp_ns("2026-09-30T12:00:05+00:00")
        after = "2026-09-30T12:00:10+00:00"
        later = "2026-09-30T12:00:11+00:00"
        document = {"event_history": [{"kind": "lead_completed", "observed_at": after,
                                       "payload": {"identity": "lead-a"}}]}
        codex = {"lead_turns": [
            {"started_at": before, "completed_at": after, "completed": True,
             "reported_outcome": "blocked"},
            {"started_at": after, "completed_at": after, "completed": True,
             "reported_outcome": "completed"}]}
        claude = {"child_prompt_shape_supported": True, "child_turns": [
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": "end_turn", "marker": "completed"}]}
        claude_continued = {"child_prompt_shape_supported": True, "child_turns": [
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": None, "marker": "completed"},
            {"child_prompt_at": later, "last_assistant_at": later,
             "last_stop_reason": "end_turn", "marker": "completed"}]}
        for provider, trace in (("codex", codex), ("claude", claude),
                                ("claude", claude_continued)):
            with self.subTest(provider=provider):
                evidence = native.require_post_removal_lead_completion(
                    document, provider, "root-a", "lead-a", trace, removed)
                self.assertTrue(evidence["native_turn_spanned_removal"])
        for changed_doc, changed_trace in (
            ({"event_history": [{"kind": "lead_completed", "observed_at": after,
                                 "payload": {"identity": "foreign"}}]}, codex),
            (document, {"lead_turns": [{"started_at": before, "completed_at": before,
                                        "completed": True, "reported_outcome": "completed"}]}),
            (document, {"lead_turns": [{"started_at": after, "completed_at": after,
                                        "completed": True, "reported_outcome": "completed"}]}),
            (document, {"lead_turns": [{"started_at": before, "completed_at": after,
                                        "completed": True, "reported_outcome": "blocked"}]}),
        ):
            with self.subTest(changed_doc=changed_doc, changed_trace=changed_trace), \
                 self.assertRaises(RuntimeError):
                native.require_post_removal_lead_completion(
                    changed_doc, "codex", "root-a", "lead-a", changed_trace, removed)
        for changed in (
            {"child_prompt_at": before, "last_assistant_at": before,
             "last_stop_reason": "end_turn", "marker": "completed"},
            {"child_prompt_at": after, "last_assistant_at": after,
             "last_stop_reason": "end_turn", "marker": "completed"},
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": "tool_use", "marker": "completed"},
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": None, "marker": "completed"},
        ):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                native.require_post_removal_lead_completion(
                    document, "claude", "root-a", "lead-a",
                    {"child_prompt_shape_supported": True, "child_turns": [changed]}, removed)
        for first in (
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": None, "marker": None},
            {"child_prompt_at": before, "last_assistant_at": after,
             "last_stop_reason": "tool_use", "marker": "completed"},
            {"child_prompt_at": after, "last_assistant_at": after,
             "last_stop_reason": None, "marker": "completed"},
        ):
            with self.subTest(first=first), self.assertRaises(RuntimeError):
                native.require_post_removal_lead_completion(
                    document, "claude", "root-a", "lead-a",
                    {"child_prompt_shape_supported": True,
                     "child_turns": [first, claude_continued["child_turns"][1]]},
                    removed)
        with self.assertRaisesRegex(RuntimeError, "did not span"):
            native.require_post_removal_lead_completion(
                document, "claude", "root-a", "lead-a",
                {"child_prompt_shape_supported": True, "child_turns": [
                    {"child_prompt_at": before, "last_assistant_at": later,
                     "last_stop_reason": None, "marker": "completed"},
                    {"child_prompt_at": after, "last_assistant_at": after,
                     "last_stop_reason": "end_turn", "marker": "completed"}]},
                removed)
        with self.assertRaisesRegex(RuntimeError, "did not span"):
            native.require_post_removal_lead_completion(
                document, "claude", "root-a", "lead-a",
                {"child_prompt_shape_supported": True, "child_turns": [
                    claude_continued["child_turns"][0],
                    {"child_prompt_at": later, "last_assistant_at": after,
                     "last_stop_reason": "end_turn", "marker": "completed"}]},
                removed)
        with self.assertRaisesRegex(RuntimeError, "invalid timestamp"):
            native.timestamp_ns("not-a-timestamp")

    def test_claude_live_update_allows_one_original_turn_to_finish_just_before_removal(self):
        removed = native.timestamp_ns("2026-09-30T12:00:05+00:00")
        prompt = "2026-09-30T12:00:00+00:00"
        just_before = "2026-09-30T12:00:04.975+00:00"
        just_after = "2026-09-30T12:00:05.367+00:00"

        def document(lead_id, completed_at=just_after):
            return {"event_history": [{"kind": "lead_completed", "observed_at": completed_at,
                                       "payload": {"identity": lead_id}}]}

        def trace(completed_at):
            return {"child_prompt_shape_supported": True, "child_turns": [{
                "child_prompt_at": prompt, "last_assistant_at": completed_at,
                "last_stop_reason": "end_turn", "marker": "completed"}]}

        first = native.require_post_removal_lead_completion(
            document("lead-a"), "claude", "root-a", "lead-a", trace(just_after), removed,
            require_native_span=False)
        second = native.require_post_removal_lead_completion(
            document("lead-b"), "claude", "root-b", "lead-b", trace(just_before), removed,
            require_native_span=False)
        self.assertTrue(first["native_turn_spanned_removal"])
        self.assertFalse(second["native_turn_spanned_removal"])
        native.require_any_native_lead_span({"a": first, "b": second})
        with self.assertRaisesRegex(RuntimeError, "no original native lead turn"):
            native.require_any_native_lead_span({"a": second, "b": second})
        with self.assertRaisesRegex(RuntimeError, "durable completion"):
            native.require_post_removal_lead_completion(
                document("lead-b", just_before), "claude", "root-b", "lead-b",
                trace(just_before), removed, require_native_span=False)
        with self.assertRaisesRegex(RuntimeError, "did not span"):
            native.require_post_removal_lead_completion(
                document("lead-b"), "claude", "root-b", "lead-b",
                {"child_prompt_shape_supported": True, "child_turns": [{
                    **trace(just_before)["child_turns"][0], "marker": "blocked"}]},
                removed, require_native_span=False)
        with self.assertRaisesRegex(RuntimeError, "Claude-only"):
            native.require_post_removal_lead_completion(
                document("lead-b"), "codex", "root-b", "lead-b", {}, removed,
                require_native_span=False)

    def test_claude_text_list_prompt_starts_new_turn_after_old_one_completed(self):
        before = "2026-09-30T12:00:00+00:00"
        removed = native.timestamp_ns("2026-09-30T12:00:05+00:00")
        after = "2026-09-30T12:00:10+00:00"
        self.assertEqual("prompt", native.claude_child_user_kind([{"type": "text", "text": "next"}]))
        self.assertEqual("tool_result", native.claude_child_user_kind(
            [{"type": "tool_result", "tool_use_id": "tool-a", "content": "done"}]))
        self.assertEqual("unsupported", native.claude_child_user_kind([{"type": "image"}]))
        child = [
            {"type": "user", "timestamp": before, "uuid": "first",
             "message": {"content": "first"}},
            {"type": "assistant", "timestamp": before, "uuid": "first-answer",
             "message": {"stop_reason": "end_turn", "content": [
                 {"type": "text", "text": 'SYMPHONY_OUTCOME: {"status":"completed"}'}]}},
            {"type": "user", "timestamp": after, "uuid": "second",
             "message": {"content": [{"type": "text", "text": "next"}]}},
            {"type": "assistant", "timestamp": after, "uuid": "second-answer",
             "message": {"stop_reason": "end_turn", "content": [
                 {"type": "text", "text": 'SYMPHONY_OUTCOME: {"status":"completed"}'}]}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "projects" / "project" / "root-a"
            directory.mkdir(parents=True)
            with patch.object(sys, "path", [str(PLUGIN), *sys.path]), \
                 patch("symphony.host_evidence._native_jsonl",
                       side_effect=lambda path: child if path.name == "agent-lead-a.jsonl" else []):
                trace = native.claude_host_trace(home, "root-a", "lead-a")
            self.assertEqual(len(trace["child_turns"]), 2)
            self.assertTrue(trace["child_prompt_shape_supported"])
            document = {"event_history": [{"kind": "lead_completed", "observed_at": after,
                                           "payload": {"identity": "lead-a"}}]}
            with self.assertRaisesRegex(RuntimeError, "did not span"):
                native.require_post_removal_lead_completion(
                    document, "claude", "root-a", "lead-a", trace, removed)
            with self.assertRaisesRegex(RuntimeError, "prompt shape"):
                native.require_post_removal_lead_completion(
                    document, "claude", "root-a", "lead-a",
                    {**trace, "child_prompt_shape_supported": False}, removed)

    def test_old_codex_wait_interrupts_only_after_stable_verified_wait(self):
        root, home, state, errors = (Path("/tmp/root"), Path("/tmp/home"),
                                      Path("/tmp/state"), Path("/tmp/errors"))
        process = Mock(args=["codex", "exec"])
        process.wait.side_effect = [subprocess.TimeoutExpired(process.args, 5),
                                    subprocess.TimeoutExpired(process.args, 5),
                                    subprocess.TimeoutExpired(process.args, 5),
                                    subprocess.TimeoutExpired(process.args, 5), -15]
        proof = {"reason": "old_runtime_wait_after_hooked_completed_turn",
                 "latest_turn_hash": "turn-a", "wait_call_hash": "wait-a"}
        changed_wait = {**proof, "wait_call_hash": "wait-b"}
        with patch.object(native, "os", SimpleNamespace(name="posix")), \
             patch.object(native.time, "monotonic",
                          side_effect=(0, 5, 10, 20, 25, 30, 35, 36)), \
             patch.object(native, "read_state_snapshot", return_value={}), \
             patch.object(native, "codex_hook_capture_summary", return_value={}), \
             patch.object(native, "codex_host_trace", return_value={}), \
             patch.object(native, "verified_old_codex_wait",
                          side_effect=(proof, changed_wait, changed_wait, changed_wait)):
            exit_code, observed = native.wait_for_old_codex_root(
                process, root, {"home": home}, state, "root-a", "run-a", "lead-a",
                errors, 100)
        self.assertEqual(-15, exit_code)
        self.assertEqual("old_runtime_wait_after_hooked_completed_turn", observed["reason"])
        self.assertEqual("wait-b", observed["wait_call_hash"])
        process.terminate.assert_called_once()

        unfinished = Mock(args=["codex", "exec"])
        unfinished.wait.side_effect = subprocess.TimeoutExpired(unfinished.args, 5)
        with patch.object(native, "os", SimpleNamespace(name="posix")), \
             patch.object(native.time, "monotonic", side_effect=(0, 10)), \
             patch.object(native, "read_state_snapshot", return_value={}), \
             patch.object(native, "codex_hook_capture_summary", return_value={}), \
             patch.object(native, "codex_host_trace", return_value={}), \
             patch.object(native, "verified_old_codex_wait", side_effect=RuntimeError("unfinished")), \
             self.assertRaises(subprocess.TimeoutExpired):
            native.wait_for_old_codex_root(
                unfinished, root, {"home": home}, state, "root-a", "run-a", "lead-a",
                errors, 8)
        unfinished.terminate.assert_not_called()

        windows = Mock(args=["codex.cmd", "exec"])
        windows.wait.return_value = 0
        with patch.object(native, "os", SimpleNamespace(name="nt")), \
             patch.object(native.time, "monotonic", return_value=0), \
             patch.object(native, "verified_old_codex_wait") as verify:
            self.assertEqual((0, None), native.wait_for_old_codex_root(
                windows, root, {"home": home}, state, "root-a", "run-a", "lead-a",
                errors, 100))
        verify.assert_not_called()
        windows.terminate.assert_not_called()

    def test_strict_codex_trace_rejects_partial_or_missing_native_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            sessions = home / "sessions"
            sessions.mkdir()
            (sessions / "root-a.jsonl").write_text('{"payload": {}}\n')
            with self.assertRaisesRegex(ValueError, "missing"):
                native.codex_host_trace(home, "root-a", "lead-a", home / "errors", strict=True)
            child = sessions / "lead-a.jsonl"
            child.write_text('{"payload": {}}')
            with self.assertRaisesRegex(ValueError, "partial"):
                native.codex_host_trace(home, "root-a", "lead-a", home / "errors", strict=True)
            child.write_text('{"payload": {}}\n{broken\n')
            with self.assertRaisesRegex(ValueError, "invalid"):
                native.codex_host_trace(home, "root-a", "lead-a", home / "errors", strict=True)

    def test_codex_trace_keeps_early_followup_beyond_tool_call_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            sessions = home / "sessions"
            sessions.mkdir()
            rows = [{"type": "response_item", "timestamp": "2026-09-30T00:00:01+00:00",
                     "payload": {"type": "function_call", "name": "followup_task",
                                 "call_id": "early-followup",
                                 "arguments": json.dumps({"target": "original_lead"})}}]
            rows.extend({"type": "response_item",
                         "timestamp": f"2026-09-30T00:00:{second:02d}+00:00",
                         "payload": {"type": "function_call", "name": "wait_agent",
                                     "call_id": f"later-{second}", "arguments": "{}"}}
                        for second in range(2, 16))
            (sessions / "root-a.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
            (sessions / "lead-a.jsonl").write_text("{}\n")
            trace = native.codex_host_trace(home, "root-a", "lead-a",
                                            home / "errors", strict=True)
            self.assertFalse(any(item["name"] == "followup_task"
                                 for item in trace["root_tool_calls_tail"]))
            self.assertEqual(trace["followup_calls"][0]["called_at"],
                             "2026-09-30T00:00:01+00:00")
            self.assertLess(native.timestamp_ns(trace["followup_calls"][0]["called_at"]),
                            native.timestamp_ns("2026-09-30T00:00:20+00:00"))

    def test_codex_trace_classifies_child_status_read_without_exposing_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            sessions = home / "sessions"
            sessions.mkdir()
            (sessions / "root-a.jsonl").write_text("{}\n")
            nonce = "a" * 32
            rows = [
                {"type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "read-1",
                    "arguments": json.dumps({"cmd": "cat NATIVE_STATUS.txt"})}},
                {"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": "read-1",
                    "output": f"WAIT {nonce}\n"}},
                {"type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": "other-2",
                    "arguments": json.dumps({"cmd": "pwd"})}},
            ]
            (sessions / "lead-a.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
            trace = native.codex_host_trace(home, "root-a", "lead-a", home / "errors")
            self.assertEqual([call["status_file_mentioned"] for call in
                              trace["lead_tool_calls"]], [True, False])
            self.assertTrue(trace["lead_tool_calls"][0]["wait_token_seen"])
            self.assertFalse(trace["lead_tool_calls"][1]["return_recorded"])
            self.assertNotIn(nonce, json.dumps(trace))

            rows = [{"type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "call_id": f"extra-{index}",
                "arguments": json.dumps({"cmd": "pwd"})}}
                for index in range(24)]
            rows.append({"type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "call_id": "late-read", "arguments": json.dumps({"cmd": "cat NATIVE_STATUS.txt"})}})
            (sessions / "lead-a.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
            trace = native.codex_host_trace(home, "root-a", "lead-a", home / "errors")
            self.assertEqual(25, trace["lead_tool_call_count"])
            self.assertEqual(24, len(trace["lead_tool_calls"]))
            self.assertTrue(trace["lead_tool_calls_truncated"])
            self.assertFalse(any(call["status_file_mentioned"] for call in
                                 trace["lead_tool_calls"]))

    def test_native_launch_pins_current_provider_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            for provider, profile in (("codex", "base"), ("claude", "sonnet-5-5")):
                root = Path(temporary) / provider
                project = root / "project"
                project.mkdir(parents=True)
                with self.subTest(provider=provider), \
                        patch.object(native, "projects", return_value=(project, project)), \
                        patch.object(native, "launch", side_effect=RuntimeError("fixture launch boundary")) as launch:
                    with self.assertRaisesRegex(RuntimeError, "fixture launch boundary"):
                        native.check_case(provider, root, False, 1, 3)
                    self.assertEqual(profile, launch.call_args.args[4]["SYMPHONY_PROFILE"])

    def test_codex_child_packet_contains_first_turn_outcome_without_root_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for recover, status in ((True, "blocked"), (False, "completed")):
                with self.subTest(recover=recover), \
                        patch.object(native.shutil, "which", return_value="codex"), \
                        patch.object(native.subprocess, "Popen") as popen:
                    native.launch("codex", project, "a", recover, {}, project, 3, "root")
                    command = popen.call_args.args[0]
                    self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
                    self.assertNotIn("-c", command)
                    root_prompt = command[-1]
                    packet_line = re.search(r"^LEAD_SPAWN_PACKET: (.+)$", root_prompt, re.MULTILINE)
                    self.assertIsNotNone(packet_line, "lead instructions must be an explicit relay packet")
                    packet = json.loads(packet_line.group(1))
                    self.assertIn("Pass LEAD_SPAWN_PACKET verbatim as spawn_agent arguments", root_prompt)
                    self.assertIn("Never replace the lead or reinterpret GATE_RELEASED as a command",
                                  root_prompt)
                    self.assertIn("Disposable native callback report", root_prompt)
                    child_message = packet["message"]
                    self.assertIn('SYMPHONY_ROLE: lead\n', child_message)
                    self.assertIn(f'SYMPHONY_OUTCOME: {{"status":"{status}"}}', child_message)
                    self.assertLess(child_message.index(f'SYMPHONY_OUTCOME: {{"status":"{status}"}}'),
                                    child_message.index("native SubagentStart hook"))
                    self.assertNotIn("report success", child_message)
                    if recover:
                        self.assertIn("deliberately blocked", child_message)
                        self.assertIn("even after the native hook releases", child_message)
                    self.assertNotIn('SYMPHONY_OUTCOME: {"status":"completed"}' if recover
                                     else 'SYMPHONY_OUTCOME: {"status":"blocked"}', child_message)
                    self.assertIn("native SubagentStart hook may briefly hold your first turn", child_message)
                    self.assertIn("GATE_RELEASED\nSYMPHONY_OUTCOME", child_message)
                    self.assertIn("There is no gate command or file to find or run", child_message)
                    self.assertIn("GATE_RELEASED is a report line, not an operation", child_message)
                    self.assertIn("Do not inspect files, execute shell commands", child_message)
                    self.assertNotIn("gate.py", child_message)
                    self.assertEqual("none", packet["fork_turns"])
                    self.assertEqual(("gpt-6-luna", "low"),
                                     (packet["model"], packet["reasoning_effort"]))
                    sessions = project / "sessions"
                    sessions.mkdir(exist_ok=True)
                    for message, expected in (
                        ("gAAAAA_fixture_opaque_not_a_real_token", True),
                        (child_message, True),
                        ("", False),
                        ({"text": child_message}, False),
                    ):
                        # Root instructions alone cannot satisfy child relay acceptance.
                        records = [
                            {"type": "response_item", "payload": {"type": "message",
                             "role": "user", "content": root_prompt}},
                            {"type": "response_item", "payload": {"type": "function_call",
                             "name": "spawn_agent", "call_id": "spawn-lead",
                             "arguments": json.dumps({**packet, "message": message})}},
                            {"type": "event_msg", "payload": {"type": "item_completed",
                             "item": {"type": "SubAgentActivity", "id": "spawn-lead",
                                      "kind": "started", "agent_thread_id": "lead"}}},
                        ]
                        (sessions / "root.jsonl").write_text(
                            "\n".join(json.dumps(record) for record in records))
                        trace = native.codex_host_trace(project, "root", "lead", project / "a.errors")
                        self.assertTrue(trace["spawn_calls"][0]["same_lead_id"])
                        self.assertEqual(expected, trace["spawn_calls"][0]["lead_packet_metadata_matches"])
                        spawn = trace["spawn_calls"][0]
                        self.assertEqual(sorted(packet), spawn["argument_keys"])
                        shape = spawn["task_text_fields"]["message"]
                        self.assertEqual("str" if isinstance(message, str) else "dict", shape["type"])
                        self.assertEqual(len(message) if isinstance(message, str) else None, shape["chars"])
                        self.assertNotIn("SYMPHONY_ROLE", json.dumps(shape))
                        self.assertNotIn("hash", shape)

    def test_codex_resume_keeps_recovery_on_original_lead(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            session = "11111111-1111-1111-1111-111111111111"
            lead = "22222222-2222-2222-2222-222222222222"
            task = "original_lead_custom_name"

            def launch(command, **kwargs):
                kwargs["stderr"].write(f"session id: {session}\n")
                return Mock(returncode=0, poll=Mock(return_value=0))

            with patch.object(native.shutil, "which", return_value="codex"), \
                    patch.object(native.subprocess, "Popen", side_effect=launch) as popen:
                native.resume_codex({}, logs, session, logs, time.monotonic() + 30,
                                    lead_id=lead, lead_task_name=task)
            command = popen.call_args.args[0]
            self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
            self.assertNotIn("-c", command)
            self.assertEqual(session, command[-2])
            prompt = command[-1]
            self.assertIn(lead, prompt)
            self.assertIn(f"target {task!r}", prompt)
            self.assertIn("Do not spawn a replacement", prompt)
            self.assertIn("If the injected status says this run is completing", prompt)
            self.assertIn("make no agent calls", prompt)
            self.assertIn("Only if the run is still recovering because gate", prompt)
            self.assertIn("followup_task", prompt)
            self.assertIn("Do not rerun the gate", prompt)
            self.assertIn("native Stop hook", prompt)
            self.assertIn("finish this root turn immediately", prompt)
            self.assertIn("returned text lacks a marker", prompt)
            self.assertTrue(json.loads((logs / "a.resume.status.json").read_text())["same_session"])

            with patch.object(native.shutil, "which", return_value="codex"), \
                    patch.object(native.subprocess, "Popen", side_effect=launch) as popen:
                native.resume_codex({"SYMPHONY_PROFILE": "full"}, logs, session, logs,
                                    time.monotonic() + 30, lead_id=lead,
                                    lead_task_name=task, native_status_nonce="nonce123")
            command = popen.call_args.args[0]
            self.assertEqual("gpt-6-astra", command[command.index("--model") + 1])
            self.assertIn("target 'original_lead_custom_name'", command[-1])
            self.assertIn("read NATIVE_STATUS.txt", command[-1])
            self.assertIn("READY nonce123", command[-1])
            self.assertIn("Never edit the file", command[-1])

            # Status-only and exact Stop probes must not become recovery requests.
            with patch.object(native.shutil, "which", return_value="codex"), \
                    patch.object(native.subprocess, "Popen", side_effect=launch) as popen:
                for options in ({"already_completed": True}, {"direct_stop": True}):
                    native.resume_codex({}, logs, session, logs, time.monotonic() + 30,
                                        lead_id=lead, lead_task_name=task, **options)
                    command = popen.call_args.args[0]
                    self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
                    self.assertNotIn("-c", command)
                    prompt = command[-1]
                    if options.get("already_completed"):
                        self.assertIn("Do not start or stop work, delegate", prompt)
                    else:
                        self.assertEqual("$symphony:symphony stop", prompt)

    def test_native_capture_recognizes_disposable_baseline_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "claude-baseline-home"
            home.mkdir()
            capture = root / "claude-hook-capture"
            capture.mkdir()
            invocation = "capture-one"
            (capture / f"{invocation}-entry.json").write_text(json.dumps({
                "invocation_id": invocation, "event": "Stop", "session_id": "root-a",
                "agent_id": None, "native_home": str(home), "started_ns": 123,
                "prompt_id_hash": "prompt-hash", "payload_keys": ["session_id"],
            }))
            (capture / f"{invocation}-exit.json").write_text(json.dumps({
                "invocation_id": invocation, "exit_code": 0, "finished_ns": 125,
            }))
            summary = native.codex_hook_capture_summary(root, "claude")
            self.assertTrue(summary["configured"])
            self.assertEqual(1, summary["event_counts"]["Stop"])
            self.assertTrue(summary["records"][0]["native_home_matches_expected"])
            self.assertEqual("prompt-hash", summary["records"][0]["prompt_id_hash"])

    def test_claude_trace_joins_root_agent_call_and_new_child_turn_without_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            session = "11111111-1111-1111-1111-111111111111"
            lead = "a1234567890123456"
            root = home / "projects" / "project" / session
            child = root / "subagents" / f"agent-{lead}.jsonl"
            child.parent.mkdir(parents=True)
            agent_type = "symphony:symphony-lead-claude-sonnet-5-5-low"
            (child.with_suffix(".meta.json")).write_text(json.dumps({
                "agentType": agent_type, "toolUseId": "first-tool"}))
            parent_rows = [
                {"type": "user", "uuid": "root-prompt-one", "timestamp": "2026-09-30T00:00:00Z",
                 "message": {"content": "private root instructions"}},
                {"type": "assistant", "uuid": "assistant-one", "timestamp": "2026-09-30T00:00:01Z",
                 "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "first-tool",
                                          "input": {"subagent_type": agent_type,
                                                    "prompt": "private lead task"}}]}},
                {"type": "user", "uuid": "tool-result", "timestamp": "2026-09-30T00:00:02Z",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "first-tool",
                                          "content": "private result"}]}},
                {"type": "user", "uuid": "root-prompt-two", "timestamp": "2026-09-30T00:00:03Z",
                 "message": {"content": "private second instructions"}},
                {"type": "assistant", "uuid": "assistant-two", "timestamp": "2026-09-30T00:00:04Z",
                 "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "second-tool",
                                          "input": {"subagent_type": agent_type,
                                                    "resume": lead,
                                                    "prompt": "private follow-up"}}]}},
                {"type": "assistant", "uuid": "assistant-three", "timestamp": "2026-09-30T00:00:05Z",
                 "message": {"content": [{"type": "tool_use", "name": "SendMessage",
                                          "id": "message-tool", "input": {"to": lead,
                                          "message": "private recovery instructions"}}]}},
            ]
            child_rows = [
                {"type": "user", "uuid": "child-prompt-one", "timestamp": "2026-09-30T00:00:01Z",
                 "message": {"content": "private first child task"}},
                {"type": "assistant", "uuid": "child-terminal-one",
                 "timestamp": "2026-09-30T00:00:02Z",
                 "message": {"stop_reason": "end_turn", "content": [
                     {"type": "text", "text": 'SYMPHONY_OUTCOME: {"status":"completed"}'}]}},
                {"type": "user", "uuid": "child-prompt-two", "timestamp": "2026-09-30T00:00:04Z",
                 "message": {"content": "private new child task"}},
                {"type": "assistant", "uuid": "child-terminal-two",
                 "timestamp": "2026-09-30T00:00:05Z",
                 "message": {"stop_reason": "end_turn", "content": [
                     {"type": "text", "text": "private markerless result"}]}},
            ]
            root.with_suffix(".jsonl").write_text("\n".join(map(json.dumps, parent_rows)) + "\n")
            child.write_text("\n".join(map(json.dumps, child_rows)) + "\n")
            trace = native.claude_host_trace(home, session, lead)
            self.assertEqual(2, len(trace["root_agent_calls"]))
            self.assertEqual(2, len(trace["child_turns"]))
            self.assertTrue(trace["root_agent_calls"][0]["matches_lead_meta"])
            self.assertFalse(trace["root_agent_calls"][1]["matches_lead_meta"])
            self.assertNotEqual(trace["root_agent_calls"][0]["root_prompt_id_hash"],
                                trace["root_agent_calls"][1]["root_prompt_id_hash"])
            self.assertIsNotNone(trace["root_agent_calls"][1]["resume_id_hash"])
            self.assertEqual(1, len(trace["root_message_calls"]))
            self.assertTrue(trace["root_message_calls"][0]["to_matches_original_lead"])
            self.assertEqual("completed", trace["child_turns"][0]["marker"])
            self.assertIsNone(trace["child_turns"][1]["marker"])
            self.assertNotIn("private", json.dumps(trace))
            for malformed, shape in (("[]", "list"), ("{}", "dict")):
                with self.subTest(malformed=malformed):
                    child_rows[-1]["message"]["content"][0]["text"] = (
                        'SYMPHONY_OUTCOME: {"status":' + malformed + "}")
                    child.write_text("\n".join(map(json.dumps, child_rows)) + "\n")
                    invalid = native.claude_host_trace(home, session, lead)
                    self.assertEqual("invalid", invalid["child_turns"][1]["marker"])
                    self.assertEqual(shape, invalid["child_turns"][1]["marker_status_shape"])

    def test_source_and_exact_retained_profiles_and_rejections(self):
        files, digest, constants = native.reviewed_snapshot_spec(PLUGIN)
        version = constants["PLUGIN_VERSION"]
        schema = constants["HOOK_SCHEMA_VERSION"]
        with tempfile.TemporaryDirectory() as temporary:
            runtime_base = Path(temporary) / "retained runtimes"
            snapshot = runtime_base / digest
            for relative in files:
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PLUGIN / relative, destination)
            source = {"session_id": "root-a", "plugin_version": version,
                      "plugin_root": str(PLUGIN), "runtime_root": str(snapshot),
                      "hook_schema_version": schema, "observed_at": "now"}
            retained = {**source, "plugin_root": str(snapshot), "runtime_root": None}
            direct_source = {**source, "runtime_root": None}
            accepts = lambda record: native.candidate_retained_profile(
                (record,), "root-a", version, PLUGIN, runtime_base)

            self.assertTrue(accepts(source))
            self.assertTrue(accepts(direct_source))
            self.assertTrue(accepts(retained))
            for changed in ({**retained, "session_id": "foreign"},
                            {**retained, "plugin_version": "1.5.1"},
                            {**retained, "hook_schema_version": schema + 1},
                            {**retained, "observed_at": ""},
                            {**retained, "plugin_root": str(runtime_base / ("0" * 64))},
                            {**source, "runtime_root": str(runtime_base / ("0" * 64))}):
                self.assertFalse(accepts(changed), changed)

            changed_file = snapshot / files[0]
            original = changed_file.read_bytes()
            changed_file.write_bytes(original + b"changed")
            self.assertFalse(accepts(retained))
            changed_file.write_bytes(original)
            extra = snapshot / "extra.txt"
            extra.write_text("extra")
            self.assertFalse(accepts(retained))
            extra.unlink()
            self.assertTrue(accepts(retained))

    def test_native_stop_commit_can_follow_parent_exit_without_false_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            active = {"recent_runs": [], "active_runs": {"codex:root-a": {"run_id": "run-a"}}}
            completed = {"recent_runs": [{"provider": "codex", "session_id": "root-a",
                                          "run_id": "run-a", "status": "completed",
                                          "outcome": {"status": "completed"}}], "active_runs": {}}
            path.write_text(json.dumps(active))

            def commit():
                replacement = path.with_suffix(".replacement")
                replacement.write_text(json.dumps(completed))
                with _locked(path):
                    replacement.replace(path)

            delayed = threading.Timer(.05, commit)
            delayed.start()
            try:
                observed = native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + 1)
            finally:
                delayed.join()
            self.assertEqual(observed["a"]["recent_runs"][0]["status"], "completed")

            inbox = path.parent / ".session-child.json"
            inbox.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                         "overflow": False}))
            def acknowledge():
                replacement = inbox.with_suffix(".replacement")
                replacement.write_text(json.dumps({"pending": [], "overflow": False}))
                with _locked(inbox.with_suffix("")):
                    replacement.replace(inbox)

            delayed_ack = threading.Timer(.05, acknowledge)
            delayed_ack.start()
            started = time.monotonic()
            try:
                native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + 1)
            finally:
                delayed_ack.join()
            self.assertGreaterEqual(time.monotonic() - started, .05)

            inbox.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                         "overflow": False}))
            with self.assertRaisesRegex(RuntimeError, "unacknowledged"):
                native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + .15)
            inbox.unlink()

            path.write_text(json.dumps(active))
            unresolved = native.wait_for_completed_docs(
                {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                time.monotonic() + .15)
            self.assertEqual(unresolved["a"]["recent_runs"], [])


if __name__ == "__main__":
    unittest.main()
