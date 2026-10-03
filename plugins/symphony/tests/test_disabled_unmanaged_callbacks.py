"""Disabled Symphony must not impose managed completion on ordinary agents."""

import json
import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.scripts.package_smoke import _materialize, _send_raw
from plugins.symphony.symphony import PLUGIN_VERSION
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import _root_admission_key, handle
from plugins.symphony.symphony.store import StateStore


class DisabledUnmanagedCallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'checkout with spaces'
        self.project.mkdir()
        self.store = StateStore(Path(self.temp.name) / 'state')
        self.fixture = json.loads((Path(__file__).parent / 'fixtures' /
                                   'disabled-unmanaged-review-codex.json').read_text())
        self.session = self.fixture['session']
        self.events = tuple(Event(item['event_id'], item['kind'], item['observed_at'], item['payload'])
                            for item in self.fixture['pending'])
        self.state = ProjectState(enabled=False)
        self.env = {'SYMPHONY_STATE_DIR': str(self.store.root), 'SYMPHONY_PROVIDER': 'codex'}

    def bind(self, state=None, provider='codex'):
        self.store.save(self.project, state or self.state)
        record = self.store.bind_session(provider, self.session, self.store._path(self.project),
                                         False, self.project, self.session)
        record.update(pending=[], retired_agents=[], overflow=False)
        self.store._write_json(self.store._session_path(provider, self.session), record)

    def hook(self, name='Stop', event=None, provider='codex'):
        payload = {'hook_event_name': name, 'session_id': self.session,
                   'cwd': str(self.project), 'stop_hook_active': False}
        if event is not None:
            payload.update(event.payload)
        return handle(payload, {**self.env, 'SYMPHONY_PROVIDER': provider})

    def archives(self):
        return [json.loads(path.read_text()) for path in
                (self.store.root / 'unmanaged-callbacks').glob('*.json')]

    def queue(self, events=None, provider='codex'):
        for event in events or self.events:
            self.store.queue_session_event(provider, self.session, event, ambiguous_owner=True)

    def test_captured_two_review_turns_do_not_block_stop_or_claim_completion(self):
        self.bind()
        self.queue()
        result = self.hook()
        self.assertNotIn('"decision": "block"', result.stdout)
        self.assertEqual(self.store.session_record('codex', self.session)['pending'], [])
        state = self.store.load(self.project)
        self.assertFalse(state.enabled)
        self.assertEqual(state.active_runs, {})
        self.assertEqual(state.recent_runs, ())
        self.assertEqual(state.terminal_receipts, ())
        archived = {item['event']['event_id']: item for item in self.archives()}
        self.assertEqual(set(archived), {event.event_id for event in self.events})
        for event in self.events:
            self.assertEqual(archived[event.event_id]['event']['payload'], event.payload)
            self.assertEqual(archived[event.event_id]['disposition'], 'outside_disabled_governance')

    def test_live_start_and_two_terminals_stay_outside_managed_inbox_on_both_hosts(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.bind(provider=provider)
                events = tuple(replace(event, payload={**event.payload, 'provider': provider,
                    'agent_type': 'default' if provider == 'codex' else 'general-purpose'})
                    for event in self.events)
                start = replace(events[0], event_id='ordinary-review-start', kind='subagent_started',
                                payload={k: v for k, v in events[0].payload.items()
                                         if k != 'last_assistant_message'})
                self.hook('SubagentStart', start, provider)
                for event in events:
                    self.hook('SubagentStop', event, provider)
                self.assertEqual(self.store.session_record(provider, self.session)['pending'], [])
                self.assertNotIn('"decision": "block"', self.hook(provider=provider).stdout)

    def test_enabled_session_still_retains_unowned_results(self):
        self.bind(replace(self.state, enabled=True))
        self.queue()
        self.assertIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(len(self.store.session_record('codex', self.session)['pending']), 2)
        self.assertEqual(self.archives(), [])

    def test_managed_role_or_identity_or_live_run_is_never_exempted(self):
        identity = self.events[0].payload['agent_id']
        run = RunState('run', 'managed task', 'working', session_id=self.session,
                       provider='codex', lead_identity=identity,
                       delegations=(Delegation(identity, 'lead', 'task', 'working', 'model', 'high'),))
        variants = (
            (self.state, replace(self.events[0], payload={**self.events[0].payload,
                'task_name': 'symphony_lead_gpt_6_1_sol_high'})),
            (self.state, replace(self.events[0], payload={**self.events[0].payload,
                'last_assistant_message': 'SYMPHONY_OUTCOME: {"status":"completed"}'})),
            (replace(self.state, active_run=run, active_runs={'codex:' + self.session: run}), self.events[0]),
            (replace(self.state, recent_runs=(replace(run, status='completed'),)), self.events[0]),
            (replace(self.state, terminal_receipts=({'provider': 'codex', 'session': self.session,
                'agent': identity, 'run_id': 'old-run', 'turn': 'old-turn', 'result': 'old-result',
                'parent': self.session, 'lead': identity},)), self.events[0]),
        )
        for state, event in variants:
            with self.subTest(state=state, event=event.event_id):
                self.bind(state)
                self.queue((event,))
                self.assertIn('"decision": "block"', self.hook().stdout)
                self.assertEqual(self.archives(), [])

    def test_foreign_parent_remains_pending(self):
        self.bind()
        event = replace(self.events[0], payload={**self.events[0].payload, 'parent_thread_id': 'foreign'})
        self.queue((event,))
        self.assertIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(self.archives(), [])

    def test_future_generation_and_retired_child_remain_pending(self):
        for future in (True, False):
            with self.subTest(future=future):
                self.bind()
                self.queue()
                record = self.store.session_record('codex', self.session)
                if future:
                    for item in record['pending']:
                        item['generation'] = record['generation'] + 1
                else:
                    record['retired_agents'] = [self.events[0].payload['agent_id']]
                self.store._write_json(self.store._session_path('codex', self.session), record)
                self.assertIn('"decision": "block"', self.hook().stdout)
                self.assertEqual(self.archives(), [])

    def test_claude_pending_admission_is_not_exempted(self):
        event = replace(self.events[0], payload={**self.events[0].payload,
            'provider': 'claude', 'agent_type': 'general-purpose'})
        key = _root_admission_key('claude', {'session_id': self.session})
        self.bind(replace(self.state, configuration={'root_admission_intents': {key: {'version': 1}}}),
                  'claude')
        self.queue((event,), 'claude')
        self.assertIn('"decision": "block"', self.hook(provider='claude').stdout)
        self.assertEqual(self.archives(), [])

    def test_archive_failure_keeps_large_live_source_outside_managed_inbox(self):
        self.bind()
        event = replace(self.events[0], payload={**self.events[0].payload,
            'last_assistant_message': 'Ordinary review finding.\n' * 8000})
        write = StateStore._write_json
        def fail_archive(path, value):
            if path.parent.name == 'unmanaged-callbacks':
                raise OSError('archive unavailable')
            return write(path, value)
        with patch.object(StateStore, '_write_json', side_effect=fail_archive):
            self.hook('SubagentStop', event)
        record = self.store.session_record('codex', self.session)
        self.assertEqual(record['pending'], [])
        self.assertFalse(record['overflow'])
        recovered = list((self.store.root / 'unmanaged-recovery').glob('*.json'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(json.loads(recovered[0].read_text())['event']['payload']['last_assistant_message'],
                         event.payload['last_assistant_message'])
        self.assertEqual(self.archives(), [])
        self.assertNotIn('"decision": "block"', self.hook().stdout)

    def test_retention_limits_full_reports_but_keeps_replay_facts_after_enable(self):
        self.bind()
        self.queue()
        with patch('plugins.symphony.symphony.store._UNMANAGED_REPORT_COUNT', 1):
            self.hook()
        self.assertEqual(len(self.archives()), 1)
        pruned = next(event for event in self.events if event.event_id !=
                      self.archives()[0]['event']['event_id'])
        self.store.save(self.project, replace(self.store.load(self.project), enabled=True))
        self.queue((pruned,))
        self.assertNotIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(self.store.session_record('codex', self.session)['pending'], [])
        self.assertEqual(self.store.load(self.project).terminal_receipts, ())

    def test_retention_byte_budget_preserves_unacknowledged_reports(self):
        self.bind()
        self.queue()
        for event in self.events:
            self.store.preserve_unmanaged_callback('codex', self.session, self.project, 1, event)
        record = self.store.session_record('codex', self.session)
        with patch('plugins.symphony.symphony.store._UNMANAGED_REPORT_BYTES', 1):
            self.store.finish_session_events(record, {self.events[0].event_id})
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.archives()[0]['event']['event_id'], self.events[1].event_id)
        self.assertEqual(len(self.store.session_record('codex', self.session)['pending']), 1)

    def test_native_retry_after_pruning_matches_identity_with_new_observed_time(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.bind(provider=provider)
                payload = {**self.events[0].payload, 'hook_event_name': 'SubagentStop',
                    'provider': provider, 'cwd': str(self.project),
                    'agent_type': 'default' if provider == 'codex' else 'general-purpose'}
                original = event_from_payload(provider, payload)
                self.queue((original,), provider)
                with patch('plugins.symphony.symphony.store._UNMANAGED_REPORT_COUNT', 0):
                    self.hook(provider=provider)
                self.assertEqual(self.archives(), [])
                self.store.save(self.project, replace(self.store.load(self.project), enabled=True))
                retry = event_from_payload(provider, payload)
                self.assertEqual(retry.event_id, original.event_id)
                self.assertNotEqual(retry.observed_at, original.observed_at)
                self.queue((retry,), provider)
                self.assertNotIn('"decision": "block"', self.hook(provider=provider).stdout)
                self.assertEqual(self.store.session_record(provider, self.session)['pending'], [])
                self.assertEqual(self.store.load(self.project).terminal_receipts, ())
                altered = replace(retry, payload={**retry.payload,
                    'last_assistant_message': 'A different report must not inherit the observation.'})
                self.queue((altered,), provider)
                self.assertIn('"decision": "block"', self.hook(provider=provider).stdout)

    def test_live_provider_payload_is_complete_and_retries_after_enable(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.bind(provider=provider)
                payload = {**self.events[0].payload, 'hook_event_name': 'SubagentStop',
                    'session_id': self.session, 'provider': provider, 'cwd': str(self.project),
                    'stop_hook_active': False, 'future_provider_field': {'values': [1, 'review']},
                    'agent_type': 'default' if provider == 'codex' else 'general-purpose'}
                original = event_from_payload(provider, payload)
                self.hook('SubagentStop', original, provider)
                archived = next(item for item in self.archives() if item['provider'] == provider)
                expected = json.loads(json.dumps(original.payload))
                self.assertEqual(archived['event']['payload'], expected)
                self.assertNotIn('_symphony_owner_conflict', archived['event']['payload'])
                self.assertNotIn('_symphony_verified_alias', archived['event']['payload'])
                self.store.save(self.project, replace(self.store.load(self.project), enabled=True))
                self.assertNotIn('"decision": "block"',
                                 self.hook('SubagentStop', original, provider).stdout)
                self.assertNotIn('"decision": "block"', self.hook(provider=provider).stdout)
                self.assertEqual(self.store.session_record(provider, self.session)['pending'], [])

    def test_ack_write_failure_protects_full_reports_even_after_facts_commit(self):
        self.bind()
        self.queue()
        session_path = self.store._session_path('codex', self.session)
        write = StateStore._write_json
        def fail_ack(path, value):
            if path == session_path:
                raise OSError('ACK unavailable')
            return write(path, value)
        with patch('plugins.symphony.symphony.store._UNMANAGED_REPORT_COUNT', 0):
            with patch.object(StateStore, '_write_json', side_effect=fail_ack):
                with self.assertRaises(OSError):
                    self.hook()
            self.assertEqual(len(self.archives()), 2)
            record = self.store.session_record('codex', self.session)
            _, index = self.store._unmanaged_observations(
                'codex', self.session, hashlib.sha256(
                    os.path.normcase(str(self.project.resolve())).encode()).hexdigest(), 1)
            self.assertTrue(all(fact['acknowledged'] for fact in index['entries'].values()))
            self.store._prune_unmanaged_reports(index, {item['event_id'] for item in record['pending']})
            self.assertEqual(len(self.archives()), 2)
            self.store.save(self.project, replace(self.store.load(self.project), enabled=True))
            self.assertNotIn('"decision": "block"', self.hook().stdout)
            self.assertEqual(self.archives(), [])
            self.assertEqual(self.store.load(self.project).terminal_receipts, ())

    def test_large_ordinary_result_does_not_overflow_the_managed_queue(self):
        self.bind()
        event = replace(self.events[0], payload={**self.events[0].payload,
            'last_assistant_message': 'Ordinary review finding.\n' * 8000})
        self.hook('SubagentStop', event)
        record = self.store.session_record('codex', self.session)
        self.assertEqual(record['pending'], [])
        self.assertFalse(record['overflow'])
        self.assertNotIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(self.archives()[0]['event']['payload']['last_assistant_message'],
                         event.payload['last_assistant_message'])

    def test_commit_before_ack_can_replay_after_enable_without_claiming_a_new_run(self):
        self.bind()
        self.queue()
        with patch.object(StateStore, 'finish_session_events', side_effect=OSError('before ACK')):
            with self.assertRaises(OSError):
                self.hook()
        state = self.store.load(self.project)
        self.store.save(self.project, replace(state, enabled=True))
        self.assertNotIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(self.store.session_record('codex', self.session)['pending'], [])
        self.assertEqual(self.store.load(self.project).terminal_receipts, ())

    def test_parallel_roots_share_checkout_without_borrowing_managed_work(self):
        other = 'other-root'
        run = RunState('live-run', 'managed task', 'working', session_id=other,
                       provider='codex', lead_identity='other-lead',
                       delegations=(Delegation('other-lead', 'lead', 'task', 'working', 'model', 'high'),))
        self.bind(replace(self.state, active_run=run, active_runs={'codex:' + other: run}))
        self.store.bind_session('codex', other, self.store._path(self.project), False, self.project, other)
        self.queue()
        def other_stop():
            return handle({'hook_event_name': 'Stop', 'session_id': other,
                           'cwd': str(self.project)}, self.env)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ordinary = pool.submit(self.hook)
            managed = pool.submit(other_stop)
            self.assertNotIn('"decision": "block"', ordinary.result().stdout)
            self.assertIn('"decision": "block"', managed.result().stdout)
        self.assertEqual(self.store.session_record('codex', self.session)['pending'], [])
        self.assertEqual(self.store.load(self.project).active_runs['codex:' + other].lead_identity,
                         'other-lead')

    def test_child_worktree_cwd_cannot_change_preserved_root_project(self):
        self.bind()
        child_worktree = Path(self.temp.name) / 'separate-worktree'
        child_worktree.mkdir()
        payload = {**self.events[0].payload, 'hook_event_name': 'SubagentStop',
                   'cwd': str(child_worktree)}
        self.assertNotIn('"decision": "block"', handle(payload, self.env).stdout)
        record = self.archives()[0]
        self.assertEqual(record['project'], hashlib.sha256(
            os.path.normcase(str(self.project.resolve())).encode()).hexdigest())
        if os.name != 'nt':
            for path in (self.store.root / 'unmanaged-callbacks').glob('*.json'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_installed_hooks_classify_native_default_review_on_both_hosts(self):
        source = Path(__file__).resolve().parents[1]
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                home = Path(self.temp.name) / ('installed-' + provider)
                home.mkdir()
                root = _materialize(source, home, PLUGIN_VERSION)
                state_dir = home / 'state'
                payload = {'session_id': self.session, 'cwd': str(self.project),
                           'hook_event_name': 'SessionStart', 'source': 'startup'}
                _send_raw(root, provider, 'SessionStart', state_dir, payload)
                child = home / 'ordinary-child.jsonl'
                rows = [{'type': 'session_meta', 'payload': {
                    'id': self.events[0].payload['agent_id'], 'cwd': str(self.project),
                    'source': {'subagent': {'thread_spawn': {
                        'parent_thread_id': self.session, 'agent_path': '/root/macos_review'}}}}}]
                rows.extend({'type': 'turn_context', 'payload': {
                    'turn_id': event.payload['turn_id'], 'model': 'gpt-6.1-sol', 'effort': 'high'}}
                    for event in self.events)
                child.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                original_native_bytes = child.read_bytes()
                for event in self.events:
                    # Codex imports parent/name/model from the native child
                    # header and exact turn, not from our callback fixture.
                    raw = {k: v for k, v in event.payload.items() if k not in
                           {'task_name', 'parent_thread_id', 'model', 'model_reasoning_effort',
                            '_symphony_child_metadata'}}
                    raw.update(provider=provider, agent_transcript_path=str(child),
                               agent_type='default' if provider == 'codex' else 'general-purpose')
                    _send_raw(root, provider, 'SubagentStop', state_dir,
                              {**payload, **raw, 'hook_event_name': 'SubagentStop'})
                result = _send_raw(root, provider, 'Stop', state_dir,
                                   {**payload, 'hook_event_name': 'Stop', 'stop_hook_active': False})
                self.assertNotEqual((result or {}).get('decision'), 'block')
                store = StateStore(state_dir)
                self.assertEqual(store.session_record(provider, self.session)['pending'], [])
                self.assertEqual(len(list((state_dir / 'unmanaged-callbacks').glob('*.json'))), 2)
                self.assertEqual(store.load(self.project).terminal_receipts, ())
                self.assertEqual(child.read_bytes(), original_native_bytes)

    def test_crash_before_ack_preserves_result_and_replay_has_no_task_credit(self):
        self.bind()
        self.queue()
        with patch.object(StateStore, 'finish_session_events', side_effect=OSError('before ACK')):
            with self.assertRaises(OSError):
                self.hook()
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(len(self.store.session_record('codex', self.session)['pending']), 2)
        self.assertNotIn('"decision": "block"', self.hook().stdout)
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(self.store.load(self.project).terminal_receipts, ())


if __name__ == '__main__':
    unittest.main()
