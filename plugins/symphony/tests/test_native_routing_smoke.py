"""The native probe requires successful fixture edits, not transcript mentions."""

import importlib.util
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[3] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("native_routing_smoke", SCRIPTS / "native_routing_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class NativeRoutingEvidenceTests(unittest.TestCase):
    def test_integration_diagnostics_keep_native_text_private(self):
        run = {'lead_identity': 'lead', 'delegations': [{'identity': 'lead', 'role': 'lead'}]}
        rows = [{'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'PRIVATE_SENTINEL'}]}}]
        with patch.object(smoke, 'native_rows', return_value=rows):
            facts = smoke.lead_integration_probe('claude', run, Path('/private-home'), Path('/private-project'))
        self.assertFalse(facts['accepted'])
        self.assertIn('source_line', facts)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(facts))

    def test_native_fixtures_supply_the_same_instructions_to_both_hosts(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider), TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / 'primary'
                project.mkdir()
                with patch.object(smoke, 'prepare_baseline_capture', return_value={}), \
                     patch.object(smoke, 'install_private_child_capture'), \
                     patch.object(smoke, 'projects', return_value=(project, project)), \
                     patch.object(smoke, 'run_git'), \
                     patch.object(smoke.shutil, 'which', side_effect=RuntimeError('fixture prepared')):
                    with self.assertRaisesRegex(RuntimeError, 'fixture prepared'):
                        smoke.check_case(provider, root, root, 'command', 60, 1)
                self.assertEqual((project / 'CLAUDE.md').read_text(), smoke.FIXTURE_COMMAND_INSTRUCTIONS)
                self.assertEqual((project / 'AGENTS.md').read_text(), smoke.FIXTURE_COMMAND_INSTRUCTIONS)

    def test_command_diagnostics_separate_native_results_from_supported_argv(self):
        rows = [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Bash',
                 'input': {'command': 'cd PRIVATE_SENTINEL && python -m unittest -q'}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call',
                 'is_error': False, 'content': 'Ran 1 test in 0.1s\nOK'}]}}]
        result = smoke.command_witness_probe('claude', rows)
        self.assertEqual(result['exact_supported_unittest_calls'], 0)
        self.assertEqual(result['matched_results'], 0)
        self.assertEqual(result['all_direct_matched_results'], 1)
        self.assertEqual(result['all_direct_ran_tests_present'], 1)
        self.assertEqual(result['all_direct_ok_present'], 1)
        self.assertEqual(result['unittest_token_sequence_present'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        self.assertFalse(smoke.unittest_verified(smoke.tool_evidence('claude', rows)))
        missing = smoke.command_witness_probe('claude', rows[:1])
        self.assertEqual(missing['all_direct_missing_results'], 1)

    def test_codex_command_verifies_distinct_completed_turns_only_with_exact_root_followup(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            identity = '00000000-0000-0000-0000-000000000002'
            root = '00000000-0000-0000-0000-000000000001'
            task = 'symphony_lead_fast_model_medium'
            run = {'run_id': 'run', 'provider': 'codex', 'session_id': root, 'lead_identity': identity,
                   'started_at': '2026-10-01T00:00:00+00:00', 'task': 'fixture', 'status': 'completed',
                   'outcome': {'status': 'completed'}, 'assessment': {'_fast_route': {'model': 'model', 'effort': 'medium'}},
                   'delegations': [{'identity': identity, 'role': 'lead', 'objective': 'fixture', 'state': 'completed',
                                    'requested_tier': 'model', 'requested_effort': 'medium'}]}
            def row(kind, payload, seconds):
                return {'type': kind, 'timestamp': f'2026-10-01T00:00:0{seconds}+00:00', 'payload': payload}
            message = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
            rows = [row('session_meta', {'id': identity, 'agent_path': '/root/' + task,
                        'source': {'subagent': {'thread_spawn': {'parent_thread_id': root}}}}, '0')]
            for index, seconds in ((1, 1), (2, 3)):
                token = 'turn-' + str(index)
                rows.extend([row('event_msg', {'type': 'task_started', 'turn_id': token}, str(seconds)),
                    row('turn_context', {'turn_id': token, 'model': 'model', 'effort': 'medium'}, str(seconds) + '.1'),
                    row('response_item', {'type': 'message', 'role': 'assistant', 'id': 'message-' + str(index),
                        'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': message}]}, str(seconds) + '.2'),
                    row('event_msg', {'type': 'task_complete', 'turn_id': token, 'last_agent_message': message}, str(seconds) + '.3')])
            directory = home / 'sessions/2026/10/01'
            directory.mkdir(parents=True)
            root_rows = [row('session_meta', {'id': root, 'cwd': str(home)}, '0'),
                row('response_item', {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'spawn',
                    'arguments': json.dumps({'task_name': task, 'model': 'model', 'reasoning_effort': 'medium'})}, '0.1'),
                row('event_msg', {'type': 'item_completed', 'thread_id': root, 'item': {'type': 'SubAgentActivity',
                    'id': 'spawn', 'kind': 'started', 'agent_thread_id': identity, 'agent_path': '/root/' + task}}, '0.2'),
                row('response_item', {'type': 'function_call', 'name': 'followup_task', 'call_id': 'followup',
                    'arguments': json.dumps({'target': task})}, '2'),
                row('event_msg', {'type': 'item_completed', 'thread_id': root, 'item': {'type': 'SubAgentActivity',
                    'id': 'followup', 'kind': 'interacted', 'agent_thread_id': identity, 'agent_path': '/root/' + task}}, '2.1'),
                row('response_item', {'type': 'function_call_output', 'call_id': 'followup', 'output': ''}, '2.2')]
            root_path = directory / (root + '.jsonl')
            def write_root(values):
                root_path.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')
            write_root(root_rows)
            receipts = [{'provider': 'codex', 'session': root, 'run_id': 'run', 'agent': identity,
                         'turn': 'turn_id:turn-' + str(index), 'lead': identity, 'parent': root,
                         'status': 'completed', 'result': str(index) * 64} for index in (1, 2)]
            def check(values, saved=receipts):
                (directory / (identity + '.jsonl')).write_text(
                    ''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')
                return smoke.codex_command_turns_verified({'terminal_receipts': saved}, run, values, identity, home, home)
            self.assertEqual(check(rows), (identity, 'eligible', rows[-2]['timestamp']))
            self.assertIsNotNone(check(rows[:5], receipts[:1]))
            for changed in (rows[:2] + [rows[1]] + rows[2:], rows[:3] + [rows[2]] + rows[3:],
                            rows[:4] + [rows[3]] + rows[4:], rows + [rows[-1]],
                            rows + [row('event_msg', {'type': 'task_started', 'turn_id': 'unfinished'}, '4')],
                            rows + [row('event_msg', {'type': 'task_failed', 'turn_id': 'turn-2'}, '4')],
                            rows[:-1], rows[:3] + [row('response_item', {**rows[3]['payload'], 'id': 'conflict',
                                'content': [{'type': 'output_text', 'text': 'SYMPHONY_FAST_DECISION: escalate'}]}, '1.2')] + rows[3:]):
                with self.subTest(changed=changed):
                    self.assertIsNone(check(changed))
            for field, value in (('parent', 'foreign'), ('lead', 'foreign'), ('run_id', 'foreign'),
                                 ('status', 'failed'), ('result', 'malformed')):
                with self.subTest(field=field):
                    self.assertIsNone(check(rows, [receipts[0], {**receipts[1], field: value}]))
            self.assertIsNone(check(rows, [*receipts, {**receipts[1], 'result': 'f' * 64}]))
            write_root(root_rows[:-1])
            self.assertIsNone(check(rows))
            write_root([root_rows[0], *root_rows[3:]])
            self.assertIsNone(check(rows))

    def test_private_claude_capture_keeps_raw_callback_outside_public_clock(self):
        from native_managed_concurrency import CODEX_HOOK_CAPTURE
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'capture_claude_hook.py'
            script.write_text(CODEX_HOOK_CAPTURE, encoding='utf-8')
            smoke.install_private_child_capture(root)
            payload = {'hook_event_name': 'SubagentStart', 'session_id': 'root', 'agent_id': 'worker',
                       'role': 'worker', 'private_field': 'PRIVATE_SENTINEL', 'cwd': str(root)}
            public = root / 'claude-hook-capture'
            completed = subprocess.run([sys.executable, str(script), 'SubagentStart', str(public), 'claude'],
                input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8',
                env={key: value for key, value in os.environ.items() if not key.startswith('SYMPHONY_')})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            private = tuple((root / 'private-child-hooks').glob('*.json'))
            self.assertEqual(len(private), 1)
            captured = json.loads(private[0].read_text(encoding='utf-8'))
            self.assertEqual(captured['raw'], payload)
            self.assertEqual(captured['canonical']['payload'], {**payload, 'provider': 'claude'})
            self.assertEqual(captured['canonical']['event_id'], smoke.event_from_payload('claude', payload).event_id)
            self.assertNotIn('PRIVATE_SENTINEL', ''.join(path.read_text(encoding='utf-8') for path in public.glob('*.json')))
            if os.name != 'nt':
                self.assertEqual(private[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(private[0].parent.stat().st_mode & 0o777, 0o700)
            transcript = root / 'child.jsonl'
            handback = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'SubagentHandback',
                         'input': {'message': 'PRIVATE_HOOK_TIME_REPORT'}}]}}
            terminal = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Goodbye'}]}}
            transcript.write_text(json.dumps(handback) + '\n' + json.dumps(terminal) + '\n', encoding='utf-8')
            stop = {**payload, 'hook_event_name': 'SubagentStop', 'agent_transcript_path': str(transcript),
                    'last_assistant_message': 'Goodbye'}
            completed = subprocess.run([sys.executable, str(script), 'SubagentStop', str(public), 'claude'],
                input=json.dumps(stop), capture_output=True, text=True, encoding='utf-8',
                env={key: value for key, value in os.environ.items() if not key.startswith('SYMPHONY_')})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            snapshots = [json.loads(path.read_text(encoding='utf-8')) for path in (root / 'private-child-hooks').glob('*.json')]
            captured_stop = next(item for item in snapshots if item['raw']['hook_event_name'] == 'SubagentStop')
            self.assertEqual(captured_stop['canonical']['payload']['last_assistant_message'], 'PRIVATE_HOOK_TIME_REPORT\nGoodbye')
            transcript.write_text(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'SubagentHandback',
                                  'input': {'message': 'LATER_REPORT'}}]}}) + '\n' + json.dumps(terminal) + '\n', encoding='utf-8')
            self.assertNotEqual(captured_stop['canonical']['event_id'], smoke.event_from_payload('claude', stop).event_id)
            self.assertNotIn('PRIVATE_HOOK_TIME_REPORT', ''.join(path.read_text(encoding='utf-8') for path in public.glob('*.json')))

    def test_claude_binding_diagnostic_requires_exact_sources_and_original_timestamps(self):
        from plugins.symphony.tests.test_assessed_contract import AssessedContractTests
        fixture = AssessedContractTests()
        fixture.begin('claude')
        self.addCleanup(fixture.doCleanups)
        fixture.worker()
        run = asdict(fixture.state.active_run)
        home = Path(fixture.environ['CLAUDE_CONFIG_DIR'])
        proof = run['assessment']['_substantive_children']['worker']
        child = next(item for item in run['delegations'] if item['identity'] == 'worker')
        fields = {'session_id': run['session_id'], 'agent_id': 'worker', 'cwd': str(fixture.project),
                  'role': 'worker', 'task': 'SYMPHONY_ROLE: worker', 'parent_thread_id': 'lead',
                  'model': child['requested_tier'], 'model_reasoning_effort': child['requested_effort'],
                  'prompt_id': 'worker-1'}
        start = smoke.event_from_payload('claude', {**fields, 'hook_event_name': 'SubagentStart'})
        terminal = smoke.event_from_payload('claude', {**fields, 'hook_event_name': 'SubagentStop',
                                                     'prompt_id': 'later-root-request',
                                                     'status': 'completed', 'last_assistant_message': 'PRIVATE_SENTINEL'})
        proof['start_event_id'] = start.event_id
        history = [{'event_id': source.event_id + ':delegation:delegation_updated',
                    'kind': 'delegation_updated', 'observed_at': timestamp} for source, timestamp in (
                        (start, proof['admitted_at']), (terminal, child['updated_at']))]
        receipt = {'provider': 'claude', 'session': run['session_id'], 'run_id': run['run_id'],
                   'agent': 'worker', 'turn': smoke._child_turn_token(terminal.payload),
                   'result': smoke._terminal_result_id(terminal)}
        document = {'event_history': history, 'terminal_receipts': [receipt]}
        private = home.parent / 'private-child-hooks'
        private.mkdir()
        for index, source in enumerate((start, terminal)):
            (private / f'{index}.json').write_text(json.dumps(source.payload), encoding='utf-8')
        result = smoke.claude_child_binding_probe(home, run, document)[0]
        self.assertEqual(result['source_stage'], 'exact_start_and_terminal')
        self.assertEqual(result['stage'], 'accepted')
        self.assertTrue(result['binding_reader_accepted'])
        self.assertFalse(result['hook_tokens_equal'])
        self.assertTrue(result['scoped_token_compatible'])
        self.assertEqual(result['start_token_kind'], 'prompt_id')
        self.assertEqual(result['terminal_token_kind'], 'prompt_id')
        self.assertEqual(result['terminal_source_filters']['proof_turn_matches'], 0)
        self.assertEqual(result['terminal_source_filters']['receipt_turn_matches'], 1)
        self.assertLessEqual(result['launch_minus_admitted_ms'], 0)
        self.assertLessEqual(result['prompt_minus_callback_ms'], 0)
        self.assertEqual(result['textual_prompt_count'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        self.assertNotIn(str(fixture.project), json.dumps(result))
        self.assertEqual(result['terminal_source_filters']['captured_stops_for_child'], 1)
        self.assertEqual(result['terminal_source_filters']['scoped_result_hash_matches'], 1)
        # Snapshot normalization at hook time, not a later mutable handback.
        for index, source in enumerate((start, terminal)):
            (private / f'{index}.json').write_text(json.dumps({'raw': source.payload,
                'canonical': {'event_id': source.event_id, 'kind': source.kind, 'payload': source.payload}}), encoding='utf-8')
        with patch.object(smoke, 'event_from_payload', side_effect=AssertionError('must not reread handback')):
            snapshotted = smoke.claude_child_binding_probe(home, run, document)[0]
        self.assertTrue(snapshotted['binding_reader_accepted'])
        alias = smoke.event_from_payload('claude', {**terminal.payload, 'session_id': 'worker'})
        alias_history = [history[0], {**history[1], 'event_id': alias.event_id + ':delegation:delegation_updated'}]
        scoped_alias = replace(alias, payload={**alias.payload, 'session_id': run['session_id']})
        alias_receipt = {**receipt, 'result': smoke._terminal_result_id(scoped_alias)}
        (private / '1.json').write_text(json.dumps({'raw': alias.payload,
            'canonical': {'event_id': alias.event_id, 'kind': alias.kind, 'payload': alias.payload}}), encoding='utf-8')
        alias_document = {'event_history': alias_history, 'terminal_receipts': [alias_receipt]}
        alias_result = smoke.claude_child_binding_probe(home, run, alias_document)[0]
        self.assertTrue(alias_result['binding_reader_accepted'])
        filters = alias_result['terminal_source_filters']
        self.assertEqual(filters['child_alias_session'], 1)
        self.assertEqual(filters['raw_result_hash_matches'], 0)
        self.assertEqual(filters['scoped_result_hash_matches'], 1)
        duplicate_path = private / 'retry.json'
        duplicate_path.write_text((private / '1.json').read_text(encoding='utf-8'), encoding='utf-8')
        retried = smoke.claude_child_binding_probe(home, run, alias_document)[0]
        self.assertTrue(retried['binding_reader_accepted'])
        self.assertEqual(retried['terminal_source_filters']['captured_stops_for_child'], 2)
        self.assertEqual(retried['terminal_source_filters']['duplicate_exact_sources'], 1)
        duplicate_path.unlink()
        for field, value in (('run_id', 'foreign'), ('session', 'foreign'), ('agent', 'foreign'), ('turn', 'foreign'), ('result', 'foreign')):
            unavailable = smoke.claude_child_binding_probe(home, run, {
                **alias_document, 'terminal_receipts': [{**alias_receipt, field: value}]})[0]
            self.assertEqual(unavailable['source_stage'], 'terminal_source_unavailable')
            self.assertNotIn('binding_reader_accepted', unavailable)
        duplicate_receipts = smoke.claude_child_binding_probe(home, run, {
            **alias_document, 'terminal_receipts': [alias_receipt, alias_receipt]})[0]
        self.assertEqual(duplicate_receipts['source_stage'], 'terminal_source_unavailable')
        self.assertEqual(duplicate_receipts['terminal_source_filters']['receipt_turn_matches'], 0)
        corrupt = {'raw': alias.payload, 'canonical': {'event_id': 'foreign', 'kind': alias.kind, 'payload': alias.payload}}
        (private / '1.json').write_text(json.dumps(corrupt), encoding='utf-8')
        unavailable = smoke.claude_child_binding_probe(home, run, alias_document)[0]
        self.assertEqual(unavailable['terminal_source_filters']['canonical_snapshot_unavailable'], 1)
        # Existing raw-only fixtures still require exact durable source IDs.
        for index, source in enumerate((start, terminal)):
            (private / f'{index}.json').write_text(json.dumps(source.payload), encoding='utf-8')
        for broken in ({'event_history': history[1:], 'terminal_receipts': [receipt]},
                       {'event_history': history[:1], 'terminal_receipts': [receipt]},
                       {'event_history': history, 'terminal_receipts': [{**receipt, 'result': 'foreign'}]},
                       {'event_history': [{**history[0], 'event_id': 'foreign-source'}, history[1]],
                        'terminal_receipts': [receipt]}):
            with self.subTest(broken=broken):
                unavailable = smoke.claude_child_binding_probe(home, run, broken)[0]
                self.assertIn('unavailable', unavailable['source_stage'])
                self.assertNotIn('binding_reader_accepted', unavailable)
        child_path = next((home / 'projects').glob('*/root/subagents/agent-worker.jsonl'))
        child_source = child_path.read_text(encoding='utf-8')
        malformed = {'type': 'user', 'message': {'content': [{'type': 'image', 'source': 'PRIVATE_SENTINEL'}]}}
        child_path.write_text(child_source + json.dumps(malformed) + '\n', encoding='utf-8')
        rejected = smoke.claude_child_binding_probe(home, run, document)[0]
        self.assertEqual(rejected['stage'], 'child_prompts')
        self.assertFalse(rejected['binding_reader_accepted'])
        self.assertEqual(rejected['unsupported_native_user_rows'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(rejected))
        child_path.write_text(child_source, encoding='utf-8')
        parent_path = next((home / 'projects').glob('*/root/subagents/agent-lead.jsonl'))
        rows = [json.loads(line) for line in parent_path.read_text(encoding='utf-8').splitlines()]
        rows[0]['timestamp'] = '2026-10-01T14:00:59+00:00'
        parent_path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        rejected = smoke.claude_child_binding_probe(home, run, document)[0]
        self.assertFalse(rejected['binding_reader_accepted'])
        self.assertEqual(rejected['stage'], 'native_launch')
        self.assertGreater(rejected['launch_minus_admitted_ms'], 0)

    def test_command_witness_diagnostics_preserve_failed_missing_and_unsafe_command_categories(self):
        def call(identity, command):
            return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': identity,
                    'name': 'Bash', 'input': {'command': command}}]}}
        rows = [call('valid', 'python -m unittest -q'), call('missing', 'python.exe -m unittest -q'),
                call('unsafe', 'python -m unittest -q || echo OK'),
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'valid',
                 'is_error': True, 'content': 'Ran 3 tests in 0.01s FAILED PRIVATE_SENTINEL'}]}}]
        result = smoke.command_witness_probe('claude', rows)
        self.assertEqual(result['exact_supported_unittest_calls'], 2)
        self.assertEqual(result['direct_shell_calls'], 3)
        self.assertEqual(result['missing_results'], 1)
        self.assertEqual(result['error_results'], 1)
        self.assertEqual(result['failed_present'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))

    def test_fast_terminal_diagnostics_separate_turns_and_keep_duplicate_records(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            identity = '00000000-0000-0000-0000-000000000002'
            root = '00000000-0000-0000-0000-000000000001'
            run = {'run_id': 'run', 'provider': 'codex', 'session_id': root, 'lead_identity': identity,
                   'started_at': '2026-10-01T00:00:00+00:00', 'task': 'fixture',
                   'assessment': {'_fast_route': {'model': 'model', 'effort': 'medium'}},
                   'delegations': [{'identity': identity, 'role': 'lead', 'objective': 'fixture', 'state': 'completed', 'requested_tier': 'model',
                                    'requested_effort': 'medium'}]}
            header = {'type': 'session_meta', 'payload': {'id': identity,
                      'source': {'subagent': {'thread_spawn': {'parent_thread_id': root}}}}}
            rows = [header]
            message = 'SYMPHONY_FAST_DECISION: eligible\nPRIVATE_SENTINEL'
            for index in (1, 2):
                token = 'turn-' + str(index)
                timestamp = f'2026-10-01T00:00:0{index}+00:00'
                rows.extend([
                    {'type': 'turn_context', 'payload': {'turn_id': token, 'model': 'model', 'effort': 'medium'}},
                    {'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': 'task_started', 'turn_id': token}},
                    {'type': 'response_item', 'timestamp': timestamp, 'payload': {'type': 'message', 'role': 'assistant',
                     'id': 'message-' + str(index), 'phase': 'final_answer',
                     'content': [{'type': 'output_text', 'text': message}]}},
                    {'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': 'task_complete',
                     'turn_id': token, 'last_agent_message': message}}])
            path = home / 'sessions' / '2026' / '10' / '01' / (identity + '.jsonl')
            path.parent.mkdir(parents=True)

            def probe(rows, receipts=()):
                path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                return smoke.codex_fast_terminal_probe({'terminal_receipts': receipts}, run, rows, identity, home, home)

            event = smoke.Event('id', 'subagent_stopped', 'now', {'provider': 'codex', 'session_id': root,
                'agent_id': identity, 'turn_id': 'turn-2', 'status': 'completed', 'model': 'model',
                'model_reasoning_effort': 'medium', 'last_assistant_message': message})
            receipt = {'provider': 'codex', 'session': root, 'run_id': 'run', 'agent': identity,
                       'turn': 'turn_id:turn-2', 'result': smoke._terminal_result_id(event)}
            result = probe(rows, [receipt])
            self.assertEqual(smoke.decision_representations('codex', rows, identity)['distinct_marker_turns'], 2)
            self.assertEqual(result['started_turns'], 2)
            self.assertEqual(result['completed_turns'], 2)
            self.assertEqual([turn['marker_count'] for turn in result['turns']], [1, 1])
            self.assertTrue(result['turns'][-1]['canonical_event_result_matches_receipt'])
            self.assertFalse(result['turns'][0]['receipt_turn_matches'])
            self.assertEqual(result['extra_turns_with_exact_root_followup'], 0)
            duplicate = rows[:4] + [rows[3]] + rows[4:]
            repeated = probe(duplicate)
            self.assertEqual(repeated['turns'][0]['matching_marker_records'], 2)
            future = rows + [{'type': 'event_msg', 'timestamp': '2026-10-01T00:00:03+00:00',
                            'payload': {'type': 'task_started', 'turn_id': 'unfinished'}}]
            self.assertTrue(probe(future)['newer_unfinished_turn'])
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
            self.assertNotIn(identity, json.dumps(result))

    def test_assessed_failure_diagnostics_separate_child_proof_and_supplied_outcome(self):
        run = {'run_id': 'run', 'lead_identity': 'lead', 'owner_generation': 1,
               'assessment': {'substantive_contract': {'version': 1, 'epoch': 'epoch'},
                   '_retryable_lead': 'PRIVATE_SENTINEL', '_substantive_children': {
                       'worker': {'role': 'worker', 'parent': 'lead', 'successful': True,
                                  'lead': 'lead', 'run_id': 'run', 'owner_generation': 1, 'epoch': 'epoch'}}},
               'delegations': [{'identity': 'lead', 'requested_tier': 'model'}]}
        rows = [{'type': 'assistant', 'message': {'role': 'assistant', 'model': 'model', 'content': [
            {'type': 'text', 'text': 'SYMPHONY_OUTCOME: {"status":"blocked","reason":"PRIVATE_SENTINEL"}'}]}}]
        with patch.object(smoke, 'native_rows', return_value=rows):
            result = smoke.assessed_completion_probe('claude', Path('/missing'), run)
        self.assertTrue(result['retryable_lead_present'])
        self.assertTrue(result['contract_version_one'])
        self.assertEqual(result['child_proof_counts']['successful'], 1)
        self.assertEqual(result['child_proof_counts']['parent_bound'], 1)
        self.assertEqual(result['supplied_outcome_categories']['blocked'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))

    def test_assessed_native_prompt_count_excludes_claude_tool_result_rows(self):
        rows = [{'type': 'user', 'message': {'content': content}} for content in (
            'native prompt', [{'type': 'text', 'text': 'native prompt'}],
            [{'type': 'tool_result', 'tool_use_id': 'private', 'content': 'PRIVATE_SENTINEL'}],
            [], [{'type': 'text', 'text': 'mixed'}, {'type': 'tool_result', 'content': 'result'}])]
        with patch.object(smoke, 'native_rows', return_value=rows):
            result = smoke.assessed_completion_probe('claude', Path('/missing'), {})
        self.assertEqual(result['native_prompt_or_started_turn_count'], 2)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))

    def test_claude_fast_identity_requires_the_exact_accepted_root_launch(self):
        from plugins.symphony.tests.test_claude_host_evidence import ClaudeHostEvidenceTests, LEAD
        fixture = ClaudeHostEvidenceTests()
        fixture.setUp()
        try:
            home = Path(fixture.environ['CLAUDE_CONFIG_DIR'])
            run = asdict(replace(fixture.run, assessment={
                '_fast_route': {'model': 'claude-sonnet-5', 'effort': 'low'},
                '_claude_fast_launch_hash': hashlib.sha256(b'toolu_launch').hexdigest()}))
            rows = smoke.native_rows('claude', home, LEAD)
            # Identity comes from the accepted launch, even without a marker.
            self.assertTrue(smoke.fast_native_identity('claude', rows, LEAD, run, home, fixture.project))
            marker_rows = [{**row, 'message': {'content': 'SYMPHONY_FAST_DECISION: eligible'}} for row in rows]
            for changes in ({}, {'_claude_fast_launch_hash': 'not-a-hash'},
                            {'_claude_fast_launch_hash': hashlib.sha256(b'foreign').hexdigest()}):
                assessment = {'_fast_route': run['assessment']['_fast_route'], **changes}
                self.assertFalse(smoke.fast_native_identity('claude', marker_rows, LEAD,
                    {**run, 'assessment': assessment}, home, fixture.project))
            parent = fixture.parent.read_text(encoding='utf-8')
            meta = fixture.meta.read_text(encoding='utf-8')
            for case in ('duplicate', 'root', 'cwd', 'relative-cwd', 'model', 'type', 'depth', 'launch', 'route'):
                with self.subTest(case=case):
                    fixture.parent.write_text(parent, encoding='utf-8')
                    fixture.meta.write_text(meta, encoding='utf-8')
                    row = json.loads(parent)
                    metadata = json.loads(meta)
                    if case == 'duplicate':
                        fixture.parent.write_text(parent + parent, encoding='utf-8')
                    elif case in ('root', 'cwd', 'relative-cwd', 'model', 'type'):
                        if case == 'root':
                            row['sessionId'] = 'foreign'
                        if case == 'cwd':
                            row['cwd'] = str(fixture.root)
                        if case == 'relative-cwd':
                            row['cwd'] = '.'
                        if case == 'model':
                            row['message']['content'][0]['input']['model'] = 'foreign-model'
                        if case == 'type':
                            row['message']['content'][0]['name'] = 'Other'
                        fixture.parent.write_text(json.dumps(row) + '\n', encoding='utf-8')
                    else:
                        if case == 'depth':
                            metadata['spawnDepth'] = 2
                        if case == 'launch':
                            metadata['toolUseId'] = 'foreign'
                        if case == 'route':
                            metadata['agentType'] = 'symphony:symphony-lead-claude-sonnet-5-high'
                        fixture.meta.write_text(json.dumps(metadata), encoding='utf-8')
                    self.assertFalse(smoke.fast_native_identity('claude', marker_rows, LEAD,
                                                               run, home, fixture.project))
            fixture.parent.write_text(parent, encoding='utf-8')
            fixture.meta.write_text(meta, encoding='utf-8')
            foreign = [{**row, 'agentId': 'foreign'} for row in rows]
            self.assertFalse(smoke.fast_native_identity('claude', foreign, LEAD, run, home, fixture.project))
            other = fixture.child.with_name(fixture.child.name).parent.parent.parent / 'other' / 'subagents'
            other.mkdir(parents=True)
            (other / fixture.child.name).write_text(fixture.child.read_text(encoding='utf-8'), encoding='utf-8')
            # Only a duplicate under the exact root/session is ambiguous.
            self.assertTrue(smoke.fast_native_identity('claude', rows, LEAD, run, home, fixture.project))
        finally:
            fixture.doCleanups()
            fixture.tearDown()

    def test_claude_completing_probe_exports_signed_chronology_without_native_content(self):
        from plugins.symphony.tests.test_claude_host_evidence import ClaudeHostEvidenceTests, SESSION
        from plugins.symphony.symphony.model import ProjectState
        fixture = ClaudeHostEvidenceTests()
        fixture.setUp()
        try:
            fixture.write_root_prompt()
            run = replace(fixture.run, status='completing',
                          started_at='2026-09-29T02:01:00.001+00:00', outcome={'status': 'completed'})
            fixture.store.save(fixture.project, ProjectState(active_run=run))
            document = json.loads(fixture.store._path(fixture.project).read_text())
            home = Path(fixture.environ['CLAUDE_CONFIG_DIR'])
            rejected = smoke.claude_completion_probe(document, SESSION, fixture.project, home)
            self.assertEqual(rejected['freshness'], 'unknown')
            self.assertEqual(rejected['prompt_activity']['launch_minus_run_start_ms'], -1)
            self.assertEqual(rejected['prompt_activity']['stage'], 'launch_before_hook')
            run = replace(run, assessment={**run.assessment,
                '_fast_route': {'model': 'claude-sonnet-5', 'effort': 'low'},
                '_claude_fast_launch_hash': hashlib.sha256(b'toolu_launch').hexdigest()})
            fixture.store.save(fixture.project, ProjectState(active_run=run))
            document = json.loads(fixture.store._path(fixture.project).read_text())
            accepted = smoke.claude_completion_probe(document, SESSION, fixture.project, home)
            self.assertEqual(accepted['freshness'], 'none')
            self.assertEqual(accepted['prompt_activity']['stage'], 'accepted')
            self.assertEqual(accepted['prompt_activity']['launch_minus_run_start_ms'], -1)
            self.assertTrue(accepted['prompt_activity']['launch_hash_present'])
            self.assertTrue(accepted['prompt_activity']['launch_hash_valid'])
            self.assertTrue(accepted['prompt_activity']['launch_hash_matches_native_meta'])
            self.assertTrue(accepted['prompt_activity']['genuine_fast_owner'])
            self.assertNotIn(SESSION, json.dumps(accepted))
            self.assertNotIn('toolu_launch', json.dumps(accepted))
            self.assertNotIn(str(fixture.project), json.dumps(accepted))
            fixture.store.save(fixture.project, ProjectState(recent_runs=(replace(run, status='completed'),)))
            archive_probe = smoke.claude_completion_probe(json.loads(
                fixture.store._path(fixture.project).read_text()), SESSION, fixture.project, home)
            self.assertTrue(archive_probe['from_completed_archive'])
            self.assertEqual(archive_probe['freshness'], 'none')
            self.assertEqual(archive_probe['prompt_activity']['launch_minus_run_start_ms'], -1)
            rows = [json.loads(line) for line in fixture.child.read_text().splitlines()]
            rows[-1]['message']['model'] = 'foreign-model'
            fixture.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            run = replace(run, assessment={**run.assessment, '_claude_native_recovery': 'prompt_id:prompt-one'})
            fixture.store.save(fixture.project, ProjectState(active_run=run))
            later_rejection = smoke.claude_completion_probe(json.loads(
                fixture.store._path(fixture.project).read_text()), SESSION, fixture.project, home)
            self.assertEqual(later_rejection['freshness'], 'unknown')
            self.assertTrue(later_rejection['native_reader']['accepted_fast_launch_anchor_matches'])
            self.assertEqual(later_rejection['native_reader']['stage'], 'terminal')
            run = replace(run, assessment={**run.assessment,
                '_claude_fast_root_prompt_hash': hashlib.sha256(b'foreign-root-prompt').hexdigest()})
            fixture.store.save(fixture.project, ProjectState(active_run=run))
            mismatch = smoke.claude_completion_probe(json.loads(
                fixture.store._path(fixture.project).read_text()), SESSION, fixture.project, home)
            components = mismatch['native_reader']
            self.assertTrue(components['launch_hash_matches_native_meta'])
            self.assertTrue(components['genuine_fast_owner'])
            self.assertTrue(components['root_prompt_hash_present'])
            self.assertFalse(components['root_prompt_hash_matches_selected_native_prompt'])
            self.assertFalse(components['root_prompt_hash_matches_any_prior_native_prompt'])
            parent = [json.loads(line) for line in fixture.parent.read_text().splitlines()]
            parent.insert(1, {**parent[0], 'uuid': 'new-native-root-prompt',
                              'timestamp': '2026-09-29T02:00:40Z'})
            fixture.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent), encoding='utf-8')
            run = replace(run, assessment={**run.assessment,
                '_claude_fast_root_prompt_hash': hashlib.sha256(b'root-prompt').hexdigest()})
            fixture.store.save(fixture.project, ProjectState(active_run=run))
            earlier_match = smoke.claude_completion_probe(json.loads(
                fixture.store._path(fixture.project).read_text(encoding='utf-8')), SESSION, fixture.project, home)['native_reader']
            self.assertEqual(earlier_match['prior_native_root_prompt_count'], 2)
            self.assertFalse(earlier_match['root_prompt_hash_matches_selected_native_prompt'])
            self.assertTrue(earlier_match['root_prompt_hash_matches_any_prior_native_prompt'])
        finally:
            fixture.doCleanups()
            fixture.tearDown()

    def test_raw_tool_diagnostics_include_failed_and_missing_calls_without_arguments(self):
        rows = [{'payload': {'type': 'custom_tool_call', 'name': 'functions.exec',
                             'input': 'PRIVATE_SENTINEL'}},
                {'payload': {'type': 'function_call', 'name': 'apply_patch', 'call_id': 'failed'}},
                {'payload': {'type': 'function_call_output', 'call_id': 'failed', 'output': 'failed'}}]
        categories = smoke.raw_tool_categories('codex', rows)
        self.assertEqual(categories['exec_wrapper'], 1)
        self.assertEqual(categories['edit'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(categories))
        claude = [{'message': {'content': [
            {'type': 'tool_use', 'id': 'a', 'name': 'Agent', 'input': {'prompt': 'PRIVATE'}},
            {'type': 'tool_use', 'id': 'b', 'name': 'Agent'},
            {'type': 'tool_result', 'tool_use_id': 'a', 'is_error': True}]}}]
        counts = smoke.raw_launch_counts('claude', claude)
        self.assertEqual(counts, {'calls': 2, 'successful_results': 0, 'error_results': 1, 'missing_results': 1})
        self.assertEqual(smoke.raw_tool_categories('claude', claude)['other'], 2)
        self.assertNotIn('PRIVATE', json.dumps(counts))

    def test_fast_native_identity_does_not_select_an_assessed_lead_sharing_its_route(self):
        fast = {'model': 'gpt-6.1-sol', 'effort': 'medium'}
        run = {'session_id': 'root', 'assessment': {'_fast_route': fast}, 'delegations': [
            {'identity': identity, 'role': 'lead', 'requested_tier': fast['model'],
             'requested_effort': fast['effort']} for identity in ('assessed', 'fast')]}
        header = {'type': 'session_meta', 'payload': {'id': 'fast',
            'agent_path': '/root/symphony_lead_fast_gpt_6_1_sol_medium',
            'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}
        self.assertTrue(smoke.fast_native_identity('codex', [header], 'fast', run))
        unique = {**header['payload'], 'agent_path': header['payload']['agent_path'] + '_second_objective'}
        self.assertTrue(smoke.fast_native_identity('codex', [{'type': 'session_meta', 'payload': unique}], 'fast', run))
        for changed in ({**header['payload'], 'id': 'assessed',
                         'agent_path': '/root/symphony_lead_gpt_6_1_sol_medium'},
                        {**header['payload'], 'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'foreign'}}}},
                        {**header['payload'], 'id': 'foreign'},
                        {**unique, 'agent_path': unique['agent_path'] + '/nested'},
                        {**unique, 'source': {'subagent': {'thread_spawn': {
                            'parent_thread_id': 'root', 'agent_path': '/root/foreign'}}}},
                        {**unique, 'agent_path': header['payload']['agent_path'] + 'foreign'},
                        {**unique, 'agent_path': header['payload']['agent_path'] + '_'}):
            self.assertFalse(smoke.fast_native_identity('codex', [{'type': 'session_meta', 'payload': changed}],
                                                       changed['id'], run))
        call = {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'functions.exec',
                                                    'input': 'PRIVATE_SENTINEL'}}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / 'codex-baseline-home' / 'sessions'
            sessions.mkdir(parents=True)
            assessed = {'type': 'session_meta', 'payload': {
                **header['payload'], 'id': 'assessed', 'agent_path': '/root/symphony_lead_gpt_6_1_sol_medium'}}
            (sessions / 'assessed.jsonl').write_text('\n'.join(json.dumps(row) for row in (assessed, call)), encoding='utf-8')
            (sessions / 'fast.jsonl').write_text(json.dumps(header), encoding='utf-8')
            diagnostics = smoke.failure_diagnostics('codex', root, {'recent_runs': [run]})
            self.assertEqual(diagnostics['fast_native_provenance']['matching_native_candidates'], 1)
            self.assertEqual(diagnostics['fast_raw_tool_categories']['exec_wrapper'], 0)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(diagnostics))

    def test_inherited_rows_cannot_prove_fast_decision_or_before_change_calls(self):
        own = {'type': 'session_meta', 'payload': {'id': 'fast'}}
        inherited = {'type': 'session_meta', 'payload': {'id': 'root'}}
        call = {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'functions.exec'}}
        self.assertTrue(smoke.fast_turn_has_only_escalation('codex', [own], [], 'fast'))
        self.assertFalse(smoke.fast_turn_has_only_escalation('codex', [own, inherited, call], [], 'fast'))
        self.assertFalse(smoke.fast_turn_has_only_escalation('codex', [own], [], 'foreign'))
        fork = {'type': 'session_meta', 'payload': {'id': 'fast', 'forked_from_id': 'root'}}
        self.assertFalse(smoke.fast_turn_has_only_escalation('codex', [fork], [], 'fast'))

    def test_pre_filter_fast_identity_diagnostics_explain_nested_and_extended_paths_without_admission(self):
        identity, root_id = 'PRIVATE_CHILD', 'PRIVATE_ROOT'
        run = {'provider': 'codex', 'session_id': root_id,
               'assessment': {'_fast_route': {'model': 'model', 'effort': 'medium'}},
               'delegations': [{'identity': identity, 'role': 'lead',
                                'requested_tier': 'model', 'requested_effort': 'medium'}]}
        path = '/PRIVATE_PATH/symphony_lead_fast_model_medium'
        header = {'type': 'session_meta', 'payload': {'id': identity,
            'source': {'subagent': {'thread_spawn': {'parent_thread_id': root_id, 'agent_path': path}}}}}
        marker = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                  'content': [{'type': 'output_text', 'text': 'SYMPHONY_FAST_DECISION: eligible'}]}}
        facts = smoke.codex_fast_identity_probe([header, marker], identity, run)
        self.assertTrue(facts['own_first_header'])
        self.assertTrue(facts['root_parent_matches'])
        self.assertTrue(facts['model_matches_fast'])
        self.assertTrue(facts['nested_basename_exact'])
        self.assertFalse(facts['top_level_path_present'])
        self.assertFalse(smoke.fast_native_identity('codex', [header, marker], identity, run))
        extended = json.loads(json.dumps(header))
        extended['payload']['agent_path'] = path + '_retry'
        facts = smoke.codex_fast_identity_probe([extended, marker], identity, run)
        self.assertTrue(facts['top_level_fast_prefix'])
        self.assertFalse(facts['top_level_basename_exact'])
        self.assertFalse(smoke.fast_native_identity('codex', [extended, marker], identity, run))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / 'codex-baseline-home' / 'sessions'
            native.mkdir(parents=True)
            (native / 'child.jsonl').write_text('\n'.join(json.dumps(row) for row in (header, marker)), encoding='utf-8')
            diagnostic = smoke.failure_diagnostics('codex', root, {'recent_runs': [run]})
            self.assertEqual(diagnostic['fast_native_provenance']['matching_native_candidates'], 0)
            self.assertEqual(len(diagnostic['fast_identity_components']), 1)
            self.assertTrue(diagnostic['fast_identity_components'][0]['nested_basename_exact'])
            self.assertNotIn('PRIVATE_', json.dumps(diagnostic))

    def test_decision_representation_diagnostics_never_deduplicate_missing_or_distinct_identity(self):
        header = {'type': 'session_meta', 'payload': {'id': 'fast'}}
        turn = {'type': 'turn_context', 'payload': {'turn_id': 'turn'}}
        message = {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'id': 'message',
            'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'SYMPHONY_FAST_DECISION: eligible'}]}}
        same = smoke.decision_representations('codex', [header, turn, message, message], 'fast')
        self.assertEqual(same['marker_lines'], 2)
        self.assertEqual(same['equivalent_same_id_records'], 1)
        self.assertEqual(same['own_turn_bound_records'], 2)
        self.assertEqual(same['phases']['final_answer'], 2)
        for changed in ({**message['payload'], 'id': None}, {**message['payload'], 'id': 'distinct'},
                        {**message['payload'], 'phase': 'commentary'},
                        {**message['payload'], 'content': [{'type': 'output_text', 'text':
                         'SYMPHONY_FAST_DECISION: escalate\nPRIVATE_SENTINEL'}]}):
            result = smoke.decision_representations('codex', [header, turn, message,
                                                    {'type': 'response_item', 'payload': changed}], 'fast')
            self.assertEqual(result['marker_lines'], 2)
            self.assertEqual(result['equivalent_same_id_records'], 0)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
        next_turn = {'type': 'turn_context', 'payload': {'turn_id': 'next'}}
        other_turn = smoke.decision_representations('codex', [header, turn, message, next_turn, message], 'fast')
        self.assertEqual(other_turn['equivalent_same_id_records'], 0)
        unbound = smoke.decision_representations('codex', [header, message, message], 'fast')
        self.assertEqual(unbound['equivalent_same_id_records'], 0)
        duplicate_line = {**message, 'payload': {**message['payload'], 'content': [{'type': 'output_text',
            'text': 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_FAST_DECISION: eligible'}]}}
        result = smoke.decision_representations('codex', [header, turn, duplicate_line], 'fast')
        self.assertEqual(result['marker_lines'], 2)
        self.assertEqual(result['marker_records'], 1)
        self.assertEqual(result['equivalent_same_id_records'], 0)

    def test_fixture_verification_is_observable_without_forcing_routing(self):
        instructions = smoke.FIXTURE_INSTRUCTIONS
        self.assertIn('one standalone native', instructions)
        self.assertIn('full structured tool result', instructions)
        self.assertIn('text(await tools.exec_command({"cmd":"python -m unittest -q"}));', instructions)
        self.assertNotIn('SYMPHONY_ROLE', instructions)
        self.assertNotIn('SYMPHONY_FAST_DECISION', instructions)

    def test_lifecycle_diagnostics_count_binding_and_conditions_without_secrets(self):
        from plugins.symphony.symphony.model import Event
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = smoke.StateStore(root / 'state')
            state_path = root / 'state' / ('a' * 64 + '.v2.json')
            store.bind_session('claude', 'root', state_path, True)
            store.bind_session('claude', 'child', state_path, True, owner_session='root')
            payload = {'provider': 'claude', 'session_id': 'root', 'agent_id': 'child',
                       'parent_thread_id': 'root', 'prompt_id': 'turn', 'last_assistant_message': 'PRIVATE_SENTINEL'}
            store.queue_session_event('claude', 'child', Event('start', 'subagent_started', 'now', payload), ambiguous_owner=True)
            store.queue_session_event('claude', 'root', Event('stop', 'subagent_stopped', 'now',
                                      {**payload, 'agent_id': 'foreign', 'prompt_id': 'other'}))
            document = {'active_runs': {'claude:root': {'provider': 'claude', 'session_id': 'root',
                'lead_identity': 'lead', 'outcome': {'status': 'completed'}, 'assessment': {
                    '_batch_pending': True, '_active_turns': {'child': 'prompt_id:turn'},
                    '_lead_route_mismatch': 'PRIVATE_SENTINEL'},
                'delegations': [{'identity': 'child', 'state': 'completed'}]}}}
            result = smoke.lifecycle_diagnostics('claude', root, document)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
            self.assertEqual(result[0]['pending_kinds']['subagent_started'], 1)
            self.assertEqual(result[0]['pending_kinds']['subagent_stopped'], 1)
            self.assertEqual(result[0]['records']['alias'], 1)
            self.assertEqual(result[0]['invocation_matches']['agent_known'], 1)
            self.assertEqual(result[0]['invocation_matches']['turn_active'], 1)
            self.assertEqual(result[0]['invocation_matches']['ambiguous_owner'], 1)
            self.assertTrue(result[0]['stop_conditions']['batch_pending'])
            self.assertTrue(result[0]['stop_conditions']['lead_route_mismatch'])

    def test_fast_decision_allows_horizontal_whitespace_but_keeps_exact_line_and_count(self):
        for suffix in ('', '  ', '\t', ' \t\r'):
            self.assertEqual(smoke.fast_decision_lines('SYMPHONY_FAST_DECISION: eligible' + suffix), ['eligible'])
        for text in ('SYMPHONY_FAST_DECISION: eligible extra', 'SYMPHONY_FAST_DECISION: yes',
                     'quoted SYMPHONY_FAST_DECISION: eligible'):
            self.assertEqual(smoke.fast_decision_lines(text), [])
        duplicated = 'SYMPHONY_FAST_DECISION: eligible  \nSYMPHONY_FAST_DECISION: eligible'
        self.assertEqual(len(smoke.fast_decision_lines(duplicated)), 2)

    def test_pending_codex_stop_diagnostics_bind_exact_prior_evidence_without_ack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'primary'
            project.mkdir()
            owner = '00000000-0000-0000-0000-000000000001'
            child = '00000000-0000-0000-0000-000000000002'
            lead = '00000000-0000-0000-0000-000000000003'
            store = smoke.StateStore(root / 'state')
            store.bind_session('codex', owner, store._path(project), True, project=project)
            store.bind_session('codex', child, store._path(project), True, project=project, owner_session=owner)
            payload = {'provider': 'codex', 'session_id': child, 'agent_id': child,
                       'parent_thread_id': owner, 'turn_id': 'PRIVATE_TURN', 'role': 'lead',
                       'model': 'model', 'model_reasoning_effort': 'medium', 'status': 'completed',
                       'last_assistant_message': 'PRIVATE_REPORT'}
            source = smoke.Event('PRIVATE_EVENT', 'subagent_stopped', '2026-10-01T00:00:03Z', payload)
            store.queue_session_event('codex', child, source, ambiguous_owner=True)
            record = store.session_record('codex', child)
            entry = record['pending'][0]
            scoped = replace(source, payload={**payload, 'session_id': owner})
            result_hash = smoke._terminal_result_id(scoped)
            token = 'turn_id:PRIVATE_TURN'
            start_hash = hashlib.sha256(('codex-host-turn\0' + child + '\0PRIVATE_TURN').encode()).hexdigest()
            current = {'run_id': 'current', 'provider': 'codex', 'session_id': owner,
                       'lead_identity': lead, 'started_at': '2026-10-01T00:00:04Z', 'assessment': {},
                       'delegations': [{'identity': lead, 'role': 'lead', 'state': 'completed'}]}
            prior = {**current, 'run_id': 'prior', 'started_at': '2026-10-01T00:00:00Z',
                     'delegations': [{'identity': child, 'role': 'lead', 'state': 'completed',
                                      'requested_tier': 'model', 'requested_effort': 'medium'}],
                     'assessment': {'_terminal_turns': {child: [token]}, '_start_event_ids': [start_hash],
                                    '_terminal_event_ids': [result_hash]}}
            receipt = {'provider': 'codex', 'session': owner, 'agent': child, 'turn': token,
                       'run_id': 'prior', 'result': result_hash, 'parent': owner, 'lead': child}
            document = {'active_runs': {'codex:' + owner: current}, 'recent_runs': [prior],
                        'terminal_receipts': [receipt], 'event_history': [
                            {'event_id': source.event_id + ':delegation:delegation_updated'}]}
            native = root / 'codex-baseline-home' / 'sessions' / (child + '.jsonl')
            native.parent.mkdir(parents=True)
            rows = [{'type': 'session_meta', 'payload': {'id': child, 'cwd': str(project),
                      'agent_path': '/root/symphony_lead_fast_model_medium',
                      'source': {'subagent': {'thread_spawn': {'parent_thread_id': owner}}}}},
                    {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:01Z',
                     'payload': {'type': 'task_started', 'turn_id': 'PRIVATE_TURN'}},
                    {'type': 'turn_context', 'payload': {'turn_id': 'PRIVATE_TURN', 'model': 'model', 'effort': 'medium'}},
                    {'type': 'event_msg', 'timestamp': '2026-10-01T00:00:02Z',
                     'payload': {'type': 'task_complete', 'turn_id': 'PRIVATE_TURN', 'last_agent_message': 'PRIVATE_REPORT'}}]
            def write(rows):
                native.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            write(rows)
            before = {path: path.read_bytes() for path in (root / 'state').rglob('*') if path.is_file()}
            probe = smoke.codex_pending_terminal_probe(root, document, current, record, entry)
            self.assertTrue(probe['source']['event_before_current_run'])
            self.assertTrue(probe['source']['ambiguous_owner'])
            self.assertTrue(probe['source']['root_normalization_bound'])
            self.assertEqual(probe['source']['parent_category'], 'root')
            self.assertEqual(probe['source']['session_category'], 'bound_alias')
            self.assertEqual(probe['runs']['current_delegation'], 0)
            self.assertEqual(probe['runs']['prior_superseded'], 1)
            self.assertEqual(probe['runs']['prior_native_start_anchor'], 1)
            self.assertEqual(probe['runs']['prior_terminal_turn'], 1)
            self.assertEqual(probe['receipts']['raw_result'], 0)
            self.assertEqual(probe['receipts']['prior_run_result_parent_scoped'], 1)
            self.assertEqual(probe['receipts']['prior_run_all_fields_scoped'], 0)
            self.assertEqual(probe['history']['derived_terminal_exact'], 1)
            self.assertTrue(probe['native']['unique_completed_turn'])
            self.assertTrue(probe['native']['report_matches_callback'])
            self.assertEqual(probe['native']['task_role'], 'fast_lead')
            self.assertEqual(probe['native']['retained_child_route_matches'], 1)
            self.assertEqual(before, {path: path.read_bytes() for path in (root / 'state').rglob('*') if path.is_file()})
            for secret in ('PRIVATE', child, owner, lead, str(project), result_hash, start_hash):
                self.assertNotIn(secret, json.dumps(probe))
            result = smoke.lifecycle_diagnostics('codex', root, document)[0]
            self.assertEqual(len(result['pending_terminals']), 1)
            self.assertFalse(result['pending_terminal_details_truncated'])
            for field, value in (('owner_session', 'foreign'), ('generation', 99), ('state_name', 'foreign')):
                foreign = smoke.codex_pending_terminal_probe(root, document, current, {**record, field: value}, entry)
                self.assertFalse(foreign['source']['root_normalization_bound'])
                self.assertEqual(foreign['receipts']['scoped_result'], 0)
            for field, value in (('provider', 'claude'), ('session', 'foreign'), ('agent', 'foreign'),
                                 ('turn', 'foreign'), ('result', 'foreign'), ('parent', 'foreign')):
                changed = smoke.codex_pending_terminal_probe(root, {**document, 'terminal_receipts': [{**receipt, field: value}]},
                                                             current, record, entry)
                self.assertEqual(changed['receipts']['prior_run_result_parent_scoped'], 0)
            write([rows[0], rows[0], *rows[1:]])
            inherited = smoke.codex_pending_terminal_probe(root, document, current, record, entry)
            self.assertFalse(inherited['native']['unforked'])
            self.assertNotIn('unique_completed_turn', inherited['native'])
            write([*rows, rows[-1]])
            duplicated = smoke.codex_pending_terminal_probe(root, document, current, record, entry)
            self.assertFalse(duplicated['native']['unique_completed_turn'])
            write([*rows, {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'new'}}])
            unfinished = smoke.codex_pending_terminal_probe(root, document, current, record, entry)
            self.assertTrue(unfinished['native']['newer_started_turn'])
            self.assertTrue(unfinished['native']['latest_turn_unfinished'])

    def test_failure_diagnostics_export_categories_without_native_secrets(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / 'codex-hook-capture'
            capture.mkdir()
            (capture / 'event.json').write_text(json.dumps({'event': 'UserPromptSubmit', 'prompt': 'PRIVATE_SENTINEL'}))
            sessions = root / 'codex-baseline-home' / 'sessions'
            sessions.mkdir(parents=True)
            (sessions / 'root.jsonl').write_text(json.dumps({'payload': {
                'type': 'function_call_output', 'output': 'Unknown model `PRIVATE_SENTINEL` for spawn_agent.'}}))
            document = {'enabled': True, 'activation': {'codex': {'state': 'guarded', 'profile': 'full',
                        'plugin_version': '1.7.0', 'private_key': 'PRIVATE_SENTINEL'}},
                        'configuration': {'assessor_boosts': {'session': 'PRIVATE_SENTINEL'}}}
            result = smoke.failure_diagnostics('codex', root, document)
            self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
            self.assertEqual(result['callbacks']['UserPromptSubmit'], 1)
            self.assertTrue(result['native_signals']['spawn_unknown_model'])
            self.assertEqual(result['profile'], 'full')

    def test_claude_phase_diagnostics_keep_exact_objective_ranges_and_private_outputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / 'logs'
            logs.mkdir()
            home = root / 'claude-baseline-home'
            session = 'PRIVATE_ROOT'
            path = home / 'projects' / 'fixture' / (session + '.jsonl')
            path.parent.mkdir(parents=True)
            rows = [{'type': 'file-history-snapshot', 'snapshot': 'PRIVATE_SNAPSHOT'},
                    {'type': 'assistant', 'sessionId': session, 'message': {'content': [
                        {'type': 'tool_use', 'name': 'Skill', 'input': {'skill': 'PRIVATE_SKILL'}}], 'stop_reason': 'tool_use'}},
                    {'type': 'assistant', 'sessionId': session, 'message': {'content': [
                        {'type': 'text', 'text': 'PRIVATE_FINAL \u201d'}], 'stop_reason': 'end_turn'}}]
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            enable = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'PRIVATE_ENABLE'}) + '\n'
            objective = json.dumps({'type': 'result', 'subtype': 'error_max_budget_usd',
                                    'is_error': True, 'result': 'PRIVATE_BUDGET'}) + '\n'
            (logs / 'stdout').write_text(enable + objective, encoding='utf-8')
            phases = [{'phase': 'enable', 'returned': True, 'cli_success': True, 'root_session': session,
                       'root_rows_before': 0, 'root_rows_after': 1,
                       'stdout_begin': 0, 'stdout_end': len(enable.encode('utf-8'))},
                      {'phase': 'objective', 'returned': True, 'cli_success': False, 'root_session': session,
                       'root_rows_before': 1, 'root_rows_after': 3,
                       'stdout_begin': len(enable.encode('utf-8')), 'stdout_end': len((enable + objective).encode('utf-8'))}]
            (logs / 'phases.json').write_text(json.dumps(phases), encoding='utf-8')
            facts = smoke.claude_phase_diagnostics(root, home)
            self.assertEqual([phase['budget_exceeded'] for phase in facts], [False, True])
            self.assertEqual([phase['result_error'] for phase in facts], [False, True])
            self.assertTrue(all(phase['native_range_available'] for phase in facts))
            self.assertEqual(facts[0]['root_tools']['Skill'], 0)
            self.assertEqual(facts[1]['root_tools']['Skill'], 1)
            self.assertEqual(facts[1]['root_tools']['Agent'], 0)
            self.assertEqual(facts[1]['root_final_end_turns'], 1)
            self.assertNotIn('PRIVATE', json.dumps(facts))
            rows[-1]['agentId'] = 'foreign-child'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            self.assertFalse(smoke.claude_phase_diagnostics(root, home)[1]['native_range_available'])
            phases[1]['stdout_end'] += 1
            phases[1]['root_rows_after'] = 100
            (logs / 'phases.json').write_text(json.dumps(phases), encoding='utf-8')
            rejected = smoke.claude_phase_diagnostics(root, home)[1]
            self.assertFalse(rejected['stdout_range_available'])
            self.assertFalse(rejected['native_range_available'])

    def test_documented_enable_precedes_plain_objectives_and_requires_durable_state(self):
        self.assertEqual(smoke.setup_prompt('codex'), '$symphony:symphony enable')
        self.assertEqual(smoke.setup_prompt('claude'), '/symphony:enable')
        self.assertTrue(all(not prompt.startswith(('$symphony', '/symphony')) for prompt in smoke.CASES.values()))
        document = {'enabled': True, 'activation': {'claude': {
            'state': 'guarded', 'profile': 'opus-5-5', 'plugin_version': '1.7.0'}}}
        smoke.assert_enabled_fixture(document, 'claude', 'opus-5-5', '1.7.0')
        for changed in ({**document, 'enabled': False}, {**document, 'active_run': {'status': 'working'}},
                        {**document, 'recent_runs': [{'status': 'completed'}]},
                        {**document, 'activation': {'claude': {'state': 'guarded', 'profile': 'old', 'plugin_version': '1.7.0'}}}):
            with self.assertRaises(RuntimeError):
                smoke.assert_enabled_fixture(changed, 'claude', 'opus-5-5', '1.7.0')

    def test_worker_origin_rejects_forked_calls_and_requires_explicit_native_route(self):
        own = {"type": "session_meta", "payload": {"id": "worker",
               "agent_path": "/root/lead/symphony_worker_gpt_6_luna_low",
               "source": {"subagent": {"thread_spawn": {"parent_thread_id": "lead"}}}}}
        self.assertTrue(smoke.worker_transcript_is_unforked("codex", [own], "worker"))
        self.assertFalse(smoke.worker_transcript_is_unforked("codex", [own], "foreign"))
        fork = {"type": "session_meta", "payload": {"id": "worker", "forked_from_id": "lead"}}
        inherited = {"type": "session_meta", "payload": {"id": "lead"}}
        self.assertFalse(smoke.worker_transcript_is_unforked("codex", [fork], "worker"))
        self.assertFalse(smoke.worker_transcript_is_unforked("codex", [own, inherited], "worker"))
        worker = {"identity": "worker", "requested_tier": "gpt-6-luna", "requested_effort": "low"}
        arguments = {"message": "SYMPHONY_ROLE: worker\nFix the greeting.", "fork_turns": "none",
                     "model": "gpt-6-luna", "reasoning_effort": "low"}
        # Sanitized native shape: encrypted packet and a returned canonical path.
        arguments["message"] = "encrypted-native-packet"
        evidence = [("spawn_agent", arguments, "", '{"task_name":"/root/lead/symphony_worker_gpt_6_luna_low"}')]
        arguments["task_name"] = "symphony_worker_gpt_6_luna_low"
        def verified(calls=evidence):
            return smoke.worker_launch_verified("codex", calls, worker, [own], "lead", "/root/lead")
        self.assertTrue(verified())
        self.assertFalse(verified(evidence + evidence))
        self.assertFalse(verified([]))
        own["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "foreign"
        self.assertFalse(verified())
        own["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "lead"
        own["payload"]["agent_path"] = "/root/foreign/symphony_worker_gpt_6_luna_low"
        self.assertFalse(verified())
        own["payload"]["agent_path"] = "/root/lead/symphony_worker_gpt_6_luna_low"
        arguments["fork_turns"] = "all"
        self.assertFalse(verified())
        arguments["fork_turns"] = "none"
        arguments.pop("model")
        self.assertFalse(verified())

    def test_codex_edit_needs_a_matching_successful_native_result(self):
        call = {"payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "patch",
                            "input": "*** Begin Patch\n*** Update File: greet.py\n-hello\n+Hello\n*** End Patch"}}
        success = {"payload": {"type": "custom_tool_call_output", "call_id": "patch", "output": "Success."}}
        failure = {"payload": {"type": "custom_tool_call_output", "call_id": "patch", "output": "Error: bad patch"}}
        for rows, expected in (([call], False), ([call, failure], False), ([call, success], True)):
            self.assertEqual(smoke.fixture_edit("codex", smoke.tool_evidence("codex", rows), Path('/fixture')), expected)
        self.assertFalse(smoke.fixture_edit("codex", [("exec_command", {"cmd": "cat greet.py"}, "", "")], Path('/fixture')))
        self.assertFalse(smoke.fixture_edit("codex", [("exec", 'text("apply_patch greet.py");', "", "Success")], Path('/fixture')))
        unknown = {"payload": {"type": "custom_tool_call_output", "call_id": "patch", "output": "{}"}}
        self.assertFalse(smoke.fixture_edit("codex", smoke.tool_evidence("codex", [call, unknown]), Path('/fixture')))
        source = 'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: greet.py\\n-hello\\n+Hello\\n*** End Patch"));'
        self.assertFalse(smoke.fixture_edit("codex", [("exec", source, "", "Script completed\nFailed to find expected lines in greet.py")], Path('/fixture')))
        native_success = json.dumps([{"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
                                     {"type": "input_text", "text": "{}"}])
        native_failure = json.dumps([{"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": "Script error:\napply_patch verification failed: Failed to find expected lines in /fixture/greet.py"}])
        self.assertTrue(smoke.fixture_edit("codex", [("exec", source, "", native_success)], Path('/fixture')))
        self.assertFalse(smoke.fixture_edit("codex", [("exec", source, "", native_failure)], Path('/fixture')))
        unknown["payload"]["output"] = "Failed to find expected lines in greet.py"
        self.assertFalse(smoke.fixture_edit("codex", smoke.tool_evidence("codex", [call, unknown]), Path('/fixture')))
        self.assertFalse(smoke.fast_turn_has_only_escalation("codex", [call, failure], []))

    def test_claude_edit_and_handback_are_read_from_native_content(self):
        call = {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "id": "edit",
                "input": {"file_path": "/fixture/greet.py", "old_string": "hello", "new_string": "Hello"}}]}}
        result = {"message": {"content": [{"type": "tool_result", "tool_use_id": "edit", "is_error": False}]}}
        self.assertTrue(smoke.fixture_edit("claude", smoke.tool_evidence("claude", [call, result]), Path('/fixture')))
        result["message"]["content"][0]["is_error"] = True
        self.assertFalse(smoke.fixture_edit("claude", smoke.tool_evidence("claude", [call, result]), Path('/fixture')))
        mention = [("Write", {"file_path": "/fixture/notes.txt", "content": "greet.py"}, "", "Success")]
        self.assertFalse(smoke.fixture_edit("claude", mention, Path('/fixture')))
        report = {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "SubagentHandback",
                  "input": {"message": "SYMPHONY_FAST_DECISION: escalate"}}]}}
        self.assertEqual(smoke.assistant_text("claude", report), "SYMPHONY_FAST_DECISION: escalate")
        self.assertFalse(smoke.fast_turn_has_only_escalation("claude", [call], []))

    def test_literal_shell_apply_patch_requires_bound_cwd_and_successful_native_result(self):
        project = Path('/fixture')
        patch = "*** Begin Patch\n*** Update File: greet.py\n@@\n-    return f'hello, {name}!'\n+    return f'Hello, {name}!'\n*** End Patch"
        command = "apply_patch <<'PATCH'\n" + patch + "\nPATCH"
        body = "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess. Updated the following files:\nM greet.py\n"
        def evidence(command=command, body=body, workdir='/fixture', suffix=''):
            source = 'const r = await tools.exec_command(' + json.dumps({'cmd': command, 'workdir': workdir}) + ');\ntext(r.output);' + suffix
            output = json.dumps([{'type': 'input_text', 'text': 'Script completed\n'},
                                 {'type': 'input_text', 'text': body}])
            return [('exec', source, '', output)]
        self.assertTrue(smoke.fixture_edit('codex', evidence(), project))
        hidden_patch = "*** Begin Patch\n*** Update File: other.py\n@@\n-a\n+b\n*** End Patch\nPATCH\n: <<'REST'\n*** Update File: greet.py\n*** End Patch"
        self.assertFalse(smoke.fixture_edit('codex', evidence(
            command="apply_patch <<'PATCH'\n" + hidden_patch + "\nPATCH",
            body=body.replace('M greet.py', 'M other.py')), project))
        for altered in (evidence(body=body.replace('Exit code: 0', 'Exit code: 1')),
                        evidence(body='Success. Updated the following files:\nM greet.py'),
                        evidence(workdir='/foreign'), evidence(command=command + '\necho success'),
                        evidence(command=command.replace('greet.py', '../greet.py')),
                        evidence(command=command.replace("<<'PATCH'", '<<PATCH')),
                        evidence(suffix='text("forged success");')):
            self.assertFalse(smoke.fixture_edit('codex', altered, project))
        # Braces and key-like text inside a quoted string are data.
        value = {'cmd': "cat file,{private:unchanged}", 'workdir': '/fixture'}
        source = 'text(await tools.exec_command(' + json.dumps(value) + '));'
        self.assertEqual(smoke.composed_call(source, 'exec_command'), value)

    def test_native_composed_call_accepts_literal_js_quote_styles_only(self):
        expected = {'cmd': "cat file,{private:unchanged}", 'workdir': '/fixture'}
        for source in ("text(await tools.exec_command({cmd: 'cat file,{private:unchanged}', workdir: '/fixture'}));",
                       "const r = await tools.exec_command({cmd: 'cat file,{private:unchanged}', workdir: '/fixture'}); text(r.output);"):
            self.assertEqual(smoke.composed_call(source, 'exec_command', output_only=True), expected)
        for source in ("text(await tools.exec_command({cmd: getCommand()}));",
                       "text(await tools.exec_command({cmd: 'read'})); text(await tools.exec_command({cmd: 'write'}));",
                       "text(await tools.exec_command({cmd: `dynamic ${command}`}));",
                       r"text(await tools.exec_command({cmd: '\U00000067reet.py'}));",
                       r"text(await tools.exec_command({cmd: '\N{LATIN SMALL LETTER G}reet.py'}));",
                       "text(await tools.exec_command({cmd: 'read'})); mutate();"):
            self.assertIsNone(smoke.composed_call(source, 'exec_command', output_only=True))

    def test_verification_requires_an_executed_unittest_command(self):
        output = '"exit_code": 0, "output": "Ran 2 tests. OK"'
        self.assertTrue(smoke.unittest_verified([("exec_command", {"cmd": "python -m unittest -q"}, "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec_command", {"cmd": "echo 'python -m unittest -q'"}, "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec", 'text("tools.exec_command python -m unittest");', "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec_command", {"cmd": 'python -m unittest -q || echo "Ran 1 test OK"'}, "", output + " FAILED")]))

    def test_finite_bash_unittest_forms_bind_cwd_and_successful_native_result(self):
        with TemporaryDirectory(prefix='fixture space ') as temporary:
            project = Path(temporary).resolve()
            stdout = '----------------------------------------------------------------------\nRan 3 tests in 0.001s\n\nOK\n'
            def evidence(command, *, text=stdout, error=False, missing=False):
                rows = [{'message': {'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Bash',
                         'input': {'command': command}}]}}]
                if not missing:
                    rows.append({'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call',
                                 'content': text, 'is_error': error}]}})
                return smoke.tool_evidence('claude', rows)
            supported = ['python -m unittest -q', 'python3 -m unittest -v 2>&1',
                         f'cd "{project}" && python.exe -m unittest -q 2>&1',
                         f"cd '{project}' && python -m unittest"]
            for command in supported:
                with self.subTest(command=command):
                    self.assertTrue(smoke.unittest_verified(evidence(command), project))
                    self.assertFalse(smoke.unittest_verified(evidence(command, error=True), project))
                    self.assertFalse(smoke.unittest_verified(evidence(command, missing=True), project))
                    for text in ('Ran 0 tests in 0.001s\n\nOK\n', stdout + 'FAILED',
                                 'Ran 3 tests in 0.001s\n', 'echo Ran 3 tests in 0.001s OK'):
                        self.assertFalse(smoke.unittest_verified(evidence(command, text=text), project))
            unsafe = [f'cd "{project.parent}" && python -m unittest -q', 'cd . && python -m unittest -q',
                      f'cd "{project}"; python -m unittest -q', 'python -m unittest -q || echo OK',
                      'echo python -m unittest -q', '$(echo python) -m unittest -q',
                      '`echo python` -m unittest -q', 'python -m unittest -q && echo OK',
                      'python -m unittest -q 2>&1; echo OK', 'python -m unittest -q | cat',
                      'python -m unittest -q > output.txt', 'python -m unittest discover',
                      'python -m unittest test_greet', '/usr/bin/python3 -m unittest -q',
                      'python -m unittest -q\necho OK']
            for command in unsafe:
                with self.subTest(command=command):
                    self.assertFalse(smoke.unittest_verified(evidence(command), project))
            self.assertFalse(smoke.unittest_verified(evidence(supported[-1])))
            # This grammar belongs to Claude Bash, not Codex native exec.
            self.assertFalse(smoke.unittest_verified([('exec_command', {'cmd': supported[2]}, '', stdout)], project))
            rows = [{'message': {'content': [{'type': 'tool_use', 'id': str(index), 'name': 'Bash',
                      'input': {'command': command}}]}} for index, command in enumerate([
                          supported[2], unsafe[0], 'python -m unittest discover',
                          '/usr/bin/python3 -m unittest -q', 'python -m unittest -q || echo OK'])]
            facts = smoke.command_witness_probe('claude', rows, project)
            self.assertEqual(facts['exact_supported_unittest_calls'], 1)
            shapes = facts['bash_shapes']
            self.assertEqual(shapes['bound_cwd_prefix_present'], 2)
            self.assertEqual(shapes['bound_cwd_matches'], 1)
            self.assertEqual(shapes['stderr_merge_present'], 1)
            self.assertEqual(shapes['discover_arguments'], 1)
            self.assertEqual(shapes['absolute_interpreter'], 1)
            self.assertEqual(shapes['unsupported_shell_operators'], 1)
            self.assertNotIn(str(project), json.dumps(facts))

    def test_captured_output_wrapper_requires_exact_command_and_leading_test_result(self):
        source = 'const r = await tools.exec_command({cmd:"python -m unittest -q && git diff -- greet.py",workdir:"/fixture"}); text(r.output);'
        stdout = '----------------------------------------------------------------------\nRan 1 test in 0.000s\n\nOK\ndiff --git a/greet.py b/greet.py\n'
        def result(text):
            return json.dumps([{"type": "input_text", "text": "Script completed\nOutput:\n"},
                               {"type": "input_text", "text": text}])
        def verified(code=source, text=stdout):
            return smoke.unittest_verified([("exec", code, "", result(text))])
        self.assertTrue(verified())
        self.assertTrue(verified(text=stdout.replace('\n', '\r\n')))
        for text in (stdout.replace('Ran 1', 'Ran 0'), 'diff --git\n+' + stdout,
                     stdout.replace('OK', 'FAILED'), stdout + 'fatal: git diff failed', ''):
            self.assertFalse(verified(text=text))
        self.assertFalse(verified(code=source.replace('&& git diff -- greet.py', '|| echo fake')))
        self.assertFalse(verified(code=source.replace('&& git diff -- greet.py', '&& echo fake')))
        self.assertFalse(verified(code=source.replace('text(r.output)', 'text("' + 'Ran 1 test OK' + '")')))
        self.assertFalse(smoke.unittest_verified([("exec", source, "", '')]))

    def test_captured_structured_wrapper_requires_actual_successful_unittest_result(self):
        source = 'const r = await tools.exec_command({cmd:"python -m unittest -q"});\ntext(JSON.stringify(r));\n'
        stdout = '----------------------------------------------------------------------\nRan 3 tests in 0.000s\n\nOK\n'
        def verified(result, code=source):
            blocks = [{'type': 'input_text', 'text': 'Script completed\nOutput:\n'},
                      {'type': 'input_text', 'text': json.dumps(result)}]
            return smoke.unittest_verified([('exec', code, '', json.dumps(blocks))])
        self.assertTrue(verified({'exit_code': 0, 'output': stdout}))
        self.assertFalse(verified({'output': stdout}))
        self.assertFalse(verified({'exit_code': 1, 'output': stdout}))
        self.assertFalse(verified({'exit_code': 0, 'output': stdout.replace('OK', 'FAILED')}))
        self.assertFalse(verified({'exit_code': 0, 'output': stdout.replace('Ran 3', 'Ran 0')}))
        self.assertFalse(verified({'exit_code': 0, 'output': stdout}, source.replace('python -m unittest -q', 'echo fake')))
        self.assertFalse(verified({'exit_code': 0, 'output': stdout}, source.replace('python -m unittest -q', 'python -m unittest -q || echo fake')))
        self.assertFalse(verified({'exit_code': 0, 'output': stdout}, source.replace('JSON.stringify(r)', 'JSON.stringify({exit_code:0,output:"fake"})')))

    def test_captured_batch_maps_success_to_the_actual_unittest_index(self):
        source = ('const results=await Promise.all([tools.exec_command({cmd:"git diff -- greet.py test_greet.py"}),'
                  'tools.exec_command({cmd:"python -m unittest -q"})]); '
                  'for (let i=0;i<results.length;i++){text(JSON.stringify({i,output:results[i].output,exit_code:results[i].exit_code}));}')
        stdout = '----------------------------------------------------------------------\nRan 2 tests in 0.000s\n\nOK\n'
        def verified(results, code=source):
            blocks = [{"type": "input_text", "text": "Script completed\nOutput:\n"}]
            blocks += [{"type": "input_text", "text": json.dumps(result)} for result in results]
            return smoke.unittest_verified([("exec", code, "", json.dumps(blocks))])
        diff = {"i": 0, "output": "diff --git", "exit_code": 0}
        tests = {"i": 1, "output": stdout, "exit_code": 0}
        self.assertTrue(verified([diff, tests]))
        self.assertFalse(verified([diff]))
        self.assertFalse(verified([diff, {**tests, "i": 0}]))
        self.assertFalse(verified([tests, diff]))
        self.assertFalse(verified([diff, {**tests, "exit_code": 1}]))
        self.assertFalse(verified([{**diff, "exit_code": 1}, tests]))
        self.assertFalse(verified([{**diff, "output": stdout}, {**tests, "output": "FAILED"}]))
        self.assertFalse(verified([diff, tests], source.replace('python -m unittest -q', 'echo "python -m unittest -q"')))
        self.assertFalse(verified([diff, tests], source.replace('python -m unittest -q', 'python -m unittest -q || echo fake')))
        self.assertFalse(verified([diff, tests], source + 'text("fabricated");'))


if __name__ == "__main__":
    unittest.main()
