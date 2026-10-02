"""Native routing alternatives require complete authority, not absent markers."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from plugins.symphony.tests.test_native_routing_smoke import smoke


def at(second):
    return f'2026-10-01T00:00:{second:02}+00:00'


class NativeAssessedFirstTests(unittest.TestCase):
    def fixture(self, provider):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = home = Path(temporary.name)
        root, assessor_id, lead_id = 'root-id', 'assessor-id', 'lead-id'
        profile = 'latest' if provider == 'codex' else 'opus-5-5'
        model = smoke.snapshot_for(provider, profile).tiers['strongest']
        values = {'size': 'small', 'complexity': 'simple', 'risk': 'normal'}
        contract = {'version': 1, 'epoch': 'assessment-source', 'accepted_at': at(8)}
        assessment = {**values, 'substantive_contract': contract, 'route': {'profile': profile}}
        children = [{'identity': assessor_id, 'role': 'assessor', 'state': 'completed',
                     'requested_tier': model, 'requested_effort': 'high'},
                    {'identity': lead_id, 'role': 'lead', 'state': 'completed',
                     'requested_tier': model, 'requested_effort': 'high'}]
        run = {'provider': provider, 'session_id': root, 'run_id': 'run', 'lead_identity': lead_id,
               'delegations': children, 'assessment': assessment}
        document = {'event_history': [
            {'event_id': 'assessment-source:assessment:assessment_accepted', 'kind': 'assessment_accepted',
             'observed_at': at(8), 'payload': assessment},
            {'event_id': 'assessment-source:delegation:delegation_updated', 'kind': 'delegation_updated',
             'observed_at': at(8), 'payload': {'identity': assessor_id, 'state': 'completed'}}]}
        report = 'SYMPHONY_ASSESSMENT: ' + json.dumps(values)
        if provider == 'codex':
            def row(kind, payload, second):
                return {'type': kind, 'timestamp': at(second), 'payload': payload}
            task = 'symphony_assessor_' + smoke.re.sub(r'\W', '_', model) + '_high'
            root_rows = [row('session_meta', {'id': root, 'cwd': str(project), 'source': 'exec'}, 0),
                row('response_item', {'type': 'function_call', 'name': 'spawn_agent', 'call_id': 'spawn',
                    'arguments': json.dumps({'task_name': task, 'model': model, 'reasoning_effort': 'high',
                                            'fork_turns': 'none'})}, 1),
                row('event_msg', {'type': 'item_completed', 'thread_id': root, 'item': {
                    'type': 'SubAgentActivity', 'id': 'spawn', 'kind': 'started',
                    'agent_thread_id': assessor_id, 'agent_path': '/root/' + task}}, 2),
                row('response_item', {'type': 'function_call_output', 'call_id': 'spawn',
                                     'output': json.dumps({'task_name': '/root/' + task})}, 3)]
            assessor = [row('session_meta', {'id': assessor_id, 'cwd': str(project), 'agent_path': '/root/' + task,
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': root}}}}, 2),
                row('event_msg', {'type': 'task_started', 'turn_id': 'assessor-turn'}, 2),
                row('turn_context', {'turn_id': 'assessor-turn', 'model': model, 'effort': 'high'}, 3),
                row('event_msg', {'type': 'task_complete', 'turn_id': 'assessor-turn', 'last_agent_message': report}, 7)]
            lead = [row('session_meta', {'id': lead_id, 'cwd': str(project),
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': root}}}}, 10),
                row('event_msg', {'type': 'task_started', 'turn_id': 'lead-turn'}, 10)]
            path = home / 'sessions/2026/10/01' / (root + '.jsonl')
        else:
            label = f'symphony:symphony-assessor-{model}-high'
            root_rows = [{'type': 'assistant', 'sessionId': root, 'cwd': str(project), 'timestamp': at(1),
                'message': {'content': [{'type': 'tool_use', 'id': 'spawn', 'name': 'Agent',
                    'input': {'subagent_type': label, 'prompt': 'Assess the full objective.', 'model': model}}]}},
                {'type': 'user', 'sessionId': root, 'timestamp': at(3), 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'spawn', 'content': 'agentId: assessor-id (for resuming)'}]}}]
            assessor = [{'type': 'user', 'sessionId': root, 'agentId': assessor_id, 'isSidechain': True,
                'uuid': 'assessor-prompt', 'timestamp': at(2), 'message': {'content': 'Assess the full objective.'}},
                {'type': 'assistant', 'sessionId': root, 'agentId': assessor_id, 'isSidechain': True,
                 'uuid': 'assessor-terminal', 'timestamp': at(7), 'effort': 'high',
                 'message': {'role': 'assistant', 'model': model, 'stop_reason': 'end_turn',
                             'content': [{'type': 'text', 'text': report}]}}]
            lead = [{'type': 'user', 'sessionId': root, 'agentId': lead_id, 'isSidechain': True,
                     'timestamp': at(10), 'message': {'content': 'Perform assessed work.'}}]
            path = home / 'projects/project' / (root + '.jsonl')
            metas = path.with_suffix('') / 'subagents'
            metas.mkdir(parents=True)
            (metas / f'agent-{assessor_id}.meta.json').write_text(json.dumps(
                {'agentType': label, 'spawnDepth': 1, 'toolUseId': 'spawn'}))
            (metas / f'agent-{lead_id}.meta.json').write_text(json.dumps(
                {'agentType': f'symphony:symphony-lead-{model}-high', 'spawnDepth': 1, 'toolUseId': 'lead'}))
        path.parent.mkdir(parents=True, exist_ok=True)
        def write_root():
            path.write_text(''.join(json.dumps(row) + '\n' for row in root_rows))
        write_root()
        return document, run, {assessor_id: assessor, lead_id: lead}, home, project, profile, root_rows, write_root

    def test_exact_first_assessor_is_supported_for_both_providers(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                values = self.fixture(provider)
                self.assertTrue(smoke.assessed_first_verified(provider, *values[:6]))

    def test_failed_hidden_prior_launch_execution_and_scope_conflicts_stay_rejected(self):
        for provider in ('codex', 'claude'):
            for case in ('prior-failed-launch', 'prior-unreturned-launch', 'root-execution', 'duplicate-call',
                         'missing-result', 'failed-result', 'foreign-root', 'foreign-model', 'late-assessment',
                         'missing-epoch-history', 'wrong-assessor-source', 'changed-report', 'same-lead',
                         'intervening-root-execution', 'unavailable-report'):
                with self.subTest(provider=provider, case=case):
                    document, run, children, home, project, profile, roots, write = self.fixture(provider)
                    assessor = children['assessor-id']
                    if case.startswith('prior-') or case in {'root-execution', 'intervening-root-execution'}:
                        prior = deepcopy(roots[1 if provider == 'codex' else 0])
                        prior['timestamp'] = at(4) if case == 'intervening-root-execution' else at(0)
                        if provider == 'codex':
                            prior['payload']['call_id'] = 'prior'
                            if case in {'root-execution', 'intervening-root-execution'}: prior['payload'].update(name='exec_command', arguments='{"cmd":"do work"}')
                            else: prior['payload']['arguments'] = '{"task_name":"unknown"}'
                            if case == 'intervening-root-execution': roots.append(prior)
                            else: roots.insert(1, prior)
                        else:
                            prior['message']['content'][0]['id'] = 'prior'
                            if case in {'root-execution', 'intervening-root-execution'}: prior['message']['content'][0].update(name='Bash', input={'command': 'do work'})
                            else: prior['message']['content'][0]['input']['subagent_type'] = 'foreign'
                            if case == 'intervening-root-execution': roots.append(prior)
                            else: roots.insert(0, prior)
                    if case == 'duplicate-call': roots.insert(2 if provider == 'codex' else 1, deepcopy(roots[1 if provider == 'codex' else 0]))
                    if case == 'missing-result': roots.pop()
                    if case == 'failed-result':
                        if provider == 'codex': roots[-1]['payload']['output'] = 'Unknown model `foreign`'
                        else: roots[-1]['message']['content'][0]['is_error'] = True
                    if case == 'foreign-root':
                        if provider == 'codex': assessor[0]['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = 'foreign'
                        else: roots[0]['sessionId'] = 'foreign'
                    if case == 'foreign-model':
                        if provider == 'codex': assessor[2]['payload']['model'] = 'foreign'
                        else: assessor[-1]['message']['model'] = 'foreign'
                    if case == 'late-assessment': run['assessment']['substantive_contract']['accepted_at'] = at(12)
                    if case == 'missing-epoch-history': document['event_history'].pop(0)
                    if case == 'wrong-assessor-source': document['event_history'][1]['payload']['identity'] = 'foreign'
                    if case == 'changed-report':
                        if provider == 'codex': assessor[-1]['payload']['last_agent_message'] = 'No assessment.'
                        else: assessor[-1]['message']['content'][0]['text'] = 'No assessment.'
                    if case == 'same-lead': run['lead_identity'] = 'assessor-id'
                    if case == 'unavailable-report':
                        run['assessment']['substantive_contract']['accepted_at'] = at(4)
                        for event in document['event_history']: event['observed_at'] = at(4)
                    write()
                    self.assertFalse(smoke.assessed_first_verified(provider, document, run, children, home, project, profile))

    def test_codex_report_before_stop_hook_is_available_before_task_complete(self):
        document, run, children, home, project, profile, _, _ = self.fixture('codex')
        run['assessment']['substantive_contract']['accepted_at'] = at(6)
        for event in document['event_history']: event['observed_at'] = at(6)
        assessor = children['assessor-id']
        assessor.insert(-1, {'type': 'response_item', 'timestamp': at(5), 'payload': {
            'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{
                'type': 'output_text', 'text': assessor[-1]['payload']['last_agent_message']}]}})
        self.assertTrue(smoke.assessed_first_verified('codex', document, run, children, home, project, profile))

    def test_codex_native_wait_during_assessment_is_supported(self):
        document, run, children, home, project, profile, roots, write = self.fixture('codex')
        roots.append({'type': 'response_item', 'timestamp': at(4), 'payload': {
            'type': 'function_call', 'name': 'wait_agent', 'call_id': 'wait',
            'arguments': '{"timeout_ms":30000}'}})
        write()
        self.assertTrue(smoke.assessed_first_verified('codex', document, run, children, home, project, profile))

    def test_lead_verification_requires_successful_result_after_worker_completion(self):
        worker = {'identity': 'worker-id'}
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                if provider == 'codex':
                    def call(name, args, identity, second):
                        return {'type': 'response_item', 'timestamp': at(second), 'payload': {
                            'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': identity}}
                    def result(identity, output, second, failed=False):
                        return {'type': 'response_item', 'timestamp': at(second), 'payload': {
                            'type': 'function_call_output', 'call_id': identity, 'output': json.dumps(output)}}
                    launch = call('spawn_agent', {}, 'launch', 1)
                    launched = result('launch', {'task_name': '/root/lead/worker'}, 2)
                    children = {'worker-id': [
                        {'type': 'session_meta', 'payload': {'id': 'worker-id', 'agent_path': '/root/lead/worker'}},
                        {'type': 'event_msg', 'timestamp': at(7), 'payload': {'type': 'task_complete'}}]}
                    verify = call('exec_command', {'cmd': 'python -m unittest -q'}, 'test', 8)
                    checked = result('test', {'exit_code': 0, 'output': 'Ran 1 test in 0.01s\nOK'}, 9)
                else:
                    def call(name, args, identity, second):
                        return {'type': 'assistant', 'timestamp': at(second), 'message': {'content': [{
                            'type': 'tool_use', 'name': name, 'input': args, 'id': identity}]}}
                    def result(identity, output, second, failed=False):
                        return {'type': 'user', 'timestamp': at(second), 'message': {'content': [{
                            'type': 'tool_result', 'tool_use_id': identity, 'content': output, 'is_error': failed}]}}
                    launch = call('Agent', {}, 'launch', 1)
                    launched = result('launch', 'agentId: worker-id (for resuming)', 7)
                    children = {'worker-id': [{'type': 'assistant', 'timestamp': at(7),
                        'message': {'stop_reason': 'end_turn'}}]}
                    verify = call('Bash', {'command': 'python -m unittest -q'}, 'test', 8)
                    checked = result('test', 'Ran 1 test in 0.01s\nOK', 9)
                rows = [launch, launched, verify, checked]
                check = lambda values: smoke.lead_verifies_after_worker_returns(
                    provider, values, [worker], children, Path('/fixture'))
                self.assertTrue(check(rows))
                early = deepcopy(verify); early['timestamp'] = at(3)
                early_result = deepcopy(checked); early_result['timestamp'] = at(4)
                self.assertFalse(check([launch, launched, early, early_result]))
                if provider == 'codex':
                    failed = result('test', {'exit_code': 1, 'output': 'Ran 1 test in 0.01s\nOK'}, 9)
                else: failed = result('test', 'Ran 1 test in 0.01s\nOK', 9, True)
                self.assertFalse(check([launch, launched, verify, failed]))
                self.assertFalse(check(rows + [checked]))

    def test_resume_command_keeps_the_native_root_for_both_providers(self):
        for provider in ('codex', 'claude'):
            argv = smoke.native_cli_command(provider, provider, Path('/project'), 'exact-root', 'second task', 6, True)
            self.assertIn('exact-root', argv)
            self.assertEqual(argv[-1], 'second task')
            if provider == 'codex': self.assertEqual(argv[:3], ['codex', 'exec', 'resume'])
            else: self.assertEqual(argv[-3:], ['--resume', 'exact-root', 'second task'])

    def test_second_command_requires_distinct_run_child_receipts_and_first_archive(self):
        with TemporaryDirectory() as directory:
            greeting = Path(directory) / 'greet.py'
            greeting.write_text('unchanged')
            old = {'provider': 'claude', 'session_id': 'root', 'run_id': 'old', 'updated_at': at(5),
                   'delegations': [{'identity': 'old-child'}], 'status': 'completed'}
            new = {**old, 'run_id': 'new', 'started_at': at(6), 'updated_at': at(7),
                   'outcome': {'status': 'completed'}, 'delegations': [{'identity': 'new-child'}]}
            first = {'provider': 'claude', 'session': 'root', 'run_id': 'old', 'agent': 'old-child', 'result': 'old-result'}
            second = {**first, 'run_id': 'new', 'agent': 'new-child', 'result': 'new-result'}
            doc = {'recent_runs': [old, new], 'terminal_receipts': [first, second]}
            store = Mock()
            store.aliases_for_owner.return_value = []
            store.session_record.return_value = {'pending': [], 'overflow': False}
            self.assertEqual(smoke.fresh_command_run_verified(doc, old, 'claude', 'root', (first,),
                             store, greeting, smoke.fingerprint(greeting)), new)
            for case in ('same-run', 'same-child', 'same-result', 'before-archive', 'changed-old', 'pending'):
                changed = deepcopy(doc)
                store.session_record.return_value = {'pending': [], 'overflow': False}
                if case == 'same-run': changed['recent_runs'][1]['run_id'] = 'old'
                if case == 'same-child': changed['recent_runs'][1]['delegations'][0]['identity'] = 'old-child'
                if case == 'same-result': changed['terminal_receipts'][1]['result'] = 'old-result'
                if case == 'before-archive': changed['recent_runs'][1]['started_at'] = at(4)
                if case == 'changed-old': changed['recent_runs'][0]['status'] = 'abandoned'
                if case == 'pending': store.session_record.return_value = {'pending': [1]}
                with self.subTest(case=case), self.assertRaises(RuntimeError):
                    smoke.fresh_command_run_verified(changed, old, 'claude', 'root', (first,), store,
                                                     greeting, smoke.fingerprint(greeting))
