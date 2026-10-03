"""The exact released 1.5.1 writer loses receipt-only acknowledgment proof."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Event, ProjectState
from plugins.symphony.symphony.routing import Assessment
# Strict ownership/credit reducer; public host-turn liveness has separate tests.
from plugins.symphony.symphony.runtime import _accept_assessment, _handle_core as handle
from plugins.symphony.symphony.store import StateStore


class Released151ReceiptLimitTests(unittest.TestCase):
    def released_store(self, root):
        fixture = Path(__file__).parent / 'fixtures/released-v1.5.1'
        source = (fixture / 'store.py.txt').read_bytes()
        provenance = json.loads((fixture / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(provenance['tag'], 'v1.5.1')
        self.assertEqual(provenance['commit'], '7e708c0919972f7538dd7751f736fe7382f96351')
        self.assertEqual(provenance['blob'], 'c3930bbf00dba6007e91f2500f492b4ca0021ac3')
        self.assertEqual(provenance['path'], 'plugins/symphony/symphony/store.py')
        self.assertEqual(hashlib.sha256(source).hexdigest(), provenance['sha256'])
        self.assertEqual(hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest(),
                         provenance['blob'])
        # Load only the tagged codec into this test's disposable state directory.
        # Application imports and installed runtimes remain unchanged.
        module = types.ModuleType('plugins.symphony.symphony.released_store_151_test')
        module.__package__ = 'plugins.symphony.symphony'
        exec(compile(source, 'released-v1.5.1/store.py', 'exec'), module.__dict__)
        return module.StateStore(root)

    def test_actual_151_write_keeps_new_owner_but_holds_unacknowledged_old_turn(self):
        from plugins.symphony.tests.test_host_evidence import HostEvidenceTests, ROOT_ID
        from plugins.symphony.tests.test_claude_host_evidence import ClaudeHostEvidenceTests, SESSION

        for provider, cls, session in (('codex', HostEvidenceTests, ROOT_ID),
                                       ('claude', ClaudeHostEvidenceTests, SESSION)):
            for with_start in (False, True):
                with self.subTest(provider=provider, with_start=with_start):
                    fixture = cls()
                    fixture.setUp()
                    try:
                        report = 'SYMPHONY_FAST_DECISION: escalate'
                        if provider == 'codex':
                            archived, payload, _ = fixture.load_archived_followup_fixture()
                            path = fixture.transcript
                            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                            rows[-1]['payload']['last_agent_message'] = report
                        else:
                            archived, payload = fixture.prepare_archived_followup(promptless=with_start)
                            path = fixture.child
                            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
                            rows[-1]['message']['content'][0]['text'] = report
                            lead = archived.delegations[0]
                            archived = replace(archived, assessment={**archived.assessment, 'topology': 'direct',
                                '_fast_route': {'model': lead.requested_tier, 'effort': lead.requested_effort}})
                        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                        fixture.store.save(fixture.project, ProjectState(recent_runs=(archived,)))
                        record = fixture.store.session_record(provider, session)
                        fixture.store.finish_session_events(record, {item['event_id'] for item in record['pending']})
                        payload['last_assistant_message'] = report
                        if with_start:
                            started = {**payload, 'hook_event_name': 'SubagentStart'}
                            started.pop('last_assistant_message', None)
                            started.pop('status', None)
                            fixture.store.queue_session_event(provider, session,
                                event_from_payload(provider, started), ambiguous_owner=True)
                        fixture.store.queue_session_event(provider, session,
                            event_from_payload(provider, payload), ambiguous_owner=True)
                        request = {'provider': provider, 'session_id': session, 'cwd': str(fixture.project),
                                   'hook_event_name': 'Stop'}
                        with patch.object(StateStore, 'finish_session_events', side_effect=OSError('commit before ACK')):
                            with self.assertRaises(OSError):
                                handle(request, fixture.environ)
                        committed = fixture.store.load(fixture.project)
                        self.assertTrue(committed.terminal_receipts[-1]['native_fast_escalation'])
                        event = Event('new-assessment', 'subagent_stopped', '2026-10-01T23:00:00+00:00', {})
                        updated, _ = _accept_assessment(committed, event, provider, {}, Assessment('small', 'simple'))
                        replacement = replace(updated.active_run, lead_identity='new-lead', owner_generation=2)
                        fixture.store.save(fixture.project, replace(updated, active_run=replacement,
                            active_runs={f'{provider}:{session}': replacement}))
                        before = fixture.store.load(fixture.project)
                        old = self.released_store(fixture.store.root)
                        old.save(fixture.project, old.load(fixture.project))
                        raw = json.loads(fixture.store._path(fixture.project).read_text(encoding='utf-8'))
                        self.assertNotIn('terminal_receipts', raw)
                        after = fixture.store.load(fixture.project)
                        self.assertEqual(after.active_run.assessment, before.active_run.assessment)
                        self.assertEqual(after.active_run.lead_identity, 'new-lead')
                        self.assertEqual(after.active_run.owner_generation, 2)
                        pending_before = fixture.store.session_record(provider, session)['pending']
                        self.assertEqual(len(pending_before), 2 if with_start else 1)
                        for _ in range(2):
                            result = handle(request, fixture.environ)
                            self.assertTrue(json.loads(result.stdout).get('decision') == 'block')
                            state = fixture.store.load(fixture.project)
                            # Preserve the unproved historical record without
                            # adding a batch hold to the independent new lead.
                            self.assertEqual(state.active_run, after.active_run)
                            self.assertEqual(state.recent_runs, after.recent_runs)
                            self.assertEqual(fixture.store.session_record(provider, session)['pending'], pending_before)
                    finally:
                        fixture.doCleanups()
                        fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
