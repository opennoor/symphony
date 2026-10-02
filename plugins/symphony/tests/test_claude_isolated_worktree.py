import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from plugins.symphony.symphony.model import Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import _prepare_delegation


SCRIPTS = Path(__file__).resolve().parents[3] / ".github" / "scripts"
spec = importlib.util.spec_from_file_location("native_managed_concurrency", SCRIPTS / "native_managed_concurrency.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
spec = importlib.util.spec_from_file_location("claude_isolated_worktree", SCRIPTS / "claude_isolated_worktree.py")
isolated = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"native_managed_concurrency": native}):
    spec.loader.exec_module(isolated)


class ClaudeIsolatedWorktreeTests(unittest.TestCase):
    def test_failure_snapshot_precedes_private_home_cleanup(self):
        roots = []

        def projects(case, worktree):
            primary = case / 'primary'
            primary.mkdir()
            return primary, primary

        def scratch(root, package):
            roots.append(root)
            home = root / 'claude-home'
            (home / 'projects').mkdir(parents=True)
            return {'CLAUDE_CONFIG_DIR': str(home)}, home

        def snapshot(root, provider):
            self.assertEqual(provider, 'claude')
            self.assertTrue((root / 'claude-home/projects').is_dir())
            return {'private_native_home_present': True}

        with patch.object(sys, 'argv', ['isolated', '--timeout', '0']), \
             patch.object(native, 'projects', side_effect=projects), \
             patch.object(isolated, 'scratch_claude', side_effect=scratch), \
             patch.object(isolated, 'git_worktree_preflight', return_value={}), \
             patch.object(isolated, 'verify_lead_model', return_value={}), \
             patch.object(isolated.shutil, 'which', return_value='claude'), \
             patch.object(isolated.subprocess, 'Popen', return_value=Mock(poll=Mock(return_value=0))), \
             patch.object(native, 'failure_state', side_effect=snapshot) as inspect, \
             patch.dict(isolated.os.environ, {}, clear=True), \
             redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(isolated.main(), 1)
        inspect.assert_called_once()
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())

    def test_fixture_route_passes_without_accepting_the_legacy_clamp(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "primary"
            project.mkdir()
            with patch.object(native, "projects", return_value=(project, project)), \
                 patch.object(isolated, "git_worktree_preflight", return_value={}), \
                 patch.object(isolated, "scratch_claude", return_value=({}, root / "home")), \
                 patch.object(native, "claude_command", return_value=Mock(
                     stdout=json.dumps({"modelUsage": {"claude-sonnet-5-5": {}}}))), \
                 patch.object(isolated.shutil, "which", return_value="claude"), \
                 patch.object(isolated.subprocess, "Popen", side_effect=RuntimeError("launch captured")) as launch:
                with self.assertRaisesRegex(RuntimeError, "launch captured"):
                    isolated.run_case(root, Path(__file__).parents[1], 300, 3)
            profile = launch.call_args.kwargs["env"]["SYMPHONY_PROFILE"]
            root_prompt = launch.call_args.args[0][-1]
            packet = json.loads(re.search(r"LEAD_AGENT_PACKET: (\{[^\n]+\})",
                                          root_prompt).group(1))
            agent = packet["subagent_type"]
            self.assertEqual((profile, agent),
                             ("sonnet-5-5", "symphony:symphony-lead-claude-sonnet-5-5-low"))
            self.assertEqual(packet["isolation"], "worktree")
            self.assertEqual(packet["description"], "Check isolated Symphony lead gate")
            self.assertIs(packet["run_in_background"], True)
            self.assertIn("SYMPHONY_ROLE: lead", packet["prompt"])
            self.assertIn("SYMPHONY_ROUTE:", packet["prompt"])
            self.assertIn("gate.py", packet["prompt"])
            self.assertIn('"topology":"delegated"', packet['prompt'])
            worker = json.loads(packet['prompt'].partition('WORKER_SPAWN_PACKET: ')[2])
            self.assertEqual(worker['subagent_type'], 'symphony:symphony-worker-claude-sonnet-5-5-low')
            self.assertIs(worker['run_in_background'], False)
            self.assertTrue(worker['prompt'].startswith('SYMPHONY_ROLE: worker\n'))
            self.assertIn(str(Path(sys.executable)).replace('\\', '/'), packet['prompt'])
            self.assertNotIn('gate.py', worker['prompt'])
            self.assertIn('An Agent launch acknowledgment is not a worker result', packet['prompt'])
            self.assertIn("without retrying without isolation", root_prompt)
            for selected, label, blocked in ((profile, agent, False),
                                             ("sonnet", "symphony:symphony-lead-claude-sonnet-5-low", True)):
                state = ProjectState(activation={"claude": {"profile": selected}},
                                     active_run=RunState("run", "fixture", status="assessed",
                                                        assessment={"size": "small", "complexity": "simple"}))
                source = Event("spawn", "pre_tool", "now", {"session_id": "root", "tool_name": "Agent",
                    "tool_input": {"subagent_type": label, "prompt": "SYMPHONY_ROLE: lead\n"
                        'SYMPHONY_ROUTE: {"size":"small","complexity":"simple"}\nRun the gate.'}})
                _, actions = _prepare_delegation(state, source, "claude")
                self.assertEqual(any(action.kind == "block_tool" for action in actions), blocked)
                if blocked:
                    self.assertIn("/symphony:proceed", actions[0].payload["reason"])

    def test_isolated_capture_uses_exact_candidate_and_preserves_original_observers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'frozen-candidate'
            home = root / 'claude-home'
            version = '1.7.0'
            installed = home / 'plugins/cache/symphony-isolated/symphony' / version / 'hooks/hooks.json'
            installed.parent.mkdir(parents=True)
            installed.write_text('{}')
            (home / 'settings.json').write_text(json.dumps({'enabledPlugins': {'symphony': True}}))
            original_mkdir = Path.mkdir
            with patch.dict(isolated.os.environ, {'ANTHROPIC_API_KEY': 'private-sentinel'}), \
                 patch.object(native, 'package_version', return_value=version), \
                 patch.object(native, 'marketplace', return_value=root / 'market'), \
                 patch.object(native, 'claude_command'), \
                 patch.object(isolated.Path, 'mkdir', autospec=True, side_effect=lambda path, *a, **kw:
                              None if path == home else original_mkdir(path, *a, **kw)):
                env, actual = isolated.scratch_claude(root, package)
            self.assertEqual(actual, home)
            settings = json.loads((home / 'settings.json').read_text())
            self.assertEqual(settings['enabledPlugins'], {'symphony': True})
            for event in ('SubagentStart', 'SubagentStop'):
                self.assertEqual(len(settings['hooks'][event]), 2)
                self.assertIn('capture_hook.py', settings['hooks'][event][0]['hooks'][0]['command'])
                self.assertIn('capture_claude_hook.py', settings['hooks'][event][1]['hooks'][0]['command'])
            script = (root / 'capture_claude_hook.py').read_text()
            self.assertIn(str(package.resolve()), script)
            self.assertTrue((root / 'private-child-hooks').is_dir())
            self.assertNotIn('private-sentinel', script)

    def test_preflight_requires_the_exact_packaged_serving_model(self):
        for models, error, accepted in (({"claude-sonnet-5-5": {}}, False, True),
                                        ({"claude-sonnet-5": {}}, False, False),
                                        (None, False, False),
                                        ({"claude-sonnet-5-5": {}, "claude-sonnet-5": {}}, False, False),
                                        ({"claude-sonnet-5-5": {}}, True, False)):
            with self.subTest(models=models, error=error), \
                 patch.object(native, "claude_command", return_value=Mock(
                     stdout=json.dumps({"modelUsage": models, "is_error": error}))) as invoke:
                if accepted:
                    self.assertEqual(isolated.verify_lead_model({}, Path("."))["model"], "claude-sonnet-5-5")
                else:
                    with self.assertRaisesRegex(RuntimeError, "exact Sonnet 5.5"):
                        isolated.verify_lead_model({}, Path("."))
                argv = invoke.call_args.args
                self.assertIn("symphony:symphony-lead-claude-sonnet-5-5-low", argv)
                self.assertIn('{"disableAllHooks":true}', argv)
                self.assertNotIn("--model", argv)
                self.assertIn("--no-session-persistence", argv)
