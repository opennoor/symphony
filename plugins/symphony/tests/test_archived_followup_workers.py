"""Sanitized replay of the October 3 archived-lead firmware incident."""
import json
import itertools
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony.model import Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import _handle_core as handle
from plugins.symphony.symphony.store import StateStore, _state_from_dict


class ArchivedFollowupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.home = self.base / 'codex'
        fixture = json.loads((Path(__file__).parent / 'fixtures/archived-followup-worker-codex.json').read_text())
        self.fixture = fixture
        self.root, self.lead, self.worker = (fixture[key] for key in ('root_id', 'lead_id', 'worker_id'))
        self.state = _state_from_dict({'schema_version': 2, 'enabled': False, 'recent_runs': [fixture['run']]})
        self.store = StateStore(self.base / 'state')
        self.environ = {'SYMPHONY_STATE_DIR': str(self.base / 'state'), 'CODEX_HOME': str(self.home),
                        'SYMPHONY_PROVIDER': 'codex', 'SYMPHONY_PROFILE': 'full'}
        self.paths = {}
        for identity, rows in fixture['transcripts'].items():
            path = self.home / 'sessions/2026/10/03' / f'rollout-{identity}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            for row in rows:
                if 'cwd' in row['payload']:
                    row['payload']['cwd'] = str(self.project)
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            self.paths[identity] = path
        self.store.save(self.project, self.state)
        self.store.bind_session('codex', self.root, self.store._path(self.project), False,
                                self.project, self.root)
        self.events = tuple(Event(item['event_id'], item['kind'], item['observed_at'],
                                  {**item['payload'], 'cwd': str(self.project)})
                            for item in fixture['pending'])

    def queue(self, events=None):
        for event in self.events if events is None else events:
            self.store.queue_session_event('codex', self.root, event, ambiguous_owner=True)

    def stop(self, hook='Stop'):
        return handle({'session_id': self.root, 'cwd': str(self.project), 'hook_event_name': hook}, self.environ)

    def assert_pending(self):
        self.assertIn('"decision": "block"', self.stop().stdout)
        self.assertEqual(4, len(self.store.session_record('codex', self.root)['pending']))
        self.assertEqual(self.state.recent_runs, self.store.load(self.project).recent_runs)

    def test_native_sequence_reconciles_fresh_worker_and_both_lead_turns(self):
        self.queue()
        self.assertNotIn('"decision": "block"', self.stop().stdout)
        state = self.store.load(self.project)
        self.assertFalse(state.active_runs)
        self.assertEqual([], self.store.session_record('codex', self.root)['pending'])
        run = state.recent_runs[0]
        self.assertEqual(self.state.recent_runs[0].run_id, run.run_id)
        self.assertEqual('completed', run.status)
        self.assertTrue(run.assessment['_substantive_children'][self.worker]['successful'])
        self.assertEqual(3, len(run.assessment['_terminal_turns'][self.lead]))

    def test_all_callback_arrival_orders_reconcile_the_same_owner(self):
        for events in itertools.permutations(self.events):
            with self.subTest(order=[event.kind for event in events]):
                self.store.save(self.project, self.state)
                self.queue(events)
                self.assertNotIn('"decision": "block"', self.stop().stdout)
                self.assertEqual([], self.store.session_record('codex', self.root)['pending'])
                run = self.store.load(self.project).recent_runs[0]
                self.assertEqual(self.state.recent_runs[0].run_id, run.run_id)
                self.assertEqual(5, len(run.delegations))

    def test_redacted_worker_callback_matches_its_complete_native_report(self):
        original = self.events[1].payload['last_assistant_message']
        credential = 'ghp_' + 'a' * 36
        report = original + '\nDiagnostic credential: ' + credential
        def substitute(value):
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (ValueError, TypeError):
                    return value.replace(original, report)
                return json.dumps(substitute(parsed)) if isinstance(parsed, (dict, list)) else value.replace(original, report)
            return value
        for path in self.paths.values():
            rows = substitute([json.loads(line) for line in path.read_text().splitlines()])
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.queue(tuple(replace(event, payload=substitute(event.payload)) for event in self.events))
        pending = self.store.session_record('codex', self.root)['pending']
        self.assertNotIn(credential, json.dumps(pending))
        self.assertIn('[REDACTED]', json.dumps(pending))
        self.assertNotIn('"decision": "block"', self.stop().stdout)
        self.assertEqual([], self.store.session_record('codex', self.root)['pending'])
        self.assertEqual('completed', self.store.load(self.project).recent_runs[0].status)

    def test_missing_lead_callbacks_use_native_root_delivery_without_inventing_worker_results(self):
        self.queue(self.events[:2])
        self.assertNotIn('"decision": "block"', self.stop().stdout)
        run = self.store.load(self.project).recent_runs[0]
        self.assertEqual('completed', run.status)
        self.assertTrue(run.assessment['_substantive_children'][self.worker]['successful'])

    def test_every_incomplete_worker_callback_combination_preserves_evidence(self):
        for mask in range(1, 16):
            if mask & 3 == 3:
                continue
            with self.subTest(mask=mask):
                self.store.save(self.project, self.state)
                events = tuple(event for index, event in enumerate(self.events) if mask & (1 << index))
                self.queue(events)
                self.assertIn('"decision": "block"', self.stop().stdout)
                self.assertEqual(len(events), len(self.store.session_record('codex', self.root)['pending']))
                self.assertEqual(self.state.recent_runs, self.store.load(self.project).recent_runs)
                self.store.finish_session_events(self.store.session_record('codex', self.root),
                                                 {event.event_id for event in events})

    def test_newer_run_and_foreign_root_are_preserved_without_borrowed_credit(self):
        newer = RunState('newer', 'recovery', status='interrupted', provider='codex',
                         session_id=self.root, lead_identity='different-lead',
                         started_at='2026-10-03T12:33:00+00:00')
        foreign = replace(newer, run_id='foreign', session_id='foreign-root')
        state = replace(self.state, active_run=newer,
                        active_runs={f'codex:{self.root}': newer, 'codex:foreign-root': foreign})
        self.store.save(self.project, state)
        self.queue()
        self.stop('SessionStart')
        actual = self.store.load(self.project)
        self.assertEqual(foreign, actual.active_runs['codex:foreign-root'])
        self.assertEqual(replace(newer, owner_seen_at=actual.active_runs[f'codex:{self.root}'].owner_seen_at),
                         actual.active_runs[f'codex:{self.root}'])
        self.assertEqual([], self.store.session_record('codex', self.root)['pending'])
        self.assertIsNone(actual.active_runs[f'codex:{self.root}'].outcome)

    def test_rejects_wrong_parent_route_result_turn_generation_or_unknown_child(self):
        variants = [('parent_thread_id', 'foreign'), ('model', 'different-model'),
                    ('last_assistant_message', 'different-result'), ('turn_id', 'different-turn'),
                    ('agent_id', 'unknown-worker'), ('agent_type', 'lead'),
                    ('task_name', 'different-task')]
        for key, value in variants:
            with self.subTest(key=key):
                self.store.save(self.project, self.state)
                event = self.events[1]
                self.queue((self.events[0], replace(event, payload={**event.payload, key: value}),
                            *self.events[2:]))
                self.assert_pending()
                self.store.finish_session_events(self.store.session_record('codex', self.root),
                                                 {item.event_id for item in self.events})
        self.state = replace(self.state, recent_runs=(replace(self.state.recent_runs[0], owner_generation=2),))
        self.store.save(self.project, self.state)
        self.queue()
        self.assert_pending()

    def test_missing_worker_callbacks_cannot_borrow_historical_worker_credit(self):
        self.queue(self.events[2:])
        self.assertIn('"decision": "block"', self.stop().stdout)
        self.assertEqual(2, len(self.store.session_record('codex', self.root)['pending']))
        self.assertEqual(self.state.recent_runs, self.store.load(self.project).recent_runs)

    def test_changed_retry_payload_does_not_match_committed_source_witness(self):
        self.queue()
        self.stop()
        event = self.events[1]
        changed = replace(event, payload={**event.payload, 'last_assistant_message': 'different report'})
        self.queue((changed,))
        self.assertIn('"decision": "block"', self.stop().stdout)
        self.assertEqual(1, len(self.store.session_record('codex', self.root)['pending']))

    def test_root_followup_must_have_successful_exact_delivery(self):
        path = self.paths[self.root]
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        followups = {row['payload']['call_id'] for row in rows
                     if row['payload'].get('name') == 'followup_task'}
        for row in rows:
            payload = row['payload']
            if payload.get('type') == 'function_call_output' and payload.get('call_id') in followups:
                payload['output'] = 'agent unavailable'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.queue()
        self.assert_pending()

    def test_native_worker_launch_and_delivered_handoff_are_required(self):
        path = self.paths[self.lead]
        original = path.read_text()
        for missing in ('spawn_agent', 'completed'):
            with self.subTest(missing=missing):
                rows = [json.loads(line) for line in original.splitlines()]
                rows = [row for row in rows if not (
                    missing == 'spawn_agent' and row['payload'].get('name') == 'spawn_agent'
                    and self.worker in original and 'firmware_history' in row['payload'].get('arguments', '')
                    or missing == 'completed' and row['payload'].get('type') == 'item_completed'
                    and row['payload'].get('item', {}).get('agent_thread_id') == self.worker
                    and row['payload']['item'].get('kind') == 'completed')]
                if missing == 'completed':
                    rows = [row for row in rows if row['payload'].get('type') != 'agent_message']
                path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                self.queue()
                self.assert_pending()
                self.store.finish_session_events(self.store.session_record('codex', self.root),
                                                 {item.event_id for item in self.events})
        path.write_text(original)

    def test_crash_after_commit_before_ack_replays_without_duplicate_credit(self):
        self.queue()
        with patch.object(StateStore, 'finish_session_events', side_effect=OSError('ACK failed')):
            with self.assertRaises(OSError):
                self.stop()
        committed = self.store.load(self.project)
        self.assertEqual(4, len(self.store.session_record('codex', self.root)['pending']))
        self.stop()
        self.assertEqual([], self.store.session_record('codex', self.root)['pending'])
        self.assertEqual(committed.recent_runs, self.store.load(self.project).recent_runs)


if __name__ == '__main__':
    unittest.main()
