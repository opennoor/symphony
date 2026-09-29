import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.host_evidence import codex_recovered_lead_event
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Delegation, ProjectState, RunState
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


ROOT_ID = "01a0ec79-988a-7903-ba2e-b003394a341b"
LEAD_ID = "01a0ec7a-bc3b-73a2-97ad-cb75564f6899"
OLD_TURN = "01a0ec7a-bc5f-7eb2-bc52-7b149e655887"
NEW_TURN = "01a0ec7b-15e6-7993-9445-c7046df07f19"


class HostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "codex-home"
        self.transcript = self.home / "sessions/2026/09/29" / f"rollout-2026-09-29-{LEAD_ID}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.environ = {"SYMPHONY_STATE_DIR": str(self.root / "state"), "CODEX_HOME": str(self.home),
                        "SYMPHONY_PROFILE": "full"}
        self.run = RunState(
            "current-run", "task", status="recovering", session_id=ROOT_ID, provider="codex",
            lead_identity=LEAD_ID, started_at="2026-09-29T02:00:00+00:00",
            updated_at="2026-09-29T02:01:00+00:00",
            assessment={"size": "small", "complexity": "simple", "_retryable_lead": LEAD_ID,
                        "_terminal_turns": {LEAD_ID: [f"turn_id:{OLD_TURN}"]}},
            delegations=(Delegation(LEAD_ID, "lead", "task", "completed", "gpt-6-luna", "low"),),
        )
        self.store = StateStore(self.root / "state")
        self.store.save(self.project, ProjectState(active_run=self.run,
                                                   active_runs={f"codex:{ROOT_ID}": self.run}))

    def tearDown(self):
        self.temp.cleanup()

    def write_turns(self, *, parent=ROOT_ID, model="gpt-6-luna", effort="low",
                    latest_complete=True, latest_time="2026-09-29T02:02:00Z",
                    message='SYMPHONY_OUTCOME: {"status":"completed"}'):
        records = [{"timestamp": "2026-09-29T01:59:00Z", "type": "session_meta",
                    "payload": {"id": LEAD_ID, "source": {"subagent": {
                        "thread_spawn": {"parent_thread_id": parent}}}}}]
        for turn_id, stamp, outcome in (
            (OLD_TURN, "2026-09-29T02:01:00Z", 'SYMPHONY_OUTCOME: {"status":"blocked"}'),
            (NEW_TURN, latest_time, message),
        ):
            records += [
                {"timestamp": stamp, "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": turn_id}},
                {"timestamp": stamp, "type": "turn_context",
                 "payload": {"turn_id": turn_id, "model": model, "effort": effort}},
            ]
            if turn_id != NEW_TURN or latest_complete:
                records.append({"timestamp": stamp, "type": "event_msg",
                                "payload": {"type": "task_complete", "turn_id": turn_id,
                                            "last_agent_message": outcome}})
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))

    def test_completed_native_followup_reconciles_same_run_at_root_stop(self):
        self.write_turns()
        result = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                         "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertNotEqual("block", json.loads(result.stdout).get("decision") if result.stdout else None)
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual("completed", state.recent_runs[-1].outcome["status"])

    def test_incomplete_or_wrong_owner_native_turn_never_completes(self):
        for changes in ({"latest_complete": False}, {"parent": "foreign-root"},
                        {"model": "gpt-6-sol"}, {"effort": "medium"},
                        {"latest_time": "2026-09-29T01:00:00Z"},
                        {"message": "completed without protocol outcome"}):
            with self.subTest(changes=changes):
                self.write_turns(**changes)
                self.assertIsNone(codex_recovered_lead_event(
                    ProjectState(active_run=self.run), ROOT_ID, self.environ))

    def test_prior_run_failure_cannot_authorize_current_run_completion(self):
        self.write_turns(latest_time="2026-09-29T01:00:00Z")
        self.assertIsNone(codex_recovered_lead_event(
            ProjectState(active_run=self.run), ROOT_ID, self.environ))

    def test_native_turn_order_accepts_late_delivery_of_older_failed_hook(self):
        self.write_turns()
        delayed = self.run.__class__(**{**self.run.__dict__,
            "updated_at": "2026-09-29T02:03:00+00:00"})
        self.assertIsNotNone(codex_recovered_lead_event(
            ProjectState(active_run=delayed), ROOT_ID, self.environ))

    def test_native_turn_already_delivered_by_hook_is_not_replayed(self):
        self.write_turns()
        assessed = dict(self.run.assessment)
        assessed["_terminal_turns"] = {LEAD_ID: [f"turn_id:{OLD_TURN}", f"turn_id:{NEW_TURN}"]}
        completed = self.run.__class__(**{**self.run.__dict__, "assessment": assessed})
        self.assertIsNone(codex_recovered_lead_event(
            ProjectState(active_run=completed), ROOT_ID, self.environ))

    def test_root_status_drains_worker_alias_before_host_lead_recovery(self):
        self.write_turns()
        run = replace(self.run, delegations=(
            *self.run.delegations,
            Delegation("worker-id", "worker", "task", "completed", "gpt-6-luna", "low"),
        ))
        self.store.save(self.project, ProjectState(active_run=run,
                                                   active_runs={f"codex:{ROOT_ID}": run}))
        worker_start = event_from_payload("codex", {
            "hook_event_name": "SubagentStart", "session_id": "worker-id",
            "parent_thread_id": ROOT_ID, "agent_id": "worker-id", "turn_id": "worker-turn-two",
            "agent_type": "symphony_worker_gpt_6_luna_low",
        })
        with self.store.session_lock("codex", "worker-id"):
            self.store.queue_session_event("codex", "worker-id", worker_start)

        handle({"session_id": ROOT_ID, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit", "turn_id": "root-turn",
                "prompt": "$symphony:symphony status"}, self.environ)

        state = self.store.load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual("working", next(item.state for item in state.active_run.delegations
                                          if item.identity == "worker-id"))
        self.assertEqual([], self.store.session_record("codex", "worker-id")["pending"])
        stop = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                       "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])


if __name__ == "__main__":
    unittest.main()
