"""Unmanaged pre-run results settle only from complete native provenance."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony.host_evidence import codex_unmanaged_pre_run_terminal
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState, persistable
from plugins.symphony.symphony.runtime import _dispose_unmanaged_pre_run_terminal, _terminal_result_id, handle
from plugins.symphony.symphony.store import StateStore


ROOT = '11111111-1111-1111-1111-111111111111'
CHILD = '22222222-2222-2222-2222-222222222222'
TURN = '33333333-3333-3333-3333-333333333333'
LEAD = '44444444-4444-4444-4444-444444444444'
LEAD_TURN = '55555555-5555-5555-5555-555555555555'
NAME = 'symphony_lead_gpt_6_1_sol_medium'
REPORT = 'The task requires assessment.\nSYMPHONY_FAST_DECISION: escalate'


def at(seconds):
    return f'2026-10-01T00:00:{seconds:06.3f}Z'


def row(kind, seconds, **payload):
    return {'type': kind, 'timestamp': at(seconds), 'payload': payload}


class UnmanagedCodexTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.home = Path(self.temp.name) / 'home'
        self.sessions = self.home / 'sessions/2026/10/01'
        self.sessions.mkdir(parents=True)
        self.store = StateStore(Path(self.temp.name) / 'state')
        self.env = {'CODEX_HOME': str(self.home), 'SYMPHONY_STATE_DIR': str(self.store.root),
                    'SYMPHONY_PROVIDER': 'codex', 'SYMPHONY_PROFILE': 'full'}
        self.run = RunState('admission', 'managed task', 'completing', lead_identity=LEAD,
                            session_id=ROOT, provider='codex', started_at=at(30), updated_at=at(50),
                            outcome={'status': 'completed'}, assessment={
                                'size': 'small', 'complexity': 'simple', 'risk': 'normal',
                                '_terminal_turns': {LEAD: ['turn_id:' + LEAD_TURN]}},
                            delegations=(Delegation(LEAD, 'lead', 'managed task', 'completed',
                                                    'gpt-6-luna', 'low'),))
        self.state = ProjectState(enabled=True, active_run=self.run,
                                  active_runs={'codex:' + ROOT: self.run}, event_history=(
                                      Event('initial-heartbeat', 'session_heartbeat', at(0),
                                            {'provider': 'codex', 'session_id': 'earlier-root'}),
                                      Event('admission', 'task_received', at(30), {'session_id': ROOT})))
        self.event = Event('original-stop', 'subagent_stopped', at(19.99), {
            'provider': 'codex', 'session_id': ROOT, 'parent_thread_id': ROOT,
            'agent_id': CHILD, 'turn_id': TURN, 'task_name': NAME, 'agent_type': NAME,
            'model': 'gpt-6.1-sol', 'model_reasoning_effort': 'medium',
            'last_assistant_message': REPORT})
        self.child = self.native_child(CHILD, TURN, NAME, 9.1, 10, 20, 'gpt-6.1-sol', 'medium', REPORT)
        self.root = [row('session_meta', 2, id=ROOT, timestamp=at(2), cwd=str(self.project), source='exec'),
                     row('response_item', 9, type='function_call', name='spawn_agent', call_id='call-original',
                         arguments=json.dumps({'task_name': NAME, 'fork_turns': 'none',
                                               'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium',
                                               'message': 'opaque-native-packet'})),
                     self.activity(9.2, 'started', 'call-original'),
                     row('response_item', 11, type='function_call_output', call_id='call-original',
                         output=json.dumps({'task_name': '/root/' + NAME})),
                     self.activity(20.1, 'completed', TURN)]
        self.lead = self.native_child(LEAD, LEAD_TURN, 'symphony_lead_gpt_6_luna_low',
                                      39, 40, 50, 'gpt-6-luna', 'low',
                                      'SYMPHONY_OUTCOME: {"status":"completed"}')
        self.write_native()

    def native_child(self, identity, turn, name, born, started, completed, model, effort, report):
        return [row('session_meta', born, id=identity, timestamp=at(born), cwd=str(self.project),
                    parent_thread_id=ROOT, agent_path='/root/' + name,
                    source={'subagent': {'thread_spawn': {'parent_thread_id': ROOT}}}),
                row('event_msg', started, type='task_started', turn_id=turn),
                row('turn_context', started + .1, turn_id=turn, model=model, effort=effort),
                row('event_msg', completed, type='task_complete', turn_id=turn, last_agent_message=report)]

    def activity(self, seconds, kind, identifier, **overrides):
        if kind == 'completed':
            identifier = 'subagent-completed-' + identifier
        return row('event_msg', seconds, type='item_completed', thread_id=ROOT,
                   item={'type': 'SubAgentActivity', 'id': identifier, 'kind': kind,
                         'agent_thread_id': CHILD, 'agent_path': '/root/' + NAME, **overrides})

    def write_native(self):
        for identity, rows in ((ROOT, self.root), (CHILD, self.child), (LEAD, self.lead)):
            (self.sessions / f'rollout-{identity}.jsonl').write_text(
                ''.join(json.dumps(item) + '\n' for item in rows), encoding='utf-8')

    def proof(self, state=None, event=None):
        event = event or self.event
        return codex_unmanaged_pre_run_terminal(state or self.state, event, ROOT,
                                                self.project, self.env, _terminal_result_id(event))

    def queue(self, state=None, event=None):
        self.store.save(self.project, state or self.state)
        self.store.bind_session('codex', ROOT, self.store._path(self.project), False, self.project, ROOT)
        self.store.queue_session_event('codex', ROOT, event or self.event, ambiguous_owner=True)

    def root_hook(self, hook='Stop'):
        return handle({'cwd': str(self.project), 'session_id': ROOT, 'hook_event_name': hook,
                       'prompt': '$symphony:symphony status', 'stop_hook_active': False}, self.env)

    def test_exact_native_unmanaged_disposition_has_no_task_credit(self):
        self.assertIsNotNone(self.proof())  # callback legitimately predates task_complete flush
        next_state, accepted = _dispose_unmanaged_pre_run_terminal(
            self.state, self.event, ROOT, self.project, self.env, 1, at(55), (self.event,))
        self.assertTrue(accepted)
        self.assertEqual(next_state.active_runs, self.state.active_runs)
        self.assertEqual(next_state.active_run, self.state.active_run)
        self.assertEqual(next_state.terminal_receipts, ())
        witness = next_state.event_history[-1]
        self.assertEqual(witness.kind, 'unmanaged_terminal_disposed')
        from plugins.symphony.symphony.reducer import reduce
        observed, actions = reduce(self.state, witness)
        self.assertEqual(actions, ())
        self.assertEqual(observed, next_state)
        self.assertEqual(reduce(observed, witness), (observed, ()))
        self.assertEqual(witness.payload['source_event_id'], self.event.event_id)
        self.assertEqual(witness.payload['result'], _terminal_result_id(self.event))
        serialized = json.dumps(witness.payload)
        self.assertNotIn(REPORT, serialized)
        self.assertNotIn('opaque-native-packet', serialized)
        self.assertNotIn(str(self.home), serialized)
        stripped = persistable(replace(witness, payload={**witness.payload, 'prompt': 'PRIVATE_SENTINEL'}))
        self.assertEqual(stripped.payload, witness.payload)

    def test_normal_root_hooks_settle_original_inbox_without_changing_lead(self):
        for hook in ('Stop', 'SessionStart', 'UserPromptSubmit'):
            with self.subTest(hook=hook):
                self.queue()
                result = self.root_hook(hook)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertEqual(self.store.session_record('codex', ROOT)['pending'], [])
                state = self.store.load(self.project)
                self.assertTrue(any(item.kind == 'unmanaged_terminal_disposed' for item in state.event_history))
                runs = (*state.active_runs.values(), *state.recent_runs)
                self.assertEqual({run.lead_identity for run in runs}, {LEAD})
                self.assertTrue(all(CHILD != receipt['agent'] for receipt in state.terminal_receipts))
                if hook == 'Stop':
                    self.assertIsNone(state.active_run)
                    self.assertEqual(state.recent_runs[-1].status, 'completed')

    def test_commit_before_ack_reloads_exact_witness_even_at_history_cap(self):
        fillers = tuple(Event(f'old-{i}', 'enable', at(1), {}) for i in range(197))
        state = replace(self.state, event_history=(*self.state.event_history, *fillers))
        self.queue(state)
        with patch.object(StateStore, 'finish_session_events', side_effect=OSError('crash before ACK')):
            with self.assertRaises(OSError):
                self.root_hook()
        committed = self.store.load(self.project)
        self.assertEqual(len(committed.event_history), 200)
        self.assertIsNotNone(committed.event_history[-2].payload.get('native_call_id'))
        self.assertEqual(len(self.store.session_record('codex', ROOT)['pending']), 1)
        result = self.root_hook()
        self.assertNotIn('"decision": "block"', result.stdout)
        self.assertEqual(self.store.session_record('codex', ROOT)['pending'], [])
        self.assertEqual(sum(item.kind == 'unmanaged_terminal_disposed'
                             for item in self.store.load(self.project).event_history), 1)

    def test_same_id_changed_result_generation_or_project_never_replays(self):
        committed, _ = _dispose_unmanaged_pre_run_terminal(
            self.state, self.event, ROOT, self.project, self.env, 1, at(55))
        for event, generation, project in (
            (replace(self.event, payload={**self.event.payload, 'last_assistant_message': REPORT + '\nchanged'}), 1, self.project),
            (self.event, 2, self.project), (self.event, True, self.project),
            (self.event, 1, self.project / 'foreign'),
            (replace(self.event, payload={**self.event.payload, 'parent_thread_id': LEAD}), 1, self.project),
        ):
            with self.subTest(generation=generation, project=project):
                unchanged, accepted = _dispose_unmanaged_pre_run_terminal(
                    committed, event, ROOT, project, self.env, generation, at(56))
                self.assertFalse(accepted)
                self.assertEqual(unchanged, committed)

    def test_conflicting_batch_start_result_or_descendant_stays_pending(self):
        conflicts = (replace(self.event, event_id='other-stop'),
                     replace(self.event, kind='subagent_started', event_id='original-start'),
                     replace(self.event, payload={**self.event.payload, 'turn_id': LEAD_TURN}),
                     Event('assessor-start', 'subagent_started', at(30), {'agent_type': 'assessor'}),
                     Event('descendant', 'subagent_stopped', at(25), {'parent_thread_id': CHILD}))
        for other in conflicts:
            with self.subTest(kind=other.kind):
                _, accepted = _dispose_unmanaged_pre_run_terminal(
                    self.state, self.event, ROOT, self.project, self.env, 1, at(55), (self.event, other))
                self.assertFalse(accepted)
        # Exact repeated snapshots are the same immutable callback.
        _, accepted = _dispose_unmanaged_pre_run_terminal(
            self.state, self.event, ROOT, self.project, self.env, 1, at(55), (self.event, self.event))
        self.assertTrue(accepted)

    def test_public_conflicting_pending_future_generation_and_retired_identity_hold(self):
        for variant in ('conflicting_result', 'future_generation', 'retired_identity'):
            with self.subTest(variant=variant):
                self.queue()
                record = self.store.session_record('codex', ROOT)
                if variant == 'conflicting_result':
                    other = replace(self.event, event_id='conflicting-stop',
                                    payload={**self.event.payload, 'last_assistant_message': REPORT + '\nother'})
                    self.store.queue_session_event('codex', ROOT, other, ambiguous_owner=True)
                elif variant == 'future_generation':
                    record['pending'][0]['generation'] += 1
                    self.store._write_json(self.store._session_path('codex', ROOT), record)
                else:
                    record['retired_agents'] = [CHILD]
                    self.store._write_json(self.store._session_path('codex', ROOT), record)
                result = self.root_hook()
                self.assertIn('"decision": "block"', result.stdout)
                state = self.store.load(self.project)
                self.assertEqual(state.active_run.lead_identity, LEAD)
                self.assertEqual(state.active_run.outcome, self.run.outcome)
                self.assertFalse(any(item.kind == 'unmanaged_terminal_disposed' for item in state.event_history))
                self.assertTrue(self.store.session_record('codex', ROOT)['pending'])
                # Restore the independent fixture's inbox for the next variant.
                self.store._session_path('codex', ROOT).unlink()

    def test_malformed_committed_witness_never_replays(self):
        committed, _ = _dispose_unmanaged_pre_run_terminal(
            self.state, self.event, ROOT, self.project, self.env, 1, at(55))
        record = committed.event_history[-1]
        for key, value in (('generation', True), ('native_completed_at', None),
                           ('native_started_at', 'not-a-date'), ('native_path', '/root/foreign'),
                           ('first_admission_at', at(5)), ('result', 'foreign-result')):
            with self.subTest(field=key):
                state = replace(committed, event_history=(*committed.event_history[:-1],
                    replace(record, payload={**record.payload, key: value})))
                unchanged, accepted = _dispose_unmanaged_pre_run_terminal(
                    state, self.event, ROOT, self.project, self.env, 1, at(56))
                self.assertFalse(accepted)
                self.assertEqual(unchanged, state)
        _, accepted = _dispose_unmanaged_pre_run_terminal(
            committed, replace(self.event, event_id='different-canonical-event'), ROOT,
            self.project, self.env, 1, at(56))
        self.assertFalse(accepted)

    def test_witness_that_cannot_persist_exactly_is_not_acknowledged(self):
        for variant in ('source_id', 'native_call_id'):
            with self.subTest(variant=variant):
                event = self.event
                if variant == 'source_id':
                    event = replace(event, event_id='x' * 513)
                else:
                    self.root[1]['payload']['call_id'] = 'x' * 513
                    self.root[2]['payload']['item']['id'] = 'x' * 513
                    self.root[3]['payload']['call_id'] = 'x' * 513
                    self.write_native()
                unchanged, accepted = _dispose_unmanaged_pre_run_terminal(
                    self.state, event, ROOT, self.project, self.env, 1, at(55))
                self.assertFalse(accepted)
                self.assertEqual(unchanged, self.state)

    def test_missing_truncated_history_or_prior_managed_identity_rejects(self):
        receipts = ({'agent': CHILD}, {'agent': '*', 'lead': CHILD},
                    {'agent': '*', 'parent': CHILD},
                    {'agent': '*', 'provider': 'codex', 'session': ROOT,
                     'result': _terminal_result_id(self.event)})
        changes = [replace(self.state, event_history=()),
                   replace(self.state, event_history=self.state.event_history[1:]),
                   replace(self.state, event_history=(*self.state.event_history,
                      *(Event(str(i), 'enable', at(1), {}) for i in range(198)))),
                   *(replace(self.state, terminal_receipts=(receipt,)) for receipt in receipts)]
        prior = replace(self.run, run_id='earlier-run', started_at=at(5), status='completed')
        changes.append(replace(self.state, recent_runs=(prior,)))
        for key, value in (('_terminal_event_ids', [_terminal_result_id(self.event)]),
                           ('_terminal_turns', {CHILD: ['turn_id:' + TURN]}),
                           ('_substantive_children', {CHILD: {'role': 'lead'}}),
                           ('_start_event_ids', [CHILD + ':start']),
                           ('_pending_delegations', [{'role': 'lead', 'model': 'gpt-6.1-sol'}])):
            run = replace(self.run, assessment={**self.run.assessment, key: value})
            changes.append(replace(self.state, active_run=run, active_runs={'codex:' + ROOT: run}))
        changes.append(replace(self.state, event_history=(*self.state.event_history,
            Event(self.event.event_id + ':delegation', 'delegation_updated', at(15), {}))))
        for state in changes:
            with self.subTest(state_change=state != self.state):
                self.assertIsNone(self.proof(state))

    def test_callback_conflicts_and_overlap_do_not_gain_disposition(self):
        for key, value in (('provider', 'claude'), ('session_id', LEAD), ('parent_thread_id', LEAD),
                           ('turn_id', LEAD_TURN), ('model', 'foreign'), ('model_reasoning_effort', 'high'),
                           ('status', 'failed'), ('last_assistant_message', 'SYMPHONY_OUTCOME: {"status":"completed"}'),
                           ('task_name', 'symphony_lead_fast_gpt_6_1_sol_medium')):
            with self.subTest(field=key):
                self.assertIsNone(self.proof(event=replace(self.event, payload={**self.event.payload, key: value})))
        run = replace(self.run, started_at=at(15))
        overlapping = replace(self.state, active_run=run, active_runs={'codex:' + ROOT: run},
                              event_history=(self.state.event_history[0],
                                             Event('admission', 'task_received', at(15), {'session_id': ROOT})))
        self.assertIsNone(self.proof(overlapping))

    def test_native_identity_reuse_failure_parent_and_chronology_reject(self):
        original_root, original_child = copy.deepcopy(self.root), copy.deepcopy(self.child)
        mutations = (
            lambda: self.root[0]['payload'].update(source={'subagent': {'thread_spawn': {'parent_thread_id': ROOT}}}),
            lambda: self.root[0]['payload'].update(agent_path='/root/foreign'),
            lambda: self.child[0]['payload'].update(source=[]),
            lambda: self.child[0]['payload']['source']['subagent']['thread_spawn'].update(agent_path='/root/foreign'),
            lambda: self.child[0]['payload'].update(forked_from_id=ROOT),
            lambda: self.root[2]['payload']['item'].update(agent_thread_id=LEAD),
            lambda: self.root[4]['payload']['item'].update(id=LEAD_TURN),
            lambda: self.root[3]['payload'].update(output='{"task_name":"/root/foreign"}'),
            lambda: self.root.append(copy.deepcopy(self.root[1])),
            lambda: self.child.append(row('event_msg', 21, type='task_started', turn_id=LEAD_TURN)),
            lambda: self.child.append(row('event_msg', 21, type='task_failed', turn_id=TURN)),
            lambda: self.child[2]['payload'].update(model='foreign'),
            lambda: self.child[0]['payload'].update(cwd='.'),
            lambda: self.child[1].update(timestamp=at(22)),
            lambda: self.root.append(row('response_item', 25, type='function_call', name='followup_task',
                                        call_id='followup', arguments=json.dumps({'target': NAME}))),
            lambda: self.root.append(row('response_item', 12, type='function_call', name='spawn_agent',
                                        call_id='overlap', arguments=json.dumps({'task_name': 'other'}))),
            lambda: self.root.append(self.activity(25, 'interacted', 'unpaired-followup')),
        )
        for ordinal, mutate in enumerate(mutations):
            self.root, self.child = copy.deepcopy(original_root), copy.deepcopy(original_child)
            mutate()
            self.write_native()
            with self.subTest(ordinal=ordinal):
                self.assertIsNone(self.proof())


if __name__ == '__main__':
    unittest.main()
