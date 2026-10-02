import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony.host_evidence import (
    codex_completing_lead_turn, codex_recovered_lead_event,
    codex_unavailable_lead_proof,
)
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony import runtime as runtime_module
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import reduce
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
                    message='SYMPHONY_OUTCOME: {"status":"completed"}',
                    old_complete_time="2026-09-29T02:01:00Z"):
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
                records.append({"timestamp": old_complete_time if turn_id == OLD_TURN else stamp,
                                "type": "event_msg",
                                "payload": {"type": "task_complete", "turn_id": turn_id,
                                            "last_agent_message": outcome}})
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records))

    def write_unavailable_root(self, *, target="symphony_lead_gpt_6_luna_low",
                               spawned_lead=LEAD_ID, output=None, extra=()):
        root = self.transcript.with_name(f"rollout-2026-09-29-{ROOT_ID}.jsonl")
        output = output if output is not None else (
            f"live agent path `/root/{target}` not found")
        records = [
            {"timestamp": "2026-09-29T02:00:00Z", "type": "session_meta",
             "payload": {"id": ROOT_ID}},
            {"timestamp": "2026-09-29T02:00:10Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "spawn-1",
                         "arguments": json.dumps({"task_name": "symphony_lead_gpt_6_luna_low",
                                                  "model": "gpt-6-luna", "reasoning_effort": "low",
                                                  "fork_turns": "none", "message": "Task"})}},
            {"timestamp": "2026-09-29T02:00:30Z", "type": "event_msg",
             "payload": {"type": "item_completed", "item": {"type": "SubAgentActivity",
                 "id": "spawn-1", "kind": "started", "agent_thread_id": spawned_lead}}},
            {"timestamp": "2026-09-29T02:01:05Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "followup_task", "call_id": "followup-1",
                         "arguments": json.dumps({"target": target, "message": "Continue"})}},
            {"timestamp": "2026-09-29T02:01:06Z", "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "followup-1",
                         "output": output}},
            *extra,
        ]
        root.write_text("".join(json.dumps(record) + "\n" for record in records))
        return root

    def test_codex_unavailable_proof_requires_exact_native_error_and_original_lineage(self):
        # The pinned 0.158 host emits this exact output without an is_error
        # flag. Its call ID and original spawn activity are the authority.
        self.write_turns(latest_complete=False)
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows
                                          if row.get("payload", {}).get("turn_id") != NEW_TURN))
        self.write_unavailable_root()
        state = self.store.load(self.project)
        proof = codex_unavailable_lead_proof(state, ROOT_ID, self.environ, "replacement")
        self.assertIsNotNone(proof)
        self.assertEqual(LEAD_ID, proof["original_lead"])
        self.assertEqual("replacement", proof["replacement_identity"])

        for changes in (
            {"target": "another_lead"},
            {"spawned_lead": "01a0ec7a-bc3b-73a2-97ad-cb75564f6898"},
            {"output": "agent unavailable"},
            {"output": "live agent path `/root/symphony_lead_gpt_6_luna_low` not found later"},
        ):
            with self.subTest(changes=changes):
                self.write_unavailable_root(**changes)
                self.assertIsNone(codex_unavailable_lead_proof(
                    state, ROOT_ID, self.environ, "replacement"))

        later_activity = {"timestamp": "2026-09-29T02:01:07Z", "type": "event_msg",
                          "payload": {"type": "item_completed", "item": {
                              "type": "SubAgentActivity", "id": "later-call",
                              "kind": "started", "agent_thread_id": LEAD_ID}}}
        self.write_unavailable_root(extra=(later_activity,))
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))

        self.write_turns(latest_complete=False)
        child_rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        child_rows = [row for row in child_rows
                      if row.get("payload", {}).get("turn_id") != NEW_TURN]
        child_rows.append({"timestamp": "2026-09-29T02:01:08Z", "type": "turn_context",
                           "payload": {"turn_id": NEW_TURN, "model": "gpt-6-luna", "effort": "low"}})
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child_rows))
        self.write_unavailable_root()
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))
        self.write_turns(latest_complete=False)
        child_rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child_rows
                                          if row.get("payload", {}).get("turn_id") != NEW_TURN))
        root = self.write_unavailable_root()
        rows = [json.loads(line) for line in root.read_text().splitlines()]
        root.write_text("".join(json.dumps(row) + "\n" for row in rows
                                if row.get("payload", {}).get("type") != "function_call_output"))
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))
        self.write_unavailable_root()
        child_rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        child_rows.append({"timestamp": "2026-09-29T02:01:08Z", "type": "event_msg",
                           "payload": {"type": "task_started", "turn_id": NEW_TURN}})
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child_rows))
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))

        self.write_turns(latest_complete=False, old_complete_time="2026-09-29T02:01:10Z")
        child_rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child_rows
                                          if row.get("payload", {}).get("turn_id") != NEW_TURN))
        self.write_unavailable_root()
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))

        self.write_turns(latest_complete=False)
        child_rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child_rows
                                          if row.get("payload", {}).get("turn_id") != NEW_TURN))
        root = self.write_unavailable_root()
        root.write_text(root.read_text().rstrip("\n"))
        self.transcript.write_text(self.transcript.read_text().rstrip("\n"))
        self.assertIsNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))

    def test_codex_unavailable_original_allows_one_owned_replacement(self):
        self.write_turns(latest_complete=False)
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows
                                          if row.get("payload", {}).get("turn_id") != NEW_TURN))
        self.write_unavailable_root()
        source = event_from_payload("codex", {
            "hook_event_name": "SubagentStart", "session_id": ROOT_ID,
            "agent_id": "replacement", "agent_type": "symphony_lead_gpt_6_luna_low",
            "turn_id": "replacement-turn", "model": "gpt-6-luna",
            "model_reasoning_effort": "low",
        })
        state = self.store.load(self.project)
        accepted, actions = runtime_module._observe_delegation(state, source, self.environ)
        self.assertEqual("replacement", accepted.active_run.lead_identity)
        self.assertEqual(self.run.owner_generation + 1, accepted.active_run.owner_generation)
        self.assertNotIn("_codex_unavailable_proof", accepted.active_run.assessment)
        self.assertNotIn("reject_lead_replacement", {item.kind for item in actions})

        self.write_unavailable_root(output="live agent path `/root/other` not found")
        guarded, actions = runtime_module._observe_delegation(state, source, self.environ)
        self.assertEqual(LEAD_ID, guarded.active_run.lead_identity)
        self.assertEqual("rejected_lead", next(item.role for item in guarded.active_run.delegations
                                               if item.identity == "replacement"))
        self.assertIn("reject_lead_replacement", {item.kind for item in actions})

    def test_codex_unavailable_proof_uses_latest_failed_turn_after_multiple_turns(self):
        self.write_turns(message='SYMPHONY_OUTCOME: {"status":"blocked"}')
        root = self.write_unavailable_root()
        rows = [json.loads(line) for line in root.read_text().splitlines()]
        for row in rows:
            if row.get("payload", {}).get("name") == "followup_task":
                row["timestamp"] = "2026-09-29T02:03:05Z"
            elif row.get("payload", {}).get("type") == "function_call_output":
                row["timestamp"] = "2026-09-29T02:03:06Z"
        root.write_text("".join(json.dumps(row) + "\n" for row in rows))
        run = replace(self.run, assessment={**self.run.assessment,
            "_retryable_lead_turn": f"turn_id:{NEW_TURN}",
            "_terminal_turns": {LEAD_ID: [f"turn_id:{OLD_TURN}", f"turn_id:{NEW_TURN}"]}})
        state = ProjectState(active_run=run)
        self.assertIsNotNone(codex_unavailable_lead_proof(
            state, ROOT_ID, self.environ, "replacement"))

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
        self.assertEqual(old.active_run.owner_generation,
                         state.recent_runs[-1].owner_generation)
        self.assertEqual("completed", state.recent_runs[-1].outcome["status"])
        record = self.store.session_record("codex", ROOT_ID)
        self.assertIsNotNone(record)
        self.assertEqual([], record["pending"])
        self.assertFalse(record["overflow"])

    def test_released_151_status_guides_stop_after_native_recovery(self):
        self.write_turns()
        self.load_released_recovering_state()
        response = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                           "hook_event_name": "UserPromptSubmit", "turn_id": "root-turn",
                           "prompt": "$symphony:symphony status"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Finish this root turn now so the native Stop hook", context)
        self.assertNotIn("Invoke the normal `$symphony:symphony stop`", context)
        self.assertNotIn("tracked work still requires reconciliation", context)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_released_151_callback_can_precede_failed_native_task_complete(self):
        # Codex may invoke SubagentStop before appending task_complete for the
        # same turn. The old callback still identifies a unique failed turn.
        self.write_turns(old_complete_time="2026-09-29T02:01:07Z")
        old = self.load_released_recovering_state()
        recovered = codex_recovered_lead_event(old, ROOT_ID, self.environ)
        self.assertIsNotNone(recovered)
        self.assertEqual(NEW_TURN, recovered.payload["turn_id"])

        # A task_complete after the newer turn started has uncertain lineage.
        self.write_turns(old_complete_time="2026-09-29T02:02:01Z")
        self.assertIsNone(codex_recovered_lead_event(old, ROOT_ID, self.environ))

        # An intermediate turn is also a boundary, even if the latest turn
        # starts much later and reports success.
        self.write_turns(old_complete_time="2026-09-29T02:01:15Z")
        rows = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        middle = "2026-09-29T02:01:10Z"
        rows[3:3] = [
            {"timestamp": middle, "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": THIRD_TURN}},
            {"timestamp": middle, "type": "turn_context",
             "payload": {"turn_id": THIRD_TURN, "model": "gpt-6-luna", "effort": "low"}},
        ]
        rows[6:6] = [{"timestamp": "2026-09-29T02:01:20Z", "type": "event_msg",
                      "payload": {"type": "task_complete", "turn_id": THIRD_TURN,
                                  "last_agent_message": 'SYMPHONY_OUTCOME: {"status":"blocked"}'}}]
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertIsNone(codex_recovered_lead_event(old, ROOT_ID, self.environ))

    def test_released_151_later_worker_failure_cannot_reuse_completed_lead_turn(self):
        self.write_turns(old_complete_time="2026-09-29T02:01:07Z")
        old = self.load_released_recovering_state()
        failed, _ = reduce(old, Event(
            "later-worker-failure", "delegation_updated", "2026-09-29T02:01:30Z",
            {"identity": "worker-1", "role": "worker", "state": "failed"},
        ))
        self.assertEqual("", failed.active_run.assessment["_retryable_lead"])
        self.assertIsNone(codex_recovered_lead_event(failed, ROOT_ID, self.environ))

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

    def load_archived_followup_fixture(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/archived-followup-codex.json").read_text())
        fixture["root"][0]["payload"]["cwd"] = str(self.project)
        root = self.transcript.with_name(f"rollout-2026-09-29-{ROOT_ID}.jsonl")
        root.write_text("".join(json.dumps(row) + "\n" for row in fixture["root"]))
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in fixture["child"]))
        self.environ["SYMPHONY_PROVIDER"] = "codex"
        model, effort = "gpt-6.1-sol", "medium"
        archived = replace(self.run, status="completed", owner_generation=7,
                           started_at="2026-10-01T09:06:17+00:00", updated_at="2026-10-01T09:08:21+00:00",
                           outcome={"status": "completed", "summary": "old"},
                           assessment={"size": "small", "complexity": "simple",
                                       "_fast_route": {"model": model, "effort": effort},
                                       "_terminal_turns": {LEAD_ID: [f"turn_id:{OLD_TURN}"]}},
                           delegations=(Delegation(LEAD_ID, "lead", "task", "completed", model, effort),))
        self.store.save(self.project, ProjectState(recent_runs=(archived,)))
        self.store.bind_session("codex", ROOT_ID, self.store._path(self.project), False,
                               self.project, ROOT_ID)
        report = fixture["child"][-1]["payload"]["last_agent_message"]
        payload = {"provider": "codex", "session_id": ROOT_ID, "cwd": str(self.project),
                   "hook_event_name": "SubagentStop", "agent_id": LEAD_ID, "agent_type": "lead",
                   "parent_thread_id": ROOT_ID, "turn_id": NEW_TURN, "status": "completed",
                   "model": model, "model_reasoning_effort": effort, "last_assistant_message": report}
        event = event_from_payload("codex", payload)
        self.store.queue_session_event("codex", ROOT_ID, event, ambiguous_owner=True)
        return archived, payload, root

    def test_archived_native_followup_reconciles_terminal_only_through_root_hooks(self):
        for hook in ("Stop", "SessionStart", "UserPromptSubmit"):
            with self.subTest(hook=hook):
                self.tearDown()
                self.setUp()
                archived, payload, _ = self.load_archived_followup_fixture()
                result = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                                 "hook_event_name": hook, "prompt": "$symphony:symphony status"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                state = self.store.load(self.project)
                self.assertIsNone(state.active_run)
                self.assertEqual(1, len(state.recent_runs))
                resumed = state.recent_runs[0]
                self.assertEqual(archived.run_id, resumed.run_id)
                self.assertEqual(archived.owner_generation, resumed.owner_generation)
                self.assertEqual({"status": "completed"}, resumed.outcome)
                self.assertIn(f"turn_id:{NEW_TURN}", resumed.assessment["_terminal_turns"][LEAD_ID])
                self.assertEqual([], self.store.session_record("codex", ROOT_ID)["pending"])
                self.store.queue_session_event("codex", ROOT_ID, event_from_payload("codex", payload), ambiguous_owner=True)
                handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                self.assertEqual(state.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(state.terminal_receipts, self.store.load(self.project).terminal_receipts)
                self.assertEqual([], self.store.session_record("codex", ROOT_ID)["pending"])

    def test_archived_followup_terminal_before_start_is_stable(self):
        for hook in ("Stop", "UserPromptSubmit"):
            with self.subTest(hook=hook):
                self.tearDown()
                self.setUp()
                _, terminal, _ = self.load_archived_followup_fixture()
                handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": hook,
                        "prompt": "$symphony:symphony status"}, self.environ)
                started = {key: value for key, value in terminal.items()
                           if key not in {"status", "last_assistant_message"}}
                started["hook_event_name"] = "SubagentStart"
                handle(started, self.environ)
                result = handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertIsNone(self.store.load(self.project).active_run)
                self.assertEqual(1, len(self.store.load(self.project).recent_runs))
                self.assertEqual([], self.store.session_record("codex", ROOT_ID)["pending"])

    def test_archived_followup_tool_result_may_arrive_after_child_completion(self):
        self.load_archived_followup_fixture()
        root = self.transcript.with_name(f"rollout-2026-09-29-{ROOT_ID}.jsonl")
        rows = [json.loads(line) for line in root.read_text().splitlines()]
        for row in rows:
            if (row["payload"].get("type") == "function_call_output"
                    and row["payload"].get("call_id") == "followup"):
                row["timestamp"] = "2026-10-01T09:50:55Z"
        root.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result = handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
        self.assertNotIn('"decision": "block"', result.stdout)
        self.assertIsNone(self.store.load(self.project).active_run)
        self.assertEqual([], self.store.session_record("codex", ROOT_ID)["pending"])

    def test_archived_followup_rejects_missing_root_project_even_in_project_cwd(self):
        for cwd in (None, "", "   ", ".", "relative", 123, []):
            with self.subTest(cwd=cwd):
                self.tearDown()
                self.setUp()
                archived, _, root = self.load_archived_followup_fixture()
                rows = [json.loads(line) for line in root.read_text().splitlines()]
                rows[0]["payload"]["cwd"] = cwd
                root.write_text("".join(json.dumps(row) + "\n" for row in rows))
                with patch("os.getcwd", return_value=str(self.project)):
                    result = handle({"cwd": str(self.project), "session_id": ROOT_ID,
                                     "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual((archived,), self.store.load(self.project).recent_runs)
                self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

    def test_archived_followup_rejects_foreign_or_missing_delivery_root(self):
        for kind in ("started", "interacted"):
            for owner in (None, "foreign-root"):
                with self.subTest(kind=kind, owner=owner):
                    self.tearDown()
                    self.setUp()
                    archived, _, root = self.load_archived_followup_fixture()
                    rows = [json.loads(line) for line in root.read_text().splitlines()]
                    for row in rows:
                        if row["payload"].get("item", {}).get("kind") == kind:
                            row["payload"]["thread_id"] = owner
                    root.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    result = handle({"cwd": str(self.project), "session_id": ROOT_ID,
                                     "hook_event_name": "Stop"}, self.environ)
                    self.assertIn('"decision": "block"', result.stdout)
                    self.assertEqual((archived,), self.store.load(self.project).recent_runs)
                    self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

    def test_archived_followup_rejects_conflicting_activity_chronology(self):
        for change in ("spawn-before-call", "spawn-after-archive", "result-before-delivery", "wrong-spawn-path", "duplicate-start"):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                archived, _, root = self.load_archived_followup_fixture()
                rows = [json.loads(line) for line in root.read_text().splitlines()]
                started = next(row for row in rows if row["payload"].get("item", {}).get("kind") == "started")
                if change == "spawn-before-call":
                    started["timestamp"] = "2026-10-01T09:06:16Z"
                elif change == "spawn-after-archive":
                    started["timestamp"] = "2026-10-01T09:50:32Z"
                elif change == "result-before-delivery":
                    result = next(row for row in rows if row["payload"].get("type") == "function_call_output"
                                  and row["payload"].get("call_id") == "followup")
                    result["timestamp"] = "2026-10-01T09:50:32Z"
                elif change == "wrong-spawn-path":
                    started["payload"]["item"]["agent_path"] = "/root/another-lead"
                else:
                    rows.append(started)
                root.write_text("".join(json.dumps(row) + "\n" for row in rows))
                result = handle({"cwd": str(self.project), "session_id": ROOT_ID,
                                 "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual((archived,), self.store.load(self.project).recent_runs)
                self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

    def test_archived_followup_drains_bound_codex_child_alias(self):
        for crash in (False, True):
            self.tearDown()
            self.setUp()
            archived, terminal, _ = self.load_archived_followup_fixture()
            record = self.store.session_record("codex", ROOT_ID)
            record["pending"] = []
            self.store._write_json(self.store._session_path("codex", ROOT_ID), record)
            self.store.bind_session("codex", LEAD_ID, self.store._path(self.project),
                                    False, self.project, ROOT_ID)
            terminal["session_id"] = LEAD_ID
            started = {key: value for key, value in terminal.items()
                       if key not in {"status", "last_assistant_message"}}
            started["hook_event_name"] = "SubagentStart"
            handle(started, self.environ)
            handle(terminal, self.environ)
            root = {"cwd": str(self.project), "session_id": ROOT_ID, "hook_event_name": "Stop"}
            if crash:
                with patch.object(StateStore, "finish_session_events", side_effect=OSError("crash")):
                    with self.assertRaises(OSError):
                        handle(root, self.environ)
            result = handle(root, self.environ)
            self.assertNotIn('"decision": "block"', result.stdout)
            state = self.store.load(self.project)
            self.assertEqual(archived.run_id, state.recent_runs[0].run_id)
            self.assertIn(f"turn_id:{NEW_TURN}", state.recent_runs[0].assessment["_terminal_turns"][LEAD_ID])
            self.assertEqual([], self.store.session_record("codex", LEAD_ID)["pending"])
            handle(started, self.environ)
            handle(terminal, self.environ)
            result = handle(root, self.environ)
            self.assertNotIn('"decision": "block"', result.stdout)
            self.assertEqual(state.recent_runs, self.store.load(self.project).recent_runs)
            self.assertEqual([], self.store.session_record("codex", LEAD_ID)["pending"])

    def test_archived_followup_requires_successful_exact_native_evidence(self):
        for change in ("missing-root", "failed-call", "wrong-target", "wrong-child", "foreign-project",
                       "foreign-root", "missing-result", "duplicate-call", "later-followup", "markerless",
                       "failed-native", "wrong-model", "wrong-effort", "wrong-turn", "foreign-parent",
                       "foreign-provider", "foreign-session", "future-generation", "new-owner", "descendant", "wrong-role"):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                archived, payload, root = self.load_archived_followup_fixture()
                rows = [json.loads(line) for line in root.read_text().splitlines()]
                child = [json.loads(line) for line in self.transcript.read_text().splitlines()]
                followup = next(row for row in rows if row["payload"].get("name") == "followup_task")
                result = next(row for row in rows if row["payload"].get("type") == "function_call_output"
                              and row["payload"].get("call_id") == "followup")
                if change == "failed-call":
                    result["payload"]["output"] = "failed"
                elif change == "wrong-target":
                    followup["payload"]["arguments"] = json.dumps({"target": "another-lead"})
                elif change == "wrong-child":
                    for row in rows:
                        if row["payload"].get("item", {}).get("kind") == "interacted":
                            row["payload"]["item"]["agent_thread_id"] = "another-child"
                elif change == "foreign-project":
                    rows[0]["payload"]["cwd"] = str(self.project.parent)
                elif change == "foreign-root":
                    rows[0]["payload"]["id"] = "foreign"
                elif change == "missing-result":
                    rows.remove(result)
                elif change == "duplicate-call":
                    rows.append(followup)
                elif change == "later-followup":
                    rows.append({**followup, "timestamp": "2026-10-01T09:51:00Z",
                                 "payload": {**followup["payload"], "call_id": "later"}})
                elif change in {"markerless", "failed-native"}:
                    child[-1]["payload"]["last_agent_message"] = (
                        "Done" if change == "markerless" else 'SYMPHONY_OUTCOME: {"status":"blocked"}')
                elif change in {"wrong-model", "wrong-effort"}:
                    context = [row for row in child if row["type"] == "turn_context"][-1]
                    context["payload"]["model" if change == "wrong-model" else "effort"] = "wrong"
                elif change == "new-owner":
                    newer = replace(archived, run_id="newer", status="active")
                    self.store.save(self.project, replace(self.store.load(self.project),
                                                         active_run=newer, active_runs={f"codex:{ROOT_ID}": newer}))
                elif change == "descendant":
                    blocked = replace(archived, delegations=(*archived.delegations, Delegation("worker", "worker", "task", "working", "gpt-6-luna", "low")))
                    self.store.save(self.project, replace(self.store.load(self.project), recent_runs=(blocked,)))
                else:
                    record = self.store.session_record("codex", ROOT_ID)
                    if change == "future-generation":
                        record["pending"][0]["generation"] += 1
                    elif change != "missing-root":
                        field = {"wrong-turn": "turn_id", "foreign-parent": "parent_thread_id",
                                 "foreign-provider": "provider", "foreign-session": "session_id",
                                 "wrong-role": "agent_type"}[change]
                        record["pending"][0]["payload"][field] = "worker" if change == "wrong-role" else "foreign"
                    self.store._write_json(self.store._session_path("codex", ROOT_ID), record)
                root.write_text("".join(json.dumps(row) + "\n" for row in rows))
                if change == "missing-root":
                    root.unlink()
                self.transcript.write_text("".join(json.dumps(row) + "\n" for row in child))
                before = self.store.load(self.project)
                result = handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                self.assertIn('"decision": "block"', result.stdout)
                self.assertEqual(before.recent_runs, self.store.load(self.project).recent_runs)
                self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

    def test_superseded_fast_lead_cannot_replace_archived_high_owner(self):
        archived, _, _ = self.load_archived_followup_fixture()
        stronger = replace(archived, lead_identity="stronger-owner", owner_generation=8,
                           delegations=(*archived.delegations, Delegation(
                               "stronger-owner", "lead", "task", "completed", "gpt-6-astra", "high")))
        self.store.save(self.project, replace(self.store.load(self.project), recent_runs=(stronger,)))
        for event in ("Stop", "UserPromptSubmit"):
            result = handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": event,
                             "prompt": "$symphony:symphony status"}, self.environ)
            self.assertIn("superseded lead", result.stdout)
            self.assertIn("Do not resume", result.stdout)
            self.assertIn("or repeat Stop", result.stdout)
        self.assertIsNone(self.store.load(self.project).active_run)
        self.assertEqual((stronger,), self.store.load(self.project).recent_runs)
        self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

    def test_archived_followup_recovery_commit_before_ack_is_idempotent(self):
        for hook in ("Stop", "SessionStart", "UserPromptSubmit"):
            with self.subTest(hook=hook):
                self.tearDown()
                self.setUp()
                self.load_archived_followup_fixture()
                with patch.object(StateStore, "finish_session_events", side_effect=OSError("crash")):
                    with self.assertRaises(OSError):
                        handle({"session_id": ROOT_ID, "cwd": str(self.project),
                                "hook_event_name": hook, "prompt": "$symphony:symphony status"}, self.environ)
                result = handle({"session_id": ROOT_ID, "cwd": str(self.project), "hook_event_name": "Stop"}, self.environ)
                self.assertNotIn('"decision": "block"', result.stdout)
                self.assertIsNone(self.store.load(self.project).active_run)
                self.assertEqual(1, len(self.store.load(self.project).recent_runs))
                self.assertEqual([], self.store.session_record("codex", ROOT_ID)["pending"])

    def test_archived_followup_terminal_requires_new_task_ownership(self):
        self.environ["SYMPHONY_PROVIDER"] = "codex"
        self.write_turns()
        self.write_unavailable_root(output="Followup task delivered")
        archived = replace(self.run, status="completed", outcome={"status": "completed"})
        self.store.save(self.project, ProjectState(recent_runs=(archived,)))
        self.store.bind_session("codex", ROOT_ID, self.store._path(self.project), False,
                                self.project, ROOT_ID)
        handle({"session_id": ROOT_ID, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD_ID,
                "parent_thread_id": ROOT_ID, "turn_id": NEW_TURN, "agent_type": "lead",
                "status": "completed", "model": "gpt-6-luna", "model_reasoning_effort": "low",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        for event, prompt in (("Stop", ""), ("UserPromptSubmit", "$symphony:symphony status")):
            result = handle({"session_id": ROOT_ID, "cwd": str(self.project),
                             "hook_event_name": event, "prompt": prompt}, self.environ)
            self.assertIn("unresolved child result", result.stdout)
        self.assertIsNone(self.store.load(self.project).active_run)
        self.assertEqual((archived,), self.store.load(self.project).recent_runs)
        self.assertEqual(1, len(self.store.session_record("codex", ROOT_ID)["pending"]))

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


class CompletionChronologyTests(unittest.TestCase):
    """File-backed native evidence, independent of callback arrival times."""

    def fixture(self, provider, *, role='worker', early=False, handback=False, lead_handback=False):
        import hashlib
        from plugins.symphony.symphony.host_evidence import assessed_completion_chronology
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / 'project'; self.project.mkdir()
        self.home = self.base / 'home'
        self.provider = provider
        self.session = ROOT_ID
        self.lead = LEAD_ID if provider == 'codex' else 'a1234567890abcde0'
        self.child = '01a0ec7c-1111-7222-8333-444444444444' if provider == 'codex' else 'b1234567890abcde0'
        self.model = 'gpt-6-luna' if provider == 'codex' else 'claude-sonnet-5'
        self.env = {'CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR': str(self.home)}
        stamp = lambda second: f'2026-10-02T10:00:{second:02d}+00:00'
        self.stamp = stamp
        self.child_report = ('SYMPHONY_DECISION: {"size":"small","complexity":"simple"}'
                             if role == 'consultant' else 'Work completed.')
        lead_at = 9 if handback else 8 if early else 12
        proof = {'run_id': 'run', 'epoch': 'epoch', 'lead': self.lead, 'parent': self.lead,
                 'owner_generation': 1, 'role': role, 'successful': True, 'start_event_id': 'child-start',
                 'admitted_at': stamp(6), 'start_parent': self.lead,
                 'turn': 'turn_id:child-turn' if provider == 'codex' else 'prompt_id:hook-root-context'}
        if provider == 'claude':
            proof.update(launch_hash=hashlib.sha256(b'spawn-child').hexdigest(),
                         native_prompt_hash=hashlib.sha256(b'child-prompt').hexdigest())
        self.run = RunState('run', 'task', status='completing', provider=provider, session_id=self.session,
            lead_identity=self.lead, owner_generation=1, started_at=stamp(0), updated_at=stamp(25),
            outcome={'status': 'completed'}, assessment={
                'size': 'small', 'complexity': 'simple',
                'substantive_contract': {'version': 1, 'epoch': 'epoch', 'accepted_at': stamp(1)},
                '_start_event_ids': ['child-start'], '_substantive_children': {self.child: proof},
                '_terminal_turns': {self.lead: ['turn_id:lead-turn'], self.child: ['turn_id:child-turn']}},
            delegations=(Delegation(self.lead, 'lead', 'task', 'completed', self.model, 'low', stamp(23)),
                         Delegation(self.child, role, 'task', 'completed', self.model, 'low', stamp(24))))
        self.state = ProjectState(active_run=self.run)
        self.check = lambda: assessed_completion_chronology(self.state, self.session, self.project, self.env)
        if provider == 'codex':
            directory = self.home / 'sessions/2026/10/02'; directory.mkdir(parents=True)
            self.paths = {identity: directory / f'rollout-{identity}.jsonl'
                          for identity in (self.session, self.lead, self.child)}
            def row(kind, payload, second):
                return {'type': kind, 'payload': payload, 'timestamp': stamp(second)}
            def header(identity, path, parent=''):
                return row('session_meta', {'id': identity, 'cwd': str(self.project), 'agent_path': path,
                    'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent}}} if parent else 'exec'}, 0)
            def call(name, args, key, second):
                return row('response_item', {'type': 'function_call', 'name': name,
                    'arguments': json.dumps(args), 'call_id': key}, second)
            def result(key, second, output=''):
                return row('response_item', {'type': 'function_call_output', 'call_id': key, 'output': output}, second)
            def launch(identity, path, parent, key, second):
                return [call('spawn_agent', {'task_name': path.rsplit('/', 1)[-1], 'model': self.model,
                    'reasoning_effort': 'low', 'fork_turns': 'none', 'message': 'Exact task'}, key, second),
                    row('event_msg', {'type': 'item_completed', 'thread_id': parent, 'item': {
                        'type': 'SubAgentActivity', 'kind': 'started', 'id': key,
                        'agent_thread_id': identity, 'agent_path': path}}, second),
                    result(key, second, json.dumps({'task_name': path}))]
            def start(token, second):
                return [row('event_msg', {'type': 'task_started', 'turn_id': token}, second),
                        row('turn_context', {'turn_id': token, 'model': self.model, 'effort': 'low'}, second)]
            def end(token, report, authored, completed):
                return [row('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                    'content': [{'type': 'output_text', 'text': report}]}, authored),
                    row('event_msg', {'type': 'task_complete', 'turn_id': token, 'last_agent_message': report}, completed)]
            self.rows = {
                self.session: [header(self.session, '/root'), *launch(self.lead, '/root/lead', self.session, 'spawn-lead', 2)],
                self.lead: [header(self.lead, '/root/lead', self.session), *start('lead-turn', 3),
                    *launch(self.child, '/root/lead/child', self.lead, 'spawn-child', 4)],
                self.child: [header(self.child, '/root/lead/child', self.lead), *start('child-turn', 5)]}
            if handback:
                self.rows[self.child] += [call('send_message', {'target': '/root/lead', 'message': self.child_report}, 'handoff', 7), result('handoff', 8)]
            if lead_handback:
                self.rows[self.lead] += [call('send_message', {'target': '/root', 'message': 'Task completed.'}, 'lead-handoff', 8), result('lead-handoff', 9)]
            self.rows[self.child] += end('child-turn', self.child_report, 10, 11)
            receipt_at = 8 if handback else 11
            receipt = (row('response_item', {'type': 'agent_message', 'author': '/root/lead/child',
                'recipient': '/root/lead', 'content': [{'type': 'input_text', 'text':
                    'Message Type: MESSAGE\nTask name: /root/lead\nSender: /root/lead/child\nPayload:\n'
                    + self.child_report}]}, receipt_at) if handback else
                row('event_msg', {'type': 'item_completed', 'thread_id': self.lead, 'item': {
                    'type': 'SubAgentActivity', 'kind': 'completed', 'agent_thread_id': self.child,
                    'agent_path': '/root/lead/child'}}, receipt_at))
            final = end('lead-turn', 'Task completed.', lead_at if not lead_handback else 12, 13)
            self.rows[self.lead] += [receipt, *final] if receipt_at <= lead_at else [final[0], receipt, final[1]]
        else:
            directory = self.home / f'projects/project/{self.session}/subagents'; directory.mkdir(parents=True)
            self.paths = {self.lead: directory / f'agent-{self.lead}.jsonl',
                          self.child: directory / f'agent-{self.child}.jsonl',
                          self.session: directory.parent.with_suffix('.jsonl')}
            def row(kind, identity, content, second, uuid, **fields):
                return {'type': kind, 'sessionId': self.session, 'agentId': identity,
                    'isSidechain': bool(identity), 'uuid': uuid, 'cwd': str(self.project),
                    'timestamp': stamp(second), 'effort': 'low',
                    'message': {'content': content, 'model': self.model, **fields}}
            def launch(identity, target, role, key, second):
                return [row('assistant', identity, [{'type': 'tool_use', 'name': 'Agent', 'id': key,
                    'input': {'subagent_type': f'symphony:symphony-{role}-{self.model}-low',
                              'prompt': f'Exact task for {target}', 'run_in_background': True}}], second, key),
                        row('user', identity, [{'type': 'tool_result', 'tool_use_id': key,
                            'content': 'Background launch acknowledged.'}], second, key + '-result')]
            def handoff(identity, key, report, second):
                return [row('assistant', identity, [{'type': 'tool_use', 'name': 'SubagentHandback', 'id': key,
                    'input': {'message': report}}], second, key, stop_reason='tool_use'),
                    row('user', identity, [{'type': 'tool_result', 'tool_use_id': key, 'content': 'OK'}], second + 1, key + '-result')]
            self.rows = {
                self.session: launch('', self.lead, 'lead', 'spawn-lead', 2),
                self.lead: [row('user', self.lead, f'Exact task for {self.lead}', 3, 'lead-prompt'),
                           *launch(self.lead, self.child, role, 'spawn-child', 4)],
                self.child: [row('user', self.child, f'Exact task for {self.child}', 5, 'child-prompt')]}
            if handback:
                self.rows[self.child] += handoff(self.child, 'handoff', self.child_report, 7)
            if lead_handback:
                self.rows[self.lead] += handoff(self.lead, 'lead-handoff', 'Task completed.', 8)
            self.rows[self.child].append(row('assistant', self.child,
                [{'type': 'text', 'text': self.child_report}], 11, 'child-end', stop_reason='end_turn'))
            if not early and not handback and not lead_handback:
                notice = row('user', self.lead,
                    f'<task-notification><task-id>{self.child}</task-id><status>completed</status>'
                    f'<result>{self.child_report}</result></task-notification>', 11, 'worker-notification')
                notice['origin'] = {'kind': 'task-notification', 'producer': 'session-task'}
                self.rows[self.lead].append(notice)
            self.rows[self.lead].append(row('assistant', self.lead,
                [{'type': 'text', 'text': 'Task completed.'}], 12 if lead_handback else lead_at, 'lead-end', stop_reason='end_turn'))
            for identity, role_name, key, depth in ((self.lead, 'lead', 'spawn-lead', 1), (self.child, role, 'spawn-child', 2)):
                self.paths[identity].with_suffix('.meta.json').write_text(json.dumps({
                    'agentType': f'symphony:symphony-{role_name}-{self.model}-low', 'toolUseId': key, 'spawnDepth': depth}))
        self.write()

    def write(self):
        for identity, rows in self.rows.items():
            self.paths[identity].write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_native_order_not_callback_arrival_controls_completion(self):
        for provider in ('codex', 'claude'):
            for role in ('worker', 'consultant'):
                for early in (False, True):
                    with self.subTest(provider=provider, role=role, early=early):
                        self.fixture(provider, role=role, early=early)
                        self.assertEqual(self.check(), 'early' if early else 'valid')
                        # A delayed child callback does not move its native completion.
                        self.state = replace(self.state, active_run=replace(self.run,
                            delegations=(self.run.delegations[0], replace(self.run.delegations[1], updated_at=self.stamp(59)))))
                        self.assertEqual(self.check(), 'early' if early else 'valid')

    def test_success_alias_in_native_lead_report_keeps_chronology_gate(self):
        for provider in ('codex', 'claude'):
            for status in ('done', 'success', 'succeeded', 'SUCCESS'):
                with self.subTest(provider=provider, status=status):
                    self.fixture(provider)
                    report = f'Task completed.\nSYMPHONY_OUTCOME: {{"status":"{status}"}}'
                    if provider == 'codex':
                        self.rows[self.lead][-2]['payload']['content'][0]['text'] = report
                        self.rows[self.lead][-1]['payload']['last_agent_message'] = report
                    else:
                        self.rows[self.lead][-1]['message']['content'][0]['text'] = report
                    self.write()
                    self.assertEqual('valid', self.check())

    def test_final_handoff_can_precede_courtesy_end_but_early_lead_report_cannot(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.fixture(provider, handback=True)
                self.assertEqual(self.check(), 'valid')
                self.fixture(provider, lead_handback=True)
                self.assertEqual(self.check(), 'early')

    def test_child_terminal_without_exact_parent_delivery_cannot_close_the_run(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.fixture(provider)
                self.rows[self.lead].pop(-3 if provider == 'codex' else -2)
                self.write()
                self.assertEqual(self.check(), 'unknown')
                self.fixture(provider)
                if provider == 'codex':
                    self.rows[self.lead][-3]['payload']['item']['agent_thread_id'] = 'foreign'
                else:
                    self.rows[self.lead][-2]['origin']['producer'] = 'foreign'
                self.write()
                self.assertEqual(self.check(), 'unknown')
        self.fixture('codex', handback=True)
        self.rows[self.lead].pop(-3)
        self.write()
        self.assertEqual(self.check(), 'unknown')

    def test_codex_handback_delivery_can_follow_the_send_ack(self):
        for case in ('valid', 'split-text', 'encrypted', 'foreign-message', 'missing', 'duplicate', 'early', 'before-send'):
            with self.subTest(case=case):
                self.fixture('codex', handback=True)
                receipt = self.rows[self.lead][-3]
                receipt['timestamp'] = '2026-10-02T10:00:08.500+00:00'
                if case == 'split-text':
                    receipt['payload']['content'] = [
                        {'type': 'input_text', 'text': 'Message Type: MESSAGE\nTask name: /root/lead\nSender: /root/lead/child\nPayload:\n'},
                        {'type': 'input_text', 'text': self.child_report}]
                if case == 'encrypted':
                    packet = {'target': '/root/lead', 'message': 'gAAAAopaque_report'}
                    self.rows[self.child][3]['payload']['arguments'] = json.dumps(packet)
                    receipt['payload']['content'] = [
                        {'type': 'input_text', 'text': 'Message Type: MESSAGE\nTask name: /root/lead\nSender: /root/lead/child\nPayload:\n'},
                        {'type': 'encrypted_content', 'encrypted_content': packet['message']}]
                if case == 'foreign-message': receipt['payload']['content'][0]['text'] += 'unrelated'
                if case == 'missing': self.rows[self.lead].remove(receipt)
                if case == 'duplicate': self.rows[self.lead].insert(-3, receipt)
                if case == 'early':
                    receipt['timestamp'] = self.stamp(10)
                    self.rows[self.lead][-3:-1] = list(reversed(self.rows[self.lead][-3:-1]))
                if case == 'before-send': receipt['timestamp'] = self.stamp(6)
                self.write()
                expected = 'valid' if case in {'valid', 'split-text', 'encrypted'} else 'early' if case == 'early' else 'unknown'
                self.assertEqual(self.check(), expected)

    def test_claude_isolated_lead_binds_child_launches_to_its_native_worktree(self):
        for case in ('valid', 'resumed-root', 'not-isolated', 'missing-cwd', 'relative-cwd', 'different-child-cwd'):
            with self.subTest(case=case):
                self.fixture('claude')
                isolated = self.project / '.claude/worktrees' / ('agent-' + self.lead)
                self.rows[self.session][0]['message']['content'][0]['input']['isolation'] = 'worktree'
                for identity in (self.lead, self.child):
                    for row in self.rows[identity]:
                        row['cwd'] = str(isolated)
                if case == 'resumed-root':
                    for row in self.rows[self.lead][1:] + self.rows[self.child]:
                        row['cwd'] = str(self.project)
                    self.rows[self.lead].insert(1, {**self.rows[self.lead][0], 'uuid': 'resumed-prompt',
                        'timestamp': '2026-10-02T10:00:03.500+00:00', 'cwd': str(self.project),
                        'message': {'content': 'Continue the owned task.'}})
                if case == 'not-isolated':
                    self.rows[self.session][0]['message']['content'][0]['input'].pop('isolation')
                if case == 'missing-cwd': self.rows[self.lead][0].pop('cwd')
                if case == 'relative-cwd': self.rows[self.lead][0]['cwd'] = 'relative'
                if case == 'different-child-cwd': self.rows[self.lead][1]['cwd'] = str(self.project / 'foreign')
                self.write()
                self.assertEqual(self.check(), 'valid' if case in {'valid', 'resumed-root'} else 'unknown')

    def test_missing_foreign_ambiguous_failed_and_unsettled_evidence_is_unknown(self):
        for provider in ('codex', 'claude'):
            for case in ('missing', 'foreign-parent', 'foreign-model', 'wrong-generation', 'wrong-epoch',
                         'missing-proof', 'failed-native', 'duplicate-terminal', 'handoff-failed', 'handoff-duplicate',
                         'handoff-missing', 'later-work', 'pending-work'):
                with self.subTest(provider=provider, case=case):
                    self.fixture(provider, handback=True)
                    rows = self.rows[self.child]
                    if case in {'wrong-generation', 'wrong-epoch', 'missing-proof'}:
                        proof = dict(self.run.assessment['_substantive_children'][self.child])
                        if case == 'wrong-generation': proof['owner_generation'] = 2
                        if case == 'wrong-epoch': proof['epoch'] = 'foreign'
                        self.state = replace(self.state, active_run=replace(self.run, assessment={**self.run.assessment,
                            '_substantive_children': {} if case == 'missing-proof' else {self.child: proof}}))
                    elif provider == 'codex':
                        if case == 'foreign-parent': rows[0]['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = ROOT_ID
                        if case == 'foreign-model': rows[2]['payload']['model'] = 'foreign'
                        if case == 'failed-native': rows[-1]['payload']['type'] = 'task_failed'
                        if case == 'duplicate-terminal': rows.append(rows[-1])
                        if case == 'handoff-failed': rows[4]['payload']['output'] = 'Error: unavailable'
                        if case == 'handoff-duplicate': rows.insert(5, rows[4])
                        if case == 'handoff-missing': rows.pop(4)
                        if case in {'later-work', 'pending-work'}:
                            rows.insert(-2 if case == 'later-work' else 3, {'type': 'response_item',
                                'timestamp': self.stamp(9 if case == 'later-work' else 6), 'payload': {
                                    'type': 'function_call', 'name': 'exec_command', 'arguments': '{"cmd":"work"}', 'call_id': 'unsettled'}})
                    else:
                        if case == 'foreign-parent': rows[0]['sessionId'] = 'foreign'
                        if case == 'foreign-model': rows[-1]['message']['model'] = 'foreign'
                        if case == 'failed-native': rows[-1]['message']['stop_reason'] = 'error'
                        if case == 'duplicate-terminal': rows.append(rows[-1])
                        if case == 'handoff-failed': rows[2]['message']['content'][0]['is_error'] = True
                        if case == 'handoff-duplicate': rows.insert(3, rows[2])
                        if case == 'handoff-missing': rows.pop(2)
                        if case in {'later-work', 'pending-work'}:
                            rows.insert(-1 if case == 'later-work' else 1, {**rows[-1],
                                'timestamp': self.stamp(9 if case == 'later-work' else 6), 'uuid': 'unsettled',
                                'message': {'model': self.model, 'stop_reason': 'tool_use', 'content': [{
                                    'type': 'tool_use', 'name': 'Bash', 'id': 'unsettled', 'input': {'command': 'work'}}]}})
                    self.write()
                    if case == 'missing': self.paths[self.child].unlink()
                    self.assertEqual(self.check(), 'unknown')

    def test_only_durably_terminal_children_before_reassessment_are_excluded(self):
        for provider in ('codex', 'claude'):
            for timestamp, status, expected in ((0, 'completed', 'valid'), (0, 'failed', 'valid'),
                    (0, 'working', 'unknown'), (1, 'completed', 'unknown'), (2, 'completed', 'unknown'),
                    ('malformed', 'completed', 'unknown')):
                with self.subTest(provider=provider, timestamp=timestamp, status=status):
                    self.fixture(provider)
                    historical = Delegation('historical', 'worker', 'old objective', status, self.model, 'low',
                        self.stamp(timestamp) if isinstance(timestamp, int) else timestamp)
                    self.state = replace(self.state, active_run=replace(self.run,
                        delegations=(*self.run.delegations, historical)))
                    self.assertEqual(self.check(), expected)

    def test_later_credited_child_supersedes_only_earlier_uncredited_terminal(self):
        cases = ((5, 'completed', 'marked', 'valid'), (5, 'failed', 'marked', 'valid'),
                 (5, 'completed', 'unmarked', 'unknown'), (7, 'completed', 'marked', 'unknown'),
                 (5, 'working', 'marked', 'unknown'), (5, 'interrupted', 'marked', 'unknown'),
                 (13, 'completed', 'marked', 'unknown'), (5, 'completed', 'wrong-replacement', 'unknown'),
                 (5, 'completed', 'old-epoch', 'unknown'), (5, 'completed', 'credited', 'unknown'),
                 ('malformed', 'completed', 'marked', 'unknown'))
        for provider in ('codex', 'claude'):
            for timestamp, status, proof_kind, expected in cases:
                with self.subTest(provider=provider, timestamp=timestamp, status=status, proof=proof_kind):
                    self.fixture(provider)
                    old = Delegation('old-child', 'worker', 'old attempt', status, self.model, 'low',
                        self.stamp(timestamp) if isinstance(timestamp, int) else timestamp)
                    proofs = dict(self.run.assessment['_substantive_children'])
                    if proof_kind == 'credited':
                        proofs[old.identity] = dict(proofs[self.child])
                    elif proof_kind != 'unmarked':
                        proofs[old.identity] = {'successful': False,
                            'superseded_by': 'foreign' if proof_kind == 'wrong-replacement' else self.child,
                            'superseded_epoch': 'foreign' if proof_kind == 'old-epoch' else 'epoch'}
                    self.state = replace(self.state, active_run=replace(self.run,
                        delegations=(*self.run.delegations, old),
                        assessment={**self.run.assessment, '_substantive_children': proofs}))
                    self.assertEqual(self.check(), expected)

    def test_all_current_children_must_return_before_the_lead_report(self):
        import hashlib
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.fixture(provider)
                other = '01a0ec7d-1111-7222-8333-444444444444' if provider == 'codex' else 'c1234567890abcde0'
                def copied(value):
                    return json.loads(json.dumps(value).replace(self.child, other).replace('spawn-child', 'spawn-other')
                        .replace('child-turn', 'other-turn').replace('child-prompt', 'other-prompt')
                        .replace('/lead/child', '/lead/other'))
                self.rows[other] = copied(self.rows[self.child])
                self.paths[other] = self.paths[self.child].with_name(self.paths[self.child].name.replace(self.child, other))
                if provider == 'codex':
                    launches = copied(self.rows[self.lead][3:6])
                    args = json.loads(launches[0]['payload']['arguments']); args['task_name'] = 'other'
                    launches[0]['payload']['arguments'] = json.dumps(args)
                    self.rows[self.lead][6:6] = launches
                    self.rows[other][-2]['timestamp'] = self.stamp(14)
                    self.rows[self.lead].insert(-1, {'type': 'event_msg', 'timestamp': self.stamp(15),
                        'payload': {'type': 'item_completed', 'thread_id': self.lead, 'item': {
                            'type': 'SubAgentActivity', 'kind': 'completed', 'agent_thread_id': other,
                            'agent_path': '/root/lead/other'}}})
                    self.rows[self.lead][-1]['timestamp'] = self.stamp(16)
                else:
                    self.rows[self.lead][3:3] = copied(self.rows[self.lead][1:3])
                    self.paths[other].with_suffix('.meta.json').write_text(json.dumps(copied(json.loads(
                        self.paths[self.child].with_suffix('.meta.json').read_text()))))
                self.rows[other][-1]['timestamp'] = self.stamp(15)
                proof = {**self.run.assessment['_substantive_children'][self.child], 'start_event_id': 'other-start'}
                if provider == 'codex': proof['turn'] = 'turn_id:other-turn'
                else: proof.update(launch_hash=hashlib.sha256(b'spawn-other').hexdigest(),
                                   native_prompt_hash=hashlib.sha256(b'other-prompt').hexdigest())
                self.state = replace(self.state, active_run=replace(self.run,
                    delegations=(*self.run.delegations, replace(self.run.delegations[1], identity=other)),
                    assessment={**self.run.assessment, '_start_event_ids': ['child-start', 'other-start'],
                        '_terminal_turns': {**self.run.assessment['_terminal_turns'], other: ['turn_id:other-turn']},
                        '_substantive_children': {**self.run.assessment['_substantive_children'], other: proof}}))
                self.write()
                self.assertEqual(self.check(), 'early')

    def test_launch_identity_report_scope_and_chronology_are_exact(self):
        for provider in ('codex', 'claude'):
            for case in ('missing-launch-result', 'failed-launch-result', 'duplicate-launch', 'foreign-launch',
                         'wrong-project', 'foreign-lead', 'failed-lead', 'duplicate-lead-terminal',
                         'reversed-report-time', 'foreign-handoff-target', 'forged-call-row', 'forged-result-row',
                         'unfinished-new-prompt'):
                if case == 'foreign-handoff-target' and provider == 'claude':
                    continue
                with self.subTest(provider=provider, case=case):
                    self.fixture(provider, handback=True)
                    lead = self.rows[self.lead]
                    worker = self.rows[self.child]
                    if provider == 'codex':
                        if case == 'missing-launch-result': lead.pop(5)
                        if case == 'failed-launch-result': lead[5]['payload']['output'] = 'Error: rejected'
                        if case == 'duplicate-launch': lead.insert(6, lead[3])
                        if case == 'foreign-launch': lead[4]['payload']['item']['agent_thread_id'] = 'foreign'
                        if case == 'wrong-project': worker[0]['payload']['cwd'] = str(self.base / 'foreign')
                        if case == 'foreign-lead': lead[0]['payload']['source']['subagent']['thread_spawn']['parent_thread_id'] = self.child
                        if case == 'failed-lead': lead[-1]['payload']['last_agent_message'] = 'SYMPHONY_OUTCOME: {"status":"failed"}'
                        if case == 'duplicate-lead-terminal': lead.append(lead[-1])
                        if case == 'reversed-report-time': worker[3]['timestamp'] = self.stamp(1)
                        if case == 'foreign-handoff-target': worker[3]['payload']['arguments'] = '{"target":"foreign","message":"done"}'
                        if case == 'forged-call-row': worker[3]['type'] = 'event_msg'
                        if case == 'forged-result-row': worker[4]['type'] = 'event_msg'
                        if case == 'unfinished-new-prompt': lead.append({'type': 'event_msg', 'timestamp': self.stamp(15),
                            'payload': {'type': 'user_message', 'message': 'Continue this task'}})
                    else:
                        if case == 'missing-launch-result': lead.pop(2)
                        if case == 'failed-launch-result': lead[2]['message']['content'][0]['is_error'] = True
                        if case == 'duplicate-launch': lead.insert(3, lead[1])
                        if case == 'foreign-launch': lead[1]['message']['content'][0]['input']['subagent_type'] = 'foreign'
                        if case == 'wrong-project': lead[1]['cwd'] = str(self.base / 'foreign')
                        if case == 'foreign-lead': self.rows[self.session][0]['sessionId'] = 'foreign'
                        if case == 'failed-lead': lead[-1]['message']['content'][0]['text'] = 'SYMPHONY_OUTCOME: {"status":"failed"}'
                        if case == 'duplicate-lead-terminal': lead.append(lead[-1])
                        if case == 'reversed-report-time': worker[1]['timestamp'] = self.stamp(1)
                        if case == 'forged-call-row': worker[1]['type'] = 'system'
                        if case == 'forged-result-row': worker[2]['type'] = 'system'
                        if case == 'unfinished-new-prompt': lead.append({**lead[0], 'uuid': 'new-prompt', 'timestamp': self.stamp(15)})
                    self.write()
                    self.assertEqual(self.check(), 'unknown')

    def test_claude_null_terminal_needs_the_existing_exact_parent_completion_proof(self):
        from itertools import product
        from plugins.symphony.tests.test_claude_historical_sendmessage import HANDBACK_FRAME
        for foreground, stop_reason in product((False, True, 'framed'), (None, 'end_turn')):
            for bad in (False, True):
                with self.subTest(foreground=foreground, stop_reason=stop_reason, bad=bad):
                    self.fixture('claude')
                    self.rows[self.child][-1]['message']['stop_reason'] = stop_reason
                    lead = self.rows[self.lead]
                    if foreground:
                        lead[1]['message']['content'][0]['input']['run_in_background'] = False
                        lead[2]['timestamp'] = self.stamp(11)
                        lead[2]['message']['content'][0]['content'] = [
                            {'type': 'text', 'text': 'foreign' if bad else self.child_report},
                            {'type': 'text', 'text': f"agentId: {self.child} (use SendMessage with to: '{self.child}', "
                             "summary: '<5-10 word recap>' to continue this agent)\n"
                               '<usage>subagent_tokens: 1\ntool_uses: 0\nduration_ms: 1</usage>'}]
                        if foreground == 'framed':
                            report, footer = lead[2]['message']['content'][0]['content']
                            lead[2]['message']['content'][0]['content'] = [{'type': 'text',
                                'text': HANDBACK_FRAME + '\n  ' + report['text'].replace('\n', '\n  ')
                                    + '\n' + footer['text']}]
                        lead.pop(-2)
                    else:
                        lead[-2]['origin']['producer'] = 'foreign' if bad else 'session-task'
                    self.write()
                    self.assertEqual(self.check(), 'unknown' if bad else 'valid')


if __name__ == "__main__":
    unittest.main()
