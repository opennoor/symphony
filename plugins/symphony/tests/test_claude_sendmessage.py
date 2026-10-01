"""An archived assessed owner resumes only through exact ordered native sends."""

import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.host_evidence import claude_archived_sendmessage_sequence
from plugins.symphony.symphony.model import ProjectState
from plugins.symphony.symphony.runtime import handle, _root_admission_key
from plugins.symphony.symphony.store import StateStore
from plugins.symphony.tests import test_claude_host_evidence as fixture
from plugins.symphony.tests.test_claude_host_evidence import SESSION, LEAD, TYPE, REPORT


def coordinator_content(message):
    return ('The coordinator sent a message while you were working:\n' + message
            + '\n\nAddress this before completing your current task.')


class ClaudeSendMessageTests(unittest.TestCase):
    setUp = fixture.ClaudeHostEvidenceTests.setUp
    write_native = fixture.ClaudeHostEvidenceTests.write_native
    write_root_prompt = fixture.ClaudeHostEvidenceTests.write_root_prompt

    def prepare(self, count=2):
        self.environ['SYMPHONY_PROVIDER'] = 'claude'
        self.write_root_prompt()
        self.archived = replace(self.run, status='completed', owner_generation=7,
            updated_at='2026-09-29T02:02:01+00:00', outcome={'status': 'completed'},
            delegations=(replace(self.run.delegations[0], state='completed'),))
        self.store.save(self.project, ProjectState(recent_runs=(self.archived,)))
        self.store.bind_session('claude', SESSION, self.store._path(self.project), False, self.project, SESSION)
        parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
        parent[-1]['message']['content'][0]['input']['prompt'] = 'Complete this task.'
        child = [json.loads(line) for line in self.child.read_text().splitlines()]
        self.events = []
        for i in range(count):
            minute = 3 + 2 * i
            message = f'Continue the same task, step {i}.'
            report = REPORT + f'\nFinal result {i}.' if i == count - 1 else f'Intermediate result {i}.'
            parent.extend([
                {'type': 'assistant', 'sessionId': SESSION, 'cwd': str(self.project),
                 'timestamp': f'2026-09-29T02:{minute:02}:00Z', 'message': {'content': [{
                    'type': 'tool_use', 'name': 'SendMessage', 'id': f'send-{i}',
                    'input': {'to': LEAD, 'message': message}}]}},
                {'type': 'user', 'sessionId': SESSION, 'timestamp': f'2026-09-29T02:{minute:02}:02Z',
                 'message': {'content': [{'type': 'tool_result', 'tool_use_id': f'send-{i}',
                    'content': json.dumps({'success': True, 'resumedAgentId': LEAD})}]}}])
            child.extend([
                {'type': 'user', 'uuid': f'prompt-send-{i}', 'sessionId': SESSION, 'agentId': LEAD,
                 'isSidechain': True, 'timestamp': f'2026-09-29T02:{minute:02}:01Z',
                 'origin': {'kind': 'coordinator'}, 'isMeta': True,
                 'message': {'content': coordinator_content(message)}},
                {'type': 'assistant', 'uuid': f'terminal-send-{i}', 'sessionId': SESSION,
                 'agentId': LEAD, 'isSidechain': True, 'timestamp': f'2026-09-29T02:{minute+1:02}:00Z',
                 'effort': 'low', 'message': {'model': 'claude-sonnet-5', 'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': report}]}}])
            payload = {'provider': 'claude', 'session_id': SESSION, 'cwd': str(self.project),
                       'agent_id': LEAD, 'agent_type': TYPE, 'prompt_id': f'hook-context-{i}'}
            for name, when in [('SubagentStart', f'2026-09-29T02:{minute:02}:01.1+00:00'),
                               ('SubagentStop', f'2026-09-29T02:{minute+1:02}:00.1+00:00')]:
                event = event_from_payload('claude', {**payload, 'hook_event_name': name,
                    **({'last_assistant_message': report, 'status': 'completed'} if name == 'SubagentStop' else {})})
                self.events.append(replace(event, observed_at=when))
        self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in child))
        self.queue()

    def queue(self, events=None):
        for event in self.events if events is None else events:
            self.store.queue_session_event('claude', SESSION, event, ambiguous_owner=True)

    def call(self, hook='Stop'):
        return handle({'cwd': str(self.project), 'session_id': SESSION, 'hook_event_name': hook,
                       'prompt': '/symphony:status'}, self.environ)

    def proof(self):
        return claude_archived_sendmessage_sequence(self.store.load(self.project), tuple(self.events),
                                                    SESSION, self.project, self.environ)

    def test_exact_native_coordinator_projection_rejects_raw_and_conflicting_metadata(self):
        for case in ('raw', 'prefix', 'suffix', 'extra', 'peer', 'malformed-origin', 'not-meta'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                rows = [json.loads(line) for line in self.child.read_text().splitlines()]
                prompt = rows[-4]
                message = 'Continue the same task, step 1.'
                if case == 'raw': prompt['message']['content'] = message
                if case == 'prefix': prompt['message']['content'] = coordinator_content(message).replace(
                    'The coordinator', 'A coordinator', 1)
                if case == 'suffix': prompt['message']['content'] = coordinator_content(message)[:-1]
                if case == 'extra': prompt['message']['content'] += '\nExtra instructions.'
                if case == 'peer': prompt['origin'] = {'kind': 'peer'}
                if case == 'malformed-origin': prompt['origin'] = 'coordinator'
                if case == 'not-meta': prompt['isMeta'] = False
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                self.assertIsNone(self.proof())
        self.setUp()
        self.prepare()
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        # Persisted optional metadata may be absent; exact projected content
        # and all independent root/native authority are still required.
        for row in rows:
            if row.get('origin', {}).get('kind') == 'coordinator':
                row.pop('origin')
                row.pop('isMeta')
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertIsNotNone(self.proof())

    def test_ordered_sequence_commits_each_turn_only_latest_outcome_and_archives_publicly(self):
        for hook in ('Stop', 'SessionStart', 'UserPromptSubmit'):
            with self.subTest(hook=hook):
                self.setUp()
                self.prepare()
                self.assertIsNotNone(self.proof())
                result = self.call(hook)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.call()
                state = self.store.load(self.project)
                self.assertIsNone(state.active_run)
                self.assertEqual(len(state.recent_runs), 1)
                run = state.recent_runs[0]
                self.assertEqual((run.run_id, run.lead_identity, run.owner_generation),
                                 (self.archived.run_id, LEAD, 7))
                self.assertEqual(run.outcome, {'status': 'completed'})
                self.assertEqual(len(state.terminal_receipts), 2)
                self.assertEqual([receipt['turn'] for receipt in state.terminal_receipts],
                                 ['prompt_id:prompt-send-0', 'prompt_id:prompt-send-1'])
                completed = [event for event in state.event_history if event.kind == 'lead_completed']
                self.assertNotIn('2026-09-29T02:04:00+00:00', [event.observed_at for event in completed])
                self.assertEqual(sum(event.observed_at == '2026-09-29T02:06:00+00:00'
                                     for event in completed), 1)
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_shared_hook_prompt_context_deduplicates_start_without_losing_native_turns(self):
        self.prepare()
        record = self.store.session_record('claude', SESSION)
        self.store.finish_session_events(record, {event.event_id for event in self.events})
        self.events = [replace(event_from_payload('claude', {
            **event.payload, 'prompt_id': 'same-root-context'}), observed_at=event.observed_at)
            for event in self.events]
        self.queue()
        self.assertEqual(len(self.store.session_record('claude', SESSION)['pending']), 3)
        self.call()
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(len(state.terminal_receipts), 2)
        self.assertEqual([receipt['turn'] for receipt in state.terminal_receipts],
                         ['prompt_id:prompt-send-0', 'prompt_id:prompt-send-1'])
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_commit_before_ack_and_partial_ack_replay_only_ack_original_owner(self):
        for mode in ('crash', 'partial', 'new-owner', 'same-run-owner', 'same-run-assessment', 'old-codec'):
            with self.subTest(mode=mode):
                self.setUp()
                self.prepare()
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('crash')):
                    with self.assertRaises(OSError):
                        self.call('SessionStart' if mode.startswith('same-run') else 'Stop')
                committed = self.store.load(self.project)
                if mode == 'partial':
                    record = self.store.session_record('claude', SESSION)
                    self.store.finish_session_events(record, {self.events[0].event_id, self.events[-1].event_id})
                if mode == 'new-owner':
                    newer = replace(self.run, run_id='new-owner', lead_identity='b9c4054c282207da0',
                                    started_at='2026-09-29T02:10:00+00:00')
                    self.store.save(self.project, replace(committed, active_run=newer,
                        active_runs={f'claude:{SESSION}': newer}))
                if mode.startswith('same-run'):
                    from plugins.symphony.symphony.reducer import reduce
                    from plugins.symphony.symphony.model import Event, Delegation
                    changed, _ = reduce(committed, Event('new-assessment', 'assessment_accepted',
                        '2026-09-29T02:10:00+00:00', {'size': 'large', 'complexity': 'complex',
                        'risk': 'high', 'topology': 'delegated', 'route': {
                            'lead_model': 'claude-opus-4-6', 'lead_effort': 'high'}}))
                    if mode == 'same-run-owner':
                        changed, _ = reduce(changed, Event('new-lead', 'lead_started',
                            '2026-09-29T02:11:00+00:00', {'identity': 'b9c4054c282207da0',
                            'owner_generation': 8, 'safe_boundary': True}))
                        self.assertEqual(changed.active_run.owner_generation, 8)
                        updated = replace(changed.active_run, delegations=(*changed.active_run.delegations,
                            Delegation('b9c4054c282207da0', 'lead', '', 'working',
                                       requested_tier='claude-opus-4-6', requested_effort='high')))
                    else:
                        updated = changed.active_run
                    self.store.save(self.project, replace(changed, active_run=updated,
                        active_runs={f'claude:{SESSION}': updated}))
                if mode == 'old-codec':
                    from plugins.symphony.tests.test_assessed_contract import AssessedContractTests
                    old = AssessedContractTests.released_store_160(self.store.root)
                    old.save(self.project, old.load(self.project))
                before = self.store.load(self.project)
                self.call('SessionStart')
                after = self.store.load(self.project)
                self.assertEqual(after.recent_runs, before.recent_runs)
                self.assertEqual(tuple(r for r in after.terminal_receipts if r.get('agent') == LEAD),
                                 tuple(r for r in before.terminal_receipts if r.get('agent') == LEAD))
                self.assertEqual(replace(after.active_run, owner_seen_at=before.active_run.owner_seen_at,
                                         updated_at=before.active_run.updated_at)
                                 if before.active_run else after.active_run, before.active_run)
                self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_native_and_callback_conflicts_hold_the_entire_sequence(self):
        cases = ('wrong-to', 'structured', 'failed-result', 'missing-result', 'duplicate-call',
                 'duplicate-result', 'wrong-resumed-id', 'foreign-cwd', 'relative-cwd', 'foreign-root',
                 'peer-call', 'wrong-message', 'newer-unfinished', 'wrong-model', 'wrong-effort',
                 'failed-final', 'malformed-final', 'duplicate-outcome', 'markerless-final',
                 'overlapping-send', 'late-result', 'competing-Agent', 'missing-terminal',
                 'foreign-callback', 'wrong-report', 'wrong-turn', 'changed-role', 'missing-owner',
                 'changed-callback-time', 'fast-owner', 'pre-terminal-callback', 'truncated-original',
                 'wrong-original-prompt', 'hidden-role', 'failed-handback', 'duplicate-handback')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
                child = [json.loads(line) for line in self.child.read_text().splitlines()]
                call = parent[-4]['message']['content'][0]
                result = parent[-3]['message']['content'][0]
                if case == 'wrong-to': call['input']['to'] = 'foreign'
                if case == 'structured': call['input']['message'] = {'type': 'message'}
                if case == 'failed-result': result['content'] = '{"success":false}'
                if case == 'missing-result': parent.pop(-3)
                if case == 'duplicate-call': parent.append(parent[-4])
                if case == 'duplicate-result': parent.append(parent[-3])
                if case == 'wrong-resumed-id': result['content'] = '{"success":true,"resumedAgentId":"foreign"}'
                if case == 'foreign-cwd': parent[-4]['cwd'] = str(self.root / 'foreign')
                if case == 'relative-cwd': parent[-4]['cwd'] = '.'
                if case == 'foreign-root': parent[-4]['sessionId'] = 'foreign'
                if case == 'peer-call': parent[-4]['agentId'] = 'foreign'
                if case == 'wrong-message': child[-4]['message']['content'] = 'Unrelated.'
                if case == 'newer-unfinished': child.append({**child[-2], 'uuid': 'unfinished',
                                                            'timestamp': '2026-09-29T02:07:00Z'})
                if case == 'wrong-model': child[-1]['message']['model'] = 'foreign'
                if case == 'wrong-effort': child[-1]['effort'] = 'high'
                if case in ('failed-final', 'malformed-final', 'duplicate-outcome', 'markerless-final'):
                    child[-1]['message']['content'][0]['text'] = {
                        'failed-final': 'SYMPHONY_OUTCOME: {"status":"failed"}',
                        'malformed-final': 'SYMPHONY_OUTCOME: bad',
                        'duplicate-outcome': REPORT + '\n' + REPORT,
                        'markerless-final': 'Done.'}[case]
                if case == 'overlapping-send': parent[-2]['timestamp'] = '2026-09-29T02:03:30Z'
                if case == 'late-result': parent[-3]['timestamp'] = '2026-09-29T02:05:30Z'
                if case == 'competing-Agent': call['name'] = 'Agent'
                if case == 'missing-terminal': child.pop(-1)
                if case in ('foreign-callback', 'wrong-report', 'wrong-turn', 'changed-role'):
                    changes = {'foreign-callback': {'session_id': 'foreign'},
                               'wrong-report': {'last_assistant_message': 'Different'},
                               'wrong-turn': {'turn_id': 'foreign'},
                               'changed-role': {'role': 'worker'}}[case]
                    self.events[-1] = replace(self.events[-1], payload={**self.events[-1].payload, **changes})
                if case == 'changed-callback-time':
                    self.events[1] = replace(self.events[1], observed_at='2026-09-29T02:05:30+00:00')
                if case == 'pre-terminal-callback':
                    self.events[1] = replace(self.events[1], observed_at='2026-09-29T02:03:02+00:00')
                if case == 'truncated-original': child = child[2:]
                if case == 'wrong-original-prompt': parent[1]['message']['content'][0]['input']['prompt'] = 'Foreign.'
                if case == 'hidden-role':
                    self.events[-1] = replace(self.events[-1], payload={**self.events[-1].payload,
                                                                      'task': 'SYMPHONY_ROLE: worker'})
                if case in ('failed-handback', 'duplicate-handback'):
                    handback = {**child[-3], 'uuid': 'handback', 'timestamp': '2026-09-29T02:03:30Z',
                        'message': {'model': 'claude-sonnet-5', 'stop_reason': 'tool_use', 'content': [{
                            'type': 'tool_use', 'id': 'handback-call', 'name': 'SubagentHandback',
                            'input': {'message': 'SYMPHONY_OUTCOME: {"status":"failed"}'}}]}}
                    result = {**child[-4], 'uuid': 'handback-result', 'timestamp': '2026-09-29T02:03:31Z',
                        'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'handback-call', 'is_error': True}]}}
                    child[3:3] = [handback, result] + ([handback] if case == 'duplicate-handback' else [])
                if case == 'missing-owner': self.store.save(self.project, ProjectState())
                if case == 'fast-owner':
                    self.store.save(self.project, ProjectState(recent_runs=(replace(self.archived,
                        assessment={'_fast_route': {'model': 'claude-sonnet-5', 'effort': 'low'}}),)))
                self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in child))
                self.assertIsNone(self.proof())

    def test_changed_same_id_and_trimmed_anchor_cannot_replay(self):
        for case in ('payload', 'time', 'trimmed', 'conflicting-receipt', 'missing-first-receipt',
                     'missing-root', 'changed-message', 'changed-native-report', 'missing-start-anchor',
                     'missing-owner-anchor', 'changed-owner-route', 'future-owner-generation'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('crash')):
                    with self.assertRaises(OSError): self.call()
                state = self.store.load(self.project)
                event = self.events[0]
                if case == 'payload': event = replace(event, payload={**event.payload, 'role': 'worker'})
                if case == 'time': event = replace(event, observed_at='2026-09-29T02:07:00+00:00')
                if case == 'trimmed': state = replace(state, recent_runs=())
                if case == 'conflicting-receipt':
                    state = replace(state, terminal_receipts=(*state.terminal_receipts,
                        {**state.terminal_receipts[0], 'result': 'foreign'}))
                if case == 'missing-first-receipt': state = replace(state, terminal_receipts=state.terminal_receipts[1:])
                if case == 'missing-root': self.parent.unlink()
                if case == 'changed-message':
                    rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                    rows[-4]['message']['content'][0]['input']['message'] = 'Foreign.'
                    self.parent.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                if case == 'changed-native-report':
                    rows = [json.loads(line) for line in self.child.read_text().splitlines()]
                    rows[-3]['message']['content'][0]['text'] = 'Different intermediate result.'
                    self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                if case == 'missing-start-anchor':
                    archived = state.recent_runs[0]
                    state = replace(state, recent_runs=(replace(archived, assessment={**archived.assessment,
                        '_start_event_ids': archived.assessment['_start_event_ids'][1:]}),))
                if case in ('missing-owner-anchor', 'changed-owner-route', 'future-owner-generation'):
                    archived = state.recent_runs[0]
                    anchor = dict(archived.assessment['_claude_sendmessage_sequences'][0])
                    if case == 'missing-owner-anchor': anchor.pop('owner')
                    if case == 'changed-owner-route': anchor['owner'] = {**anchor['owner'], 'model': 'foreign'}
                    if case == 'future-owner-generation': anchor['owner_generation'] = archived.owner_generation + 1
                    state = replace(state, recent_runs=(replace(archived, assessment={**archived.assessment,
                        '_claude_sendmessage_sequences': (anchor,)}),))
                from plugins.symphony.symphony.runtime import _committed_sendmessage_source
                self.assertFalse(_committed_sendmessage_source(state, event, SESSION, 1, self.project, self.environ))

    def test_supplied_receipt_provenance_cannot_conflict_with_native_sequence(self):
        from plugins.symphony.symphony.runtime import _committed_sendmessage_source
        for field in ('native_agent_type', 'native_model', 'native_effort',
                      'native_followup_start_id', 'native_launch_prompt_hash', 'native_fast_escalation'):
            with self.subTest(field=field):
                self.setUp()
                self.prepare()
                with patch.object(StateStore, 'finish_session_events', side_effect=OSError('crash')):
                    with self.assertRaises(OSError): self.call()
                state = self.store.load(self.project)
                conflicting = replace(state, terminal_receipts=tuple({**receipt, field:
                    True if field == 'native_fast_escalation' else 'foreign'}
                    for receipt in state.terminal_receipts))
                self.assertFalse(_committed_sendmessage_source(conflicting, self.events[-1],
                    SESSION, 1, self.project, self.environ))
                absent = replace(state, terminal_receipts=tuple({key: value for key, value in receipt.items()
                    if key != field} for receipt in state.terminal_receipts))
                self.assertTrue(_committed_sendmessage_source(absent, self.events[-1],
                    SESSION, 1, self.project, self.environ))

    def test_conflicting_batch_and_generation_never_partially_commit(self):
        for case in ('worker', 'same-id-conflict', 'different-result', 'generation', 'retired'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare()
                before = self.store.load(self.project)
                extra = replace(self.events[-1], event_id='extra-source', payload={**self.events[-1].payload,
                    **({'agent_id': 'foreign', 'role': 'worker'} if case == 'worker' else
                       {'last_assistant_message': 'Different result.'})})
                if case in ('worker', 'same-id-conflict', 'different-result'):
                    if case == 'same-id-conflict': extra = replace(extra, event_id=self.events[-1].event_id)
                    self.queue([extra])
                else:
                    record = self.store.session_record('claude', SESSION)
                    if case == 'generation': record['pending'][0]['generation'] = record['generation'] + 1
                    else: record['retired_agents'] = [LEAD]
                    self.store._session_path('claude', SESSION).write_text(json.dumps(record))
                if case == 'same-id-conflict':
                    # The inbox's exact-ID collision is held by its own guard;
                    # proof never receives two contradictory facts as one turn.
                    continue
                self.call()
                after = self.store.load(self.project)
                self.assertEqual(after.recent_runs, before.recent_runs)
                self.assertEqual(tuple(r for r in after.terminal_receipts if r.get('agent') == LEAD),
                                 tuple(r for r in before.terminal_receipts if r.get('agent') == LEAD))
                self.assertTrue(self.store.session_record('claude', SESSION)['pending'])

    def test_root_intent_consumption_requires_exact_observed_send_not_old_backlog(self):
        for case in ('exact', 'new-objective', 'no-hook', 'control-detour'):
            with self.subTest(case=case):
                self.setUp()
                self.prepare(1)
                self.store.finish_session_events(self.store.session_record('claude', SESSION),
                                                {event.event_id for event in self.events})
                key = _root_admission_key('claude', {'session_id': SESSION})
                state = self.store.load(self.project)
                self.store.save(self.project, replace(state, configuration={'root_admission_intents': {
                    key: {'version': 1, 'prompt_context': hashlib.sha256(b'context').hexdigest()}}}))
                if case != 'no-hook':
                    handle({'cwd': str(self.project), 'session_id': SESSION, 'hook_event_name': 'PreToolUse',
                            'tool_name': 'SendMessage', 'tool_use_id': 'send-0', 'prompt_id': 'context',
                            'tool_input': {'to': LEAD, 'message': 'Continue the same task, step 0.'}}, self.environ)
                if case in ('new-objective', 'control-detour'):
                    handle({'cwd': str(self.project), 'session_id': SESSION, 'hook_event_name': 'UserPromptSubmit',
                            'prompt_id': 'new-context', 'prompt': '/symphony:status' if case == 'control-detour'
                            else '/symphony:start A new task.'}, self.environ)
                self.queue()
                self.call()
                pending = self.store.load(self.project).configuration.get('root_admission_intents', {})
                self.assertEqual(key in pending, case in ('new-objective', 'no-hook'))


if __name__ == '__main__':
    unittest.main()
