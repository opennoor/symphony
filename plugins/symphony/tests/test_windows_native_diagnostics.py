"""Private native launcher diagnostics preserve the command and export fixed facts."""
from contextlib import nullcontext, redirect_stdout
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[3] / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('windows_native_activation', SCRIPTS / 'windows_native_activation.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class WindowsNativeDiagnosticsTests(unittest.TestCase):
    def test_checker_receipt_exports_categories_without_private_stderr(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            failed = subprocess.CompletedProcess([], 1, '',
                'python.exe not recognized C:\\PRIVATE_PATH\\config token=PRIVATE_SECRET')
            checked = subprocess.CompletedProcess([], 0, 'guarded: matching current-session heartbeat', '')
            rejected = subprocess.CompletedProcess([], 1, '', 'PRIVATE_FOREIGN_SESSION')
            output = io.StringIO()
            environment = SimpleNamespace(name='nt', environ={'SystemRoot': 'C:\\Windows'})
            windows = SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False))
            with patch.object(native, 'os', environment), \
                    patch.object(native.ctypes, 'windll', windows, create=True), \
                    patch.object(native.tempfile, 'TemporaryDirectory', return_value=nullcontext(str(root))), \
                    patch.object(native, 'prepare_baseline_capture', return_value={'CODEX_HOME': str(root / 'home')}), \
                    patch.object(native, 'prepare_private_native_clock'), \
                    patch.object(native, 'projects', return_value=(project, None)), \
                    patch.object(native.shutil, 'which', return_value='codex.exe'), \
                    patch.object(native, 'original_native_activation', return_value=(checked, 'session', {'plugin_root': str(root)})), \
                    patch.object(native, 'native_session_rows', return_value=[{'payload': {'content': [{'text':
                        'Activation diagnostic for status only: PRIVATE_CHECKER_COMMAND'}]}}]), \
                    patch.object(native.subprocess, 'run', side_effect=[failed, checked, failed, checked, rejected]), \
                    redirect_stdout(output):
                self.assertEqual(native.main(), 0)
            receipt = json.loads(output.getvalue())
            self.assertTrue(receipt['foreign_session_rejected'])
            self.assertEqual(len(receipt['windows_native_activation']), 2)
            for evidence in receipt['windows_native_activation']:
                self.assertEqual(evidence['old_checker_exit'], 1)
                self.assertEqual(evidence['new_checker_exit'], 0)
                self.assertNotIn('old_launch_error', evidence)
                self.assertEqual(evidence['old_launch_error_categories'], native.error_categories(failed.stderr))
                self.assertTrue(evidence['old_launch_error_categories']['python_unavailable'])
            self.assertNotIn('PRIVATE_', output.getvalue())
            self.assertIn('PRIVATE_SECRET', failed.stderr, 'raw stderr remains private and unchanged')

    def test_native_jsonl_is_utf8_and_malformed_bytes_are_not_accepted(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'sessions').mkdir()
            path = home / 'sessions/native-fixture.jsonl'
            path.write_text(json.dumps({'note': '✓'}, ensure_ascii=False) + '\n', encoding='utf-8')
            self.assertEqual(native.native_session_rows(home, 'fixture'), [{'note': '✓'}])
            path.write_bytes(b'{"note":"\x9d"}\n')
            with self.assertRaises(UnicodeDecodeError):
                native.native_session_rows(home, 'fixture')

    @unittest.skipUnless(shutil.which('pwsh') or sys.platform == 'win32', 'needs PowerShell')
    def test_encoded_private_clock_preserves_input_with_literal_paths_and_utf8(self):
        # Snap pwsh cannot see the host /tmp, including checkouts placed there.
        visible_root = Path.home() if sys.platform == 'linux' else Path(__file__).parent
        with TemporaryDirectory(prefix="clock path ' ", dir=visible_root) as directory:
            root = Path(directory).resolve()
            home = root / 'home'
            home.mkdir()
            destination = root / 'capture path'
            destination.mkdir()
            capture = root / 'capture_codex_hook.py'
            capture.write_text("import json,pathlib,sys\npayload=json.load(sys.stdin)\n"
                "destination=pathlib.Path(sys.argv[2]);invocation='fixture'\nevent = sys.argv[1]\n"
                "print(json.dumps({'input_preserved':payload['note']=='✓'}))\n", encoding='utf-8')
            (home / 'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
                {'command_windows': f'python.exe -I "{capture}" SessionStart "{destination}" codex'}]}]}}), encoding='utf-8')
            native.prepare_private_native_clock(root, home)
            clock = json.loads((home / 'hooks.json').read_text(encoding='utf-8'))['hooks']['SessionStart'][0]['hooks'][0]['command_windows']
            ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-EncodedCommand', clock.split()[-1]],
                input=json.dumps({'note': '✓'}, ensure_ascii=False), capture_output=True, text=True,
                encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['input_preserved'])
            self.assertEqual(json.loads((destination / 'private-payload-SessionStart-fixture.json').read_text(encoding='utf-8')), {'note': '✓'})

    def test_native_capture_decodes_utf8_and_retains_malformed_bytes_safely(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text(json.dumps({'activation': {'codex': {'state': 'guarded', 'session_id': 'abcd'}}}))
            code = 'import sys;sys.stderr.buffer.write(b"session id: abcd\\n"+"✓".encode("utf-8")+bytes([0x9d]))'
            with patch.object(native, 'state_file', return_value=state):
                result, session, _ = native.original_native_activation(root, root, {}, '1.7.0',
                    [sys.executable, '-c', code])
            self.assertEqual(session, 'abcd')
            self.assertIn('✓', result.stderr)
            self.assertIn('\ufffd', result.stderr)

    def test_private_instrumentation_preserves_original_and_payload_stdin(self):
        with TemporaryDirectory(prefix='diagnostic path ') as directory:
            root = Path(directory)
            home = root / 'home'
            plugin = home / 'plugins/cache/symphony-baseline/symphony/1.7.0'
            (plugin / 'hooks').mkdir(parents=True)
            command = 'powershell.exe -NoProfile -Command "exit 3"'
            manifest = plugin / 'hooks/codex.json'
            manifest.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
                {'commandWindows': command, 'command': 'unchanged', 'timeout': 10}]}]}}))
            capture = root / 'capture_codex_hook.py'
            capture.write_text('event = sys.argv[1]\n')
            (home / 'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [
                {'command_windows': 'python.exe -I capture.py SessionStart private codex'}]}]}}))
            selected, originals, diagnostics = native.instrument_installed_hooks(root, home, '1.7.0')
            native.prepare_private_native_clock(root, home)
            self.assertEqual(selected, plugin)
            self.assertEqual(originals[('SessionStart', 0)], command)
            handler = json.loads(manifest.read_text())['hooks']['SessionStart'][0]['hooks'][0]
            self.assertEqual(handler['commandWindows'], '(' + command + ') 2>>"' + str(diagnostics / 'SessionStart-0.stderr') + '"')
            self.assertEqual(handler['command'], 'unchanged')
            self.assertEqual(handler['timeout'], 10)
            self.assertNotIn('json.load', capture.read_text(), 'capture reuses already read payload without consuming stdin twice')
            self.assertIn('json.dumps(payload)', capture.read_text())
            clock = json.loads((home / 'hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]
            self.assertTrue(clock['command_windows'].startswith('powershell.exe -NoProfile -NonInteractive -EncodedCommand '))
            code = base64.b64decode(clock['command_windows'].split()[-1]).decode('utf-16-le')
            self.assertEqual(code, "& '" + sys.executable.replace("'", "''") + "' -I 'capture.py' 'SessionStart' 'private' 'codex';exit $LASTEXITCODE")
            self.assertNotIn('$', clock['command_windows'])

    def test_failure_replay_uses_exact_original_payload_and_only_creation_flag_differs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'codex-hook-capture'
            capture.mkdir()
            payload = '{"session_id":"aaaa","private":"PRIVATE_SENTINEL"}'
            (capture / 'private-payload-SessionStart-one.json').write_text(payload)
            diagnostics = root / 'private'
            diagnostics.mkdir()
            (diagnostics / 'SessionStart-0.stderr').write_text('The handle is invalid PRIVATE_SECRET')
            completed = subprocess.CompletedProcess([], 1, 'private output', 'session id: aaaa\nThe handle is invalid PRIVATE_SECRET')
            output = io.StringIO()
            with patch.object(native.subprocess, 'run', return_value=completed) as run, redirect_stdout(output):
                native.activation_failure_diagnostics(root, root, {'COMSPEC': 'C:\\Windows\\cmd.exe'}, root / 'plugin',
                    {('SessionStart', 0): 'powershell.exe -Command "original"'}, diagnostics, completed)
            self.assertEqual(run.call_count, 4)
            first, second, cmd, cmd_no_window = run.call_args_list
            self.assertEqual(first.args, second.args)
            self.assertEqual(first.kwargs['input'], payload)
            self.assertEqual(first.kwargs['env']['PLUGIN_ROOT'], str(root / 'plugin'))
            self.assertEqual(first.kwargs['creationflags'], 0)
            self.assertEqual(second.kwargs['creationflags'], 0x08000000)
            self.assertEqual(first.kwargs['encoding'], 'utf-8')
            self.assertEqual(first.kwargs['errors'], 'replace')
            self.assertEqual(first.args[0][-3:], ['-NoProfile', '-Command', 'powershell.exe -Command "original"'])
            self.assertEqual(cmd.args[0], 'C:\\Windows\\cmd.exe /C "powershell.exe -Command "original""')
            self.assertEqual(cmd.args, cmd_no_window.args)
            self.assertEqual(cmd.kwargs['creationflags'], 0)
            self.assertEqual(cmd_no_window.kwargs['creationflags'], 0x08000000)
            self.assertNotIn('PRIVATE', output.getvalue())
            evidence = json.loads(output.getvalue())['windows_native_activation_failure']
            self.assertTrue(evidence['native_payload_available'])
            self.assertTrue(evidence['private_hook_stderr'][0]['categories']['invalid_handle'])

    def test_missing_payload_and_unknown_error_do_not_publish_raw_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / 'codex-hook-capture'
            capture.mkdir()
            (capture / 'private-payload-SessionStart-wrapped.json').write_text('{"session_id":"wrapped-only"}')
            output = io.StringIO()
            completed = subprocess.CompletedProcess([], 0, 'PRIVATE', 'PRIVATE')
            with patch.object(native.subprocess, 'run') as run, redirect_stdout(output):
                native.activation_failure_diagnostics(root, root, {}, root, {}, root, completed)
            run.assert_not_called()
            evidence = json.loads(output.getvalue())['windows_native_activation_failure']
            self.assertFalse(evidence['native_payload_available'])
            self.assertNotIn('PRIVATE', output.getvalue())
            self.assertFalse(any(native.error_categories('PRIVATE').values()))

    def test_wrapped_diagnostic_success_never_replaces_original_failed_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            command = ['codex', 'exec', 'help']
            original = subprocess.CompletedProcess(command, 0, '', 'original hook failed')
            wrapped = subprocess.CompletedProcess(command, 0, 'wrapped success', '')
            with patch.object(native, 'instrument_installed_hooks', return_value=(root, {}, root)) as instrument, \
                    patch.object(native.subprocess, 'run', return_value=wrapped) as launch, \
                    patch.object(native, 'activation_failure_diagnostics') as diagnostic:
                native.diagnose_original_failure(root, root, {'CODEX_HOME': str(root)}, '1.7.0', command, original)
            instrument.assert_called_once()
            launch.assert_called_once()
            self.assertIs(diagnostic.call_args.args[-2], original)
            self.assertIs(diagnostic.call_args.args[-1], wrapped)

    def test_original_native_gate_fails_after_wrapped_success_and_never_wraps_healthy_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            command = ['codex', 'exec', 'help']
            original = subprocess.CompletedProcess(command, 0, '', 'session id: abcd\n')
            order = []
            with patch.object(native.subprocess, 'run', side_effect=lambda *args, **kwargs: (order.append('original'), original)[1]), \
                    patch.object(native, 'state_file', return_value=state), \
                    patch.object(native, 'diagnose_original_failure', side_effect=lambda *args: order.append('diagnostic')):
                with self.assertRaisesRegex(RuntimeError, 'produced no Symphony state'):
                    native.original_native_activation(root, root, {}, '1.7.0', command)
                self.assertEqual(order, ['original', 'diagnostic'])
                state.write_text(json.dumps({'activation': {'codex': {'state': 'guarded', 'session_id': 'abcd'}}}))
                order.clear()
                result, session, heartbeat = native.original_native_activation(root, root, {}, '1.7.0', command)
                self.assertEqual(order, ['original'])
                self.assertIs(result, original)
                self.assertEqual(session, 'abcd')
                self.assertEqual(heartbeat['state'], 'guarded')
