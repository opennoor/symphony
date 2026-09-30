import importlib.util
import json
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
            agent = re.search(r"subagent_type=([^, ]+)", launch.call_args.args[0][-1]).group(1)
            self.assertEqual((profile, agent),
                             ("sonnet-5-5", "symphony:symphony-lead-claude-sonnet-5-5-low"))
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
