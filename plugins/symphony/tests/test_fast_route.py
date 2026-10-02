"""Fast lead gates on native lifecycle facts for both providers."""

import json
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.scripts.generate_agents import BODIES
from plugins.symphony.symphony.model import Event
from plugins.symphony.symphony.runtime import handle, _assessment_from_marker, _ASSESSOR_CONTRACT, _CONSULTANT_CONTRACT, _LEAD_VERIFICATION_CONTRACT, _decision_markers
from plugins.symphony.symphony.routing import Assessment, fast_lead_selection, profiles_for, resolve_tier, route_for, snapshot_for
from plugins.symphony.symphony.store import StateStore


class FastRouteTests(unittest.TestCase):
    def test_fast_launch_identity_is_distinct_from_later_assessed_names(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.tearDown()
                self.setUp()
                self.hook(provider, 'SessionStart')
                control = '$symphony:symphony enable' if provider == 'codex' else '/symphony:enable'
                self.hook(provider, 'UserPromptSubmit', prompt=control)
                result = self.hook(provider, 'UserPromptSubmit', prompt='Implement a bounded feature')
                selected = fast_lead_selection(snapshot_for(provider, profiles_for(provider)[0]['id']))
                if provider == 'codex':
                    name = 'symphony_lead_fast_' + selected['model'].replace('-', '_').replace('.', '_') + '_' + selected['effort']
                    self.assertIn('task_name=\\"' + name + '\\"', result.stdout)
                    self.assertIn('never use them for a fast spawn', result.stdout)
                    self.assertIn('append a unique underscore suffix for a fresh child', result.stdout)
                    self.assertIn('for assessor and assessed lead', result.stdout)
                else:
                    self.assertIn('packaged type alone does not identify a fast launch', result.stdout)
                    self.assertIn('SYMPHONY_FAST_ROUTE: lead', result.stdout)
                self.assertIn('spawn the assessor directly', result.stdout)
                self.assertIn('after an attempted fast lead returns native escalation', result.stdout)

    def test_enable_control_only_stays_at_root_and_accompanying_task_keeps_routing(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.tearDown()
                self.setUp()
                control = '$symphony:symphony enable' if provider == 'codex' else '/symphony:enable'
                self.hook(provider, 'SessionStart')
                for _ in range(2):
                    result = self.hook(provider, 'UserPromptSubmit', prompt=control)
                    self.assertIn('control-only invocation', result.stdout)
                    self.assertNotIn('Symphony fast route:', result.stdout)
                    self.assertIsNone(self.run_state())
                routed = self.hook(provider, 'UserPromptSubmit', prompt=control + ' Run python -m unittest -q')
                self.assertIn('Symphony fast route:', routed.stdout)
                plain = self.hook(provider, 'UserPromptSubmit', prompt='Run python -m unittest -q')
                self.assertIn('Symphony fast route:', plain.stdout)

    def test_provider_assessment_markers_reject_invalid_risk_and_keep_legacy_default(self):
        for provider in ('codex', 'claude'):
            for risk in ('low', 'material concerns', 'critical', '', None, 1, False, ['normal'], {'risk': 'normal'}):
                with self.subTest(provider=provider, risk=risk):
                    report = 'SYMPHONY_ASSESSMENT: ' + json.dumps({'size': 'small', 'complexity': 'simple', 'risk': risk})
                    self.assertIsNone(_assessment_from_marker({'provider': provider, 'last_assistant_message': report}, 'SYMPHONY_ASSESSMENT:'))
            report = 'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}'
            self.assertEqual(_assessment_from_marker({'provider': provider, 'last_assistant_message': report}, 'SYMPHONY_ASSESSMENT:').risk, 'normal')

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

    def start_fast(self, provider, profile=None, model=None, effort="medium", identity="fast-1", launch_id="auto", root_prompt=None):
        self.hook(provider, "SessionStart", profile)
        selected = fast_lead_selection(snapshot_for(provider, profile or profiles_for(provider)[0]["id"]))
        model = model or selected["model"]
        packet = "SYMPHONY_ROLE: lead\nSYMPHONY_FAST_ROUTE: lead\nRun git status --short and report the result"
        agent_type = (f"symphony:symphony-lead-{model}-{effort}" if provider == "claude"
                      else f"symphony_lead_fast_{model.replace('-', '_').replace('.', '_')}_{effort}")
        tool_input = ({"subagent_type": agent_type, "prompt": packet} if provider == "claude"
                      else {"model": model, "reasoning_effort": effort, "message": packet,
                            "fork_turns": "none", "task_name": agent_type})
        prepared = self.hook(provider, "PreToolUse", profile,
                             tool_name="Agent" if provider == "claude" else "spawn_agent",
                             tool_use_id="fast-spawn-" + identity if launch_id == 'auto' else launch_id,
                             prompt_id=root_prompt,
                             tool_input=tool_input)
        started = {"agent_id": identity, "agent_type": agent_type, "task": packet}
        if "deny" not in prepared.stdout and "block" not in prepared.stdout:
            self.last_fast_start = self.hook(provider, "SubagentStart", profile, **started)
        return prepared, started

    def test_only_accepted_claude_fast_launch_freezes_bounded_native_id(self):
        for provider, launch_id in (('claude', 'toolu_launch'), ('codex', 'toolu_launch'),
                                   *(('claude', value) for value in (None, '', ' whitespace ', 1, 'a' * 161))):
            with self.subTest(provider=provider, launch_id=launch_id):
                self.tearDown()
                self.setUp()
                self.start_fast(provider, launch_id=launch_id, root_prompt='root-prompt')
                assessment = self.run_state().assessment
                if provider == 'claude' and launch_id == 'toolu_launch':
                    self.assertEqual(assessment['_claude_fast_launch_hash'],
                                     hashlib.sha256(launch_id.encode()).hexdigest())
                    self.assertNotIn('_claude_fast_root_prompt_hash', assessment)
                    blocked = self.hook(provider, 'PreToolUse', tool_name='Agent', tool_use_id='different',
                        tool_input={'subagent_type': f"symphony:symphony-lead-{assessment['_fast_route']['model']}-medium",
                                    'prompt': 'SYMPHONY_ROLE: lead\nSYMPHONY_FAST_ROUTE: lead\nRun a command'})
                    self.assertIn('deny', blocked.stdout)
                    self.assertEqual(self.run_state().assessment['_claude_fast_launch_hash'],
                                     assessment['_claude_fast_launch_hash'])
                else:
                    self.assertNotIn('_claude_fast_launch_hash', assessment)

    def test_fast_start_retry_and_continuation_get_only_fast_contract(self):
        for provider in ('codex', 'claude'):
            for phase in ('first', 'retry', 'continuation'):
                with self.subTest(provider=provider, phase=phase):
                    self.tearDown()
                    self.setUp()
                    _, started = self.start_fast(provider)
                    result = self.last_fast_start
                    if phase != 'first':
                        self.stop_fast(provider, started,
                            'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
                            if phase == 'continuation' else '',
                            status='completed' if phase == 'continuation' else 'failed')
                        result = self.hook(provider, 'SubagentStart', **started,
                            **({'turn_id': 'next-turn'} if provider == 'codex' else {'prompt_id': 'next-turn'}))
                    guidance = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
                    self.assertIn('registered Symphony fast lead', guidance)
                    self.assertIn('WHOLE objective', guidance)
                    self.assertIn('escalation before any changes', guidance)
                    self.assertIn('Do not spawn workers', guidance)
                    self.assertIn('SYMPHONY_FAST_DECISION: escalate', guidance)
                    self.assertNotIn('Symphony worker routes', guidance)
                    self.assertNotIn('Every worker spawn', guidance)

    def stop_fast(self, provider, started, report, status="completed", profile=None):
        self.hook(provider, "SubagentStop", profile, **started,
                  status=status, last_assistant_message=report)

    def start_native_codex_fast(self, observed_model=None):
        provider = "codex"
        self.hook(provider, "SessionStart")
        selected = fast_lead_selection(snapshot_for(provider, profiles_for(provider)[0]["id"]))
        model = selected["model"]
        task_name = f"symphony_lead_fast_{model.replace('-', '_').replace('.', '_')}_medium"
        full_task = "Run git status --short and report the result\nConfirm the command exited successfully"
        packet = f"SYMPHONY_ROLE: lead\nSYMPHONY_FAST_ROUTE: lead\n{full_task}"
        transcript = Path(self.temp.name) / "fast.jsonl"
        transcript.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {"id": "fast-native", "agent_path": f"/root/{task_name}"}}),
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

    def test_native_guidance_limits_direct_work_to_whole_mechanical_objectives(self):
        # These assertions protect the agent contract, not a semantic classifier.
        # Real model decisions are exercised separately by native routing smoke.
        for provider in ("codex", "claude"):
            for task in ("Run git status --short and report the result",
                         "Read the specified browser page through known steps",
                         "Add a tiny uppercase feature",
                         "Run the checks and fix any failures"):
                with self.subTest(provider=provider, task=task):
                    self.hook(provider, "SessionStart")
                    control = "/symphony:start " if provider == "claude" else "$symphony:symphony start "
                    result = self.hook(provider, "UserPromptSubmit", prompt=control + task)
                    payload = json.loads(result.stdout)
                    text = payload["hookSpecificOutput"]["additionalContext"]
                    self.assertIn(task, text)
                    self.assertIn("WHOLE objective consists only of predetermined mechanical steps", text)
                    self.assertIn("before any changes", text)
                    self.assertIn("bash/git command", text)
                    self.assertIn("browser page", text)
                    self.assertIn("even for a tiny feature", text)
                    self.assertIn("run-and-fix request escalates as a whole", text)
                    for substantive in ("Implementation", "diagnosis", "design", "substantive review",
                                        "product judgment", "mixed work", "uncertainty"):
                        self.assertIn(substantive, text)
                    self.assertIn("SYMPHONY_FAST_DECISION: escalate", text)
                    self.assertIn('"complexity":"simple|mixed|complex"', text)
                    self.assertIn('"risk":"normal|high"', text)
                    self.assertIn("substantive small work uses one worker", text)
                    self.assertIsNone(self.run_state())
        lead = BODIES["lead"]
        self.assertIn("WHOLE objective consists only of predetermined mechanical steps", lead)
        self.assertIn("assign the substantive work to one worker", lead)
        self.assertIn("assign substantive work to bounded worker packets", lead)
        self.assertNotIn("do quick glue work", lead)
        self.assertIn('risk normal/high', BODIES["assessor"])
        self.assertIn("substantive small work", BODIES["assessor"])

    def test_assessor_topology_cannot_override_worker_execution(self):
        for provider in ("codex", "claude"):
            for size, expected in (("small", "delegated"), ("medium", "mixed")):
                with self.subTest(provider=provider, size=size):
                    self.tearDown()
                    self.setUp()
                    self.hook(provider, "SessionStart")
                    strongest = profiles_for(provider)[0]["tiers"]["strongest"]
                    agent_type = (f"symphony:symphony-assessor-{strongest}-high" if provider == "claude"
                                  else f"symphony_assessor_{strongest.replace('-', '_').replace('.', '_')}_high")
                    packet = "SYMPHONY_ROLE: assessor\nAdd a tiny uppercase feature"
                    tool_input = ({"subagent_type": agent_type, "prompt": packet} if provider == "claude"
                                  else {"model": strongest, "reasoning_effort": "high", "message": packet})
                    self.hook(provider, "PreToolUse", tool_name="Agent" if provider == "claude" else "spawn_agent",
                              tool_input=tool_input, tool_use_id="assessor")
                    started = {"agent_id": "assessor", "agent_type": agent_type, "task": packet}
                    self.hook(provider, "SubagentStart", **started)
                    report = json.dumps({"size": size, "complexity": "simple", "risk": "normal",
                                         "rationale": "tiny implementation", "topology": "direct"})
                    self.hook(provider, "SubagentStop", **started, status="completed",
                              last_assistant_message="SYMPHONY_ASSESSMENT: " + report)
                    assessment = self.run_state().assessment
                    self.assertEqual(assessment["topology"], expected)
                    self.assertEqual(assessment["route"]["execution"], expected)
                    status = "/symphony:status" if provider == "claude" else "$symphony:symphony status"
                    output = self.hook(provider, "UserPromptSubmit", prompt=status).stdout
                    self.assertIn("Topology: " + expected, output)
                    self.assertIn("Accepted topology: " + expected, output)
                    self.assertIn("small tasks need one worker", output)
                    self.assertIn("Delegate implementation before editing", output)
                    self.assertIn('SYMPHONY_OUTCOME:', output)

    def test_codex_exact_profile_cells_and_handoff_fit_the_host_context_limit(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / "hooks/codex.json").read_text())
        limit = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]["additionalContextLimit"]
        for profile in profiles_for("codex"):
            with self.subTest(profile=profile["id"]):
                self.tearDown()
                self.setUp()
                self.hook("codex", "SessionStart", profile["id"])
                task = ("Add a tiny feature. " + "Acceptance detail. " * 30).strip()
                response = self.hook("codex", "UserPromptSubmit", profile["id"], prompt="$symphony:symphony start " + task)
                text = json.loads(response.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertLess(len(text), limit)
                self.assertIn(task, text)
                self.assertIn("never reuse the fast lead selection", text)
                self.assertIn("await its terminal assessment without sending additional work", text)
                for size in ("small", "medium", "large"):
                    for complexity in ("simple", "mixed", "complex"):
                        route = resolve_tier(route_for(Assessment(size, complexity)), snapshot_for("codex", profile["id"]))
                        self.assertIn(f"{size}/{complexity} `{route['lead_model']}/{route['lead_effort']}`", text)

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
                started_assessor = self.hook(provider, "SubagentStart", **assessor)
                context = json.loads(started_assessor.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn(_ASSESSOR_CONTRACT, context)
                self.assertIn(_ASSESSOR_CONTRACT, BODIES['assessor'])
                self.assertIsNotNone(_assessment_from_marker({'last_assistant_message': _ASSESSOR_CONTRACT}, 'SYMPHONY_ASSESSMENT:'))
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
                started_lead = self.hook(provider, "SubagentStart", agent_id="lead-2", agent_type=lead_type, task=packet)
                self.assertEqual(self.run_state().lead_identity, "lead-2")
                self.assertEqual(self.run_state().owner_generation, 2)
                guidance = json.loads(started_lead.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn('SYMPHONY_ROLE: worker', guidance)
                self.assertIn(_LEAD_VERIFICATION_CONTRACT, guidance)
                self.assertIn(_LEAD_VERIFICATION_CONTRACT, BODIES['lead'])
                self.assertNotIn('SYMPHONY_LEAD_SPAWN_PACKET:', guidance)
                if provider == 'codex':
                    self.assertIn('fork_turns="none"', guidance)
                    self.assertIn('symphony_worker_<model>_<effort>', guidance)
                    self.assertIn('symphony_consultant_<model>_<effort>', guidance)
                    self.assertLess(len(guidance), 2048)
                else:
                    self.assertIn('symphony:symphony-worker-', guidance)
                consultant_type = (f"symphony:symphony-consultant-{strongest}-high" if provider == 'claude'
                    else f"symphony_consultant_{strongest.replace('-', '_').replace('.', '_')}_high")
                started = self.hook(provider, 'SubagentStart', agent_id='consultant-1',
                    parent_thread_id='lead-2', agent_type=consultant_type, model=strongest,
                    model_reasoning_effort='high', task='SYMPHONY_ROLE: consultant\n'
                    'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}\nReview independently.')
                context = json.loads(started.stdout)['hookSpecificOutput']['additionalContext']
                self.assertIn(_CONSULTANT_CONTRACT, context)
                self.assertIn(_CONSULTANT_CONTRACT, BODIES['consultant'])
                self.assertEqual(_decision_markers(_CONSULTANT_CONTRACT),
                                 ({'size': 'small', 'complexity': 'simple'},))
                self.assertNotIn('SYMPHONY_LEAD_SPAWN_PACKET:', context)

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

    def test_fresh_native_fast_lead_opens_after_archive_and_replay_is_idempotent(self):
        self.hook("codex", "SessionStart")
        choice = fast_lead_selection(snapshot_for("codex", profiles_for("codex")[0]["id"]))
        report = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
        starts = []
        for identity in ("first-fast", "second-fast"):
            started = {"agent_id": identity, "parent_thread_id": "codex-session",
                       "turn_id": identity + "-turn", "agent_type": "default",
                       "task_name": "symphony_lead_fast_native",
                       "model": choice["model"], "model_reasoning_effort": choice["effort"]}
            starts.append(started)
            self.hook("codex", "SubagentStart", **started)
            self.assertEqual(identity, self.run_state().lead_identity)
            self.stop_fast("codex", started, report)
            self.hook("codex", "Stop")
            self.assertIsNone(self.run_state())
        before = self.store.load(self.project)
        self.assertEqual(2, len(before.recent_runs))
        for started in starts:
            self.hook("codex", "SubagentStart", **started)
            self.stop_fast("codex", started, report)
        after = self.store.load(self.project)
        self.assertEqual(before.recent_runs, after.recent_runs)
        self.assertEqual(before.terminal_receipts, after.terminal_receipts)
        self.assertEqual([], self.store.session_record("codex", "codex-session")["pending"])

    def test_archived_fast_lead_resume_and_foreign_parent_still_block(self):
        for changes, receipt_only in (({"agent_id": "fast-1"}, False),
                                      ({"agent_id": "fast-1"}, True),
                                      ({"agent_id": "fresh", "parent_thread_id": "foreign-root"}, False),
                                      ({"agent_id": "fresh", "parent_thread_id": ""}, False)):
            with self.subTest(changes=changes, receipt_only=receipt_only):
                self.tearDown()
                self.setUp()
                _, started = self.start_fast("codex")
                report = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
                self.stop_fast("codex", started, report)
                self.hook("codex", "Stop")
                if receipt_only:
                    self.store.save(self.project, replace(self.store.load(self.project), recent_runs=()))
                resumed = {**started, "parent_thread_id": "codex-session",
                           "turn_id": "new-turn", **changes}
                self.hook("codex", "SubagentStart", **resumed)
                self.stop_fast("codex", resumed, report)
                self.assertIsNone(self.run_state())
                self.assertEqual(2, len(self.store.session_record("codex", "codex-session")["pending"]))
                self.assertIn("block", self.hook("codex", "Stop").stdout)

    def load_retained_fast_fixture(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/retained-fast-v1.6.0.json").read_text())
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store._path(self.project).write_text(json.dumps(fixture["state"]))
        record = fixture["session"]
        record.update(project=str(self.project), state_name=self.store._path(self.project).name)
        self.store._write_json(self.store._session_path("codex", "codex-session"), record)
        return record

    def test_released_retained_fast_pair_recovers_at_normal_root_entry_points(self):
        for event, fields in (("Stop", {}), ("SessionStart", {}),
                              ("UserPromptSubmit", {"prompt": "$symphony:symphony status"})):
            with self.subTest(event=event):
                self.tearDown()
                self.setUp()
                before = self.load_retained_fast_fixture()
                response = self.hook("codex", event, **fields)
                self.assertNotIn('"decision": "block"', response.stdout)
                self.hook("codex", "Stop")
                state = self.store.load(self.project)
                self.assertIsNone(state.active_run)
                self.assertEqual(["archived-fast", "retained-fast"],
                                 [run.lead_identity for run in state.recent_runs])
                self.assertEqual("completed", state.recent_runs[-1].outcome["status"])
                self.assertEqual([], self.store.session_record("codex", "codex-session")["pending"])
                for item in before["pending"]:
                    self.store.queue_session_event("codex", "codex-session", Event(
                        item["event_id"], item["kind"], item["observed_at"], item["payload"]),
                        ambiguous_owner=True)
                self.hook("codex", "Stop")
                after = self.store.load(self.project)
                self.assertEqual(state.recent_runs, after.recent_runs)
                self.assertEqual(state.terminal_receipts, after.terminal_receipts)
                self.assertEqual([], self.store.session_record("codex", "codex-session")["pending"])

    def test_recovered_pair_survives_commit_before_inbox_ack(self):
        for event in ("Stop", "SessionStart", "UserPromptSubmit"):
            with self.subTest(event=event):
                self.tearDown()
                self.setUp()
                self.load_retained_fast_fixture()
                with patch.object(StateStore, "finish_session_events", side_effect=OSError("crash")):
                    with self.assertRaises(OSError):
                        self.hook("codex", event, prompt="$symphony:symphony status")
                record = self.store.session_record("codex", "codex-session")
                # A partial ACK must also be harmless: only the terminal remains.
                record["pending"] = record["pending"][1:]
                record["pending"][0]["generation"] += 1
                self.store._write_json(self.store._session_path("codex", "codex-session"), record)
                self.assertIn('"decision": "block"', self.hook("codex", "Stop").stdout)
                self.assertEqual(1, len(self.store.session_record("codex", "codex-session")["pending"]))
                record["pending"][0]["generation"] -= 1
                self.store._write_json(self.store._session_path("codex", "codex-session"), record)
                self.assertNotIn('"decision": "block"', self.hook("codex", "Stop").stdout)
                self.assertIsNone(self.run_state())
                self.assertEqual(2, len(self.store.load(self.project).recent_runs))
                self.assertEqual([], self.store.session_record("codex", "codex-session")["pending"])

    def test_retained_fast_pair_requires_exact_invocation_and_ownership(self):
        for change in ("parent", "session", "provider", "turn", "missing-turn", "identity",
                       "generation", "retired", "receipt-only", "reversed", "conflict", "terminal-only"):
            with self.subTest(change=change):
                self.tearDown()
                self.setUp()
                record = self.load_retained_fast_fixture()
                start, terminal = record["pending"]
                if change in {"parent", "session", "provider", "turn"}:
                    field = {"parent": "parent_thread_id", "session": "session_id",
                             "provider": "provider", "turn": "turn_id"}[change]
                    terminal["payload"][field] = "foreign"
                elif change == "missing-turn":
                    for item in record["pending"]:
                        item["payload"].pop("turn_id")
                elif change == "identity":
                    for item in record["pending"]:
                        item["payload"]["agent_id"] = "archived-fast"
                elif change == "generation":
                    terminal["generation"] += 1
                elif change == "retired":
                    record["retired_agents"] = ["retained-fast"]
                elif change == "receipt-only":
                    state = self.store.load(self.project)
                    self.store.save(self.project, replace(state, recent_runs=()))
                    for item in record["pending"]:
                        item["payload"]["agent_id"] = "archived-fast"
                elif change == "reversed":
                    start["observed_at"], terminal["observed_at"] = terminal["observed_at"], start["observed_at"]
                elif change == "conflict":
                    record["pending"].append({**terminal, "event_id": "conflicting-result",
                                              "payload": {**terminal["payload"], "status": "failed"}})
                else:
                    record["pending"] = [terminal]
                self.store._write_json(self.store._session_path("codex", "codex-session"), record)
                self.assertIn('"decision": "block"', self.hook("codex", "Stop").stdout)
                self.assertIsNone(self.run_state())
                self.assertEqual(record["pending"], self.store.session_record("codex", "codex-session")["pending"])

    def test_claude_consecutive_fast_runs_with_ordinary_pretool_and_callbacks(self):
        for identity in ("first-claude", "second-claude"):
            prepared, start = self.start_fast("claude", identity=identity)
            self.assertNotIn("deny", prepared.stdout)
            self.assertEqual(identity, self.run_state().lead_identity)
            self.stop_fast("claude", start,
                           'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}')
            self.hook("claude", "Stop")
            self.assertIsNone(self.run_state())
        self.assertEqual(2, len(self.store.load(self.project).recent_runs))
        self.assertEqual([], self.store.session_record("claude", "claude-session")["pending"])

    def test_sibling_session_cannot_reconcile_another_roots_retained_fast_pair(self):
        record = self.load_retained_fast_fixture()
        self.hook("codex", "SessionStart", session_id="sibling")
        self.assertNotIn('"decision": "block"', self.hook("codex", "Stop", session_id="sibling").stdout)
        self.assertEqual(record["pending"], self.store.session_record("codex", "codex-session")["pending"])
        self.assertIsNone(self.run_state())

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
