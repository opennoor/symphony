"""Claude Code behaviours observed in a real session that Symphony mishandled."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Strict ownership/credit reducer; public host-turn liveness has separate tests.
from plugins.symphony.symphony.runtime import _handle_core as handle
from plugins.symphony.symphony.routing import profiles_for
from plugins.symphony.symphony.store import StateStore

CLAUDE_FULL = profiles_for("claude")[0]
STRONGEST = CLAUDE_FULL["tiers"]["strongest"]
ASSESSMENT = '{"size":"medium","complexity":"mixed","risk":"normal","rationale":"r","topology":"mixed"}'


class ClaudeCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.environ = {
            "SYMPHONY_STATE_DIR": str(self.root / "state"),
            "SYMPHONY_PROFILE": CLAUDE_FULL["id"],
            "SYMPHONY_PROVIDER": "claude",
        }

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, event: str, **fields):
        payload = {"session_id": "s", "cwd": str(self.project), "hook_event_name": event, **fields}
        result = handle(payload, self.environ)
        return json.loads(result.stdout) if result.stdout else {}

    def state(self):
        return StateStore(self.root / "state").load(self.project)

    def spawn_assessor(self):
        self.hook("SessionStart")
        self.hook(
            "PreToolUse",
            tool_name="Agent",
            tool_input={
                "subagent_type": f"symphony:symphony-assessor-{STRONGEST}-high",
                "prompt": "SYMPHONY_ROLE: assessor\nClassify the task",
            },
        )
        self.hook(
            "SubagentStart",
            agent_id="assessor-1",
            agent_type=f"symphony:symphony-assessor-{STRONGEST}-high",
        )

    def test_fable_is_the_strongest_claude_model(self):
        self.assertEqual(CLAUDE_FULL["id"], "fable-5-5")
        self.assertEqual(STRONGEST, "claude-fable-5-1")

    def test_guidance_names_the_exact_agent_types_and_how_to_wait(self):
        self.hook("UserPromptSubmit", prompt="/symphony:start Build the feature")
        text = self.hook("UserPromptSubmit", prompt="/symphony:start Build the feature")[
            "hookSpecificOutput"
        ]["additionalContext"]
        self.assertIn("`symphony:symphony-lead-claude-opus-5-5-medium`", text)
        self.assertIn("SYMPHONY_FAST_DECISION: escalate", text)
        self.assertIn("even for a tiny feature", text)
        self.assertIn("requires escalation before any changes", text)
        self.assertIn("end your turn", text)

    def test_assessment_marker_is_read_from_the_handback_report(self):
        self.spawn_assessor()
        transcript = self.root / "assessor.jsonl"
        handback = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "SubagentHandback",
                        "input": {"message": f"Report\nSYMPHONY_ASSESSMENT: {ASSESSMENT}"},
                    }
                ]
            },
        }
        final = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Assessment delivered."}]}}
        transcript.write_text(json.dumps(handback) + "\n" + json.dumps(final) + "\n", encoding="utf-8")
        self.hook(
            "SubagentStop",
            agent_id="assessor-1",
            agent_type=f"symphony:symphony-assessor-{STRONGEST}-high",
            agent_transcript_path=str(transcript),
            last_assistant_message="Assessment delivered.",
        )
        self.assertEqual(self.state().active_run.assessment.get("size"), "medium")

    def test_unmarked_host_agents_are_not_recorded_as_workers(self):
        self.spawn_assessor()
        self.hook("SubagentStart", agent_id="host-1", agent_type="")
        self.hook("SubagentStop", agent_id="host-1", agent_type="")
        identities = [item.identity for item in self.state().active_run.delegations]
        self.assertEqual(identities, ["assessor-1"])

    def test_stop_with_background_agents_waits_without_abandoning(self):
        self.spawn_assessor()
        for active in (False, True):
            output = self.hook(
                "Stop", stop_hook_active=active,
                background_tasks=[{"id": "assessor-1", "type": "subagent"}]
            )
            self.assertNotEqual(output.get("decision"), "block")
        run = self.state().active_run
        self.assertIsNotNone(run, "the run must survive while its agents run in the background")

    def test_stop_without_background_agents_still_guards_once(self):
        self.spawn_assessor()
        self.assertEqual(self.hook("Stop", background_tasks=[]).get("decision"), "block")

    def test_unrelated_or_roleless_background_tasks_do_not_release_managed_run(self):
        self.spawn_assessor()
        for tasks in ([{"id": "other", "type": "subagent"}], [{"id": "shell", "type": "shell"}], [{"id": "assessor-1"}]):
            with self.subTest(tasks=tasks):
                self.assertEqual(self.hook("Stop", background_tasks=tasks).get("decision"), "block")


if __name__ == "__main__":
    unittest.main()
