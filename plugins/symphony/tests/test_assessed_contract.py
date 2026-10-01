"""New assessed routes require scoped child work; legacy native recovery stays valid."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import _substantive_child_completed, _stop_block_reason, reduce
from plugins.symphony.symphony.store import StateStore
from plugins.symphony.symphony.routing import Assessment
from plugins.symphony.symphony.runtime import (
    _accept_assessment, _observe_delegation, _queue_pending_delegation,
    _recovery_guidance, _render_actions,
    handle,
)


class AssessedContractTests(unittest.TestCase):
    @staticmethod
    def released_store_160(root):
        fixture = Path(__file__).parent / 'fixtures/released-v1.6.0'
        source = (fixture / 'store.py.txt').read_bytes()
        provenance = json.loads((fixture / 'provenance.json').read_text())
        assert hashlib.sha256(source).hexdigest() == provenance['sha256']
        # Exact released codec/writer, loaded only in this test. The model
        # definitions are unchanged since the recorded release.
        module = types.ModuleType('plugins.symphony.symphony.released_store_160')
        module.__package__ = 'plugins.symphony.symphony'
        exec(compile(source, 'released-v1.6.0/store.py', 'exec'), module.__dict__)
        return module.StateStore(root)

    def begin(self, provider, *, legacy=False):
        self.provider = provider
        self.sequence = 0
        self.state = ProjectState(enabled=True, active_run=RunState(
            'run', 'Implement a small feature', 'assessing', provider=provider,
            session_id='root', started_at='2026-10-01T14:00:00+00:00'))
        self.assess()
        if legacy:
            assessment = dict(self.state.active_run.assessment)
            assessment.pop('substantive_contract')
            self.state = replace(self.state, active_run=replace(self.state.active_run, assessment=assessment))
        self.child('lead', 'lead', False, 'lead-1')

    def event(self, kind, **payload):
        self.sequence += 1
        return Event(f'event-{self.sequence}', kind, f'2026-10-01T14:00:{self.sequence:02d}+00:00', payload)

    def assess(self):
        self.state, _ = _accept_assessment(self.state, self.event('subagent_stopped'),
                                            self.provider, {}, Assessment('small', 'simple'))

    def child(self, identity, role, terminal, turn, *, status='completed', report='', parent=None):
        route = self.state.active_run.assessment['route']
        fields = {'provider': self.provider, 'session_id': 'root', 'agent_id': identity,
                  'role': role, 'task': 'SYMPHONY_ROLE: ' + role,
                  'parent_thread_id': parent or ('root' if role == 'lead' else 'lead'),
                  'model': route['lead_model'], 'model_reasoning_effort': route['lead_effort'],
                  'turn_id' if self.provider == 'codex' else 'prompt_id': turn}
        if terminal:
            fields.update(status=status, last_assistant_message=report)
        self.state, actions = _observe_delegation(self.state, self.event(
            'subagent_stopped' if terminal else 'subagent_started', **fields))
        return actions

    def worker(self, identity='worker', turn='worker-1', *, status='completed', parent=None):
        self.child(identity, 'worker', False, turn, parent=parent)
        return self.child(identity, 'worker', True, turn, status=status, parent=parent)

    def test_missing_worker_recovers_same_lead_then_archives_cleanly(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                actions = self.child('lead', 'lead', True, 'lead-1')
                run = self.state.active_run
                self.assertEqual(run.status, 'recovering')
                self.assertIsNone(run.outcome)
                self.assertEqual(run.owner_generation, 1)
                self.assertTrue(any(action.kind == 'request_substantive_work' for action in actions))
                self.assertFalse(any(action.kind == 'replace_lead' for action in actions))
                self.assertIn('SAME registered lead', _recovery_guidance(self.state, provider))
                self.assertIn('SAME registered lead', _render_actions(actions, self.state, provider)[-1].payload['text'])
                self.assertEqual(_stop_block_reason(run)['reason'], 'substantive_child_missing')
                self.child('lead', 'lead', False, 'lead-2')
                self.worker()
                self.child('lead', 'lead', True, 'lead-2')
                self.assertEqual(self.state.active_run.status, 'completing')
                self.assertEqual(self.state.active_run.owner_generation, 1)
                self.state, _ = reduce(self.state, self.event('stop_requested'))
                self.assertIsNone(self.state.active_run)
                self.assertEqual(self.state.recent_runs[-1].status, 'completed')

    def test_pending_failed_foreign_and_stale_children_cannot_satisfy_scope(self):
        for provider in ('codex', 'claude'):
            for scenario in ('pending', 'failed', 'foreign', 'new-assessment', 'new-generation', 'new-lead', 'new-turn'):
                with self.subTest(provider=provider, scenario=scenario):
                    self.begin(provider)
                    if scenario == 'pending':
                        self.child('worker', 'worker', False, 'worker-1')
                    elif scenario == 'failed':
                        self.worker(status='failed')
                    elif scenario == 'foreign':
                        self.worker(parent='foreign')
                    else:
                        self.worker()
                        self.assertTrue(_substantive_child_completed(self.state.active_run))
                        if scenario == 'new-assessment':
                            self.assess()
                        elif scenario == 'new-generation':
                            self.state = replace(self.state, active_run=replace(self.state.active_run, owner_generation=2))
                        elif scenario == 'new-lead':
                            self.state = replace(self.state, active_run=replace(self.state.active_run, lead_identity='other'))
                        else:
                            self.child('worker', 'worker', False, 'worker-2')
                            self.child('worker', 'worker', True, 'worker-1')
                    self.assertFalse(_substantive_child_completed(self.state.active_run))

    def test_current_classified_consultant_can_complete_deferred_lead(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                self.child('reviewer', 'consultant', False, 'review-1')
                self.child('reviewer', 'consultant', True, 'review-1')
                self.child('lead', 'lead', True, 'lead-1')
                self.assertIn('_pending_lead_completion', self.state.active_run.assessment)
                self.child('reviewer', 'consultant', False, 'review-2')
                self.child('reviewer', 'consultant', True, 'review-2',
                           report='SYMPHONY_DECISION: {"size":"small","complexity":"simple"}')
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                self.assertEqual(self.state.active_run.status, 'completing')

    def test_reducer_direct_completion_and_stop_cannot_bypass_proof(self):
        self.begin('codex')
        self.state, actions = reduce(self.state, self.event('lead_completed', identity='lead',
                                  owner_generation=1, outcome={'status': 'completed'}))
        self.assertEqual(self.state.active_run.status, 'recovering')
        self.assertEqual(actions[-1].kind, 'request_substantive_work')
        self.assertEqual(self.state.active_run.assessment['_retryable_lead'], 'lead')
        self.assertEqual(self.state.active_run.assessment['_retryable_lead_turn'], 'turn_id:lead-1')
        self.assertIsNotNone(_stop_block_reason(self.state.active_run))
        self.child('lead', 'lead', False, 'lead-2')
        self.worker()
        self.child('lead', 'lead', True, 'lead-2')
        self.state, _ = reduce(self.state, self.event('stop_requested'))
        self.assertIsNone(self.state.active_run)

    def test_pending_intent_never_credits_an_untracked_terminal_and_fresh_worker_recovers(self):
        for provider in ('codex', 'claude'):
            for fresh in (True, False):
                with self.subTest(provider=provider, fresh=fresh):
                    self.begin(provider)
                    route = self.state.active_run.assessment['route']
                    self.state = _queue_pending_delegation(self.state, 'worker', 'Implement',
                                                           route['lead_model'], route['lead_effort'])
                    if not fresh:
                        self.assess()
                    self.child('worker', 'worker', True, 'worker-1')
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.child('lead', 'lead', True, 'lead-1')
                    self.assertEqual(self.state.active_run.status, 'recovering')
                    self.child('lead', 'lead', False, 'lead-2')
                    self.worker(identity='fresh-worker', turn='fresh-worker-1')
                    self.child('lead', 'lead', True, 'lead-2')
                    self.state, _ = reduce(self.state, self.event('stop_requested'))
                    self.assertIsNone(self.state.active_run)

    def test_legacy_routes_and_same_task_continuation_preserve_native_completion(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider, legacy=True)
                self.child('lead', 'lead', True, 'lead-1')
                self.assertEqual(self.state.active_run.status, 'completing')
                self.begin(provider)
                self.worker()
                self.child('lead', 'lead', True, 'lead-1')
                self.state, _ = reduce(self.state, self.event('stop_requested'))
                archived = self.state.recent_runs[-1]
                self.state = replace(self.state, active_run=replace(archived, status='completing'))
                self.child('lead', 'lead', False, 'lead-2')
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                self.child('lead', 'lead', True, 'lead-2')
                self.assertEqual(self.state.active_run.status, 'completing')

    def test_archived_fast_escalation_never_completes_under_old_direct_route(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider, legacy=True)
                run = self.state.active_run
                selected = run.assessment['route']
                assessment = {**run.assessment, 'topology': 'direct',
                              '_fast_route': {'model': selected['lead_model'], 'effort': selected['lead_effort']}}
                self.state = replace(self.state, active_run=replace(run, assessment=assessment))
                source = self.event('subagent_stopped', provider=provider, session_id='root', agent_id='lead',
                    role='lead', status='completed', _symphony_native_recovery=True,
                    **({'turn_id': 'lead-1'} if provider == 'codex' else {'prompt_id': 'lead-1'}),
                    last_assistant_message='SYMPHONY_FAST_DECISION: escalate\nSYMPHONY_OUTCOME: {"status":"completed"}')
                self.state, actions = _observe_delegation(self.state, source)
                self.assertEqual(self.state.active_run.status, 'assessing')
                self.assertIsNone(self.state.active_run.outcome)
                self.assertNotIn('size', self.state.active_run.assessment)
                self.assertNotIn('route', self.state.active_run.assessment)
                self.assertTrue(self.state.active_run.assessment['_fast_escalated'])
                self.assertIn('independent assessor', actions[-1].payload['text'])

    def test_native_archived_escalation_without_outcome_starts_reassessment(self):
        from plugins.symphony.tests.test_host_evidence import HostEvidenceTests, ROOT_ID
        from plugins.symphony.tests.test_claude_host_evidence import ClaudeHostEvidenceTests, SESSION
        from plugins.symphony.symphony.adapters import event_from_payload
        for provider, cls, session in (('codex', HostEvidenceTests, ROOT_ID),
                                       ('claude', ClaudeHostEvidenceTests, SESSION)):
            for hook in ('Stop', 'SessionStart', 'UserPromptSubmit'):
                for variant in ('normal', 'crash', 'partial', 'crash-with-start', 'new-owner', 'assessed-owner', 'bad-outcome', 'duplicate-decision', 'foreign-parent', 'tokenless-start', 'alias-new-owner', 'start-new-owner', 'mixed-new-owner', 'mixed-alias-new-owner', 'mixed-start-new-owner', 'mixed-trimmed'):
                    mixed = variant.startswith('mixed-')
                    ack = 'new-owner' if variant == 'mixed-trimmed' else variant.removeprefix('mixed-')
                    with self.subTest(provider=provider, hook=hook, ack=variant):
                        if hook != 'Stop' and ack in {'assessed-owner', 'bad-outcome', 'duplicate-decision', 'foreign-parent', 'new-owner', 'alias-new-owner', 'start-new-owner'}:
                            continue
                        fixture = cls()
                        fixture.setUp()
                        try:
                            report = 'SYMPHONY_FAST_DECISION: escalate'
                            if ack == 'bad-outcome':
                                report += '\nSYMPHONY_OUTCOME: not-json'
                            if ack == 'duplicate-decision':
                                report += '\nSYMPHONY_FAST_DECISION: escalate'
                            if provider == 'codex':
                                archived, payload, _ = fixture.load_archived_followup_fixture()
                                path = fixture.transcript
                                rows = [json.loads(line) for line in path.read_text().splitlines()]
                                rows[-1]['payload']['last_agent_message'] = report
                            else:
                                archived, payload = fixture.prepare_archived_followup(promptless=ack in {'tokenless-start', 'start-new-owner'})
                                path = fixture.child
                                rows = [json.loads(line) for line in path.read_text().splitlines()]
                                rows[-1]['message']['content'][0]['text'] = report
                                lead = archived.delegations[0]
                                archived = replace(archived, assessment={**archived.assessment, 'topology': 'direct',
                                    '_fast_route': {'model': lead.requested_tier, 'effort': lead.requested_effort}})
                            if ack == 'assessed-owner':
                                archived = replace(archived, assessment={**archived.assessment, '_fast_escalated': True})
                            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                            fixture.store.save(fixture.project, ProjectState(recent_runs=(archived,)))
                            record = fixture.store.session_record(provider, session)
                            fixture.store.finish_session_events(record, {item['event_id'] for item in record['pending']})
                            if ack == 'alias-new-owner':
                                payload['session_id'] = payload['agent_id']
                                payload['parent_thread_id'] = session
                            payload['last_assistant_message'] = report
                            if ack == 'foreign-parent':
                                payload['parent_thread_id'] = 'foreign-root'
                            fixture.store.queue_session_event(provider, session,
                                event_from_payload(provider, payload), ambiguous_owner=True)
                            request = {'provider': provider, 'session_id': session, 'cwd': str(fixture.project),
                                       'hook_event_name': hook, 'prompt': '$symphony:symphony status' if provider == 'codex' else '/symphony:status'}
                            rejected = ack in {'assessed-owner', 'bad-outcome', 'duplicate-decision', 'foreign-parent'}
                            if ack not in {'normal', 'assessed-owner', 'bad-outcome', 'duplicate-decision', 'foreign-parent'}:
                                finish = fixture.store.finish_session_events
                                def interrupt_ack(record, identities):
                                    if ack == 'partial':
                                        finish(record, {item['event_id'] for item in record['pending']
                                                        if item['kind'] == 'subagent_started'})
                                    raise OSError('simulated commit before ACK')
                                if ack in {'partial', 'crash-with-start', 'tokenless-start', 'start-new-owner'}:
                                    started = {**payload, 'hook_event_name': 'SubagentStart'}
                                    started.pop('last_assistant_message', None)
                                    started.pop('status', None)
                                    fixture.store.queue_session_event(provider, session,
                                        event_from_payload(provider, started), ambiguous_owner=True)
                                with patch.object(StateStore, 'finish_session_events', side_effect=interrupt_ack):
                                    with self.assertRaises(OSError):
                                        handle(request, fixture.environ)
                            if ack in {'new-owner', 'alias-new-owner', 'start-new-owner'}:
                                committed = fixture.store.load(fixture.project)
                                run = committed.active_run
                                event = Event('new-assessment', 'subagent_stopped', '2026-10-01T23:00:00+00:00', {})
                                updated, _ = _accept_assessment(replace(committed, active_run=run), event,
                                                               provider, {}, Assessment('small', 'simple'))
                                replacement = replace(updated.active_run, lead_identity='new-lead', owner_generation=2)
                                fixture.store.save(fixture.project, replace(updated, active_run=replacement,
                                    active_runs={f'{provider}:{session}': replacement}))
                            if mixed:
                                before = fixture.store.load(fixture.project)
                                self.assertTrue(before.terminal_receipts[-1]['native_fast_escalation'])
                                self.assertTrue(before.terminal_receipts[-1]['native_followup_start_id'])
                                old_store = self.released_store_160(fixture.store.root)
                                old_store.save(fixture.project, old_store.load(fixture.project))
                                after = fixture.store.load(fixture.project)
                                self.assertNotIn('native_fast_escalation', after.terminal_receipts[-1])
                                self.assertNotIn('native_followup_start_id', after.terminal_receipts[-1])
                                self.assertEqual(after.active_run.assessment, before.active_run.assessment)
                                self.assertEqual(after.active_run.owner_generation, 2)
                                if variant == 'mixed-trimmed':
                                    assessment = {key: value for key, value in after.active_run.assessment.items()
                                                  if key not in {'_archived_fast_escalation_turn', '_start_event_ids',
                                                                 '_terminal_turns', '_terminal_event_ids'}}
                                    trimmed = replace(after.active_run, assessment=assessment)
                                    fixture.store.save(fixture.project, replace(after, active_run=trimmed,
                                        active_runs={f'{provider}:{session}': trimmed}, recent_runs=()))
                            result = handle(request, fixture.environ)
                            if hook == 'Stop' and not rejected and ack not in {'new-owner', 'alias-new-owner', 'start-new-owner'}:
                                self.assertIn('fresh independent assessment', result.stdout)
                            state = fixture.store.load(fixture.project)
                            if rejected:
                                self.assertIsNone(state.active_run)
                                self.assertEqual(state.recent_runs, (archived,))
                                self.assertEqual(len(fixture.store.session_record(provider, session)['pending']), 1)
                                continue
                            if ack in {'new-owner', 'alias-new-owner', 'start-new-owner'}:
                                self.assertEqual(state.active_run.lead_identity, 'new-lead')
                                self.assertEqual(state.active_run.owner_generation, 2)
                                self.assertIn('substantive_contract', state.active_run.assessment)
                                self.assertEqual(len(fixture.store.session_record(provider, session)['pending']),
                                                 1 if variant == 'mixed-trimmed' else 0)
                                if variant == 'mixed-trimmed':
                                    self.assertIn('reloading alone does not restore missing proof', result.stdout)
                                continue
                            self.assertEqual(state.active_run.status, 'assessing')
                            self.assertIsNone(state.active_run.outcome)
                            self.assertNotIn('route', state.active_run.assessment)
                            self.assertNotIn('size', state.active_run.assessment)
                            self.assertEqual(state.recent_runs, ())
                            self.assertEqual(len(fixture.store.session_record(provider, session)['pending']), 0)
                            if ack == 'tokenless-start':
                                from plugins.symphony.symphony.host_evidence import claude_committed_native_start_replay
                                from plugins.symphony.symphony.runtime import _pending_child_disposition
                                for field, value in (('provider', 'foreign'), ('session_id', 'foreign-root'),
                                    ('parent_thread_id', 'foreign-root'), ('agent_id', 'foreign-child'),
                                    ('role', 'worker'), ('model', 'foreign-model')):
                                    bad = {**payload, 'hook_event_name': 'SubagentStart', field: value}
                                    bad.pop('last_assistant_message', None)
                                    bad.pop('status', None)
                                    event = event_from_payload(provider, bad)
                                    event = replace(event, payload={**event.payload, field: value})
                                    if provider == 'claude':
                                        self.assertFalse(claude_committed_native_start_replay(
                                            state, event, session, fixture.project, fixture.environ), field)
                                    else:
                                        self.assertNotEqual(_pending_child_disposition(state, event, provider, session), 'stale', field)
                                if provider == 'claude':
                                    native_rows = [json.loads(line) for line in fixture.child.read_text().splitlines()]
                                    new_prompt = next(row.copy() for row in native_rows if row.get('type') == 'user')
                                    new_prompt.update(uuid='newer-unfinished-prompt', timestamp='2099-01-01T00:00:00Z',
                                                      message={'role': 'user', 'content': 'New unfinished objective'})
                                    native_rows.append(new_prompt)
                                    fixture.child.write_text(''.join(json.dumps(row) + '\n' for row in native_rows))
                                    start = {**payload, 'hook_event_name': 'SubagentStart'}
                                    start.pop('last_assistant_message', None)
                                    self.assertFalse(claude_committed_native_start_replay(state,
                                        event_from_payload(provider, start), session, fixture.project, fixture.environ))
                        finally:
                            fixture.doCleanups()
                            fixture.tearDown()


    def test_present_malformed_or_unknown_contract_is_not_legacy(self):
        self.begin('codex')
        self.worker()
        for contract in (None, {}, {'version': 2}, {'version': True, 'epoch': 'event-1'}):
            run = replace(self.state.active_run, assessment={**self.state.active_run.assessment,
                                                           'substantive_contract': contract})
            self.assertFalse(_substantive_child_completed(run))

    def test_mixed_runtime_ack_requires_every_retained_acceptance_anchor(self):
        from plugins.symphony.symphony.host_evidence import retained_fast_escalation_receipt
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                turn = 'turn_id:turn' if provider == 'codex' else 'prompt_id:prompt'
                start = (hashlib.sha256(b'codex-host-turn\0old-lead\0turn').hexdigest()
                         if provider == 'codex' else 'a' * 64) + ':followup-start'
                receipt = dict(provider=provider, session='root', agent='old-lead', lead='old-lead',
                    parent='root', run_id='run', turn=turn, result='b' * 64, status='completed',
                    native_agent_type='symphony_lead', native_model='model', native_effort='medium')
                assessment = {'_archived_fast_escalation_turn': turn, '_start_event_ids': (start,),
                              '_terminal_turns': {'old-lead': (turn,)}, '_terminal_event_ids': ('b' * 64,)}
                run = RunState('run', 'new objective', provider=provider, session_id='root',
                    owner_generation=2, lead_identity='new-lead', assessment=assessment,
                    delegations=(Delegation('old-lead', 'lead', '', 'completed', 'model', 'medium'),))
                state = ProjectState(active_run=run)
                recovered = retained_fast_escalation_receipt(state, receipt)
                self.assertTrue(recovered['native_fast_escalation'])
                self.assertEqual(recovered['_retained_followup_start_ids'], (start,))
                self.assertEqual(state.active_run, run)
                for field in ('provider', 'session', 'agent', 'lead', 'parent', 'run_id', 'turn',
                              'result', 'status', 'native_model', 'native_effort'):
                    bad = {**receipt, field: 'foreign'}
                    self.assertEqual(retained_fast_escalation_receipt(state, bad), bad, field)
                for field, value in (('native_fast_escalation', False), ('native_fast_escalation', True),
                                     ('native_fast_escalation', 'true'), ('native_followup_start_id', ''),
                                     ('native_followup_start_id', 'foreign')):
                    bad = {**receipt, field: value}
                    self.assertEqual(retained_fast_escalation_receipt(state, bad), bad, field)
                for field, value in (('_archived_fast_escalation_turn', 'foreign'),
                    ('_start_event_ids', ()), ('_start_event_ids', start),
                    ('_terminal_turns', {'old-lead': ()}), ('_terminal_turns', {'old-lead': turn}),
                    ('_terminal_event_ids', ()), ('_terminal_event_ids', 'b' * 64)):
                    bad_run = replace(run, assessment={**assessment, field: value})
                    self.assertEqual(retained_fast_escalation_receipt(
                        replace(state, active_run=bad_run), receipt), receipt, field)
                self.assertEqual(retained_fast_escalation_receipt(ProjectState(), receipt), receipt)
                self.assertEqual(retained_fast_escalation_receipt(
                    replace(state, active_run=replace(run, delegations=())), receipt), receipt)

    def test_unmanaged_child_and_replayed_success_after_failure_do_not_count(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                for kind in ('subagent_started', 'subagent_stopped'):
                    self.state, _ = _observe_delegation(self.state, self.event(kind,
                        provider=provider, session_id='root', agent_id='unmanaged',
                        parent_thread_id='root', status='completed'))
                self.assertFalse(_substantive_child_completed(self.state.active_run))

                self.worker()
                self.child('worker', 'worker', True, 'worker-1', status='failed')
                self.child('worker', 'worker', True, 'worker-1')
                self.assertFalse(_substantive_child_completed(self.state.active_run))

    def test_first_tokenless_invocation_uses_accepted_start_and_reused_start_stays_ambiguous(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                fields = {'provider': provider, 'session_id': 'root', 'agent_id': 'tokenless-worker',
                          'parent_thread_id': 'lead', 'role': 'worker', 'task': 'SYMPHONY_ROLE: worker'}
                start = self.event('subagent_started', **fields)
                self.state, _ = _observe_delegation(self.state, start)
                terminal = self.event('subagent_stopped', **fields, status='completed')
                self.state, _ = _observe_delegation(self.state, terminal)
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                self.assertEqual(self.state.active_run.assessment['_substantive_children']['tokenless-worker']['turn'], '')
                self.child('lead', 'lead', True, 'lead-1')
                self.state, _ = _observe_delegation(self.state, start)
                self.state, _ = _observe_delegation(self.state, terminal)
                self.assertFalse(_substantive_child_completed(self.state.active_run))
                self.assertIn('tokenless-worker', self.state.active_run.assessment['_ambiguous_child_starts'])

    def test_assessed_native_success_needs_no_marker_but_supplied_reports_are_strict(self):
        for provider in ('codex', 'claude'):
            for report, expected in (('', 'completing'),
                ('SYMPHONY_OUTCOME: {"status":"completed"}', 'completing'),
                ('SYMPHONY_OUTCOME: {"status":"blocked"}', 'recovering'),
                ('SYMPHONY_OUTCOME: not-json', 'recovering'),
                ('SYMPHONY_OUTCOME: {"status":"completed"}\nSYMPHONY_OUTCOME: {"status":"completed"}', 'recovering'),
                ('SYMPHONY_OUTCOME: {"status":"failed"}\nSYMPHONY_OUTCOME: {"status":"completed"}', 'recovering')):
                with self.subTest(provider=provider, report=report):
                    self.begin(provider)
                    self.worker()
                    self.child('lead', 'lead', True, 'lead-1', report=report)
                    self.assertEqual(self.state.active_run.status, expected)
