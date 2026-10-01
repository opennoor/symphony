"""Fast lead gates on native lifecycle facts for both providers."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.routing import Assessment, fast_lead_selection, profiles_for, resolve_tier, route_for, snapshot_for
from plugins.symphony.symphony.store import StateStore


class FastRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = StateStore(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, provider, event, profile=None, **fields):
        profile = profile or profiles_for(provider)[0]["id"]
        env = {"SYMPHONY_STATE_DIR": str(self.store.root), "SYMPHONY_PROFILE": profile,
               "SYMPHONY_PROVIDER": provider}
        payload = {"cwd": str(self.project), "session_id": f"{provider}-session",
                   "hook_event_name": event, **fields}
        return handle(payload, env)

    def run_state(self):
        return self.store.load(self.project).active_run

    def start_fast(self, provider, profile=None, model=None, effort="medium"):
        self.hook(provider, "SessionStart", profile)
        selected = fast_lead_selection(snapshot_for(provider, profile or profiles_for(provider)[0]["id"]))
        model = model or selected["model"]
        packet = "SYMPHONY_ROLE: lead\nSYMPHONY_FAST_ROUTE: lead\nHandle bounded task"
        agent_type = (f"symphony:symphony-lead-{model}-{effort}" if provider == "claude"
                      else f"symphony_lead_fast_{model.replace('-', '_').replace('.', '_')}_{effort}")
        tool_input = ({"subagent_type": agent_type, "prompt": packet} if provider == "claude"
                      else {"model": model, "reasoning_effort": effort, "message": packet,
                            "fork_turns": "none", "task_name": agent_type})
        prepared = self.hook(provider, "PreToolUse", profile,
                             tool_name="Agent" if provider == "claude" else "spawn_agent",
                             tool_use_id="fast-spawn", tool_input=tool_input)
        started = {"agent_id": "fast-1", "agent_type": agent_type, "task": packet}
        if "deny" not in prepared.stdout and "block" not in prepared.stdout:
            self.hook(provider, "SubagentStart", profile, **started)
        return prepared, started

    def stop_fast(self, provider, started, report, status="completed", profile=None):
        self.hook(provider, "SubagentStop", profile, **started,
                  status=status, last_assistant_message=report)

    def start_native_codex_fast(self, observed_model=None):
        provider = "codex"
        self.hook(provider, "SessionStart")
        selected = fast_lead_selection(snapshot_for(provider, profiles_for(provider)[0]["id"]))
        model = selected["model"]
        task_name = f"symphony_lead_fast_{model.replace('-', '_').replace('.', '_')}_medium"
        full_task = "Handle bounded task\nCheck the second requested condition"
        packet = f"SYMPHONY_ROLE: lead\nSYMPHONY_FAST_ROUTE: lead\n{full_task}"
        transcript = Path(self.temp.name) / "fast.jsonl"
        transcript.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {"agent_path": f"/root/{task_name}"}}),
            json.dumps({"type": "turn_context", "payload": {"model": observed_model or model, "effort": "medium"}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": packet}]}}),
        )), encoding="utf-8")
        started = {"agent_id": "fast-native", "agent_type": "default",
                   "agent_transcript_path": str(transcript)}
        self.hook(provider, "SubagentStart", **started)
        return started, full_task

    def test_both_providers_complete_only_after_explicit_eligible_outcome(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.tearDown()
                self.setUp()
                prepared, started = self.start_fast(provider)
                self.assertNotIn("deny", prepared.stdout)
                self.assertTrue(self.run_state().assessment["_fast_pending"])
                self.stop_fast(provider, started,
                               'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
                self.assertEqual(self.run_state().status, "completing")
                self.hook(provider, "Stop")
                self.assertIsNone(self.run_state())
                self.assertEqual(self.store.load(self.project).recent_runs[-1].status, "completed")

    def test_escalation_waits_for_independent_assessor_before_replacement(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.tearDown()
                self.setUp()
                _, started = self.start_fast(provider)
                self.stop_fast(provider, started, "SYMPHONY_FAST_DECISION: escalate")
                run = self.run_state()
                self.assertEqual(run.status, "assessing")
                self.assertTrue(run.assessment["_fast_escalated"])
                self.assertEqual(run.lead_identity, "fast-1")
                stop = self.hook(provider, "Stop")
                self.assertTrue("block" in stop.stdout or "deny" in stop.stdout)
                self.assertIsNotNone(self.run_state())

                strongest = profiles_for(provider)[0]["tiers"]["strongest"]
                assessor_type = (f"symphony:symphony-assessor-{strongest}-high" if provider == "claude"
                                 else f"symphony_assessor_{strongest.replace('-', '_').replace('.', '_')}_high")
                packet = "SYMPHONY_ROLE: assessor\nHandle bounded task"
                tool_input = ({"subagent_type": assessor_type, "prompt": packet} if provider == "claude"
                              else {"model": strongest, "reasoning_effort": "high", "message": packet})
                self.hook(provider, "PreToolUse", tool_name="Agent" if provider == "claude" else "spawn_agent",
                          tool_input=tool_input, tool_use_id="assessor-spawn")
                assessor = {"agent_id": "assessor-2", "agent_type": assessor_type, "task": packet}
                self.hook(provider, "SubagentStart", **assessor)
                self.assertEqual(self.run_state().lead_identity, "fast-1")
                self.hook(provider, "SubagentStop", **assessor, status="completed",
                          last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"medium","complexity":"mixed","risk":"normal","rationale":"not bounded","topology":"mixed"}')
                self.assertEqual(self.run_state().assessment["size"], "medium")

                choice = resolve_tier(route_for(Assessment("medium", "mixed")), snapshot_for(provider, profiles_for(provider)[0]["id"]))
                lead_type = (f"symphony:symphony-lead-{choice['lead_model']}-{choice['lead_effort']}"
                             if provider == "claude" else
                             f"symphony_lead_{choice['lead_model'].replace('-', '_').replace('.', '_')}_{choice['lead_effort']}")
                route = '{"size":"medium","complexity":"mixed","risk":"normal","rationale":"not bounded","topology":"mixed"}'
                packet = f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {route}\nHandle bounded task"
                tool_input = ({"subagent_type": lead_type, "prompt": packet} if provider == "claude"
                              else {"model": choice["lead_model"], "reasoning_effort": choice["lead_effort"],
                                    "message": packet})
                prepared = self.hook(provider, "PreToolUse", tool_name="Agent" if provider == "claude" else "spawn_agent",
                                     tool_input=tool_input, tool_use_id="lead-spawn")
                self.assertNotIn("deny", prepared.stdout)
                self.hook(provider, "SubagentStart", agent_id="lead-2", agent_type=lead_type, task=packet)
                self.assertEqual(self.run_state().lead_identity, "lead-2")
                self.assertEqual(self.run_state().owner_generation, 2)

    def test_unavailable_floor_uses_assessor_guidance_and_denies_fast_spawn(self):
        for provider in ("codex", "claude"):
            profile = profiles_for(provider)[-1]["id"]
            self.hook(provider, "SessionStart", profile)
            control = "/symphony:start Fix a bug" if provider == "claude" else "$symphony:symphony start Fix a bug"
            prompt = self.hook(provider, "UserPromptSubmit", profile, prompt=control)
            self.assertIn("assessor", prompt.stdout)
            self.assertFalse(fast_lead_selection(snapshot_for(provider, profile))["model"])
            prepared, _ = self.start_fast(provider, profile, model=profiles_for(provider)[-1]["tiers"]["capable"])
            self.assertTrue("deny" in prepared.stdout or "block" in prepared.stdout)
            self.assertIsNone(self.run_state())

    def test_capability_floor_uses_each_providers_profile(self):
        expected = {"codex": ("gpt-6-sol", "medium"),
                    "claude": ("claude-sonnet-5-5", "medium")}
        for provider, (model, effort) in expected.items():
            profile = next(item for item in profiles_for(provider) if item["tiers"]["capable"] == model)
            self.assertEqual(fast_lead_selection(snapshot_for(provider, profile["id"])),
                             {"model": model, "effort": effort})

    def test_malformed_failed_and_missing_decisions_stay_recoverable(self):
        for report, status in (("Done", "completed"),
                               ("SYMPHONY_FAST_DECISION: nonsense", "completed"),
                               ('SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_FAST_DECISION: escalate\nSYMPHONY_OUTCOME: {"status":"completed"}', "completed"),
                               ('SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {bad}', "completed"),
                               ('SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}', "failed"),
                               ('SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}', "interrupted")):
            with self.subTest(report=report, status=status):
                self.tearDown()
                self.setUp()
                _, started = self.start_fast("codex")
                self.stop_fast("codex", started, report, status)
                self.assertEqual(self.run_state().status, "recovering")
                self.assertIsNone(self.run_state().outcome)

    def test_replayed_terminal_cannot_change_accepted_fast_decision(self):
        _, started = self.start_fast("codex")
        report = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
        self.stop_fast("codex", started, report)
        first = self.run_state()
        self.stop_fast("codex", started, report)
        second = self.run_state()
        self.assertEqual(second.status, first.status)
        self.assertEqual(len(second.delegations), len(first.delegations))

    def test_codex_native_start_without_pre_tool_still_tracks_fast_lead(self):
        provider = "codex"
        started, full_task = self.start_native_codex_fast()
        self.assertEqual(self.run_state().lead_identity, "fast-native")
        self.assertTrue(self.run_state().assessment["_fast_pending"])
        self.assertEqual(self.run_state().task, full_task)
        self.hook(provider, "SubagentStop", **started, status="completed",
                  last_assistant_message='SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
        self.assertEqual(self.run_state().status, "completing")

    def test_native_wrong_model_escalates_instead_of_trapping_replacement(self):
        started, full_task = self.start_native_codex_fast(observed_model="gpt-6-luna")
        self.stop_fast("codex", started,
                       'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
        self.assertEqual(self.run_state().status, "assessing")
        self.assertTrue(self.run_state().assessment["_fast_escalated"])
        self.assertEqual(self.run_state().task, full_task)
        self.assertIsNone(self.run_state().outcome)
        self.assertNotIn("_retryable_lead", self.run_state().assessment)

    def test_boost_denies_fast_spawn_and_native_child_cannot_complete(self):
        self.hook("codex", "SessionStart")
        self.hook("codex", "UserPromptSubmit", prompt="$symphony:symphony boost xhigh")
        prepared, _ = self.start_fast("codex")
        self.assertIn("block", prepared.stdout)
        started, _ = self.start_native_codex_fast()
        self.assertTrue(self.run_state().assessment["_fast_disallowed"])
        self.stop_fast("codex", started,
                       'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
        self.assertEqual(self.run_state().status, "assessing")
        self.assertIsNone(self.run_state().outcome)

    def test_terminal_descendant_allows_escalation_but_not_fast_completion(self):
        _, started = self.start_fast("codex")
        child = {"agent_id": "worker-1", "agent_type": "symphony_worker_gpt_6_luna_low",
                 "parent_thread_id": "fast-1"}
        self.hook("codex", "SubagentStart", **child)
        self.hook("codex", "SubagentStop", **child, status="completed")
        self.stop_fast("codex", started, "SYMPHONY_FAST_DECISION: escalate")
        self.assertEqual(self.run_state().status, "assessing")
        self.assertTrue(self.run_state().assessment["_fast_escalated"])

    def test_created_or_waiting_descendant_blocks_handoff(self):
        for child_state in ("created", "waiting"):
            with self.subTest(child_state=child_state):
                self.tearDown()
                self.setUp()
                _, started = self.start_fast("codex")
                self.hook("codex", "SubagentStart", agent_id="worker-1",
                          agent_type="symphony_worker_gpt_6_luna_low",
                          parent_thread_id="fast-1", status=child_state)
                self.stop_fast("codex", started, "SYMPHONY_FAST_DECISION: escalate")
                self.assertTrue(self.run_state().assessment["_fast_pending"])
                self.assertNotIn("_fast_escalated", self.run_state().assessment)

    def test_old_fast_lead_restart_cannot_reclaim_escalated_run(self):
        _, started = self.start_fast("codex")
        self.stop_fast("codex", started, "SYMPHONY_FAST_DECISION: escalate")
        self.hook("codex", "SubagentStart", **started, turn_id="later-turn")
        run = self.run_state()
        self.assertTrue(run.assessment["_fast_escalated"])
        self.assertEqual(run.status, "assessing")
        self.hook("codex", "SubagentStop", **started, turn_id="later-turn", status="completed",
                  last_assistant_message='SYMPHONY_OUTCOME: {"status":"completed"}')
        self.assertTrue(self.run_state().assessment["_fast_escalated"])
        self.assertIsNone(self.run_state().outcome)

    def test_stop_keeps_fast_run_with_tracked_descendant(self):
        _, started = self.start_fast("codex")
        blocked = self.hook("codex", "PreToolUse", tool_name="spawn_agent",
                            tool_input={"model": "gpt-6-luna", "reasoning_effort": "low",
                                        "message": "SYMPHONY_ROLE: worker\nUnauthorized child"})
        self.assertIn("block", blocked.stdout)
        self.hook("codex", "SubagentStart", agent_id="worker-1", agent_type="symphony_worker_gpt_6_luna_low",
                  parent_thread_id="fast-1", status="working")
        self.assertIn("worker-1", [item.identity for item in self.run_state().delegations])
        self.stop_fast("codex", started,
                       'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
        self.assertIsNotNone(self.run_state())
        self.hook("codex", "Stop")
        self.assertIsNotNone(self.run_state())


if __name__ == "__main__":
    unittest.main()
