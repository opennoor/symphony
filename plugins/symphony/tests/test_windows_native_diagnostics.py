"""Private native launcher diagnostics preserve the command and export fixed facts."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[3] / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('windows_native_activation', SCRIPTS / 'windows_native_activation.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class WindowsNativeDiagnosticsTests(unittest.TestCase):
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
            self.assertEqual(clock['command_windows'], '"' + sys.executable + '" -I capture.py SessionStart private codex')

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
            self.assertEqual(run.call_count, 2)
            first, second = run.call_args_list
            self.assertEqual(first.args, second.args)
            self.assertEqual(first.kwargs['input'], payload)
            self.assertEqual(first.kwargs['env']['PLUGIN_ROOT'], str(root / 'plugin'))
            self.assertEqual(first.kwargs['creationflags'], 0)
            self.assertEqual(second.kwargs['creationflags'], 0x08000000)
            self.assertEqual(first.args[0], 'C:\\Windows\\cmd.exe /C "powershell.exe -Command "original""')
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
