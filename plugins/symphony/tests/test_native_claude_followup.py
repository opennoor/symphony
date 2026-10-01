"""Private native replay harness provenance, tested with host-shaped fixtures."""

import importlib.util
import json
from pathlib import Path
import shutil
import unittest

from plugins.symphony.symphony import adapters, host_evidence, model, runtime, store
from plugins.symphony.tests import test_claude_sendmessage as fixture
from plugins.symphony.tests.test_claude_host_evidence import SESSION


SPEC = importlib.util.spec_from_file_location('native_claude_followup',
    Path(__file__).resolve().parents[3] / '.github/scripts/native_claude_followup.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class NativeClaudeFollowupTests(unittest.TestCase):
    def test_both_native_sibling_scenarios_run_when_first_fails(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(probe, 'check_native_sendmessage', side_effect=[RuntimeError('private-token'), {}]) as check:
                with self.assertRaises(RuntimeError):
                    probe.check_native_sendmessage_pair(root, root, {}, {}, 60, 1)
            self.assertEqual([call.kwargs['label'] for call in check.call_args_list], ['a', 'b'])
            self.assertTrue(check.call_args_list[-1].kwargs['historical_worker'])
            self.assertNotIn('private-token', (root / 'sendmessage-required.json').read_text())

    def test_mixed_copies_queue_both_roles_and_preserve_sibling_pending_inbox(self):
        from dataclasses import replace
        from plugins.symphony.tests.test_claude_historical_sendmessage import HistoricalClaudeSendMessageTests, WORKER
        f = HistoricalClaudeSendMessageTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare_send()
        sibling = replace(f.original, run_id='sibling-run', session_id='sibling-root', lead_identity='sibling-lead',
                          assessment={'size': 'small', 'complexity': 'simple', 'topology': 'direct'},
                          delegations=(replace(f.original.delegations[0], identity='sibling-lead'),))
        f.store.save(f.project, replace(f.state, recent_runs=(f.original, sibling)))
        f.store.bind_session('claude', 'sibling-root', f.store._path(f.project), False, f.project, 'sibling-root')
        sibling_event = model.Event('sibling-pending', 'subagent_stopped', '2026-09-29T02:07:00+00:00',
            {'provider': 'claude', 'session_id': 'sibling-root', 'agent_id': 'unknown-sibling', 'status': 'completed'})
        f.store.queue_session_event('claude', 'sibling-root', sibling_event, ambiguous_owner=True)
        f.store.finish_session_events(f.store.session_record('claude', SESSION), {event.event_id for event in f.events})
        # Give the synthetic archived owners the same codec round trips as a
        # real baseline, including its legacy empty terminal placeholders.
        f.store.save(f.project, f.store.load(f.project))
        original = f.root / 'mixed-before'
        shutil.copytree(f.store.root, original)
        all_sources = tuple(sorted((*f.events, *f.worker_events), key=lambda event: event.observed_at))
        f.queue(all_sources)
        runtime.handle({'cwd': str(f.project), 'session_id': SESSION, 'hook_event_name': 'Stop'}, f.environ)
        after = f.store.load(f.project)
        run = next(run for run in after.recent_runs if run.session_id == SESSION)
        private = f.root / 'mixed-capture'
        private.mkdir()
        for index, event in enumerate(all_sources):
            (private / f'{index}.json').write_text(json.dumps({'canonical': {
                'event_id': event.event_id, 'kind': event.kind, 'payload': event.payload}}))
        sequences = run.assessment['_claude_sendmessage_sequences']
        sources = probe.captured_sequence_sources(after, SESSION, private, adapters, host_evidence, model, sequences=sequences)
        self.assertEqual(sources, all_sources)
        dispositions = probe.exact_historical_delta(run, sequences, WORKER, 'Acknowledge the original work only.')
        with self.assertRaises(RuntimeError): probe.exact_historical_delta(run, sequences, WORKER, 'not requested')
        output = probe.replay_native_copies(f.root, original, Path(f.environ['CLAUDE_CONFIG_DIR']), f.project,
            SESSION, sources, f.environ, runtime, store, expected_sequences=sequences,
            expected_receipts=probe.exact_delta_receipts(after, SESSION, run, sequences),
            expected_worker_dispositions=dispositions)
        self.assertTrue(all(item['historical_worker_replies'] == 1 for item in output.values()))
        for copied in f.root.glob('replay-*'):
            copied_store = store.StateStore(copied / 'state')
            self.assertEqual(copied_store._session_path('claude', 'sibling-root').read_bytes(),
                             f.store._session_path('claude', 'sibling-root').read_bytes())

    def test_rejection_trace_distinguishes_exact_private_native_predicates(self):
        from unittest.mock import patch
        f = fixture.ClaudeSendMessageTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare()
        before = f.store.load(f.project)
        bytes_before = f.store._path(f.project).read_bytes()
        pending = f.store.session_record('claude', SESSION)['pending']
        events = [model.Event(item['event_id'], item['kind'], item['observed_at'], item['payload'])
                  for item in pending]
        positive = probe.sequence_rejection_probe(before, events, SESSION, f.project, f.environ, host_evidence)
        self.assertTrue(positive['accepted'])
        self.assertTrue(all(item['result_success'] and item['coordinator_projection_matches']
                            for item in positive['sequence']['deliveries']))
        self.assertTrue(all(not item['message_matches_child_prompt']
                            for item in positive['sequence']['deliveries']))
        self.assertTrue(all(item['original_launch_prompt_matches'] for item in positive['native_readers']))
        original = f.parent.read_bytes()
        rows = [json.loads(line) for line in original.splitlines()]
        result = rows[-1]['message']['content'][0]
        result['content'] = json.dumps({'success': False, 'message': 'token/path-private-sentinel'})
        f.parent.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        negative = probe.sequence_rejection_probe(before, events, SESSION, f.project, f.environ, host_evidence)
        self.assertFalse(negative['accepted'])
        self.assertFalse(negative['sequence']['deliveries'][-1]['result_success'])
        exported = json.dumps(negative)
        for secret in ('token/path-private-sentinel', str(f.project), SESSION, f.events[0].event_id):
            self.assertNotIn(secret, exported)
        self.assertEqual(f.store._path(f.project).read_bytes(), bytes_before)
        previous = lambda *args: None
        with patch.object(probe.sys, 'gettrace', return_value=previous), patch.object(probe.sys, 'settrace') as traced:
            probe.sequence_rejection_probe(before, events, SESSION, f.project, f.environ, host_evidence)
        self.assertIs(traced.call_args.args[0], previous)

    def prepare(self):
        f = fixture.ClaudeSendMessageTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare()
        original = f.root / 'original-state'
        record = f.store.session_record('claude', SESSION)
        f.store.finish_session_events(record, {event.event_id for event in f.events})
        shutil.copytree(f.store.root, original)
        f.queue()
        f.call()
        private = f.root / 'capture'
        private.mkdir()
        for index, event in enumerate(f.events):
            (private / f'{index}.json').write_text(json.dumps({'canonical': {
                'event_id': event.event_id, 'kind': event.kind, 'payload': event.payload}}), encoding='utf-8')
        return f, private, original

    def test_exact_private_snapshot_binding_and_identical_retry(self):
        f, private, _ = self.prepare()
        shutil.copyfile(private / '0.json', private / 'retry.json')
        events = probe.captured_sequence_sources(f.store.load(f.project), SESSION, private,
                                                 adapters, host_evidence, model)
        self.assertEqual(events, tuple(f.events))
        changed = json.loads((private / '0.json').read_text())
        changed['canonical']['payload']['role'] = 'worker'
        (private / '0.json').write_text(json.dumps(changed))
        (private / 'retry.json').unlink()
        with self.assertRaises(RuntimeError):
            probe.captured_sequence_sources(f.store.load(f.project), SESSION, private,
                                            adapters, host_evidence, model)

    def test_new_request_must_have_exact_new_turn_delta(self):
        from dataclasses import replace
        f, _, original = self.prepare()
        before = store.StateStore(original).load(f.project).recent_runs[0]
        after = f.store.load(f.project).recent_runs[0]
        messages = ['Continue the same task, step 0.', 'Continue the same task, step 1.']
        self.assertEqual(len(probe.requested_sequence_delta(before, after, messages)), 1)
        for old, new, request in ((after, after, messages),
                (before, after, ['wrong', messages[1]]),
                (before, replace(after, lead_identity='foreign'), messages),
                (before, replace(after, owner_generation=8), messages)):
            with self.assertRaises(RuntimeError):
                probe.requested_sequence_delta(old, new, request)
        # A baseline already containing older turns cannot fulfill an ignored
        # new command, even when its old archive and receipts remain healthy.
        self.assertTrue(after.assessment['_claude_sendmessage_sequences'])
        with self.assertRaises(RuntimeError):
            probe.requested_sequence_delta(after, after, messages)

    def test_host_shaped_copies_preserve_original_native_files_and_hide_credentials(self):
        f, private, original = self.prepare()
        home = Path(f.environ['CLAUDE_CONFIG_DIR'])
        credential = 'private-token-sentinel'
        (home / '.credentials.json').write_text(credential)
        native_before = {path: path.read_bytes() for path in (f.parent, f.child, f.meta)}
        sources = probe.captured_sequence_sources(f.store.load(f.project), SESSION, private,
                                                   adapters, host_evidence, model)
        output = probe.replay_native_copies(f.root, original, home, f.project, SESSION,
                                            sources, f.environ, runtime, store,
            expected_sequences=f.store.load(f.project).recent_runs[0].assessment[
                '_claude_sendmessage_sequences'],
            expected_receipts=tuple(dict(r) for r in f.store.load(f.project).terminal_receipts))
        self.assertEqual(set(output), {'Stop', 'SessionStart', 'status', 'failed-ack', 'partial-ack'})
        self.assertTrue(all(facts['same_owner'] and facts['native_turns'] == 2
                            and facts['pending_callbacks'] == 0 for facts in output.values()))
        self.assertNotIn(credential, json.dumps(output))
        self.assertNotIn(str(f.project), json.dumps(output))
        self.assertEqual(native_before, {path: path.read_bytes() for path in native_before})
        for copied in f.root.glob('replay-*'):
            self.assertFalse((copied / 'native-home/.credentials.json').exists())

    def test_copied_native_sources_retain_first_arriving_late_callback_order(self):
        from plugins.symphony.tests.test_claude_historical_sendmessage import HistoricalClaudeSendMessageTests
        f = HistoricalClaudeSendMessageTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare_send()
        f.store.save(f.project, f.state)
        f.store.save(f.project, f.store.load(f.project))
        record = f.store.session_record('claude', SESSION)
        record['pending'] = []
        f.store._session_path('claude', SESSION).write_text(json.dumps(record))
        original = f.root / 'original-state'
        shutil.copytree(f.store.root, original)
        f.queue((f.worker_events[1],))
        f.queue(f.events)
        payload = {'hook_event_name': 'Stop', 'session_id': SESSION, 'cwd': str(f.project)}
        runtime.handle(payload, f.environ)
        f.queue((f.worker_events[0],))
        runtime.handle(payload, f.environ)
        after = f.store.load(f.project)
        run = after.recent_runs[0]
        private = f.root / 'capture'
        private.mkdir()
        for index, event in enumerate((*f.events, *f.worker_events)):
            (private / f'{index}.json').write_text(json.dumps({'canonical': {
                'event_id': event.event_id, 'kind': event.kind, 'payload': event.payload}}))
        sources = probe.captured_sequence_sources(after, SESSION, private, adapters, host_evidence, model)
        self.assertEqual(len(sources), 6)
        output = probe.replay_native_copies(f.root, original, Path(f.environ['CLAUDE_CONFIG_DIR']),
            f.project, SESSION, sources, f.environ, runtime, store,
            expected_sequences=run.assessment['_claude_sendmessage_sequences'],
            expected_receipts=probe.exact_delta_receipts(after, SESSION, run,
                run.assessment['_claude_sendmessage_sequences']),
            expected_worker_dispositions=run.assessment['_claude_historical_sendmessage_dispositions'],
            expected_late_sources=run.assessment['_claude_sendmessage_late_sources'])
        self.assertTrue(all(item['pending_callbacks'] == 0 for item in output.values()))


if __name__ == '__main__':
    unittest.main()
