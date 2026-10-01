#!/usr/bin/env python3
"""Run the existing installed Claude start control; export fixed failure facts."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def command(executable):
    return [executable, '--print', '--model', 'haiku', '--max-budget-usd', '0.50',
            '--output-format', 'text', '/symphony:start CI routing probe']


def error_categories(text):
    patterns = {
        'budget_exceeded': r'max(?:imum)? budget|budget (?:exceeded|limit)|error_max_budget_usd',
        'authentication': r'authentication|unauthorized|not logged in|invalid api key|login required',
        'model_unavailable': r'model.*(?:unavailable|not found|not supported)|invalid model',
        'hook_failure': r'hook.*(?:failed|error)|ParserError|no working Python',
        'permission': r'permission denied|access.*denied|UnauthorizedAccess',
        'runtime_exception': r'Traceback|RuntimeError|Exception',
    }
    return {name: bool(re.search(pattern, text, re.IGNORECASE)) for name, pattern in patterns.items()}


def run_probe(executable, private_dir, timeout):
    private_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        private_dir.chmod(0o700)
    stdout = private_dir / 'claude-start.stdout'
    stderr = private_dir / 'claude-start.stderr'
    timed_out = launch_failed = False
    exit_code = None
    with stdout.open('wb') as out, stderr.open('wb') as err:
        try:
            # Same argv, inherited login/environment and cwd as the old command.
            process = subprocess.Popen(command(executable), stdout=out, stderr=err)
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # A Windows .cmd shim may have a live Node child after killing
                # only the shim. Terminate this disposable invocation's tree.
                if os.name == 'nt':
                    try:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=out, stderr=err, timeout=15, check=False)
                    except (OSError, subprocess.SubprocessError):
                        launch_failed = True
                process.kill()
                process.wait(timeout=15)
        except (OSError, subprocess.SubprocessError):
            launch_failed = True
    # Binary captures avoid the runner's cp1252 decoder. Replacement is only
    # diagnostic: these bytes never serve as native lifecycle evidence.
    text = ''
    for path in (stdout, stderr):
        with path.open('rb') as stream:
            text += stream.read(1024 * 1024).decode('utf-8', errors='replace') + '\n'
    return {'exit_code': exit_code, 'timed_out': timed_out, 'launch_failed': launch_failed,
            'stdout_nonempty': stdout.stat().st_size > 0,
            'stderr_nonempty': stderr.stat().st_size > 0,
            'categories': error_categories(text)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error('timeout must be between 1 and 600 seconds')
    scratch = Path(os.environ['RUNNER_TEMP'])
    executable = shutil.which('claude.cmd' if os.name == 'nt' else 'claude')
    receipt = run_probe(executable or 'claude.cmd', scratch / 'symphony-private-start-control', args.timeout)
    diagnostics = scratch / 'symphony-native-diagnostics'
    diagnostics.mkdir(exist_ok=True)
    (diagnostics / 'claude-windows-start.json').write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt['exit_code'] == 0 and not receipt['timed_out'] and not receipt['launch_failed'] else 1


if __name__ == '__main__':
    sys.exit(main())
