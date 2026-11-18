"""Orchestrator mode on its three hosts: the Codex and Claude Code hook, the
Hermes plugin, and the launcher's place in the pinned runtime."""

from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from plugins.symphony.scripts import generate_hermes
from plugins.symphony.scripts.generate_hooks import bootstrap, generated, pinned
from plugins.symphony.symphony import PLUGIN_VERSION
from plugins.symphony.symphony import orchestrator as orch
from plugins.symphony.symphony.model import RunState
from plugins.symphony.symphony.routing import profiles_for
from plugins.symphony.symphony.runtime import CONTROLS, _parse_control, handle
from plugins.symphony.symphony.store import StateStore


PLUGIN = Path(__file__).resolve().parents[1]
HERMES = PLUGIN.parent / "symphony-hermes"


def load_hermes_package():
    """Import the Hermes package the way Hermes does: a directory with relative imports."""
    name = "symphony_hermes_under_test"
    for loaded in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
        del sys.modules[loaded]
    spec = importlib.util.spec_from_file_location(name, HERMES / "__init__.py",
                                                  submodule_search_locations=[str(HERMES)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class HookControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def environ(self, provider):
        return {"SYMPHONY_STATE_DIR": str(self.root / f"state-{provider}"), "SYMPHONY_PROVIDER": provider,
                "SYMPHONY_PROFILE": profiles_for(provider)[0]["id"], "PATH": os.environ.get("PATH", "")}

    def prompt(self, provider, text, environ=None):
        payload = {"session_id": "root", "cwd": str(self.project), "hook_event_name": "UserPromptSubmit",
                   "prompt": text}
        if provider == "codex":
            payload.update({"turn_id": "turn", "model": "host-model"})
        result = handle(payload, environ or self.environ(provider))
        return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] if result.stdout else ""

    def control(self, provider, argument=""):
        prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
        return self.prompt(provider, (prefix + "orchestrate " + argument).strip())

    def state(self, provider):
        return StateStore(self.root / f"state-{provider}").load(self.project)

    def test_orchestrate_is_a_control_on_both_hosts(self):
        self.assertIn("orchestrate", CONTROLS)
        self.assertEqual(_parse_control("$symphony:symphony orchestrate on"), ("orchestrate", "on"))
        self.assertEqual(_parse_control("/symphony:orchestrate Build it\nwith checks"),
                         ("orchestrate", "Build it\nwith checks"))
        self.assertEqual(_parse_control("SYMPHONY_CONTROL: orchestrate\nARGUMENTS: off"), ("orchestrate", "off"))
        command = (PLUGIN / "commands" / "orchestrate.md").read_text()
        self.assertTrue(command.endswith("SYMPHONY_CONTROL: orchestrate\nARGUMENTS: $ARGUMENTS\n"))
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                self.assertIn("orchestrate [on|off|<task>]", self.prompt(provider, prefix + "help"))

    def test_the_switch_is_kept_per_project_and_opens_no_task(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.assertIn("orchestrator mode is off", self.control(provider))
                on = self.control(provider, "on")
                self.assertIn("orchestrator mode is on", on)
                self.assertIn("creates no managed task", on)
                self.assertIn("Symphony is not enabled here", on)
                self.assertEqual(self.state(provider).configuration["orchestrator"], {"enabled": True})
                self.assertIsNone(self.state(provider).active_run)
                self.assertIn("orchestrator mode is on", self.control(provider, "status"))
                prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                self.assertIn("Orchestrator mode: on", self.prompt(provider, prefix + "status"))
                self.assertIn("orchestrator mode is off", self.control(provider, "off"))
                self.assertNotIn("orchestrator", self.state(provider).configuration)
                self.assertNotIn("Orchestrator mode", self.prompt(provider, prefix + "status"))

    def test_tasks_go_to_the_launcher_while_the_mode_is_on(self):
        launcher = str(orch.launcher_script())
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                native = self.prompt(provider, prefix + "start Build it")
                self.assertIn("SYMPHONY_ROLE: assessor", native)
                self.assertNotIn(launcher, native)
                self.control(provider, "on")
                self.assertEqual(self.prompt(provider, "Build it"), "")  # the project is not enabled
                started = self.prompt(provider, prefix + "start Build it\nand verify")
                self.assertIn(f'"{launcher}" assess --task-file', started)
                self.assertNotIn("SYMPHONY_ROLE: assessor", started)
                self.assertTrue(started.endswith("Build it\nand verify"))
                self.assertIn("Every task prompt in this project is orchestrated",
                              self.prompt(provider, prefix + "enable") + self.control(provider, "status"))
                self.assertIn(f'"{launcher}" assess --task-file', self.prompt(provider, "Fix the other thing"))
                self.assertIsNone(self.state(provider).active_run)

    def test_one_task_can_be_orchestrated_while_the_mode_is_off(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                text = self.control(provider, "Fix the bug")
                self.assertIn("assess --task-file", text)
                self.assertTrue(text.endswith("Fix the bug"))
                self.assertNotIn("orchestrator", self.state(provider).configuration)

    def test_a_native_run_in_progress_is_finished_natively(self):
        provider = "claude"
        self.control(provider, "on")
        run = RunState(run_id="run-1", task="earlier task", session_id="root", provider=provider,
                       owner_seen_at=datetime.now(timezone.utc).isoformat())
        StateStore(self.root / f"state-{provider}").update(self.project, lambda state: (
            replace(state, active_run=run, active_runs={**state.active_runs, f"{provider}:root": run}), ()))
        for text in (self.control(provider, "Another task"), self.prompt(provider, "/symphony:start Another task")):
            self.assertNotIn("assess --task-file", text)

    def test_an_orchestrated_session_is_never_routed_again(self):
        for provider in ("codex", "claude"):
            environ = {**self.environ(provider), "SYMPHONY_ORCHESTRATED": "1", "SYMPHONY_AGENT_SESSIONS": "1"}
            with self.subTest(provider=provider):
                for text in ("enable", "$symphony:symphony start Build it", "/symphony:orchestrate on"):
                    self.assertEqual(self.prompt(provider, text, environ), "")
                self.assertFalse((self.root / f"state-{provider}").exists())


class FakeHermesContext:
    def __init__(self):
        self.skills, self.commands, self.hooks = {}, {}, {}

    def register_skill(self, name, path):
        self.skills[name] = Path(path)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class HermesHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.saved = {key: os.environ.get(key) for key in
                      ("SYMPHONY_STATE_DIR", "SYMPHONY_ORCHESTRATOR_NO_PROBE", "CODEX_HOME")}
        os.environ.update({"SYMPHONY_STATE_DIR": str(self.root / "state"),
                           "SYMPHONY_ORCHESTRATOR_NO_PROBE": "1", "CODEX_HOME": str(self.root / "no-codex-home")})
        self.package = load_hermes_package()
        self.context = FakeHermesContext()
        self.package.register(self.context)
        from symphony_hermes_under_test.symphony import orchestrator
        self.launcher = orchestrator

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def command(self, text):
        return self.context.commands["symphony"][0](text, cwd=str(self.project))

    def guidance(self, session="s1"):
        return self.context.hooks["pre_llm_call"](
            session_id=session, user_message="Build it", conversation_history=[], is_first_turn=True,
            model="any", platform="cli", cwd=str(self.project))

    def test_the_manifest_matches_what_register_provides(self):
        manifest = (HERMES / "plugin.yaml").read_text()
        self.assertRegex(manifest, r"(?m)^name: symphony$")
        self.assertEqual(re.search(r'(?m)^version: "([^"]+)"$', manifest).group(1), PLUGIN_VERSION)
        self.assertEqual(re.findall(r"(?m)^  - (\w+)$", manifest), sorted(self.context.hooks))
        self.assertEqual(set(self.context.commands), {"symphony"})
        skill = self.context.skills["orchestrator"]
        self.assertEqual(skill, HERMES / "skills" / "orchestrator" / "SKILL.md")
        self.assertRegex(skill.read_text(), r"(?s)\A---\nname: orchestrator\ndescription: .+?\n---\n")

    def test_the_package_is_the_launcher_and_nothing_hermes_would_refuse(self):
        self.assertEqual(generate_hermes.stale(), [])
        shipped = sorted(path.relative_to(HERMES).as_posix() for path in HERMES.rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
        self.assertEqual(shipped, sorted({*generate_hermes.COPIED, *generate_hermes.OWN}))
        # Hermes scans what it installs and blocks an encoded command such as the
        # Codex Windows hook launcher. No hook launcher or hook runtime ships here.
        self.assertFalse((HERMES / "hooks").exists())
        for name in shipped:
            self.assertNotRegex((HERMES / name).read_text(encoding="utf-8"), r"(?i)base64[^\n]*env")
        # The Codex and Claude Code plugin stays free of Hermes files.
        self.assertEqual(sorted(path.parent.name for path in PLUGIN.glob("skills/*/SKILL.md")), ["symphony"])
        for name in ("plugin.yaml", "__init__.py", "hermes"):
            self.assertFalse((PLUGIN / name).exists())

    def test_a_changed_launcher_makes_the_package_stale(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "symphony-hermes"
            shutil.copytree(HERMES, target, ignore=shutil.ignore_patterns("__pycache__"))
            self.assertEqual(generate_hermes.stale(PLUGIN, target), [])
            (target / "symphony" / "orchestrator.py").write_text("# edited in place\n")
            (target / "symphony" / "runtime.py").write_text("# not part of the package\n")
            (target / "profiles.json").unlink()
            self.assertEqual(generate_hermes.stale(PLUGIN, target),
                             ["profiles.json", "symphony/orchestrator.py", "symphony/runtime.py"])
            generate_hermes.write(PLUGIN, target)
            self.assertEqual(generate_hermes.stale(PLUGIN, target), ["symphony/runtime.py"])

    @unittest.skipIf(os.name == "nt", "runs the packaged script through a POSIX interpreter path")
    def test_the_packaged_launcher_runs_on_its_own(self):
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "installed"
            shutil.copytree(HERMES, target, ignore=shutil.ignore_patterns("__pycache__"))
            environ = {"PATH": os.environ.get("PATH", ""), "SYMPHONY_STATE_DIR": str(Path(temporary) / "state"),
                       "SYMPHONY_ORCHESTRATOR_NO_PROBE": "1", "CODEX_HOME": str(Path(temporary) / "codex"),
                       "SYMPHONY_CODEX_CLI": "/fake/codex", "SYMPHONY_CLAUDE_CLI": "/fake/claude"}
            done = subprocess.run([sys.executable, "-I", str(target / "scripts" / "symphony_agent.py"),
                                   "select", "--role", "assessor", "--json"],
                                  capture_output=True, text=True, env=environ, cwd=temporary, timeout=60)
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual({item["provider"] for item in json.loads(done.stdout)["candidates"]},
                             {"codex", "claude"})

    def test_guidance_is_silent_until_the_mode_is_on_for_this_project(self):
        self.assertIsNone(self.guidance())
        self.assertIn("Symphony orchestrator mode: off", self.command("status"))
        on = self.command("on")
        self.assertIn("orchestrator mode is on for this project", on)
        self.assertTrue(self.launcher.hermes_mode(self.project, os.environ))
        # Hermes has its own switch; it does not touch the hook hosts' project state.
        self.assertEqual(list((self.root / "state").glob("*.json")), [])
        first = self.guidance()["context"]
        self.assertEqual(self.launcher.launcher_script(), HERMES / "scripts" / "symphony_agent.py")
        self.assertIn(f'"{self.launcher.launcher_script()}" assess --task-file', first)
        self.assertIn("lead --run <run>", first)
        again = self.guidance()["context"]
        self.assertLess(len(again), len(first))
        self.assertIn("symphony:orchestrator", again)
        self.assertEqual(self.guidance("another-session")["context"], first)
        other = self.root / "other-project"
        other.mkdir()
        self.assertIsNone(self.context.hooks["pre_llm_call"](session_id="s9", cwd=str(other)))
        self.assertEqual(self.command("off"), "Symphony orchestrator mode is off for this project.")
        self.assertIsNone(self.guidance())

    def test_the_command_answers_instead_of_raising(self):
        self.assertIn("/symphony on", self.command("unknown words"))
        self.assertIn("no orchestrated run found", self.command("agents"))
        self.assertIn("codex", self.command("catalog"))
        (self.root / "state").write_text("not a directory")
        self.assertIn("Symphony could not do that", self.command("on"))
        self.assertIsNone(self.guidance())


class PinnedRuntimeTests(unittest.TestCase):
    def test_the_launcher_is_part_of_the_pinned_runtime(self):
        names = pinned(PLUGIN)
        for required in ("scripts/symphony_agent.py", "scripts/symphony_hook.py", "symphony/orchestrator.py",
                         "symphony/orchestrator_cli.py", "symphony/runtime.py", "commands/orchestrate.md",
                         "skills/symphony/references/orchestrator-mode.md", "profiles.json", "model-policy.json"):
            self.assertIn(required, names)
        self.assertEqual(names, sorted(set(names)))
        for outside in ("scripts/generate_hooks.py", "scripts/generate_hermes.py"):
            self.assertNotIn(outside, names)

    def test_file_names_no_longer_grow_the_launchers(self):
        code = bootstrap(PLUGIN)
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            for index in range(40):
                (root / "agents" / f"symphony-worker-a-future-model-with-a-long-name-{index}-high.md").write_text("x")
            grown = bootstrap(root)
            # Only the digest differs; its compressed size moves by a few characters.
            self.assertLess(abs(len(grown) - len(code)), 32)
            self.assertNotEqual(grown, code)
            generated(root)  # would raise past either host's command limit

    @unittest.skipIf(os.name == "nt", "the POSIX launcher is exercised here; Windows has its own job")
    def test_a_file_added_or_changed_after_review_is_refused(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "plugin"
            shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            for path, content in generated(root).items():
                path.write_text(content)
            command = json.loads((root / "hooks" / "codex.json").read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"]

            def start(runtimes):
                environ = {**os.environ, "PLUGIN_ROOT": str(root), "SYMPHONY_RUNTIME_DIR": str(directory / runtimes),
                           "SYMPHONY_STATE_DIR": str(directory / "state"), "CODEX_HOME": str(directory / "codex"),
                           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
                payload = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(directory),
                           "turn_id": "t", "model": "m"}
                return subprocess.run([shutil.which("bash"), "-c", command], input=json.dumps(payload),
                                      capture_output=True, text=True, env=environ, cwd=directory, timeout=30)

            clean = start("runtimes-clean")
            self.assertEqual((clean.returncode, clean.stderr), (0, ""))
            snapshot = next((directory / "runtimes-clean").iterdir())
            kept = sorted(path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file())
            self.assertEqual(kept, pinned(root))

            (root / "symphony" / "added_after_review.py").write_text("raise SystemExit('unreviewed code ran')\n")
            added = start("runtimes-added")
            self.assertEqual(added.returncode, 1)
            self.assertIn("missing or changed", added.stderr)
            # The snapshot retained before the change still runs, as it did before.
            self.assertEqual(start("runtimes-clean").returncode, 0)
            (root / "symphony" / "added_after_review.py").unlink()
            self.assertEqual(start("runtimes-added").returncode, 0)

            launcher = root / "scripts" / "symphony_agent.py"
            launcher.write_text(launcher.read_text() + "\n# edited\n")
            self.assertEqual(start("runtimes-edited").returncode, 1)


if __name__ == "__main__":
    unittest.main()
