"""Native Claude recovery must prove the original lead's latest terminal."""

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.host_evidence import (
    claude_committed_native_terminal_replay, claude_recovered_lead_event,
)
from plugins.symphony.symphony.adapters import event_from_payload
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


SESSION = "01a0ec79-988a-7903-ba2e-b003394a341b"
LEAD = "a9c4054c282207da0"
TYPE = "symphony:symphony-lead-claude-sonnet-5-low"
REPORT = 'SYMPHONY_OUTCOME: {"status":"completed"}'


class ClaudeHostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        home = self.root / "claude-home"
        self.session_dir = home / "projects" / "-fixture" / SESSION
        self.child = self.session_dir / "subagents" / f"agent-{LEAD}.jsonl"
        self.child.parent.mkdir(parents=True)
        self.parent = self.session_dir.with_suffix(".jsonl")
        self.meta = self.child.with_suffix(".meta.json")
        self.environ = {"SYMPHONY_STATE_DIR": str(self.root / "state"),
                        "CLAUDE_CONFIG_DIR": str(home), "SYMPHONY_PROFILE": "full"}
        self.run = RunState(
            "original-run", "task", status="active", session_id=SESSION, provider="claude",
            lead_identity=LEAD, started_at="2026-09-29T02:00:00+00:00",
            updated_at="2026-09-29T02:01:00+00:00",
            assessment={"size": "medium", "complexity": "complex",
                        "_lead_expected_route": {"identity": LEAD,
                                                 "model": "claude-sonnet-5", "effort": "low"}},
            delegations=(Delegation(LEAD, "lead", "task", "working", "claude-sonnet-5", "low"),),
        )
        self.store = StateStore(self.root / "state")
        self.store.save(self.project, ProjectState(active_run=self.run,
                                                   active_runs={f"claude:{SESSION}": self.run}))
        self.write_native()

    def write_native(self, *, parent_session=SESSION, agent=LEAD,
                     model="claude-sonnet-5", effort="low", final=True,
                     report=REPORT, later_prompt=False, handback=False,
                     handback_error=False):
        self.meta.write_text(json.dumps({"agentType": TYPE, "toolUseId": "toolu_launch",
                                         "spawnDepth": 1}))
        parent = [{"type": "assistant", "sessionId": parent_session,
                   "cwd": str(self.project), "timestamp": "2026-09-29T02:01:00Z",
                   "message": {"content": [{"type": "tool_use", "name": "Agent",
                                            "id": "toolu_launch",
                                            "input": {"subagent_type": TYPE}}]}}]
        self.parent.write_text("".join(json.dumps(row) + "\n" for row in parent))
        rows = [{"type": "user", "uuid": "prompt-one", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:01Z",
                 "message": {"content": "Complete this task."}}]
        if handback:
            rows.extend([
                {"type": "assistant", "uuid": "handback", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:40Z",
                 "effort": effort, "message": {"model": model, "stop_reason": "tool_use",
                   "content": [{"type": "tool_use", "name": "SubagentHandback",
                                "id": "toolu_handback", "input": {"message": report}}]}},
                {"type": "user", "uuid": "handback-result", "sessionId": SESSION,
                 "agentId": agent, "isSidechain": True, "timestamp": "2026-09-29T02:01:41Z",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_handback",
                                          "is_error": handback_error}]}},
            ])
        if final:
            rows.append({"type": "assistant", "uuid": "terminal-one", "sessionId": SESSION,
                         "agentId": agent, "isSidechain": True,
                         "timestamp": "2026-09-29T02:02:00Z", "effort": effort,
                         "message": {"model": model, "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": "Done." if handback else report}]}})
        if later_prompt:
            rows.append({"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
                         "agentId": agent, "isSidechain": True,
                         "timestamp": "2026-09-29T02:03:00Z",
                         "message": {"content": "Do more work."}})
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def recovered(self):
        return claude_recovered_lead_event(ProjectState(active_run=self.run), SESSION,
                                           self.project, self.environ)

    def test_missing_old_callback_reconciles_original_run_on_root_stop(self):
        recovered = self.recovered()
        self.assertIsNotNone(recovered)
        self.assertEqual(LEAD, recovered.payload["agent_id"])
        self.assertEqual("prompt-one", recovered.payload["prompt_id"])
        response = handle({"session_id": SESSION, "cwd": str(self.project),
                           "hook_event_name": "UserPromptSubmit",
                           "prompt": "$symphony:symphony status"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Invoke the normal `/symphony:stop`", context)
        self.assertNotIn("tracked work still requires reconciliation", context)
        active = self.store.load(self.project).active_run
        self.assertEqual("original-run", active.run_id)
        self.assertEqual("completing", active.status)
        receipts = len(self.store.load(self.project).terminal_receipts)
        # A delayed ordinary hook for the same native result is a replay.
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        after_callback = self.store.load(self.project)
        self.assertEqual("completing", after_callback.active_run.status)
        self.assertEqual(receipts, len(after_callback.terminal_receipts))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None)
        settled = self.store.load(self.project)
        self.assertIsNone(settled.active_run)
        self.assertEqual("original-run", settled.recent_runs[-1].run_id)
        self.assertEqual("completed", settled.recent_runs[-1].outcome["status"])
        for delivered_session in (SESSION, LEAD):
            handle({"session_id": delivered_session, "cwd": str(self.project),
                    "hook_event_name": "SubagentStop", "agent_id": LEAD,
                    "parent_thread_id": SESSION, "agent_type": TYPE,
                    "agent_transcript_path": str(self.child),
                    "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        self.assertIsNone(self.store.load(self.project).active_run)
        for identity in (SESSION, LEAD):
            record = self.store.session_record("claude", identity)
            self.assertFalse(record and record["pending"])
        self.store.save(self.project, replace(self.store.load(self.project), recent_runs=()))
        handle({"session_id": LEAD, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child), "retry": 2,
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", LEAD)
        self.assertFalse(record and record["pending"])

    def test_session_start_guides_stop_after_native_recovery(self):
        response = handle({"session_id": SESSION, "cwd": str(self.project),
                           "hook_event_name": "SessionStart"}, self.environ)
        context = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Invoke the normal `/symphony:stop`", context)
        self.assertNotIn("continue unfinished work", context)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_latest_successful_background_handback_is_accepted(self):
        self.write_native(handback=True)
        self.assertIsNotNone(self.recovered())
        self.write_native(handback=True, handback_error=True)
        self.assertIsNone(self.recovered())
        self.write_native(handback=True)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows[-1]["message"]["content"][0]["text"] = (
            'SYMPHONY_OUTCOME: {"status":"blocked"}')
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertIsNone(self.recovered())

    def test_newer_prompt_blocks_stop_after_native_completion(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        self.assertEqual("completing", self.store.load(self.project).active_run.status)
        self.write_native(later_prompt=True)
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", json.loads(stop.stdout)["decision"])
        self.assertEqual("completing", self.store.load(self.project).active_run.status)

    def test_delayed_first_callback_replays_after_newer_completed_native_turn(self):
        first_report = "First result.\n" + REPORT
        second_report = "Second result.\n" + REPORT
        self.write_native(report=first_report)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": "2026-09-29T02:03:00Z",
             "message": {"content": "Continue."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": "2026-09-29T02:04:00Z",
             "effort": "low", "message": {"model": "claude-sonnet-5",
                                       "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": second_report}]}}
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        stop = handle({"session_id": SESSION, "cwd": str(self.project),
                       "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", json.loads(stop.stdout).get("decision") if stop.stdout else None)
        self.assertEqual("completed", self.store.load(self.project).recent_runs[-1].status)
        handle({"session_id": LEAD, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child),
                "last_assistant_message": first_report, "status": "completed"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        record = self.store.session_record("claude", LEAD)
        self.assertFalse(record and record["pending"])

    def test_same_text_new_turn_is_not_swallowed_as_old_replay(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStart", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE}, self.environ)
        self.assertEqual("active", self.store.load(self.project).active_run.status)
        owned = self.store.load(self.project)
        sibling = replace(self.run, run_id="sibling-run", session_id="sibling-root",
                          lead_identity="sibling-lead")
        two_roots = replace(owned, active_run=sibling,
                            active_runs={**owned.active_runs, "claude:sibling-root": sibling})
        untagged = event_from_payload("claude", {
            "session_id": SESSION, "hook_event_name": "SubagentStop",
            "agent_id": LEAD, "parent_thread_id": SESSION,
            "agent_type": TYPE, "last_assistant_message": REPORT,
            "status": "completed"})
        self.assertFalse(claude_committed_native_terminal_replay(
            two_roots, untagged, SESSION, self.project, self.environ))
        prompted_at = datetime.now(timezone.utc)
        completed_at = prompted_at + timedelta(milliseconds=1)
        rows = [json.loads(line) for line in self.child.read_text().splitlines()]
        rows.extend([
            {"type": "user", "uuid": "prompt-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": prompted_at.isoformat(),
             "message": {"content": "Continue."}},
            {"type": "assistant", "uuid": "terminal-two", "sessionId": SESSION,
             "agentId": LEAD, "isSidechain": True, "timestamp": completed_at.isoformat(),
             "effort": "low", "message": {"model": "claude-sonnet-5",
                                       "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": REPORT}]}}
        ])
        self.child.write_text("".join(json.dumps(row) + "\n" for row in rows))
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "SubagentStop", "agent_id": LEAD,
                "parent_thread_id": SESSION, "agent_type": TYPE,
                "agent_transcript_path": str(self.child),
                "last_assistant_message": REPORT, "status": "completed"}, self.environ)
        run = self.store.load(self.project).active_run
        self.assertEqual("completing", run.status)
        self.assertIn("prompt_id:prompt-two", run.assessment["_terminal_turns"][LEAD])
        self.assertEqual("completed", self.store.load(self.project).active_run.outcome["status"])

    def test_missing_partial_foreign_or_stale_evidence_never_completes(self):
        for changes in ({"parent_session": "foreign-root"}, {"agent": "foreign-agent"},
                        {"model": "claude-opus-5"}, {"effort": "high"},
                        {"final": False}, {"later_prompt": True},
                        {"report": "Done without protocol marker."}):
            with self.subTest(changes=changes):
                self.write_native(**changes)
                self.assertIsNone(self.recovered())
        self.write_native()
        self.child.write_text(self.child.read_text() + '{"type":')
        self.assertIsNone(self.recovered())

    def test_later_worker_failure_cannot_be_overwritten_by_stale_lead_terminal(self):
        failure = Event("failed-worker", "delegation_updated", "2026-09-29T02:03:00Z",
                        {"identity": "worker-one", "role": "worker", "state": "failed"})
        run = RunState(**{**self.run.__dict__, "delegations": (
            *self.run.delegations,
            Delegation("worker-one", "worker", "task", "failed", "claude-sonnet-5",
                       "low", "2026-09-29T02:03:00Z"),
        )})
        state = ProjectState(active_run=run, event_history=(failure,))
        self.assertIsNone(claude_recovered_lead_event(
            state, SESSION, self.project, self.environ))
        self.assertIsNone(claude_recovered_lead_event(
            ProjectState(active_run=run), SESSION, self.project, self.environ))

    def test_wrong_agent_route_or_parent_launch_is_rejected(self):
        self.meta.write_text(json.dumps({"agentType": "symphony:symphony-lead-claude-opus-5-low",
                                         "toolUseId": "toolu_launch", "spawnDepth": 1}))
        self.assertIsNone(self.recovered())
        self.write_native()
        parent = json.loads(self.parent.read_text())
        parent["message"]["content"][0]["id"] = "unrelated-launch"
        self.parent.write_text(json.dumps(parent) + "\n")
        self.assertIsNone(self.recovered())

    def test_replay_rejects_conflicting_owner_or_native_prompt(self):
        handle({"session_id": SESSION, "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "$symphony:symphony status"}, self.environ)
        state = self.store.load(self.project)
        payload = {"session_id": SESSION, "agent_id": LEAD,
                   "parent_thread_id": SESSION, "hook_event_name": "SubagentStop",
                   "agent_type": TYPE, "last_assistant_message": REPORT,
                   "status": "completed"}
        source = event_from_payload("claude", payload)
        self.assertTrue(claude_committed_native_terminal_replay(
            state, source, SESSION, self.project, self.environ))
        for changes in ({"parent_thread_id": "foreign-root"},
                        {"prompt_id": "different-native-prompt"},
                        {"_symphony_owner_conflict": True},
                        {"agent_type": "symphony:symphony-lead-claude-opus-5-low"}):
            with self.subTest(changes=changes):
                conflicting = Event(source.event_id, source.kind, source.observed_at,
                                    {**source.payload, **changes})
                self.assertFalse(claude_committed_native_terminal_replay(
                    state, conflicting, SESSION, self.project, self.environ))


if __name__ == "__main__":
    unittest.main()
