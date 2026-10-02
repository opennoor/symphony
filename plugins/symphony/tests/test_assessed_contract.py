"""New assessed routes require scoped child work; legacy native recovery stays valid."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import patch

from plugins.symphony.symphony.model import Action, Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import _substantive_child_completed, _stop_block_reason, reduce
from plugins.symphony.symphony.store import StateStore
from plugins.symphony.symphony.routing import Assessment, snapshot_for
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

    def begin(self, provider, *, legacy=False, sizing=None):
        self.provider = provider
        self.sequence = 0
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'project'
        self.project.mkdir()
        self.environ = {'CLAUDE_CONFIG_DIR': str(Path(temporary.name) / 'claude')}
        self.state = ProjectState(enabled=True, active_run=RunState(
            'run', 'Implement a small feature', 'assessing', provider=provider,
            session_id='root', started_at='2026-10-01T14:00:00+00:00'))
        self.assess(sizing)
        if legacy:
            assessment = dict(self.state.active_run.assessment)
            assessment.pop('substantive_contract')
            self.state = replace(self.state, active_run=replace(self.state.active_run, assessment=assessment))
        self.child('lead', 'lead', False, 'lead-1')

    def event(self, kind, **payload):
        self.sequence += 1
        return Event(f'event-{self.sequence}', kind, f'2026-10-01T14:00:{self.sequence:02d}+00:00', payload)

    def assess(self, sizing=None):
        self.state, _ = _accept_assessment(self.state, self.event('subagent_stopped'),
                                            self.provider, {}, sizing or Assessment('small', 'simple'))

    def child(self, identity, role, terminal, turn, *, status='completed', report='', parent=None, hook_fields=None):
        route = self.state.active_run.assessment['route']
        fields = {'provider': self.provider, 'session_id': 'root', 'agent_id': identity,
                  'cwd': str(self.project),
                  'role': role, 'task': 'SYMPHONY_ROLE: ' + role,
                  'parent_thread_id': parent or ('root' if role == 'lead' else 'lead'),
                  'model': route['lead_model'], 'model_reasoning_effort': route['lead_effort'],
                  'turn_id' if self.provider == 'codex' else 'prompt_id': turn}
        for key, value in (hook_fields or {}).items():
            if value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        if terminal:
            fields.update(status=status, last_assistant_message=report)
        elif self.provider == 'claude' and role in {'worker', 'consultant'}:
            from plugins.symphony.tests.native_child_fixture import write_claude_child_launch
            write_claude_child_launch(self.environ['CLAUDE_CONFIG_DIR'], self.project,
                self.state.active_run, identity, role, fields['model'], fields['model_reasoning_effort'], turn)
        self.state, actions = _observe_delegation(self.state, self.event(
            'subagent_stopped' if terminal else 'subagent_started', **fields), self.environ)
        return actions

    def worker(self, identity='worker', turn='worker-1', *, status='completed', parent=None):
        self.child(identity, 'worker', False, turn, parent=parent)
        return self.child(identity, 'worker', True, turn, status=status, parent=parent)

    def test_flagged_route_needs_a_distinct_successful_independent_review(self):
        decision = 'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}'
        for provider in ('codex', 'claude'):
            for sizing in (Assessment('small', 'simple', 'high'), Assessment('small', 'complex')):
                with self.subTest(provider=provider, sizing=sizing):
                    self.begin(provider, sizing=sizing)
                    self.assertTrue(self.state.active_run.assessment['substantive_contract']['review_required'])
                    self.worker()
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.child('lead', 'lead', True, 'lead-1')
                    self.assertEqual(self.state.active_run.status, 'recovering')
                    self.assertIn('SYMPHONY_REVIEW: passed', _recovery_guidance(self.state, provider))
                    self.child('lead', 'lead', False, 'lead-2')
                    self.child('unmarked-review', 'consultant', False, 'review-1')
                    self.child('unmarked-review', 'consultant', True, 'review-1', report=decision)
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.child('reviewer', 'consultant', False, 'review-2')
                    self.child('reviewer', 'consultant', True, 'review-2',
                               report=decision + '\nSYMPHONY_REVIEW: passed')
                    self.assertTrue(_substantive_child_completed(self.state.active_run))
                    self.child('lead', 'lead', True, 'lead-2')
                    self.assertEqual(self.state.active_run.status, 'completing')

                with self.subTest(provider=provider, sizing=sizing, case='self-review'):
                    self.begin(provider, sizing=sizing)
                    self.child('reviewer', 'worker', False, 'review-1')
                    self.child('reviewer', 'worker', True, 'review-1', report='SYMPHONY_REVIEW: passed')
                    self.assertFalse(_substantive_child_completed(self.state.active_run))

                with self.subTest(provider=provider, sizing=sizing, case='stale-review'):
                    self.begin(provider, sizing=sizing)
                    self.child('early-reviewer', 'worker', False, 'review-1')
                    self.child('early-reviewer', 'worker', True, 'review-1', report='SYMPHONY_REVIEW: passed')
                    self.worker()
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.child('fresh-reviewer', 'worker', False, 'review-2')
                    self.child('fresh-reviewer', 'worker', True, 'review-2', report='SYMPHONY_REVIEW: passed')
                    self.assertTrue(_substantive_child_completed(self.state.active_run))

                with self.subTest(provider=provider, sizing=sizing, case='old-contract'):
                    self.begin(provider, sizing=sizing)
                    run = self.state.active_run
                    contract = dict(run.assessment['substantive_contract'])
                    contract.pop('review_required')
                    self.state = replace(self.state, active_run=replace(run, assessment={**run.assessment,
                                         'substantive_contract': contract}))
                    self.worker()
                    self.assertTrue(_substantive_child_completed(self.state.active_run))

    def test_review_only_child_cannot_replace_work_unless_contract_is_legacy(self):
        for provider in ('codex', 'claude'):
            for role in ('worker', 'consultant'):
                for legacy in (False, True):
                    with self.subTest(provider=provider, role=role, legacy=legacy):
                        self.begin(provider)
                        run = self.state.active_run
                        self.assertIs(run.assessment['substantive_contract']['review_required'], False)
                        if legacy:
                            contract = dict(run.assessment['substantive_contract'])
                            contract.pop('review_required')
                            self.state = replace(self.state, active_run=replace(run, assessment={
                                **run.assessment, 'substantive_contract': contract}))
                        report = 'SYMPHONY_REVIEW: passed'
                        if role == 'consultant':
                            report += '\nSYMPHONY_DECISION: {"size":"small","complexity":"simple"}'
                        self.child('reviewer', role, False, 'review-1')
                        self.child('reviewer', role, True, 'review-1', report=report)
                        self.assertEqual(_substantive_child_completed(self.state.active_run), legacy)
                        self.child('lead', 'lead', True, 'lead-1')
                        if not legacy:
                            self.assertEqual(self.state.active_run.status, 'recovering')
                            self.assertIsNone(self.state.active_run.outcome)
                            self.assertEqual(_stop_block_reason(self.state.active_run)['reason'],
                                             'substantive_child_missing')
                            self.state, _ = reduce(self.state, self.event('stop_requested'))
                            self.assertIsNotNone(self.state.active_run)
                            self.child('lead', 'lead', False, 'lead-2')
                            self.worker()
                            self.child('lead', 'lead', True, 'lead-2')
                        self.assertEqual(self.state.active_run.status, 'completing')
                        self.state, _ = reduce(self.state, self.event('stop_requested'))
                        self.assertIsNone(self.state.active_run)
                        self.assertEqual(self.state.recent_runs[-1].status, 'completed')

    def late_assessor(self, provider):
        self.begin(provider)
        self.state = replace(self.state, event_history=(), active_run=replace(
            self.state.active_run, status='assessing', assessment={}, lead_identity=None, delegations=()))
        fields = {'provider': provider, 'session_id': 'root', 'agent_id': 'assessor', 'role': 'assessor',
                  'task': 'SYMPHONY_ROLE: assessor', 'model': snapshot_for(provider).tiers['strongest'],
                  'model_reasoning_effort': 'high', 'turn_id' if provider == 'codex' else 'prompt_id': 'assessor-turn'}
        self.state, _ = _observe_delegation(self.state, self.event('subagent_started', **fields), self.environ)
        self.state, _ = _accept_assessment(self.state, self.event('pre_tool_use'), provider,
            {'message': 'SYMPHONY_ROLE: lead'}, Assessment('small', 'simple'))
        self.child('lead', 'lead', False, 'lead-1')
        self.worker()
        return fields

    def test_late_original_assessor_confirms_without_expiring_completed_worker(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                fields = self.late_assessor(provider)
                contract = self.state.active_run.assessment['substantive_contract']
                proofs = self.state.active_run.assessment['_substantive_children']
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                stopped = self.event('subagent_stopped', **fields, status='completed', last_assistant_message=
                    'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple","risk":"normal","rationale":"Confirmed after handback"}')
                if provider == 'claude':
                    stopped = replace(stopped, payload={**stopped.payload, 'prompt_id': 'later-root-context'})
                self.state, _ = _observe_delegation(self.state, stopped, self.environ)
                self.assertEqual(self.state.active_run.assessment['substantive_contract'],
                                 {key: value for key, value in contract.items() if key != 'confirmation'})
                self.assertEqual(self.state.active_run.assessment['_substantive_children'], proofs)
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                before = self.state
                self.state, _ = _observe_delegation(self.state, stopped, self.environ)
                self.assertEqual(self.state, before)
                self.child('lead', 'lead', True, 'lead-1')
                self.state, _ = reduce(self.state, self.event('stop_requested'))
                self.assertIsNone(self.state.active_run)
                self.assertEqual(self.state.recent_runs[-1].status, 'completed')

    def test_assessor_confirmation_is_single_use_not_a_later_terminal_shortcut(self):
        fields = self.late_assessor('claude')
        report = 'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple","risk":"normal"}'
        self.state, _ = _observe_delegation(self.state, self.event('subagent_stopped',
            **{**fields, 'prompt_id': 'first-root-context'}, status='completed', last_assistant_message=report), self.environ)
        contract = self.state.active_run.assessment['substantive_contract']
        self.assertTrue(_substantive_child_completed(self.state.active_run))
        self.state, _ = _observe_delegation(self.state, self.event('subagent_stopped',
            **{**fields, 'prompt_id': 'later-context'}, status='completed', last_assistant_message=report), self.environ)
        self.assertNotEqual(self.state.active_run.assessment['substantive_contract']['epoch'], contract['epoch'])
        self.assertFalse(_substantive_child_completed(self.state.active_run))

    def test_changed_assessment_invocation_or_scope_never_reuses_worker_credit(self):
        for provider in ('codex', 'claude'):
            for case in ('reassess', 'risk', 'profile', 'generation', 'run', 'session', 'provider',
                         'foreign-assessor', 'restarted-assessor', 'foreign-callback-session',
                         'foreign-native-turn', 'predates-start'):
                with self.subTest(provider=provider, case=case):
                    fields = self.late_assessor(provider)
                    original = self.state.active_run.assessment['substantive_contract']
                    sizing = Assessment('small', 'simple', 'high' if case == 'risk' else 'normal')
                    run = self.state.active_run
                    if case == 'reassess': self.state = replace(self.state, needs_reassessment=True)
                    if case == 'generation': self.state = replace(self.state, active_run=replace(run, owner_generation=2))
                    if case == 'run': self.state = replace(self.state, active_run=replace(run, run_id='new-run'))
                    if case == 'session': self.state = replace(self.state, active_run=replace(run, session_id='new-root'))
                    if case == 'provider': self.state = replace(self.state, active_run=replace(run, provider='foreign'))
                    if case == 'profile':
                        recorded = {**run.assessment, 'route': {**run.assessment['route'], 'profile': 'foreign'}}
                        self.state = replace(self.state, active_run=replace(run, assessment=recorded))
                    if case == 'foreign-assessor': fields['agent_id'] = 'foreign'
                    if case == 'foreign-callback-session': fields['session_id'] = 'foreign'
                    if case == 'foreign-native-turn': fields['turn_id'] = 'foreign-native-turn'
                    if case == 'restarted-assessor':
                        self.state, _ = _observe_delegation(self.state, self.event('subagent_started',
                            **{**fields, 'turn_id' if provider == 'codex' else 'prompt_id': 'new-assessor-turn'}), self.environ)
                    source = self.event('subagent_stopped', **fields)
                    if case == 'predates-start': source = replace(source, observed_at='2026-10-01T13:00:00+00:00')
                    self.state, _ = _accept_assessment(self.state, source,
                                                       provider, {}, sizing)
                    self.assertNotEqual(self.state.active_run.assessment['substantive_contract']['epoch'], original['epoch'])
                    self.assertFalse(_substantive_child_completed(self.state.active_run))

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

    def test_missing_lead_receives_the_exact_selected_spawn_packet(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                run = replace(self.state.active_run, lead_identity=None, delegations=(), status='assessed',
                              task='Implement feature A, fix B, and verify both.')
                self.state = replace(self.state, active_run=run)
                reason = _stop_block_reason(run)
                self.assertEqual(reason, {'reason': 'lead_not_started'})
                action = _render_actions((Action('block_stop', reason),), self.state, provider)[0]
                text = action.payload['reason']
                self.assertNotIn('SAME registered lead', text)
                packet = json.loads(text.split('SYMPHONY_LEAD_SPAWN_PACKET: ', 1)[1])
                route = run.assessment['route']
                if provider == 'codex':
                    self.assertEqual(packet['task_name'], 'symphony_lead_' + route['lead_model'].replace('-', '_').replace('.', '_') + '_' + route['lead_effort'])
                    self.assertEqual((packet['model'], packet['reasoning_effort'], packet['fork_turns']),
                                     (route['lead_model'], route['lead_effort'], 'none'))
                    message = packet['message']
                else:
                    self.assertEqual(packet['subagent_type'], f"symphony:symphony-lead-{route['lead_model']}-{route['lead_effort']}")
                    self.assertTrue(packet['run_in_background'])
                    message = packet['prompt']
                self.assertTrue(message.startswith('SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: '))
                self.assertIn(run.task, message)
                self.assertIn('Delegate implementation before editing', message)
                fresh = _render_actions((Action('route_run', {}),), self.state, provider)[0]
                self.assertEqual(json.loads(fresh.payload['text'].split('SYMPHONY_LEAD_SPAWN_PACKET: ', 1)[1]), packet)

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
                if provider == 'claude':
                    # Native reuse retains both prompts; classification repair
                    # clears the invalid result but cannot assign scoped credit.
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.assertIn('with one native prompt', _recovery_guidance(self.state, provider))
                    self.child('lead', 'lead', False, 'lead-2')
                    self.child('fresh-reviewer', 'consultant', False, 'fresh-review')
                    self.child('fresh-reviewer', 'consultant', True, 'fresh-review',
                               report='SYMPHONY_DECISION: {"size":"small","complexity":"simple"}')
                    self.child('lead', 'lead', True, 'lead-2')
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
                    self.assertEqual(self.state.active_run.assessment['_substantive_children']['worker']
                                     ['superseded_by'], 'fresh-worker')
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

    def test_root_parent_and_role_relabeling_never_satisfy_scoped_work(self):
        for provider in ('codex', 'claude'):
            for scenario in ('root-parent', 'consultant-to-worker', 'worker-to-consultant', 'duplicate-start-role'):
                with self.subTest(provider=provider, scenario=scenario):
                    self.begin(provider)
                    if scenario == 'root-parent':
                        self.worker(parent='root')
                    else:
                        start_role = 'worker' if scenario == 'worker-to-consultant' else 'consultant'
                        terminal_role = 'consultant' if start_role == 'worker' else 'worker'
                        self.child('reviewer', start_role, False, 'review-1')
                        admitted = dict(self.state.active_run.assessment['_substantive_children']['reviewer'])
                        if scenario == 'duplicate-start-role':
                            self.child('reviewer', terminal_role, False, 'review-1')
                            proof = self.state.active_run.assessment['_substantive_children']['reviewer']
                            self.assertEqual(proof['role'], start_role)
                            self.assertEqual(proof['start_event_id'], admitted['start_event_id'])
                        self.child('reviewer', terminal_role, True, 'review-1',
                                   report='SYMPHONY_DECISION: {"size":"small","complexity":"simple"}')
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                    self.child('lead', 'lead', True, 'lead-1')
                    self.assertNotEqual(self.state.active_run.status, 'completing')

    def test_reducer_requires_immutable_admitted_role_parent_and_start(self):
        self.begin('codex')
        self.worker()
        run = self.state.active_run
        original = run.assessment['_substantive_children']['worker']
        for key, value in (('role', 'consultant'), ('parent', 'root'), ('start_event_id', 'foreign')):
            proof = {**original, key: value}
            updated = replace(run, assessment={**run.assessment, '_substantive_children': {'worker': proof}})
            self.assertFalse(_substantive_child_completed(updated), key)

    def test_claude_native_parent_binding_can_resolve_only_an_admitted_same_scope_start(self):
        from plugins.symphony.tests.native_child_fixture import write_claude_child_launch
        for scenario in ('valid', 'missing-start', 'new-assessment', 'foreign-parent', 'missing-native',
                         'prompt-context-change', 'explicit-turn-change', 'wrong-role', 'duplicate-launch', 'foreign-cwd', 'foreign-native-parent',
                         'old-launch', 'launch-after-start', 'foreign-session', 'wrong-model', 'wrong-effort',
                         'missing-cwd', 'relative-cwd', 'relative-source-cwd', 'model-override', 'future-prompt',
                         'newer-string-prompt', 'newer-text-block-prompt', 'missing-prompt-id', 'malformed-prompt'):
            with self.subTest(scenario=scenario):
                self.begin('claude')
                run = self.state.active_run
                route = run.assessment['route']
                fields = dict(provider='claude', session_id='root', agent_id='worker', role='worker',
                              cwd=str(self.project), model=route['lead_model'],
                              model_reasoning_effort=route['lead_effort'], prompt_id='worker-turn')
                start = self.event('subagent_started', **fields)
                if scenario != 'missing-start':
                    self.state, _ = _observe_delegation(self.state, start, self.environ)
                    self.assertFalse(_substantive_child_completed(self.state.active_run))
                parent, child = write_claude_child_launch(self.environ['CLAUDE_CONFIG_DIR'], self.project,
                    run, 'worker', 'worker', fields['model'], fields['model_reasoning_effort'], 'worker-turn')
                if scenario == 'new-assessment':
                    self.assess()
                if scenario == 'foreign-parent':
                    fields['parent_thread_id'] = 'root'
                if scenario == 'prompt-context-change':
                    fields['prompt_id'] = 'other-turn'
                if scenario == 'explicit-turn-change':
                    fields['turn_id'] = 'other-turn'
                if scenario == 'wrong-role':
                    fields['role'] = 'consultant'
                if scenario == 'missing-native':
                    child.with_suffix('.meta.json').unlink()
                parent_rows = [json.loads(line) for line in parent.read_text(encoding='utf-8').splitlines()]
                if scenario == 'duplicate-launch':
                    parent_rows.append(parent_rows[-1])
                if scenario == 'foreign-cwd':
                    parent_rows[-1]['cwd'] = str(self.project.parent / 'foreign')
                if scenario == 'missing-cwd':
                    parent_rows[-1].pop('cwd')
                if scenario == 'relative-cwd':
                    parent_rows[-1]['cwd'] = '.'
                if scenario == 'relative-source-cwd':
                    fields['cwd'] = '.'
                if scenario == 'model-override':
                    parent_rows[-1]['message']['content'][0]['input']['model'] = 'foreign-model'
                if scenario == 'foreign-native-parent':
                    parent_rows[-1]['agentId'] = 'root'
                if scenario == 'old-launch':
                    parent_rows[-1]['timestamp'] = '2026-10-01T13:00:00+00:00'
                if scenario == 'launch-after-start':
                    parent_rows[-1]['timestamp'] = '2026-10-01T15:00:00+00:00'
                if scenario == 'foreign-session':
                    parent_rows[-1]['sessionId'] = 'foreign'
                if scenario in {'wrong-model', 'wrong-effort'}:
                    meta = json.loads(child.with_suffix('.meta.json').read_text(encoding='utf-8'))
                    meta['agentType'] = 'symphony:symphony-worker-foreign-low'
                    if scenario == 'wrong-effort':
                        meta['agentType'] = f"symphony:symphony-worker-{fields['model']}-max"
                    child.with_suffix('.meta.json').write_text(json.dumps(meta), encoding='utf-8')
                parent.write_text(''.join(json.dumps(row) + '\n' for row in parent_rows), encoding='utf-8')
                child_rows = [json.loads(line) for line in child.read_text(encoding='utf-8').splitlines()]
                if scenario == 'future-prompt':
                    child_rows[-1]['timestamp'] = '2099-01-01T00:00:00+00:00'
                if scenario in {'newer-string-prompt', 'newer-text-block-prompt'}:
                    child_rows.append({**child_rows[-1], 'uuid': 'newer-unfinished-prompt',
                        'message': {'content': 'New objective' if scenario == 'newer-string-prompt'
                                    else [{'type': 'text', 'text': 'New objective'}]}})
                if scenario == 'missing-prompt-id':
                    child_rows[-1].pop('uuid')
                if scenario == 'malformed-prompt':
                    child_rows[-1]['message']['content'] = [{'type': 'text', 'text': 1}]
                child.write_text(''.join(json.dumps(row) + '\n' for row in child_rows), encoding='utf-8')
                terminal = self.event('subagent_stopped', **fields, status='completed')
                self.state, _ = _observe_delegation(self.state, terminal, self.environ)
                self.assertEqual(_substantive_child_completed(self.state.active_run), scenario in {'valid', 'prompt-context-change'})
                if scenario in {'valid', 'prompt-context-change'}:
                    proof = self.state.active_run.assessment['_substantive_children']['worker']
                    self.assertEqual(proof['start_event_id'], start.event_id)
                    self.assertEqual(proof['parent'], 'lead')
                    self.child('lead', 'lead', True, 'lead-1')
                    self.state, _ = reduce(self.state, self.event('stop_requested'))
                    self.assertIsNone(self.state.active_run)

    def test_first_tokenless_invocation_uses_accepted_start_and_reused_start_stays_ambiguous(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.begin(provider)
                fields = {'provider': provider, 'session_id': 'root', 'agent_id': 'tokenless-worker',
                          'cwd': str(self.project), 'parent_thread_id': 'lead',
                          'role': 'worker', 'task': 'SYMPHONY_ROLE: worker'}
                if provider == 'claude':
                    from plugins.symphony.tests.native_child_fixture import write_claude_child_launch
                    route = self.state.active_run.assessment['route']
                    fields.update(model=route['lead_model'], model_reasoning_effort=route['lead_effort'])
                    write_claude_child_launch(self.environ['CLAUDE_CONFIG_DIR'], self.project,
                        self.state.active_run, 'tokenless-worker', 'worker', fields['model'],
                        fields['model_reasoning_effort'], 'tokenless')
                start = self.event('subagent_started', **fields)
                self.state, _ = _observe_delegation(self.state, start, self.environ)
                terminal = self.event('subagent_stopped', **fields, status='completed')
                self.state, _ = _observe_delegation(self.state, terminal, self.environ)
                self.assertTrue(_substantive_child_completed(self.state.active_run))
                self.assertEqual(self.state.active_run.assessment['_substantive_children']['tokenless-worker']['turn'], '')
                self.child('lead', 'lead', True, 'lead-1')
                self.state, _ = _observe_delegation(self.state, start)
                self.state, _ = _observe_delegation(self.state, terminal)
                self.assertFalse(_substantive_child_completed(self.state.active_run))
                self.assertIn('tokenless-worker', self.state.active_run.assessment['_ambiguous_child_starts'])

    def test_claude_prompt_context_changes_require_exact_immutable_native_invocation(self):
        for scenario in ('changed', 'start-absent', 'stop-absent', 'duplicate-start',
                         'explicit-start', 'explicit-stop', 'explicit-conflict', 'unknown-kind',
                         'role-change', 'foreign-parent', 'missing-native', 'changed-launch',
                         'reused-native', 'before-start'):
            with self.subTest(scenario=scenario):
                self.begin('claude')
                start_fields = ({'prompt_id': None} if scenario == 'start-absent' else
                                {'turn_id': 'original'} if scenario in {'explicit-start', 'explicit-conflict'} else {})
                self.child('worker', 'worker', False, 'root-request', hook_fields=start_fields)
                run = self.state.active_run
                original = dict(run.assessment['_substantive_children']['worker'])
                fields = dict(provider='claude', session_id='root', agent_id='worker',
                              cwd=str(self.project), role='worker', prompt_id='later-request')
                proofs = dict(run.assessment['_substantive_children'])
                if scenario == 'unknown-kind':
                    modified = dict(original)
                    modified.pop('turn_kind')
                    proofs['worker'] = modified
                    self.state = replace(self.state, active_run=replace(run, assessment={
                        **run.assessment, '_substantive_children': proofs}))
                    original = modified
                if scenario == 'stop-absent':
                    fields.pop('prompt_id')
                if scenario in {'explicit-stop', 'explicit-conflict'}:
                    fields['turn_id'] = 'different'
                if scenario == 'duplicate-start':
                    self.state, _ = _observe_delegation(self.state, self.event('subagent_started', **fields), self.environ)
                    self.assertEqual(self.state.active_run.assessment['_substantive_children']['worker'], original)
                if scenario == 'role-change':
                    fields['role'] = 'consultant'
                if scenario == 'foreign-parent':
                    fields['parent_thread_id'] = 'root'
                child = next(Path(self.environ['CLAUDE_CONFIG_DIR']).glob('projects/*/root/subagents/agent-worker.jsonl'))
                if scenario == 'missing-native':
                    child.with_suffix('.meta.json').unlink()
                if scenario == 'changed-launch':
                    meta = json.loads(child.with_suffix('.meta.json').read_text(encoding='utf-8'))
                    meta['toolUseId'] = 'foreign-launch'
                    child.with_suffix('.meta.json').write_text(json.dumps(meta), encoding='utf-8')
                if scenario == 'reused-native':
                    rows = [json.loads(line) for line in child.read_text(encoding='utf-8').splitlines()]
                    rows.append({**rows[0], 'uuid': 'new-native-invocation'})
                    child.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                terminal = self.event('subagent_stopped', **fields, status='completed')
                if scenario == 'before-start':
                    terminal = replace(terminal, observed_at='2026-10-01T14:00:02+00:00')
                self.state, _ = _observe_delegation(self.state, terminal, self.environ)
                accepted = scenario in {'changed', 'start-absent', 'stop-absent', 'duplicate-start'}
                self.assertEqual(_substantive_child_completed(self.state.active_run), accepted)
                proof = self.state.active_run.assessment['_substantive_children']['worker']
                for key in ('turn', 'turn_kind', 'start_event_id', 'admitted_at', 'role',
                            'epoch', 'run_id', 'lead', 'owner_generation'):
                    self.assertEqual(proof.get(key), original.get(key), key)
                if accepted:
                    self.assertEqual(self.state.terminal_receipts[-1]['turn'],
                                     'prompt_id:later-request' if scenario != 'stop-absent' else '')
                    self.child('lead', 'lead', True, 'lead-1')
                    self.state, _ = reduce(self.state, self.event('stop_requested'))
                    self.assertIsNone(self.state.active_run)

    def test_native_turn_ids_and_untyped_stored_proofs_cannot_bridge_prompt_contexts(self):
        from plugins.symphony.symphony.runtime import _substantive_turn_matches
        for provider in ('claude', 'codex'):
            self.assertTrue(_substantive_turn_matches(provider, 'turn_id:own', 'turn_id:own', 'turn_id', 'turn_id'))
            self.assertFalse(_substantive_turn_matches(provider, 'turn_id:old', 'turn_id:new', 'turn_id', 'turn_id'))
            self.assertFalse(_substantive_turn_matches(provider, '', '', 'none', 'turn_id'))
            for kind in (None, [], {}, 'foreign'):
                self.assertFalse(_substantive_turn_matches(provider, 'prompt_id:old', 'prompt_id:new', kind, 'prompt_id'))
        self.assertFalse(_substantive_turn_matches('codex', 'prompt_id:old', 'prompt_id:new', 'prompt_id', 'prompt_id'))

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
