#!/usr/bin/env python3
"""Prove the installed Codex checker works without PATH under a standard user."""

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from native_managed_concurrency import prepare_baseline_capture, projects, state_file


def error_categories(text):
    """Publish fixed categories only; captured native stderr stays private."""
    patterns = {
        'invalid_handle': r'the handle is invalid|invalid handle',
        'powershell_parse': r'ParserError|unexpected token|MissingEndParenthesis',
        'python_unavailable': r'no working Python|No Python executable found|python.*not recognized',
        'python_probe_timeout': r'probe timed out',
        'invalid_executable': r'not a valid.*application|Win32Exception',
        'digest_mismatch': r'digest.*mismatch|tamper|integrity',
        'file_missing': r'FileNotFoundError|cannot find.*path|No such file',
        'permission': r'PermissionError|Access.*denied|UnauthorizedAccess',
        'runtime_exception': r'Traceback|RuntimeError|Exception',
    }
    return {name: bool(re.search(pattern, text, re.IGNORECASE)) for name, pattern in patterns.items()}


def instrument_installed_hooks(root, home, version):
    """Only this disposable installation gets stderr grouping and private payloads."""
    plugin = home / 'plugins/cache/symphony-baseline/symphony' / version
    manifest = plugin / 'hooks/codex.json'
    document = json.loads(manifest.read_text())
    diagnostics = root / 'private-native-hook-diagnostics'
    diagnostics.mkdir()
    originals = {}
    for event, groups in document['hooks'].items():
        for index, handler in enumerate(hook for group in groups for hook in group['hooks']):
            original = handler.get('commandWindows')
            if not original:
                continue
            filename = re.sub(r'[^A-Za-z0-9_-]', '_', event) + '-' + str(index) + '.stderr'
            wrapped = '(' + original + ') 2>>"' + str(diagnostics / filename) + '"'
            if len(wrapped) >= 8170:
                raise RuntimeError('private stderr wrapper exceeds native Windows command limit')
            handler['commandWindows'] = wrapped
            originals[(event, index)] = original
    manifest.write_text(json.dumps(document))
    return plugin, originals, diagnostics


def prepare_private_native_clock(root, home):
    """Observe original native payloads without changing installed commands."""
    capture = root / 'capture_codex_hook.py'
    source = capture.read_text()
    marker = 'event = sys.argv[1]'
    if source.count(marker) != 1:
        raise RuntimeError('private native payload capture insertion is ambiguous')
    source = source.replace(marker, marker + "\n(destination / ('private-payload-' + event + '-' + invocation + '.json')).write_text(json.dumps(payload))")
    capture.write_text(source)
    clock_manifest = home / 'hooks.json'
    clock = json.loads(clock_manifest.read_text())
    for groups in clock['hooks'].values():
        for handler in (hook for group in groups for hook in group['hooks']):
            command = handler.get('command_windows', '')
            if not command.startswith('python.exe -I '):
                raise RuntimeError('native observation clock command is unexpected')
            handler['command_windows'] = '"' + sys.executable + '" -I ' + command.removeprefix('python.exe -I ')
    clock_manifest.write_text(json.dumps(clock))


def activation_failure_diagnostics(root, project, env, plugin, originals, diagnostics, result, instrumented=None, instrumented_error=None):
    evidence = {'native_cli_exit': result.returncode, 'original_gate_failed': True,
                'instrumented_cli_exit': instrumented.returncode if instrumented else None, 'private_hook_stderr': [], 'original_command_replay': []}
    evidence['instrumented_launch_failed'] = isinstance(instrumented_error, OSError)
    evidence['instrumented_timed_out'] = isinstance(instrumented_error, subprocess.TimeoutExpired)
    for path in diagnostics.glob('*.stderr'):
        text = path.read_bytes()[:65536].decode('utf-8', errors='replace')
        evidence['private_hook_stderr'].append({'event': path.stem.split('-')[0]
            if path.stem.split('-')[0] in {'SessionStart', 'UserPromptSubmit', 'Stop', 'Interrupt'} else 'other',
            'captured': True, 'nonempty': bool(text), 'categories': error_categories(text)})
    payloads = list((root / 'codex-hook-capture').glob('private-payload-SessionStart-*.json'))
    session = re.search(r'^session id: ([0-9a-f-]+)[ \t]*\r?$', result.stderr, re.MULTILINE)
    if session:
        payloads = [path for path in payloads if json.loads(path.read_text()).get('session_id') == session[1]]
    else:
        payloads = []
    original = originals.get(('SessionStart', 0))
    if len(payloads) == 1 and original:
        payload = payloads[0].read_text()
        # Rust's native command runner uses COMSPEC /C with raw outer quotes.
        # Compare its CREATE_NO_WINDOW flag using the original installed command.
        command = subprocess.list2cmdline([env.get('COMSPEC', os.environ.get('COMSPEC', 'cmd.exe'))]) + ' /C "' + original + '"'
        replay_env = {**env, 'PLUGIN_ROOT': str(plugin)}
        for label, flags in (('normal', 0), ('no-window', 0x08000000)):
            try:
                replay = subprocess.run(command, cwd=project, env=replay_env, input=payload,
                    capture_output=True, text=True, timeout=30, creationflags=flags)
                evidence['original_command_replay'].append({'creation_mode': label, 'exit_code': replay.returncode,
                    'categories': error_categories(replay.stderr), 'stdout_nonempty': bool(replay.stdout)})
            except (OSError, subprocess.TimeoutExpired) as error:
                evidence['original_command_replay'].append({'creation_mode': label,
                    'launch_failed': isinstance(error, OSError), 'timed_out': isinstance(error, subprocess.TimeoutExpired),
                    'categories': error_categories(str(error))})
    evidence['native_payload_available'] = len(payloads) == 1
    print(json.dumps({'windows_native_activation_failure': evidence}), flush=True)


def diagnose_original_failure(root, project, env, version, command, original):
    plugin, commands, diagnostics = instrument_installed_hooks(root, Path(env['CODEX_HOME']), version)
    instrumented = error = None
    try:
        instrumented = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exception:
        error = exception
    activation_failure_diagnostics(root, project, env, plugin, commands, diagnostics, original, instrumented,
                                   instrumented_error=error)


def original_native_activation(root, project, env, version, command):
    """Only an original unwrapped native heartbeat may pass the activation gate."""
    result = subprocess.run(command, cwd=project, env=env,
        capture_output=True, text=True, timeout=120)
    if result.returncode:
        diagnose_original_failure(root, project, env, version, command, result)
        raise RuntimeError("installed standard-user Codex help command failed")
    match = re.search(r'^session id: ([0-9a-f-]+)$', result.stderr, re.MULTILINE)
    if not match:
        diagnose_original_failure(root, project, env, version, command, result)
        raise RuntimeError("installed standard-user native session is unavailable")
    session = match.group(1)
    state = state_file(root / 'state', project)
    if not state.is_file():
        diagnose_original_failure(root, project, env, version, command, result)
        raise RuntimeError('installed native Codex produced no Symphony state; see fixed diagnostic categories')
    document = json.loads(state.read_text())
    heartbeat = document.get('activation', {}).get('codex', {})
    if heartbeat.get("state") != "guarded" or heartbeat.get("session_id") != session:
        diagnose_original_failure(root, project, env, version, command, result)
        raise RuntimeError("installed standard-user heartbeat did not match this native session")
    return result, session, heartbeat


def main():
    if os.name != "nt" or ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("native activation proof must run as a Windows standard user")
    candidate = Path(__file__).resolve().parents[2] / "plugins" / "symphony"
    with tempfile.TemporaryDirectory(prefix="symphony-native-checker-") as scratch:
        root = Path(scratch)
        env = {key: value for key, value in os.environ.items() if not key.startswith("SYMPHONY_")}
        env.update(prepare_baseline_capture("codex", root, candidate))
        env.update(SYMPHONY_STATE_DIR=str(root / "state"), SYMPHONY_RUNTIME_DIR=str(root / "runtimes"),
                   SYMPHONY_HOOK_DECISIONS_DIR=str(root / "decisions"), SYMPHONY_PROFILE="latest")
        version = json.loads((candidate / '.codex-plugin/plugin.json').read_text())['version']
        prepare_private_native_clock(root, Path(env['CODEX_HOME']))
        project, _ = projects(root, False)
        command = [shutil.which("codex"), "exec", "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--model", "gpt-6-luna",
            "-C", str(project), "$symphony:symphony help"]
        result, session, heartbeat = original_native_activation(root, project, env, version, command)
        # The actual native hook's returned context is captured in its transcript.
        rows = [json.loads(line) for path in (Path(env["CODEX_HOME"]) / "sessions").rglob(f"*{session}.jsonl")
                for line in path.read_text().splitlines()]
        texts = [block.get("text", "") for row in rows for block in row.get("payload", {}).get("content", [])
                 if isinstance(block, dict)]
        commands = []
        for text in texts:
            for line in text.splitlines():
                marker = "Check activation through the verified launcher: "
                if marker in line:
                    commands.append(line.split(marker, 1)[1].strip())
        if not commands or len(set(commands)) != 1:
            raise RuntimeError("actual installed native hook did not supply one checker command")
        command = commands[0]
        powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        env["CODEX_SESSION_ID"] = session
        aliases = root / "WindowsApps-like alias"
        aliases.mkdir()
        (aliases / "python.exe").write_bytes(b"not a Windows executable")
        evidence = []
        for label, path in (("missing-path", ""), ("broken-first-alias", str(aliases))):
            env["PATH"] = path
            old = "python.exe -I '" + str(Path(heartbeat["plugin_root"]) / "scripts/check_activation.py").replace("'", "''") + "'"
            failed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", old],
                cwd=project, env=env, capture_output=True, text=True, timeout=20)
            if failed.returncode == 0:
                raise RuntimeError("old bare-python checker did not reproduce the activation failure")
            checked = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                cwd=project, env=env, capture_output=True, text=True, timeout=20)
            if checked.returncode or "guarded: matching current-session heartbeat" not in checked.stdout:
                raise RuntimeError("hook-supplied absolute checker failed after native activation")
            evidence.append({"environment": label, "old_checker_exit": failed.returncode,
                "old_launch_error": failed.stderr.strip().splitlines()[0][:200] if failed.stderr.strip() else "native launch failed",
                "new_checker_exit": checked.returncode, "native_heartbeat": "guarded", "standard_user": True})
        env["CODEX_SESSION_ID"] = "foreign-session"
        rejected = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=project, env=env, capture_output=True, text=True, timeout=20)
        if rejected.returncode == 0:
            raise RuntimeError("absolute interpreter bypassed the original session check")
        print(json.dumps({"provider": "codex", "windows_native_activation": evidence,
                          "foreign_session_rejected": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
