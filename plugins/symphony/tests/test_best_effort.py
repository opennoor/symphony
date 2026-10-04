"""Symphony is a best-effort helper: its own problems never stop the user.

Rule 2: a Stop is held only while the user's delegated work is really
running, or an assessed task's lead was never launched. Rule 6: everything
else (bookkeeping, unverifiable evidence, ownership) is counted privately.
A settled run never captures the next prompt.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.model import Delegation, ProjectState, RunState
from plugins.symphony.symphony.routing import profiles_for
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


class BestEffortStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.store = StateStore(root / "state")
        self.env = {"SYMPHONY_STATE_DIR": str(self.store.root), "SYMPHONY_PROVIDER": "codex",
                    "SYMPHONY_PROFILE": profiles_for("codex")[0]["id"], "SYMPHONY_REPORT_WORKER": "0"}

    def seed(self, *delegations, status="active", lead="lead", **assessment):
        run = RunState("run", "task", session_id="root", provider="codex", status=status,
                       lead_identity=lead, delegations=delegations, assessment=assessment)
        self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                   active_runs={"codex:root": run}))

    def send(self, event, **fields):
        return handle({"session_id": "root", "cwd": str(self.project), "hook_event_name": event,
                       "turn_id": "turn", "model": "m", **fields}, self.env)

    def diagnostics(self):
        return [json.loads(path.read_text()) for path in (self.store.root / "diagnostics").glob("*.json")]

    def test_symphony_bookkeeping_never_holds_the_turn(self):
        # The lead ended without a reconciled outcome: a Symphony evidence
        # problem, not unfinished user work.
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering")
        result = self.send("Stop")
        self.assertEqual("", result.stdout)
        self.assertIn("lead_outcome_missing", {item["signal"] for item in self.diagnostics()})

    def test_running_delegated_work_is_held_once_per_turn(self):
        lead = Delegation("lead", "lead", "task", "working", "m", "medium")
        self.seed(lead)
        first = self.send("Stop")
        self.assertEqual("block", json.loads(first.stdout)["decision"])
        self.assertEqual("", self.send("Stop", stop_hook_active=True).stdout)

    def test_a_new_prompt_closes_a_settled_run_instead_of_replaying_it(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering", _reported_nonsuccess="blocked")
        result = self.send("UserPromptSubmit", prompt="Now rename the config file")
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual("superseded", state.recent_runs[-1].status)
        text = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("remains", text)
        self.assertIn("Now rename the config file", text)

    def test_running_work_is_not_superseded_by_a_prompt(self):
        lead = Delegation("lead", "lead", "task", "working", "m", "medium")
        self.seed(lead)
        self.send("UserPromptSubmit", prompt="How is it going?")
        self.assertIsNotNone(self.store.load(self.project).active_run)

    def test_automatic_host_prompts_never_supersede(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering")
        self.send("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>")
        self.assertIsNotNone(self.store.load(self.project).active_run)


if __name__ == "__main__":
    unittest.main()
