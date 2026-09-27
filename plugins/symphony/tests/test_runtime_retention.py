"""Captured commands must survive removal of their reviewed cache version."""

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from plugins.symphony.scripts.generate_hooks import bootstrap, generated
from plugins.symphony.scripts.package_smoke import _payload


PLUGIN = Path(__file__).resolve().parents[1]


class RuntimeRetentionTests(unittest.TestCase):
    def materialize(self, directory, name):
        root = directory / name
        shutil.copytree(PLUGIN, root)
        for path, content in generated(root).items():
            path.write_text(content)
        return root

    def command(self, root, provider, event):
        filename = "codex.json" if provider == "codex" else "hooks.json"
        handler = json.loads((root / "hooks" / filename).read_text())["hooks"][event][0]["hooks"][0]
        if os.name == "nt" and provider == "codex":
            return f'cmd.exe /C "{handler["commandWindows"]}"'
        bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
        return [bash, "-c", handler["command"]]

    def run_hook(self, command, root, provider, directory, payload):
        env = {**os.environ, "PLUGIN_ROOT": str(root), "CLAUDE_PLUGIN_ROOT": str(root),
               "SYMPHONY_RUNTIME_DIR": str(directory / "retained runtimes"),
               "SYMPHONY_STATE_DIR": str(directory / "state"), "SYMPHONY_PROFILE": "full",
               "PYTHONPATH": str(directory),
               "CODEX_HOME": str(directory / "empty codex home")}
        if os.name == "nt":
            # The VM invokes Python by absolute path and does not install it
            # globally; provide that same interpreter to the captured hook.
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                text=True, env=env, cwd=directory, timeout=20, check=False)
        return result, env

    def test_captured_hooks_and_checker_survive_removed_cache_without_losing_outcome(self):
        self.exercise_removed_cache()

    def exercise_removed_cache(self, providers=("codex", "claude")):
        for provider in providers:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "base64.py").write_text("raise RuntimeError('untrusted project import')\n")
                root = self.materialize(directory, "Reviewed Old Plugin With Spaces")
                commands = {event: self.command(root, provider, event) for event in
                            ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop")}
                checker_code = bootstrap(root)
                payloads = {}
                for event, role in (("SessionStart", "assessor"), ("UserPromptSubmit", "assessor"),
                                    ("SubagentStart", "assessor"), ("SubagentStop", "assessor"),
                                    ("SubagentStart", "lead")):
                    payload = _payload(root, provider, event, directory, "old-session", role)
                    result, env = self.run_hook(commands[event], root, provider, directory, payload)
                    self.assertEqual(result.returncode, 0, result.stderr)
                # Prepare captured payloads before removing all the old files.
                for event in ("SubagentStop", "UserPromptSubmit", "Stop"):
                    payloads[event] = _payload(root, provider, event, directory, "old-session", "lead")
                payloads["UserPromptSubmit"]["prompt"] = "$symphony:symphony status" if provider == "codex" else "/symphony:status"
                state_file = next((directory / "state").glob("*.v2.json"))
                document = json.loads(state_file.read_text())
                self.assertIn(f"{provider}:old-session", document["active_runs"])
                retained = Path(document["activation"][provider]["runtime_root"])
                # A newer session uses its separately reviewed package; the old
                # captured command must never resolve to this version.
                new_root = self.materialize(directory, "Reviewed New Plugin With Spaces")
                init = new_root / "symphony" / "__init__.py"
                old_version = json.loads((root / ".codex-plugin/plugin.json").read_text())["version"]
                init.write_text(init.read_text().replace(old_version, "99.0.0"))
                for path, content in generated(new_root).items():
                    path.write_text(content)
                result, _ = self.run_hook(self.command(new_root, provider, "SessionStart"), new_root,
                                          provider, directory, {"hook_event_name": "SessionStart",
                                                                "session_id": "new-session", "cwd": str(directory)})
                self.assertEqual(result.returncode, 0, result.stderr)
                shutil.rmtree(root)
                for event in ("SubagentStop", "UserPromptSubmit"):
                    result, env = self.run_hook(commands[event], root, provider, directory, payloads[event])
                    self.assertEqual(result.returncode, 0, result.stderr)
                document = json.loads(state_file.read_text())
                self.assertEqual(document["activation"][provider]["plugin_version"], old_version)
                self.assertEqual(document["activation"][provider]["runtime_root"], str(retained))
                outcome_run = document["active_runs"].get(f"{provider}:old-session") or document["recent_runs"][-1]
                self.assertIsNotNone(outcome_run["outcome"])
                if provider == "codex":
                    checked = subprocess.run([sys.executable, "-I", "-c", checker_code,
                                              str(root), "codex", "--check-activation"], cwd=directory,
                                             env={**env, "CODEX_SESSION_ID": "old-session"}, capture_output=True,
                                             text=True, check=False)
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr + json.dumps({
                        "expected_source": str(root.resolve()), "expected_runtime": str(retained.resolve()),
                        "activation": document["activation"][provider]}, sort_keys=True))
                result, _ = self.run_hook(commands["Stop"], root, provider, directory, payloads["Stop"])
                self.assertEqual(result.returncode, 0, result.stderr)
                document = json.loads(state_file.read_text())
                self.assertNotIn(f"{provider}:old-session", document["active_runs"])
                self.assertEqual(document["recent_runs"][-1]["status"], "completed")

    def test_retained_or_source_tampering_fails_closed(self):
        for changed_path in ("symphony/runtime.py", "json.py", "scripts/check_activation.py"):
            with self.subTest(changed_path=changed_path), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                root = self.materialize(directory, "Reviewed Plugin")
                command = self.command(root, "codex", "SessionStart")
                checker_code = bootstrap(root)
                payload = {"hook_event_name": "SessionStart", "session_id": "trusted", "cwd": str(directory)}
                result, env = self.run_hook(command, root, "codex", directory, payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                retained = next((directory / "retained runtimes").iterdir())
                changed = retained / changed_path
                changed.write_text("raise RuntimeError('must never execute')\n")
                before = next((directory / "state").glob("*.v2.json")).read_bytes()
                result, _ = self.run_hook(command, root, "codex", directory, payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing to execute", result.stderr)
                self.assertEqual(next((directory / "state").glob("*.v2.json")).read_bytes(), before)
                checked = subprocess.run([sys.executable, "-I", "-c", checker_code, str(root),
                                          "codex", "--check-activation"], cwd=directory,
                                         env={**env, "CODEX_SESSION_ID": "trusted"},
                                         capture_output=True, text=True, check=False)
                self.assertNotEqual(checked.returncode, 0)
                self.assertIn("refusing to execute", checked.stderr)

    def test_removed_unpinned_package_never_executes_a_newest_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.materialize(directory, "1.0.0")
            command = self.command(root, "codex", "SessionStart")
            self.materialize(directory, "99.0.0")
            shutil.rmtree(root)
            result, _ = self.run_hook(command, root, "codex", directory,
                                     {"hook_event_name": "SessionStart", "session_id": "gone", "cwd": str(directory)})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing or changed", result.stderr)
            self.assertFalse((directory / "state").exists())

    def test_first_pin_is_atomic_for_parallel_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.materialize(directory, "Reviewed Parallel Plugin")
            command = self.command(root, "codex", "SessionStart")
            def launch(index):
                return self.run_hook(command, root, "codex", directory,
                                     {"hook_event_name": "SessionStart", "session_id": f"parallel-{index}", "cwd": str(directory)})[0]
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(launch, range(4)))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list((directory / "retained runtimes").iterdir())), 1)
            document = json.loads(next((directory / "state").glob("*.v2.json")).read_text())
            sessions = {event["payload"].get("session_id") for event in document["event_history"]}
            self.assertTrue({f"parallel-{index}" for index in range(4)} <= sessions)
