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
        self.assertTrue(all(item['result_success'] and item['message_matches_child_prompt']
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


if __name__ == '__main__':
    unittest.main()
