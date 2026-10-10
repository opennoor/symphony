"""Native Claude recovery must prove the original lead's latest terminal."""

import json
import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony.host_evidence import (
    claude_committed_native_terminal_replay, claude_recovered_lead_event,
)
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import _observe_delegation, _handle_core as handle
from plugins.symphony.symphony.store import StateStore


SESSION = "01a0ec79-988a-7903-ba2e-b003394a341b"
LEAD = "a9c4054c282207da0"
TYPE = "symphony:symphony-lead-claude-sonnet-5-low"
REPORT = 'SYMPHONY_OUTCOME: {"status":"completed"}'


class ClaudeHostEvidenceTests(unittest.TestCase):
    def prepare_fast_handback_and_final(self, *, split_terminal=False, handback=True, final=None):
        report = 'Verified mechanical result.\nSYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
        model, effort = 'claude-opus-5-5', 'medium'
        agent_type = f'symphony:symphony-lead-{model}-{effort}'
        self.write_native(model=model, effort=effort, handback=handback, report=report)
        if not handback:
            # Captured from Claude print mode, which offers no SubagentHandback:
            # the lead's final message is its one report, ending with the ack.
            report = final if final is not None else report + '\n\nReport delivered.'
        self.write_root_prompt()
        parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
        parent[-1]['message']['content'][0]['input']['subagent_type'] = agent_type
        self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
        self.meta.write_text(json.dumps({'agentType': agent_type, 'toolUseId': 'toolu_launch', 'spawnDepth': 1}))
        child = [json.loads(line) for line in self.child.read_text().splitlines()]
        child[-1]['message'].update(id='msg_native_final', content=[{'type': 'text', 'text': report}])
        if split_terminal:
            # Native Claude appends thinking/text chunks of one message as
            # distinct assistant rows, each carrying the same end_turn.
            thinking = {**child[-1], 'uuid': 'terminal-thinking', 'timestamp': '2026-09-29T02:01:59.995Z',
                        'message': {**child[-1]['message'], 'content': [{'type': 'thinking', 'thinking': 'Verified.'}]}}
            child.insert(-1, thinking)
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in child))
        run = replace(self.run, assessment={
            '_fast_pending': True, '_fast_route': {'model': model, 'effort': effort},
            '_claude_fast_report_contract': 1,
            '_claude_fast_launch_hash': hashlib.sha256(b'toolu_launch').hexdigest(),
            '_claude_lead_start_identity': LEAD,
            '_claude_lead_start_prompt_hash': hashlib.sha256(b'start-request-journal').hexdigest(),
            '_lead_expected_route': {'identity': LEAD, 'model': model, 'effort': effort}},
            delegations=(replace(self.run.delegations[0], requested_tier=model, requested_effort=effort),))
        self.store.save(self.project, ProjectState(active_run=run, active_runs={f'claude:{SESSION}': run}))
        callback = {'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'SubagentStop',
                    'agent_id': LEAD, 'agent_type': agent_type, 'agent_transcript_path': str(self.child),
                    'prompt_id': 'fdb4a2a5-137f-4bb9-8704-21c5328f6dac',
                    'last_assistant_message': report, 'status': 'completed'}
        merged = event_from_payload('claude', callback).payload['last_assistant_message']
        self.assertEqual(2 if handback else report.count('SYMPHONY_FAST_DECISION: eligible'),
                         merged.count('SYMPHONY_FAST_DECISION: eligible'))
        return callback

    def assert_fast_callback_archives(self, callback):
        handle(callback, self.environ)
        run = self.store.load(self.project).active_run
        self.assertEqual('completing', run.status)
        self.assertNotIn('_fast_pending', run.assessment)
        self.assertEqual({'status': 'completed'}, run.outcome)
        result = handle({'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'Stop'}, self.environ)
        self.assertNotIn('"decision": "block"', result.stdout)
        self.assertIsNone(self.store.load(self.project).active_run)
        archived = self.store.load(self.project)
        handle(callback, self.environ)
        self.assertEqual(archived.recent_runs, self.store.load(self.project).recent_runs)
        self.assertEqual(archived.terminal_receipts, self.store.load(self.project).terminal_receipts)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_fast_handback_and_final_repeated_markers_use_one_native_report(self):
        self.assert_fast_callback_archives(self.prepare_fast_handback_and_final())

    def test_fast_report_without_a_handback_tool_completes_from_the_final_message(self):
        self.assert_fast_callback_archives(self.prepare_fast_handback_and_final(handback=False))

    def test_fast_report_without_a_handback_still_needs_one_decision_and_success(self):
        base = 'Merged.\nSYMPHONY_FAST_DECISION: eligible\n'
        for name, final in (('two-decisions', base + base + 'SYMPHONY_OUTCOME: {"status":"completed"}'),
                            ('no-decision', 'Merged.\nSYMPHONY_OUTCOME: {"status":"completed"}'),
                            ('blocked', base + 'SYMPHONY_OUTCOME: {"status":"blocked"}'),
                            ('two-outcomes', base + 'SYMPHONY_OUTCOME: {"status":"completed"}\n'
                                             'SYMPHONY_OUTCOME: {"status":"completed"}')):
            with self.subTest(case=name):
                self.tearDown()
                self.setUp()
                handle(self.prepare_fast_handback_and_final(handback=False, final=final), self.environ)
                run = self.store.load(self.project).active_run
                self.assertIsNotNone(run)
                self.assertNotEqual({'status': 'completed'}, run.outcome)
                handle({'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'Stop'},
                       self.environ)
                self.assertFalse(self.store.load(self.project).recent_runs)

    def test_fast_native_terminal_thinking_and_text_chunks_are_one_message(self):
        self.assert_fast_callback_archives(self.prepare_fast_handback_and_final(split_terminal=True))

    def test_fast_native_callback_replay_after_recent_run_trim_is_idempotent(self):
        callback = self.prepare_fast_handback_and_final()
        self.assert_fast_callback_archives(callback)
        retained = replace(self.store.load(self.project), recent_runs=())
        self.assertIs(True, retained.terminal_receipts[-1]['native_fast_owner'])
        self.assertEqual(1, retained.terminal_receipts[-1]['native_fast_report_contract'])
        self.store.save(self.project, retained)
        handle(callback, self.environ)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])
        after = self.store.load(self.project)
        self.assertIsNone(after.active_run)
        self.assertEqual(retained.recent_runs, after.recent_runs)
        self.assertEqual(retained.terminal_receipts, after.terminal_receipts)

    def test_trimmed_fast_receipt_cannot_ack_missing_foreign_or_conflicting_native_report(self):
        for case in ('missing-native', 'foreign-parent', 'conflicting-report'):
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                callback = self.prepare_fast_handback_and_final()
                self.assert_fast_callback_archives(callback)
                retained = replace(self.store.load(self.project), recent_runs=())
                self.store.save(self.project, retained)
                if case == 'missing-native':
                    self.child.unlink()
                elif case == 'foreign-parent':
                    callback['parent_thread_id'] = 'foreign-root'
                else:
                    callback['last_assistant_message'] = callback['last_assistant_message'].replace(
                        'Verified mechanical result.', 'A different mechanical result.')
                self.assertFalse(claude_committed_native_terminal_replay(
                    retained, event_from_payload('claude', callback), SESSION, self.project, self.environ))

    def test_fast_handback_with_markerless_goodbye_retains_native_report(self):
        callback = self.prepare_fast_handback_and_final()
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows[-1]['message']['content'][0]['text'] = 'Report delivered.'
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        callback['last_assistant_message'] = 'Report delivered.'
        self.assert_fast_callback_archives(callback)

    def test_fast_callback_without_transcript_path_cannot_bypass_canonical_report(self):
        callback = self.prepare_fast_handback_and_final()
        del callback['agent_transcript_path']
        handle(callback, self.environ)
        self.assertEqual('active', self.store.load(self.project).active_run.status)
        self.assertIsNone(self.store.load(self.project).active_run.outcome)
        self.assertTrue(self.store.session_record('claude', SESSION)['pending'])
        result = handle({'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'Stop'}, self.environ)
        self.assertIn('"decision": "block"', result.stdout)
        self.assertFalse(self.store.load(self.project).recent_runs)

    def test_fast_canonical_escalation_handback_enters_assessment(self):
        callback = self.prepare_fast_handback_and_final()
        report = 'Discovered material uncertainty.\nSYMPHONY_FAST_DECISION: escalate'
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows[1]['message']['content'][0]['input']['message'] = report
        rows[-1]['message']['content'][0]['text'] = 'Report delivered.'
        self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        callback['last_assistant_message'] = 'Report delivered.'
        handle(callback, self.environ)
        run = self.store.load(self.project).active_run
        self.assertEqual('assessing', run.status)
        self.assertTrue(run.assessment['_fast_escalated'])
        self.assertNotIn('_fast_pending', run.assessment)
        self.assertIsNone(run.outcome)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])

    def test_fast_handback_and_final_full_report_disagreement_is_rejected(self):
        from plugins.symphony.symphony.host_evidence import claude_current_native_lead_event
        for prose in ('Deleted branch feature-b; verification succeeded.',
                      'Verification succeeded; feature-a was deleted.'):
            with self.subTest(final_prose=prose):
                callback = self.prepare_fast_handback_and_final()
                markers = '\nSYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
                handback = 'Deleted branch feature-a; verification succeeded.' + markers
                final = prose + markers
                rows = [json.loads(line) for line in self.child.read_text().splitlines()]
                rows[1]['message']['content'][0]['input']['message'] = handback
                rows[-1]['message']['content'][0]['text'] = final
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                callback['last_assistant_message'] = final
                source = event_from_payload('claude', callback)
                self.assertEqual(handback + '\n' + final, source.payload['last_assistant_message'])
                self.assertIsNone(claude_current_native_lead_event(self.store.load(self.project), source,
                    SESSION, self.project, self.environ))
                handle(callback, self.environ)
                self.assertEqual('active', self.store.load(self.project).active_run.status)
                result = handle({'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'Stop'}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertFalse(self.store.load(self.project).recent_runs)

    def test_fast_repeated_marker_normalization_requires_exact_native_evidence(self):
        from plugins.symphony.symphony.host_evidence import claude_current_native_lead_event
        for case in ('foreign-session', 'wrong-model', 'malformed-journal', 'early-callback', 'different-report',
                     'duplicate-handback-marker', 'conflicting-handback', 'failed-ack', 'duplicate-ack',
                     'early-ack', 'later-prompt', 'distinct-terminal-message', 'earlier-terminal-text', 'duplicate-uuid',
                     'duplicate-handback', 'foreign-handback', 'alternate-goodbye', 'missing-native-file'):
            # A transcript with no handback whose final message is the whole
            # report is the print-mode shape; it completes (tested above).
            with self.subTest(case=case):
                callback = self.prepare_fast_handback_and_final(split_terminal=True)
                source = event_from_payload('claude', callback)
                rows = [json.loads(line) for line in self.child.read_text().splitlines()]
                if case in ('foreign-session', 'wrong-model', 'malformed-journal', 'different-report'):
                    field, value = {'foreign-session': ('session_id', 'foreign'),
                        'wrong-model': ('model', 'claude-sonnet-5'),
                        'malformed-journal': ('prompt_id', 'foreign'),
                        'different-report': ('last_assistant_message', source.payload['last_assistant_message'] + '\nChanged.') }[case]
                    source = replace(source, payload={**source.payload, field: value})
                elif case == 'early-callback':
                    source = replace(source, observed_at='2026-09-29T02:01:59Z')
                elif case in ('duplicate-handback-marker', 'conflicting-handback'):
                    block = rows[1]['message']['content'][0]['input']
                    block['message'] = (block['message'] + '\nSYMPHONY_FAST_DECISION: eligible'
                        if case == 'duplicate-handback-marker' else block['message'].replace('eligible', 'escalate'))
                elif case == 'failed-ack':
                    rows[2]['message']['content'][0]['is_error'] = True
                elif case == 'duplicate-ack':
                    rows.insert(3, json.loads(json.dumps(rows[2])))
                elif case == 'early-ack':
                    rows[2]['timestamp'] = '2026-09-29T02:01:39Z'
                elif case == 'later-prompt':
                    rows.append({**rows[0], 'uuid': 'newer-prompt', 'timestamp': '2026-09-29T02:03:00Z'})
                elif case == 'distinct-terminal-message':
                    rows[-2]['message']['id'] = 'different-native-message'
                elif case == 'earlier-terminal-text':
                    rows[-2]['message']['content'] = [{'type': 'text', 'text': callback['last_assistant_message']}]
                elif case == 'duplicate-uuid':
                    rows[-2]['uuid'] = rows[-1]['uuid']
                elif case == 'duplicate-handback':
                    rows.insert(2, json.loads(json.dumps(rows[1])))
                elif case == 'foreign-handback':
                    rows[1]['agentId'] = 'foreign'
                elif case == 'alternate-goodbye':
                    rows[-1]['message']['content'][0]['text'] = 'Finished.'
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                if case == 'missing-native-file':
                    self.child.unlink()
                self.assertIsNone(claude_current_native_lead_event(self.store.load(self.project), source,
                    SESSION, self.project, self.environ))
                if case in ('duplicate-handback', 'foreign-handback', 'alternate-goodbye', 'missing-native-file'):
                    callback['last_assistant_message'] = rows[-1]['message']['content'][0]['text']
                    handle(callback, self.environ)
                    result = handle({'session_id': SESSION, 'cwd': str(self.project),
                                     'hook_event_name': 'Stop'}, self.environ)
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertFalse(self.store.load(self.project).recent_runs)

    def test_unknown_native_stop_retry_releases_turn_without_archiving_or_losing_evidence(self):
        self.write_root_prompt()
        run = replace(self.run, status='completing', started_at='2026-09-29T02:01:00.001+00:00',
            outcome={'status': 'completed'}, delegations=(replace(self.run.delegations[0], state='completed'),))
        self.store.save(self.project, ProjectState(active_run=run))
        request = {'session_id': SESSION, 'cwd': str(self.project), 'hook_event_name': 'Stop'}
        for value in (False, 'true', 1):
            blocked = handle({**request, 'stop_hook_active': value}, self.environ)
            self.assertEqual(json.loads(blocked.stdout).get('decision'), 'block')
        before = self.store.load(self.project)
        # The user's own stop closes the run it cannot verify, never as completed.
        explicit = handle({**request, 'hook_event_name': 'UserPromptSubmit',
                           'prompt': '/symphony:stop', 'stop_hook_active': True}, self.environ)
        self.assertNotEqual(json.loads(explicit.stdout or '{}').get('decision'), 'block')
        stopped = self.store.load(self.project)
        self.assertIsNone(stopped.active_run)
        self.assertEqual('stopped', stopped.recent_runs[-1].status)
        self.store.save(self.project, before)
        released = handle({**request, 'stop_hook_active': True}, self.environ)
        self.assertEqual(set(json.loads(released.stdout)), {'systemMessage'})
        self.assertNotIn('additionalContext', released.stdout)
        self.assertIn('host turn only', released.stdout)
        after = self.store.load(self.project)
        self.assertEqual(after.active_run, before.active_run)
        self.assertEqual(after.recent_runs, before.recent_runs)
        self.assertEqual(after.terminal_receipts, before.terminal_receipts)
        self.assertFalse(self.store.session_record('claude', SESSION)['pending'])
        self.assertIn('without an outcome marker', released.stdout)
        self.assertNotIn('exact marker', released.stdout)
        accepted = replace(run, assessment={**run.assessment,
            '_fast_route': {'model': 'claude-sonnet-5', 'effort': 'low'},
            '_claude_fast_launch_hash': hashlib.sha256(b'toolu_launch').hexdigest()})
        self.store.save(self.project, replace(after, active_run=accepted,
            active_runs={f'claude:{SESSION}': accepted}))
        archived = handle(request, self.environ)
        self.assertNotIn('block', archived.stdout)
        self.assertIsNone(self.store.load(self.project).active_run)

    def test_observed_fast_launch_can_precede_hook_but_not_its_native_child(self):
        from plugins.symphony.symphony.host_evidence import (
            _claude_native_lead_event, _claude_native_prompt_activity, claude_completing_lead_turn)
        for case in ('valid', 'missing-id', 'wrong-id', 'empty-id', 'wrong-prompt', 'legacy', 'assessed',
                     'duplicate-id', 'foreign-root', 'foreign-cwd', 'foreign-provider', 'foreign-session',
                     'child-before-hook', 'terminal-before-hook', 'wrong-model', 'wrong-effort', 'wrong-route'):
            with self.subTest(case=case):
                self.write_native(handback=True, report='SYMPHONY_FAST_DECISION: eligible\n' + REPORT)
                fixture = [json.loads(line) for line in self.child.read_text().splitlines()]
                fixture[-1]['message']['content'][0]['text'] = 'Report delivered.'
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in fixture))
                self.write_root_prompt()
                assessment = {'_fast_route': {'model': 'claude-sonnet-5', 'effort': 'low'},
                    '_claude_fast_launch_hash': hashlib.sha256(b'toolu_launch').hexdigest(),
                    '_claude_fast_root_prompt_hash': hashlib.sha256(b'root-prompt').hexdigest(),
                    '_claude_native_recovery': 'prompt_id:prompt-one'}
                if case == 'missing-id':
                    assessment.pop('_claude_fast_launch_hash')
                if case == 'wrong-id':
                    assessment['_claude_fast_launch_hash'] = hashlib.sha256(b'foreign').hexdigest()
                if case == 'empty-id':
                    assessment['_claude_fast_launch_hash'] = ''
                if case == 'wrong-prompt':
                    assessment['_claude_fast_root_prompt_hash'] = hashlib.sha256(b'foreign').hexdigest()
                if case == 'legacy':
                    assessment.pop('_fast_route')
                if case == 'assessed':
                    assessment['_fast_escalated'] = True
                if case == 'wrong-route':
                    assessment['_fast_route']['effort'] = 'medium'
                run = replace(self.run, status='completing', started_at='2026-09-29T02:01:00.001+00:00',
                    assessment=assessment, outcome={'status': 'completed'},
                    provider='codex' if case == 'foreign-provider' else 'claude',
                    session_id='foreign' if case == 'foreign-session' else SESSION,
                    delegations=(replace(self.run.delegations[0], state='completed'),))
                parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
                child = [json.loads(line) for line in self.child.read_text().splitlines()]
                if case == 'duplicate-id':
                    parent.append(parent[-1])
                if case == 'foreign-root':
                    parent[-1]['sessionId'] = 'foreign'
                if case == 'foreign-cwd':
                    parent[-1]['cwd'] = str(self.root / 'foreign')
                if case == 'child-before-hook':
                    child[0]['timestamp'] = '2026-09-29T02:01:00.000500Z'
                if case == 'terminal-before-hook':
                    child[-1]['timestamp'] = '2026-09-29T02:01:00.000500Z'
                if case == 'wrong-model':
                    child[-1]['message']['model'] = 'foreign'
                if case == 'wrong-effort':
                    child[-1]['effort'] = 'medium'
                self.parent.write_text(''.join(json.dumps(row) + '\n' for row in parent))
                self.child.write_text(''.join(json.dumps(row) + '\n' for row in child))
                state = ProjectState(active_run=run, active_runs={f'claude:{SESSION}': run})
                event = _claude_native_lead_event(state, SESSION, self.project, self.environ, require_missing=False)
                self.assertEqual(event is not None, case in {'valid', 'wrong-prompt'})
                if case in {'valid', 'wrong-prompt'}:
                    self.assertEqual(_claude_native_prompt_activity(state, SESSION, self.project, self.environ)[0], 'single')
                    self.assertEqual(claude_completing_lead_turn(state, SESSION, self.project, self.environ)[0], 'none')
                    self.store.save(self.project, state)
                    result = handle({'session_id': SESSION, 'cwd': str(self.project),
                                     'hook_event_name': 'Stop'}, self.environ)
                    self.assertNotIn('block', result.stdout)
                    self.assertIsNone(self.store.load(self.project).active_run)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        home = self.root / "claude-home"
        self.session_dir = home / "projects" / "-fixture" / SESSION
        self.child = self.session_dir / "subagents" / f"agent-{LEAD}.jsonl"
        self.child.parent.mkdir(parents=True)
        self.parent = self.session_dir.with_suffix(".jsonl")
        self.meta = self.child.with_suffix(".meta.json")
        self.environ = {"SYMPHONY_STATE_DIR": str(self.root / "state"),
                        "CLAUDE_CONFIG_DIR": str(home), "SYMPHONY_PROFILE": "full"}
        self.run = RunState(
            "original-run", "task", status="active", session_id=SESSION, provider="claude",
            lead_identity=LEAD, started_at="2026-09-29T02:00:00+00:00",
            updated_at="2026-09-29T02:01:00+00:00",
            assessment={"size": "medium", "complexity": "complex",
                        "_lead_expected_route": {"identity": LEAD,
                                                 "model": "claude-sonnet-5", "effort": "low"}},
            delegations=(Delegation(LEAD, "lead", "task", "working", "claude-sonnet-5", "low"),),
        )
        self.store = StateStore(self.root / "state")
        self.store.save(self.project, ProjectState(active_run=self.run,
                                                   active_runs={f"claude:{SESSION}": self.run}))
        self.write_native()

    def write_native(self, *, parent_session=SESSION, agent=LEAD,
                     model="claude-sonnet-5", effort="low", final=True,
                     report=REPORT, later_prompt=False, handback=False,
                     handback_error=False):
        self.meta.write_text(json.dumps({"agentType": TYPE, "toolUseId": "toolu_launch",
                                         "spawnDepth": 1}))
        parent = [{"type": "assistant", "sessionId": parent_session,
                   "cwd": str(self.project), "timestamp": "2026-09-29T02:01:00Z",
                   "message": {"content": [{"type": "tool_use", "name": "Agent",
                                            "id": "toolu_launch",
                                            "input": {"subagent_type": TYPE}}]}}]
        self.parent.write_text("".join(json.dumps(row) + "\n" for row in parent))
        rows = [{"type": "user", "uuid": "prompt-one", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:01Z",
                 "message": {"content": "Complete this task."}}]
        if handback:
            rows.extend([
                {"type": "assistant", "uuid": "handback", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:40Z",
                 "effort": effort, "message": {"model": model, "stop_reason": "tool_use",
                   "content": [{"type": "tool_use", "name": "SubagentHandback",
                                "id": "toolu_handback", "input": {"message": report}}]}},
                {"type": "user", "uuid": "handback-result", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:41Z",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_handback",
                                          "is_error": handback_error}]}},
            ])
        if final:
            rows.append({"type": "assistant", "uuid": "terminal-one", "sessionId": SESSION,
                         "agentId": agent, "isSidechain": True,
                         "timestamp": "2026-09-29T02:02:00Z", "effort": effort,
                         "message": {"model": model, "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": "Done." if handback else report}]}})
        if later_prompt:
            rows.append({"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
                         "agentId": agent, "isSidechain": True,
                         "timestamp": "2026-09-29T02:03:00Z",
                         "message": {"content": "Do more work."}})
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def prepare_archived_followup(self, *, with_start=False, promptless=False, report_suffix=''):
        self.environ["SYMPHONY_PROVIDER"] = "claude"
        self.write_root_prompt()
        archived = replace(self.run, status="completed", owner_generation=7,
                           updated_at="2026-09-29T02:02:01+00:00", outcome={"status": "completed"},
                           assessment={**self.run.assessment, "_claude_native_recovery": "prompt_id:prompt-one",
                                       "_terminal_turns": {LEAD: ["prompt_id:prompt-one"]}},
                           delegations=(replace(self.run.delegations[0], state="completed"),))
        self.store.save(self.project, ProjectState(recent_runs=(archived,)))
        self.store.bind_session("claude", SESSION, self.store._path(self.project), False, self.project, SESSION)
        parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
        parent.extend([
            {"type": "user", "uuid": "root-two", "sessionId": SESSION,
             "timestamp": "2026-09-29T02:02:50Z", "message": {"content": "Continue the original task."}},
            {"type": "assistant", "sessionId": SESSION, "cwd": str(self.project),
             "timestamp": "2026-09-29T02:03:00Z", "message": {"content": [{
                 "type": "tool_use", "name": "Agent", "id": "resume-call",
                 "input": {"resume": LEAD, "subagent_type": TYPE, "prompt": "Continue the original task."}}]}},
            {"type": "user", "sessionId": SESSION, "timestamp": "2026-09-29T02:04:01Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "resume-call",
                                      "content": "Agent completed", "is_error": False}]}},
        ])
        self.parent.write_text("".join(json.dumps(row) + "\n" for row in parent))
        report = REPORT + "\nNew result." + report_suffix
        child = [json.loads(line) for line in self.child.read_text().splitlines()]
        child.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION, "agentId": LEAD,
             "isSidechain": True, "timestamp": "2026-09-29T02:03:01Z",
             "message": {"content": "Continue the original task."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION, "agentId": LEAD,
             "isSidechain": True, "timestamp": "2026-09-29T02:04:00Z", "effort": "low",
             "message": {"model": "claude-sonnet-5", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": report}]}},
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in child))
        payload = {"provider": "claude", "cwd": str(self.project), "session_id": SESSION,
                   "agent_id": LEAD, "agent_type": TYPE, "prompt_id": "root-two"}
        if promptless:
            payload.pop("prompt_id")
        if with_start:
            self.store.queue_session_event("claude", SESSION, event_from_payload("claude", {
                **payload, "hook_event_name": "SubagentStart"}), ambiguous_owner=True)
        terminal = {**payload, "hook_event_name": "SubagentStop", "status": "completed",
                    "last_assistant_message": report}
        self.store.queue_session_event("claude", SESSION, event_from_payload("claude", terminal), ambiguous_owner=True)
        return archived, terminal

    def test_archived_claude_redacted_callback_matches_only_the_same_native_report(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                self.setUp()
                credential = 'ghp_' + 'b' * 36
                archived, _ = self.prepare_archived_followup(
                    report_suffix='\nDiagnostic credential: ' + credential)
                record = self.store.session_record('claude', SESSION)
                self.assertNotIn(credential, json.dumps(record['pending']))
                self.assertIn('[REDACTED]', json.dumps(record['pending']))
                if mismatch:
                    report = record['pending'][0]['payload']['last_assistant_message']
                    record['pending'][0]['payload']['last_assistant_message'] = report.replace(
                        'New result.', 'Changed result.')
                    self.store._write_json(self.store._session_path('claude', SESSION), record)
                result = handle({'cwd': str(self.project), 'session_id': SESSION,
                                 'hook_event_name': 'Stop'}, self.environ)
                state = self.store.load(self.project)
                if mismatch:
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertEqual((archived,), state.recent_runs)
                    self.assertEqual(1, len(self.store.session_record('claude', SESSION)['pending']))
                else:
                    self.assertNotIn('"decision": "block"', result.stdout)
                    self.assertEqual([], self.store.session_record('claude', SESSION)['pending'])
                    self.assertIn('prompt_id:prompt-two', state.recent_runs[0].assessment['_terminal_turns'][LEAD])

    def test_archived_claude_resume_reconciles_through_root_hooks_and_replays(self):
        for event, promptless in ((event, promptless) for event in
                                  ("Stop", "SessionStart", "UserPromptSubmit") for promptless in (False, True)):
            with self.subTest(event=event, promptless=promptless):
                self.setUp()
                archived, terminal = self.prepare_archived_followup(promptless=promptless)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": event, "prompt": "/symphony:status"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}, self.environ)
                state = self.store.load(self.project)
                self.assertIsNone(state.active_run)
                self.assertEqual(1, len(state.recent_runs))
                self.assertEqual(archived.run_id, state.recent_runs[0].run_id)
                self.assertEqual(7, state.recent_runs[0].owner_generation)
                self.assertIn("prompt_id:prompt-two", state.recent_runs[0].assessment["_terminal_turns"][LEAD])
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])
                receipt = next(item for item in state.terminal_receipts if item["turn"] == "prompt_id:prompt-two")
                self.assertIn(receipt["native_followup_start_id"], state.recent_runs[0].assessment["_start_event_ids"])
                self.assertTrue(receipt["native_followup_start_id"].endswith(":followup-start"))
                self.store.queue_session_event("claude", SESSION, event_from_payload("claude", terminal), ambiguous_owner=True)
                handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}, self.environ)
                self.assertEqual(state.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(state.terminal_receipts, self.store.load(self.project).terminal_receipts)
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])

    def test_archived_claude_resume_preserves_start_and_terminal_across_crash(self):
        for event, promptless in ((event, promptless) for event in
                                  ("Stop", "SessionStart", "UserPromptSubmit") for promptless in (False, True)):
            with self.subTest(event=event, promptless=promptless):
                self.setUp()
                self.prepare_archived_followup(with_start=True, promptless=promptless)
                with patch.object(StateStore, "finish_session_events", side_effect=OSError("crash")):
                    with self.assertRaises(OSError):
                        handle({"cwd": str(self.project), "session_id": SESSION,
                                "hook_event_name": event, "prompt": "/symphony:status"}, self.environ)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertIsNone(self.store.load(self.project).active_run)
                self.assertEqual(1, len(self.store.load(self.project).recent_runs))
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])

    def test_archived_claude_resume_terminal_before_start_is_stable(self):
        for hook, with_start, promptless in ((hook, start, promptless) for hook, start in
                (("Stop", False), ("UserPromptSubmit", False), ("Stop", True)) for promptless in (False, True)):
            with self.subTest(hook=hook, with_start=with_start, promptless=promptless):
                self.setUp()
                _, terminal = self.prepare_archived_followup(with_start=with_start, promptless=promptless)
                handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": hook,
                        "prompt": "/symphony:status"}, self.environ)
                started = {key: value for key, value in terminal.items()
                           if key not in {"status", "last_assistant_message"}}
                started["hook_event_name"] = "SubagentStart"
                handle(started, self.environ)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertIsNone(self.store.load(self.project).active_run)
                self.assertEqual(1, len(self.store.load(self.project).recent_runs))
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])
                # A subsequent resume in the same root prompt is new work,
                # even before its child transcript has flushed a new prompt.
                rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                resume = rows[-2]
                newer = {**resume, "timestamp": "2026-10-02T02:00:00Z",
                         "message": {"content": [{**resume["message"]["content"][0], "id": "new-resume"}]}}
                rows.append(newer)
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                self.assertFalse(claude_committed_native_terminal_replay(
                    self.store.load(self.project), event_from_payload("claude", terminal),
                    SESSION, self.project, self.environ))
                handle(started, self.environ)
                self.assertEqual(1, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_promptless_original_receipt_cannot_ack_a_new_queued_resume(self):
        self.environ["SYMPHONY_PROVIDER"] = "claude"
        handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}, self.environ)
        terminal = {"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "SubagentStop",
                    "agent_id": LEAD, "agent_type": TYPE, "last_assistant_message": REPORT}
        source = event_from_payload("claude", terminal)
        self.assertTrue(claude_committed_native_terminal_replay(
            self.store.load(self.project), source, SESSION, self.project, self.environ))
        rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
        rows.append({"type": "assistant", "sessionId": SESSION, "cwd": str(self.project),
                     "timestamp": "2026-10-02T02:00:00Z", "message": {"content": [{
                         "type": "tool_use", "name": "Agent", "id": "new-resume",
                         "input": {"resume": LEAD, "subagent_type": TYPE}}]}})
        self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
        for trimmed in (False, True):
            with self.subTest(trimmed=trimmed):
                state = self.store.load(self.project)
                if trimmed:
                    state = replace(state, recent_runs=())
                self.assertFalse(claude_committed_native_terminal_replay(
                    state, source, SESSION, self.project, self.environ))
        handle(terminal, self.environ)
        result = handle({"cwd": str(self.project), "session_id": SESSION,
                         "hook_event_name": "Stop"}, self.environ)
        self.assertIn('"decision": "block"', result.stdout)
        self.assertEqual(1, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_followup_receipt_replay_checks_latest_root_and_child(self):
        for promptless in (False, True):
            with self.subTest(promptless=promptless):
                self.setUp()
                _, terminal = self.prepare_archived_followup(promptless=promptless)
                handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}, self.environ)
                state = replace(self.store.load(self.project), recent_runs=())
                self.store.save(self.project, state)
                source = event_from_payload("claude", terminal)
                source = replace(source, payload={**source.payload, "_symphony_owner_conflict": True})
                self.assertTrue(claude_committed_native_terminal_replay(
                    state, source, SESSION, self.project, self.environ))
                rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                resume = rows[-2]
                rows.append({**resume, "timestamp": "2026-10-02T02:00:00Z",
                             "message": {"content": [{**resume["message"]["content"][0], "id": "new-resume"}]}})
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                self.assertFalse(claude_committed_native_terminal_replay(
                    state, source, SESSION, self.project, self.environ))

    def test_promptless_archived_followup_accepts_successful_native_handback(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.setUp()
                _, terminal = self.prepare_archived_followup(promptless=True)
                rows = [json.loads(line) for line in self.child.read_text().splitlines()]
                final = rows.pop()
                rows.extend([
                    {**final, "uuid": "handback-two", "timestamp": "2026-09-29T02:03:50Z",
                     "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use", "content": [{
                         "type": "tool_use", "name": "SubagentHandback", "id": "handback-two",
                         "input": {"message": terminal["last_assistant_message"]}}]}},
                    {**rows[-1], "uuid": "handback-result-two", "timestamp": "2026-09-29T02:03:51Z",
                     "message": {"content": [{"type": "tool_result", "tool_use_id": "handback-two",
                                              "is_error": failed}]}},
                    {**final, "message": {**final["message"], "content": [{"type": "text", "text": "Done."}]}}
                ])
                self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
                terminal.update(agent_transcript_path=str(self.child), last_assistant_message="Done.")
                record = self.store.session_record("claude", SESSION)
                record["pending"] = []
                self.store._write_json(self.store._session_path("claude", SESSION), record)
                self.store.queue_session_event("claude", SESSION, event_from_payload("claude", terminal),
                                               ambiguous_owner=True)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertEqual(failed, '\"decision\": \"block\"' in result.stdout)
                self.assertEqual(int(failed), len(self.store.session_record("claude", SESSION)["pending"]))

    def test_archived_claude_resume_rejects_missing_project_and_invocation_ids(self):
        for field in ("cwd", "id"):
            for value in ((None, "", 123, [], ".", "relative") if field == "cwd" else (None, "", 123, [])):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    archived, _ = self.prepare_archived_followup(promptless=True)
                    rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                    if field == "cwd":
                        rows[-2]["cwd"] = value
                    else:
                        rows[-2]["message"]["content"][0]["id"] = value
                        rows[-1]["message"]["content"][0]["tool_use_id"] = value
                    self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    with patch("os.getcwd", return_value=str(self.project)):
                        result = handle({"cwd": str(self.project), "session_id": SESSION,
                                         "hook_event_name": "Stop"}, self.environ)
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertEqual((archived,), self.store.load(self.project).recent_runs)
                    self.assertEqual(1, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_archived_claude_resume_accepts_background_ack_before_child_terminal(self):
        for explicit_background in (False, True):
            with self.subTest(explicit_background=explicit_background):
                self.setUp()
                self.prepare_archived_followup(promptless=True)
                rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                if explicit_background:
                    rows[-2]["message"]["content"][0]["input"]["run_in_background"] = True
                # Background is also the host default. The Agent result proves
                # dispatch; only the separate child native terminal proves success.
                rows[-1]["timestamp"] = "2026-09-29T02:03:00.500Z"
                rows[-1]["message"]["content"][0]["content"] = "Agent launched in background"
                rows[-1]["toolUseResult"] = {"status": "async_launched", "agentId": LEAD}
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])
                self.assertIn("prompt_id:prompt-two",
                              self.store.load(self.project).recent_runs[0].assessment["_terminal_turns"][LEAD])

    def test_archived_claude_followup_drains_bound_child_alias_and_replays(self):
        for parent in (None, SESSION):
            for crash in (False, True):
                with self.subTest(parent=parent, crash=crash):
                    self.setUp()
                    _, terminal = self.prepare_archived_followup(promptless=True)
                    record = self.store.session_record("claude", SESSION)
                    record["pending"] = []
                    self.store._write_json(self.store._session_path("claude", SESSION), record)
                    self.store.bind_session("claude", LEAD, self.store._path(self.project),
                                            False, self.project, SESSION)
                    terminal["session_id"] = LEAD
                    if parent:
                        terminal["parent_thread_id"] = parent
                    started = {key: value for key, value in terminal.items()
                               if key not in {"status", "last_assistant_message"}}
                    started["hook_event_name"] = "SubagentStart"
                    handle(started, self.environ)
                    handle(terminal, self.environ)
                    root = {"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}
                    if crash:
                        with patch.object(StateStore, "finish_session_events", side_effect=OSError("crash")):
                            with self.assertRaises(OSError):
                                handle(root, self.environ)
                    result = handle(root, self.environ)
                    self.assertNotIn('"decision": "block"', result.stdout)
                    before = self.store.load(self.project)
                    self.assertEqual(1, len(before.recent_runs))
                    self.assertIn("prompt_id:prompt-two", before.recent_runs[0].assessment["_terminal_turns"][LEAD])
                    receipt = next(item for item in before.terminal_receipts if item["turn"] == "prompt_id:prompt-two")
                    self.assertTrue(receipt["native_followup_start_id"].endswith(":followup-start"))
                    self.assertEqual([], self.store.session_record("claude", LEAD)["pending"])
                    handle(started, self.environ)
                    handle(terminal, self.environ)
                    result = handle(root, self.environ)
                    self.assertNotIn('"decision": "block"', result.stdout)
                    self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                    self.assertEqual(before.terminal_receipts, self.store.load(self.project).terminal_receipts)
                    self.assertEqual([], self.store.session_record("claude", LEAD)["pending"])

    def test_archived_claude_followup_retains_foreign_parent_and_sibling_alias(self):
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                self.setUp()
                self.prepare_archived_followup(promptless=True)
                record = self.store.session_record("claude", SESSION)
                terminal = record["pending"][0]["payload"]
                terminal["hook_event_name"] = "SubagentStop"
                record["pending"] = []
                self.store._write_json(self.store._session_path("claude", SESSION), record)
                alias = "sibling-child" if sibling else LEAD
                self.store.bind_session("claude", alias, self.store._path(self.project),
                                        False, self.project, SESSION)
                terminal["session_id"] = alias
                terminal["parent_thread_id"] = SESSION if sibling else "foreign-root"
                handle(terminal, self.environ)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                state = self.store.load(self.project)
                self.assertNotIn("prompt_id:prompt-two", state.recent_runs[0].assessment["_terminal_turns"][LEAD])
                self.assertIsNone(state.active_run)
                self.assertEqual(1, len(self.store.session_record("claude", alias)["pending"]))

    def test_archived_claude_resume_matches_text_block_root_prompt(self):
        for uuid in ("root-two", None, "", 123):
            with self.subTest(uuid=uuid):
                self.setUp()
                _, terminal = self.prepare_archived_followup(with_start=True)
                rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                rows[-3]["message"]["content"] = [{"type": "text", "text": "Continue the original task."}]
                rows[-3]["uuid"] = uuid
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                root = {"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}
                result = handle(root, self.environ)
                if uuid != "root-two":
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertEqual(2, len(self.store.session_record("claude", SESSION)["pending"]))
                    continue
                self.assertNotIn('"decision": "block"', result.stdout)
                state = self.store.load(self.project)
                self.assertIn("prompt_id:prompt-two", state.recent_runs[0].assessment["_terminal_turns"][LEAD])
                handle(terminal, self.environ)
                self.assertNotIn('"decision": "block"', handle(root, self.environ).stdout)
                self.assertEqual(state.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])

    def test_archived_claude_resume_retains_conflicting_start_route(self):
        for field in ("agent_type", "model", "model_reasoning_effort"):
            with self.subTest(field=field):
                self.setUp()
                self.prepare_archived_followup(with_start=True)
                record = self.store.session_record("claude", SESSION)
                record["pending"][0]["payload"][field] = (
                    "symphony:symphony-lead-claude-opus-5-5-high" if field == "agent_type" else "wrong")
                self.store._write_json(self.store._session_path("claude", SESSION), record)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                state = self.store.load(self.project)
                self.assertNotIn("prompt_id:prompt-two", state.recent_runs[0].assessment["_terminal_turns"][LEAD])
                self.assertIsNone(state.active_run)
                self.assertEqual(2, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_archived_claude_resume_requires_prompt_after_archive_and_before_call(self):
        for timestamp in (None, "invalid", "2026-09-29T02:02:00Z", "2026-09-29T02:02:01Z",
                          "2026-09-29T02:03:00.000001Z", "2026-09-29T02:03:00Z"):
            with self.subTest(timestamp=timestamp):
                self.setUp()
                archived, _ = self.prepare_archived_followup(with_start=True)
                rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
                rows[-3]["timestamp"] = timestamp
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                if timestamp == "2026-09-29T02:03:00Z":
                    self.assertNotIn('"decision": "block"', result.stdout)
                    self.assertEqual([], self.store.session_record("claude", SESSION)["pending"])
                else:
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertEqual((archived,), self.store.load(self.project).recent_runs)
                    self.assertEqual(2, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_committed_claude_resume_replay_rejects_missing_or_future_prompt_time(self):
        _, terminal = self.prepare_archived_followup(with_start=True)
        handle({"cwd": str(self.project), "session_id": SESSION, "hook_event_name": "Stop"}, self.environ)
        archived = self.store.load(self.project)
        source = event_from_payload("claude", terminal)
        self.assertTrue(claude_committed_native_terminal_replay(archived, source, SESSION, self.project, self.environ))
        rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
        for timestamp in (None, "invalid", "2026-09-29T02:03:00.000001Z"):
            with self.subTest(timestamp=timestamp):
                rows[-3]["timestamp"] = timestamp
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
                self.assertFalse(claude_committed_native_terminal_replay(
                    archived, source, SESSION, self.project, self.environ))

    def test_archived_claude_resume_requires_exact_successful_evidence(self):
        for change, promptless in ((change, promptless) for change in (
                "missing-resume", "failed-resume", "foreign-resume", "duplicate-resume",
                "foreign-project", "foreign-root", "missing-result", "markerless", "failed-native",
                "wrong-model", "wrong-effort", "foreign-parent", "wrong-prompt", "future-generation")
                for promptless in (False, True)):
            with self.subTest(change=change, promptless=promptless):
                self.setUp()
                self.prepare_archived_followup(promptless=promptless)
                parent = [json.loads(line) for line in self.parent.read_text().splitlines()]
                child = [json.loads(line) for line in self.child.read_text().splitlines()]
                if change == "missing-resume":
                    del parent[-2]
                elif change == "failed-resume":
                    parent[-1]["message"]["content"][0]["is_error"] = True
                elif change == "foreign-resume":
                    parent[-2]["message"]["content"][0]["input"]["resume"] = "other-child"
                elif change == "duplicate-resume":
                    parent.append(parent[-2])
                elif change == "foreign-project":
                    parent[-2]["cwd"] = str(self.project.parent)
                elif change == "foreign-root":
                    parent[-2]["sessionId"] = "foreign"
                elif change == "missing-result":
                    parent.pop()
                elif change in {"markerless", "failed-native"}:
                    child[-1]["message"]["content"][0]["text"] = (
                        "Done" if change == "markerless" else 'SYMPHONY_OUTCOME: {"status":"blocked"}')
                elif change == "wrong-model":
                    child[-1]["message"]["model"] = "wrong"
                elif change == "wrong-effort":
                    child[-1]["effort"] = "wrong"
                else:
                    record = self.store.session_record("claude", SESSION)
                    if change == "future-generation":
                        record["pending"][0]["generation"] += 1
                    else:
                        record["pending"][0]["payload"]["parent_thread_id" if change == "foreign-parent" else "prompt_id"] = "foreign"
                    self.store._write_json(self.store._session_path("claude", SESSION), record)
                self.parent.write_text("".join(json.dumps(row) + "\n" for row in parent))
                self.child.write_text("".join(json.dumps(row) + "\n" for row in child))
                before = self.store.load(self.project)
                result = handle({"cwd": str(self.project), "session_id": SESSION,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                self.assertIsNone(self.store.load(self.project).active_run)
                self.assertEqual(1, len(self.store.session_record("claude", SESSION)["pending"]))

    def recovered(self):
        return claude_recovered_lead_event(ProjectState(active_run=self.run), SESSION,
                                           self.project, self.environ)

    def write_root_prompt(self):
        parent = self.parent.read_text()
        root_prompt = {"type": "user", "uuid": "root-prompt", "sessionId": SESSION,
                       "timestamp": "2026-09-29T02:00:30Z",
                       "message": {"content": "Start the original managed lead."}}
        self.parent.write_text(json.dumps(root_prompt) + "\n" + parent)

    def test_archived_native_result_accepts_late_original_root_prompt_callback(self):
        self.write_root_prompt()
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None,
                            stop.stdout)
        archived = self.store.load(self.project)
        self.assertIsNone(archived.active_run)
        self.assertEqual("original-run", archived.recent_runs[-1].run_id)
        self.assertEqual("completed", archived.recent_runs[-1].status)
        callback = {"session_id": SESSION, "cwd": str(self.project),
                    "hook_event_name": "SubagentStop", "agent_id": LEAD,
                    "agent_type": TYPE, "prompt_id": "root-prompt",
                    "agent_transcript_path": str(self.child),
                    "last_assistant_message": REPORT, "status": "completed"}
        handle(callback, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", SESSION)
        self.assertFalse(record and record["pending"])
        self.assertEqual(archived.recent_runs[-1].run_id,
                         self.store.load(self.project).recent_runs[-1].run_id)

        # A callback queued by the previous hook must clear on the next root
        # event without losing its original run or needing a new lead.
        with self.store.session_lock("claude", SESSION):
            self.store.queue_session_event(
                "claude", SESSION, event_from_payload("claude", callback),
                ambiguous_owner=True)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", SESSION)
        self.assertFalse(record and record["pending"])

        for changed in ("foreign-root-prompt", "prompt-two"):
            with self.subTest(changed=changed):
                source = event_from_payload("claude", {**callback, "prompt_id": changed})
                self.assertFalse(claude_committed_native_terminal_replay(
                    archived, source, SESSION, self.project, self.environ))

        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.append({"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
                     "agentId": LEAD, "isSidechain": True,
                     "timestamp": "2026-09-29T02:03:00Z",
                     "message": {"content": "New work is still running."}})
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        source = event_from_payload("claude", callback)
        self.assertFalse(claude_committed_native_terminal_replay(
            archived, source, SESSION, self.project, self.environ))
        conflicted = Event(source.event_id, source.kind, source.observed_at,
                           {**source.payload, "_symphony_owner_conflict": True})
        self.assertFalse(claude_committed_native_terminal_replay(
            archived, conflicted, SESSION, self.project, self.environ))

    def test_archived_native_result_accepts_exact_lead_start_hook_prompt(self):
        self.write_root_prompt()
        launch = {"session_id": SESSION, "cwd": str(self.project),
                  "hook_event_name": "SubagentStart", "agent_id": LEAD,
                  "agent_type": TYPE, "prompt_id": "shared-hook-prompt",
                  "status": "working"}
        # The native launch precedes the child terminal. Hook arrival time in
        # this fixture must reflect that ordering for Stop freshness checks.
        started = replace(event_from_payload("claude", launch),
                          observed_at="2026-09-29T02:01:02+00:00")
        state, _ = _observe_delegation(self.store.load(self.project), started)
        self.assertIn("_claude_lead_start_prompt_hash", state.active_run.assessment,
                      state.active_run.assessment)
        self.store.save(self.project, replace(state, active_runs={f"claude:{SESSION}": state.active_run}))
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None,
                            stop.stdout)
        archived = self.store.load(self.project)
        self.assertIsNone(archived.active_run)
        self.assertEqual("completed", archived.recent_runs[-1].status)
        self.assertEqual(1, len(archived.terminal_receipts))
        self.assertIn("native_launch_prompt_hash", archived.terminal_receipts[0],
                      archived.terminal_receipts)
        self.assertEqual(
            archived.recent_runs[-1].assessment["_claude_lead_start_prompt_hash"],
            archived.terminal_receipts[0]["native_launch_prompt_hash"],
        )
        callback = {**launch, "hook_event_name": "SubagentStop", "status": "completed",
                    "agent_transcript_path": str(self.child),
                    "last_assistant_message": REPORT}
        source = event_from_payload("claude", callback)
        self.assertTrue(claude_committed_native_terminal_replay(
            replace(archived, recent_runs=()), source, SESSION,
            self.project, self.environ))
        handle(callback, self.environ)
        record = self.store.session_record("claude", SESSION)
        self.assertFalse(record and record["pending"])
        for prompt in ("assessor-only-prompt", "newer-hook-prompt"):
            with self.subTest(prompt=prompt):
                source = event_from_payload("claude", {**callback, "prompt_id": prompt})
                self.assertFalse(claude_committed_native_terminal_replay(
                    archived, source, SESSION, self.project, self.environ))
        original_source = event_from_payload("claude", callback)
        conflicted = replace(original_source, payload={**original_source.payload,
                                              "_symphony_owner_conflict": True})
        self.assertTrue(claude_committed_native_terminal_replay(
            archived, conflicted, SESSION, self.project, self.environ))
        with self.store.session_lock("claude", SESSION):
            self.store.queue_session_event("claude", SESSION, original_source,
                                           ambiguous_owner=True)
        stop_again = handle({"session_id": SESSION, "cwd": str(self.project),
                             "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop_again.stdout).get("decision")
                            if stop_again.stdout else None, stop_again.stdout)
        self.assertFalse(self.store.session_record("claude", SESSION)["pending"])
        for foreign in ({"session_id": "foreign-root"},
                        {"parent_thread_id": "foreign-root"}):
            with self.subTest(foreign=foreign):
                self.assertFalse(claude_committed_native_terminal_replay(
                    archived, event_from_payload("claude", {**callback, **foreign}),
                    SESSION, self.project, self.environ))
        other = replace(self.run, session_id="foreign-root", status="active")
        shared_identity = replace(archived, active_runs={"claude:foreign-root": other})
        foreign_path = self.root / "foreign-child.jsonl"
        foreign_path.write_text(self.child.read_text() + json.dumps({
            "type": "user", "uuid": "foreign-newer-prompt", "sessionId": "foreign-root",
            "agentId": LEAD, "isSidechain": True,
            "timestamp": "2026-09-29T02:04:00Z",
            "message": {"content": "New work in another root."}}) + "\n")
        ambiguous_foreign = event_from_payload("claude", {
            **callback, "session_id": LEAD, "parent_thread_id": "",
            "agent_transcript_path": str(foreign_path),
        })
        ambiguous_foreign = replace(ambiguous_foreign, payload={
            **ambiguous_foreign.payload, "_symphony_owner_conflict": True})
        self.assertFalse(claude_committed_native_terminal_replay(
            shared_identity, ambiguous_foreign, SESSION, self.project,
            self.environ))
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.append({"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
                     "agentId": LEAD, "isSidechain": True,
                     "timestamp": "2026-09-29T02:03:00Z",
                     "message": {"content": "A newer turn is running."}})
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertFalse(claude_committed_native_terminal_replay(
            archived, event_from_payload("claude", callback), SESSION,
            self.project, self.environ))

    def test_replacement_lead_receipt_uses_its_own_launch_prompt(self):
        replacement_id = "b9c4054c282207da0"
        original = replace(
            self.run, status="recovering",
            assessment={**self.run.assessment,
                        "_claude_lead_start_identity": LEAD,
                        "_claude_lead_start_prompt_hash": hashlib.sha256(
                            b"old-lead-hook-prompt").hexdigest()})
        state = ProjectState(active_run=original)
        launch = {"session_id": SESSION, "cwd": str(self.project),
                  "hook_event_name": "SubagentStart", "agent_id": replacement_id,
                  "agent_type": TYPE, "prompt_id": "replacement-hook-prompt",
                  "status": "working"}
        state, _ = _observe_delegation(state, event_from_payload("claude", launch))
        self.assertEqual(replacement_id, state.active_run.lead_identity)
        self.assertEqual(replacement_id,
                         state.active_run.assessment["_claude_lead_start_identity"])
        replacement_hash = hashlib.sha256(b"replacement-hook-prompt").hexdigest()
        self.assertEqual(replacement_hash,
                         state.active_run.assessment["_claude_lead_start_prompt_hash"])
        state, _ = _observe_delegation(state, event_from_payload("claude", {
            **launch, "hook_event_name": "SubagentStop", "status": "completed",
            "prompt_id": "new-native-child-prompt",
            "model": "claude-sonnet-5", "model_reasoning_effort": "low",
            "last_assistant_message": REPORT,
            "_symphony_native_recovery": True,
        }))
        self.assertEqual(replacement_hash,
                         state.terminal_receipts[-1]["native_launch_prompt_hash"])

    def test_malformed_parent_prompt_with_queued_callback_blocks_stop(self):
        self.write_root_prompt()
        rows = [json.loads(line) for line in self.parent.read_text().splitlines()]
        rows[0]["message"] = "malformed native prompt"
        self.parent.write_text("".join(json.dumps(row) + "\n" for row in rows))
        callback = event_from_payload("claude", {
            "session_id": SESSION, "hook_event_name": "SubagentStop",
            "agent_id": LEAD, "agent_type": TYPE, "prompt_id": "root-prompt",
            "last_assistant_message": REPORT, "status": "completed"})
        with self.store.session_lock("claude", SESSION):
            self.store.queue_session_event("claude", SESSION, callback,
                                           ambiguous_owner=True)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout).get("decision"))
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)
        self.assertEqual(1, len(self.store.session_record("claude", SESSION)["pending"]))

    def test_missing_old_callback_reconciles_original_run_on_root_stop(self):
        recovered = self.recovered()
        self.assertIsNotNone(recovered)
        self.assertEqual(LEAD, recovered.payload["agent_id"])
        self.assertEqual("prompt-one", recovered.payload["prompt_id"])
        response = handle({"session_id": SESSION, "cwd": str(self.project),
                           "hook_event_name": "UserPromptSubmit",
                           "prompt": "$symphony:symphony status"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("end this root turn; the native Stop hook verifies and archives the run", context)
        self.assertNotIn("tracked work still requires reconciliation", context)
        active = self.store.load(self.project).active_run
        self.assertEqual("original-run", active.run_id)
        self.assertEqual("completing", active.status)
        receipts = len(self.store.load(self.project).terminal_receipts)
        # A delayed ordinary hook for the same native result is a replay.
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        after_callback = self.store.load(self.project)
        self.assertEqual("completing", after_callback.active_run.status)
        self.assertEqual(receipts, len(after_callback.terminal_receipts))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None)
        settled = self.store.load(self.project)
        self.assertIsNone(settled.active_run)
        self.assertEqual("original-run", settled.recent_runs[-1].run_id)
        self.assertEqual("completed", settled.recent_runs[-1].outcome["status"])
        for delivered_session in (SESSION, LEAD):
            handle({"session_id": delivered_session, "cwd": str(self.project),
                    "hook_event_name": "SubagentStop", "agent_id": LEAD,
                    "parent_thread_id": SESSION, "agent_type": TYPE,
                    "agent_transcript_path": str(self.child),
                    "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        self.assertIsNone(self.store.load(self.project).active_run)
        for identity in (SESSION, LEAD):
            record = self.store.session_record("claude", identity)
            self.assertFalse(record and record["pending"])
        self.store.save(self.project, replace(self.store.load(self.project), recent_runs=()))
        handle({"session_id": LEAD, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child), "retry": 2,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", LEAD)
        self.assertFalse(record and record["pending"])

    def test_session_start_guides_stop_after_native_recovery(self):
        response = handle({"session_id": SESSION, "cwd": str(self.project),
                           "hook_event_name": "SessionStart"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("end this root turn; the native Stop hook verifies and archives the run", context)
        self.assertNotIn("continue unfinished work", context)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_latest_successful_background_handback_is_accepted(self):
        self.write_native(handback=True)
        self.assertIsNotNone(self.recovered())
        self.write_native(handback=True, handback_error=True)
        self.assertIsNone(self.recovered())
        self.write_native(handback=True)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows[-1]["message"]["content"][0]["text"] = (
            'SYMPHONY_OUTCOME: {"status":"blocked"}')
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertIsNone(self.recovered())

    def test_newer_prompt_blocks_stop_after_native_completion(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)
        self.write_native(later_prompt=True)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_newer_native_prompt_blocks_stop_after_ordinary_hook_completion(self):
        # The ordinary callback can arrive before Claude finishes writing its
        # end_turn record, so no native recovery anchor is attached yet.
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        active = self.store.load(self.project).active_run
        self.assertEqual("completing", active.status)
        self.assertNotIn("_claude_native_recovery", active.assessment)
        self.write_native(later_prompt=True)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        accepted_at = datetime.fromisoformat(next(item.updated_at for item in active.delegations
                                                   if item.identity == LEAD))
        rows[-1]["timestamp"] = (accepted_at + timedelta(microseconds=1)).isoformat()
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)

    def test_accepted_claude_launch_intent_prevents_archive_before_child_start(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        launched = handle({"session_id": SESSION, "cwd": str(self.project),
                           "hook_event_name": "PreToolUse", "tool_name": "Agent",
                           "tool_input": {"subagent_type":
                                          "symphony:symphony-worker-claude-sonnet-5-low",
                                          "prompt": "SYMPHONY_ROLE: worker\nComplete the task."}},
                          self.environ)
        self.assertNotEqual("block", json.loads(launched.stdout).get("decision")
                            if launched.stdout else None)
        active = self.store.load(self.project).active_run
        self.assertTrue(active.assessment.get("_pending_delegations"))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)

    def test_ordinary_callback_archives_when_native_turn_has_no_later_prompt(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        self.assertNotIn("_claude_native_recovery", self.store.load(self.project).active_run.assessment)
        self.write_native()
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("completed", self.store.load(self.project).recent_runs[-1].status)

    def test_ordinary_callback_routes_newer_valid_native_completion_before_stop(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        active = self.store.load(self.project).active_run
        self.write_native()
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        accepted_at = datetime.fromisoformat(next(item.updated_at for item in active.delegations
                                                   if item.identity == LEAD))
        started = accepted_at + timedelta(microseconds=1)
        rows.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": started.isoformat(),
             "message": {"content": "Continue the same task."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True,
             "timestamp": (started + timedelta(microseconds=1)).isoformat(), "effort": "low",
             "message": {"model": "claude-sonnet-5", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": REPORT}]}},
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        settled = self.store.load(self.project)
        self.assertEqual("completed", settled.recent_runs[-1].status)
        self.assertIn("prompt_id:prompt-two",
                      settled.recent_runs[-1].assessment["_terminal_turns"][LEAD])

    def prepare_ordinary_markerless_continuation(self, *, handback=False, report="Task completed.", prompt_count=2):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": "Task completed.", "status": "completed"}, self.environ)
        active = self.store.load(self.project).active_run
        self.assertEqual("completing", active.status)
        self.assertNotIn("_claude_native_recovery", active.assessment)
        self.write_native(report=report if prompt_count == 1 else "Earlier result.",
                          handback=handback and prompt_count == 1)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        if prompt_count == 1:
            return active, rows
        began = datetime.fromisoformat(active.delegations[0].updated_at) + timedelta(seconds=1)
        if prompt_count == 3:
            rows.extend([
                {"type": "user", "uuid": "prompt-middle", "sessionId": SESSION,
                 "agentId": LEAD, "isSidechain": True, "timestamp": (began - timedelta(seconds=0.5)).isoformat(),
                 "message": {"content": "Reconcile the same task."}},
                {"type": "assistant", "uuid": "terminal-middle", "sessionId": SESSION,
                 "agentId": LEAD, "isSidechain": True, "timestamp": (began - timedelta(seconds=0.25)).isoformat(),
                 "effort": "low", "message": {"model": "claude-sonnet-5", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "Earlier reconciliation completed."}]}},
            ])
        rows.append({"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
            "agentId": LEAD, "isSidechain": True, "timestamp": began.isoformat(),
            "message": {"content": "Reconcile the same task."}})
        if handback:
            rows.extend([
                {"type": "assistant", "uuid": "handback-two", "sessionId": SESSION,
                 "agentId": LEAD, "isSidechain": True, "timestamp": (began + timedelta(seconds=1)).isoformat(),
                 "effort": "low", "message": {"model": "claude-sonnet-5", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "name": "SubagentHandback", "id": "toolu-two",
                                  "input": {"message": report}}]}},
                {"type": "user", "uuid": "handback-result-two", "sessionId": SESSION,
                 "agentId": LEAD, "isSidechain": True, "timestamp": (began + timedelta(seconds=2)).isoformat(),
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu-two",
                                          "is_error": False}]}},
            ])
        rows.append({"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
            "agentId": LEAD, "isSidechain": True, "timestamp": (began + timedelta(seconds=3)).isoformat(),
            "effort": "low", "message": {"model": "claude-sonnet-5", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Goodbye." if handback else report}]}})
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return active, rows

    def test_ordinary_assessed_markerless_latest_turn_archives_through_stop(self):
        from plugins.symphony.symphony.host_evidence import _claude_native_lead_event
        for count, handback, report in ((count, handback, report) for count in (1, 2, 3)
                                       for handback, report in ((False, "Task completed."),
                                                               (True, "Task completed."), (True, REPORT))):
            with self.subTest(prompt_count=count, handback=handback, report=report):
                self.store.save(self.project, ProjectState(active_run=self.run))
                active, _ = self.prepare_ordinary_markerless_continuation(
                    handback=handback, report=report, prompt_count=count)
                state = self.store.load(self.project)
                if report != REPORT:
                    self.assertIsNone(_claude_native_lead_event(state, SESSION, self.project, self.environ,
                                                              require_missing=False))
                stop = handle({"session_id": SESSION, "cwd": str(self.project),
                               "hook_event_name": "Stop"}, self.environ)
                self.assertNotEqual("block", json.loads(stop.stdout or "{}").get("decision"))
                settled = self.store.load(self.project)
                self.assertIsNone(settled.active_run)
                self.assertEqual(active.run_id, settled.recent_runs[-1].run_id)
                self.assertEqual("completed", settled.recent_runs[-1].status)
                if count > 1:
                    self.assertIn("prompt_id:prompt-two", settled.recent_runs[-1].assessment["_terminal_turns"][LEAD])

    def test_assessed_markerless_freshness_preserves_conflicting_and_strict_evidence_guards(self):
        cases = ("unfinished", "wrong-model", "wrong-effort", "foreign-parent", "foreign-child",
                 "failed-final", "blocked-outcome", "malformed-outcome", "duplicate-outcome",
                 "failed-handback", "duplicate-handback", "failed-handback-outcome",
                 "malformed-handback-outcome", "recovery-anchor", "fast-owner", "fast-pending")
        for case in cases:
            with self.subTest(case=case):
                self.store.save(self.project, ProjectState(active_run=self.run))
                active, rows = self.prepare_ordinary_markerless_continuation(handback="handback" in case)
                if case == "unfinished":
                    rows.append({**rows[-1], "type": "user", "uuid": "prompt-three",
                        "timestamp": (datetime.fromisoformat(rows[-1]["timestamp"]) + timedelta(seconds=1)).isoformat(),
                        "message": {"content": "More work."}})
                elif case == "wrong-model":
                    rows[-1]["message"]["model"] = "foreign-model"
                elif case == "wrong-effort":
                    rows[-1]["effort"] = "high"
                elif case == "foreign-parent":
                    parent = json.loads(self.parent.read_text().splitlines()[0])
                    parent["sessionId"] = "foreign-root"
                    self.parent.write_text(json.dumps(parent) + "\n")
                elif case == "foreign-child":
                    rows[-1]["agentId"] = "foreign-child"
                elif case == "failed-final":
                    rows[-1]["message"]["stop_reason"] = "error"
                elif case in {"blocked-outcome", "malformed-outcome", "duplicate-outcome"}:
                    rows[-1]["message"]["content"][0]["text"] = ({
                        "blocked-outcome": 'SYMPHONY_OUTCOME: {"status":"blocked"}',
                        "malformed-outcome": "SYMPHONY_OUTCOME: broken",
                        "duplicate-outcome": REPORT + "\n" + REPORT})[case]
                elif case == "failed-handback":
                    rows[-2]["message"]["content"][0]["is_error"] = True
                elif case == "duplicate-handback":
                    rows[-3]["message"]["content"].append(dict(rows[-3]["message"]["content"][0]))
                elif case in {"failed-handback-outcome", "malformed-handback-outcome"}:
                    rows[-3]["message"]["content"][0]["input"]["message"] = (
                        'SYMPHONY_OUTCOME: {"status":"failed"}' if case == "failed-handback-outcome"
                        else "SYMPHONY_OUTCOME: broken")
                else:
                    assessment = dict(active.assessment)
                    if case == "recovery-anchor":
                        assessment["_claude_native_recovery"] = "prompt_id:prompt-one"
                    elif case == "fast-owner":
                        assessment["_fast_route"] = {"model": "claude-sonnet-5", "effort": "low"}
                    elif case == "fast-pending":
                        assessment["_fast_pending"] = True
                    active = replace(active, assessment=assessment)
                    state = self.store.load(self.project)
                    self.store.save(self.project, replace(state, active_run=active,
                        active_runs={f"claude:{SESSION}": active}))
                self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
                stop = handle({"session_id": SESSION, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
                held = self.store.load(self.project)
                self.assertEqual(active.run_id, held.active_run.run_id)
                self.assertEqual("completing", held.active_run.status)
                self.assertEqual({"status": "completed"}, held.active_run.outcome)

    def test_markerless_freshness_rechecks_after_pending_launch_settles(self):
        for settlement in ("failed-launch", "completed-worker"):
            with self.subTest(settlement=settlement):
                self.store.save(self.project, ProjectState(active_run=self.run))
                active, _ = self.prepare_ordinary_markerless_continuation()
                worker_input = {"subagent_type": "symphony:symphony-worker-claude-sonnet-5-low",
                                "prompt": "SYMPHONY_ROLE: worker\nFinish bounded work."}
                common = {"session_id": SESSION, "cwd": str(self.project)}
                handle({**common, "hook_event_name": "PreToolUse", "tool_name": "Agent",
                        "tool_input": worker_input}, self.environ)
                request = {**common, "hook_event_name": "Stop"}
                blocked = handle(request, self.environ)
                self.assertEqual("block", json.loads(blocked.stdout or "{}").get("decision"))
                held = self.store.load(self.project)
                self.assertEqual(active.run_id, held.active_run.run_id)
                self.assertEqual("completing", held.active_run.status)
                self.assertNotIn("_claude_native_recovery", held.active_run.assessment)
                if settlement == "failed-launch":
                    handle({**common, "hook_event_name": "PostToolUseFailure", "tool_name": "Agent",
                            "tool_input": worker_input, "error": "Launch rejected"}, self.environ)
                else:
                    worker = {**common, "agent_id": "aa1234567890abcd", "parent_thread_id": LEAD,
                              "agent_type": worker_input["subagent_type"]}
                    handle({**worker, "hook_event_name": "SubagentStart"}, self.environ)
                    still_working = handle(request, self.environ)
                    self.assertEqual("block", json.loads(still_working.stdout or "{}").get("decision"))
                    handle({**worker, "hook_event_name": "SubagentStop", "status": "completed",
                            "last_assistant_message": "Bounded work finished."}, self.environ)
                archived = handle(request, self.environ)
                self.assertNotEqual("block", json.loads(archived.stdout or "{}").get("decision"))
                settled = self.store.load(self.project)
                self.assertIsNone(settled.active_run)
                self.assertEqual(active.run_id, settled.recent_runs[-1].run_id)
                self.assertEqual("completed", settled.recent_runs[-1].status)

    def test_delayed_old_callback_cannot_archive_newer_unfinished_native_turn(self):
        self.write_native(later_prompt=True)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        self.assertNotIn("_claude_native_recovery", self.store.load(self.project).active_run.assessment)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)

    def test_partial_native_tail_does_not_erase_known_newer_turn(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        self.write_native(later_prompt=True)
        with self.child.open("a") as stream:
            stream.write('{"type":')
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)

    def test_malformed_unrelated_parent_row_blocks_instead_of_crashing(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        self.write_native(later_prompt=True)
        with self.parent.open("a") as stream:
            stream.write(json.dumps({"type": "assistant", "sessionId": SESSION,
                                     "message": "malformed unrelated row"}) + "\n")
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.write_native(later_prompt=True)
        with self.parent.open("a") as stream:
            stream.write(json.dumps({"type": "assistant", "sessionId": SESSION,
                                     "message": {"content": 1}}) + "\n")
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))

    def test_malformed_child_user_row_cannot_be_skipped_as_single_turn(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        for later_prompt in (False, True):
            with self.subTest(later_prompt=later_prompt):
                self.write_native(later_prompt=later_prompt)
                with self.child.open("a") as stream:
                    stream.write(json.dumps({"type": "user", "sessionId": SESSION,
                                             "agentId": LEAD, "isSidechain": True,
                                             "message": "malformed new prompt"}) + "\n")
                stop = handle({"session_id": SESSION, "cwd": str(self.project),
                               "hook_event_name": "Stop"}, self.environ)
                self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))

    def test_child_user_block_list_cannot_hide_a_new_prompt(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        for content in ([None], [{"type": "text", "text": "Do more work."}]):
            with self.subTest(content=content):
                self.write_native()
                with self.child.open("a") as stream:
                    stream.write(json.dumps({"type": "user", "sessionId": SESSION,
                                             "agentId": LEAD, "isSidechain": True,
                                             "message": {"content": content}}) + "\n")
                stop = handle({"session_id": SESSION, "cwd": str(self.project),
                               "hook_event_name": "Stop"}, self.environ)
                self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))

    def test_malformed_newer_assistant_text_blocks_stop_without_hook_exception(self):
        self.write_native(final=False)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        self.write_native(later_prompt=True)
        with self.child.open("a") as stream:
            stream.write(json.dumps({"type": "assistant", "uuid": "terminal-two",
                                     "sessionId": SESSION, "agentId": LEAD,
                                     "isSidechain": True, "timestamp": "2026-09-29T02:04:00Z",
                                     "effort": "low", "message": {
                                         "model": "claude-sonnet-5", "stop_reason": "end_turn",
                                         "content": [{"type": "text", "text": 123}]}}) + "\n")
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("original-run", self.store.load(self.project).active_run.run_id)

    def test_ordinary_callback_without_native_transcript_keeps_hook_authority(self):
        self.child.unlink()
        self.meta.unlink()
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout or "{}").get("decision"))
        self.assertEqual("completed", self.store.load(self.project).recent_runs[-1].status)

    def test_new_lead_invocation_after_archive_needs_a_fresh_run(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        settled = handle({"session_id": SESSION, "cwd": str(self.project),
                          "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(settled.stdout or "{}").get("decision"))
        original = self.store.load(self.project).recent_runs[-1]
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStart", "agent_id": "ordinary-explore",
                "parent_thread_id": SESSION, "agent_type": "Explore",
                "prompt_id": "ordinary-prompt"}, self.environ)
        self.assertFalse(self.store.session_record("claude", SESSION)["pending"])
        sibling_lead = "b1234567890123456"
        sibling = replace(self.run, run_id="sibling-run", session_id="sibling-root",
                          lead_identity=sibling_lead,
                          delegations=(Delegation(sibling_lead, "lead", "other task", "working",
                                                  "claude-sonnet-5", "low"),))
        self.store.save(self.project, replace(self.store.load(self.project),
                        active_run=sibling, active_runs={"claude:sibling-root": sibling}))
        launch = handle({"session_id": SESSION, "cwd": str(self.project),
                         "hook_event_name": "PreToolUse", "tool_name": "Agent",
                         "tool_input": {"subagent_type": TYPE,
                                        "prompt": "SYMPHONY_ROLE: lead\n"
                                                  'SYMPHONY_ROUTE: {"size":"medium",'
                                                  '"complexity":"complex","risk":"normal"}'}},
                        self.environ)
        self.assertEqual("deny", json.loads(launch.stdout or "{}").get(
            "hookSpecificOutput", {}).get("permissionDecision"))
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStart", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "prompt_id": "new-root-prompt"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "prompt_id": "new-root-prompt",
                "last_assistant_message": "A markerless new result.",
                "status": "completed"}, self.environ)
        state = self.store.load(self.project)
        self.assertEqual("sibling-run", state.active_runs["claude:sibling-root"].run_id)
        self.assertEqual("working", state.active_runs["claude:sibling-root"].delegations[0].state)
        self.assertNotIn(f"claude:{SESSION}", state.active_runs)
        self.assertEqual(original.run_id, state.recent_runs[-1].run_id)
        self.assertEqual(original.outcome, state.recent_runs[-1].outcome)
        held = self.store.session_record("claude", SESSION)
        self.assertEqual(2, len(held["pending"]))
        blocked = handle({"session_id": SESSION, "cwd": str(self.project),
                          "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(blocked.stdout or "{}").get("decision"))

    def test_delayed_first_callback_replays_after_newer_completed_native_turn(self):
        first_report = "First result.\n" + REPORT
        second_report = "Second result.\n" + REPORT
        self.write_native(report=first_report)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": "2026-09-29T02:03:00Z",
             "message": {"content": "Continue."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": "2026-09-29T02:04:00Z",
             "effort": "low", "message": {"model": "claude-sonnet-5",
                                       "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": second_report}]}}
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None)
        self.assertEqual("completed", self.store.load(self.project).recent_runs[-1].status)
        handle({"session_id": LEAD, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child),
                "last_assistant_message": first_report, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", LEAD)
        self.assertFalse(record and record["pending"])

    def test_same_text_new_turn_is_not_swallowed_as_old_replay(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStart", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE}, self.environ)
        self.assertEqual("active", self.store.load(self.project).active_run.status)
        owned = self.store.load(self.project)
        sibling = replace(self.run, run_id="sibling-run", session_id="sibling-root",
                          lead_identity="sibling-lead")
        two_roots = replace(owned, active_run=sibling,
                            active_runs={**owned.active_runs, "claude:sibling-root": sibling})
        untagged = event_from_payload("claude", {
            "session_id": SESSION, "hook_event_name": "SubagentStop",
            "agent_id": LEAD, "parent_thread_id": SESSION,
            "agent_type": TYPE, "last_assistant_message": REPORT,
            "status": "completed"})
        self.assertFalse(claude_committed_native_terminal_replay(
            two_roots, untagged, SESSION, self.project, self.environ))
        prompted_at = datetime.now(timezone.utc)
        completed_at = prompted_at + timedelta(milliseconds=1)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": prompted_at.isoformat(),
             "message": {"content": "Continue."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": completed_at.isoformat(),
             "effort": "low", "message": {"model": "claude-sonnet-5",
                                       "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": REPORT}]}}
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child),
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        run = self.store.load(self.project).active_run
        self.assertEqual("completing", run.status)
        self.assertIn("prompt_id:prompt-two", run.assessment["_terminal_turns"][LEAD])
        self.assertEqual("completed", self.store.load(self.project).active_run.outcome["status"])

    def test_missing_partial_foreign_or_stale_evidence_never_completes(self):
        for changes in ({"parent_session": "foreign-root"}, {"agent": "foreign-agent"},
                        {"model": "claude-opus-5"}, {"effort": "high"},
                        {"final": False}, {"later_prompt": True},
                        {"report": "Done without protocol marker."}):
            with self.subTest(changes=changes):
                self.write_native(**changes)
                self.assertIsNone(self.recovered())
        self.write_native()
        self.child.write_text(self.child.read_text() + '{"type":')
        self.assertIsNone(self.recovered())

    def test_later_worker_failure_cannot_be_overwritten_by_stale_lead_terminal(self):
        failure = Event("failed-worker", "delegation_updated", "2026-09-29T02:03:00Z",
                        {"identity": "worker-one", "role": "worker", "state": "failed"})
        run = RunState(**{**self.run.__dict__, "delegations": (
            *self.run.delegations,
            Delegation("worker-one", "worker", "task", "failed", "claude-sonnet-5",
                       "low", "2026-09-29T02:03:00Z"),
        )})
        state = ProjectState(active_run=run, event_history=(failure,))
        self.assertIsNone(claude_recovered_lead_event(
            state, SESSION, self.project, self.environ))
        self.assertIsNone(claude_recovered_lead_event(
            ProjectState(active_run=run), SESSION, self.project, self.environ))

    def test_wrong_agent_route_or_parent_launch_is_rejected(self):
        self.meta.write_text(json.dumps({"agentType": "symphony:symphony-lead-claude-opus-5-low",
                                         "toolUseId": "toolu_launch", "spawnDepth": 1}))
        self.assertIsNone(self.recovered())
        self.write_native()
        parent = json.loads(self.parent.read_text())
        parent["message"]["content"][0]["id"] = "unrelated-launch"
        self.parent.write_text(json.dumps(parent) + "\n")
        self.assertIsNone(self.recovered())

    def test_replay_rejects_conflicting_owner_or_native_prompt(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        state = self.store.load(self.project)
        payload = {"session_id": SESSION, "agent_id": LEAD,
                   "parent_thread_id": SESSION, "hook_event_name": "SubagentStop",
                   "agent_type": TYPE, "last_assistant_message": REPORT,
                   "status": "completed"}
        source = event_from_payload("claude", payload)
        self.assertTrue(claude_committed_native_terminal_replay(
            state, source, SESSION, self.project, self.environ))
        for changes in ({"parent_thread_id": "foreign-root"},
                        {"prompt_id": "different-native-prompt"},
                        {"_symphony_owner_conflict": True},
                        {"agent_type": "symphony:symphony-lead-claude-opus-5-low"}):
            with self.subTest(changes=changes):
                conflicting = Event(source.event_id, source.kind, source.observed_at,
                                    {**source.payload, **changes})
                self.assertFalse(claude_committed_native_terminal_replay(
                    state, conflicting, SESSION, self.project, self.environ))


if __name__ == "__main__":
    unittest.main()
