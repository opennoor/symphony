"""The native probe requires successful fixture edits, not transcript mentions."""

import importlib.util
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory

SCRIPTS = Path(__file__).resolve().parents[3] / ".github" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("native_routing_smoke", SCRIPTS / "native_routing_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class NativeRoutingEvidenceTests(unittest.TestCase):
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

    def test_verification_requires_an_executed_unittest_command(self):
        output = '"exit_code": 0, "output": "Ran 2 tests. OK"'
        self.assertTrue(smoke.unittest_verified([("exec_command", {"cmd": "python -m unittest -q"}, "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec_command", {"cmd": "echo 'python -m unittest -q'"}, "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec", 'text("tools.exec_command python -m unittest");', "", output)]))
        self.assertFalse(smoke.unittest_verified([("exec_command", {"cmd": 'python -m unittest -q || echo "Ran 1 test OK"'}, "", output + " FAILED")]))

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
