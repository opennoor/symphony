import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.host_evidence import (
    codex_completing_lead_turn, codex_recovered_lead_event,
)
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Delegation, ProjectState, RunState
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


ROOT_ID = "01a0ec79-988a-7903-ba2e-b003394a341b"
LEAD_ID = "01a0ec7a-bc3b-73a2-97ad-cb75564f6899"
OLD_TURN = "01a0ec7a-bc5f-7eb2-bc52-7b149e655887"
NEW_TURN = "01a0ec7b-15e6-7993-9445-c7046df07f19"
THIRD_TURN = "01a0ec7b-89d6-74e8-a205-f70595f7f78b"


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
                        "_retryable_lead_turn": f"turn_id:{OLD_TURN}",
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

    def load_released_recovering_state(self):
        # Generated with v1.5.1 event_from_payload, _observe_delegation, and
        # StateStore.save after syncing its canonical active_runs alias.
        fixture = Path(__file__).parent / "fixtures" / "recovering-v1.5.1.json"
        raw = json.loads(fixture.read_text())
        self.assertEqual(2, raw["schema_version"])
        self.assertNotIn("_retryable_lead_turn", raw["active_run"]["assessment"])
        self.assertNotIn("_terminal_turns", raw["active_run"]["assessment"])
        self.store._path(self.project).write_bytes(fixture.read_bytes())
        return self.store.load(self.project)

    def test_released_151_recovering_state_reconciles_unique_native_followup(self):
        self.write_turns()
        old = self.load_released_recovering_state()
        self.assertIsNotNone(codex_recovered_lead_event(
            old, ROOT_ID, self.environ))
        result = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                         "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertNotEqual("block", json.loads(result.stdout).get("decision") if result.stdout else None)
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual("legacy-run", state.recent_runs[-1].run_id)
        self.assertEqual(LEAD_ID, state.recent_runs[-1].lead_identity)
        self.assertEqual("completed", state.recent_runs[-1].outcome["status"])

    def test_released_151_status_guides_stop_after_native_recovery(self):
        self.write_turns()
        self.load_released_recovering_state()
        response = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                           "hook_event_name": "UserPromptSubmit", "turn_id": "root-turn",
                           "prompt": "$symphony:symphony status"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Invoke the normal `$symphony:symphony stop`", context)
        self.assertNotIn("tracked work still requires reconciliation", context)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_released_151_recovery_requires_unique_failed_turn(self):
        for edit in ("no_failure_event", "duplicate_lead_start", "extra_prior_failed_turn", "wrong_parent",
                     "wrong_model", "newer_running"):
            with self.subTest(edit=edit):
                self.write_turns(parent="foreign-root" if edit == "wrong_parent" else ROOT_ID,
                                 model="gpt-6-sol" if edit == "wrong_model" else "gpt-6-luna",
                                 latest_complete=edit != "newer_running")
                old = self.load_released_recovering_state()
                if edit == "no_failure_event":
                    old = replace(old, event_history=old.event_history[:-1])
                elif edit == "duplicate_lead_start":
                    old = replace(old, event_history=(old.event_history[0], *old.event_history))
                elif edit == "extra_prior_failed_turn":
                    rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
                    duplicate = []
                    for row in rows:
                        if (row.get("payload") or {}).get("turn_id") == OLD_TURN:
                            copied = json.loads(json.dumps(row))
                            copied["payload"]["turn_id"] = THIRD_TURN
                            duplicate.append(copied)
                    rows[4:4] = duplicate
                    self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
                self.assertIsNone(codex_recovered_lead_event(old, ROOT_ID, self.environ))

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

    def test_new_native_lead_turn_blocks_stop_until_its_result_is_reconciled(self):
        completing = replace(self.run, status="completing", outcome={"status": "completed"},
                             assessment={**self.run.assessment, "_retryable_lead": ""})
        self.store.save(self.project, ProjectState(active_run=completing,
                                                   active_runs={f"codex:{ROOT_ID}": completing}))
        self.write_turns(latest_complete=False)
        stop = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                       "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

        self.write_turns()
        stop = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                       "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None)
        settled = self.store.load(self.project)
        self.assertIsNone(settled.active_run)
        self.assertEqual("completed", settled.recent_runs[-1].status)

    def test_markerless_or_unverified_native_followup_cannot_reuse_prior_outcome(self):
        completing = replace(self.run, status="completing", outcome={"status": "completed"},
                             assessment={**self.run.assessment, "_retryable_lead": ""})
        self.store.save(self.project, ProjectState(active_run=completing,
                                                   active_runs={f"codex:{ROOT_ID}": completing}))
        for changes in ({"latest_complete": False}, {"parent": "foreign-root"},
                        {"model": "gpt-6-sol"}, {"effort": "medium"}):
            with self.subTest(changes=changes):
                self.write_turns(**changes)
                freshness, observed = codex_completing_lead_turn(
                    ProjectState(active_run=completing), ROOT_ID, self.environ)
                self.assertIn(freshness, {"running", "unknown"})
                self.assertIsNone(observed)
        self.write_turns(message="No protocol outcome here.")
        freshness, observed = codex_completing_lead_turn(
            ProjectState(active_run=completing), ROOT_ID, self.environ)
        self.assertEqual("completed", freshness)
        self.assertEqual("blocked", observed.payload["status"])
        self.transcript.write_text(self.transcript.read_text() + '{"type":')
        self.assertEqual("unknown", codex_completing_lead_turn(
            ProjectState(active_run=completing), ROOT_ID, self.environ)[0])

    def test_delayed_old_hook_cannot_make_observed_latest_turn_look_new(self):
        self.write_turns()
        assessment = {**self.run.assessment, "_retryable_lead": "",
                      "_terminal_turns": {LEAD_ID: [f"turn_id:{NEW_TURN}",
                                                     f"turn_id:{OLD_TURN}"]}}
        completing = replace(self.run, status="completing", outcome={"status": "completed"},
                             assessment=assessment)
        freshness, event = codex_completing_lead_turn(
            ProjectState(active_run=completing), ROOT_ID, self.environ)
        self.assertEqual("none", freshness)
        self.assertIsNone(event)

    def test_durable_markerless_followup_can_recover_on_new_completed_native_turn(self):
        completing = replace(self.run, status="completing", outcome={"status": "completed"},
                             assessment={**self.run.assessment, "_retryable_lead": ""})
        self.store.save(self.project, ProjectState(active_run=completing,
                                                   active_runs={f"codex:{ROOT_ID}": completing}))
        self.write_turns(message="No protocol outcome here.")
        records = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        for record in records:
            if record.get("payload", {}).get("turn_id") == OLD_TURN and record.get("payload", {}).get("type") == "task_complete":
                record["payload"]["last_agent_message"] = 'SYMPHONY_OUTCOME: {"status":"completed"}'
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        first = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                        "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertEqual("block", json.loads(first.stdout)["decision"])
        failed = self.store.load(self.project).active_run
        self.assertEqual("recovering", failed.status)
        self.assertIn(f"turn_id:{NEW_TURN}", failed.assessment["_terminal_turns"][LEAD_ID])

        third = "2026-09-29T02:04:00Z"
        for record in (
            {"timestamp": third, "type": "event_msg", "payload": {"type": "task_started", "turn_id": THIRD_TURN}},
            {"timestamp": third, "type": "turn_context", "payload": {"turn_id": THIRD_TURN,
                                                               "model": "gpt-6-luna", "effort": "low"}},
            {"timestamp": third, "type": "event_msg", "payload": {"type": "task_complete",
                                                              "turn_id": THIRD_TURN, "last_agent_message":
                                                              'SYMPHONY_OUTCOME: {"status":"completed"}'}},
        ):
            records.append(record)
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
        final = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                        "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertNotEqual("block", json.loads(final.stdout).get("decision") if final.stdout else None)
        self.assertEqual("completed", self.store.load(self.project).recent_runs[-1].status)

    def test_recovery_never_uses_older_failure_when_exact_durable_failure_is_absent(self):
        self.write_turns()
        later_missing = "01a0ec7c-064d-7597-8557-265448837536"
        assessment = {**self.run.assessment,
                      "_retryable_lead_turn": f"turn_id:{later_missing}",
                      "_terminal_turns": {LEAD_ID: [f"turn_id:{OLD_TURN}",
                                                     f"turn_id:{later_missing}"]}}
        recovering = replace(self.run, assessment=assessment)
        self.assertIsNone(codex_recovered_lead_event(
            ProjectState(active_run=recovering), ROOT_ID, self.environ))
        self.store.save(self.project, ProjectState(active_run=recovering,
                                                   active_runs={f"codex:{ROOT_ID}": recovering}))
        stop = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                       "hook_event_name": "Stop", "turn_id": "root-turn"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])
        self.assertEqual("recovering", self.store.load(self.project).active_run.status)


if __name__ == "__main__":
    unittest.main()
