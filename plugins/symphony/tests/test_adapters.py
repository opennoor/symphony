import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.adapters import detect_provider, event_from_payload, render
from plugins.symphony.symphony.model import Action


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(provider: str, name: str) -> dict:
    return json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))


class AdapterContractTests(unittest.TestCase):
    def test_detects_provider_from_native_payload(self):
        self.assertEqual(detect_provider(fixture("codex", "user_prompt")), "codex")
        self.assertEqual(detect_provider(fixture("claude", "user_prompt")), "claude")

    def test_user_prompt_normalizes_to_canonical_event(self):
        event = event_from_payload("codex", fixture("codex", "user_prompt"))
        self.assertEqual(event.kind, "user_prompt")
        self.assertEqual(event.payload["session_id"], "codex-session")
        self.assertEqual(event.payload["prompt"], "Implement the feature")
        self.assertEqual(event.payload["provider"], "codex")

    def test_codex_subagent_event_reads_name_and_effort_from_child_transcript(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / "child.jsonl"
            transcript.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "agent-1",
                                    "agent_path": "/root/symphony_assessor_gpt_6_astra_high",
                                    "source": {"subagent": {"thread_spawn": {
                                        "parent_thread_id": "root-session",
                                    }}},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn_context",
                                "payload": {"model": "gpt-6-astra", "effort": "high"},
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )

            event = event_from_payload(
                "codex",
                {
                    "hook_event_name": "SubagentStart",
                    "agent_id": "agent-1",
                    "agent_type": "default",
                    "model": "root-model",
                    "transcript_path": str(transcript),
                },
            )

        self.assertEqual(event.payload["task_name"], "symphony_assessor_gpt_6_astra_high")
        self.assertEqual(event.payload["parent_thread_id"], "root-session")
        self.assertEqual(event.payload["model"], "gpt-6-astra")
        self.assertEqual(event.payload["model_reasoning_effort"], "high")

    def test_malformed_transcript_rows_do_not_hide_child_metadata_or_handback(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / "child.jsonl"
            transcript.write_text('not json\n' + json.dumps({"type": "session_meta", "payload": {
                "id": "child",
                "agent_path": "/root/symphony_worker_model_high", "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "lead-thread"}},
                },
            }}) + '\n' + json.dumps({"type": "turn_context", "payload": {
                "model": "model", "effort": "high",
            }}) + '\n{"SubagentHandback": broken}\n' + json.dumps({"message": {"content": [{
                "type": "tool_use", "name": "SubagentHandback", "input": {
                    "message": 'SYMPHONY_OUTCOME: {"status":"blocked"}',
                },
            }]}}), encoding="utf-8")
            payload = {"hook_event_name": "SubagentStop", "agent_id": "child", "agent_transcript_path": str(transcript)}
            codex = event_from_payload("codex", payload)
            claude = event_from_payload("claude", payload)
            self.assertEqual(codex.payload["parent_thread_id"], "lead-thread")
            self.assertNotIn("model_reasoning_effort", codex.payload)
            self.assertIn('"blocked"', claude.payload["last_assistant_message"])

    def test_claude_handback_stays_in_its_native_child_turn(self):
        blocked = 'SYMPHONY_OUTCOME: {"status":"blocked"}'
        completed = 'SYMPHONY_OUTCOME: {"status":"completed"}'
        def prompt(content):
            return {"type": "user", "message": {"content": content}}
        def report(text):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use",
                "name": "SubagentHandback", "input": {"message": text}}]}}
        def final(text):
            return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        for case in ('plain-prompt', 'text-block-prompt', 'new-handback', 'same-turn-result', 'delayed-stop'):
            with self.subTest(case=case), TemporaryDirectory() as temporary:
                transcript = Path(temporary) / 'child.jsonl'
                rows = [prompt('First task.'), report(blocked), final('First turn ended.')]
                latest = 'Latest turn ended.'
                if case == 'same-turn-result':
                    rows.append(prompt([{"type": "tool_result", "tool_use_id": "handback", "content": "OK"}]))
                    expected = blocked
                else:
                    rows.append(prompt([{"type": "text", "text": "Continue task."}]
                                       if case == 'text-block-prompt' else 'Continue task.'))
                    expected = ''
                if case in {'new-handback', 'delayed-stop'}:
                    rows.append(report(completed))
                    expected = completed if case == 'new-handback' else ''
                rows.append(final(latest))
                transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
                callback = 'First turn ended.' if case == 'delayed-stop' else latest
                event = event_from_payload('claude', {'hook_event_name': 'SubagentStop',
                    'agent_id': 'child', 'agent_transcript_path': str(transcript),
                    'last_assistant_message': callback})
                self.assertEqual(event.payload['last_assistant_message'],
                                 expected + '\n' + callback if expected else callback)

    def test_identical_claude_goodbyes_require_an_exact_native_turn(self):
        with TemporaryDirectory() as temporary:
            transcript = Path(temporary) / 'child.jsonl'
            reports = ['SYMPHONY_OUTCOME: {"status":"blocked"}',
                       'SYMPHONY_OUTCOME: {"status":"completed"}']
            rows = []
            for turn, report in zip(('first', 'latest'), reports):
                rows.extend([
                    {'type': 'user', 'uuid': turn, 'message': {'content': 'Task.'}},
                    {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                        'name': 'SubagentHandback', 'input': {'message': report}}]}},
                    {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Goodbye.'}]}}])
            transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            payload = {'hook_event_name': 'SubagentStop', 'agent_id': 'child',
                       'agent_transcript_path': str(transcript), 'last_assistant_message': 'Goodbye.'}
            self.assertEqual(event_from_payload('claude', payload).payload['last_assistant_message'], 'Goodbye.')
            self.assertEqual(event_from_payload('claude', {**payload, 'last_assistant_message': ''})
                             .payload['last_assistant_message'], '')
            for turn, report in zip(('first', 'latest'), reports):
                event = event_from_payload('claude', {**payload, 'turn_id': turn})
                self.assertEqual(event.payload['last_assistant_message'], report + '\nGoodbye.')
            for turn in ('foreign', '', None):
                self.assertEqual(event_from_payload('claude', {**payload, 'turn_id': turn})
                                 .payload['last_assistant_message'], 'Goodbye.')

    def test_legacy_codex_callback_never_combines_resumed_native_turns(self):
        with TemporaryDirectory() as temporary:
            transcript = Path(temporary) / 'child.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'child',
                'agent_path': '/root/symphony_lead_fast_first_medium',
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}
            first = {'type': 'turn_context', 'payload': {'turn_id': 'first-turn', 'model': 'first', 'effort': 'medium'}}
            task = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}
            later = {'type': 'turn_context', 'payload': {'turn_id': 'later-turn', 'model': 'later', 'effort': 'high'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                for padding in (0, 70):
                    with self.subTest(hook=hook, padding=padding):
                        rows = [header, first, task] + [{'type': 'metadata'}] * padding + [later]
                        transcript.write_text('\n'.join(json.dumps(row) for row in rows))
                        payload = {'hook_event_name': hook, 'agent_id': 'child', 'model': 'callback',
                                   'agent_transcript_path': str(transcript)}
                        event = event_from_payload('codex', payload)
                        self.assertEqual(event.payload['model'], 'callback')
                        self.assertNotIn('model_reasoning_effort', event.payload)
                        self.assertNotIn('task', event.payload)
                        self.assertEqual(event.payload['parent_thread_id'], 'root')
                        self.assertEqual(event.payload['task_name'], 'symphony_lead_fast_first_medium')
                        bound = event_from_payload('codex', {**payload, 'turn_id': 'later-turn'})
                        self.assertEqual(bound.payload['model'], 'later')
                        self.assertEqual(bound.payload['model_reasoning_effort'], 'high')
                        self.assertNotIn('task', bound.payload)

    def test_legacy_codex_callback_requires_a_complete_context_inventory(self):
        with TemporaryDirectory() as temporary:
            transcript = Path(temporary) / 'child.jsonl'
            rows = [{'type': 'session_meta', 'payload': {'id': 'child', 'agent_path': '/root/lead',
                     'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}},
                    {'type': 'turn_context', 'payload': {'model': 'first', 'effort': 'medium'}},
                    {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}]
            for hook in ('SubagentStart', 'SubagentStop'):
                for tail in ('{"type":"turn_context","payload":',
                             '{"type":"turn_context","payload":null}', '[]'):
                    with self.subTest(hook=hook, tail=tail):
                        transcript.write_text('\n'.join(json.dumps(row) for row in rows) + '\n' + tail)
                        event = event_from_payload('codex', {'hook_event_name': hook, 'agent_id': 'child',
                            'agent_transcript_path': str(transcript), 'model': 'callback'})
                        self.assertEqual(event.payload['model'], 'callback')
                        self.assertNotIn('model_reasoning_effort', event.payload)
                        self.assertNotIn('task', event.payload)
                        self.assertEqual(event.payload['task_name'], 'lead')
                        self.assertEqual(event.payload['parent_thread_id'], 'root')

    def test_codex_transcript_metadata_requires_exact_nonempty_header_and_callback_identity(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'child.jsonl'
            header = {'id': 'child', 'agent_path': '/root/symphony_lead_fast_model_medium',
                      'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}
            turn = {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'model': 'model', 'effort': 'medium'}}
            task = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                payload = {'hook_event_name': hook, 'agent_id': 'child', 'turn_id': 'turn',
                           'agent_transcript_path': str(transcript), 'model': 'callback-model'}
                for identity in (None, '', 0, True, [], {}, 'foreign', 'child'):
                    with self.subTest(hook=hook, header_identity=identity):
                        candidate = {**header, 'id': identity}
                        if identity is None:
                            candidate.pop('id')
                        transcript.write_text('\n'.join(json.dumps(row) for row in
                            ({'type': 'session_meta', 'payload': candidate}, turn, task)), encoding='utf-8')
                        event = event_from_payload('codex', payload)
                        if identity == 'child':
                            self.assertEqual(event.payload['parent_thread_id'], 'root')
                            self.assertEqual(event.payload['model'], 'model')
                            self.assertIn('task', event.payload)
                        else:
                            self.assertEqual(event.payload['_symphony_child_metadata'], ())
                            self.assertEqual(event.payload['model'], 'callback-model')
                            for field in ('parent_thread_id', 'task_name', 'task', 'model_reasoning_effort'):
                                self.assertNotIn(field, event.payload)
                transcript.write_text('\n'.join(json.dumps(row) for row in
                    ({'type': 'session_meta', 'payload': header}, turn, task)), encoding='utf-8')
                for identity in (None, '', 0, True, [], {}):
                    with self.subTest(hook=hook, callback_identity=identity):
                        invalid = {**payload, 'agent_id': identity}
                        if identity is None:
                            invalid.pop('agent_id')
                        self.assertEqual(event_from_payload('codex', invalid).payload['_symphony_child_metadata'], ())
                transcript.write_text('\n'.join(json.dumps(row) for row in (turn, task)), encoding='utf-8')
                self.assertEqual(event_from_payload('codex', payload).payload['_symphony_child_metadata'], ())
                for malformed in (None, [], 'invalid header', 0, True):
                    for preceding_header in (False, True):
                        with self.subTest(hook=hook, malformed_payload=malformed, preceding_header=preceding_header):
                            prefix = ({'type': 'session_meta', 'payload': header},) if preceding_header else ()
                            transcript.write_text('\n'.join(json.dumps(row) for row in
                                (*prefix, {'type': 'session_meta', 'payload': malformed},
                                 {'type': 'session_meta', 'payload': header}, turn, task)), encoding='utf-8')
                            rejected = event_from_payload('codex', payload)
                            self.assertEqual(rejected.payload['_symphony_child_metadata'], ())
                            self.assertEqual(rejected.payload['model'], 'callback-model')
                # Rows before the child's verified header do not supply metadata.
                transcript.write_text('\n'.join(json.dumps(row) for row in
                    (turn, task, {'type': 'session_meta', 'payload': header})), encoding='utf-8')
                event = event_from_payload('codex', payload)
                self.assertEqual(event.payload['model'], 'callback-model')
                self.assertNotIn('task', event.payload)

    def test_explicit_codex_turn_does_not_import_metadata_from_a_partial_stream(self):
        with TemporaryDirectory() as temporary:
            transcript = Path(temporary) / 'child.jsonl'
            prefix = [{'type': 'session_meta', 'payload': {'id': 'child', 'agent_path': '/root/worker',
                       'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'lead'}}}}},
                      {'type': 'turn_context', 'payload': {'turn_id': 'own', 'model': 'native', 'effort': 'high'}},
                      {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}]
            for hook in ('SubagentStart', 'SubagentStop'):
                for suffix in ('{"type":"turn_context","payload":', '[]', '{"type":"turn_context","payload":null}'):
                    with self.subTest(hook=hook, suffix=suffix):
                        transcript.write_text(''.join(json.dumps(row) + '\n' for row in prefix) + suffix)
                        event = event_from_payload('codex', {'hook_event_name': hook, 'agent_id': 'child',
                            'turn_id': 'own', 'model': 'callback', 'agent_transcript_path': str(transcript)})
                        self.assertEqual(event.payload['model'], 'callback')
                        self.assertNotIn('model_reasoning_effort', event.payload)
                        self.assertNotIn('task', event.payload)
                        self.assertEqual(event.payload['parent_thread_id'], 'lead')

    def test_explicit_codex_callback_turn_requires_exact_context_before_task_metadata(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'child.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'child', 'agent_path': '/root/worker',
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'lead'}}}}}
            task = {'type': 'event_msg', 'payload': {'type': 'user_message',
                    'message': 'SYMPHONY_FAST_ROUTE: lead\nStale context task'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                for token in (None, '', 0, True, [], {}, 'foreign', 'own-turn'):
                    with self.subTest(hook=hook, native_turn=token):
                        context = {'type': 'turn_context', 'payload': {
                            'model': 'native-model', 'effort': 'high', 'turn_id': token}}
                        if token is None: context['payload'].pop('turn_id')
                        transcript.write_text('\n'.join(json.dumps(row) for row in (header, context, task)))
                        payload = {'hook_event_name': hook, 'agent_id': 'child', 'turn_id': 'own-turn',
                                   'agent_transcript_path': str(transcript), 'model': 'callback-model'}
                        event = event_from_payload('codex', payload)
                        self.assertEqual(event.payload['task_name'], 'worker')
                        self.assertEqual(event.payload['parent_thread_id'], 'lead')
                        if token == 'own-turn':
                            self.assertEqual(event.payload['model'], 'native-model')
                            self.assertIn('task', event.payload)
                        else:
                            self.assertEqual(event.payload['model'], 'callback-model')
                            self.assertNotIn('model_reasoning_effort', event.payload)
                            self.assertNotIn('task', event.payload)
                context['payload']['turn_id'] = 'own-turn'
                transcript.write_text('\n'.join(json.dumps(row) for row in (header, task, context)))
                event = event_from_payload('codex', payload)
                self.assertEqual(event.payload['model'], 'native-model')
                self.assertNotIn('task', event.payload)
                # A tokenless legacy callback retains header-bound native metadata.
                context['payload'].pop('turn_id')
                transcript.write_text('\n'.join(json.dumps(row) for row in (header, context, task)))
                payload.pop('turn_id')
                event = event_from_payload('codex', payload)
                self.assertEqual(event.payload['model'], 'native-model')
                self.assertIn('task', event.payload)

    def test_explicit_codex_turn_requires_one_unique_native_context(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'child.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'child', 'agent_path': '/root/worker',
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'lead'}}}}}
            context = {'type': 'turn_context', 'payload': {'turn_id': 'own-turn', 'model': 'first-model', 'effort': 'high'}}
            task = {'type': 'event_msg', 'payload': {'type': 'user_message',
                'message': 'SYMPHONY_FAST_ROUTE: lead\nFirst turn task'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                for same_metadata in (False, True):
                    with self.subTest(hook=hook, same_metadata=same_metadata):
                        duplicate = context if same_metadata else {'type': 'turn_context', 'payload': {
                            'turn_id': 'own-turn', 'model': 'second-model', 'effort': 'low'}}
                        for second_turn in ('own-turn', 'foreign-turn'):
                            duplicate = {**duplicate, 'payload': {**duplicate['payload'], 'turn_id': second_turn}}
                            transcript.write_text('\n'.join(json.dumps(row) for row in (header, context, task, duplicate)))
                            event = event_from_payload('codex', {'hook_event_name': hook, 'agent_id': 'child',
                                'turn_id': 'own-turn', 'agent_transcript_path': str(transcript), 'model': 'callback-model'})
                            self.assertEqual(event.payload['task_name'], 'worker')
                            self.assertEqual(event.payload['parent_thread_id'], 'lead')
                            if second_turn == 'own-turn':
                                self.assertEqual(event.payload['model'], 'callback-model')
                                self.assertNotIn('model_reasoning_effort', event.payload)
                                self.assertNotIn('task', event.payload)
                            else:
                                self.assertEqual(event.payload['model'], 'first-model')
                                self.assertEqual(event.payload['model_reasoning_effort'], 'high')
                                self.assertIn('task', event.payload)

    def test_malformed_copied_codex_header_rejects_all_native_metadata(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'child.jsonl'
            child = {'type': 'session_meta', 'payload': {'id': 'child',
                'agent_path': '/root/symphony_lead_fast_model_medium', 'source': {
                    'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}
            context = {'type': 'turn_context', 'payload': {
                'turn_id': 'own-turn', 'model': 'native-model', 'effort': 'medium'}}
            task = {'type': 'event_msg', 'payload': {
                'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                for identity, late in ((value, late) for value in (None, '', 0, True, [], {}, 'ancestor')
                                       for late in (False, True)):
                    with self.subTest(hook=hook, copied_header_identity=identity, late=late):
                        copied = {'type': 'session_meta', 'payload': {'id': identity}}
                        if identity is None:
                            copied['payload'].pop('id')
                        rows = (child, context, task, copied) if late else (child, copied, context, task)
                        transcript.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
                        event = event_from_payload('codex', {
                            'hook_event_name': hook, 'agent_id': 'child', 'turn_id': 'own-turn',
                            'agent_transcript_path': str(transcript), 'model': 'callback-model'})
                        if identity == 'ancestor':
                            self.assertEqual(event.payload['parent_thread_id'], 'root')
                            self.assertEqual(event.payload['model'], 'native-model')
                            self.assertIn('task', event.payload)
                        else:
                            self.assertEqual(event.payload['_symphony_child_metadata'], ())
                            self.assertEqual(event.payload['model'], 'callback-model')
                            for field in ('parent_thread_id', 'task_name', 'task', 'model_reasoning_effort'):
                                self.assertNotIn(field, event.payload)

    def test_malformed_explicit_codex_callback_turn_imports_no_native_metadata(self):
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'child.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'child',
                'agent_path': '/root/symphony_lead_fast_model_medium', 'source': {
                    'subagent': {'thread_spawn': {'parent_thread_id': 'root'}}}}}
            task = {'type': 'event_msg', 'payload': {
                'type': 'user_message', 'message': 'SYMPHONY_FAST_ROUTE: lead'}}
            for hook in ('SubagentStart', 'SubagentStop'):
                for token in (None, '', 0, 1, False, True, [], {}, '1'):
                    with self.subTest(hook=hook, callback_turn=token):
                        context = {'type': 'turn_context', 'payload': {
                            'turn_id': str(token), 'model': 'native-model', 'effort': 'medium'}}
                        transcript.write_text('\n'.join(json.dumps(row) for row in (header, context, task)))
                        event = event_from_payload('codex', {
                            'hook_event_name': hook, 'agent_id': 'child', 'turn_id': token,
                            'agent_transcript_path': str(transcript), 'model': 'callback-model'})
                        if token == '1':
                            self.assertEqual(event.payload['model'], 'native-model')
                            self.assertIn('task', event.payload)
                        else:
                            self.assertEqual(event.payload['_symphony_child_metadata'], ())
                            self.assertEqual(event.payload['model'], 'callback-model')
                            for field in ('parent_thread_id', 'task_name', 'task', 'model_reasoning_effort'):
                                self.assertNotIn(field, event.payload)

    def test_codex_fork_keeps_child_header_and_binds_only_its_callback_turn(self):
        # Sanitized native fork: child header, copied parent header/turn, own turn.
        rows = [
            {"type": "session_meta", "payload": {"id": "child", "forked_from_id": "lead",
                "agent_path": "/root/symphony_lead_gpt_6_luna_low/run_and_fix", "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "lead"}}}}},
            {"type": "session_meta", "payload": {"id": "lead",
                "agent_path": "/root/symphony_lead_gpt_6_luna_low", "source": {
                    "subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}},
            {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": "parent-model", "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "SYMPHONY_FAST_ROUTE: lead\nAncestor task"}},
            {"type": "turn_context", "payload": {"turn_id": "child-turn", "model": "child-model", "effort": "low"}},
        ]
        with TemporaryDirectory() as temp:
            transcript = Path(temp) / 'fork.jsonl'
            transcript.write_text('\n'.join(json.dumps(row) for row in rows))
            payload = {"hook_event_name": "SubagentStart", "agent_id": "child", "turn_id": "child-turn",
                       "agent_transcript_path": str(transcript)}
            event = event_from_payload("codex", payload)
            self.assertEqual(event.payload['task_name'], 'run_and_fix')
            self.assertEqual(event.payload['parent_thread_id'], 'lead')
            self.assertEqual(event.payload['model'], 'child-model')
            self.assertEqual(event.payload['model_reasoning_effort'], 'low')
            self.assertNotIn('task', event.payload)
            payload.pop('turn_id')
            ambiguous = event_from_payload('codex', payload)
            self.assertNotIn('model', ambiguous.payload)
            self.assertNotIn('model_reasoning_effort', ambiguous.payload)
            payload['agent_id'] = 'foreign'
            foreign = event_from_payload('codex', payload)
            self.assertEqual(foreign.payload['_symphony_child_metadata'], ())

    def test_event_id_is_stable_for_replayed_payload(self):
        payload = fixture("codex", "user_prompt")
        self.assertEqual(
            event_from_payload("codex", payload).event_id,
            event_from_payload("codex", payload).event_id,
        )

    def test_codex_context_uses_hook_specific_output(self):
        result = render("codex", (Action("inject_context", {"text": "Assess this task."}),))
        body = json.loads(result.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(body["hookSpecificOutput"]["additionalContext"], "Assess this task.")

    def test_codex_stop_block_uses_continuation_decision(self):
        result = render("codex", (Action("block_stop", {"reason": "Worker remains active."}),))
        self.assertEqual(json.loads(result.stdout), {"decision": "block", "reason": "Worker remains active."})

    def test_claude_pre_tool_block_uses_permission_decision(self):
        result = render(
            "claude",
            (Action("block_tool", {"reason": "Declare a Symphony role."}),),
            "PreToolUse",
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Declare a Symphony role.",
                }
            },
        )

    def test_codex_pre_tool_block_uses_block_decision(self):
        result = render(
            "codex",
            (Action("block_tool", {"reason": "Declare a Symphony role."}),),
            "PreToolUse",
        )

        self.assertEqual(
            json.loads(result.stdout),
            {"decision": "block", "reason": "Declare a Symphony role."},
        )

    def test_claude_context_uses_additional_context(self):
        result = render(
            "claude",
            (Action("inject_context", {"text": "Recover this task."}),),
            "SessionStart",
        )
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertEqual(output["additionalContext"], "Recover this task.")

    def test_registered_lifecycle_events_normalize(self):
        expected = {
            "SessionStart": "session_heartbeat",
            "UserPromptSubmit": "user_prompt",
            "PreToolUse": "pre_tool_use",
            "SubagentStart": "subagent_started",
            "SubagentStop": "subagent_stopped",
            "PostToolUse": "post_tool_use",
            "PostToolUseFailure": "post_tool_failed",
            "Stop": "stop_requested",
            "Interrupt": "interrupt",
        }
        for hook_name, kind in expected.items():
            with self.subTest(hook_name=hook_name):
                event = event_from_payload("codex", {"hook_event_name": hook_name})
                self.assertEqual(event.kind, kind)

    def test_malformed_payload_is_a_nonblocking_fault(self):
        event = event_from_payload("codex", {"hook_event_name": "Unknown"})
        self.assertEqual(event.kind, "unknown")


if __name__ == "__main__":
    unittest.main()
