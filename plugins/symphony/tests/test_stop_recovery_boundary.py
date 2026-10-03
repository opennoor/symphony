"""Public hook liveness is separate from the core's strict completion proof."""
import json
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony import PLUGIN_VERSION
from plugins.symphony.scripts.package_smoke import _materialize, _send_raw
from plugins.symphony.symphony.adapters import HookResult
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore, _local_lock, state_lock_budget


class StopRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'private-project'
        self.project.mkdir()
        self.store = StateStore(self.base / 'state')

    def fixture(self, provider, session='private-session'):
        run = RunState('private-run', 'private-objective', provider=provider, session_id=session,
                       status='working', lead_identity='private-lead',
                       delegations=(Delegation('private-lead', 'lead', 'private-objective', 'working',
                                               'gpt-6-luna' if provider == 'codex' else 'claude-sonnet-5', 'medium'),))
        state = ProjectState(enabled=True, active_runs={f'{provider}:{session}': run})
        self.store.save(self.project, state)
        self.store.bind_session(provider, session, self.store._path(self.project), False,
                                self.project, session)
        payload = {'session_id': session, 'cwd': str(self.project), 'hook_event_name': 'Stop'}
        env = {'SYMPHONY_STATE_DIR': str(self.store.root), 'SYMPHONY_PROVIDER': provider,
               'SYMPHONY_PROFILE': 'full', 'SYMPHONY_REPORT_WORKER': '0'}
        return state, payload, env

    def diagnostic(self):
        paths = list((self.store.root / 'diagnostics').glob('*.json'))
        self.assertTrue(paths)
        reports = [json.loads(path.read_text()) for path in paths]
        for report in reports:
            self.assertEqual({'schema', 'plugin_version', 'provider', 'platform', 'category',
                              'outcome', 'day', 'occurrences'}, set(report))
            text = json.dumps(report)
            for secret in ('private-session', 'private-run', 'private-objective', 'private-project',
                           'private-lead', 'private-secret', str(self.base)):
                self.assertNotIn(secret, text)
        return reports

    def test_unowned_results_end_turn_quietly_without_ack_or_completion_on_both_providers(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                state, payload, env = self.fixture(provider)
                event = Event('private-event', 'subagent_stopped', '2026-10-03T12:00:00+00:00',
                    {'provider': provider, 'session_id': payload['session_id'], 'agent_id': 'private-orphan',
                     'parent_thread_id': payload['session_id'], 'last_assistant_message': 'private-secret'})
                self.store.queue_session_event(provider, payload['session_id'], event, ambiguous_owner=True)
                before = self.store.session_record(provider, payload['session_id'])['pending']
                for _ in range(4):
                    self.assertEqual('', handle(payload, env).stdout)
                self.assertEqual(before, self.store.session_record(provider, payload['session_id'])['pending'])
                actual = self.store.load(self.project)
                self.assertFalse(actual.recent_runs)
                self.assertFalse(actual.terminal_receipts)
                self.assertIsNone(actual.active_runs[f'{provider}:{payload["session_id"]}'].outcome)
                self.assertEqual(state.active_runs[f'{provider}:{payload["session_id"]}'].delegations,
                                 actual.active_runs[f'{provider}:{payload["session_id"]}'].delegations)
        self.assertTrue(all(report['category'] == 'bookkeeping' for report in self.diagnostic()))

    def test_active_work_has_one_stop_recovery_attempt_per_user_turn_without_host_retry_flag(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                _, payload, env = self.fixture(provider)
                first = json.loads(handle(payload, env).stdout)
                self.assertEqual('block', first['decision'])
                for _ in range(4):
                    self.assertEqual('', handle(payload, env).stdout)
                handle({**payload, 'hook_event_name': 'UserPromptSubmit',
                        'prompt': '$symphony:symphony status' if provider == 'codex' else '/symphony:status'}, env)
                self.assertEqual('block', json.loads(handle(payload, env).stdout)['decision'])
                state = self.store.load(self.project)
                self.assertFalse(state.recent_runs)
                self.assertIsNone(state.active_runs[f'{provider}:private-session'].outcome)

    def test_foreign_pending_result_cannot_hold_an_already_proven_current_completion(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                state, payload, env = self.fixture(provider)
                key = f'{provider}:private-session'
                original = state.active_runs[key]
                completed = replace(original, status='completing', outcome={'status': 'completed'},
                    delegations=tuple(replace(item, state='completed') for item in original.delegations))
                self.store.save(self.project, replace(state, active_runs={key: completed}))
                event = Event('foreign-result', 'subagent_stopped', '2026-10-03T12:00:00+00:00',
                    {'provider': provider, 'session_id': 'private-session', 'agent_id': 'foreign-child',
                     'parent_thread_id': 'foreign-root', 'last_assistant_message': 'private-secret'})
                self.store.queue_session_event(provider, 'private-session', event, ambiguous_owner=True)
                pending = self.store.session_record(provider, 'private-session')['pending']
                # The existing completion is a legacy, already recorded one;
                # there is no later native turn in this focused ownership test.
                with patch('plugins.symphony.symphony.runtime.' + provider + '_recovered_lead_event', return_value=None), \
                     patch('plugins.symphony.symphony.runtime.' + provider + '_completing_lead_turn', return_value=('none', None)):
                    self.assertEqual('', handle(payload, env).stdout)
                actual = self.store.load(self.project)
                self.assertNotIn(key, actual.active_runs)
                self.assertEqual('completed', actual.recent_runs[-1].status)
                self.assertEqual(completed.delegations, actual.recent_runs[-1].delegations)
                self.assertEqual(pending, self.store.session_record(provider, 'private-session')['pending'])

    def test_root_parented_unknown_result_still_withholds_current_completion_credit(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                state, payload, env = self.fixture(provider)
                key = f'{provider}:private-session'
                run = replace(state.active_runs[key], status='completing', outcome={'status': 'completed'},
                    delegations=tuple(replace(item, state='completed') for item in state.active_runs[key].delegations))
                self.store.save(self.project, replace(state, active_runs={key: run}))
                event = Event('missing-launch', 'subagent_stopped', '2026-10-03T12:00:00+00:00',
                    {'provider': provider, 'session_id': 'private-session', 'agent_id': 'unproved-child',
                     'parent_thread_id': 'private-session', 'last_assistant_message': 'private-secret'})
                self.store.queue_session_event(provider, 'private-session', event, ambiguous_owner=True)
                self.assertEqual('', handle(payload, env).stdout)
                self.assertIn(key, self.store.load(self.project).active_runs)
                self.assertFalse(self.store.load(self.project).recent_runs)
                self.assertEqual(1, len(self.store.session_record(provider, 'private-session')['pending']))

    def test_alias_storage_fault_does_not_lock_known_root_discovery_tools(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                _, payload, env = self.fixture(provider)
                with patch.object(StateStore, 'aliases_for_owner', side_effect=OSError('private-secret')):
                    result = handle({**payload, 'hook_event_name': 'PreToolUse', 'tool_name': 'Read',
                                     'tool_input': {'file_path': str(self.project / 'README.md')}}, env)
                response = json.loads(result.stdout or '{}')
                self.assertNotEqual('block', response.get('decision'))
                self.assertNotEqual('deny', response.get('hookSpecificOutput', {}).get('permissionDecision'))
                actual = self.store.load(self.project)
                self.assertFalse(actual.recent_runs)
                self.assertIsNone(actual.active_runs[f'{provider}:private-session'].outcome)

    def test_retained_overflow_does_not_lock_known_root_tools_or_grant_success(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                _, payload, env = self.fixture(provider)
                record = self.store.session_record(provider, 'private-session')
                record['overflow'] = True
                self.store._write_json(self.store._session_path(provider, 'private-session'), record)
                result = handle({**payload, 'hook_event_name': 'PreToolUse', 'tool_name': 'Read',
                                 'tool_input': {'file_path': str(self.project / 'README.md')}}, env)
                output = json.loads(result.stdout or '{}')
                self.assertNotEqual('block', output.get('decision'))
                self.assertNotEqual('deny', output.get('hookSpecificOutput', {}).get('permissionDecision'))
                self.assertEqual('', handle(payload, env).stdout)
                self.assertTrue(self.store.session_record(provider, 'private-session')['overflow'])
                actual = self.store.load(self.project)
                self.assertFalse(actual.recent_runs)
                self.assertIsNone(actual.active_runs[f'{provider}:private-session'].outcome)

    def test_unexpected_stop_faults_are_quiet_and_never_change_managed_evidence(self):
        for provider in ('codex', 'claude'):
            for error in (OSError('private-secret'), ValueError('private-secret'), RuntimeError('private-secret')):
                with self.subTest(provider=provider, error=type(error).__name__):
                    _, payload, env = self.fixture(provider)
                    before = self.store._path(self.project).read_bytes()
                    with patch('plugins.symphony.symphony.runtime._handle_core', side_effect=error):
                        self.assertEqual('', handle(payload, env).stdout)
                    self.assertEqual(before, self.store._path(self.project).read_bytes())
        self.assertTrue(all(report['outcome'] == 'deferred' for report in self.diagnostic()))

    def test_malformed_stop_entry_metadata_cannot_block_or_claim_completion(self):
        for provider in ('codex', 'claude'):
            _, payload, env = self.fixture(provider)
            before = self.store._path(self.project).read_bytes()
            self.assertEqual('', handle({**payload, 'cwd': 123}, env).stdout)
            self.assertEqual(before, self.store._path(self.project).read_bytes())
            with self.assertRaises(TypeError):
                handle({**payload, 'cwd': 123, 'hook_event_name': 'PreToolUse'}, env)

    def test_non_stop_faults_and_explicit_stop_controls_are_not_silently_accepted(self):
        _, payload, env = self.fixture('codex')
        for hook in ('UserPromptSubmit', 'PreToolUse', 'SubagentStart', 'SubagentStop'):
            with self.subTest(hook=hook):
                with patch('plugins.symphony.symphony.runtime._handle_core', side_effect=OSError('fault')):
                    with self.assertRaises(OSError):
                        handle({**payload, 'hook_event_name': hook}, env)
        explicit = {**payload, 'hook_event_name': 'UserPromptSubmit', 'prompt': '$symphony:symphony stop'}
        blocked = HookResult(json.dumps({'decision': 'block', 'reason': 'unreconciled'}), 'bookkeeping')
        with patch('plugins.symphony.symphony.runtime._handle_core', return_value=blocked):
            self.assertEqual(blocked, handle(explicit, env))

    def test_stop_state_lock_wait_is_bounded_and_does_not_drop_any_state(self):
        _, payload, env = self.fixture('codex')
        path = self.store._path(self.project)
        before = path.read_bytes()
        lock = _local_lock(path)
        lock.acquire()
        try:
            began = time.monotonic()
            with state_lock_budget(0.04):
                self.assertEqual('', handle(payload, env).stdout)
            self.assertLess(time.monotonic() - began, 0.5)
        finally:
            lock.release()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual('state_io', self.diagnostic()[0]['category'])

    def test_parallel_roots_do_not_consume_each_others_recovery_budget(self):
        state, payload, env = self.fixture('codex', 'root-a')
        second = replace(state.active_runs['codex:root-a'], session_id='root-b', run_id='second-run',
                         lead_identity='second-lead', delegations=(Delegation('second-lead', 'lead', '', 'working',
                                                                          'gpt-6-luna', 'medium'),))
        self.store.save(self.project, replace(state, active_runs={**state.active_runs, 'codex:root-b': second}))
        self.store.bind_session('codex', 'root-b', self.store._path(self.project), False, self.project, 'root-b')
        for session in ('root-a', 'root-b'):
            first = handle({**payload, 'session_id': session}, env)
            self.assertEqual('block', json.loads(first.stdout)['decision'])
        for session in ('root-a', 'root-b'):
            self.assertEqual('', handle({**payload, 'session_id': session}, env).stdout)
        self.assertEqual(2, len(self.store.load(self.project).active_runs))

    def test_diagnostic_storage_failure_cannot_lock_the_host_turn(self):
        _, payload, env = self.fixture('codex')
        with patch('plugins.symphony.symphony.runtime._handle_core', side_effect=RuntimeError('fault')):
            with patch.object(StateStore, 'record_recovery_diagnostic', side_effect=OSError('read only')):
                self.assertEqual('', handle(payload, env).stdout)

    def test_diagnostics_reject_unapproved_fields_and_opaque_values(self):
        valid = {'schema': 1, 'plugin_version': '1.7.5', 'provider': 'codex', 'platform': 'linux',
                 'category': 'bookkeeping', 'outcome': 'deferred'}
        for report in ({**valid, 'prompt': 'private-secret'}, {**valid, 'category': 'private-secret'}):
            with self.assertRaises(ValueError):
                self.store.record_recovery_diagnostic(report)
        self.assertFalse((self.store.root / 'diagnostics').exists())
        self.store.record_recovery_diagnostic(valid)
        self.store.record_recovery_diagnostic(valid)
        self.assertEqual(2, self.diagnostic()[0]['occurrences'])

    def test_public_boundary_keeps_tool_and_routing_denials_intact(self):
        _, payload, env = self.fixture('claude')
        denied = HookResult(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': 'assessment required'}}))
        for hook in ('PreToolUse', 'SubagentStart', 'UserPromptSubmit'):
            with self.subTest(hook=hook):
                with patch('plugins.symphony.symphony.runtime._handle_core', return_value=denied):
                    self.assertEqual(denied, handle({**payload, 'hook_event_name': hook}, env))

    def test_verified_installed_hooks_defer_malformed_bookkeeping_quietly_on_both_providers(self):
        source = Path(__file__).resolve().parents[1]
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                home = self.base / ('installed-' + provider)
                home.mkdir()
                root = _materialize(source, home, PLUGIN_VERSION)
                state_dir = home / 'state'
                store = StateStore(state_dir)
                payload = {'session_id': '01a101b2-e6f0-7161-a8e0-a55845e50c12',
                           'provider': provider, 'cwd': str(self.project),
                           'hook_event_name': 'SessionStart', 'source': 'startup'}
                _send_raw(root, provider, 'SessionStart', state_dir, payload)
                path = store._session_path(provider, payload['session_id'])
                raw = json.loads(path.read_text())
                raw['generation'] = 'private-secret'
                path.write_text(json.dumps(raw))
                original = path.read_bytes()
                result = _send_raw(root, provider, 'Stop', state_dir,
                                   {**payload, 'hook_event_name': 'Stop'})
                self.assertIsNone(result)
                self.assertEqual(original, path.read_bytes())
                report = json.loads(next((state_dir / 'diagnostics').glob('*.json')).read_text())
                self.assertEqual('state_shape', report['category'])
                self.assertNotIn('private-secret', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
