"""Consent, privacy, accumulation and background publication; GitHub is mocked."""
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from plugins.symphony.symphony import diagnostics as d
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


class DiagnosticReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name) / 'state')
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.report = dict(schema=1, plugin_version='1.7.5', provider='codex', platform='linux',
                           category='bookkeeping', outcome='deferred')
        self.store.record_recovery_diagnostic(self.report)
        self.env = {'SYMPHONY_STATE_DIR': str(self.store.root), 'SYMPHONY_REPORT_WORKER': '0',
                    'CODEX_HOME': str(Path(self.temp.name) / 'codex'),
                    'CLAUDE_CONFIG_DIR': str(Path(self.temp.name) / 'claude')}
        self.launch_patch = patch.object(d, 'launch_worker')
        self.launch = self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)

    def offered(self, provider='codex', session='root'):
        self.assertEqual('', d.notice(self.store, provider, session, self.env))
        self.launch.assert_called_once()
        with patch.object(d, '_gh', side_effect=['User', 'true']):
            d.worker(str(self.store.root), 'probe')
        text = d.notice(self.store, provider, session, self.env)
        self.assertIn('nonblocking', text)
        self.assertIn('public', text)
        self.assertIn('@User', text)
        return d._read(self.store)

    def approved(self):
        record = self.offered()
        result = d.control(self.store, 'submit ' + record['id'], 'codex', 'root', self.env)
        self.assertIn('background', result)
        return d._read(self.store)

    def native_reply(self, provider, prompt):
        now = datetime.now(timezone.utc).isoformat()
        if provider == 'codex':
            path = Path(self.env['CODEX_HOME']) / 'sessions/2026/10/03/rollout-root.jsonl'
            rows = [{'timestamp': now, 'type': 'session_meta', 'payload': {'id': 'root', 'source': 'cli'}},
                    {'timestamp': now, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': prompt}]}}]
        else:
            path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/project/root.jsonl'
            rows = [{'timestamp': now, 'type': 'user', 'sessionId': 'root', 'isSidechain': False,
                     'message': {'role': 'user', 'content': [{'type': 'text', 'text': prompt}]}}]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path, rows

    def test_probe_reads_access_without_creating_issue_and_offer_is_once_across_roots(self):
        self.offered()
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.assertEqual('', d.notice(self.store, 'claude', 'other-root', self.env))
        self.assertEqual('offered', d._read(self.store)['phase'])

    def test_successful_recovery_and_expected_background_waits_do_not_offer_sharing(self):
        for path in (self.store.root / 'diagnostics').glob('*.json'):
            path.unlink()
        self.store.record_recovery_diagnostic({**self.report, 'category': 'native_recovery', 'outcome': 'recovered'})
        self.store.record_recovery_diagnostic({**self.report, 'category': 'incomplete_work'})
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.launch.assert_not_called()
        self.assertFalse(d._path(self.store).is_file())

    def test_missing_local_access_is_quiet_and_does_not_touch_managed_state(self):
        d.notice(self.store, 'codex', 'root', self.env)
        with patch.object(d, '_gh', side_effect=OSError('private token')) as gh:
            d.worker(str(self.store.root), 'probe')
        self.assertEqual(1, gh.call_count)
        self.assertEqual('unavailable', d._read(self.store)['phase'])
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.assertNotIn('private token', d._path(self.store).read_text())
        self.assertFalse(list(self.store.root.glob('*.v2.json')))

    def test_unexpected_optional_reporting_fault_does_not_change_root_routing_output(self):
        from plugins.symphony.symphony.adapters import HookResult
        original = HookResult(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                                                'additionalContext': 'original routing'}}))
        with patch('plugins.symphony.symphony.runtime._handle_core', return_value=original), \
             patch.object(d, 'notice', side_effect=RuntimeError('private reporting fault')):
            result = handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'root',
                             'cwd': str(self.project), 'prompt': 'continue the task'},
                            {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
        self.assertEqual(original, result)

    def test_blocked_root_entry_does_not_claim_an_unseen_reporting_offer(self):
        from plugins.symphony.symphony.adapters import HookResult
        original = HookResult(json.dumps({'decision': 'block', 'reason': 'real ownership guard'}))
        with patch('plugins.symphony.symphony.runtime._handle_core', return_value=original), \
             patch.object(d, 'notice') as notice:
            result = handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'root',
                             'cwd': str(self.project), 'prompt': 'continue the task'},
                            {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
        self.assertEqual(original, result)
        notice.assert_not_called()
        self.assertFalse(d._path(self.store).is_file())

    def test_reports_accumulate_while_waiting_and_approval_freezes_all_counts(self):
        record = self.offered()
        self.store.record_recovery_diagnostic(self.report)
        self.store.record_recovery_diagnostic({**self.report, 'provider': 'claude', 'platform': 'windows'})
        with patch.object(d, '_gh') as gh:
            d.worker(str(self.store.root), 'publish')
        gh.assert_not_called()
        d.control(self.store, 'submit ' + record['id'], 'codex', 'root', self.env)
        approved = d._read(self.store)
        self.assertEqual(3, sum(item['occurrences'] for item in approved['reports']))
        self.store.record_recovery_diagnostic(self.report)
        self.assertEqual(3, sum(item['occurrences'] for item in d._read(self.store)['reports']))

    def test_decline_or_other_session_cannot_publish(self):
        record = self.offered()
        self.assertIn('does not belong', d.control(self.store, 'submit ' + record['id'], 'codex', 'other', self.env))
        d.control(self.store, 'decline ' + record['id'], 'codex', 'root', self.env)
        with patch.object(d, '_gh') as gh:
            d.worker(str(self.store.root), 'publish')
        gh.assert_not_called()
        self.assertEqual('declined', d._read(self.store)['phase'])
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.assertEqual(1, len(d.snapshot(self.store)))

    def test_public_issue_uses_approved_snapshot_fixed_repo_and_no_private_evidence(self):
        record = self.approved()
        with patch.object(d, '_gh', side_effect=['User', '[]', 'https://github.com/opennoor/symphony/issues/123']) as gh:
            d.worker(str(self.store.root), 'publish')
        self.assertEqual(3, gh.call_count)
        args = gh.call_args_list[-1].args[0]
        self.assertEqual(['issue', 'create', '--repo', 'opennoor/symphony'], args[:4])
        body = (self.store.root / 'diagnostic-issue.md').read_text()
        for private in ('User', 'root', str(self.project), 'last_assistant_message'):
            self.assertNotIn(private, body)
        self.assertIn('"occurrences": 1', body)
        self.assertIn(record['id'], body)
        if os.name != 'nt':
            self.assertEqual(0o600, (self.store.root / 'diagnostic-issue.md').stat().st_mode & 0o777)
        self.assertEqual('published', d._read(self.store)['phase'])
        self.assertIn('/issues/123', d.notice(self.store, 'codex', 'root', self.env))
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))

    def test_lost_create_response_searches_exact_marker_and_never_duplicates(self):
        record = self.approved()
        with patch.object(d, '_gh', side_effect=['User', '[]', subprocess.TimeoutExpired('gh', 8)]):
            d.worker(str(self.store.root), 'publish')
        self.assertEqual('approved', d._read(self.store)['phase'])
        with patch.object(d, '_gh', side_effect=['User', '[]']) as gh:
            d.worker(str(self.store.root), 'publish')
        self.assertEqual(2, gh.call_count)
        matches = [{'url': 'https://github.com/opennoor/symphony/issues/123',
                    'body': '<!-- symphony-diagnostic:' + record['id'] + ' -->'}]
        with patch.object(d, '_gh', side_effect=['User', json.dumps(matches)]) as gh:
            d.worker(str(self.store.root), 'publish')
        self.assertEqual(2, gh.call_count)
        self.assertEqual('published', d._read(self.store)['phase'])

    def test_failed_search_never_attempts_issue_creation(self):
        self.approved()
        with patch.object(d, '_gh', side_effect=['User', OSError('offline')]) as gh:
            d.worker(str(self.store.root), 'publish')
        self.assertEqual(2, gh.call_count)
        self.assertNotIn('create_attempted', d._read(self.store))

    def test_approved_snapshot_recovers_lost_worker_launch_without_reasking(self):
        record = self.approved()
        record['publish_scheduled'] = 0
        self.store._write_json(d._path(self.store), record)
        self.launch.reset_mock()
        self.assertEqual('', d.notice(self.store, 'claude', 'another-root', self.env))
        self.launch.assert_not_called()
        self.assertEqual(record, d._read(self.store))
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.launch.assert_called_once_with(self.store, 'publish', self.env)
        self.assertEqual(record['reports'], d._read(self.store)['reports'])
        self.assertEqual('', d.notice(self.store, 'codex', 'root', self.env))
        self.assertEqual(1, self.launch.call_count)

    def test_account_change_requires_new_consent(self):
        self.approved()
        with patch.object(d, '_gh', return_value='DifferentUser') as gh:
            d.worker(str(self.store.root), 'publish')
        self.assertEqual(1, gh.call_count)
        self.assertEqual('checking', d._read(self.store)['phase'])
        self.assertNotIn('reports', d._read(self.store))

    def test_tampered_approved_report_cannot_publish_raw_text_or_destination(self):
        record = self.approved()
        for field, value in [('category', 'private task text'), ('provider', 'private provider'),
                             ('occurrences', True), ('plugin_version', 'private-token')]:
            with self.subTest(field=field):
                damaged = {**record, 'reports': [{**record['reports'][0], field: value}]}
                with self.assertRaises(ValueError):
                    d.issue_body(damaged)
        d._path(self.store).write_text(json.dumps({**record, 'repository': 'attacker/repo'}))
        with patch.object(d, '_gh') as gh:
            d.worker(str(self.store.root), 'publish')
        gh.assert_not_called()

    def test_local_unknown_and_symlinked_records_are_not_reported(self):
        path = self.store.root / 'diagnostics' / 'raw.json'
        path.write_text(json.dumps({'raw_log': 'private-secret'}))
        self.assertEqual(1, len(d.snapshot(self.store)))
        if os.name != 'nt':
            link = path.with_name('link.json')
            link.symlink_to(next(entry for entry in path.parent.glob('*.json') if entry != path))
            self.assertEqual(1, len(d.snapshot(self.store)))

    def test_async_native_reply_must_match_question_answer_and_root_for_both_providers(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                # One separate local queue per host.
                if provider == 'claude':
                    d._path(self.store).unlink()
                    self.launch.reset_mock()
                record = self.offered(provider)
                reply = {'question': d.question(record), 'answer': d.SHARE}
                prompt = '<send_user_message_question_reply>\n' + json.dumps([reply]) + '\n</send_user_message_question_reply>'
                self.assertEqual('submit ' + record['id'], d.reply_control(self.store, prompt, provider, 'root'))
                self.assertIsNone(d.reply_control(self.store, prompt, provider, 'foreign'))
                self.assertIsNone(d.reply_control(self.store, 'quoted ' + prompt, provider, 'root'))
                bad = prompt.replace('Your work continues', 'Stop working')
                self.assertIsNone(d.reply_control(self.store, bad, provider, 'root'))
                result = handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'root', 'cwd': str(self.project),
                                 'prompt': prompt}, {**self.env, 'SYMPHONY_PROVIDER': provider})
                self.assertIn('Sharing approved', result.stdout)
                self.assertEqual('approved', d._read(self.store)['phase'])

    def test_report_control_is_root_only_and_does_not_need_routing_or_spawn(self):
        record = self.offered()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'root', 'cwd': str(self.project),
                   'prompt': '$symphony:symphony report submit ' + record['id']}
        with patch('plugins.symphony.symphony.runtime._handle_core', side_effect=AssertionError('must not run')):
            result = handle(payload, {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
        self.assertIn('Sharing approved', result.stdout)
        self.assertFalse(list(self.store.root.glob('*.v2.json')))

    def test_child_or_quoted_command_cannot_approve(self):
        record = self.offered()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'root', 'cwd': str(self.project),
                   'prompt': '$symphony:symphony report submit ' + record['id'], 'agent_id': 'child'}
        with patch('plugins.symphony.symphony.runtime._handle_core', return_value=dummy_result()):
            handle(payload, {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
            handle({**payload, 'agent_id': '', 'prompt': 'The log said:\n' + payload['prompt']},
                   {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
        self.assertEqual('offered', d._read(self.store)['phase'])

    def test_background_launch_uses_absolute_python_and_no_shell_or_wait(self):
        self.launch_patch.stop()
        with patch.object(d.subprocess, 'Popen') as launch:
            d.launch_worker(self.store, 'probe', {})
        self.assertEqual(d.sys.executable, launch.call_args.args[0][0])
        self.assertIn('-B', launch.call_args.args[0])
        self.assertEqual(self.store.root.resolve(), launch.call_args.kwargs['cwd'])
        self.assertNotIn('shell', launch.call_args.kwargs)
        self.assertEqual(subprocess.DEVNULL, launch.call_args.kwargs['stdout'])
        self.assertEqual(subprocess.DEVNULL, launch.call_args.kwargs['stderr'])

    def test_gh_has_bounded_timeout_noninteractive_fixed_host_and_no_raw_error(self):
        result = subprocess.CompletedProcess(['gh'], 1, 'private-auth-output', 'private-auth-error')
        with patch.object(d.subprocess, 'run', return_value=result) as run:
            with self.assertRaisesRegex(OSError, 'GitHub operation unavailable'):
                d._gh(['api', 'user'], {'GH_HOST': 'attacker', 'GH_DEBUG': 'api'})
        self.assertEqual(8, run.call_args.kwargs['timeout'])
        self.assertEqual('github.com', run.call_args.kwargs['env']['GH_HOST'])
        self.assertEqual('', run.call_args.kwargs['env']['GH_DEBUG'])

    def test_real_isolated_reply_relay_requires_native_user_consent_without_bytecode(self):
        before = set(Path(d.__file__).parent.rglob('*'))
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                if provider == 'claude':
                    d._path(self.store).unlink()
                    self.launch.reset_mock()
                record = self.offered(provider)
                reply = '<send_user_message_question_reply>' + json.dumps([
                    {'question': d.question(record), 'answer': d.SHARE}]) + '</send_user_message_question_reply>'
                command = [d.sys.executable, '-I', '-B', str(Path(d.__file__)), '--relay-reply',
                           '--provider', provider, '--session', 'root', '--state-dir', str(self.store.root)]
                env = {**os.environ, 'SYMPHONY_REPORT_WORKER': '0',
                       'CODEX_HOME': str(Path(self.temp.name) / 'untrusted-tool-home'),
                       'CLAUDE_CONFIG_DIR': str(Path(self.temp.name) / 'untrusted-tool-home')}
                for prompt in ('not a user approval', reply):
                    result = subprocess.run(command, input=prompt, text=True, capture_output=True, env=env, timeout=5)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('No matching', result.stdout)
                self.assertEqual('offered', d._read(self.store)['phase'])
                self.native_reply(provider, reply)
                for _ in range(2):
                    result = subprocess.run(command, input=reply, text=True, capture_output=True, env=env, timeout=5)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('Sharing approved', result.stdout)
                self.assertEqual('approved', d._read(self.store)['phase'])
        self.assertEqual(before, set(Path(d.__file__).parent.rglob('*')))

    def test_native_reply_proof_rejects_documents_tools_assistants_children_and_old_messages(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                if provider == 'claude':
                    d._path(self.store).unlink()
                    self.launch.reset_mock()
                record = self.offered(provider)
                reply = '<send_user_message_question_reply>' + json.dumps([
                    {'question': d.question(record), 'answer': d.SHARE}]) + '</send_user_message_question_reply>'
                path, rows = self.native_reply(provider, reply)
                self.assertTrue(d.native_reply_verified(self.store, reply, provider, 'root'))
                variants = []
                if provider == 'codex':
                    variants.extend([
                        [{**rows[0], 'payload': {**rows[0]['payload'], 'id': 'foreign'}}, rows[1]],
                        [{**rows[0], 'payload': {**rows[0]['payload'], 'source': {'subagent': {'thread_spawn': {}}}}}, rows[1]],
                        [rows[0], {**rows[1], 'payload': {**rows[1]['payload'], 'role': 'assistant'}}],
                        [rows[0], {**rows[1], 'payload': {'type': 'function_call_output', 'output': reply}}],
                        [rows[0], {**rows[1], 'payload': {**rows[1]['payload'],
                         'content': [{'type': 'input_text', 'text': 'Attached document:\n' + reply}]}}],
                    ])
                else:
                    variants.extend([[{**rows[0], field: value}] for field, value in (
                        ('sessionId', 'foreign'), ('isSidechain', True), ('agentId', 'child'),
                        ('isMeta', True), ('parentToolUseID', 'child-launch'))])
                    variants.extend([
                        [{**rows[0], 'type': 'assistant', 'message': {**rows[0]['message'], 'role': 'assistant'}}],
                        [{**rows[0], 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': reply}]}}],
                        [{**rows[0], 'message': {'role': 'user', 'content': 'Attached document:\n' + reply}}],
                    ])
                variants.append([*rows[:-1], {**rows[-1], 'timestamp': '2000-01-01T00:00:00+00:00'}])
                for damaged in variants:
                    path.write_text(''.join(json.dumps(row) + '\n' for row in damaged))
                    self.assertFalse(d.native_reply_verified(self.store, reply, provider, 'root'))
                self.assertEqual('offered', d._read(self.store)['phase'])

    def test_bound_child_alias_cannot_approve_root_report_control(self):
        record = self.offered(session='child')
        self.store.bind_session('codex', 'child', self.store._path(self.project), False, self.project, 'root')
        with patch('plugins.symphony.symphony.runtime._handle_core', return_value=dummy_result()):
            handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'child', 'cwd': str(self.project),
                    'prompt': '$symphony:symphony report submit ' + record['id']},
                   {**self.env, 'SYMPHONY_PROVIDER': 'codex'})
        self.assertEqual('offered', d._read(self.store)['phase'])

    def test_actual_installed_hooks_accept_root_report_control_on_both_providers_without_a_run(self):
        from plugins.symphony.scripts.package_smoke import _materialize, _send_raw
        plugin = Path(__file__).resolve().parents[1]
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                home = Path(self.temp.name) / provider
                installed = _materialize(plugin, home, self.report['plugin_version'])
                store = StateStore(home / 'state')
                store.record_recovery_diagnostic(self.report)
                proposal = {'id': 'a' * 32, 'phase': 'offered', 'account': 'User',
                            'scope': d._scope(provider, 'root')}
                store._write_json(d._path(store), proposal)
                prompt = ('$symphony:symphony report submit ' if provider == 'codex' else
                          '/symphony:report submit ') + proposal['id']
                if provider == 'claude':
                    # Exercise the real slash-command expansion, not just a
                    # literal native prompt that bypasses the wrapper template.
                    prompt = (installed / 'commands/report.md').read_text().split('---', 2)[-1].strip()
                    prompt = prompt.replace('$ARGUMENTS', 'submit ' + proposal['id'])
                payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'root',
                           'cwd': str(self.project), 'prompt': prompt}
                result = _send_raw(installed, provider, 'UserPromptSubmit', store.root, payload)
                self.assertIn('Sharing approved', json.dumps(result))
                self.assertEqual('approved', d._read(store)['phase'])
                self.assertFalse(list(store.root.glob('*.v2.json')))
                retained = next((home / 'runtimes').iterdir())
                self.assertFalse(list(retained.rglob('__pycache__')))


def dummy_result():
    from plugins.symphony.symphony.adapters import HookResult
    return HookResult()
