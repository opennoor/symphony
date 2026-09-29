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
        if (directory / "pin barrier" / "barrier.py").is_file():
            env["SYMPHONY_TEST_PIN_BARRIER"] = str(directory / "pin barrier" / "barrier.py")
        if os.name == "nt":
            # The VM invokes Python by absolute path and does not install it
            # globally; provide that same interpreter to the captured hook.
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                text=True, env=env, cwd=directory, timeout=20, check=False)
        return result, env

    def test_captured_hooks_and_checker_survive_removed_cache_without_losing_outcome(self):
        self.exercise_removed_cache()

    def test_new_checker_accepts_only_verified_old_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.materialize(directory, "Reviewed Old Plugin With Spaces")
            for event, role in (("SessionStart", "assessor"),
                                ("UserPromptSubmit", "assessor"),
                                ("SubagentStart", "assessor")):
                payload = _payload(root, "codex", event, directory, "old-session", role)
                result, env = self.run_hook(self.command(root, "codex", event), root,
                                            "codex", directory, payload)
                self.assertEqual(result.returncode, 0, result.stderr)
            state_file = next((directory / "state").glob("*.v2.json"))
            document = json.loads(state_file.read_text())
            old_heartbeat = document["activation"]["codex"].copy()
            old_schema = old_heartbeat["hook_schema_version"]
            self.assertIn("codex:old-session", document["active_runs"])

            new_root = self.materialize(directory, "Reviewed New Plugin With Spaces")
            init = new_root / "symphony" / "__init__.py"
            old_version = json.loads((root / ".codex-plugin/plugin.json").read_text())["version"]
            init.write_text(init.read_text().replace(old_version, "99.0.0"))
            for path, content in generated(new_root).items():
                path.write_text(content)
            result, _ = self.run_hook(self.command(new_root, "codex", "SessionStart"),
                                      new_root, "codex", directory,
                                      {"hook_event_name": "SessionStart", "session_id": "new-session",
                                       "cwd": str(directory)})
            self.assertEqual(result.returncode, 0, result.stderr)
            shutil.rmtree(root)

            def check(session="old-session"):
                return subprocess.run([sys.executable, "-I", str(new_root / "scripts/check_activation.py")],
                                      cwd=directory, env={**env, "CODEX_SESSION_ID": session},
                                      capture_output=True, text=True, check=False)

            self.assertEqual(check().returncode, 0, check().stdout)
            self.assertEqual(check("new-session").returncode, 0)
            self.assertNotEqual(check("unknown-session").returncode, 0)

            document = json.loads(state_file.read_text())
            latest = document["activation"]["codex"]
            alias = str(new_root / "symphony" / "..")
            latest["plugin_root"] = alias.swapcase() if os.name == "nt" else alias
            latest["session_profiles"] = []
            document["event_history"] = []
            state_file.write_text(json.dumps(document))
            self.assertEqual(check("new-session").returncode, 0)

            document = json.loads(state_file.read_text())
            document["activation"]["codex"]["session_profiles"] = [old_heartbeat]
            document["activation"]["codex"]["session_profiles"][0]["hook_schema_version"] = -1
            state_file.write_text(json.dumps(document))
            self.assertNotEqual(check().returncode, 0)

            # The same reviewed session can begin another run after this one ends.
            document["activation"]["codex"]["session_profiles"][0]["hook_schema_version"] = old_schema
            document["active_runs"].pop("codex:old-session")
            state_file.write_text(json.dumps(document))
            self.assertEqual(check().returncode, 0)
            retained = Path(old_heartbeat["runtime_root"])
            (retained / "symphony" / "runtime.py").write_text("raise RuntimeError('tampered')\n")
            self.assertNotEqual(check().returncode, 0)

    def test_two_active_sessions_finish_from_old_runtime_after_update(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                root = self.materialize(directory, "Old Reviewed Plugin With Spaces")
                commands = {event: self.command(root, provider, event) for event in
                            ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop")}
                for session in ("old-a", "old-b"):
                    for event, role in (("SessionStart", "assessor"),
                                        ("UserPromptSubmit", "assessor"),
                                        ("SubagentStart", "assessor"),
                                        ("SubagentStop", "assessor"),
                                        ("SubagentStart", "lead")):
                        payload = _payload(root, provider, event, directory, session, role)
                        result, env = self.run_hook(commands[event], root, provider, directory, payload)
                        self.assertEqual(0, result.returncode, result.stderr)
                state_file = next((directory / "state").glob("*.v2.json"))
                document = json.loads(state_file.read_text())
                self.assertEqual({f"{provider}:old-a", f"{provider}:old-b"},
                                 set(document["active_runs"]))

                new_root = self.materialize(directory, "New Reviewed Plugin With Spaces")
                init = new_root / "symphony" / "__init__.py"
                old_version = json.loads((root / ".codex-plugin/plugin.json").read_text())["version"]
                init.write_text(init.read_text().replace(old_version, "99.0.0"))
                for path, content in generated(new_root).items():
                    path.write_text(content)
                result, _ = self.run_hook(self.command(new_root, provider, "SessionStart"),
                                          new_root, provider, directory,
                                          _payload(new_root, provider, "SessionStart", directory, "new-session"))
                self.assertEqual(0, result.returncode, result.stderr)
                shutil.rmtree(root)

                for session in ("old-a", "old-b"):
                    if provider == "codex":
                        checked = subprocess.run(
                            [sys.executable, "-I", str(new_root / "scripts/check_activation.py")],
                            cwd=directory, env={**env, "CODEX_SESSION_ID": session},
                            capture_output=True, text=True, check=False)
                        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
                    payload = _payload(new_root, provider, "SubagentStop", directory, session, "lead")
                    payload["last_assistant_message"] = 'SYMPHONY_OUTCOME: {"status":"completed"}'
                    result, _ = self.run_hook(commands["SubagentStop"], root, provider, directory, payload)
                    self.assertEqual(0, result.returncode, result.stderr)
                    result, _ = self.run_hook(commands["Stop"], root, provider, directory,
                                              _payload(new_root, provider, "Stop", directory, session))
                    self.assertEqual(0, result.returncode, result.stderr)
                document = json.loads(state_file.read_text())
                self.assertFalse(document["active_runs"])
                recent = {run["session_id"]: run for run in document["recent_runs"]
                          if run["session_id"] in {"old-a", "old-b"}}
                self.assertEqual({"old-a", "old-b"}, set(recent))
                self.assertTrue(all(run["status"] == "completed" and
                                    run["outcome"] == {"status": "completed"} for run in recent.values()))

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
                if provider == "codex":
                    checked = subprocess.run([sys.executable, "-I", str(new_root / "scripts/check_activation.py")],
                                             cwd=directory, env={**env, "CODEX_SESSION_ID": "old-session"},
                                             capture_output=True, text=True, check=False)
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
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
        for round_index in range(int(os.environ.get("SYMPHONY_FIRST_PIN_ROUNDS", "1"))):
            self.exercise_first_pin_round(round_index)

    def exercise_first_pin_round(self, round_index):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = self.materialize(directory, "Reviewed Parallel Plugin")
            gate = directory / "pin barrier"
            gate.mkdir()
            (gate / "barrier.py").write_text("""import time
gate = Path(os.environ["SYMPHONY_TEST_PIN_BARRIER"]).parent
(gate / ("ready-" + str(os.getpid()))).touch()
deadline = time.monotonic() + 20
while len(list(gate.glob("ready-*"))) < 4:
    if time.monotonic() > deadline:
        raise RuntimeError("first-pin barrier timed out")
    time.sleep(0.005)
""")
            bootstrap_path = root / "scripts" / "bootstrap_runtime.py"
            source = bootstrap_path.read_text()
            needle = "    if not verified(target, snapshot=True):"
            self.assertIn(needle, source)
            bootstrap_path.write_text(source.replace(
                needle, '    exec(Path(os.environ["SYMPHONY_TEST_PIN_BARRIER"]).read_text())\n' + needle, 1))
            for path, content in generated(root).items():
                path.write_text(content)
            command = self.command(root, "codex", "SessionStart")
            def launch(index):
                return self.run_hook(command, root, "codex", directory,
                                     {"hook_event_name": "SessionStart", "session_id": f"parallel-{index}", "cwd": str(directory)})[0]
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(launch, range(4)))
            state_files = sorted((directory / "state").glob("*.v2.json"))
            session_sets = {
                path.name: sorted({str(event["payload"].get("session_id")) for event in
                                   json.loads(path.read_text())["event_history"]})
                for path in state_files
            }
            runtime_dir = directory / "retained runtimes"
            retained = sorted(runtime_dir.iterdir()) if runtime_dir.exists() else []
            expected = {f"parallel-{index}" for index in range(4)}
            observed = set().union(*(set(items) for items in session_sets.values()))
            if (any(result.returncode for result in results) or len(retained) != 1
                    or len(state_files) != 1 or not expected <= observed
                    or len(list(gate.glob("ready-*"))) != 4):
                def output(result):
                    # Captured hook context embeds a long launcher; keep the
                    # signal and omit that command from CI diagnostics.
                    stdout = result.stdout.split("Check activation through the verified launcher:", 1)[0]
                    return {"returncode": result.returncode, "stdout": stdout[:400],
                            "stderr": result.stderr[:400]}

                snapshots = {path.name: sorted(item.relative_to(path).as_posix()
                                                for item in path.rglob("*") if item.is_file())
                             for path in retained}
                self.fail(json.dumps({"round": round_index, "hooks": [output(item) for item in results],
                                      "state_files": session_sets,
                                      "retained_files": snapshots,
                                      "barrier_arrivals": len(list(gate.glob("ready-*"))),
                                      "missing_sessions": sorted(expected - observed)}, sort_keys=True))
