import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict:
    return json.loads((PLUGIN / relative).read_text(encoding="utf-8"))


def handlers(relative: str):
    for groups in load_json(relative)["hooks"].values():
        for group in groups:
            yield from group["hooks"]


class PackageContractTests(unittest.TestCase):
    def test_release_ci_requires_locally_tested_commit_without_provider_calls(self):
        workflow = (PLUGIN.parents[1] / '.github/workflows/ci.yml').read_text()
        triggers = workflow.split('\njobs:', 1)[0]
        self.assertIn('  workflow_dispatch:\n    inputs:\n      tested_sha:', triggers)
        self.assertIn('        required: true\n        type: string', triggers)
        self.assertNotIn('push:', triggers)
        self.assertNotIn('pull_request:', triggers)
        self.assertNotIn('schedule:', triggers)
        self.assertIn("  release-candidate:\n    if: github.event_name == 'workflow_dispatch' "
                      "&& github.ref_type == 'branch'", workflow)
        self.assertIn('TESTED_SHA: ${{ inputs.tested_sha }}', workflow)
        for forbidden in ('secrets.', 'api.openai.com', 'api.anthropic.com', '@openai/codex',
                          '@anthropic-ai/claude-code', 'claude.ai/install.sh', 'codex login',
                          'codex exec', 'claude --print', 'claude.cmd --', 'native_effort_smoke.py',
                          'native_routing_smoke.py', 'native_managed_concurrency.py',
                          'windows_native_activation.py', 'claude_start_probe.py', 'claude_isolated_worktree.py',
                          'refresh_profiles.py --agent-probe', 'refresh_profiles.py --verify'):
            self.assertNotIn(forbidden, workflow)
        for name in ('verify', 'windows-hooks', 'dockur-windows'):
            self.assertIn(f'  {name}:\n    needs: release-candidate\n'
                          "    if: needs.release-candidate.outputs.pending == 'true'", workflow)
        self.assertIn("  release:\n    needs: [verify, windows-hooks, dockur-windows]\n"
                      "    if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn('--target "$GITHUB_SHA"', workflow)
        refresh = (PLUGIN.parents[1] / '.github/workflows/capability-refresh.yml').read_text()
        self.assertIn('  workflow_dispatch:', refresh)
        self.assertNotIn('  schedule:', refresh)
        self.assertIn('  refresh:\n    if: ${{ false }}\n', refresh)
        if os.name == 'nt':
            return  # The release gate runs on Ubuntu; Windows checks its wiring above.
        command = workflow.split('        id: version\n', 1)[1].split('        run: |\n', 1)[1].split('\n\n', 1)[0]
        command = '\n'.join(line[10:] for line in command.splitlines())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', temporary], check=True)
            subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                            'commit', '-q', '--allow-empty', '-m', 'fixture'], cwd=root, check=True)
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
            manifests = [root / 'plugins/symphony' / provider / 'plugin.json'
                         for provider in ('.claude-plugin', '.codex-plugin')]
            for path in manifests:
                path.parent.mkdir(parents=True)
            output = root / 'outputs'
            for version, codex, tagged, tested, dispatched, expected in (
                    ('1.7.0', '1.7.0', False, head, head, 'pending=true'),
                    ('1.7.0', '1.7.0', True, head, head, 'pending=false'),
                    ('1.7.1', '1.7.0', False, head, head, None),
                    ('invalid', 'invalid', False, head, head, None),
                    ('1.7.1', '1.7.1', False, '', head, None),
                    ('1.7.1', '1.7.1', False, 'invalid', head, None),
                    ('1.7.1', '1.7.1', False, '0' * 40, head, None),
                    ('1.7.1', '1.7.1', False, head, '0' * 40, None),
                    ('1.7.1', '1.7.1', False, '0' * 40, '0' * 40, None)):
                with self.subTest(version=version, codex=codex, tagged=tagged, tested=tested, dispatched=dispatched):
                    for path, value in zip(manifests, (version, codex)):
                        path.write_text(json.dumps({'version': value}))
                    if tagged:
                        subprocess.run(['git', 'tag', 'v' + version], cwd=root, check=True)
                    output.write_text('')
                    result = subprocess.run(['bash', '-e', '-c', command], cwd=root, capture_output=True,
                                            text=True, env={**os.environ, 'GITHUB_OUTPUT': str(output),
                                                            'TESTED_SHA': tested, 'GITHUB_SHA': dispatched})
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(output.read_text(), '')
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(output.read_text().strip(), expected)

    def test_both_provider_manifests_declare_the_same_released_version(self):
        from plugins.symphony.symphony import PLUGIN_VERSION

        codex = load_json(".codex-plugin/plugin.json")["version"]
        claude = load_json(".claude-plugin/plugin.json")["version"]
        self.assertEqual(codex, claude, "provider manifests must not drift apart")
        self.assertEqual(codex, PLUGIN_VERSION, "the package must report what it ships as")
        self.assertRegex(codex, r"^\d+\.\d+\.\d+$")

    def test_hook_commands_are_root_relative_and_materialized(self):
        for provider, relative, root_name in (
            ("codex", "hooks/codex.json", "PLUGIN_ROOT"),
            ("claude", "hooks/hooks.json", "CLAUDE_PLUGIN_ROOT"),
        ):
            for handler in handlers(relative):
                command = handler["command"]
                self.assertIn(root_name, command, provider)
                self.assertNotIn("/cache/", command, provider)
                self.assertIn(" -I -c ", command, provider)
                self.assertIn("base64.b64decode(", command, provider)
                self.assertTrue((PLUGIN / "scripts/symphony_hook.py").is_file(), provider)

    def test_captured_launchers_match_all_reviewed_runtime_files(self):
        from plugins.symphony.scripts.generate_hooks import generated

        for path, expected in generated().items():
            self.assertEqual(path.read_text(), expected, "run scripts/generate_hooks.py after package changes")

    def test_windows_relay_uses_stored_gzip_and_fits_native_command_limit(self):
        import base64
        import gzip
        import shlex
        from plugins.symphony.scripts.generate_hooks import bootstrap, generated

        relay = (PLUGIN / 'scripts/codex_hook.ps1').read_text()
        for path, content in generated().items():
            handler = json.loads(content)['hooks']['SessionStart'][0]['hooks'][0]
            if path.name == 'codex.json':
                command = handler['commandWindows']
                self.assertLess(len(command), 8170)
                provider = 'codex'
            else:
                whole_command = handler['command']
                bash = (str(Path(os.environ['ProgramFiles']) / 'Git/bin/bash.exe')
                        if os.name == 'nt' else r'C:\Program Files\Git\bin\bash.exe')
                invocation = subprocess.list2cmdline([bash, '-c', whole_command])
                self.assertLess(len('cmd.exe /d /s /c ' + invocation), 8170)
                tokens = shlex.split(whole_command)
                self.assertEqual(tokens.count('SYMPHONY_CAPTURED_BOOTSTRAP=' + bootstrap() + ';'), 1)
                command = next(token for token in tokens if token.startswith('iex '))
                provider = 'claude'
            payload = base64.b64decode(re.search(r"FromBase64String\('([^']+)'", command)[1])
            binding = "$b = [Environment]::GetEnvironmentVariable('SYMPHONY_CAPTURED_BOOTSTRAP')"
            expected = relay.replace("$b = '__SYMPHONY_BOOTSTRAP__'", binding).replace('__SYMPHONY_PROVIDER__', provider).encode()
            self.assertEqual(gzip.decompress(payload), expected)
            self.assertEqual(payload[:10], bytes.fromhex('1f8b08000000000000ff'))
            self.assertEqual((payload[10] >> 1) & 3, 0, 'DEFLATE must use stored blocks')
            self.assertEqual(len(payload), len(expected) + 23)

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_outer_powershell_preserves_exact_wrapper_and_reproduces_old_variable_expansion(self):
        from plugins.symphony.scripts.generate_hooks import generated
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        document = json.loads(generated()[PLUGIN / 'hooks/codex.json'])
        command = document['hooks']['SessionStart'][0]['hooks'][0]['commandWindows']
        wrapper = command.split('-Command "', 1)[1][:-1]
        self.assertNotIn('$', wrapper)
        stream = re.search(r"\[IO.MemoryStream\]::new\(\[Convert\]::FromBase64String\('[^']+'\)\)", wrapper)[0]
        old = '$m=' + stream + ';' + wrapper.replace(stream, '$m')
        capture = ('function global:cmd.exe { $t=$null; $e=$null; '
            '[void][Management.Automation.Language.Parser]::ParseInput($args[-1],[ref]$t,[ref]$e); '
            '@{received=$args[-1];parse_error_ids=@($e | ForEach-Object ErrorId)} | ConvertTo-Json -Compress }; ')
        for candidate, expected in ((wrapper, True), (old, False)):
            result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                capture + command.replace(wrapper, candidate)], input='{}', capture_output=True,
                text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value['received'] == candidate, expected)
            self.assertEqual(not value['parse_error_ids'], expected)

    @unittest.skipUnless(os.name == 'nt', 'runs actual Windows selected shells')
    def test_native_selected_windows_shells_run_hooks_after_source_cache_removal(self):
        from plugins.symphony.scripts.generate_hooks import generated
        from plugins.symphony.symphony.store import StateStore
        with tempfile.TemporaryDirectory(prefix='selected shell fixture ') as temporary:
            directory = Path(temporary)
            root = directory / 'Reviewed Plugin With Spaces'
            shutil.copytree(PLUGIN, root)
            project = directory / 'Project With Spaces'
            project.mkdir()
            command = json.loads(generated(root)[root / 'hooks/codex.json'])['hooks']['SessionStart'][0]['hooks'][0]['commandWindows']
            env = {**os.environ, 'PLUGIN_ROOT': str(root), 'SYMPHONY_STATE_DIR': str(directory / 'state'),
                'SYMPHONY_RUNTIME_DIR': str(directory / 'runtimes'), 'PSExecutionPolicyPreference': 'Restricted',
                'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')}
            ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            shells = ([ps, '-NoProfile', '-Command', command], env.get('COMSPEC', 'cmd.exe') + ' /C "' + command + '"')
            for removed in (False, True):
                if removed:
                    shutil.rmtree(root)
                for index, shell in enumerate(shells):
                    for flags in (0, 0x08000000):
                        session = f'native-shell-{removed}-{index}-{flags}'
                        result = subprocess.run(shell, input=json.dumps({'hook_event_name': 'SessionStart',
                            'session_id': session, 'cwd': str(project), 'note': '✓'}, ensure_ascii=False),
                            cwd=project, env=env, capture_output=True, text=True, encoding='utf-8',
                            timeout=30, creationflags=flags)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        activation = StateStore(directory / 'state').load(project).activation['codex']
                        self.assertEqual(activation['state'], 'guarded')
                        self.assertEqual(activation['session_id'], session)

    @staticmethod
    def _mock_windows_discovery(relay, code):
        import base64
        encoded = base64.b64encode(code.encode()).decode()
        arguments = '-I -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        target = next(line for line in relay.splitlines() if line.startswith('$q=') and "System32\\cmd.exe" in line)
        return relay.replace(target, '$q=[Diagnostics.ProcessStartInfo]::new('
                             + quote(sys.executable) + ',' + quote(arguments) + ')')

    def _probe_executable(self, directory, delay=0, name='python3.exe', marker=None):
        probe = directory / name
        if os.name == 'nt':
            source = directory / 'probe.cs'
            executable = sys.executable.replace('"', '""')
            mark = ('System.IO.File.WriteAllText(@"' + str(marker).replace('"', '""') + '","x"); '
                    if marker else '')
            source.write_text('using System; class Probe { static void Main() { ' + mark
                              + 'System.Threading.Thread.Sleep(' + str(delay) + '); Console.WriteLine(@"'
                              + executable + '"); } }')
            quote = lambda path: "'" + str(path).replace("'", "''") + "'"
            ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            compiled = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                'Add-Type -Path ' + quote(source) + ' -OutputAssembly ' + quote(probe)
                + ' -OutputType ConsoleApplication -ErrorAction Stop'],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
        else:
            probe.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(' + str(delay / 1000)
                             + ')\nprint(' + repr(sys.executable) + ')\n')
            probe.chmod(0o755)
        return probe

    def test_windows_discovery_is_native_bounded_and_fits_event_timeouts(self):
        source = (PLUGIN / 'scripts/codex_hook.ps1').read_text()
        self.assertIn("System32\\cmd.exe", source)
        self.assertIn('/d /u /v:off /c', source)
        self.assertIn('[Text.Encoding]::Unicode', source)
        self.assertIn('$q.StandardOutputEncoding=[Text.Encoding]::UTF8', source)
        self.assertNotIn('Get-Command', source)
        self.assertIn(':probe for($n=0;$n -lt $all.Count;$n++)', source)
        self.assertIn("foreach($name in 'python.exe','python3.exe','py.exe')", source)
        self.assertIn('$cs=$all|Group-Object Name -AsHashTable', source)
        self.assertNotIn('Select-Object -Skip $n -First 1', source)
        self.assertLess(source.index('$end=[DateTime]::UtcNow.AddSeconds(4)'),
                        source.index('$cs=$all|Group-Object Name -AsHashTable'))
        self.assertIn('$p.WaitForExit([Math]::Min(2500,$ms))', source)
        self.assertIn('$end=[DateTime]::UtcNow.AddSeconds(4)', source)
        self.assertEqual(source.count('-I -X utf8 -c'), 2)
        for manifest in ('hooks/codex.json', 'hooks/hooks.json'):
            for groups in load_json(manifest)['hooks'].values():
                for group in groups:
                    for hook in group['hooks']:
                        self.assertGreaterEqual(hook['timeout'], 20, manifest)

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_windows_relay_does_not_hide_other_interpreters_behind_broken_python_entries(self):
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        for working_name, broken_count in (('python3.exe', 6), ('python.exe', 2), ('python.exe', 6), ('py.exe', 6)):
            with self.subTest(working_name=working_name, broken_count=broken_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directories = [root / ('broken' + str(n)) for n in range(broken_count)] + [root / 'working']
                for directory in directories:
                    directory.mkdir()
                for directory in directories[:-1]:
                    (directory / 'python.exe').write_bytes(b'not an interpreter')
                probe = self._probe_executable(directories[-1], name=working_name)
                records = [str(directory / 'python.exe') for directory in directories[:-1]] + [str(probe)]
                code = 'import sys;sys.stdout.buffer.write(' + repr(''.join('"' + path + '"\n' for path in records)) + ".encode('utf-16-le'));sys.stdout.buffer.flush()"
                relay = self._mock_windows_discovery((PLUGIN / 'scripts/codex_hook.ps1').read_text()
                            .replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)'), code)
                for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                    with self.subTest(provider=provider):
                        result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                            relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                            text=True, timeout=15, env={**os.environ, 'PATH': os.pathsep.join(map(str, directories)), variable: temporary})
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), '123')

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_windows_relay_deduplicates_repeated_path_candidates(self):
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken, working = root / 'broken', root / 'working'
            broken.mkdir()
            working.mkdir()
            (broken / 'python.exe').write_bytes(b'not an interpreter')
            probe = self._probe_executable(working)
            records = [str(broken / 'python.exe'), str(broken / 'python.exe'), str(probe)]
            code = 'import sys;sys.stdout.buffer.write(' + repr(''.join('"' + path + '"\n' for path in records)) + ".encode('utf-16-le'));sys.stdout.buffer.flush()"
            relay = self._mock_windows_discovery((PLUGIN / 'scripts/codex_hook.ps1').read_text()
                        .replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)'), code)
            for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                    relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                    text=True, timeout=15, env={**os.environ,
                        'PATH': os.pathsep.join((str(broken), str(broken), str(working))), variable: temporary})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '123')

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_windows_discovery_timeout_preserves_candidates_without_running_the_hook_twice(self):
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory() as temporary:
            probe = self._probe_executable(Path(temporary))
            for partial in ('none', 'complete', 'truncated-tail'):
                for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                    with self.subTest(partial=partial, provider=provider):
                        output = '"' + str(probe) + '"\n' if partial != 'none' else ''
                        if partial == 'truncated-tail':
                            output += '"unfinished'
                        code = 'import sys,time;sys.stdout.buffer.write(' + repr(output) + ".encode('utf-16-le'));sys.stdout.buffer.flush();time.sleep(20)"
                        relay = self._mock_windows_discovery((PLUGIN / 'scripts/codex_hook.ps1').read_text()
                                    .replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)'), code)
                        began = time.monotonic()
                        result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                            relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                            text=True, timeout=10, env={**os.environ, 'PATH': temporary, variable: temporary})
                        self.assertLess(time.monotonic() - began, 8)
                        if partial != 'none':
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout.strip(), '123')
                        else:
                            self.assertEqual(result.returncode, 1)
                            self.assertEqual(result.stdout, '')
                            self.assertIn('Python discovery timed out', result.stderr)

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_windows_relay_accepts_a_slow_working_interpreter_probe(self):
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory(prefix='slow interpreter probe ') as temporary:
            directory = Path(temporary)
            probe = self._probe_executable(directory, delay=1800)
            relay = (PLUGIN / 'scripts/codex_hook.ps1').read_text().replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)')
            if os.name != 'nt':
                code = 'import sys;sys.stdout.buffer.write(' + repr('"' + str(probe) + '"\n') + ".encode('utf-16-le'));sys.stdout.buffer.flush()"
                relay = self._mock_windows_discovery(relay, code)
            for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                    relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                    text=True, timeout=15, env={**os.environ, 'PATH': temporary, variable: temporary})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '123')

    @unittest.skipUnless(os.name == 'nt', 'runs native cmd discovery on Windows')
    def test_native_windows_discovery_preserves_unicode_and_metacharacters_without_cwd_search(self):
        ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / 'Unicode 国王 %n %PATH% ! & ^ ( )'
            good.mkdir()
            shutil.copy2(sys.executable, good / 'python.exe')
            for dll in Path(sys.base_prefix).glob('*.dll'):
                shutil.copy2(dll, good / dll.name)
            shutil.copytree(Path(sys.base_prefix) / 'Lib', good / 'Lib',
                ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'idlelib',
                                              'tkinter', 'ensurepip', 'venv'))
            marker = root / 'decoy-ran'
            self._probe_executable(root, name='python.exe', marker=marker)
            directories = [root / ('absent' + str(n)) for n in range(160)]
            entries = [r'\\symphony.invalid\unavailable', '', '.', 'C:relative', r'\relative', '"' + str(good) + '"']
            entries.extend(map(str, directories))
            self.assertGreater(len(';'.join(entries)), 8191)
            relay = (PLUGIN / 'scripts/codex_hook.ps1').read_text().replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)')
            for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                began = time.monotonic()
                result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                    "[Console]::OutputEncoding=[Text.Encoding]::GetEncoding(1252);"
                    + relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                    text=True, timeout=10, cwd=root,
                    env={**os.environ, 'PATH': ';'.join(entries), variable: temporary})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '123')
                self.assertFalse(marker.exists(), 'empty or relative PATH searched the current project')
                self.assertLess(time.monotonic() - began, 8)

    @unittest.skipUnless(os.name == 'nt', 'needs Windows drive-root paths')
    def test_native_windows_discovery_keeps_drive_root_absolute(self):
        ps = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            drive_root = work.anchor
            marker = work / 'decoy-ran'
            self._probe_executable(work, name='python.exe', marker=marker)
            good = work / 'working'
            good.mkdir()
            self._probe_executable(good, name='python.exe')
            relay = (PLUGIN / 'scripts/codex_hook.ps1').read_text()
            inspect = relay.split('$ps=', 1)[0] + (
                '[Console]::WriteLine($ds[0]);'
                '[Console]::WriteLine((N ([IO.Path]::GetDirectoryName('
                '$env:SYMPHONY_ROOT_CANDIDATE))))')
            env = {**os.environ, 'PATH': drive_root,
                   'SYMPHONY_ROOT_CANDIDATE': str(Path(drive_root) / 'python.exe')}
            result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command', inspect],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [drive_root, drive_root])
            for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                    relay.replace('__SYMPHONY_BOOTSTRAP__', 'import sys;sys.stdin.buffer.read();print(123)')
                         .replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                    text=True, timeout=15, cwd=work,
                    env={**os.environ, 'PATH': drive_root + os.pathsep + str(good), variable: temporary})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '123')
                self.assertFalse(marker.exists(), 'drive-root PATH searched the current project')

    @unittest.skipUnless(shutil.which('pwsh') or os.name == 'nt', 'needs PowerShell')
    def test_empty_windows_path_never_launches_discovery(self):
        ps = shutil.which('pwsh') or str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / 'discovery-ran'
            code = 'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("ran")'
            relay = self._mock_windows_discovery((PLUGIN / 'scripts/codex_hook.ps1').read_text(), code)
            for provider, variable in (('codex', 'PLUGIN_ROOT'), ('claude', 'CLAUDE_PLUGIN_ROOT')):
                for path in ('', os.pathsep.join(('', '.', 'C:relative'))):
                    with self.subTest(provider=provider, path=path):
                        result = subprocess.run([ps, '-NoProfile', '-NonInteractive', '-Command',
                            "$env:PATH='" + path.replace("'", "''") + "';"
                            + relay.replace('__SYMPHONY_PROVIDER__', provider)], input='{}', capture_output=True,
                            text=True, timeout=10, env={**os.environ, variable: temporary})
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn('No candidates', result.stderr)
                        self.assertFalse(marker.exists(), 'unlisted interpreter discovery ran')

    @unittest.skipUnless(Path('/usr/bin/python3').is_file(), 'needs a second system Python')
    def test_generated_launchers_match_system_python_compression_backend(self):
        import hashlib
        from plugins.symphony.scripts.generate_hooks import generated

        expected = {path.name: content for path, content in generated().items()}
        code = ('import sys,json,hashlib;sys.path.insert(0,sys.argv[1]);'
                'import generate_hooks as g;'
                'print(hashlib.sha256(json.dumps({p.name:c for p,c in g.generated().items()},'
                'sort_keys=True).encode()).hexdigest())')
        result = subprocess.run(['/usr/bin/python3', '-c', code, str(PLUGIN / 'scripts')],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest())

    def test_hook_starts_when_datetime_utc_is_unavailable(self):
        """A generic python3 hook must work on Python 3.10, before UTC existed."""
        from plugins.symphony.symphony.store import StateStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            state_root = Path(directory) / "state"
            script = PLUGIN / "scripts" / "symphony_hook.py"
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import datetime, runpy, sys; "
                    "datetime.__dict__.pop('UTC', None); "
                    "runpy.run_path(sys.argv[1], run_name='__main__')"
                ), str(script)],
                input=json.dumps({
                    "hook_event_name": "SessionStart",
                    "session_id": "python310-smoke",
                    "cwd": str(project),
                }),
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYMPHONY_STATE_DIR": str(state_root),
                    "SYMPHONY_PROVIDER": "codex",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            activation = StateStore(state_root).load(project).activation["codex"]
            self.assertEqual(activation["session_id"], "python310-smoke")
            self.assertEqual(activation["state"], "guarded")

    def test_hook_accepts_utf8_bom_from_windows_powershell_relay(self):
        from plugins.symphony.symphony.store import StateStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            state_root = Path(directory) / "state"
            payload = json.dumps({
                "hook_event_name": "SessionStart",
                "session_id": "bom-session",
                "cwd": str(project),
            }).encode("utf-8")
            result = subprocess.run(
                [sys.executable, str(PLUGIN / "scripts/symphony_hook.py")],
                input=b"\xef\xbb\xbf" + payload,
                capture_output=True,
                env={**os.environ, "SYMPHONY_STATE_DIR": str(state_root),
                     "SYMPHONY_PROVIDER": "codex"},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(b"Symphony hook fault", result.stderr)
            activation = StateStore(state_root).load(project).activation["codex"]
            self.assertEqual(activation["session_id"], "bom-session")
            self.assertEqual(activation["state"], "guarded")

    def test_codex_activation_check_requires_matching_current_session_and_plugin(self):
        from plugins.symphony.symphony.store import project_key

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            state_root = Path(directory) / "state"
            environment = {**os.environ, "SYMPHONY_STATE_DIR": str(state_root),
                           "SYMPHONY_PROVIDER": "codex", "CODEX_SESSION_ID": "root-session"}
            checker = PLUGIN / "scripts/check_activation.py"

            def check(env=environment):
                return subprocess.run([sys.executable, str(checker)], cwd=project,
                                      capture_output=True, text=True, env=env, check=False)

            self.assertNotEqual(check().returncode, 0)
            hook = subprocess.run(
                [sys.executable, str(PLUGIN / "scripts/symphony_hook.py")],
                input=json.dumps({"hook_event_name": "UserPromptSubmit",
                                  "session_id": "root-session", "cwd": str(project), "prompt": "hello"}),
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertEqual(hook.returncode, 0, hook.stderr)
            current = check({**environment, "CODEX_THREAD_ID": "child-thread"})
            self.assertEqual(current.returncode, 0, current.stdout)
            self.assertIn("guarded", current.stdout)

            other = subprocess.run(
                [sys.executable, str(PLUGIN / "scripts/symphony_hook.py")],
                input=json.dumps({"hook_event_name": "SessionStart",
                                  "session_id": "other-session", "cwd": str(project)}),
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertEqual(other.returncode, 0, other.stderr)
            self.assertEqual(check().returncode, 0, "another session hid this session's heartbeat")

            self.assertEqual(subprocess.run(
                [sys.executable, str(PLUGIN / "scripts/symphony_hook.py")],
                input=json.dumps({"hook_event_name": "SessionStart",
                                  "session_id": "root-session", "cwd": str(project)}),
                capture_output=True, text=True, env=environment, check=False,
            ).returncode, 0)

            path = state_root / f"{project_key(project)}.v2.json"
            original = path.read_text()
            self.assertNotEqual(check({**environment, "CODEX_SESSION_ID": "unknown-session"}).returncode, 0)
            document = json.loads(original)
            foreign = next(event for event in document["event_history"]
                           if event["kind"] == "session_heartbeat")
            foreign["payload"].update({"provider": "claude", "session_id": "unknown-session"})
            path.write_text(json.dumps(document))
            self.assertNotEqual(check({**environment, "CODEX_SESSION_ID": "unknown-session"}).returncode, 0)
            for key, value in (("plugin_version", "0.0.0"), ("plugin_root", "/other/plugin"),
                               ("hook_schema_version", -1), ("observed_at", "")):
                document = json.loads(original)
                document["activation"]["codex"][key] = value
                document["activation"]["codex"]["session_profiles"] = []
                document["event_history"] = []
                path.write_text(json.dumps(document))
                with self.subTest(key=key):
                    self.assertNotEqual(check().returncode, 0)

            document = json.loads(original)
            heartbeat = document["activation"]["codex"]
            document["activation"]["codex"] = {
                **heartbeat,
                "session_id": "other-session",
                "session_profiles": [{
                    key: heartbeat[key]
                    for key in ("session_id", "plugin_version", "plugin_root",
                                "hook_schema_version", "observed_at")
                }],
            }
            # The current owner's heartbeat may be older than the 200-event
            # durable history window while other sessions keep using the repo.
            document["event_history"] = [
                {"event_id": f"later-{index}", "kind": "user_prompt",
                 "observed_at": "2026-09-25T00:00:00+00:00", "payload": {}}
                for index in range(201)
            ]
            path.write_text(json.dumps(document))
            self.assertEqual(check().returncode, 0,
                             "a retained active-session heartbeat must outlive event history")
            document["activation"]["codex"]["session_profiles"][0]["plugin_root"] = "/other/plugin"
            path.write_text(json.dumps(document))
            self.assertNotEqual(check().returncode, 0)
            path.write_text(original)
            self.assertNotEqual(check({key: value for key, value in environment.items()
                                       if key != "CODEX_SESSION_ID"}).returncode, 0)
            self.assertEqual(path.read_text(), original)

    def test_codex_windows_hooks_use_the_packaged_launcher(self):
        import base64
        import gzip
        import re
        from plugins.symphony.scripts.generate_hooks import bootstrap

        prefix = 'cmd.exe /c powershell.exe -NoProfile -NonInteractive -Command "'
        for handler in handlers("hooks/codex.json"):
            command = handler["commandWindows"]
            self.assertTrue(command.startswith(prefix))
            self.assertTrue(command.endswith('"'))
            wrapper = command[len(prefix):-1]
            self.assertTrue(wrapper.isascii())
            self.assertNotIn('"', wrapper)
            payload = re.search(r"FromBase64String\('([^']+)'\)", wrapper).group(1)
            source = gzip.decompress(base64.b64decode(payload)).decode()
            expected = (PLUGIN / "scripts/codex_hook.ps1").read_text().replace(
                "$b = '__SYMPHONY_BOOTSTRAP__'", "$b = [Environment]::GetEnvironmentVariable('SYMPHONY_CAPTURED_BOOTSTRAP')").replace('__SYMPHONY_PROVIDER__', 'codex')
            capture = "[Environment]::SetEnvironmentVariable('SYMPHONY_CAPTURED_BOOTSTRAP','" + bootstrap().replace("'", "''") + "');"
            self.assertTrue(wrapper.startswith(capture))
            self.assertEqual(wrapper.count(capture), 1)
            self.assertEqual(source, expected)
            self.assertIn("-I -X utf8 -c", source)
            self.assertLess(len(command) + len('cmd.exe /C ""'), 8191)
            self.assertNotIn(".ps1", source)
            self.assertEqual(command.count('"'), 2)

    def test_claude_hooks_select_available_python_with_a_quoted_plugin_path(self):
        for handler in handlers("hooks/hooks.json"):
            self.assertEqual(handler["shell"], "bash")
            self.assertIn("python3", handler["command"])
            self.assertIn("python", handler["command"])
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}"', handler["command"])
            self.assertIn(" -I -c ", handler["command"])

        from plugins.symphony.symphony.store import StateStore

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "Claude Plugin With Spaces"
            shutil.copytree(PLUGIN, root)
            project = home / "Project With Spaces"
            project.mkdir()
            state_root = home / "state"
            handler = next(handlers("hooks/hooks.json"))
            env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(root),
                   "SYMPHONY_STATE_DIR": str(state_root), "SYMPHONY_RUNTIME_DIR": str(home / "runtimes"),
                   "SYMPHONY_CAPTURED_BOOTSTRAP": "raise RuntimeError('inherited bootstrap executed')"}
            env.pop("SYMPHONY_PROVIDER", None)
            if os.name == "nt":
                bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe")
                aliases = home / "WindowsApps-like alias"
                aliases.mkdir()
                (aliases / 'python.exe').write_bytes(b'not a Windows executable')
                env['PATH'] = str(aliases) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
                env['PLUGIN_ROOT'] = str(home / 'Wrong Codex Root')
            else:
                bash = shutil.which("bash")
                bin_dir = home / "python3 only"
                bin_dir.mkdir()
                (bin_dir / "python3").symlink_to(sys.executable)
                env["PATH"] = str(bin_dir)
                env["OS"] = ""
            result = subprocess.run(
                [bash, "-c", handler["command"] + '; printf "\\n%s" "$SYMPHONY_CAPTURED_BOOTSTRAP"'],
                input=json.dumps({"hook_event_name": "SessionStart", "session_id": "claude-session",
                                  "cwd": str(project), "model": "claude-sonnet"}),
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.endswith(env['SYMPHONY_CAPTURED_BOOTSTRAP']),
                            'captured bootstrap export escaped the hook subshell')
            activation = StateStore(state_root).load(project).activation
            self.assertEqual(activation["claude"]["state"], "guarded")
            self.assertEqual(activation["claude"]["session_id"], "claude-session")
            self.assertNotIn("codex", activation)

    @unittest.skipUnless(os.name == "nt", "runs the Windows shell command")
    def test_windows_launcher_propagates_runtime_failure_without_reexecuting_hook(self):
        from plugins.symphony.scripts.generate_hooks import generated
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / 'Reviewed Plugin'
            shutil.copytree(PLUGIN, root)
            marker = directory / 'executions'
            (root / 'scripts/symphony_hook.py').write_text(
                "from pathlib import Path\np=Path(" + repr(str(marker)) + ")\n"
                "p.write_text(p.read_text()+'x' if p.exists() else 'x')\nraise SystemExit(73)\n")
            manifest = json.loads(generated(root)[root / 'hooks/codex.json'])
            command = manifest['hooks']['SessionStart'][0]['hooks'][0]['commandWindows']
            env = {**os.environ, 'PLUGIN_ROOT': str(root), 'SYMPHONY_RUNTIME_DIR': str(directory / 'runtimes'),
                'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''),
                'PSExecutionPolicyPreference': 'Restricted'}
            result = subprocess.run('cmd.exe /C "' + command + '"', env=env, cwd=directory,
                input='{}', capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 73, result.stderr)
            self.assertEqual(marker.read_text(), 'x')

    @unittest.skipUnless(os.name == "nt", "runs the Windows shell command")
    def test_codex_windows_hooks_run_without_a_working_py_launcher(self):
        from plugins.symphony.symphony import HOOK_SCHEMA_VERSION
        from plugins.symphony.symphony.store import StateStore

        if os.environ.get("SYMPHONY_REQUIRE_STANDARD_USER"):
            import ctypes
            self.assertEqual(ctypes.windll.shell32.IsUserAnAdmin(), 0)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "Symphony Plugin With Spaces"
            shutil.copytree(PLUGIN, root)
            project = home / "Current Project"
            project.mkdir()
            (project / "symphony_hook.py").write_text("raise SystemExit(79)\n")
            state_root = home / "state"
            launcher_dir = home / "broken launcher"
            launcher_dir.mkdir()
            (launcher_dir / "py.exe").write_bytes(b"not a Windows executable")
            alias_dir = home / "WindowsApps-like alias"
            alias_dir.mkdir()
            (alias_dir / "python.exe").write_bytes(b"not a Windows executable")
            env = os.environ.copy()
            env.update({
                "PATH": os.pathsep.join((str(launcher_dir), str(Path(sys.executable).parent),
                                          str(alias_dir),
                                          str(Path(os.environ["SystemRoot"]) / "System32"),
                                          str(Path(os.environ["SystemRoot"]) / "System32" /
                                              "WindowsPowerShell" / "v1.0"))),
                "PLUGIN_ROOT": str(root),
                "SYMPHONY_STATE_DIR": str(state_root),
                "SYMPHONY_RUNTIME_DIR": str(home / "runtimes"),
                "PSExecutionPolicyPreference": "Restricted",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            policy = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$env:PSExecutionPolicyPreference"],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(policy.returncode, 0, policy.stderr)
            self.assertEqual(policy.stdout.strip(), "Restricted")
            blocked_script = home / "unsigned.ps1"
            blocked_script.write_text("exit 79\n")
            blocked = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(blocked_script)],
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertNotEqual(blocked.returncode, 79, "test policy allowed an unsigned script")
            self.assertTrue(blocked.stderr.strip(), "script block had no policy error")
            python = subprocess.run(["cmd", "/d", "/s", "/c", "python --version"],
                                    capture_output=True, text=True, env=env, check=False)
            self.assertEqual(python.returncode, 0, python.stderr)
            if not os.environ.get("SYMPHONY_REQUIRE_STANDARD_USER"):
                # A corrupt .exe can trigger an invisible Windows error dialog
                # on a credentialed, non-interactive test desktop.
                broken_py = subprocess.run(["cmd", "/d", "/s", "/c", "py -3 --version"],
                                           capture_output=True, text=True, env=env, check=False)
                self.assertNotEqual(broken_py.returncode, 0)
            config = load_json("hooks/codex.json")
            # Codex wraps commandWindows in quotes when calling cmd.exe /C.
            def run_hook(command: str, payload: str, environment: dict[str, str]):
                return subprocess.run(f'cmd.exe /C "{command}"', input=payload,
                                      capture_output=True, text=True, env=environment,
                                      cwd=project, check=False)

            payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "windows-session",
                                  "cwd": str(project)})
            for event, groups in config["hooks"].items():
                command = next(handler["commandWindows"] for group in groups
                               for handler in group["hooks"])
                event_payload = json.dumps({"hook_event_name": event, "session_id": "windows-session",
                                            "cwd": str(project)})
                result = run_hook(command, event_payload, env)
                self.assertEqual(result.returncode, 0, f"{event}: {result.stderr}")
            interrupt = config["hooks"]["Interrupt"][0]["hooks"][0]["commandWindows"]
            timings = []
            for _ in range(6):
                started = time.perf_counter()
                result = run_hook(interrupt, json.dumps({"hook_event_name": "Interrupt",
                                                        "session_id": "windows-session",
                                                        "cwd": str(project)}), env)
                timings.append(time.perf_counter() - started)
                self.assertEqual(result.returncode, 0, result.stderr)
            limit = float(os.environ.get("SYMPHONY_WINDOWS_INTERRUPT_LIMIT", "3"))
            self.assertLess(max(timings), limit, f"Interrupt hook exceeded {limit}s: {timings}")
            print(f"Windows Interrupt hook max {max(timings):.2f}s across {len(timings)} runs")
            activation = StateStore(state_root).load(project).activation["codex"]
            self.assertEqual(activation["session_id"], "windows-session")
            self.assertEqual(activation["state"], "guarded")
            self.assertEqual(activation["plugin_version"], load_json(".codex-plugin/plugin.json")["version"])
            self.assertTrue(os.path.samefile(activation["plugin_root"], root))
            self.assertEqual(activation["hook_schema_version"], HOOK_SCHEMA_VERSION)
            self.assertTrue(activation["observed_at"])

            (launcher_dir / "python.exe").write_bytes(b"not a Windows executable")
            missing_state = home / "missing-python-state"
            env["SYMPHONY_STATE_DIR"] = str(missing_state)
            start = config["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
            failed = run_hook(start, payload, env)
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertTrue(missing_state.exists(), 'broken first alias did not fall back to usable Python')
            env["PATH"] = os.pathsep.join((str(launcher_dir), str(alias_dir),
                str(Path(os.environ["SystemRoot"]) / "System32"),
                str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0")))
            env["SYMPHONY_STATE_DIR"] = str(home / 'no-python-state')
            unavailable = run_hook(start, payload, env)
            self.assertNotEqual(unavailable.returncode, 0)
            self.assertIn('no working Python 3.10+', unavailable.stderr)
            self.assertFalse((home / 'no-python-state').exists())

        # The digest-pinned Dockur image invokes this exact test entrypoint.
        # Its guest has no Git Bash; native Windows CI exercises both hosts.
        from plugins.symphony.tests.test_runtime_retention import RuntimeRetentionTests

        RuntimeRetentionTests().exercise_removed_cache(("codex",))

    def test_hook_manifests_contain_only_supported_events(self):
        self.assertEqual(
            set(load_json("hooks/codex.json")["hooks"]),
            {"SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop", "Interrupt"},
        )
        self.assertEqual(
            set(load_json("hooks/hooks.json")["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart", "SubagentStop", "PostToolUse", "PostToolUseFailure", "Stop"},
        )

    def test_provider_help_uses_only_native_command_syntax(self):
        codex = (PLUGIN / "skills/symphony/SKILL.md").read_text(encoding="utf-8")
        claude = (PLUGIN / "commands/help.md").read_text(encoding="utf-8")
        self.assertIn("`$symphony:symphony ...`", codex)
        self.assertIn("Codex does not support `/symphony:*`", codex)
        self.assertIn("`/symphony:help`", claude)
        self.assertNotIn("$symphony", claude)

    def test_all_control_wrappers_exist(self):
        from plugins.symphony.symphony.runtime import CONTROLS

        self.assertEqual({path.stem for path in (PLUGIN / "commands").glob("*.md")}, CONTROLS)

    def test_every_control_is_documented_where_a_user_would_look(self):
        """A control nobody can discover may as well not exist.

        Adding one means touching four lists, and nothing checked that they
        agreed, so the help quietly fell behind the code.
        """
        from plugins.symphony.symphony.runtime import CONTROLS, _help

        readme = (PLUGIN.parent.parent / "README.md").read_text(encoding="utf-8")
        claude_help = (PLUGIN / "commands/help.md").read_text(encoding="utf-8")
        # assertTrue, not assertIn: a failed assertIn prints the whole README.
        for name in sorted(CONTROLS):
            for missing, where in (
                (f"/symphony:{name}" not in claude_help, "the Claude help command"),
                (f"/symphony:{name}" not in readme, "the README's Claude list"),
                (f"$symphony:symphony {name}" not in readme, "the README's Codex list"),
                (name not in _help("claude"), "the Claude help line"),
                (name not in _help("codex"), "the Codex help line"),
            ):
                self.assertFalse(missing, f"control {name!r} is missing from {where}")

    def test_role_contracts_classify_each_actionable_packet(self):
        roles = (PLUGIN / "skills/symphony/references/role-contracts.md").read_text(encoding="utf-8")
        self.assertIn("size", roles)
        self.assertIn("complexity", roles)
        self.assertIn("Each consultant decision is classified separately", roles)
        self.assertIn('fork_turns="none"', roles)
        self.assertIn("SYMPHONY_ASSESSMENT:", roles)
        self.assertIn("one `SYMPHONY_DECISION` line per actionable decision", roles)

    def test_claude_role_agents_pin_model_and_effort_in_their_names(self):
        agents = list((PLUGIN / "agents").glob("symphony-*.md"))
        self.assertGreaterEqual(len(agents), 4)
        for path in agents:
            text = path.read_text(encoding="utf-8")
            model = re.search(r"^model: (\S+)$", text, re.MULTILINE)
            effort = re.search(r"^effort: (\S+)$", text, re.MULTILINE)
            self.assertIsNotNone(model, path.name)
            self.assertIsNotNone(effort, path.name)
            self.assertIn(f"-{model.group(1)}-{effort.group(1)}", path.stem)
        assessor = next((PLUGIN / "agents").glob("symphony-assessor-*-high.md")).read_text(encoding="utf-8")
        self.assertIn("SYMPHONY_ASSESSMENT:", assessor)
        for path in (PLUGIN / "agents").glob("symphony-consultant-*.md"):
            self.assertIn("SYMPHONY_DECISION:", path.read_text(encoding="utf-8"))

    def test_capability_routing_assigns_supporting_workflows(self):
        routing = (PLUGIN / "skills/symphony/references/capability-routing.md").read_text(encoding="utf-8")
        for capability in ("Ponytail", "Context7", "Compound Engineering", "Superpowers", "Codebase Memory", "Matt Pocock"):
            self.assertIn(capability, routing)
        for status in ("absent", "disabled", "failed", "incompatible", "incomplete"):
            self.assertIn(status, routing)
        self.assertIn("native practice", routing)
        readme = (PLUGIN.parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertTrue("does not persist cross-session" in readme, "README overstates notice deduplication")

    def test_generated_agents_keep_phase_practice_and_evidence_policy(self):
        roles = {
            "assessor": ("applicable phase practices", "native fallbacks", "evidence needed"),
            "lead": ("ce-plan", "ce-work", "ce-code-review", "Verify the integrated"),
            "worker": ("Codebase Memory", "Context7", "TDD", "verify your result"),
            "consultant": ("ce-code-review", "code-review", "review independently"),
        }
        for role, phrases in roles.items():
            agents = list((PLUGIN / "agents").glob(f"symphony-{role}-*.md"))
            self.assertTrue(agents, role)
            for path in agents:
                body = path.read_text(encoding="utf-8")
                for phrase in (*phrases, "advertised and callable", "native fallback", "artifact or fresh command/result", "incomplete"):
                    self.assertIn(phrase, body, path.name)
                self.assertNotIn("1% chance", body, path.name)

        root = (PLUGIN / "skills/symphony/SKILL.md").read_text(encoding="utf-8")
        for phrase in ("brainstorming", "native practice", "acceptance evidence", "fresh verification"):
            self.assertIn(phrase, root)
        result = subprocess.run(
            [sys.executable, str(PLUGIN / "scripts/generate_agents.py"), "--check"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
