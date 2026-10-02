"""Historical worker replies retain old credit without granting new work."""

from dataclasses import replace
import json
import hashlib
import unittest

from plugins.symphony.symphony.host_evidence import (
    _claude_historical_worker_origin, _claude_historical_worker_terminal,
    _claude_historical_worker_deliveries,
    claude_archived_mixed_sendmessage_sequence,
    claude_native_parent_completion,
)
from plugins.symphony.symphony.model import Event, ProjectState
from plugins.symphony.symphony.runtime import _observe_delegation, _sendmessage_sequence_digest, event_from_payload, handle
from plugins.symphony.symphony.store import StateStore
from unittest.mock import patch
from plugins.symphony.tests import test_claude_sendmessage as fixture
from plugins.symphony.tests.test_claude_host_evidence import SESSION, LEAD

WORKER = 'c9c4054c282207da0'
TYPE = 'symphony:symphony-worker-claude-sonnet-5-low'


class HistoricalClaudeSendMessageTests(unittest.TestCase):
    setUp = fixture.ClaudeSendMessageTests.setUp
    write_native = fixture.ClaudeSendMessageTests.write_native
    write_root_prompt = fixture.ClaudeSendMessageTests.write_root_prompt
    queue = fixture.ClaudeSendMessageTests.queue

    def prepare(self, *, persist_start=False):
        fixture.ClaudeSendMessageTests.prepare(self)
        lead_rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        lead_rows.insert(1, {'type': 'assistant', 'sessionId': SESSION, 'agentId': LEAD,
            'isSidechain': True, 'cwd': str(self.project), 'timestamp': '2026-09-29T02:01:20Z',
            'message': {'model': 'claude-sonnet-5', 'stop_reason': 'tool_use', 'content': [{
                'type': 'tool_use', 'name': 'Agent', 'id': 'original-worker-launch',
                'input': {'subagent_type': TYPE, 'model': 'claude-sonnet-5', 'prompt': 'Original worker task.'}}]},
            'effort': 'low'})
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in lead_rows))
        self.worker = self.child.parent / f'agent-{WORKER}.jsonl'
        self.worker.with_suffix('.meta.json').write_text(json.dumps({'agentType': TYPE, 'spawnDepth': 2,
            'toolUseId': 'original-worker-launch'}))
        self.rows = [
            {'type': 'user', 'sessionId': SESSION, 'agentId': WORKER, 'isSidechain': True,
             'uuid': 'original-worker-prompt', 'timestamp': '2026-09-29T02:01:21Z',
             'message': {'content': 'Original worker task.'}},
            {'type': 'assistant', 'sessionId': SESSION, 'agentId': WORKER, 'isSidechain': True,
             'uuid': 'original-worker-terminal', 'timestamp': '2026-09-29T02:01:40Z', 'effort': 'low',
             'message': {'model': 'claude-sonnet-5', 'stop_reason': 'end_turn',
                         'content': [{'type': 'text', 'text': 'Original worker success.'}]}}]
        self.write_worker()
        run = replace(self.archived, status='active', outcome=None,
            assessment={**self.archived.assessment, 'topology': 'delegated', 'substantive_contract': {
                'version': 1, 'epoch': 'assessment-epoch', 'accepted_at': '2026-09-29T02:00:30+00:00'}})
        state = ProjectState(active_run=run)
        payload = {'provider': 'claude', 'session_id': SESSION, 'agent_id': WORKER,
            'agent_type': TYPE, 'role': 'worker', 'cwd': str(self.project), 'prompt_id': 'worker-context',
            'model': 'claude-sonnet-5', 'model_reasoning_effort': 'low'}
        state, _ = _observe_delegation(state, Event('worker-start', 'subagent_started',
            '2026-09-29T02:01:22+00:00', payload), self.environ)
        if persist_start:
            # A real assessed run already retained the assessor's terminal ID.
            state = replace(state, active_run=replace(state.active_run, assessment={
                **state.active_run.assessment, '_terminal_event_ids': ('a' * 64,)}))
            self.store.save(self.project, state)
            state = self.store.load(self.project)
        state, _ = _observe_delegation(state, Event('worker-stop', 'subagent_stopped',
            '2026-09-29T02:01:41+00:00', {**payload, 'status': 'completed',
            'last_assistant_message': 'Original worker success.'}), self.environ)
        self.original = replace(state.active_run, status='completed', outcome={'status': 'completed'},
                                updated_at=self.archived.updated_at)
        self.state = replace(state, active_run=None, active_runs={}, recent_runs=(self.original,))
        self.assertTrue(self.original.assessment['_substantive_children'][WORKER]['successful'])
        self.rows.extend([
            {**self.rows[0], 'uuid': 'worker-send-prompt', 'timestamp': '2026-09-29T02:04:11Z',
             'origin': {'kind': 'coordinator'}, 'isMeta': True,
             'message': {'content': fixture.coordinator_content('Acknowledge the original work only.')}},
            {**self.rows[1], 'uuid': 'worker-send-terminal', 'timestamp': '2026-09-29T02:04:40Z',
             'message': {'model': 'claude-sonnet-5', 'stop_reason': 'end_turn',
                         'content': [{'type': 'text', 'text': 'Original work remains complete.'}]}}])
        self.write_worker()

    def test_original_worker_survives_native_start_persistence(self):
        self.prepare(persist_start=True)
        receipts = [item for item in self.state.terminal_receipts if item.get('agent') == WORKER]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(sum(bool(item.get('result')) for item in receipts), 1)
        self.assertIsNotNone(_claude_historical_worker_origin(
            self.state, self.original, WORKER, self.project, self.environ))

    def write_worker(self):
        self.worker.write_text(''.join(json.dumps(row) + '\n' for row in self.rows))

    def test_null_worker_terminal_requires_exact_native_parent_delivery(self):
        from copy import deepcopy
        for case in ('valid', 'missing', 'origin', 'producer', 'parent', 'session', 'sidechain', 'not-meta',
                     'time', 'late', 'wrong-status', 'wrong-id', 'wrong-result', 'duplicate', 'interim-note',
                     'duplicate-tag', 'nested-result', 'nested-status', 'scalar-attribute', 'unexpected-text',
                     'missing-stop-reason', 'api-error', 'unfinished-tool'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                terminal = deepcopy(self.rows[1])
                terminal['message']['stop_reason'] = None
                text = 'Original worker success.'
                content = f'<task-notification><task-id>{WORKER}</task-id><status>completed</status><result>{text}</result></task-notification>'
                notification = {'type': 'user', 'sessionId': SESSION, 'agentId': LEAD, 'isSidechain': True,
                    'isMeta': True, 'uuid': 'native-parent-result', 'timestamp': '2026-09-29T02:01:40.5Z',
                    'origin': {'kind': 'task-notification', 'producer': 'session-task'}, 'message': {'content': content}}
                rows = [notification]
                if case == 'missing': rows = []
                if case == 'origin': notification.pop('origin')
                if case == 'producer': notification['origin']['producer'] = 'user'
                if case == 'parent': notification['agentId'] = 'foreign'
                if case == 'session': notification['sessionId'] = 'foreign'
                if case == 'sidechain': notification['isSidechain'] = False
                if case == 'not-meta': notification['isMeta'] = False
                if case == 'time': notification['timestamp'] = '2026-09-29T02:01:39Z'
                if case == 'late': notification['timestamp'] = self.rows[2]['timestamp']
                if case == 'wrong-status': notification['message']['content'] = content.replace('completed', 'failed')
                if case == 'wrong-id': notification['message']['content'] = content.replace(WORKER, 'foreign')
                if case == 'wrong-result': notification['message']['content'] = content.replace(text, 'Different report.')
                if case == 'duplicate': rows.append(deepcopy(notification))
                if case == 'interim-note': notification['message']['content'] = content.replace('</task-notification>', '<note>Background work remains.</note></task-notification>')
                if case == 'duplicate-tag': notification['message']['content'] = content.replace('</task-notification>', '<status>completed</status></task-notification>')
                if case == 'nested-result': notification['message']['content'] = content.replace(text, '<text>' + text + '</text>')
                if case == 'nested-status': notification['message']['content'] = content.replace('completed</status>', 'completed<note>Background work remains.</note></status>')
                if case == 'scalar-attribute': notification['message']['content'] = content.replace('<status>', '<status interim="true">')
                if case == 'unexpected-text': notification['message']['content'] = content.replace('</status>', '</status>Background work remains.')
                if case == 'missing-stop-reason': terminal['message'].pop('stop_reason')
                if case == 'api-error': terminal['isApiErrorMessage'] = True
                if case == 'unfinished-tool': terminal['message']['content'].append({'type': 'tool_use', 'name': 'Agent', 'id': 'unfinished'})
                before = self.rows[2]['timestamp']
                from plugins.symphony.symphony.host_evidence import _instant
                self.assertEqual(claude_native_parent_completion(rows, terminal, text, SESSION, LEAD, WORKER,
                                 before=_instant(before)), case == 'valid')
                worker_rows = [*self.rows[:1], terminal, *self.rows[2:]]
                result = _claude_historical_worker_terminal(worker_rows, 0, WORKER, SESSION,
                    'claude-sonnet-5', 'low', parent_rows=rows, parent=LEAD)
                self.assertEqual(result is not None, case == 'valid')

    def test_null_foreground_worker_requires_the_exact_native_agent_result(self):
        from copy import deepcopy
        from plugins.symphony.symphony.host_evidence import _instant
        for case in ('valid', 'root', 'no-hash', 'wrong-hash', 'missing-result', 'duplicate-call',
                     'duplicate-result', 'session', 'parent', 'sidechain', 'no-uuid', 'api-error',
                     'error-result', 'early-result', 'late-result', 'wrong-report', 'wrong-footer',
                     'async-ack', 'async-launch', 'wrong-prompt', 'foreign-prompt', 'wrong-tool'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                terminal = deepcopy(self.rows[1])
                terminal['message']['stop_reason'] = None
                text = 'Original worker success.'
                parent = LEAD if case != 'root' else ''
                call = {'type': 'assistant', 'sessionId': SESSION, 'agentId': parent,
                    'isSidechain': bool(parent), 'uuid': 'native-parent-call',
                    'timestamp': '2026-09-29T02:01:20Z', 'message': {'content': [{
                        'type': 'tool_use', 'name': 'Agent', 'id': 'original-worker-launch',
                        'input': {'prompt': self.rows[0]['message']['content']}}]}}
                footer = (f"agentId: {WORKER} (use SendMessage with to: '{WORKER}', "
                          "summary: '<5-10 word recap>' to continue this agent)\n"
                          "<usage>subagent_tokens: 10\ntool_uses: 0\nduration_ms: 120</usage>")
                delivery = {'type': 'user', 'sessionId': SESSION, 'agentId': parent,
                    'isSidechain': bool(parent), 'uuid': 'native-parent-result',
                    'timestamp': '2026-09-29T02:01:40.5Z', 'message': {'content': [{
                        'type': 'tool_result', 'tool_use_id': 'original-worker-launch',
                        'content': [{'type': 'text', 'text': text}, {'type': 'text', 'text': footer}]}]}}
                prompt = deepcopy(self.rows[0])
                rows = [call, delivery]
                launch_hash = hashlib.sha256(b'original-worker-launch').hexdigest()
                if case == 'no-hash': launch_hash = ''
                if case == 'wrong-hash': launch_hash = 'b' * 64
                if case == 'missing-result': rows.pop()
                if case == 'duplicate-call': rows.append(deepcopy(call))
                if case == 'duplicate-result': rows.append(deepcopy(delivery))
                if case == 'session': delivery['sessionId'] = 'foreign'
                if case == 'parent': delivery['agentId'] = 'foreign'
                if case == 'sidechain': delivery['isSidechain'] = False
                if case == 'no-uuid': delivery.pop('uuid')
                if case == 'api-error': delivery['isApiErrorMessage'] = True
                if case == 'error-result': delivery['message']['content'][0]['is_error'] = True
                if case == 'early-result': delivery['timestamp'] = '2026-09-29T02:01:39Z'
                if case == 'late-result': delivery['timestamp'] = self.rows[2]['timestamp']
                if case == 'wrong-report': delivery['message']['content'][0]['content'][0]['text'] = 'Different result.'
                if case == 'wrong-footer': delivery['message']['content'][0]['content'][1]['text'] = footer.replace(WORKER, 'foreign')
                if case == 'async-ack': delivery['message']['content'][0]['content'] = [{'type': 'text', 'text': 'Async agent launched successfully.\n' + footer}]
                if case == 'async-launch': call['message']['content'][0]['input']['run_in_background'] = True
                if case == 'wrong-prompt': call['message']['content'][0]['input']['prompt'] = 'Different task.'
                if case == 'foreign-prompt': prompt['agentId'] = 'foreign'
                if case == 'wrong-tool': call['message']['content'][0]['name'] = 'SendMessage'
                expected = case in {'valid', 'root'}
                self.assertEqual(claude_native_parent_completion(rows, terminal, text, SESSION, parent, WORKER,
                    before=_instant(self.rows[2]['timestamp']), prompt=prompt, launch_hash=launch_hash), expected)
                worker_rows = [prompt, terminal, *self.rows[2:]]
                self.assertEqual(_claude_historical_worker_terminal(worker_rows, 0, WORKER, SESSION,
                    'claude-sonnet-5', 'low', parent_rows=rows, parent=parent,
                    launch_hash=launch_hash) is not None, expected)

    def prepare_send(self, *, persist_start=False):
        self.prepare(persist_start=persist_start)
        parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
        parent[-2:-2] = [
            {'type': 'assistant', 'sessionId': SESSION, 'cwd': str(self.project),
             'timestamp': '2026-09-29T02:04:10Z', 'message': {'content': [{
                'type': 'tool_use', 'name': 'SendMessage', 'id': 'worker-send',
                'input': {'to': WORKER, 'message': 'Acknowledge the original work only.'}}]}},
            {'type': 'user', 'sessionId': SESSION, 'timestamp': '2026-09-29T02:04:12Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'worker-send',
                'content': json.dumps({'success': True, 'resumedAgentId': WORKER})}]}}]
        self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
        payload = {'provider': 'claude', 'session_id': SESSION, 'agent_id': WORKER,
                   'agent_type': TYPE, 'prompt_id': 'new-worker-context', 'role': 'worker'}
        self.worker_events = (Event('later-worker-start', 'subagent_started',
            '2026-09-29T02:04:11.1+00:00', payload), Event('later-worker-stop', 'subagent_stopped',
            '2026-09-29T02:04:40.1+00:00', {**payload, 'status': 'completed',
            'last_assistant_message': 'Original work remains complete.'}))
        return parent

    def test_exact_worker_delivery_and_callback_proof_changes_no_credit(self):
        self.prepare_send()
        proof = _claude_historical_worker_deliveries(self.state, self.original, self.worker_events,
                                                    self.project, self.environ)
        self.assertIsNotNone(proof)
        self.assertEqual(proof[2], {'later-worker-start': 0, 'later-worker-stop': 0})
        self.assertEqual(proof[3][0]['call_hash'], hashlib.sha256(b'worker-send').hexdigest())
        self.assertEqual(proof[3][0]['agent'], WORKER)
        self.assertEqual(self.state.recent_runs, (self.original,))
        self.assertEqual(len(self.state.terminal_receipts), 1)

    def test_superseded_lead_ack_preserves_historical_worker_credit(self):
        self.prepare_send(persist_start=True)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        next(row for row in rows if row.get('uuid') == 'terminal-send-0')['message']['stop_reason'] = None
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.store.save(self.project, self.state)
        self.queue(self.worker_events)
        before = self.store.load(self.project)
        result = handle({'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}, self.environ)
        self.assertNotIn('"decision": "block"', result.stdout)
        after = self.store.load(self.project)
        self.assertEqual(after.recent_runs[0].assessment['_substantive_children'],
                         before.recent_runs[0].assessment['_substantive_children'])
        self.assertEqual(tuple(receipt for receipt in after.terminal_receipts if receipt.get('agent') == WORKER),
                         tuple(receipt for receipt in before.terminal_receipts if receipt.get('agent') == WORKER))
        self.assertEqual(sum(receipt.get('agent') == LEAD for receipt in after.terminal_receipts),
                         1 + sum(receipt.get('agent') == LEAD for receipt in before.terminal_receipts))
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_historical_ack_rejects_native_errors_new_tools_and_orphan_results(self):
        for case in ('original-error', 'terminal-error', 'earlier-error', 'Agent', 'SendMessage',
                     'Write', 'Edit', 'Bash', 'Read', 'orphan-result'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare_send()
                if case == 'original-error': self.rows[1]['isApiErrorMessage'] = True
                elif case == 'terminal-error': self.rows[-1]['isApiErrorMessage'] = True
                elif case == 'earlier-error': self.rows.insert(-1, {**self.rows[-1],
                    'uuid': 'api-error', 'timestamp': '2026-09-29T02:04:20Z', 'isApiErrorMessage': True,
                    'message': {'model': 'claude-sonnet-5', 'stop_reason': None, 'content': []}})
                elif case == 'orphan-result': self.rows.insert(-1, {**self.rows[-2],
                    'uuid': 'orphan-result', 'timestamp': '2026-09-29T02:04:20Z',
                    'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'foreign', 'content': 'OK'}]}})
                else: self.rows.insert(-1, {**self.rows[-1], 'uuid': 'unproven-tool',
                    'timestamp': '2026-09-29T02:04:20Z', 'message': {'model': 'claude-sonnet-5',
                        'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': case,
                            'id': 'unproven-tool', 'input': {}}]}})
                self.write_worker()
                self.assertIsNone(claude_archived_mixed_sendmessage_sequence(self.state,
                    tuple(sorted((*self.events, *self.worker_events), key=lambda event: event.observed_at)),
                    SESSION, self.project, self.environ))

    def test_mixed_public_transaction_preserves_original_credit_and_worker_receipt(self):
        for hook in ('Stop', 'SessionStart', 'UserPromptSubmit'):
            with self.subTest(hook=hook):
                self.setUp()
                self.prepare_send(persist_start=True)
                sources = tuple(sorted((*self.events, *self.worker_events), key=lambda event: event.observed_at))
                self.assertIsNotNone(claude_archived_mixed_sendmessage_sequence(
                    self.state, sources, SESSION, self.project, self.environ))
                self.store.save(self.project, self.state)
                self.queue(self.worker_events)
                original_receipts = tuple(receipt for receipt in self.store.load(self.project).terminal_receipts
                                          if receipt.get('agent') == WORKER)
                result = handle({'hook_event_name': hook, 'cwd': str(self.project), 'session_id': SESSION,
                                 'prompt': '/symphony:status'}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                handle({'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}, self.environ)
                after = self.store.load(self.project)
                self.assertIsNone(after.active_run)
                run = after.recent_runs[0]
                self.assertEqual(run.assessment['_substantive_children'], self.original.assessment['_substantive_children'])
                self.assertEqual(next(item for item in run.delegations if item.identity == WORKER),
                                 next(item for item in self.original.delegations if item.identity == WORKER))
                self.assertEqual(tuple(receipt for receipt in after.terminal_receipts if receipt.get('agent') == WORKER),
                                 original_receipts)
                self.assertEqual(len(run.assessment['_claude_historical_sendmessage_dispositions']), 2)
                self.assertEqual(run.outcome, {'status': 'completed'})
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_delayed_lead_stop_across_proved_worker_send_preserves_replay_and_credit(self):
        for mode in ('original', 'partial-ack', 'superseded', 'late-witness'):
            with self.subTest(mode=mode):
                self.setUp()
                self.prepare_send(persist_start=True)
                if mode == 'superseded':
                    fixture.ClaudeSendMessageTests.supersede_intermediate(self)
                # The native lead ended at 02:04:00. A worker send at 02:04:10
                # races with its delayed Stop; the next lead send is 02:05:00.
                delayed = replace(self.events[1], observed_at='2026-09-29T02:04:10.05+00:00')
                if mode == 'late-witness':
                    delayed = replace(delayed, payload={**delayed.payload, 'turn_id': 'prompt-send-0'})
                self.events[1] = delayed
                sources = tuple(sorted((*self.events, *self.worker_events), key=lambda event: event.observed_at))
                self.assertIsNotNone(claude_archived_mixed_sendmessage_sequence(
                    self.state, sources, SESSION, self.project, self.environ))
                self.store.save(self.project, self.state)
                self.store.finish_session_events(self.store.session_record('claude', SESSION),
                                                {source.event_id for source in self.events})
                self.queue(tuple(source for source in sources
                                 if mode != 'late-witness' or source.event_id != delayed.event_id))
                original_receipts = tuple(item for item in self.store.load(self.project).terminal_receipts
                                          if item.get('agent') == WORKER)
                payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
                if mode == 'partial-ack':
                    with patch.object(StateStore, 'finish_session_events', side_effect=OSError('ACK crash')):
                        with self.assertRaises(OSError):
                            handle(payload, self.environ)
                    self.store.finish_session_events(self.store.session_record('claude', SESSION),
                                                    {self.events[0].event_id, self.worker_events[0].event_id})
                else:
                    self.assertNotIn('"decision": "block"', handle(payload, self.environ).stdout)
                if mode == 'late-witness':
                    self.queue((delayed,))
                self.assertNotIn('"decision": "block"', handle(payload, self.environ).stdout)
                before_retry = self.store.load(self.project)
                self.queue((delayed,))
                self.assertNotIn('"decision": "block"', handle(payload, self.environ).stdout)
                after = self.store.load(self.project)
                self.assertEqual(after.recent_runs, before_retry.recent_runs)
                self.assertEqual(after.terminal_receipts, before_retry.terminal_receipts)
                self.assertIsNone(after.active_run)
                self.assertEqual(after.recent_runs[0].outcome, {'status': 'completed'})
                self.assertEqual(after.recent_runs[0].assessment['_substantive_children'],
                                 self.original.assessment['_substantive_children'])
                self.assertEqual(tuple(item for item in after.terminal_receipts if item.get('agent') == WORKER),
                                 original_receipts)
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_delayed_lead_stop_keeps_owner_same_lead_and_global_delivery_bounds(self):
        for case in ('parent', 'session', 'identity', 'turn', 'report', 'same-lead-boundary',
                     'start-across-worker', 'ack-across-worker', 'worker-stop-across-lead', 'new-owner'):
            with self.subTest(case=case):
                self.setUp()
                parent = self.prepare_send()
                self.events[1] = replace(self.events[1], observed_at='2026-09-29T02:04:10.05+00:00')
                key = {'parent': 'parent_thread_id', 'session': 'session_id', 'identity': 'agent_id',
                       'turn': 'turn_id', 'report': 'last_assistant_message'}.get(case)
                if key:
                    self.events[1] = replace(self.events[1], payload={**self.events[1].payload, key: 'foreign'})
                if case == 'same-lead-boundary':
                    self.events[1] = replace(self.events[1], observed_at='2026-09-29T02:05:00+00:00')
                if case == 'start-across-worker':
                    self.events[0] = replace(self.events[0], observed_at='2026-09-29T02:04:10.01+00:00')
                if case == 'ack-across-worker':
                    next(row for row in parent if row.get('type') == 'user'
                         and isinstance(row.get('message', {}).get('content'), list)
                         and row.get('message', {}).get('content', [{}])[0].get('tool_use_id') == 'send-0')[
                             'timestamp'] = '2026-09-29T02:04:10.01Z'
                    self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
                if case == 'worker-stop-across-lead':
                    self.worker_events = (*self.worker_events[:1], replace(self.worker_events[-1],
                        observed_at='2026-09-29T02:05:00+00:00'))
                if case == 'new-owner':
                    newer = replace(self.run, run_id='new-owner', lead_identity='new-lead', owner_generation=8)
                    self.state = replace(self.state, active_run=newer, active_runs={f'claude:{SESSION}': newer})
                self.assertIsNone(claude_archived_mixed_sendmessage_sequence(self.state,
                    tuple(sorted((*self.events, *self.worker_events), key=lambda event: event.observed_at)),
                    SESSION, self.project, self.environ))

    def commit_without_source(self, target, kind):
        self.prepare_send()
        all_sources = (*self.events, *self.worker_events)
        late = next(source for source in all_sources if source.kind == kind and
                    source.payload['agent_id'] == (WORKER if target == 'worker' else LEAD))
        if kind == 'subagent_stopped':
            native_turn = 'worker-send-prompt' if target == 'worker' else 'prompt-send-0'
            late = replace(late, payload={**late.payload, 'turn_id': native_turn})
        self.store.save(self.project, self.state)
        record = self.store.session_record('claude', SESSION)
        record['pending'] = []
        self.store._session_path('claude', SESSION).write_text(json.dumps(record))
        self.queue(tuple(source for source in all_sources if source.event_id != late.event_id))
        payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
        self.assertNotIn('"decision": "block"', handle(payload, self.environ).stdout)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])
        self.assertIsNone(self.store.load(self.project).active_run)
        return late, payload

    def test_first_arriving_late_start_and_stop_only_add_ack_witness(self):
        for target in ('worker', 'lead'):
            for kind in ('subagent_started', 'subagent_stopped'):
                for hook in ('Stop', 'SessionStart', 'UserPromptSubmit'):
                    with self.subTest(target=target, kind=kind, hook=hook):
                        self.setUp()
                        late, payload = self.commit_without_source(target, kind)
                        before = self.store.load(self.project)
                        self.queue((late,))
                        result = handle({**payload, 'hook_event_name': hook,
                                         'prompt': '/symphony:status'}, self.environ)
                        self.assertNotIn('"decision": "block"', result.stdout)
                        after = self.store.load(self.project)
                        old, new = before.recent_runs[0], after.recent_runs[0]
                        assessment = dict(new.assessment)
                        witnesses = assessment.pop('_claude_sendmessage_late_sources')
                        self.assertEqual(len(witnesses), 1)
                        self.assertEqual(witnesses[0]['event_id'], late.event_id)
                        self.assertEqual(witnesses[0]['observed_at'], late.observed_at)
                        self.assertEqual(replace(new, assessment=assessment), old)
                        self.assertEqual(after.terminal_receipts, before.terminal_receipts)
                        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])
                        self.queue((late,))
                        handle(payload, self.environ)
                        self.assertEqual(self.store.load(self.project).recent_runs, after.recent_runs)

    def test_late_callback_commit_before_ack_preserves_new_and_sibling_owners(self):
        for mode in ('failed-ack', 'new-owner', 'same-run-owner', 'old-codec', 'sibling'):
            with self.subTest(mode=mode):
                self.setUp()
                late, payload = self.commit_without_source('worker', 'subagent_started')
                state = self.store.load(self.project)
                if mode == 'new-owner':
                    newer = replace(self.run, run_id='new-owner', lead_identity='e9c4054c282207da0',
                                    started_at='2026-09-29T02:10:00+00:00')
                    state = replace(state, active_run=newer, active_runs={f'claude:{SESSION}': newer})
                if mode == 'same-run-owner':
                    from plugins.symphony.symphony.model import Delegation
                    old = state.recent_runs[0]
                    newer = replace(old, status='active', outcome=None, owner_generation=8,
                        lead_identity='e9c4054c282207da0', assessment={**old.assessment,
                            'substantive_contract': {'version': 1, 'epoch': 'new-epoch',
                                'accepted_at': '2026-09-29T02:10:00+00:00'}, '_substantive_children': {}},
                        delegations=(*old.delegations, Delegation('e9c4054c282207da0', 'lead', '', 'working',
                            requested_tier='claude-opus-4-6', requested_effort='high')))
                    state = replace(state, active_run=newer, active_runs={f'claude:{SESSION}': newer}, recent_runs=())
                if mode == 'sibling':
                    sibling = replace(self.run, run_id='sibling', session_id='sibling', lead_identity='sibling-lead')
                    state = replace(state, active_run=sibling, active_runs={'claude:sibling': sibling})
                self.store.save(self.project, state)
                self.queue((late,))
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('late ACK crash')):
                    with self.assertRaises(OSError):
                        handle({**payload, 'hook_event_name': 'SessionStart'}, self.environ)
                committed = self.store.load(self.project)
                if mode == 'old-codec':
                    from plugins.symphony.tests.test_assessed_contract import AssessedContractTests
                    old = AssessedContractTests.released_store_160(self.store.root)
                    old.save(self.project, old.load(self.project))
                    committed = self.store.load(self.project)
                handle({**payload, 'hook_event_name': 'SessionStart'}, self.environ)
                after = self.store.load(self.project)
                self.assertEqual(committed.recent_runs, after.recent_runs)
                self.assertEqual(committed.terminal_receipts, after.terminal_receipts)
                self.assertEqual({key: replace(run, owner_seen_at=None, updated_at='')
                                  for key, run in committed.active_runs.items()},
                                 {key: replace(run, owner_seen_at=None, updated_at='')
                                  for key, run in after.active_runs.items()})
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_unproven_late_callback_or_corrupted_witness_holds_whole_batch(self):
        for case in ('wrong-report', 'explicit-turn', 'wrong-role', 'known-id', 'missing-native',
                     'missing-receipt', 'generation', 'ambiguous-sequence', 'corrupt-late', 'unknown-batch',
                     'context-only-terminal'):
            with self.subTest(case=case):
                self.setUp()
                late, payload = self.commit_without_source('worker', 'subagent_stopped')
                state = self.store.load(self.project)
                if case == 'context-only-terminal': late = replace(late, payload={key: value for key, value in late.payload.items() if key != 'turn_id'})
                if case == 'wrong-report': late = replace(late, payload={**late.payload, 'last_assistant_message': 'foreign'})
                if case == 'explicit-turn': late = replace(late, payload={**late.payload, 'turn_id': 'foreign'})
                if case == 'wrong-role': late = replace(late, payload={**late.payload, 'task': 'SYMPHONY_ROLE: lead'})
                if case == 'known-id': late = replace(late, event_id=self.worker_events[0].event_id)
                if case == 'missing-native': self.parent.unlink()
                if case == 'missing-receipt': state = replace(state, terminal_receipts=state.terminal_receipts[:-1])
                if case == 'ambiguous-sequence':
                    old = state.recent_runs[0]
                    sequence = old.assessment['_claude_sendmessage_sequences'][0]
                    state = replace(state, recent_runs=(replace(old, assessment={**old.assessment,
                        '_claude_sendmessage_sequences': (sequence, sequence)}),))
                self.store.save(self.project, state)
                self.queue((late,))
                if case == 'generation':
                    record = self.store.session_record('claude', SESSION)
                    record['pending'][0]['generation'] += 1
                    self.store._session_path('claude', SESSION).write_text(json.dumps(record))
                if case == 'unknown-batch': self.queue((replace(late, event_id='unknown',
                                                             payload={**late.payload, 'agent_id': 'foreign'}),))
                if case == 'corrupt-late':
                    with patch.object(StateStore, 'finish_session_events', side_effect=OSError('late ACK crash')):
                        with self.assertRaises(OSError): handle(payload, self.environ)
                    state = self.store.load(self.project)
                    old = state.recent_runs[0]
                    witness = {**old.assessment['_claude_sendmessage_late_sources'][0], 'result': 'foreign'}
                    self.store.save(self.project, replace(state, recent_runs=(replace(old, assessment={**old.assessment,
                        '_claude_sendmessage_late_sources': (witness,)}),)))
                before = self.store.load(self.project)
                pending = self.store.session_record('claude', SESSION)['pending']
                self.assertIn('"decision": "block"', handle(payload, self.environ).stdout)
                self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(pending, self.store.session_record('claude', SESSION)['pending'])

    def test_unflushed_new_start_and_terminal_are_not_old_late_sources(self):
        for case in ('start-after-terminal', 'start-after-final-terminal', 'start-after-commit', 'terminal-after-commit',
                     'context-only-terminal', 'legacy-no-bound', 'malformed-bound'):
            with self.subTest(case=case):
                self.setUp()
                late, payload = self.commit_without_source('lead', 'subagent_started')
                state = self.store.load(self.project)
                old = state.recent_runs[0]
                anchor = old.assessment['_claude_sendmessage_sequences'][0]
                self.assertTrue(anchor['committed_at'])
                if case == 'start-after-terminal':
                    late = replace(late, observed_at='2026-09-29T02:04:00.1+00:00')
                if case == 'start-after-final-terminal':
                    late = replace(self.events[-2], event_id='unflushed-new-start',
                        observed_at='2026-09-29T02:08:00+00:00',
                        payload={**self.events[-2].payload, 'prompt_id': 'genuinely-new-context'})
                if case == 'start-after-commit':
                    late = replace(late, observed_at='2999-01-01T00:00:00+00:00')
                if case in {'terminal-after-commit', 'context-only-terminal'}:
                    late = replace(self.events[-1], event_id='unseen-terminal',
                        observed_at='2999-01-01T00:00:00+00:00' if case == 'terminal-after-commit'
                        else self.events[-1].observed_at,
                        payload={**self.events[-1].payload, **({'turn_id': 'prompt-send-1'}
                                 if case == 'terminal-after-commit' else {})})
                if case in {'legacy-no-bound', 'malformed-bound'}:
                    changed = dict(anchor)
                    if case == 'legacy-no-bound': changed.pop('committed_at')
                    else: changed['committed_at'] = 'malformed'
                    # Retain exact worker dispositions with the changed anchor digest.
                    digest = _sendmessage_sequence_digest
                    assessment = {**old.assessment, '_claude_sendmessage_sequences': (changed,),
                        '_claude_historical_sendmessage_dispositions': tuple({**item, 'sequence_hash': digest(changed)}
                            for item in old.assessment['_claude_historical_sendmessage_dispositions'])}
                    state = replace(state, recent_runs=(replace(old, assessment=assessment),))
                    self.store.save(self.project, state)
                self.queue((late,))
                before = self.store.load(self.project)
                self.assertIn('"decision": "block"', handle(payload, self.environ).stdout)
                self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(len(self.store.session_record('claude', SESSION)['pending']), 1)

    def test_original_commit_time_is_immutable_and_known_legacy_retries_remain_exact(self):
        self.prepare_send()
        self.store.save(self.project, self.state)
        self.queue(self.worker_events)
        payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
        original_normalizer = event_from_payload
        trigger_time = '2026-09-29T02:06:01+00:00'
        with patch('plugins.symphony.symphony.runtime.event_from_payload', side_effect=lambda provider, source:
                   replace(original_normalizer(provider, source), observed_at=trigger_time)):
            handle(payload, self.environ)
        state = self.store.load(self.project)
        old = state.recent_runs[0]
        sequence = old.assessment['_claude_sendmessage_sequences'][0]
        self.assertEqual(sequence['committed_at'], trigger_time)
        self.queue((self.events[-1],))
        handle({**payload, 'hook_event_name': 'SessionStart'}, self.environ)
        self.assertEqual(self.store.load(self.project).recent_runs[0].assessment[
            '_claude_sendmessage_sequences'][0], sequence)
        legacy = dict(sequence)
        legacy.pop('committed_at')
        assessment = {**old.assessment, '_claude_sendmessage_sequences': (legacy,),
            '_claude_historical_sendmessage_dispositions': tuple({**item,
                'sequence_hash': _sendmessage_sequence_digest(legacy)}
                for item in old.assessment['_claude_historical_sendmessage_dispositions'])}
        self.store.save(self.project, replace(state, recent_runs=(replace(old, assessment=assessment),)))
        expected = self.store.load(self.project).recent_runs[0].assessment
        self.queue((self.events[-1], self.worker_events[-1]))
        self.assertNotIn('"decision": "block"', handle(payload, self.environ).stdout)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])
        self.assertEqual(self.store.load(self.project).recent_runs[0].assessment, expected)

    def test_new_native_lead_turn_cannot_be_disposed_as_an_old_late_report(self):
        self.prepare_send()
        self.store.save(self.project, self.state)
        self.queue(self.worker_events)
        payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
        handle(payload, self.environ)
        parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
        parent.append({**parent[-2], 'timestamp': '2026-09-29T02:07:00Z', 'message': {'content': [{
            'type': 'tool_use', 'name': 'SendMessage', 'id': 'genuinely-new',
            'input': {'to': LEAD, 'message': 'New uncommitted work.'}}]}})
        self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.append({**rows[-2], 'uuid': 'new-native-prompt', 'timestamp': '2026-09-29T02:07:01Z',
                     'message': {'content': fixture.coordinator_content('New uncommitted work.')}})
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        source = replace(self.events[-1], event_id='new-native-callback',
                         observed_at='2026-09-29T02:08:00+00:00')
        before = self.store.load(self.project)
        self.queue((source,))
        self.assertIn('"decision": "block"', handle(payload, self.environ).stdout)
        self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
        self.assertEqual(len(self.store.session_record('claude', SESSION)['pending']), 1)

    def test_mixed_ack_crash_partial_ack_and_new_owner_are_ack_only(self):
        for mode in ('failed', 'partial', 'new-owner', 'same-run-owner', 'old-codec'):
            with self.subTest(mode=mode):
                self.setUp()
                self.prepare_send()
                self.store.save(self.project, self.state)
                self.queue(self.worker_events)
                payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('ACK crash')):
                    with self.assertRaises(OSError):
                        handle(payload, self.environ)
                committed = self.store.load(self.project)
                if mode == 'partial':
                    self.store.finish_session_events(self.store.session_record('claude', SESSION),
                        {self.worker_events[0].event_id, self.events[-1].event_id})
                if mode == 'new-owner':
                    newer = replace(self.run, run_id='new-owner', lead_identity='e9c4054c282207da0',
                                    started_at='2026-09-29T02:10:00+00:00')
                    committed = replace(committed, active_run=newer, active_runs={f'claude:{SESSION}': newer})
                    self.store.save(self.project, committed)
                if mode == 'same-run-owner':
                    from plugins.symphony.symphony.model import Delegation
                    old_run = committed.recent_runs[0]
                    newer = replace(old_run, status='active', outcome=None, owner_generation=8,
                        lead_identity='e9c4054c282207da0', assessment={**old_run.assessment,
                            'substantive_contract': {'version': 1, 'epoch': 'new-epoch',
                                'accepted_at': '2026-09-29T02:10:00+00:00'}, '_substantive_children': {}},
                        delegations=(*old_run.delegations, Delegation('e9c4054c282207da0', 'lead', '', 'working',
                            requested_tier='claude-opus-4-6', requested_effort='high')))
                    committed = replace(committed, active_run=newer, active_runs={f'claude:{SESSION}': newer},
                                        recent_runs=())
                    self.store.save(self.project, committed)
                if mode == 'old-codec':
                    from plugins.symphony.tests.test_assessed_contract import AssessedContractTests
                    old = AssessedContractTests.released_store_160(self.store.root)
                    old.save(self.project, old.load(self.project))
                before = self.store.load(self.project)
                handle({**payload, 'hook_event_name': 'SessionStart'}, self.environ)
                after = self.store.load(self.project)
                self.assertEqual(before.recent_runs, after.recent_runs)
                self.assertEqual(tuple(item for item in before.terminal_receipts if item.get('turn') and item.get('result')),
                                 tuple(item for item in after.terminal_receipts if item.get('turn') and item.get('result')))
                self.assertEqual(replace(after.active_run, owner_seen_at=before.active_run.owner_seen_at,
                                         updated_at=before.active_run.updated_at)
                                 if before.active_run else after.active_run, before.active_run)
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_mixed_order_and_unknown_callback_hold_every_event_before_mutation(self):
        for case in ('preceding-lead-overlap', 'preceding-lead-result-overlap', 'worker-last', 'unknown-callback'):
            with self.subTest(case=case):
                self.setUp()
                parent = self.prepare_send()
                if case == 'preceding-lead-overlap':
                    child = [json.loads(line) for line in self.child.read_text().splitlines()]
                    child[-3]['timestamp'] = '2026-09-29T02:04:20Z'
                    self.child.write_text(''.join(json.dumps(row) + '\n' for row in child))
                if case == 'preceding-lead-result-overlap':
                    parent[-5]['timestamp'] = '2026-09-29T02:04:20Z'
                if case == 'worker-last':
                    parent[-4]['timestamp'] = '2026-09-29T02:07:10Z'
                    parent[-3]['timestamp'] = '2026-09-29T02:07:12Z'
                    parent = [*parent[:-4], *parent[-2:], *parent[-4:-2]]
                    self.rows[-2]['timestamp'] = '2026-09-29T02:07:11Z'
                    self.rows[-1]['timestamp'] = '2026-09-29T02:07:40Z'
                    self.write_worker()
                self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
                self.store.save(self.project, self.state)
                self.queue(self.worker_events)
                if case == 'unknown-callback':
                    self.queue((replace(self.worker_events[-1], event_id='unknown', payload={
                        **self.worker_events[-1].payload, 'agent_id': 'foreign'}),))
                before = self.store.load(self.project)
                pending = self.store.session_record('claude', SESSION)['pending']
                result = handle({'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}, self.environ)
                after = self.store.load(self.project)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual(before.recent_runs, after.recent_runs)
                self.assertEqual(tuple(item for item in before.terminal_receipts if item.get('turn') and item.get('result')),
                                 tuple(item for item in after.terminal_receipts if item.get('turn') and item.get('result')))
                self.assertEqual(pending, self.store.session_record('claude', SESSION)['pending'])

    def test_multiple_workers_are_persisted_in_native_global_call_order(self):
        parent = self.prepare_send()
        second = 'a8c4054c282207da0'  # Lexically first, delivered second.
        child = self.worker.parent / f'agent-{second}.jsonl'
        metadata = {'agentType': TYPE, 'spawnDepth': 2, 'toolUseId': 'second-worker-launch'}
        child.with_suffix('.meta.json').write_text(json.dumps(metadata))
        rows = [{**row, 'agentId': second, 'uuid': row['uuid'] + '-second'} for row in self.rows[:2]]
        rows[0]['timestamp'], rows[1]['timestamp'] = '2026-09-29T02:01:26Z', '2026-09-29T02:01:45Z'
        child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        lead = [json.loads(line) for line in self.child.read_text().splitlines()]
        launch = json.loads(json.dumps(lead[1]))
        launch['timestamp'] = '2026-09-29T02:01:25Z'
        launch['message']['content'][0]['id'] = 'second-worker-launch'
        lead.insert(2, launch)
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in lead))
        state = replace(self.state, active_run=replace(self.original, status='active', outcome=None), recent_runs=())
        payload = {'provider': 'claude', 'session_id': SESSION, 'agent_id': second, 'agent_type': TYPE,
                   'role': 'worker', 'cwd': str(self.project), 'prompt_id': 'second-original-context'}
        state, _ = _observe_delegation(state, Event('second-worker-start', 'subagent_started',
            '2026-09-29T02:01:27+00:00', payload), self.environ)
        state, _ = _observe_delegation(state, Event('second-worker-stop', 'subagent_stopped',
            '2026-09-29T02:01:46+00:00', {**payload, 'status': 'completed',
            'last_assistant_message': 'Original worker success.'}), self.environ)
        self.original = replace(state.active_run, status='completed', outcome={'status': 'completed'},
                                updated_at=self.original.updated_at)
        self.state = replace(state, active_run=None, recent_runs=(self.original,))
        self.assertTrue(self.original.assessment['_substantive_children'][second]['successful'])
        rows.extend([{**rows[0], 'uuid': 'second-later-prompt', 'timestamp': '2026-09-29T02:04:46Z',
                      'origin': {'kind': 'coordinator'}, 'isMeta': True,
                      'message': {'content': fixture.coordinator_content('Second worker acknowledgment.')}},
                     {**rows[1], 'uuid': 'second-later-terminal', 'timestamp': '2026-09-29T02:04:55Z',
                      'message': {'model': 'claude-sonnet-5', 'stop_reason': 'end_turn',
                                  'content': [{'type': 'text', 'text': 'Second worker acknowledged.'}]}}])
        child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        parent[-2:-2] = [
            {'type': 'assistant', 'sessionId': SESSION, 'cwd': str(self.project),
             'timestamp': '2026-09-29T02:04:45Z', 'message': {'content': [{
                'type': 'tool_use', 'name': 'SendMessage', 'id': 'second-worker-send',
                'input': {'to': second, 'message': 'Second worker acknowledgment.'}}]}},
            {'type': 'user', 'sessionId': SESSION, 'timestamp': '2026-09-29T02:04:47Z',
             'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'second-worker-send',
                'content': json.dumps({'success': True, 'resumedAgentId': second})}]}}]
        self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
        proof = _claude_historical_worker_deliveries(self.state, self.original, (), self.project, self.environ)
        self.assertIsNotNone(proof)
        self.assertEqual([turn['agent'] for turn in proof[3]], [WORKER, second])
        self.assertEqual([native.payload['agent_id'] for native in proof[1]], [WORKER, second])

    def test_missing_or_conflicting_mixed_replay_anchor_holds_without_old_outcome(self):
        for case in ('worker-receipt', 'worker-history', 'worker-native', 'worker-disposition',
                     'worker-origin', 'worker-route', 'lead-receipt', 'disposition-result', 'disposition-time'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare_send()
                self.store.save(self.project, self.state)
                self.queue(self.worker_events)
                payload = {'hook_event_name': 'Stop', 'cwd': str(self.project), 'session_id': SESSION}
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('ACK crash')):
                    with self.assertRaises(OSError): handle(payload, self.environ)
                committed = self.store.load(self.project)
                run = committed.recent_runs[0]
                assessment = dict(run.assessment)
                if case in ('worker-receipt', 'lead-receipt'):
                    identity = WORKER if case == 'worker-receipt' else LEAD
                    committed = replace(committed, terminal_receipts=tuple(receipt for receipt in committed.terminal_receipts
                                                                       if receipt.get('agent') != identity))
                if case == 'worker-history': committed = replace(committed, event_history=tuple(event for event in
                    committed.event_history if event.event_id != 'worker-start:delegation:delegation_updated'))
                if case == 'worker-native': self.worker.unlink()
                if case == 'worker-disposition': assessment['_claude_historical_sendmessage_dispositions'] = ()
                if case in ('worker-origin', 'worker-route'):
                    sequence = json.loads(json.dumps(assessment['_claude_sendmessage_sequences'][0]))
                    origin = sequence['historical_workers']['origins'][WORKER]
                    if case == 'worker-origin': origin['proof']['native_launch_hash'] = '0' * 64
                    else: origin['model'] = 'foreign'
                    assessment['_claude_sendmessage_sequences'] = (sequence,)
                if case in ('disposition-result', 'disposition-time'):
                    dispositions = [dict(item) for item in assessment['_claude_historical_sendmessage_dispositions']]
                    dispositions[0]['result' if case == 'disposition-result' else 'observed_at'] = (
                        '0' * 64 if case == 'disposition-result' else '2026-09-29T02:00:00+00:00')
                    assessment['_claude_historical_sendmessage_dispositions'] = dispositions
                committed = replace(committed, recent_runs=(replace(run, assessment=assessment),))
                self.store.save(self.project, committed)
                before = self.store.load(self.project)
                pending = self.store.session_record('claude', SESSION)['pending']
                result = handle(payload, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(pending, self.store.session_record('claude', SESSION)['pending'])

    def test_unproven_worker_delivery_or_callback_is_not_an_exclusion(self):
        for case in ('wrong-target', 'wrong-message', 'failed-result', 'missing-result',
                     'duplicate-call', 'wrong-resumed-id', 'overlap', 'foreign-root', 'foreign-cwd',
                     'newer-prompt', 'malformed-prompt', 'foreign-callback', 'wrong-report',
                     'wrong-parent', 'wrong-role', 'pre-terminal-callback', 'failed-terminal',
                     'raw-prompt', 'peer-origin', 'not-meta'):
            with self.subTest(case=case):
                self.setUp()
                parent = self.prepare_send()
                events = self.worker_events
                call = parent[-4]['message']['content'][0]
                if case == 'wrong-target': call['input']['to'] = 'foreign'
                if case == 'wrong-message': call['input']['message'] = 'Foreign'
                if case == 'failed-result': parent[-3]['message']['content'][0]['content'] = '{"success":false}'
                if case == 'missing-result': parent.pop(-3)
                if case == 'duplicate-call': parent.insert(-2, parent[-4])
                if case == 'wrong-resumed-id': parent[-3]['message']['content'][0]['content'] = json.dumps({
                    'success': True, 'resumedAgentId': 'foreign'})
                if case == 'overlap': self.rows[-1]['timestamp'] = '2026-09-29T02:05:10Z'
                if case == 'foreign-root': parent[-4]['sessionId'] = 'foreign'
                if case == 'foreign-cwd': parent[-4]['cwd'] = '/foreign'
                if case == 'newer-prompt': self.rows.append({**self.rows[-2], 'uuid': 'newer',
                    'timestamp': '2026-09-29T02:04:50Z'})
                if case == 'malformed-prompt': self.rows[-2]['message']['content'] = [{'type': 'text', 'text': 'new'}]
                if case == 'raw-prompt': self.rows[-2]['message']['content'] = 'Acknowledge the original work only.'
                if case == 'peer-origin': self.rows[-2]['origin'] = {'kind': 'peer'}
                if case == 'not-meta': self.rows[-2]['isMeta'] = False
                if case in ('foreign-callback', 'wrong-report', 'wrong-parent', 'wrong-role'):
                    events = (events[0], replace(events[1], payload={**events[1].payload, **{
                        'foreign-callback': {'agent_id': 'foreign'}, 'wrong-report': {'last_assistant_message': 'wrong'},
                        'wrong-parent': {'parent_thread_id': SESSION}, 'wrong-role': {'role': 'lead'}}[case]}))
                if case == 'pre-terminal-callback': events = (events[0], replace(events[1],
                    observed_at='2026-09-29T02:04:20+00:00'))
                if case == 'failed-terminal': self.rows[-1]['message']['content'][0]['text'] = \
                    'SYMPHONY_OUTCOME: {"status":"failed"}'
                self.write_worker()
                self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
                self.assertIsNone(_claude_historical_worker_deliveries(self.state, self.original, events,
                    self.project, self.environ))

    def test_original_credit_is_retained_and_native_reply_is_only_a_proof(self):
        self.prepare()
        origin = _claude_historical_worker_origin(self.state, self.original, WORKER, self.project, self.environ)
        self.assertIsNotNone(origin)
        terminal = _claude_historical_worker_terminal(self.rows, 2, WORKER, SESSION, 'claude-sonnet-5', 'low')
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal.payload['prompt_id'], 'worker-send-prompt')
        self.assertEqual(self.state.recent_runs[0], self.original)
        # Ordinary credit reader still rejects this reused child.
        from plugins.symphony.symphony.host_evidence import claude_substantive_launch
        self.assertIsNone(claude_substantive_launch(self.original, Event('fresh-credit', 'subagent_stopped',
            terminal.observed_at, {**terminal.payload, 'cwd': str(self.project)}), 'worker',
            self.original.assessment['_substantive_children'][WORKER]['admitted_at'], self.environ))

    def test_missing_original_credit_scope_or_receipt_holds(self):
        self.prepare()
        for case in ('missing-receipt', 'conflicting-result', 'wrong-receipt-parent', 'missing-start', 'missing-terminal',
                     'failed-proof', 'foreign-generation', 'foreign-epoch', 'wrong-launch-hash'):
            with self.subTest(case=case):
                state, run = self.state, self.original
                if case == 'missing-receipt': state = replace(state, terminal_receipts=())
                if case == 'conflicting-result': state = replace(state, terminal_receipts=(
                    *state.terminal_receipts, {**state.terminal_receipts[0], 'result': 'f' * 64}))
                if case == 'wrong-receipt-parent': state = replace(state, terminal_receipts=(
                    {**state.terminal_receipts[0], 'parent': 'foreign'},))
                if case == 'missing-start': state = replace(state, event_history=state.event_history[1:])
                if case == 'missing-terminal': state = replace(state, event_history=state.event_history[:1])
                if case in ('failed-proof', 'foreign-generation', 'foreign-epoch', 'wrong-launch-hash'):
                    proof = {**run.assessment['_substantive_children'][WORKER], **{
                        'failed-proof': {'successful': False}, 'foreign-generation': {'owner_generation': 8},
                        'foreign-epoch': {'epoch': 'foreign'}, 'wrong-launch-hash': {'launch_hash': 'foreign'}}[case]}
                    run = replace(run, assessment={**run.assessment, '_substantive_children': {WORKER: proof}})
                self.assertIsNone(_claude_historical_worker_origin(state, run, WORKER, self.project, self.environ))

    def test_handback_is_bound_to_its_successful_ordered_result(self):
        self.prepare()
        call = {**self.rows[-1], 'uuid': 'handback', 'timestamp': '2026-09-29T02:04:20Z',
            'message': {'model': 'claude-sonnet-5', 'stop_reason': 'tool_use', 'content': [{
                'type': 'tool_use', 'id': 'hb', 'name': 'SubagentHandback',
                'input': {'message': 'Original work remains complete.'}}]}}
        result = {**self.rows[-2], 'uuid': 'result', 'timestamp': '2026-09-29T02:04:21Z',
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'hb', 'content': 'OK'}]}}
        good = [*self.rows[:-1], call, result, self.rows[-1]]
        self.assertIsNotNone(_claude_historical_worker_terminal(good, 2, WORKER, SESSION,
                                                               'claude-sonnet-5', 'low', ack_only=True))
        for case in ('result-before-call', 'reversed-time', 'missing-time', 'failed-result', 'duplicate-id'):
            with self.subTest(case=case):
                rows = json.loads(json.dumps(good))
                if case == 'result-before-call': rows[-3], rows[-2] = rows[-2], rows[-3]
                if case == 'reversed-time': rows[-2]['timestamp'] = '2026-09-29T02:04:15Z'
                if case == 'missing-time': rows[-2].pop('timestamp')
                if case == 'failed-result': rows[-2]['message']['content'][0]['is_error'] = True
                if case == 'duplicate-id': rows.insert(-1, rows[-3])
                self.assertIsNone(_claude_historical_worker_terminal(rows, 2, WORKER, SESSION,
                                                                    'claude-sonnet-5', 'low', ack_only=True))

    def test_native_descendant_terminal_requires_own_latest_success(self):
        self.prepare()
        for case in ('foreign-root', 'foreign-child', 'foreign-route', 'foreign-effort', 'failed-marker',
                     'malformed-marker', 'duplicate-marker', 'unfinished', 'earlier-end-turn'):
            with self.subTest(case=case):
                rows = json.loads(json.dumps(self.rows))
                if case == 'foreign-root': rows[-1]['sessionId'] = 'foreign'
                if case == 'foreign-child': rows[-1]['agentId'] = 'foreign'
                if case == 'foreign-route': rows[-1]['message']['model'] = 'foreign'
                if case == 'foreign-effort': rows[-1]['effort'] = 'high'
                if case in ('failed-marker', 'malformed-marker', 'duplicate-marker'):
                    text = {'failed-marker': 'SYMPHONY_OUTCOME: {"status":"failed"}',
                        'malformed-marker': 'SYMPHONY_OUTCOME: bad',
                        'duplicate-marker': 'SYMPHONY_OUTCOME: {"status":"completed"}\n' * 2}[case]
                    rows[-1]['message']['content'][0]['text'] = text
                if case == 'unfinished': rows[-1]['message']['stop_reason'] = 'tool_use'
                if case == 'earlier-end-turn': rows.insert(-1, {**rows[-1], 'uuid': 'extra-terminal'})
                self.assertIsNone(_claude_historical_worker_terminal(rows, 2, WORKER, SESSION,
                                                                    'claude-sonnet-5', 'low'))


if __name__ == '__main__':
    unittest.main()
