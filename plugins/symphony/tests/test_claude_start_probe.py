"""The Windows control probe preserves the gate and publishes no native text."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[3] / '.github/scripts/claude_start_probe.py'
spec = importlib.util.spec_from_file_location('claude_start_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ClaudeStartProbeTests(unittest.TestCase):
    def run_fake(self, *, code=0, stderr=b'', timeout=False):
        process = Mock(pid=123)
        process.wait.side_effect = [subprocess.TimeoutExpired('PRIVATE_COMMAND', 600), 1] if timeout else [code]
        def launch(argv, **kwargs):
            self.assertEqual(argv, probe.command('claude.cmd'))
            self.assertNotIn('shell', kwargs)
            kwargs['stdout'].write('Private greeting: héllo\n'.encode())
            kwargs['stderr'].write(stderr)
            return process
        with TemporaryDirectory() as directory, patch.object(probe.subprocess, 'Popen', side_effect=launch), \
                patch.object(probe.subprocess, 'run') as terminate_tree:
            root = Path(directory)
            result = probe.run_probe('claude.cmd', root / 'private', 600)
            self.assertEqual((root / 'private/claude-start.stdout').read_bytes(),
                             'Private greeting: héllo\n'.encode())
            self.assertEqual((root / 'private/claude-start.stderr').read_bytes(), stderr)
            if timeout and probe.os.name == 'nt':
                self.assertEqual(terminate_tree.call_args.args[0], ['taskkill', '/PID', '123', '/T', '/F'])
            else:
                terminate_tree.assert_not_called()
            return result, process

    def test_success_retains_original_command_and_budget(self):
        self.assertEqual(probe.command('claude.cmd'), ['claude.cmd', '--print', '--model', 'haiku',
            '--max-budget-usd', '0.50', '--output-format', 'text',
            '/symphony:start Run git rev-parse --show-toplevel and report its output.'])
        result, process = self.run_fake()
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['timed_out'])
        self.assertFalse(result['launch_failed'])
        process.kill.assert_not_called()
        self.assertNotIn('Private', json.dumps(result))

    def test_nonzero_utf8_and_invalid_bytes_publish_only_categories(self):
        result, _ = self.run_fake(code=1, stderr=b'PRIVATE_AUTH_SENTINEL\x9d budget exceeded; ParserError')
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['categories']['budget_exceeded'])
        self.assertTrue(result['categories']['hook_failure'])
        self.assertTrue(result['stderr_nonempty'])
        self.assertNotIn('PRIVATE_AUTH_SENTINEL', json.dumps(result))
        self.assertNotIn('ParserError', json.dumps(result))

    def test_stop_hook_block_has_its_own_private_category(self):
        self.assertTrue(probe.error_categories('Blocked by hook: PRIVATE_CHILD')['stop_blocked'])
        self.assertNotIn('PRIVATE_CHILD', json.dumps(probe.error_categories('Blocked by hook: PRIVATE_CHILD')))

    def test_timeout_cannot_be_reported_as_success(self):
        result, process = self.run_fake(timeout=True)
        self.assertTrue(result['timed_out'])
        self.assertIsNone(result['exit_code'])
        process.kill.assert_called_once()
        self.assertNotIn('PRIVATE_COMMAND', json.dumps(result))

    def test_launch_error_message_is_private(self):
        with TemporaryDirectory() as directory, patch.object(probe.subprocess, 'Popen',
                side_effect=OSError('PRIVATE_AUTH_SENTINEL')):
            result = probe.run_probe('claude.cmd', Path(directory) / 'private', 600)
        self.assertTrue(result['launch_failed'])
        self.assertIsNone(result['exit_code'])
        self.assertNotIn('PRIVATE_AUTH_SENTINEL', json.dumps(result))

    def test_main_writes_only_fixed_receipt_and_returns_failure(self):
        receipt = {'exit_code': 1, 'timed_out': False, 'launch_failed': False,
                   'stdout_nonempty': True, 'stderr_nonempty': True,
                   'categories': probe.error_categories('authentication PRIVATE_AUTH_SENTINEL')}
        with TemporaryDirectory() as directory, patch.dict(probe.os.environ, {'RUNNER_TEMP': directory}), \
                patch.object(sys, 'argv', ['probe', '--timeout', '600']), \
                patch.object(probe.shutil, 'which', return_value='claude.cmd'), \
                patch.object(probe, 'run_probe', return_value=receipt), patch('builtins.print') as output:
            self.assertEqual(probe.main(), 1)
            saved = Path(directory) / 'symphony-native-diagnostics/claude-windows-start.json'
            self.assertEqual(json.loads(saved.read_text(encoding='utf-8')), receipt)
            self.assertNotIn('PRIVATE_AUTH_SENTINEL', saved.read_text(encoding='utf-8'))
            self.assertNotIn('PRIVATE_AUTH_SENTINEL', str(output.call_args))

    def test_windows_workflow_preserves_start_failure_gate(self):
        workflow = (SCRIPT.parents[1] / 'workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn('python .github/scripts/claude_start_probe.py --timeout 600\n'
                      "          if ($LASTEXITCODE -ne 0) { throw 'Claude start control failed' }", workflow)
        self.assertIn("symphony-native-diagnostics/*.json", workflow)


if __name__ == '__main__':
    unittest.main()
