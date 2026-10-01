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
        project, _ = projects(root, False)
        result = subprocess.run([shutil.which("codex"), "exec", "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--model", "gpt-6-luna",
            "-C", str(project), "$symphony:symphony help"], cwd=project, env=env,
            capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("installed standard-user Codex help command failed")
        match = re.search(r'^session id: ([0-9a-f-]+)$', result.stderr, re.MULTILINE)
        if not match:
            raise RuntimeError("installed standard-user native session is unavailable")
        session = match.group(1)
        document = json.loads(state_file(root / "state", project).read_text())
        heartbeat = document["activation"]["codex"]
        if heartbeat.get("state") != "guarded" or heartbeat.get("session_id") != session:
            raise RuntimeError("installed standard-user heartbeat did not match this native session")
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
