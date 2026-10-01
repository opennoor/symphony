import json
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.adapters import HookResult, event_from_payload
from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony import runtime as runtime_module
from plugins.symphony.symphony.runtime import compact_delegations, format_delegation, handle
from plugins.symphony.symphony.routing import Assessment, MATRIX, profiles_for, resolve_tier, route_for, snapshot_for
from plugins.symphony.symphony.store import StateStore, project_key

CODEX_FULL = profiles_for("codex")[0]
CLAUDE_FULL = profiles_for("claude")[0]
CODEX_STRONGEST = CODEX_FULL["tiers"]["strongest"]
CLAUDE_STRONGEST = CLAUDE_FULL["tiers"]["strongest"]


def route_choice(size="small", complexity="simple", provider="codex"):
    return snapshot_for(provider, profiles_for(provider)[0]["id"]).matrix[f"{size}/{complexity}"]


def claude_agent_type(role, choice):
    return f"symphony:symphony-{role}-{choice['model']}-{choice['effort']}"


def codex_agent_type(role, model, effort):
    return f"symphony_{role}_{model.replace('-', '_').replace('.', '_')}_{effort}"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.spawn_count = 0
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state_root = self.root / "state"
        # Pin the entitlement profile so tests never probe the host machine.
        self.environ = {
            "SYMPHONY_STATE_DIR": str(self.state_root),
            "SYMPHONY_PROFILE": CODEX_FULL["id"],
        }
        self.claude_environ = {**self.environ, "SYMPHONY_PROFILE": CLAUDE_FULL["id"]}
        self.simple = route_choice()
        self.mixed = route_choice("medium", "mixed")
        self.large = route_choice("large", "complex")
        self.claude_simple = route_choice(provider="claude")
        self.claude_medium = route_choice("medium", "simple", "claude")
        self.claude_mixed = route_choice("medium", "mixed", "claude")
        self.claude_large = route_choice("large", "simple", "claude")
        self.claude_assessor_type = f"symphony:symphony-assessor-{CLAUDE_STRONGEST}-high"

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, prompt: str, provider: str = "codex") -> dict:
        result = {
            "session_id": getattr(self, "session_override", f"{provider}-session"),
            "cwd": str(self.project),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
        if provider == "codex":
            result.update({"turn_id": "turn-1", "model": "codex-model"})
        return result

    def output(self, result) -> dict:
        return json.loads(result.stdout) if result.stdout else {}

    def context(self, result) -> str:
        return self.output(result).get("hookSpecificOutput", {}).get("additionalContext", "")

    def flush(self, provider: str = "codex") -> str:
        """Collect guidance deferred from a subagent-stop result.

        Neither host accepts injected context on a stop result, so Symphony
        holds it until the next event that does accept one.
        """
        control = "/symphony:status" if provider == "claude" else "$symphony:symphony status"
        return self.context(handle(self.payload(control, provider), self.environ))

    def open_run(self, task: str = "Implement the feature", provider: str = "codex"):
        """Open a run the way the contract requires: by spawning the assessor.

        A prompt alone no longer opens a run, so every test that needs tracked
        work must put an assessor in front of the host.
        """
        environ = self.claude_environ if provider == "claude" else self.environ
        # A real session heartbeats before it spawns anything, and that is when
        # the entitlement profile is recorded.
        handle({**self.payload("", provider), "hook_event_name": "SessionStart"}, environ)
        self.spawn_count += 1
        hook = {
            **self.payload("", provider),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent" if provider == "claude" else "spawn_agent",
            "tool_use_id": f"spawn-{self.spawn_count}",
        }
        if provider == "claude":
            hook["tool_input"] = {
                "subagent_type": f"symphony-assessor-{CLAUDE_STRONGEST}-high",
                "prompt": f"SYMPHONY_ROLE: assessor\n{task}",
            }
        else:
            hook["tool_input"] = {
                "message": f"SYMPHONY_ROLE: assessor\n{task}",
                "model": CODEX_STRONGEST,
                "reasoning_effort": "high",
            }
        return handle(hook, environ)

    def seed_run(self, run: RunState, provider: str = "codex", enabled: bool = False):
        session = run.session_id or f"{provider}-session"
        run = replace(run, session_id=session, provider=provider)
        StateStore(self.state_root).save(self.project, ProjectState(
            enabled=enabled, active_run=run, active_runs={f"{provider}:{session}": run},
        ))
        return run

    def complete_substantive_worker(self, provider='codex', environ=None):
        """Supply native child work for newly assessed completion fixtures."""
        environ = environ or (self.claude_environ if provider == 'claude' else self.environ)
        run = StateStore(self.state_root).load(self.project).active_run
        choice = resolve_tier(route_for(Assessment('small', 'simple')),
                              snapshot_for(provider, run.assessment['route']['profile']))
        self.spawn_count += 1
        identity = f'fixture-worker-{self.spawn_count}'
        agent_type = (claude_agent_type('worker', {'model': choice['lead_model'], 'effort': choice['lead_effort']})
                      if provider == 'claude' else codex_agent_type('worker', choice['lead_model'], choice['lead_effort']))
        worker = {**self.payload('', provider), 'provider': provider, 'hook_event_name': 'SubagentStart', 'agent_id': identity,
                  'agent_type': agent_type, 'parent_thread_id': run.lead_identity,
                  'task': 'SYMPHONY_ROLE: worker\nComplete the bounded fixture work',
                  'model': choice['lead_model'], 'model_reasoning_effort': choice['lead_effort']}
        if provider == 'claude':
            from plugins.symphony.tests.native_child_fixture import write_claude_child_launch
            home = self.root / 'claude-native'
            environ = {**environ, 'CLAUDE_CONFIG_DIR': str(home)}
            write_claude_child_launch(home, self.project, run, identity, 'worker',
                choice['lead_model'], choice['lead_effort'], f'worker-{self.spawn_count}')
        handle(worker, environ)
        handle({**worker, 'hook_event_name': 'SubagentStop', 'status': 'completed'}, environ)

    def test_enable_persists_and_next_task_requests_bounded_assessment(self):
        enabled = handle(self.payload("$symphony:symphony enable"), self.environ)
        self.assertIn("enabled", self.context(enabled).lower())

        task = handle(self.payload("Implement the feature"), self.environ)

        self.assertIn("assess", self.context(task).lower())
        state = StateStore(self.state_root).load(self.project)
        self.assertTrue(state.enabled)
        self.assertIsNone(state.active_run, "a prompt guides; only the assessor opens a run")

        self.open_run("Implement the feature")
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.active_run.task, "Implement the feature")

    def test_second_session_enable_and_repeated_stops_leave_owner_run_intact(self):
        owner = RunState(
            "owner-run", "Owner task", session_id="owner-session", provider="codex", lead_identity="owner-lead",
            delegations=(Delegation("owner-lead", "lead", "Owner task", "working", "", ""),),
        )
        store = StateStore(self.state_root)
        self.seed_run(owner)
        second = {**self.payload("enable"), "session_id": "second-session"}

        enabled = handle(second, self.environ)
        self.assertIn("enabled", self.context(enabled).lower())
        for active in (False, True, True):
            stop = handle({**second, "hook_event_name": "Stop", "stop_hook_active": active}, self.environ)
            self.assertNotEqual(self.output(stop).get("decision"), "block")
            self.assertEqual(store.load(self.project).active_run, owner)

        disabled = handle({**second, "prompt": "$symphony:symphony disable"}, self.environ)
        self.assertIn("disabled", self.context(disabled).lower())
        state = store.load(self.project)
        self.assertFalse(state.enabled)
        self.assertEqual(state.active_runs["codex:owner-session"], owner)

    def test_status_is_session_scoped_and_agents_all_lists_other_active_runs(self):
        first = RunState("first-run", "First task", session_id="first-session", provider="codex")
        second = RunState("second-run", "Second task", session_id="second-session", provider="codex")
        StateStore(self.state_root).save(self.project, ProjectState(
            enabled=True, active_run=first,
            active_runs={"codex:first-session": first, "codex:second-session": second},
        ))
        third = {**self.payload("$symphony:symphony status"), "session_id": "third-session"}

        status = self.context(handle(third, self.environ))
        agents = self.context(handle({**third, "prompt": "$symphony:symphony agents --all"}, self.environ))

        self.assertIn("Run (this session): none", status)
        self.assertNotIn("first-run", status)
        self.assertNotIn("second-run", status)
        self.assertIn("first-run", agents)
        self.assertIn("second-run", agents)

    def test_known_lead_tool_and_child_events_stay_with_own_root(self):
        first = RunState("first-run", "First task", status="active", session_id="first-session",
                         provider="codex", lead_identity="lead-one")
        second = RunState("second-run", "Second task", status="active", session_id="second-session",
                          provider="codex", lead_identity="lead-two")
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(
            enabled=True, active_run=first,
            active_runs={"codex:first-session": first, "codex:second-session": second},
        ))
        child = {**self.payload(""), "session_id": "lead-one"}
        prepared = handle({**child, "hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                           "tool_input": {"message": "SYMPHONY_ROLE: worker\nImplement first task",
                                          "model": self.simple["model"],
                                          "reasoning_effort": self.simple["effort"]}}, self.environ)
        self.assertNotEqual(self.output(prepared).get("decision"), "block")
        started = {**child, "hook_event_name": "SubagentStart", "agent_id": "worker-one",
                   "agent_type": codex_agent_type("worker", self.simple["model"], self.simple["effort"]),
                   "parent_thread_id": "lead-one"}
        handle(started, self.environ)
        handle({**self.payload("$symphony:symphony status"), "session_id": "first-session"},
               self.environ)
        state = store.load(self.project)
        self.assertEqual([item.identity for item in state.active_runs["codex:first-session"].delegations],
                         ["worker-one"])
        self.assertEqual(state.active_runs["codex:second-session"], second)

        handle({**started, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        handle({**self.payload("$symphony:symphony status"), "session_id": "first-session"},
               self.environ)
        state = store.load(self.project)
        self.assertEqual("completed", state.active_runs["codex:first-session"].delegations[0].state)
        self.assertEqual(state.active_runs["codex:second-session"], second)

        child_stop = handle({**child, "hook_event_name": "Stop", "stop_hook_active": True}, self.environ)
        self.assertNotEqual(self.output(child_stop).get("decision"), "block")
        self.assertEqual(store.load(self.project).active_runs["codex:first-session"].status, "active")

    def test_foreign_stop_cannot_claim_migrated_owner_with_same_session_id(self):
        legacy = self.state_root / f"{project_key(self.project)}.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({
            "schema_version": 1, "enabled": True,
            "activation": {"codex": {"session_id": "shared"}},
            "active_run": {"run_id": "old-run", "task": "Codex task", "status": "active",
                           "session_id": "shared", "lead_identity": "codex-lead"},
        }))
        store = StateStore(self.state_root)
        imported = store.load(self.project).active_runs["codex:shared"]

        for active in (False, True, True):
            stop = {**self.payload("", "claude"), "session_id": "shared",
                    "hook_event_name": "Stop", "stop_hook_active": active}
            self.assertNotEqual(self.output(handle(stop, self.claude_environ)).get("decision"), "block")
            self.assertEqual(store.load(self.project).active_runs["codex:shared"], imported)

    def test_two_sessions_keep_assessors_and_stops_in_their_own_runs(self):
        store = StateStore(self.state_root)
        handle(self.payload("$symphony:symphony enable"), self.environ)
        for session, agent in (("first-session", "first-assessor"), ("second-session", "second-assessor")):
            base = {**self.payload(""), "session_id": session}
            handle({**base, "hook_event_name": "SessionStart"}, self.environ)
            handle({**base, "hook_event_name": "PreToolUse", "tool_name": "spawn_agent", "tool_input": {
                "message": f"SYMPHONY_ROLE: assessor\n{session} task",
                "model": CODEX_STRONGEST, "reasoning_effort": "high",
            }}, self.environ)
            handle({**base, "hook_event_name": "SubagentStart", "agent_id": agent,
                    "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high")}, self.environ)

        state = store.load(self.project)
        self.assertEqual(len(state.active_runs), 2)
        self.assertEqual({run.session_id for run in state.active_runs.values()}, {"first-session", "second-session"})
        self.assertEqual(
            {run.session_id: {item.identity for item in run.delegations} for run in state.active_runs.values()},
            {"first-session": {"first-assessor"}, "second-session": {"second-assessor"}},
        )
        for active in (False, True):
            stop = handle({**self.payload(""), "session_id": "third-session",
                           "hook_event_name": "Stop", "stop_hook_active": active}, self.environ)
            self.assertNotEqual(self.output(stop).get("decision"), "block")
        self.assertEqual(len(store.load(self.project).active_runs), 2)

    def test_pending_root_keeps_profile_past_other_sessions_and_history_bound(self):
        store = StateStore(self.state_root)
        handle(self.payload("$symphony:symphony enable"), self.environ)
        for index in range(6):
            session = f"starting-{index}"
            base = {**self.payload(""), "session_id": session}
            handle({**base, "hook_event_name": "SessionStart"}, self.environ)
            handle({**base, "prompt": f"Task for {session}"}, self.environ)
        for index in range(205):
            visitor = {**self.payload(""), "session_id": f"visitor-{index}",
                       "hook_event_name": "SessionStart"}
            handle(visitor, self.environ)
        before = store.load(self.project)
        self.assertEqual(len(before.event_history), 200)
        self.assertFalse(before.active_runs)

        oldest = {**self.payload(""), "session_id": "starting-0", "hook_event_name": "PreToolUse",
                  "tool_name": "spawn_agent", "tool_input": {
                      "message": "SYMPHONY_ROLE: assessor\nTask for starting-0",
                      "model": CODEX_STRONGEST, "reasoning_effort": "high",
                  }}
        result = self.output(handle(oldest, self.environ))
        after = store.load(self.project)

        self.assertNotEqual(result.get("decision"), "block", result.get("reason"))
        self.assertEqual(after.activation["codex"]["profile"], CODEX_FULL["id"])
        self.assertIn("codex:starting-0", after.active_runs)
        self.assertNotIn("starting-0", after.activation["codex"]["pending_sessions"])
        handle({**self.payload("$symphony:symphony stop"), "session_id": "starting-1"}, self.environ)
        self.assertNotIn("starting-1", store.load(self.project).activation["codex"]["pending_sessions"])

    def test_one_shot_start_does_not_enable_project(self):
        result = handle(self.payload("$symphony:symphony start Check the release"), self.environ)
        self.assertIn("assess", self.context(result).lower())
        self.assertIn("Check the release", self.context(result))

        self.open_run("Check the release")
        state = StateStore(self.state_root).load(self.project)
        self.assertFalse(state.enabled)
        self.assertEqual(state.active_run.task, "Check the release")

    def test_bypass_does_not_mutate_enablement_or_active_run(self):
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(enabled=True))
        result = handle(self.payload("$symphony:symphony bypass Explain this file"), self.environ)
        state = store.load(self.project)
        self.assertTrue(state.enabled)
        self.assertIsNone(state.active_run)
        self.assertIn("outside symphony", self.context(result).lower())

    def test_status_records_current_hook_heartbeat_as_guarded(self):
        result = handle(self.payload("$symphony:symphony status"), self.environ)
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.activation["codex"]["state"], "guarded")
        self.assertEqual(state.activation["codex"]["session_id"], "codex-session")
        self.assertEqual(
            state.activation["codex"]["plugin_version"],
            __import__("plugins.symphony.symphony", fromlist=["x"]).PLUGIN_VERSION,
        )
        self.assertTrue(state.activation["codex"]["plugin_root"].endswith("plugins/symphony"))
        self.assertIn("guarded", self.context(result).lower())
        self.assertNotIn("unarmed", self.context(result).lower())

    def test_reconciled_completing_run_guides_provider_native_finalization(self):
        for provider, control in (("codex", "$symphony:symphony"),
                                  ("claude", "/symphony:")):
            with self.subTest(provider=provider):
                run = self.seed_run(RunState(
                    "run-ready", "task", status="completing", lead_identity="lead-1",
                    outcome={"status": "completed"},
                    delegations=(Delegation("lead-1", "lead", "task", "completed", "", ""),),
                ), provider=provider, enabled=True)
                environ = self.claude_environ if provider == "claude" else self.environ
                text = self.context(handle(self.payload(f"{control}status" if provider == "claude"
                                                        else f"{control} status", provider), environ))
                if provider == "claude":
                    self.assertIn(f"`{control}stop`", text)
                    self.assertIn("Invoke the normal", text)
                else:
                    self.assertIn("Finish this root turn now so the native Stop hook", text)
                    self.assertIn("Do not type a stop control as assistant prose", text)
                    self.assertNotIn("Invoke the normal", text)
                self.assertIn("then check durable status", text)
                if provider == "claude":
                    self.assertIn("Do not follow up or replace a completed lead", text)
                else:
                    self.assertIn("or spawn or follow up a completed lead", text)
                self.assertNotIn("tracked work still requires reconciliation", text)
                self.assertEqual("completing", StateStore(self.state_root).load(self.project)
                                 .active_runs[f"{provider}:{run.session_id}"].status)

                guidance = runtime_module._recovery_guidance(
                    ProjectState(active_run=run), provider)
                self.assertIn("Invoke the normal" if provider == "claude" else
                              "Finish this root turn", guidance)
                self.assertNotIn("continue unfinished work", guidance)

    def test_completing_run_with_unfinished_work_does_not_guide_stop(self):
        run = self.seed_run(RunState(
            "run-working", "task", status="completing", lead_identity="lead-1",
            outcome={"status": "completed"},
            delegations=(Delegation("lead-1", "lead", "task", "completed", "", ""),
                         Delegation("worker-1", "worker", "task", "working", "", "")),
        ), enabled=True)
        status = self.context(handle(self.payload("$symphony:symphony status"), self.environ))
        self.assertNotIn("Invoke the normal", status)
        self.assertIn("tracked work still requires reconciliation", status)
        guidance = runtime_module._recovery_guidance(ProjectState(active_run=run), "codex")
        self.assertIn("continue unfinished work", guidance)

    def test_status_names_agents_left_unreconciled_by_force_stop(self):
        run = RunState(
            "run-1", "task", session_id="codex-session",
            delegations=(Delegation("lead-1", "lead", "work", "working", "", ""),),
        )
        self.seed_run(run, enabled=True)

        handle(self.payload("$symphony:symphony stop --force"), self.environ)
        state = StateStore(self.state_root).load(self.project)
        status = self.context(handle(self.payload("$symphony:symphony status"), self.environ))

        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "force_stopped")
        self.assertEqual(state.recent_runs[-1].unreconciled, ("lead-1",))
        self.assertIn("lead-1", status)
        self.assertIn("never reconciled", status)

    def test_status_names_the_current_project_and_empty_run_scope(self):
        text = self.context(handle(self.payload("$symphony:symphony status"), self.environ))
        self.assertIn("Symphony (this project): disabled", text)
        self.assertIn("Run (this session): none", text)

    def test_observed_role_requires_an_explicit_role_token(self):
        self.assertEqual(
            runtime_module._observed_role({"agent_type": "symphony_lead_gpt_5_high"}),
            "lead",
        )
        self.assertEqual(
            runtime_module._observed_role({"agent_type": "symphony_consultant_gpt_6_high"}),
            "consultant",
        )
        self.assertEqual(
            runtime_module._observed_role({"task_name": "/tmp/leader-not-an-agent"}),
            "",
        )
        self.assertEqual(
            runtime_module._observed_role({"role": "lead", "task_name": "consultant-not-role"}),
            "lead",
        )

    def test_status_does_not_call_a_historical_heartbeat_current(self):
        handle(self.payload("$symphony:symphony status"), self.environ)
        state = StateStore(self.state_root).load(self.project)
        text = runtime_module._status(state, False, "codex", "new-session")
        self.assertIn("pending verification", text)
        self.assertIn("historical heartbeat", text.lower())

    def test_reconciliation_ignores_malformed_health_roster(self):
        run = RunState(
            "run-1", "task", session_id="old-session",
            delegations=(Delegation("agent-1", "worker", "task", "working", "tier", "medium"),),
        )
        state = ProjectState(active_run=run)
        source = Event("event", "session_heartbeat", "2026-09-22T12:00:00+00:00")
        next_state, actions = runtime_module._reconcile_session(
            state, source,
            {"session_id": "new-session", "active_agent_ids": [None]},
        )
        self.assertEqual(next_state, state)
        self.assertEqual(actions, ())

    def test_claude_marker_uses_the_same_control_contract(self):
        result = handle(self.payload("SYMPHONY_CONTROL: enable", "claude"), self.environ)
        self.assertIn("enabled", self.context(result).lower())
        self.assertTrue(StateStore(self.state_root).load(self.project).enabled)

    def test_raw_claude_slash_command_is_applied_before_skill_expansion(self):
        result = handle(self.payload("/symphony:enable", "claude"), self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertTrue(state.enabled)
        self.assertIsNone(state.active_run)
        self.assertIn("enabled", self.context(result).lower())

    def test_raw_claude_start_preserves_the_full_task(self):
        result = handle(self.payload("/symphony:start Check the release safely", "claude"), self.environ)
        self.assertIn("Check the release safely", self.context(result))

        self.open_run("Check the release safely", "claude")
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.task, "Check the release safely")

    def test_claude_command_arguments_start_a_one_shot_run(self):
        prompt = "SYMPHONY_CONTROL: start\nARGUMENTS: Check the release"
        result = handle(self.payload(prompt, "claude"), self.environ)
        self.assertIn("assess", self.context(result).lower())

        self.open_run("Check the release", "claude")
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.task, "Check the release")

    def test_help_is_inert_and_uses_provider_native_syntax(self):
        result = handle(self.payload("$symphony:symphony help"), self.environ)
        text = self.context(result)
        self.assertIn("$symphony:symphony", text)
        self.assertNotIn("/symphony:start", text)
        self.assertIsNone(StateStore(self.state_root).load(self.project).active_run)

    def test_stop_blocks_when_host_observed_delegation_is_active(self):
        delegation = Delegation("w1", "worker", "work", "working", "balanced", "medium")
        run = RunState("run-1", "task", delegations=(delegation,))
        self.seed_run(run, enabled=True)
        payload = self.payload("")
        payload["hook_event_name"] = "Stop"

        result = handle(payload, self.environ)
        replay = handle(payload, self.environ)

        self.assertEqual(self.output(result)["decision"], "block")
        self.assertIn("w1", self.output(result)["reason"])
        self.assertIn("for this project", self.output(result)["reason"])
        self.assertEqual(self.output(replay)["decision"], "block")

    def test_host_observed_lead_completion_allows_normal_stop(self):
        self.open_run("Ship it")
        marker = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )
        handle(
            {
                **self.payload(""),
                "hook_event_name": "PreToolUse",
                "tool_name": "spawn_agent",
                "tool_input": {
                    "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                    "model": self.simple["model"],
                    "reasoning_effort": self.simple["effort"],
                },
            },
            self.environ,
        )
        started = self.payload("")
        started.update(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "lead-1",
                "agent_type": f"symphony_lead_{self.simple['model'].replace('-', '_')}_{self.simple['effort']}",
                "model": self.simple["model"],
                "model_reasoning_effort": self.simple["effort"],
            }
        )
        handle(started, self.environ)
        self.complete_substantive_worker()
        stopped = {**started, "hook_event_name": "SubagentStop", "status": "completed"}
        handle(stopped, self.environ)

        stop = self.payload("")
        stop["hook_event_name"] = "Stop"
        result = handle(stop, self.environ)
        state = StateStore(self.state_root).load(self.project)

        self.assertEqual(result.stdout, "")
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")

    def test_codex_lead_completion_waits_for_accepted_assessment(self):
        self.open_run("Ship it")
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": f"symphony_lead_{self.simple['model'].replace('-', '_')}_high",
            "model": self.simple["model"],
            "model_reasoning_effort": "high",
        }
        handle(lead, self.environ)

        missing = handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )

        self.assertEqual(missing.stdout, "", "a stop result carries no injected context")
        self.assertIn("accepted assessment", self.flush().lower())
        self.assertIsNone(StateStore(self.state_root).load(self.project).active_run.outcome)

        assessor = {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "assessor-1",
            "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high"),
            "model": CODEX_STRONGEST, "model_reasoning_effort": "high",
        }
        handle(assessor, self.environ)
        marker = json.dumps({"size": "small", "complexity": "simple", "risk": "normal",
                             "rationale": "bounded task", "topology": "direct"})
        handle({**assessor, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {marker}"}, self.environ)
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done at high effort"}, self.environ)
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertIsNone(state.active_run.outcome)
        self.assertIn("observed", self.output(handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ))["reason"])

    def test_codex_lead_completion_waits_for_matrix_effort(self):
        self.open_run("Ship it")
        assessor = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": "symphony_assessor_gpt_6_high",
            "model_reasoning_effort": "high",
        }
        handle(assessor, self.environ)
        assessment = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )
        handle(
            {
                **assessor,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {assessment}",
            },
            self.environ,
        )
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "symphony_lead_gpt_5_high",
            "model_reasoning_effort": "high",
        }
        handle(lead, self.environ)

        result = handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )

        self.assertEqual(result.stdout, "", "a stop result carries no injected context")
        self.assertIn(f"{self.simple['model']}/{self.simple['effort']}".lower(), self.flush().lower())
        recovering = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual(recovering.status, "recovering")
        replacement = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-2",
            "agent_type": f"symphony_lead_{self.simple['model'].replace('-', '_')}_{self.simple['effort']}",
            "model": self.simple["model"],
            "model_reasoning_effort": self.simple["effort"],
        }
        handle(replacement, self.environ)
        self.complete_substantive_worker()
        replaced = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual(replaced.lead_identity, "lead-2")
        self.assertEqual(replaced.owner_generation, 2)
        handle(
            {
                **replacement,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "completing")
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual(
            StateStore(self.state_root).load(self.project).recent_runs[-1].outcome["status"],
            "completed",
        )

    def test_avalon_assessment_guides_and_enforces_resolved_effort(self):
        self.open_run("Implement the Avalon task")
        assessor = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high"),
            "model": CODEX_STRONGEST,
            "model_reasoning_effort": "high",
        }
        handle(assessor, self.environ)
        marker = json.dumps({
            "size": "small", "complexity": "mixed", "risk": "normal",
            "rationale": "bounded Avalon task", "topology": "direct",
        })
        handle({**assessor, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {marker}"}, self.environ)
        expected = resolve_tier(route_for(Assessment("small", "mixed")), snapshot_for("codex", CODEX_FULL["id"]))
        guidance = self.flush()
        self.assertIn(f"Selected codex lead ({CODEX_FULL['id']} profile)", guidance)
        self.assertIn(f"{expected['lead_model']}/{expected['lead_effort']}", guidance)

        def spawn(effort):
            return self.output(handle({
                **self.payload(""), "hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                "tool_input": {
                    "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nImplement the Avalon task",
                    "model": expected["lead_model"], "reasoning_effort": effort,
                },
            }, self.environ))

        wrong = "high" if expected["lead_effort"] != "high" else "medium"
        rejected = spawn(wrong)
        self.assertEqual(rejected["decision"], "block")
        self.assertIn(f"{expected['lead_model']}/{expected['lead_effort']}", rejected["reason"])
        self.assertIn(f"{expected['lead_model']}/{wrong}", rejected["reason"])
        wrong_lead = {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead-wrong",
            "agent_type": codex_agent_type("lead", expected["lead_model"], wrong),
            "model": expected["lead_model"], "model_reasoning_effort": wrong,
        }
        handle(wrong_lead, self.environ)
        handle({**wrong_lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done at the wrong effort"}, self.environ)
        blocked_stop = self.output(handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ))
        self.assertEqual(blocked_stop["decision"], "block")
        self.assertIn(f"expected {expected['lead_model']}/{expected['lead_effort']}", blocked_stop["reason"])
        self.assertIn(f"observed {expected['lead_model']}/{wrong}", blocked_stop["reason"])
        self.assertNotEqual(spawn(expected["lead_effort"]).get("decision"), "block")

        lead = {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead-1",
            "agent_type": codex_agent_type("lead", expected["lead_model"], expected["lead_effort"]),
            "model": expected["lead_model"], "model_reasoning_effort": expected["lead_effort"],
        }
        handle(lead, self.environ)
        self.complete_substantive_worker()
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Avalon task done"}, self.environ)
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "completing")
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual(StateStore(self.state_root).load(self.project).recent_runs[-1].status, "completed")

    def test_stale_lead_stop_cannot_block_completed_current_lead(self):
        choice = route_choice("small", "mixed")
        model, effort = choice["model"], choice["effort"]
        run = RunState(
            "run-1", "task", status="completing", lead_identity="current",
            assessment={
                "size": "small", "complexity": "mixed", "risk": "normal",
                "route": {"lead_model": model, "lead_effort": effort},
                "_lead_expected_route": {"identity": "current", "model": model, "effort": effort},
            },
            outcome={"status": "completed"},
            delegations=(
                Delegation("current", "lead", "task", "completed", model, effort),
                Delegation("old", "lead", "task", "failed", model, "high"),
                Delegation("worker", "worker", "task", "working", model, effort),
            ),
        )
        self.seed_run(run)
        stale = {**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "old",
                 "status": "completed", "model": model, "model_reasoning_effort": "high"}
        handle(stale, self.environ)
        self.assertNotIn("_lead_route_mismatch", StateStore(self.state_root).load(self.project).active_run.assessment)
        handle({**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "worker",
                "status": "completed"}, self.environ)
        stop = self.output(handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ))
        self.assertNotEqual(stop.get("decision"), "block")
        self.assertEqual(StateStore(self.state_root).load(self.project).recent_runs[-1].status, "completed")

    def test_every_profile_cell_and_risk_agrees_from_guidance_to_completion(self):
        for provider in ("codex", "claude"):
            for profile in profiles_for(provider):
                profile_id = profile["id"]
                for size, complexity in MATRIX:
                    for risk in ("normal", "high"):
                        with self.subTest(provider=provider, profile=profile_id,
                                          size=size, complexity=complexity, risk=risk):
                            self.project = self.root / f"{provider}-{profile_id}-{size}-{complexity}-{risk}"
                            self.project.mkdir()
                            self.session_override = f"{provider}-{profile_id}-{size}-{complexity}-{risk}"
                            self.environ["SYMPHONY_PROFILE"] = profile_id
                            self.claude_environ["SYMPHONY_PROFILE"] = profile_id
                            environ = self.claude_environ if provider == "claude" else self.environ
                            route = route_for(Assessment(size, complexity, risk))
                            expected = resolve_tier(route, snapshot_for(provider, profile_id))
                            model, effort = expected["lead_model"], expected["lead_effort"]
                            self.open_run("Execute the task", provider)
                            assessor_model = profile["tiers"]["strongest"]
                            assessor_type = (claude_agent_type("assessor", {"model": assessor_model, "effort": "high"})
                                             if provider == "claude" else codex_agent_type("assessor", assessor_model, "high"))
                            assessor = {
                                **self.payload("", provider), "hook_event_name": "SubagentStart",
                                "agent_id": "assessor-1", "agent_type": assessor_type,
                                "provider": provider,
                                "model": assessor_model, "model_reasoning_effort": "high",
                            }
                            handle(assessor, environ)
                            marker = json.dumps({
                                "size": size, "complexity": complexity, "risk": risk,
                                "rationale": "test matrix route", "topology": route.execution,
                            })
                            handle({**assessor, "hook_event_name": "SubagentStop", "status": "completed",
                                    "last_assistant_message": f"SYMPHONY_ASSESSMENT: {marker}"}, environ)
                            state = StateStore(self.state_root).load(self.project)
                            recorded = state.active_run.assessment["route"]
                            self.assertEqual((recorded["lead_model"], recorded["lead_effort"]), (model, effort))
                            guidance = self.flush(provider)
                            self.assertIn(f"Selected {provider} lead ({profile_id} profile): {model}/{effort}", guidance)

                            proceed = "/symphony:proceed" if provider == "claude" else "$symphony:symphony proceed"
                            handle(self.payload(proceed, provider), environ)
                            if provider == "claude":
                                lead_type = claude_agent_type("lead", {"model": model, "effort": effort})
                                tool_name = "Agent"
                                tool_input = {
                                    "prompt": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nExecute the task",
                                    "subagent_type": lead_type,
                                }
                            else:
                                lead_type = codex_agent_type("lead", model, effort)
                                tool_name = "spawn_agent"
                                tool_input = {
                                    "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nExecute the task",
                                    "model": model, "reasoning_effort": effort,
                                }
                            prepared = self.output(handle({
                                **self.payload("", provider), "hook_event_name": "PreToolUse",
                                "tool_name": tool_name, "tool_input": tool_input,
                            }, environ))
                            self.assertNotEqual(prepared.get("decision"), "block", prepared.get("reason"))
                            lead = {
                                **self.payload("", provider), "hook_event_name": "SubagentStart",
                                "agent_id": "lead-1", "agent_type": lead_type,
                                "provider": provider,
                                "model": model, "model_reasoning_effort": effort,
                            }
                            handle(lead, environ)
                            self.complete_substantive_worker(provider, environ)
                            handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                                    "last_assistant_message": "Completed"}, environ)
                            completing = StateStore(self.state_root).load(self.project)
                            self.assertEqual(completing.active_run.status, "completing")
                            handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                            completed = StateStore(self.state_root).load(self.project)
                            self.assertIsNone(completed.active_run)
                            self.assertEqual(completed.recent_runs[-1].status, "completed")

    def test_codex_native_lifecycle_accepts_assessment_and_lead_outcome(self):
        self.open_run("Return OK")

        assessor_transcript = self.root / "assessor.jsonl"
        assessor_transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"agent_path": f"/root/{codex_agent_type('assessor', CODEX_STRONGEST, 'high')}"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {"model": CODEX_STRONGEST, "effort": "high"},
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )
        assessor = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": "default",
            "transcript_path": str(assessor_transcript),
        }
        handle(assessor, self.environ)
        observed_assessor = StateStore(self.state_root).load(self.project).active_run.delegations[-1]
        self.assertEqual(observed_assessor.role, "assessor")
        self.assertEqual(observed_assessor.requested_effort, "high")
        assessment = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded read-only response",
                "topology": "direct",
            }
        )
        handle(
            {
                **assessor,
                "hook_event_name": "SubagentStop",
                "transcript_path": "",
                "agent_transcript_path": str(assessor_transcript),
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {assessment}",
            },
            self.environ,
        )

        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.assessment["size"], "small")
        self.assertEqual(state.active_run.assessment["complexity"], "simple")
        self.assertEqual(state.active_run.assessment["route"]["lead_model"], self.simple["model"])
        self.assertEqual(state.active_run.assessment["route"]["lead_effort"], self.simple["effort"])

        lead_transcript = self.root / "lead.jsonl"
        lead_transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"agent_path": f"/root/symphony_lead_{self.simple['model'].replace('-', '_')}_{self.simple['effort']}"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {"model": self.simple["model"], "effort": self.simple["effort"]},
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "default",
            "transcript_path": str(lead_transcript),
        }
        handle(lead, self.environ)
        self.complete_substantive_worker()
        handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "transcript_path": "",
                "agent_transcript_path": str(lead_transcript),
                "last_assistant_message": "OK",
            },
            self.environ,
        )

        stop = {**self.payload(""), "hook_event_name": "Stop"}
        self.assertEqual(handle(stop, self.environ).stdout, "")
        completed = StateStore(self.state_root).load(self.project).recent_runs[-1]
        self.assertEqual(completed.outcome["status"], "completed")
        self.assertNotIn("message", completed.outcome)

    def test_codex_low_effort_assessor_result_is_not_accepted(self):
        handle({**self.payload(""), "hook_event_name": "SessionStart"}, self.environ)
        assessor = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": "symphony_assessor_gpt_5_low",
            "model_reasoning_effort": "low",
        }
        handle(assessor, self.environ)
        assessment = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )

        result = handle(
            {
                **assessor,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {assessment}",
            },
            self.environ,
        )

        self.assertEqual(result.stdout, "", "a stop result carries no injected context")
        self.assertIn("high effort", self.flush().lower())
        self.assertNotIn("size", StateStore(self.state_root).load(self.project).active_run.assessment)

    def test_codex_stop_can_fill_missing_start_metadata_from_child_transcript(self):
        environ = {**self.environ, "SYMPHONY_PROVIDER": "codex"}
        self.open_run("Ship it")
        start = {
            "session_id": "codex-session",
            "cwd": str(self.project),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": "default",
        }
        handle(start, environ)
        transcript = self.root / "late-assessor.jsonl"
        transcript.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"agent_path": f"/root/symphony_assessor_{CODEX_STRONGEST.replace('-', '_')}_high"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {"model": CODEX_STRONGEST, "effort": "high"},
                        }
                    ),
                )
            ),
            encoding="utf-8",
        )
        assessment = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )

        handle(
            {
                **start,
                "hook_event_name": "SubagentStop",
                "agent_transcript_path": str(transcript),
                "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {assessment}",
            },
            environ,
        )

        state = StateStore(self.state_root).load(self.project)
        assessor = state.active_run.delegations[-1]
        self.assertEqual((assessor.requested_tier, assessor.requested_effort), (CODEX_STRONGEST, "high"))
        self.assertEqual(state.active_run.assessment["size"], "small")

    def test_failed_lead_stays_recoverable_and_stop_remains_guarded(self):
        self.open_run("Ship it")
        started = self.payload("")
        started.update(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "lead-1",
                "agent_type": "symphony_lead",
            }
        )
        handle(started, self.environ)
        handle({**started, "hook_event_name": "SubagentStop", "status": "failed"}, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertEqual(state.active_run.delegations[-1].state, "failed")
        self.assertIn("safe replacement", self.flush())
        stop = {**self.payload(""), "hook_event_name": "Stop"}
        self.assertEqual(self.output(handle(stop, self.environ))["decision"], "block")

    def test_delayed_failed_terminal_cannot_replace_a_newer_working_lead_turn(self):
        run = RunState(
            "run-1", "task", session_id="codex-session", provider="codex",
            lead_identity="lead-1", assessment={
                "size": "small", "complexity": "simple",
                "_terminal_turns": {"lead-1": ("turn_id:old",)},
            },
            delegations=(Delegation("lead-1", "lead", "task", "working",
                                    self.simple["model"], self.simple["effort"]),),
        )
        state = ProjectState(active_run=run, active_runs={"codex:codex-session": run})
        common = {"provider": "codex", "session_id": "codex-session",
                  "agent_id": "lead-1", "agent_type": codex_agent_type(
                      "lead", self.simple["model"], self.simple["effort"]),
                  "model": self.simple["model"],
                  "model_reasoning_effort": self.simple["effort"]}
        for new_token, old_already_terminal in (("new", True), ("", True),
                                                  ("new", False)):
            with self.subTest(new_token=new_token, old_already_terminal=old_already_terminal):
                prior = state if old_already_terminal else replace(
                    state, active_run=replace(state.active_run,
                        assessment={"size": "small", "complexity": "simple"}))
                started, _ = runtime_module._observe_delegation(prior, Event(
                    f"new-start-{new_token}-{old_already_terminal}", "subagent_started",
                    "2026-09-30T00:00:02+00:00",
                    {**common, **({"turn_id": new_token} if new_token else {})},
                ))
                guarded, actions = runtime_module._observe_delegation(started, Event(
                    f"late-failed-{new_token}-{old_already_terminal}", "subagent_stopped",
                    "2026-09-30T00:00:03+00:00",
                    {**common, "turn_id": "old", "status": "failed",
                     "last_assistant_message": "changed retry metadata"},
                ))
                self.assertEqual((), actions)
                self.assertEqual("working", guarded.active_run.delegations[0].state)
                self.assertEqual("lead-1", guarded.active_run.lead_identity)
                self.assertEqual("active", guarded.active_run.status)
                if old_already_terminal:
                    self.assertEqual(started, guarded)
                else:
                    self.assertEqual(("lead-1",), guarded.active_run.assessment[
                        "_ambiguous_child_stops"])
                    untagged, _ = runtime_module._observe_delegation(guarded, Event(
                        "untagged-complete", "subagent_stopped",
                        "2026-09-30T00:00:04+00:00",
                        {**common, "status": "completed",
                         "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'},
                    ))
                    self.assertEqual("active", untagged.active_run.status)
                    self.assertEqual(("lead-1",), untagged.active_run.assessment[
                        "_ambiguous_child_stops"])

    def test_codex_followup_terminal_can_arrive_without_another_start_hook(self):
        self.seed_run(RunState(
            "run-1", "task", session_id="codex-session", provider="codex",
            lead_identity="lead-1", assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("lead-1", "lead", "task", "working",
                                    self.simple["model"], self.simple["effort"]),),
        ))
        packet = {**self.payload(""), "agent_id": "lead-1",
                  "agent_type": codex_agent_type("lead", self.simple["model"],
                                                 self.simple["effort"]),
                  "model": self.simple["model"],
                  "model_reasoning_effort": self.simple["effort"]}
        handle({**packet, "hook_event_name": "SubagentStart", "turn_id": "old"},
               self.environ)
        handle({**packet, "hook_event_name": "SubagentStop", "turn_id": "old",
                "status": "completed", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"blocked"}'}, self.environ)
        first = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual("recovering", first.status)
        self.assertNotIn("lead-1", first.assessment.get("_active_turns", {}))
        guidance = runtime_module._recovery_guidance(
            StateStore(self.state_root).load(self.project), "codex")
        self.assertIn("finish this root turn so native Stop", guidance)
        self.assertIn("missing text marker alone never authorizes replacement", guidance)
        handle({**packet, "hook_event_name": "SubagentStop", "turn_id": "new",
                "status": "completed", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        second = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual("completing", second.status)
        self.assertEqual("lead-1", second.lead_identity)

    def test_same_text_result_after_a_new_start_is_a_new_lifecycle(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                run = RunState(
                    "run", "task", session_id="session", provider=provider, lead_identity="lead",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead", "lead", "task", "working", "", ""),
                                 Delegation("worker", "worker", "task", "working", "", "")),
                )
                state = ProjectState(active_run=run)
                base = {"provider": provider, "session_id": "session", "agent_id": "worker",
                        "last_assistant_message": "Done."}
                for event, extra in (("SubagentStop", {"status": "completed"}),
                                     ("SubagentStart", {"status": "working"}),
                                     ("SubagentStop", {"status": "completed"})):
                    state, _ = runtime_module._observe_delegation(
                        state, event_from_payload(provider, {**base, **extra, "hook_event_name": event}))
                worker = next(item for item in state.active_run.delegations if item.identity == "worker")
                self.assertEqual("completed", worker.state)

    def test_identical_no_id_restart_keeps_outcome_unreconciled(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                run = RunState(
                    "run", "task", session_id="session", provider=provider, lead_identity="lead",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead", "lead", "task", "working", "", ""),
                                 Delegation("worker", "worker", "task", "working", "", "")),
                )
                state = ProjectState(active_run=run)
                base = {"provider": provider, "session_id": "session", "agent_id": "worker",
                        "last_assistant_message": "Done."}
                start = event_from_payload(provider, {**base, "hook_event_name": "SubagentStart"})
                stop = event_from_payload(provider, {**base, "hook_event_name": "SubagentStop",
                                                     "status": "completed"})
                for event in (start, stop, start):
                    state, _ = runtime_module._observe_delegation(state, event)
                worker = next(item for item in state.active_run.delegations if item.identity == "worker")
                self.assertEqual("interrupted", worker.state)
                self.assertIn("worker", state.active_run.assessment["_ambiguous_child_starts"])

    def test_identified_old_result_cannot_replay_into_a_new_child_turn(self):
        for provider, field in (("codex", "turn_id"), ("claude", "prompt_id")):
            with self.subTest(provider=provider):
                run = RunState(
                    "run", "task", session_id="session", provider=provider, lead_identity="lead",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead", "lead", "task", "working", "", ""),
                                 Delegation("worker", "worker", "task", "working", "", "")),
                )
                state = ProjectState(active_run=run)
                base = {"provider": provider, "session_id": "session", "agent_id": "worker",
                        "last_assistant_message": "Done."}

                def observe(name, token, status):
                    nonlocal state
                    state, _ = runtime_module._observe_delegation(state, event_from_payload(
                        provider, {**base, field: token, "hook_event_name": name, "status": status}))

                observe("SubagentStop", "turn-one", "completed")
                observe("SubagentStart", "turn-one", "working")  # delayed old start
                self.assertEqual("completed", next(item.state for item in state.active_run.delegations
                                                  if item.identity == "worker"))
                observe("SubagentStart", "turn-two", "working")
                observe("SubagentStart", "turn-one", "working")  # delayed after new turn began
                observe("SubagentStop", "turn-two", "failed")
                observe("SubagentStart", "turn-two", "working")  # duplicate start
                observe("SubagentStop", "turn-one", "completed")  # stale old result
                self.assertEqual("failed", next(item.state for item in state.active_run.delegations
                                               if item.identity == "worker"))
                observe("SubagentStop", "turn-two", "completed")
                self.assertEqual("completed", next(item.state for item in state.active_run.delegations
                                                  if item.identity == "worker"))

    def test_terminal_retry_can_correct_explicit_role_evidence(self):
        run = RunState(
            "run", "task", session_id="session", provider="codex", lead_identity="lead",
            assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("lead", "lead", "task", "working", "", ""),
                         Delegation("worker", "worker", "task", "working", "", "")),
        )
        state = ProjectState(active_run=run)
        base = {"provider": "codex", "session_id": "session", "agent_id": "worker",
                "turn_id": "turn-one", "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done."}
        for role in ("worker", "consultant"):
            state, _ = runtime_module._observe_delegation(
                state, event_from_payload("codex", {**base, "role": role}))
        worker = next(item for item in state.active_run.delegations if item.identity == "worker")
        self.assertEqual("consultant", worker.role)

    def test_same_lead_followup_recovers_without_another_start_hook(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        stopped = {**self.payload(""), "hook_event_name": "SubagentStop",
                   "agent_id": "lead", "agent_type": "default"}
        # Native Codex followup_task emits another SubagentStop for the same
        # identity, without a second SubagentStart.
        handle({**stopped, "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"blocked"}'}, self.environ)
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "recovering")
        self.assertIn("tracked lead has no reconciled outcome",
                      self.output(handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ))["reason"])

        # Native followup_task can end again with identical prose, but its
        # new turn_id makes this a new result rather than a replay.
        handle({**stopped, "turn_id": "turn-2",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"blocked"}'}, self.environ)
        self.assertEqual(2, len(StateStore(self.state_root).load(self.project)
                                .active_run.assessment["_terminal_event_ids"]))

        handle({**stopped, "turn_id": "turn-3",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "completing")
        self.assertEqual(state.active_run.outcome, {"status": "completed"})
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        archived = StateStore(self.state_root).load(self.project)
        self.assertIsNone(archived.active_run)
        self.assertEqual(archived.recent_runs[-1].outcome, {"status": "completed"})

    def test_child_worktree_terminal_updates_original_root_project(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        child = self.root / "other-worktree"
        child.mkdir()
        terminal = {**self.payload(""), "cwd": str(child), "hook_event_name": "SubagentStop",
                    "agent_id": "lead", "turn_id": "lead-turn-1", "agent_type": "default",
                    "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}

        handle(terminal, self.environ)

        store = StateStore(self.state_root)
        self.assertEqual("completing", store.load(self.project).active_run.status)
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertIsNone(store.load(self.project).active_run)
        self.assertEqual("completed", store.load(self.project).recent_runs[-1].status)
        self.assertFalse(store._path(child).exists())

    def test_unresolved_legacy_owner_queues_terminal_and_replays_at_root_stop(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        child = self.root / "other-worktree"
        child.mkdir()
        terminal = {**self.payload(""), "cwd": str(child), "hook_event_name": "SubagentStop",
                    "agent_id": "lead", "turn_id": "lead-turn-1", "agent_type": "default",
                    "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        store = StateStore(self.state_root)
        with patch.object(StateStore, "active_owner_paths", side_effect=[None, (store._path(self.project),)]):
            handle(terminal, self.environ)
            self.assertEqual("active", store.load(self.project).active_run.status)
            queued = store.session_record("codex", "codex-session")
            self.assertEqual(1, len(queued["pending"]))
            stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)

        self.assertNotEqual("block", self.output(stop).get("decision"))
        self.assertIsNone(store.load(self.project).active_run)
        self.assertEqual([], store.session_record("codex", "codex-session")["pending"])

    def test_claude_pre_run_unmarked_callbacks_do_not_block_managed_stop(self):
        choice = route_choice(provider="claude")
        self.seed_run(RunState(
            "managed-run", "task", status="completing", lead_identity="managed-lead",
            assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("managed-lead", "lead", "task", "completed",
                                    choice["model"], choice["effort"]),),
            outcome={"status": "completed"}, started_at="2026-10-01T05:05:20+00:00",
        ), provider="claude")
        store = StateStore(self.state_root)
        for kind, observed_at in (
            ("subagent_started", "2026-10-01T05:05:04+00:00"),
            ("subagent_stopped", "2026-10-01T05:05:07+00:00"),
        ):
            event = Event(
                f"generic:{kind}", kind, observed_at,
                {"session_id": "claude-session", "agent_id": "generic-child",
                 "prompt_id": "generic-prompt", "agent_type": ""},
            )
            with store.session_lock("claude", "claude-session"):
                store.queue_session_event("claude", "claude-session", event,
                                          ambiguous_owner=True)

        with patch.object(runtime_module, "claude_recovered_lead_event", return_value=None), \
             patch.object(runtime_module, "claude_completing_lead_turn",
                          return_value=("complete", None)):
            stop = handle({**self.payload("", "claude"), "hook_event_name": "Stop"},
                          self.claude_environ)

        self.assertNotEqual("block", self.output(stop).get("decision"))
        self.assertEqual([], store.session_record("claude", "claude-session")["pending"])
        self.assertIsNone(store.load(self.project).active_run)

    def test_claude_pre_run_managed_child_conflict_still_blocks_stop(self):
        choice = route_choice(provider="claude")
        self.seed_run(RunState(
            "managed-run", "task", status="completing", lead_identity="managed-lead",
            assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("managed-lead", "lead", "task", "completed",
                                    choice["model"], choice["effort"]),),
            outcome={"status": "completed"}, started_at="2026-10-01T05:05:20+00:00",
        ), provider="claude")
        store = StateStore(self.state_root)
        event = Event(
            "managed:restart", "subagent_started", "2026-10-01T05:05:04+00:00",
            {"session_id": "claude-session", "agent_id": "managed-lead",
             "prompt_id": "new-lead-prompt", "agent_type": ""},
        )
        with store.session_lock("claude", "claude-session"):
            store.queue_session_event("claude", "claude-session", event,
                                      ambiguous_owner=True)

        stop = handle({**self.payload("", "claude"), "hook_event_name": "Stop"},
                      self.claude_environ)

        self.assertEqual("block", self.output(stop).get("decision"))
        self.assertEqual(1, len(store.session_record("claude", "claude-session")["pending"]))
        self.assertIsNotNone(store.load(self.project).active_run)

    def test_claude_pre_run_marked_child_conflict_still_blocks_stop(self):
        choice = route_choice(provider="claude")
        self.seed_run(RunState(
            "managed-run", "task", status="completing", lead_identity="managed-lead",
            assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("managed-lead", "lead", "task", "completed",
                                    choice["model"], choice["effort"]),),
            outcome={"status": "completed"}, started_at="2026-10-01T05:05:20+00:00",
        ), provider="claude")
        store = StateStore(self.state_root)
        event = Event(
            "managed:marked-start", "subagent_started", "2026-10-01T05:05:04+00:00",
            {"session_id": "claude-session", "agent_id": "unknown-child",
             "prompt_id": "marked-prompt", "agent_type": "symphony:symphony-worker",
             "task": "SYMPHONY_ROLE: worker"},
        )
        with store.session_lock("claude", "claude-session"):
            store.queue_session_event("claude", "claude-session", event,
                                      ambiguous_owner=True)

        stop = handle({**self.payload("", "claude"), "hook_event_name": "Stop"},
                      self.claude_environ)

        self.assertEqual("block", self.output(stop).get("decision"))
        self.assertEqual(1, len(store.session_record("claude", "claude-session")["pending"]))
        self.assertIsNotNone(store.load(self.project).active_run)

    def test_claude_pre_run_malformed_agent_type_is_not_discarded(self):
        run = self.seed_run(RunState(
            "managed-run", "task", started_at="2026-10-01T05:05:20+00:00",
        ), provider="claude")
        event = Event(
            "malformed:start", "subagent_started", "2026-10-01T05:05:04+00:00",
            {"session_id": "claude-session", "agent_id": "unknown-child",
             "agent_type": "symphony:symphony-unknown",
             "_symphony_owner_conflict": True},
        )
        state = StateStore(self.state_root).load(self.project)

        self.assertEqual("hold", runtime_module._pending_child_disposition(
            state, event, "claude", run.session_id))

    def test_child_session_inbox_is_drained_by_its_verified_root_stop(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        child = {**self.payload(""), "session_id": "lead", "parent_thread_id": "codex-session",
                 "hook_event_name": "SubagentStop", "agent_id": "lead", "turn_id": "lead-turn-1",
                 "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        with patch.object(StateStore, "active_owner_paths", return_value=None):
            handle(child, self.environ)
        self.assertEqual(1, len(store.session_record("codex", "lead")["pending"]))

        root_stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)

        self.assertNotEqual("block", self.output(root_stop).get("decision"))
        self.assertIsNone(store.load(self.project).active_run)
        self.assertEqual([], store.session_record("codex", "lead")["pending"])

    def test_child_alias_with_conflicting_parent_cannot_complete_root(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        child = {**self.payload(""), "session_id": "lead", "parent_thread_id": "foreign-root",
                 "hook_event_name": "SubagentStop", "agent_id": "lead", "turn_id": "lead-turn-1",
                 "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        with patch.object(StateStore, "active_owner_paths", return_value=None):
            handle(child, self.environ)

        root_stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", self.output(root_stop).get("decision"))
        self.assertEqual("active", StateStore(self.state_root).load(self.project).active_run.status)

    def test_pending_worker_restart_precedes_queued_lead_completion_across_inboxes(self):
        choice = route_choice()
        run = RunState("root-run", "task", lead_identity="lead", status="active",
                       assessment={"size": "small", "complexity": "simple"},
                       delegations=(
                           Delegation("lead", "lead", "task", "working",
                                      choice["model"], choice["effort"]),
                           Delegation("worker", "worker", "task", "completed",
                                      choice["model"], choice["effort"]),
                       ))
        self.seed_run(run)
        store = StateStore(self.state_root)
        worker = event_from_payload("codex", {
            **self.payload(""), "session_id": "worker", "parent_thread_id": "codex-session",
            "hook_event_name": "SubagentStart", "agent_id": "worker", "turn_id": "new-worker-turn",
            "agent_type": codex_agent_type("worker", choice["model"], choice["effort"]),
        })
        lead = event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
            "turn_id": "lead-terminal-turn", "last_assistant_message":
            'SYMPHONY_OUTCOME: {"status":"completed"}',
        })
        with store.session_lock("codex", "worker"):
            store.queue_session_event("codex", "worker", worker)
        with store.session_lock("codex", "codex-session"):
            store.queue_session_event("codex", "codex-session", lead)

        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)

        self.assertEqual("block", self.output(stop).get("decision"))
        state = store.load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual("working", next(item.state for item in state.active_run.delegations
                                          if item.identity == "worker"))
        self.assertEqual([], store.session_record("codex", "worker")["pending"])

    def test_child_lead_terminal_waits_for_root_pending_worker_restart(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(
                                   Delegation("lead", "lead", "task", "working",
                                              choice["model"], choice["effort"]),
                                   Delegation("worker", "worker", "task", "completed",
                                              choice["model"], choice["effort"]),
                               )))
        store = StateStore(self.state_root)
        restart = event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "worker",
            "turn_id": "worker-turn-two", "agent_type":
            codex_agent_type("worker", choice["model"], choice["effort"]),
        })
        with store.session_lock("codex", "codex-session"):
            store.queue_session_event("codex", "codex-session", restart)
        handle({**self.payload(""), "session_id": "lead", "parent_thread_id": "codex-session",
                "hook_event_name": "SubagentStop", "agent_id": "lead", "turn_id": "lead-turn-one",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        self.assertIsNotNone(store.load(self.project).active_run)
        self.assertEqual(1, len(store.session_record("codex", "lead")["pending"]))

        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", self.output(stop).get("decision"))
        run = store.load(self.project).active_run
        self.assertEqual("working", next(item.state for item in run.delegations
                                          if item.identity == "worker"))

    def test_pending_new_lead_turn_invalidates_earlier_queued_completion(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        completed = event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
            "turn_id": "lead-turn-one", "last_assistant_message":
            'SYMPHONY_OUTCOME: {"status":"completed"}',
        })
        restarted = event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead",
            "turn_id": "lead-turn-two", "agent_type":
            codex_agent_type("lead", choice["model"], choice["effort"]),
        })
        with store.session_lock("codex", "codex-session"):
            store.queue_session_event("codex", "codex-session", completed)
            store.queue_session_event("codex", "codex-session", restarted)

        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)

        self.assertEqual("block", self.output(stop).get("decision"))
        run = store.load(self.project).active_run
        self.assertIsNotNone(run)
        self.assertIsNone(run.outcome)
        self.assertEqual("working", next(item.state for item in run.delegations
                                          if item.identity == "lead"))

    def test_current_lead_start_is_inside_pending_completion_batch(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        completed = event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
            "turn_id": "lead-turn-one", "last_assistant_message":
            'SYMPHONY_OUTCOME: {"status":"completed"}',
        })
        with store.session_lock("codex", "codex-session"):
            store.queue_session_event("codex", "codex-session", completed)
        handle({**self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead",
                "turn_id": "lead-turn-two", "agent_type":
                codex_agent_type("lead", choice["model"], choice["effort"])}, self.environ)

        run = store.load(self.project).active_run
        self.assertIsNotNone(run)
        self.assertIsNone(run.outcome)
        self.assertEqual("working", next(item.state for item in run.delegations
                                          if item.identity == "lead"))

    def test_current_older_terminal_precedes_newer_queued_start(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        newer_start = replace(event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead",
            "turn_id": "lead-turn-two", "agent_type":
            codex_agent_type("lead", choice["model"], choice["effort"]),
        }), observed_at="2026-09-29T02:03:00+00:00")
        with store.session_lock("codex", "codex-session"):
            store.queue_session_event("codex", "codex-session", newer_start)
        old_terminal = replace(event_from_payload("codex", {
            **self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
            "turn_id": "lead-turn-one", "last_assistant_message":
            'SYMPHONY_OUTCOME: {"status":"completed"}',
        }), observed_at="2026-09-29T02:02:00+00:00")
        with patch.object(runtime_module, "event_from_payload", return_value=old_terminal):
            handle(old_terminal.payload, self.environ)

        run = store.load(self.project).active_run
        self.assertIsNotNone(run)
        self.assertIsNone(run.outcome)
        self.assertEqual("working", next(item.state for item in run.delegations
                                          if item.identity == "lead"))

    def test_queued_old_failed_terminal_cannot_replace_newer_same_lead_turn(self):
        choice = route_choice()
        for old_already_terminal in (True, False):
            with self.subTest(old_already_terminal=old_already_terminal):
                assessment = {"size": "small", "complexity": "simple"}
                if old_already_terminal:
                    assessment["_terminal_turns"] = {"lead": ("turn_id:old",)}
                self.seed_run(RunState(
                    "root-run", "task", lead_identity="lead", status="active",
                    assessment=assessment,
                    delegations=(Delegation("lead", "lead", "task", "working",
                                            choice["model"], choice["effort"]),),
                ))
                store = StateStore(self.state_root)
                handle({**self.payload(""), "hook_event_name": "SubagentStart",
                        "agent_id": "lead", "turn_id": "new", "agent_type":
                        codex_agent_type("lead", choice["model"], choice["effort"])}, self.environ)
                failed = event_from_payload("codex", {
                    **self.payload(""), "hook_event_name": "SubagentStop",
                    "agent_id": "lead", "turn_id": "old", "status": "failed",
                    "last_assistant_message": "different retry result",
                })
                with store.session_lock("codex", "codex-session"):
                    store.queue_session_event("codex", "codex-session", failed)
                    if not old_already_terminal:
                        untagged = event_from_payload("codex", {
                            **self.payload(""), "hook_event_name": "SubagentStop",
                            "agent_id": "lead", "turn_id": "", "status": "completed",
                            "last_assistant_message":
                            'SYMPHONY_OUTCOME: {"status":"completed"}',
                        })
                        store.queue_session_event("codex", "codex-session", untagged)
                stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
                run = store.load(self.project).active_run
                self.assertEqual("block", self.output(stop).get("decision"))
                self.assertEqual("lead", run.lead_identity)
                self.assertEqual("active", run.status)
                self.assertEqual("working", run.delegations[0].state)
                self.assertEqual("turn_id:new", run.assessment["_active_turns"]["lead"])
                self.assertEqual(bool(run.assessment.get("_ambiguous_child_stops")),
                                 not old_already_terminal)

    def test_disabled_session_without_managed_history_ignores_ordinary_child_start(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                environ = self.claude_environ if provider == "claude" else self.environ
                session = f"ordinary-{provider}"
                project = self.root / provider
                project.mkdir()
                state = ProjectState(enabled=False)
                store = StateStore(self.state_root)
                store.save(project, state)
                base = {"session_id": session, "cwd": str(project)}
                handle({**base, "hook_event_name": "SessionStart"}, environ)
                handle({**base, "hook_event_name": "SubagentStart", "agent_id": "explore-child",
                        "parent_thread_id": session, "agent_type": "Explore",
                        "prompt_id": "ordinary-prompt"}, environ)
                record = store.session_record(provider, session)
                self.assertFalse(record and record["pending"])
                stop = handle({**base, "hook_event_name": "Stop"}, environ)
                self.assertNotEqual("block", self.output(stop).get("decision"))

    def test_new_child_alias_is_discovered_before_it_enters_root_roster(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple",
                                           "_pending_delegations": [{"role": "worker"}]},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        child_start = {**self.payload(""), "session_id": "new-worker", "parent_thread_id": "codex-session",
                       "hook_event_name": "SubagentStart", "agent_id": "new-worker",
                       "turn_id": "new-worker-turn", "agent_type":
                       codex_agent_type("worker", choice["model"], choice["effort"])}
        handle(child_start, self.environ)
        store = StateStore(self.state_root)
        self.assertEqual(1, len(store.session_record("codex", "new-worker")["pending"]))
        handle({**self.payload("$symphony:symphony status"), "session_id": "codex-session"},
               self.environ)
        run = store.load(self.project).active_run
        self.assertIn("new-worker", {item.identity for item in run.delegations})
        self.assertEqual([], store.session_record("codex", "new-worker")["pending"])

    def test_busy_owner_lookup_still_discovers_new_child_inbox(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple",
                                           "_pending_delegations": [{"role": "worker"}]},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        child_start = {**self.payload(""), "session_id": "new-worker", "parent_thread_id": "lead",
                       "hook_event_name": "SubagentStart", "agent_id": "new-worker",
                       "turn_id": "new-worker-turn", "agent_type":
                       codex_agent_type("worker", choice["model"], choice["effort"])}
        with patch.object(StateStore, "active_owner_paths", return_value=None):
            handle(child_start, self.environ)
        store = StateStore(self.state_root)
        self.assertEqual(1, len(store.session_record("codex", "new-worker")["pending"]))
        self.assertIn("new-worker", store.aliases_for_owner("codex", "lead"))

        handle({**self.payload("$symphony:symphony status"), "session_id": "codex-session"},
               self.environ)
        run = store.load(self.project).active_run
        self.assertIn("new-worker", {item.identity for item in run.delegations})
        self.assertEqual([], store.session_record("codex", "new-worker")["pending"])

    def test_shared_lead_id_cannot_claim_unbound_child_from_other_project(self):
        choice = route_choice()
        lead = Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"])
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"_pending_delegations": [{"role": "worker"}]},
                               delegations=(lead,)))
        other = self.root / "other-project"
        other.mkdir()
        other_run = RunState("other-run", "task", session_id="other-root", provider="codex",
                             lead_identity="lead", status="active",
                             assessment={"_pending_delegations": [{"role": "worker"}]},
                             delegations=(lead,))
        store = StateStore(self.state_root)
        store.save(other, ProjectState(active_run=other_run,
                                       active_runs={"codex:other-root": other_run}))
        handle({**self.payload(""), "session_id": "new-worker", "parent_thread_id": "lead",
                "hook_event_name": "SubagentStart", "agent_id": "new-worker",
                "turn_id": "new-worker-turn", "agent_type":
                codex_agent_type("worker", choice["model"], choice["effort"])}, self.environ)
        self.assertEqual(1, len(store.session_record("codex", "new-worker")["pending"]))
        self.assertTrue(store.session_record("codex", "new-worker")["pending"][0]["ambiguous_owner"])

        first_stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        second_stop = handle({**self.payload(""), "cwd": str(other), "session_id": "other-root",
                              "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", self.output(first_stop).get("decision"))
        self.assertEqual("block", self.output(second_stop).get("decision"))
        self.assertEqual(1, len(store.session_record("codex", "new-worker")["pending"]))
        self.assertNotIn("new-worker", {item.identity for item in store.load(self.project).active_run.delegations})
        self.assertNotIn("new-worker", {item.identity for item in store.load(other).active_run.delegations})

    def test_shared_lead_id_within_one_project_keeps_child_event_unclaimed(self):
        choice = route_choice()
        lead = Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"])
        first = RunState("first-run", "task", session_id="first-root", provider="codex",
                         lead_identity="lead", status="active",
                         assessment={"_pending_delegations": [{"role": "worker"}]},
                         delegations=(lead,))
        second = replace(first, run_id="second-run", session_id="second-root")
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(active_run=first,
                   active_runs={"codex:first-root": first, "codex:second-root": second}))
        handle({**self.payload(""), "session_id": "new-worker", "parent_thread_id": "lead",
                "hook_event_name": "SubagentStart", "agent_id": "new-worker",
                "turn_id": "new-worker-turn", "agent_type":
                codex_agent_type("worker", choice["model"], choice["effort"])}, self.environ)
        self.assertTrue(store.session_record("codex", "new-worker")["pending"][0]["ambiguous_owner"])

        for root in ("first-root", "second-root"):
            stop = handle({**self.payload(""), "session_id": root,
                           "hook_event_name": "Stop"}, self.environ)
            self.assertEqual("block", self.output(stop).get("decision"))
        self.assertEqual(1, len(store.session_record("codex", "new-worker")["pending"]))
        for root in ("first-root", "second-root"):
            self.assertNotIn("new-worker", {item.identity for item in
                             store.load(self.project).active_runs[f"codex:{root}"].delegations})

    def test_settled_bound_root_cannot_complete_foreign_root_in_same_project(self):
        choice = route_choice()
        handle({**self.payload(""), "session_id": "settled-root",
                "hook_event_name": "SessionStart"}, self.environ)
        lead = Delegation("foreign-lead", "lead", "task", "working",
                          choice["model"], choice["effort"])
        foreign = RunState("foreign-run", "task", session_id="foreign-root", provider="codex",
                           lead_identity="foreign-lead", status="active",
                           assessment={"size": "small", "complexity": "simple"},
                           delegations=(lead,))
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(active_run=foreign,
                                             active_runs={"codex:foreign-root": foreign}))
        handle({**self.payload(""), "session_id": "foreign-root",
                "hook_event_name": "SessionStart"}, self.environ)
        terminal = {**self.payload(""), "session_id": "settled-root",
                    "parent_thread_id": "foreign-root", "hook_event_name": "SubagentStop",
                    "agent_id": "foreign-lead", "turn_id": "foreign-turn",
                    "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        handle(terminal, self.environ)
        self.assertEqual("active", store.load(self.project).active_runs["codex:foreign-root"].status)
        pending = store.session_record("codex", "settled-root")["pending"]
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0]["ambiguous_owner"])

        stop = handle({**self.payload(""), "session_id": "settled-root",
                       "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", self.output(stop).get("decision"))
        self.assertEqual(1, len(store.session_record("codex", "settled-root")["pending"]))
        self.assertEqual("active", store.load(self.project).active_runs["codex:foreign-root"].status)
        self.assertNotIn("settled-root", store.aliases_for_owner("codex", "foreign-root"))

        store.save(self.project, ProjectState(recent_runs=(replace(foreign, status="completed"),)))
        foreign_stop = handle({**self.payload(""), "session_id": "foreign-root",
                               "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", self.output(foreign_stop).get("decision"))

    def test_completed_lead_terminal_replay_does_not_leave_pending_child(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                handle({**self.payload("", provider), "hook_event_name": "SessionStart"}, environ)
                terminal = {**self.payload("", provider),
                            "hook_event_name": "SubagentStop", "agent_id": "finished-lead",
                            "parent_thread_id": session, "status": "completed",
                            "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
                if provider == "codex":
                    terminal["turn_id"] = "finished-turn"
                result_id = runtime_module._terminal_result_id(
                    event_from_payload(provider, terminal))
                choice = route_choice(provider=provider)
                finished = RunState(
                    "finished-run", "task", status="completed", session_id=session,
                    provider=provider, lead_identity="finished-lead",
                    assessment={"_terminal_event_ids": (result_id,)},
                    delegations=(Delegation("finished-lead", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                    outcome={"status": "completed"},
                )
                store = StateStore(self.state_root)
                store.save(self.project, ProjectState(recent_runs=(finished,)))
                handle(terminal, environ)
                self.assertEqual([], store.session_record(provider, session)["pending"])
                replay = event_from_payload(provider, terminal)
                store.queue_session_event(provider, session, replay)
                stop = self.output(handle({**self.payload("", provider),
                                           "hook_event_name": "Stop"}, environ))
                self.assertNotEqual("block", stop.get("decision"), stop)
                self.assertEqual([], store.session_record(provider, session)["pending"])
                self.assertEqual("completed", store.load(self.project).recent_runs[-1].status)

    def test_archived_start_replay_cannot_block_its_root_or_sibling(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                session = f"{provider}-archived-root"
                sibling = f"{provider}-active-root"
                environ = self.claude_environ if provider == "claude" else self.environ
                handle({**self.payload("", provider), "session_id": session,
                        "hook_event_name": "SessionStart"}, environ)
                handle({**self.payload("", provider), "session_id": sibling,
                        "hook_event_name": "SessionStart"}, environ)
                start = {**self.payload("", provider), "session_id": session,
                         "hook_event_name": "SubagentStart", "agent_id": "old-lead",
                         "parent_thread_id": session, "role": "lead"}
                start["turn_id" if provider == "codex" else "prompt_id"] = "old-invocation"
                start_event = event_from_payload(provider, start)
                alias_start = {**start, "session_id": "old-lead"}
                alias_event = event_from_payload(provider, alias_start)
                choice = route_choice(provider=provider)
                finished = RunState(
                    "old-run", "task", status="completed", session_id=session,
                    provider=provider, lead_identity="old-lead",
                    assessment={"_start_event_ids": (start_event.event_id,
                                                     alias_event.event_id)},
                    delegations=(Delegation("old-lead", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                    outcome={"status": "completed"},
                )
                active = RunState("sibling-run", "task", status="active",
                                  session_id=sibling, provider=provider,
                                  lead_identity="sibling-lead")
                store = StateStore(self.state_root)
                initial = ProjectState(active_run=active,
                                       active_runs={f"{provider}:{sibling}": active},
                                       recent_runs=(finished,))
                store.save(self.project, initial)
                baseline = store.load(self.project)

                handle(start, environ)
                self.assertEqual([], store.session_record(provider, session)["pending"])
                # A crash may leave the same exact start in an older inbox.
                store.queue_session_event(provider, session, start_event,
                                          ambiguous_owner=True)
                stop = self.output(handle({**self.payload("", provider),
                                           "session_id": session,
                                           "hook_event_name": "Stop"}, environ))
                self.assertNotEqual("block", stop.get("decision"), stop)
                self.assertEqual([], store.session_record(provider, session)["pending"])
                # The same committed start can arrive through a child-session
                # alias after the original run has archived.
                for ambiguous in (False, True):
                    store.queue_session_event(provider, "old-lead", alias_event,
                                              ambiguous_owner=ambiguous)
                    stop = self.output(handle({**self.payload("", provider),
                                               "session_id": session,
                                               "hook_event_name": "Stop"}, environ))
                    self.assertNotEqual("block", stop.get("decision"), stop)
                    self.assertEqual([], store.session_record(provider, "old-lead")["pending"])
                state = store.load(self.project)
                self.assertEqual(baseline.active_runs, state.active_runs)
                self.assertEqual(baseline.recent_runs, state.recent_runs)

                # The same bytes can be a genuine start in a newer run when
                # a host omits turn identity. It must change that run rather
                # than inherit the prior completion.
                no_id_start = {key: value for key, value in start.items()
                               if key not in {"turn_id", "prompt_id"}}
                no_id_event = event_from_payload(provider, no_id_start)
                old_with_no_id = replace(finished, assessment={
                    "_start_event_ids": (start_event.event_id, no_id_event.event_id)})
                new = replace(old_with_no_id, run_id="new-run", status="completing",
                              assessment={}, outcome={"status": "completed"})
                store.save(self.project, ProjectState(
                    active_run=new,
                    active_runs={f"{provider}:{session}": new,
                                 f"{provider}:{sibling}": active},
                    recent_runs=(old_with_no_id,),
                ))
                handle(no_id_start, environ)
                restarted = store.load(self.project).active_runs[f"{provider}:{session}"]
                self.assertEqual("active", restarted.status)
                self.assertIsNone(restarted.outcome)

    def test_unidentified_archived_assessor_start_is_not_silently_discarded(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                session = f"{provider}-unidentified-root"
                environ = self.claude_environ if provider == "claude" else self.environ
                handle({**self.payload("", provider), "session_id": session,
                        "hook_event_name": "SessionStart"}, environ)
                start = {**self.payload("", provider), "session_id": session,
                         "hook_event_name": "SubagentStart", "agent_id": "assessor",
                         "parent_thread_id": session, "role": "assessor"}
                start.pop("turn_id", None)
                event = event_from_payload(provider, start)
                finished = RunState(
                    "old-run", "task", status="completed", session_id=session,
                    provider=provider,
                    assessment={"_start_event_ids": (event.event_id,)},
                    delegations=(Delegation("assessor", "assessor", "task", "completed",
                                            "model", "high"),),
                    outcome={"status": "completed"},
                )
                store = StateStore(self.state_root)
                store.save(self.project, ProjectState(recent_runs=(finished,)))
                handle(start, environ)
                self.assertIn(f"{provider}:{session}",
                              store.load(self.project).active_runs)

    def test_completed_lead_remains_owned_until_root_stop_for_native_followups(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        terminal = {**self.payload(""), "hook_event_name": "SubagentStop",
                    "agent_id": "lead", "parent_thread_id": "codex-session",
                    "model": choice["model"], "model_reasoning_effort": choice["effort"]}
        handle({**terminal, "turn_id": "lead-turn-one",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        first = store.load(self.project)
        self.assertEqual("completing", first.active_runs["codex:codex-session"].status)
        self.assertEqual([], store.session_record("codex", "codex-session")["pending"])

        handle({**terminal, "turn_id": "lead-turn-two", "status": "blocked",
                "last_assistant_message": "Waiting for a follow-up result."}, self.environ)
        self.assertEqual("recovering", store.load(self.project)
                         .active_runs["codex:codex-session"].status)
        handle({**terminal, "turn_id": "lead-turn-three",
                "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        final = store.load(self.project)
        self.assertEqual("completing", final.active_runs["codex:codex-session"].status)
        self.assertEqual([], store.session_record("codex", "codex-session")["pending"])
        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", self.output(stop).get("decision"))
        archived = store.load(self.project)
        self.assertNotIn("codex:codex-session", archived.active_runs)
        self.assertEqual("completed", archived.recent_runs[-1].status)

    def test_root_resume_preserves_completed_lead_until_stop(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        handle({**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
                "status": "completed", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        handle({**self.payload(""), "hook_event_name": "SessionStart", "source": "resume"}, self.environ)
        resumed = store.load(self.project)
        self.assertEqual("completing", resumed.active_runs["codex:codex-session"].status)
        self.assertEqual({"status": "completed"}, resumed.active_run.outcome)
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        archived = store.load(self.project)
        self.assertNotIn("codex:codex-session", archived.active_runs)
        self.assertEqual("completed", archived.recent_runs[-1].status)

    def test_claude_resume_guides_same_agent_message_for_unreconciled_lead(self):
        choice = route_choice(provider="claude")
        run = self.seed_run(RunState(
            "original", "task", lead_identity="lead-1", status="active",
            assessment={"size": "small", "complexity": "simple"},
            delegations=(Delegation("lead-1", "lead", "task", "working",
                                    choice["model"], choice["effort"]),)), provider="claude")
        resumed = handle({**self.payload("", "claude"), "hook_event_name": "SessionStart",
                          "source": "resume"}, self.claude_environ)
        guidance = self.context(resumed)
        self.assertIn("SendMessage", guidance)
        self.assertIn("to: lead-1", guidance)
        self.assertIn("same agent", guidance)
        self.assertIn("SYMPHONY_OUTCOME", guidance)
        self.assertEqual(run.run_id, StateStore(self.state_root).load(self.project)
                         .active_runs["claude:claude-session"].run_id)

        completed = replace(run, status="completing", outcome={"status": "completed"},
                            delegations=(replace(run.delegations[0], state="completed"),))
        self.assertNotIn("SendMessage", runtime_module._recovery_guidance(
            ProjectState(active_run=completed), "claude"))

    def test_explicit_graceful_stop_shares_native_freshness_gate(self):
        choice = route_choice()
        run = RunState("run", "task", lead_identity="lead", status="completing",
                       outcome={"status": "completed"},
                       delegations=(Delegation("lead", "lead", "task", "completed",
                                               choice["model"], choice["effort"]),))
        self.seed_run(run)
        with patch.object(runtime_module, "codex_completing_lead_turn",
                          return_value=("running", None)):
            blocked = handle(self.payload("$symphony:symphony stop"), self.environ)
        self.assertEqual("block", self.output(blocked)["decision"])
        self.assertIn("newer native turn still running", self.output(blocked)["reason"])
        store = StateStore(self.state_root)
        self.assertEqual("completing", store.load(self.project).active_run.status)
        with patch.object(runtime_module, "codex_completing_lead_turn",
                          return_value=("complete", None)):
            allowed = handle({**self.payload("$symphony:symphony stop"),
                              "turn_id": "turn-2"}, self.environ)
        self.assertNotEqual("block", self.output(allowed).get("decision"))
        archived = store.load(self.project)
        self.assertIsNone(archived.active_run)
        self.assertEqual("completed", archived.recent_runs[-1].status)

    def test_completing_run_denies_new_lead_spawn_but_preserves_other_work(self):
        marker = ('SYMPHONY_ROUTE: {"size":"small","complexity":"simple",'
                  '"risk":"normal","rationale":"test","topology":"direct"}')
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                original = RunState(
                    "original", "task", session_id=f"{provider}-session", provider=provider,
                    lead_identity="lead-1", status="completing", outcome={"status": "completed"},
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead-1", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                )
                sibling = replace(original, run_id="sibling", session_id="other-session",
                                  status="assessed", lead_identity=None, outcome=None,
                                  delegations=())
                StateStore(self.state_root).save(self.project, ProjectState(
                    enabled=True, active_run=original,
                    activation={provider: {"profile": (CLAUDE_FULL if provider == "claude"
                                                       else CODEX_FULL)["id"]}},
                    active_runs={f"{provider}:{original.session_id}": original,
                                 f"{provider}:other-session": sibling}))
                environ = self.claude_environ if provider == "claude" else self.environ

                def spawn(session):
                    hook = {**self.payload("", provider), "session_id": session,
                            "hook_event_name": "PreToolUse",
                            "tool_name": "Agent" if provider == "claude" else "spawn_agent"}
                    if provider == "claude":
                        hook["tool_input"] = {"subagent_type":
                            f"symphony-lead-{choice['model']}-{choice['effort']}",
                            "prompt": f"SYMPHONY_ROLE: lead\n{marker}\nDo task"}
                    else:
                        hook["tool_input"] = {"message": f"SYMPHONY_ROLE: lead\n{marker}\nDo task",
                                              "model": choice["model"],
                                              "reasoning_effort": choice["effort"]}
                    if provider == "claude":
                        with patch.object(runtime_module, "_snapshot",
                                          return_value=snapshot_for(provider, CLAUDE_FULL["id"])), \
                             patch.object(runtime_module, "_clamp_actions", return_value=()):
                            return handle(hook, environ)
                    return handle(hook, environ)

                denied = spawn(original.session_id)
                decision = self.output(denied)
                if provider == "claude":
                    self.assertEqual("deny", decision["hookSpecificOutput"]["permissionDecision"])
                    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
                    sibling_decision = self.output(spawn("other-session"))
                    self.assertNotEqual("deny", sibling_decision.get("hookSpecificOutput", {})
                                        .get("permissionDecision"))
                else:
                    self.assertEqual("block", decision.get("decision"))
                    reason = decision.get("reason", "")
                    self.assertNotEqual("block", self.output(spawn("other-session")).get("decision"))
                self.assertIn("stop", reason.lower())
                stored = StateStore(self.state_root).load(self.project)
                self.assertEqual("completing", stored.active_runs[f"{provider}:{original.session_id}"].status)
                self.assertEqual("assessed", stored.active_runs[f"{provider}:other-session"].status)

                recovering = replace(original, status="recovering", outcome=None)
                StateStore(self.state_root).save(self.project, ProjectState(
                    enabled=True, active_run=recovering,
                    activation={provider: {"profile": (CLAUDE_FULL if provider == "claude"
                                                       else CODEX_FULL)["id"]}},
                    active_runs={f"{provider}:{original.session_id}": recovering}))
                recovery_decision = self.output(spawn(original.session_id))
                self.assertNotEqual("block", recovery_decision.get("decision"))
                self.assertNotEqual("deny", recovery_decision.get("hookSpecificOutput", {})
                                    .get("permissionDecision"))
                if provider == "codex":
                    followup = {**self.payload("", provider), "hook_event_name": "PreToolUse",
                                "tool_name": "followup_task", "tool_input": {"target": "lead-1"}}
                    self.assertNotEqual("block", self.output(handle(followup, environ)).get("decision"))

    def test_rejected_native_lead_start_and_stop_do_not_join_completing_run(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                original = RunState(
                    "original", "task", session_id="owner", provider=provider,
                    lead_identity="lead-1", status="completing", outcome={"status": "completed"},
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead-1", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                )
                sibling = replace(original, run_id="sibling", session_id="other",
                                  status="assessed", lead_identity=None, outcome=None,
                                  delegations=())
                state = ProjectState(active_run=original, active_runs={
                    f"{provider}:owner": original, f"{provider}:other": sibling})
                extra = {"provider": provider, "session_id": "owner", "agent_id": "lead-2",
                         "agent_type": (codex_agent_type("lead", choice["model"], choice["effort"])
                                        if provider == "codex" else claude_agent_type("lead", choice)),
                         "parent_thread_id": "owner", "turn_id": "new-turn"}
                for event, status in (("SubagentStart", "working"),
                                      ("SubagentStop", "completed")):
                    state, actions = runtime_module._observe_delegation(
                        state, event_from_payload(provider, {**extra, "hook_event_name": event,
                                                            "status": status,
                                                            "last_assistant_message":
                                                            'SYMPHONY_OUTCOME: {"status":"completed"}'}))
                    run = state.active_run
                    self.assertEqual("completing", run.status)
                    self.assertEqual(original.outcome, run.outcome)
                    self.assertEqual("lead-1", run.lead_identity)
                    self.assertEqual(["lead-1"], [item.identity for item in run.delegations
                                                  if item.role == "lead"])
                    rejected = next(item for item in run.delegations if item.identity == "lead-2")
                    self.assertEqual("rejected_lead", rejected.role)
                    self.assertEqual(status, rejected.state)
                    if event == "SubagentStart":
                        self.assertIn("reject_lead_replacement",
                                      {action.kind for action in actions})
                        self.assertIsNotNone(runtime_module._stop_block_reason(run))
                    else:
                        self.assertIsNone(runtime_module._stop_block_reason(run))
                        self.assertEqual("lead-2", state.terminal_receipts[-1]["agent"])
                    self.assertEqual(sibling, state.active_runs[f"{provider}:other"])
                same, same_actions = runtime_module._observe_delegation(
                    state, event_from_payload(provider, {**extra, "agent_id": "lead-1",
                                                        "turn_id": "followup-turn",
                                                        "hook_event_name": "SubagentStart"}))
                self.assertNotIn("reject_lead_replacement",
                                 {action.kind for action in same_actions})
                self.assertEqual("lead-1", same.active_run.lead_identity)
                recovering = replace(original, status="recovering", outcome=None)
                replaced, recovery_actions = runtime_module._observe_delegation(
                    replace(state, active_run=recovering),
                    event_from_payload(provider, {**extra, "hook_event_name": "SubagentStart"}))
                self.assertNotIn("reject_lead_replacement",
                                 {action.kind for action in recovery_actions})
                self.assertEqual("lead-2", replaced.active_run.lead_identity)

                if provider == "codex":
                    retryable = replace(
                        recovering,
                        assessment={**recovering.assessment, "_retryable_lead": "lead-1"},
                    )
                    guarded, guarded_actions = runtime_module._observe_delegation(
                        replace(state, active_run=retryable),
                        event_from_payload(provider, {**extra, "hook_event_name": "SubagentStart",
                                                      "turn_id": "replacement-turn"}),
                    )
                    self.assertEqual("lead-1", guarded.active_run.lead_identity)
                    self.assertEqual(retryable.owner_generation, guarded.active_run.owner_generation)
                    self.assertEqual("recovering", guarded.active_run.status)
                    self.assertEqual("lead-1", guarded.active_run.assessment["_retryable_lead"])
                    self.assertEqual("rejected_lead", next(
                        item.role for item in guarded.active_run.delegations
                        if item.identity == "lead-2"
                    ))
                    self.assertIn("reject_lead_replacement",
                                  {action.kind for action in guarded_actions})
                    self.assertIsNotNone(runtime_module._stop_block_reason(guarded.active_run))
                    settled, _ = runtime_module._observe_delegation(
                        guarded,
                        event_from_payload(provider, {**extra, "hook_event_name": "SubagentStop",
                                                      "turn_id": "replacement-turn",
                                                      "status": "failed"}),
                    )
                    self.assertEqual("lead-1", settled.active_run.lead_identity)
                    self.assertEqual("recovering", settled.active_run.status)
                    self.assertEqual("lead-1", settled.active_run.assessment["_retryable_lead"])
                    self.assertEqual("failed", next(
                        item.state for item in settled.active_run.delegations
                        if item.identity == "lead-2"
                    ))
                    self.assertEqual(sibling, settled.active_runs["codex:other"])

    def test_route_mismatch_guidance_does_not_retry_same_claude_lead(self):
        run = RunState(
            "run", "task", session_id="owner", provider="claude",
            lead_identity="lead-1", status="recovering", owner_generation=2,
            assessment={
                "_lead_route_mismatch": "wrong route",
                "_lead_route_mismatch_owner": {"identity": "lead-1", "generation": 2},
            },
            delegations=(Delegation("lead-1", "lead", "task", "failed", "wrong", "low"),),
        )
        guidance = runtime_module._recovery_guidance(ProjectState(active_run=run), "claude")
        self.assertIn("spawn one replacement", guidance)
        self.assertNotIn("SendMessage", guidance)

    def test_rejected_inflight_lead_consumes_pending_launch_and_unblocks_after_terminal(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                pending = {"role": "lead", "model": choice["model"],
                           "effort": choice["effort"], "objective": "second launch"}
                run = RunState(
                    "run", "task", session_id="owner", provider=provider,
                    lead_identity="lead-1", status="completing", outcome={"status": "completed"},
                    assessment={"size": "small", "complexity": "simple",
                                "_pending_delegations": (pending,)},
                    delegations=(Delegation("lead-1", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                )
                state = ProjectState(active_run=run)
                base = {"provider": provider, "session_id": "owner", "agent_id": "late-lead",
                        "agent_type": ("default" if provider == "codex"
                                       else claude_agent_type("lead", choice)),
                        "parent_thread_id": "owner"}
                for event, status in (("SubagentStart", "working"),
                                      ("SubagentStop", "completed")):
                    state, _ = runtime_module._observe_delegation(
                        state, event_from_payload(provider, {**base, "hook_event_name": event,
                                                            "status": status}))
                    self.assertNotIn("_pending_delegations", state.active_run.assessment)
                    self.assertEqual("lead-1", state.active_run.lead_identity)
                    self.assertEqual({"status": "completed"}, state.active_run.outcome)
                self.assertIsNone(runtime_module._stop_block_reason(state.active_run))

    def test_rejected_native_lead_failure_does_not_invalidate_original_outcome(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                run = RunState(
                    "original", "task", session_id="owner", provider=provider,
                    lead_identity="lead-1", status="completing", outcome={"status": "completed"},
                    delegations=(Delegation("lead-1", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                )
                state = ProjectState(active_run=run)
                base = {"provider": provider, "session_id": "owner", "agent_id": "lead-2",
                        "agent_type": "symphony-lead-gpt-6-luna-low" if provider == "codex"
                                      else "symphony:symphony-lead-claude-sonnet-5-low",
                        "parent_thread_id": "owner"}
                for name, status in (("SubagentStart", "working"),
                                     ("SubagentStop", "failed")):
                    state, _ = runtime_module._observe_delegation(
                        state, event_from_payload(provider, {**base, "hook_event_name": name,
                                                            "status": status}))
                self.assertEqual("completing", state.active_run.status)
                self.assertEqual({"status": "completed"}, state.active_run.outcome)
                self.assertEqual("lead-1", state.active_run.lead_identity)
                self.assertEqual("failed", next(item.state for item in state.active_run.delegations
                                                if item.identity == "lead-2"))
                self.assertIsNone(runtime_module._stop_block_reason(state.active_run))

    def test_native_verification_exception_blocks_managed_stop_only(self):
        for provider, evidence_function in (
            ("codex", "codex_recovered_lead_event"),
            ("codex", "codex_completing_lead_turn"),
            ("claude", "claude_recovered_lead_event"),
            ("claude", "claude_completing_lead_turn"),
        ):
            with self.subTest(provider=provider, evidence_function=evidence_function):
                choice = route_choice(provider=provider)
                self.seed_run(RunState(
                    "run", "task", lead_identity="lead", status="completing",
                    outcome={"status": "completed"},
                    delegations=(Delegation("lead", "lead", "task", "completed",
                                            choice["model"], choice["effort"]),),
                ), provider=provider)
                environ = self.claude_environ if provider == "claude" else self.environ
                stop = {**self.payload("", provider), "hook_event_name": "Stop"}
                with patch.object(runtime_module, evidence_function, side_effect=TypeError("bad native row")):
                    result = handle(stop, environ)
                self.assertEqual("block", self.output(result).get("decision"))
                self.assertEqual("completing", StateStore(self.state_root).load(self.project)
                                 .active_runs[f"{provider}:{provider}-session"].status)

        for provider, evidence_function in (("codex", "codex_recovered_lead_event"),
                                            ("claude", "claude_recovered_lead_event")):
            with self.subTest(provider=provider, disabled=True):
                StateStore(self.state_root).save(self.project, ProjectState(enabled=False))
                environ = self.claude_environ if provider == "claude" else self.environ
                with patch.object(runtime_module, evidence_function, side_effect=TypeError("bad native row")):
                    result = handle({**self.payload("", provider), "hook_event_name": "Stop"},
                                    environ)
                self.assertNotEqual("block", self.output(result).get("decision"))

    def test_claude_unknown_latest_turn_guides_same_lead_continuation(self):
        choice = route_choice(provider="claude")
        self.seed_run(RunState(
            "run", "task", lead_identity="original-lead", status="completing",
            outcome={"status": "completed"},
            delegations=(Delegation("original-lead", "lead", "task", "completed",
                                    choice["model"], choice["effort"]),),
        ), provider="claude")
        stop = {**self.payload("", "claude"), "hook_event_name": "Stop"}
        with patch.object(runtime_module, "claude_recovered_lead_event", return_value=None), \
             patch.object(runtime_module, "claude_completing_lead_turn",
                          return_value=("unknown", None)):
            decision = self.output(handle(stop, self.claude_environ))
        self.assertEqual("block", decision.get("decision"))
        reason = decision.get("reason", "")
        self.assertIn("latest native Claude turn", reason)
        self.assertIn("SendMessage", reason)
        self.assertIn("to: original-lead", reason)
        self.assertIn("at most one", reason)
        self.assertIn("do not repeat SendMessage or Stop", reason)
        self.assertIn("/symphony:stop", reason)
        self.assertEqual("completing", StateStore(self.state_root).load(self.project)
                         .active_runs["claude:claude-session"].status)

    def test_restarted_worker_failure_after_lead_completion_requires_recovery(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),
                                            Delegation("worker", "worker", "task", "completed",
                                                       "model", "high"))))
        store = StateStore(self.state_root)
        handle({**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
                "status": "completed", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)
        self.assertEqual("completing", store.load(self.project).active_run.status)
        handle({**self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "worker",
                "parent_thread_id": "codex-session", "role": "worker"}, self.environ)
        self.assertEqual("working", next(item.state for item in store.load(self.project)
                                         .active_run.delegations if item.identity == "worker"))
        handle({**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "worker",
                "parent_thread_id": "codex-session", "status": "failed"}, self.environ)
        recovering = store.load(self.project)
        self.assertEqual("recovering", recovering.active_run.status)
        self.assertIsNone(recovering.active_run.outcome)
        self.assertEqual("block", self.output(handle({**self.payload(""), "hook_event_name": "Stop"},
                                                     self.environ)).get("decision"))
        self.assertEqual((), store.load(self.project).recent_runs)

    def test_archived_lead_exemption_requires_recorded_result_and_no_new_run(self):
        session = "codex-session"
        handle({**self.payload(""), "hook_event_name": "SessionStart"}, self.environ)
        terminal = {**self.payload(""), "hook_event_name": "SubagentStop",
                    "agent_id": "same-lead", "parent_thread_id": session,
                    "status": "completed", "turn_id": "old-turn",
                    "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        result_id = runtime_module._terminal_result_id(event_from_payload("codex", terminal))
        model, effort = self.simple["model"], self.simple["effort"]
        lead = Delegation("same-lead", "lead", "task", "completed", model, effort)
        finished = RunState("old-run", "task", status="completed", session_id=session,
                            provider="codex", lead_identity="same-lead",
                            assessment={"_terminal_event_ids": (result_id,)},
                            delegations=(lead,), outcome={"status": "completed"})
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(recent_runs=(finished,)))

        changed = {**terminal, "turn_id": "new-turn"}
        handle(changed, self.environ)
        pending = store.session_record("codex", session)["pending"]
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0]["ambiguous_owner"])
        self.assertEqual("block", self.output(handle({**self.payload(""),
            "hook_event_name": "Stop"}, self.environ)).get("decision"))

        # A later run in the same root must observe a new terminal turn.
        store.finish_session_events(store.session_record("codex", session),
                                    {pending[0]["event_id"]})
        active = replace(finished, run_id="new-run", status="active", outcome=None,
                         assessment={}, delegations=(replace(lead, state="working"),))
        store.save(self.project, ProjectState(active_run=active,
                   active_runs={f"codex:{session}": active}, recent_runs=(finished,)))
        handle(changed, self.environ)
        new_state = store.load(self.project)
        self.assertIn(runtime_module._terminal_result_id(event_from_payload("codex", changed)),
                      new_state.active_runs[f"codex:{session}"].assessment.get(
                          "_terminal_event_ids", ()))
        self.assertEqual([], store.session_record("codex", session)["pending"])

    def test_late_worker_terminal_is_not_archived_lead_replay(self):
        session = "codex-session"
        handle({**self.payload(""), "hook_event_name": "SessionStart"}, self.environ)
        terminal = {**self.payload(""), "hook_event_name": "SubagentStop",
                    "agent_id": "worker", "parent_thread_id": session,
                    "status": "failed", "turn_id": "worker-turn"}
        committed = {**terminal, "status": "completed", "turn_id": "earlier-turn"}
        result_id = runtime_module._terminal_result_id(event_from_payload("codex", committed))
        run = RunState("done", "task", status="completed", session_id=session,
                       provider="codex", lead_identity="lead",
                       assessment={"_terminal_event_ids": (result_id,)},
                       delegations=(Delegation("lead", "lead", "task", "completed", "m", "e"),
                                    Delegation("worker", "worker", "task", "completed", "m", "e")),
                       outcome={"status": "completed"})
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(recent_runs=(run,)))
        handle(terminal, self.environ)
        self.assertEqual(1, len(store.session_record("codex", session)["pending"]))
        self.assertEqual("block", self.output(handle({**self.payload(""),
            "hook_event_name": "Stop"}, self.environ)).get("decision"))

    def test_old_terminal_retry_cannot_complete_new_run_with_same_lead(self):
        session = "codex-session"
        handle({**self.payload(""), "hook_event_name": "SessionStart"}, self.environ)
        model, effort = self.simple["model"], self.simple["effort"]
        old = {**self.payload(""), "hook_event_name": "SubagentStop",
               "agent_id": "shared-lead", "parent_thread_id": session,
               "status": "completed", "turn_id": "old-turn", "model": model,
               "model_reasoning_effort": effort,
               "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}
        result_id = runtime_module._terminal_result_id(event_from_payload("codex", old))
        lead = Delegation("shared-lead", "lead", "task", "completed", model, effort)
        archived = RunState("old-run", "task", status="completed", session_id=session,
                            provider="codex", lead_identity="shared-lead",
                            assessment={"_terminal_event_ids": (result_id,)},
                            delegations=(lead,), outcome={"status": "completed"})
        middle_result = runtime_module._terminal_result_id(event_from_payload(
            "codex", {**old, "turn_id": "middle-turn"}))
        middle = replace(archived, run_id="middle-run",
                         assessment={"_terminal_event_ids": (middle_result,)})
        fresh = RunState("new-run", "new task", status="active", session_id=session,
                         provider="codex", lead_identity="shared-lead",
                         assessment={"size": "small", "complexity": "simple",
                                     "route": {"lead_model": model, "lead_effort": effort},
                                     "_lead_expected_route": {"identity": "shared-lead",
                                                              "model": model, "effort": effort}},
                         delegations=(replace(lead, state="working"),))
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(active_run=fresh,
                   active_runs={f"codex:{session}": fresh}, recent_runs=(archived, middle)))

        handle({**old, "stop_hook_active": True}, self.environ)
        state = store.load(self.project)
        self.assertEqual("new-run", state.active_runs[f"codex:{session}"].run_id)
        self.assertIsNone(state.active_runs[f"codex:{session}"].outcome)
        self.assertEqual([], store.session_record("codex", session)["pending"])

        handle({**old, "turn_id": "new-turn"}, self.environ)
        state = store.load(self.project)
        self.assertEqual("completing", state.active_run.status)
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        state = store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(["old-run", "middle-run", "new-run"],
                         [run.run_id for run in state.recent_runs])

    def test_terminal_receipt_survives_visible_archive_eviction(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                model, effort = choice["model"], choice["effort"]
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                lead = Delegation("reused-lead", "lead", "task", "working", model, effort)
                old_run = RunState("old-run", "task", status="active", session_id=session,
                                   provider=provider, lead_identity="reused-lead",
                                   assessment={"size": "small", "complexity": "simple"},
                                   delegations=(lead,))
                self.seed_run(old_run, provider)
                old = {**self.payload("", provider), "hook_event_name": "SubagentStop",
                       "agent_id": "reused-lead", "parent_thread_id": session,
                       "status": "completed", "last_assistant_message":
                       'SYMPHONY_OUTCOME: {"status":"completed"}'}
                if provider == "codex":
                    old.update({"model": model, "model_reasoning_effort": effort,
                                "turn_id": "old-turn"})
                else:
                    old["prompt_id"] = "old-turn"
                handle(old, environ)
                store = StateStore(self.state_root)
                done = store.load(self.project)
                self.assertEqual("completing", done.active_run.status)
                handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                done = store.load(self.project)
                self.assertIsNone(done.active_run)
                self.assertEqual(1, len(done.terminal_receipts))
                fillers = tuple(RunState(f"filler-{i}", "filler", status="completed",
                                         session_id=f"other-{i}", provider=provider)
                                for i in range(21))
                new_run = replace(old_run, run_id="new-run", task="new task",
                                  assessment={"size": "small", "complexity": "simple",
                                              "route": {"lead_model": model, "lead_effort": effort},
                                              "_lead_expected_route": {"identity": "reused-lead",
                                                                       "model": model, "effort": effort}})
                store.save(self.project, replace(done, active_run=new_run,
                           active_runs={f"{provider}:{session}": new_run},
                           recent_runs=(*done.recent_runs, *fillers)))
                state = store.load(self.project)
                self.assertNotIn("old-run", {run.run_id for run in state.recent_runs})
                self.assertEqual(1, len(state.terminal_receipts))

                handle({**old, "stop_hook_active": True}, environ)
                self.assertEqual("new-run", store.load(self.project).active_run.run_id)
                self.assertEqual([], store.session_record(provider, session)["pending"])
                changed = {**old, "last_assistant_message": "changed result from old turn"}
                handle(changed, environ)
                pending = store.session_record(provider, session)["pending"]
                self.assertEqual(1, len(pending))
                self.assertEqual("block", self.output(handle({**self.payload("", provider),
                    "hook_event_name": "Stop"}, environ)).get("decision"))
                self.assertEqual("new-run", store.load(self.project).active_run.run_id)
                store.finish_session_events(store.session_record(provider, session),
                                            {pending[0]["event_id"]})
                token_field = "turn_id" if provider == "codex" else "prompt_id"
                handle({**old, token_field: "new-turn"}, environ)
                self.assertEqual("completing", store.load(self.project).active_run.status)
                handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                self.assertIsNone(store.load(self.project).active_run)

    def test_upgrade_preserves_available_legacy_terminal_lineage(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                lead = Delegation("legacy-lead", "lead", "task", "working",
                                  choice["model"], choice["effort"])
                old_run = RunState("legacy-run", "task", session_id=session,
                                   provider=provider, lead_identity="legacy-lead",
                                   assessment={"size": "small", "complexity": "simple"},
                                   delegations=(lead,))
                self.seed_run(old_run, provider)
                old = {**self.payload("", provider), "hook_event_name": "SubagentStop",
                       "agent_id": "legacy-lead", "parent_thread_id": session,
                       "status": "completed", "last_assistant_message":
                       'SYMPHONY_OUTCOME: {"status":"completed"}'}
                if provider == "codex":
                    old.update({"model": choice["model"],
                                "model_reasoning_effort": choice["effort"],
                                "turn_id": "legacy-turn"})
                else:
                    old["prompt_id"] = "legacy-turn"
                handle(old, environ)
                store = StateStore(self.state_root)
                path = store._path(self.project)
                raw = json.loads(path.read_text())
                raw.pop("terminal_receipts")  # A 1.5.1 state still had run assessment lineage.
                path.write_text(json.dumps(raw) + "\n")
                migrated = store.load(self.project)
                self.assertTrue(migrated.terminal_receipts)
                fillers = tuple(RunState(f"filler-{i}", "task", status="completed",
                                         session_id=f"other-{i}", provider=provider)
                                for i in range(21))
                current = replace(old_run, run_id="new-run", task="new task")
                store.save(self.project, replace(migrated, active_run=current,
                           active_runs={f"{provider}:{session}": current},
                           recent_runs=(*migrated.recent_runs, *fillers)))
                self.assertNotIn("legacy-run", {run.run_id for run in
                                               store.load(self.project).recent_runs})
                handle({**old, "stop_hook_active": True}, environ)
                self.assertEqual("new-run", store.load(self.project).active_run.run_id)

    def test_upgrade_preserves_active_legacy_terminal_evidence(self):
        run = RunState("active-legacy", "task", session_id="codex-session",
                       provider="codex", lead_identity="lead",
                       assessment={"_terminal_event_ids": ("a" * 64,),
                                   "_terminal_turns": {"lead": ("turn_id:old",)}},
                       delegations=(Delegation("lead", "lead", "task", "working",
                                               self.simple["model"], self.simple["effort"]),))
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(active_run=run,
                   active_runs={"codex:codex-session": run}))
        path = store._path(self.project)
        raw = json.loads(path.read_text())
        raw.pop("terminal_receipts")
        path.write_text(json.dumps(raw) + "\n")
        receipts = store.load(self.project).terminal_receipts
        self.assertTrue(any(item["result"] == "a" * 64 for item in receipts))
        self.assertTrue(any(item["agent"] == "lead" and item["turn"] == "turn_id:old"
                            for item in receipts))

    def test_overflow_only_child_alias_blocks_completion(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="completing",
                               outcome={"status": "completed"},
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "completed",
                                                       choice["model"], choice["effort"]),
                                            Delegation("worker", "worker", "task", "completed",
                                                       choice["model"], choice["effort"]))))
        store = StateStore(self.state_root)
        too_large = event_from_payload("codex", {
            **self.payload(""), "session_id": "worker", "parent_thread_id": "codex-session",
            "hook_event_name": "SubagentStop", "agent_id": "worker",
            "last_assistant_message": "x" * 140_000,
        })
        with store.session_lock("codex", "worker"):
            store.queue_session_event("codex", "worker", too_large)
        self.assertTrue(store.session_record("codex", "worker")["overflow"])
        self.assertEqual([], store.session_record("codex", "worker")["pending"])

        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual("block", self.output(stop).get("decision"))
        self.assertIsNotNone(store.load(self.project).active_run)

    def test_inbox_replay_after_committed_reducer_is_idempotent(self):
        choice = route_choice()
        self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        store = StateStore(self.state_root)
        terminal = {**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
                    "turn_id": "turn-1", "last_assistant_message":
                    'SYMPHONY_OUTCOME: {"status":"completed"}'}
        with patch.object(StateStore, "active_owner_paths", return_value=None):
            handle(terminal, self.environ)
        with patch.object(StateStore, "finish_session_events", side_effect=RuntimeError("after commit")):
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertIsNone(store.load(self.project).active_run)
        self.assertEqual(1, len(store.session_record("codex", "codex-session")["pending"]))

        stop = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertNotEqual("block", self.output(stop).get("decision"))
        self.assertEqual(1, len(store.load(self.project).recent_runs))
        self.assertEqual([], store.session_record("codex", "codex-session")["pending"])

    def test_alias_terminal_replay_after_commit_uses_root_session_hash(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                                       assessment={"size": "small", "complexity": "simple"},
                                       delegations=(Delegation("lead", "lead", "task", "working",
                                                               choice["model"], choice["effort"]),)), provider)
                terminal = {**self.payload("", provider), "session_id": "lead",
                            "parent_thread_id": session, "hook_event_name": "SubagentStop",
                            "agent_id": "lead", "last_assistant_message":
                            'SYMPHONY_OUTCOME: {"status":"completed"}'}
                with patch.object(StateStore, "active_owner_paths", return_value=None):
                    handle(terminal, environ)
                store = StateStore(self.state_root)
                with patch.object(StateStore, "finish_session_events", side_effect=RuntimeError("after commit")):
                    with self.assertRaisesRegex(RuntimeError, "after commit"):
                        handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                self.assertIsNone(store.load(self.project).active_run)
                self.assertEqual(1, len(store.session_record(provider, "lead")["pending"]))
                stop = self.output(handle({**self.payload("", provider),
                                           "hook_event_name": "Stop"}, environ))
                self.assertNotEqual("block", stop.get("decision"), stop)
                self.assertEqual([], store.session_record(provider, "lead")["pending"])

    def test_committed_worker_and_lead_batch_replays_after_ack_crash(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                                       assessment={"size": "small", "complexity": "simple"},
                                       delegations=(
                                           Delegation("lead", "lead", "task", "working",
                                                      choice["model"], choice["effort"]),
                                           Delegation("worker", "worker", "task", "working",
                                                      choice["model"], choice["effort"]),
                                       )), provider)
                store = StateStore(self.state_root)
                worker = event_from_payload(provider, {
                    **self.payload("", provider), "hook_event_name": "SubagentStop",
                    "agent_id": "worker", "status": "completed",
                })
                lead = event_from_payload(provider, {
                    **self.payload("", provider), "hook_event_name": "SubagentStop",
                    "agent_id": "lead", "last_assistant_message":
                    'SYMPHONY_OUTCOME: {"status":"completed"}',
                })
                with store.session_lock(provider, session):
                    store.queue_session_event(provider, session, worker)
                    store.queue_session_event(provider, session, lead)
                with patch.object(StateStore, "finish_session_events", side_effect=RuntimeError("after commit")):
                    with self.assertRaisesRegex(RuntimeError, "after commit"):
                        handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                self.assertIsNone(store.load(self.project).active_run)
                self.assertEqual(2, len(store.session_record(provider, session)["pending"]))
                stop = self.output(handle({**self.payload("", provider),
                                           "hook_event_name": "Stop"}, environ))
                self.assertNotEqual("block", stop.get("decision"))
                self.assertEqual([], store.session_record(provider, session)["pending"])

    def test_bound_parentless_alias_terminal_replays_after_ack_crash(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                choice = route_choice(provider=provider)
                session = f"{provider}-session"
                environ = self.claude_environ if provider == "claude" else self.environ
                self.seed_run(RunState("root-run", "task", lead_identity="lead", status="active",
                                       assessment={"size": "small", "complexity": "simple"},
                                       delegations=(Delegation("lead", "lead", "task", "working",
                                                               choice["model"], choice["effort"]),)), provider)
                terminal = {**self.payload("", provider), "session_id": "lead",
                            "hook_event_name": "SubagentStop", "agent_id": "lead",
                            "last_assistant_message":
                            'SYMPHONY_OUTCOME: {"status":"completed"}'}
                handle(terminal, environ)
                store = StateStore(self.state_root)
                self.assertEqual(session, store.session_record(provider, "lead")["owner_session"])
                with patch.object(StateStore, "finish_session_events", side_effect=RuntimeError("after commit")):
                    with self.assertRaisesRegex(RuntimeError, "after commit"):
                        handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                self.assertIsNone(store.load(self.project).active_run)
                self.assertEqual(1, len(store.session_record(provider, "lead")["pending"]))
                stop = self.output(handle({**self.payload("", provider),
                                           "hook_event_name": "Stop"}, environ))
                self.assertNotEqual("block", stop.get("decision"), stop)
                self.assertEqual([], store.session_record(provider, "lead")["pending"])

    def test_settled_root_can_rebind_new_project_but_delayed_old_agent_cannot_complete_it(self):
        choice = route_choice()
        self.seed_run(RunState("old-run", "old task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "old task", "working",
                                                       choice["model"], choice["effort"]),)))
        old_terminal = {**self.payload(""), "hook_event_name": "SubagentStop", "agent_id": "lead",
                        "turn_id": "old-turn", "last_assistant_message":
                        'SYMPHONY_OUTCOME: {"status":"completed"}'}
        handle(old_terminal, self.environ)
        store = StateStore(self.state_root)
        self.assertEqual("completing", store.load(self.project).active_run.status)
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertIsNone(store.load(self.project).active_run)
        new_project = self.root / "new-project"
        new_project.mkdir()
        new_prompt = {**self.payload("Build new task"), "cwd": str(new_project)}
        handle(new_prompt, self.environ)
        record = store.session_record("codex", "codex-session")
        self.assertEqual(store._path(new_project).name, record["state_name"])
        self.assertEqual(2, record["generation"])

        new_run = RunState("new-run", "new task", lead_identity="lead", status="active",
                           session_id="codex-session", provider="codex",
                           assessment={"size": "small", "complexity": "simple"},
                           delegations=(Delegation("lead", "lead", "new task", "working",
                                                   choice["model"], choice["effort"]),))
        store.save(new_project, ProjectState(active_run=new_run,
                                             active_runs={"codex:codex-session": new_run}))
        handle({**old_terminal, "cwd": str(new_project)}, self.environ)
        self.assertEqual("active", store.load(new_project).active_run.status)
        blocked = self.output(handle({**new_prompt, "hook_event_name": "Stop"}, self.environ))
        self.assertEqual("block", blocked.get("decision"))

    def test_same_lead_followup_recovers_a_persisted_150_run(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(Delegation("lead", "lead", "task", "working",
                                                       choice["model"], choice["effort"]),)))
        stopped = {**self.payload(""), "hook_event_name": "SubagentStop",
                   "agent_id": "lead", "agent_type": "default"}
        handle({**stopped, "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"blocked"}'}, self.environ)
        store = StateStore(self.state_root)
        state = store.load(self.project)
        legacy = replace(state.active_run, assessment={key: value for key, value in state.active_run.assessment.items()
                                                       if key != "_retryable_lead"})
        store.save(self.project, replace(state, active_run=legacy,
                                         active_runs={"codex:codex-session": legacy},
                                         event_history=(*state.event_history, Event(
                                             "foreign-worker", "delegation_updated", legacy.updated_at,
                                             {"identity": "another-session-worker", "role": "worker", "state": "failed"}
                                         ))))

        handle({**stopped, "last_assistant_message": 'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)

        self.assertEqual("completing", store.load(self.project).active_run.status)
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertIsNone(store.load(self.project).active_run)

    def test_worker_failure_after_blocked_lead_requires_new_lead(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(
                                   Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"]),
                                   Delegation("worker", "worker", "task", "working", "model", "high"),
                               )))
        stopped = {**self.payload(""), "hook_event_name": "SubagentStop"}
        handle({**stopped, "agent_id": "lead", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"blocked"}'}, self.environ)
        store = StateStore(self.state_root)
        state = store.load(self.project)
        legacy = replace(state.active_run, assessment={key: value for key, value in state.active_run.assessment.items()
                                                       if key != "_retryable_lead"})
        store.save(self.project, replace(state, active_run=legacy,
                                         active_runs={"codex:codex-session": legacy}))
        handle({**stopped, "agent_id": "worker", "status": "FAILED"}, self.environ)
        handle({**stopped, "agent_id": "lead", "last_assistant_message":
                'SYMPHONY_OUTCOME: {"status":"completed"}'}, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertIsNone(state.active_run.outcome)

    def test_unknown_payload_fields_do_not_disturb_the_guard(self):
        self.open_run("Ship it")
        started = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "worker-1",
            "agent_type": "worker",
            "tokens": "not-an-int",
            "duration_seconds": False,
        }
        handle(started, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.active_run.delegations[-1].identity, "worker-1")
        stop = {**self.payload(""), "hook_event_name": "Stop"}
        self.assertEqual(self.output(handle(stop, self.environ))["decision"], "block")

    def test_disable_preserves_active_run_until_observed_agents_stop(self):
        handle(self.payload("$symphony:symphony enable Ship it"), self.environ)
        self.open_run("Ship it")
        started = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "worker-1",
            "agent_type": "worker",
        }
        handle(started, self.environ)

        disabled = handle(self.payload("$symphony:symphony disable"), self.environ)
        stopping = StateStore(self.state_root).load(self.project)
        self.assertFalse(stopping.enabled)
        self.assertEqual(stopping.active_run.status, "stopping")
        self.assertIn("worker-1", self.context(disabled))

        handle({**started, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        finished = StateStore(self.state_root).load(self.project)
        self.assertIsNone(finished.active_run)
        self.assertEqual(finished.recent_runs[-1].status, "disabled")

    def test_session_start_reconciles_only_when_host_reports_active_ids(self):
        delegation = Delegation("lead-1", "lead", "work", "working", "", "")
        run = RunState("run-1", "task", status="interrupted", lead_identity="lead-1", delegations=(delegation,))
        self.seed_run(run, enabled=True)

        unknown = {**self.payload(""), "hook_event_name": "SessionStart"}
        unknown_result = handle(unknown, self.environ)
        self.assertEqual(
            self.output(unknown_result)["hookSpecificOutput"]["hookEventName"],
            "SessionStart",
        )
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "interrupted")

        handle({**unknown, "active_agent_ids": []}, self.environ)
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "recovering")

    def test_pre_tool_route_marker_persists_assessment_and_status(self):
        self.open_run("Ship it")
        marker = json.dumps(
            {
                "size": "medium",
                "complexity": "mixed",
                "risk": "normal",
                "rationale": "bounded work",
                "topology": "mixed",
            }
        )
        hook = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                "model": self.mixed["model"],
                "reasoning_effort": self.mixed["effort"],
            },
        }
        handle(hook, self.environ)
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "lead",
        }
        handle(lead, self.environ)

        status = self.context(handle(self.payload("$symphony:symphony status"), self.environ))
        self.assertIn("Assessment: medium/mixed", status)
        self.assertIn("Topology: mixed", status)
        self.assertIn(f"Lead route: {self.mixed['model']}/{self.mixed['effort']}", status)
        self.assertIn("Lead: lead-1", status)

    def test_pre_tool_use_denies_unclassified_agent_spawn(self):
        handle(self.payload("$symphony:symphony enable"), self.environ)
        hook = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {"message": "Inspect the repository"},
        }

        output = self.output(handle(hook, self.environ))

        self.assertEqual(output["decision"], "block")
        self.assertIn("SYMPHONY_ROLE", output["reason"])

    def test_prepared_assessor_role_survives_generic_host_label(self):
        handle(self.payload("$symphony:symphony start Ship it"), self.environ)
        prepared = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "SYMPHONY_ROLE: assessor\nAssess the task",
                "model": "gpt-strong",
                "reasoning_effort": "high",
            },
        }
        prepared_output = self.output(handle(prepared, self.environ))
        self.assertNotIn("decision", prepared_output)

        started = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": "general-purpose",
        }
        handle(started, self.environ)

        delegation = StateStore(self.state_root).load(self.project).active_run.delegations[-1]
        self.assertEqual(delegation.role, "assessor")
        self.assertEqual(delegation.requested_tier, "gpt-strong")
        self.assertEqual(delegation.requested_effort, "high")

    def test_prepared_lead_role_survives_generic_host_label(self):
        self.open_run("Ship it")
        handle(
            {
                **self.payload(""),
                "hook_event_name": "SubagentStart",
                "agent_id": "assessor-1",
                "agent_type": f"symphony_assessor_{CODEX_STRONGEST.replace('-', '_')}_high",
            },
            self.environ,
        )
        marker = json.dumps(
            {
                "size": "large",
                "complexity": "complex",
                "risk": "normal",
                "rationale": "broad project work",
                "topology": "delegated",
            }
        )
        prepared = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nRun it",
                "model": self.large["model"],
                "reasoning_effort": self.large["effort"],
            },
        }
        prepared_output = self.output(handle(prepared, self.environ))
        self.assertNotIn("decision", prepared_output)

        started = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "claude",
        }
        handle(started, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.lead_identity, "lead-1")
        self.assertEqual(state.active_run.delegations[-1].role, "lead")
        self.assertEqual(state.active_run.delegations[-1].requested_tier, self.large["model"])
        self.assertEqual(state.active_run.delegations[-1].objective, "Run it")

    def test_lead_spawn_without_route_is_denied(self):
        self.open_run("Ship it")
        hook = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "SYMPHONY_ROLE: lead\nRun it",
                "model": "gpt-balanced",
                "reasoning_effort": "high",
            },
        }

        output = self.output(handle(hook, self.environ))

        self.assertEqual(output["decision"], "block")
        self.assertIn("SYMPHONY_ROUTE", output["reason"])

    def test_consultant_spawn_requires_decision_local_classification(self):
        self.open_run("Ship it")
        store = StateStore(self.state_root)
        state = store.load(self.project)
        run = replace(state.active_run, status="active", lead_identity="lead-1")
        store.save(
            self.project,
            replace(
                state,
                active_run=run,
                active_runs={**state.active_runs, "codex:codex-session": run},
            ),
        )
        hook = {
            **self.payload(""),
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": "SYMPHONY_ROLE: consultant\nDecide the storage boundary",
                "model": "gpt-strong",
                "reasoning_effort": "high",
            },
        }

        output = self.output(handle(hook, self.environ))

        self.assertEqual(output["decision"], "block")
        self.assertIn("SYMPHONY_DECISION", output["reason"])

    def test_unclassified_consultant_result_blocks_lead_completion(self):
        self.open_run("Ship it")
        marker = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )
        handle(
            {
                **self.payload(""),
                "hook_event_name": "PreToolUse",
                "tool_name": "spawn_agent",
                "tool_input": {
                    "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                    "model": self.simple["model"],
                    "reasoning_effort": self.simple["effort"],
                },
            },
            self.environ,
        )
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "symphony_lead_gpt_5_medium",
        }
        consultant = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "consultant-1",
            "agent_type": "symphony_consultant_gpt_6_high",
            "parent_thread_id": "lead-1",
        }
        handle(lead, self.environ)
        handle(consultant, self.environ)

        missing = handle(
            {
                **consultant,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Use the simpler storage boundary.",
            },
            self.environ,
        )
        self.assertIn("SYMPHONY_DECISION", self.flush())
        handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )
        self.assertIsNone(StateStore(self.state_root).load(self.project).active_run.outcome)

        classified = "\n".join(
            (
                'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}',
                'SYMPHONY_DECISION: {"size":"small","complexity":"mixed"}',
            )
        )
        handle(
            {
                **consultant,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": classified,
            },
            self.environ,
        )
        reconciled = StateStore(self.state_root).load(self.project)
        self.assertEqual("completing", reconciled.active_run.status)
        self.assertEqual(reconciled.active_run.outcome, {"status": "completed"})
        handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        self.assertEqual(
            StateStore(self.state_root).load(self.project).recent_runs[-1].outcome["status"],
            "completed",
        )
        self.assertEqual(len(StateStore(self.state_root).load(self.project).recent_runs), 1)

    def test_late_unclassified_consultant_blocks_deferred_stop(self):
        marker = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )
        self.open_run("Ship it")
        handle(
            {
                **self.payload(""),
                "hook_event_name": "PreToolUse",
                "tool_name": "spawn_agent",
                "tool_input": {
                    "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                    "model": self.simple["model"],
                    "reasoning_effort": self.simple["effort"],
                },
            },
            self.environ,
        )
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "lead",
        }
        consultant = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "consultant-1",
            "agent_type": "consultant",
        }
        handle(lead, self.environ)
        self.complete_substantive_worker()
        handle(consultant, self.environ)
        handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )
        handle(
            {
                **consultant,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Use the simple option.",
            },
            self.environ,
        )

        blocked = handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)

        self.assertEqual(self.output(blocked)["decision"], "block")
        self.assertIn("consultant", self.output(blocked)["reason"].lower())
        self.assertIn("Keep protocol markers out of the final answer", self.output(blocked)["reason"])
        self.assertIn("status confirms", self.output(blocked)["reason"])
        self.assertIsNotNone(StateStore(self.state_root).load(self.project).active_run)

        handle(
            {
                **consultant,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": 'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}',
            },
            self.environ,
        )
        released = handle({**self.payload(""), "hook_event_name": "Stop", "stop_hook_active": True}, self.environ)
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(released.stdout, "")
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")
        self.assertEqual(state.recent_runs[-1].outcome, {"status": "completed"})

    def test_route_rejection_cancels_deferred_lead_completion(self):
        self.open_run("Ship it")
        assessor = {
            **self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "assessor-1",
            "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high"),
            "model": CODEX_STRONGEST, "model_reasoning_effort": "high",
        }
        handle(assessor, self.environ)
        handle({**assessor, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": 'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple","risk":"normal","rationale":"bounded","topology":"direct"}'},
               self.environ)
        lead = {**self.payload(""), "hook_event_name": "SubagentStart",
                "agent_id": "lead-1", "agent_type": codex_agent_type("lead", self.simple["model"], self.simple["effort"]),
                "model": self.simple["model"], "model_reasoning_effort": self.simple["effort"]}
        consultant = {**self.payload(""), "hook_event_name": "SubagentStart",
                      "agent_id": "consultant-1", "agent_type": "consultant"}
        handle(lead, self.environ)
        handle(consultant, self.environ)
        handle({**consultant, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Unclassified advice"}, self.environ)
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        self.assertIn("_pending_lead_completion", StateStore(self.state_root).load(self.project).active_run.assessment)

        transcript = self.root / "corrected-lead.jsonl"
        transcript.write_text(json.dumps({
            "type": "turn_context", "payload": {"model": "wrong-model", "effort": self.simple["effort"]},
        }), encoding="utf-8")
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "agent_transcript_path": str(transcript)}, self.environ)
        rejected = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual(rejected.status, "recovering")
        self.assertIn("_lead_route_mismatch", rejected.assessment)

        handle({**consultant, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": 'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}'},
               self.environ)
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertIsNone(state.active_run.outcome)

    def test_late_old_lead_result_cannot_replace_deferred_current_lead(self):
        self.open_run("Ship it")
        assessor = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high"),
            "model": CODEX_STRONGEST,
            "model_reasoning_effort": "high",
        }
        handle(assessor, self.environ)
        handle(
            {
                **assessor,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": 'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple","risk":"normal","rationale":"bounded","topology":"direct"}',
            },
            self.environ,
        )
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": codex_agent_type("lead", self.simple["model"], self.simple["effort"]),
            "model": self.simple["model"],
            "model_reasoning_effort": self.simple["effort"],
        }
        consultant = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "consultant-1",
            "agent_type": "consultant",
        }
        handle(lead, self.environ)
        handle(consultant, self.environ)
        handle(
            {**consultant, "hook_event_name": "SubagentStop", "status": "completed",
             "last_assistant_message": "Unclassified advice"},
            self.environ,
        )
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        handle({**self.payload(""), "hook_event_name": "Interrupt"}, self.environ)
        replacement = {**lead, "agent_id": "lead-2"}
        handle(replacement, self.environ)
        self.complete_substantive_worker()
        self.assertNotIn(
            "_pending_lead_completion",
            StateStore(self.state_root).load(self.project).active_run.assessment,
        )
        handle({**replacement, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed"}, self.environ)
        handle(
            {**consultant, "hook_event_name": "SubagentStop", "status": "completed",
             "last_assistant_message": 'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}'},
            self.environ,
        )

        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "completing")
        handle({**self.payload(""), "hook_event_name": "Stop"}, self.environ)
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")
        self.assertEqual(state.recent_runs[-1].outcome, {"status": "completed"})

    def test_invalid_consultant_does_not_suppress_failed_lead_recovery(self):
        self.open_run("Ship it")
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": "lead",
        }
        consultant = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "consultant-1",
            "agent_type": "consultant",
        }
        handle(lead, self.environ)
        handle(consultant, self.environ)
        handle(
            {
                **consultant,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Unclassified advice",
            },
            self.environ,
        )

        result = handle(
            {**lead, "hook_event_name": "SubagentStop", "status": "failed"},
            self.environ,
        )

        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.status, "recovering")
        self.assertIn("safe replacement", self.flush().lower())

    def test_claude_pending_spawns_match_native_roles_out_of_order(self):
        self.seed_run(RunState("run-1", "task", lead_identity="lead-1"), "claude")
        worker_type = claude_agent_type("worker", self.claude_large)
        consultant_type = f"symphony:symphony-consultant-{CLAUDE_STRONGEST}-high"
        for role, agent_type, extra in (
            ("worker", worker_type, ""),
            (
                "consultant",
                consultant_type,
                '\nSYMPHONY_DECISION: {"size":"small","complexity":"mixed"}',
            ),
        ):
            handle(
                {
                    **self.payload("", "claude"),
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Agent",
                    "tool_input": {
                        "prompt": f"SYMPHONY_ROLE: {role}{extra}\nDo it",
                        "subagent_type": agent_type,
                    },
                },
                self.environ,
            )
        consultant_started = {
            **self.payload("", "claude"),
            "hook_event_name": "SubagentStart",
            "agent_id": "consultant-1",
            "agent_type": consultant_type,
        }
        handle(consultant_started, self.environ)
        handle(consultant_started, self.environ)
        handle(
            {
                **self.payload("", "claude"),
                "hook_event_name": "SubagentStart",
                "agent_id": "worker-1",
                "agent_type": worker_type,
            },
            self.environ,
        )

        roles = {
            item.identity: item.role
            for item in StateStore(self.state_root).load(self.project).active_run.delegations
        }
        self.assertEqual(roles, {"consultant-1": "consultant", "worker-1": "worker"})

    def test_claude_same_role_pending_spawns_match_native_model_and_effort(self):
        self.seed_run(RunState("run-1", "task", lead_identity="lead-1"), "claude")
        low_type = claude_agent_type("worker", self.claude_large)
        high_type = claude_agent_type("worker", self.claude_mixed)
        for agent_type in (low_type, high_type):
            handle(
                {
                    **self.payload("", "claude"),
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Agent",
                    "tool_input": {
                        "prompt": "SYMPHONY_ROLE: worker\nDo it",
                        "subagent_type": agent_type,
                    },
                },
                self.environ,
            )
        for identity, agent_type in (
            ("worker-high", high_type),
            ("worker-low", low_type),
        ):
            handle(
                {
                    **self.payload("", "claude"),
                    "hook_event_name": "SubagentStart",
                    "agent_id": identity,
                    "agent_type": agent_type,
                },
                self.environ,
            )

        delegations = {
            item.identity: (item.requested_tier, item.requested_effort)
            for item in StateStore(self.state_root).load(self.project).active_run.delegations
        }
        self.assertEqual(delegations["worker-high"], (self.claude_mixed["model"], self.claude_mixed["effort"]))
        self.assertEqual(delegations["worker-low"], (self.claude_large["model"], self.claude_large["effort"]))

    def test_claude_replacement_preparation_preserves_recovery_generation(self):
        marker = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )
        run = RunState(
            "run-1",
            "task",
            status="recovering",
            lead_identity="lead-1",
            assessment={
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
                "route": {"lead_model": self.claude_medium["model"], "lead_effort": self.claude_medium["effort"]},
                "_invalid_consultants": ["consultant-1"],
            },
        )
        self.seed_run(run, "claude")
        lead_type = claude_agent_type("lead", self.claude_medium)
        prepared = {
            **self.payload("", "claude"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nRecover",
                "subagent_type": lead_type,
            },
        }
        handle(prepared, self.environ)
        handle(
            {
                **self.payload("", "claude"),
                "hook_event_name": "SubagentStart",
                "agent_id": "lead-2",
                "agent_type": lead_type,
            },
            self.environ,
        )

        replaced = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual(replaced.status, "active")
        self.assertEqual(replaced.lead_identity, "lead-2")
        self.assertEqual(replaced.owner_generation, 2)
        self.assertEqual(replaced.assessment["_invalid_consultants"], ["consultant-1"])

    def test_claude_agent_spawn_is_denied_without_a_symphony_agent_type(self):
        handle(self.payload("/symphony:start Ship it", "claude"), self.environ)
        hook = {
            **self.payload("", "claude"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "SYMPHONY_ROLE: assessor\nAssess the task",
                "model": CLAUDE_STRONGEST,
            },
        }

        output = self.output(handle(hook, self.environ))

        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Symphony agent type", decision["permissionDecisionReason"])

    def test_claude_agent_model_override_cannot_change_packaged_role_model(self):
        handle(self.payload("/symphony:start Ship it", "claude"), self.environ)
        hook = {
            **self.payload("", "claude"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "SYMPHONY_ROLE: assessor\nAssess the task",
                "subagent_type": self.claude_assessor_type,
                "model": "different-model",
            },
        }

        decision = self.output(handle(hook, self.environ))["hookSpecificOutput"]

        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("model override", decision["permissionDecisionReason"].lower())

    def test_claude_symphony_agent_type_supplies_model_and_effort(self):
        handle(self.payload("/symphony:start Ship it", "claude"), self.environ)
        prepared = {
            **self.payload("", "claude"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "SYMPHONY_ROLE: assessor\nAssess the task",
                "subagent_type": self.claude_assessor_type,
            },
        }
        self.assertEqual(handle(prepared, self.environ).stdout, "")

        started = {
            **self.payload("", "claude"),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": self.claude_assessor_type,
        }
        handle(started, self.environ)

        delegation = StateStore(self.state_root).load(self.project).active_run.delegations[-1]
        self.assertEqual(delegation.role, "assessor")
        self.assertEqual(delegation.requested_tier, CLAUDE_STRONGEST)
        self.assertEqual(delegation.requested_effort, "high")

    def test_claude_assessment_feedback_is_delivered_to_parent_post_tool_use(self):
        handle(self.payload("/symphony:start Ship it", "claude"), self.environ)
        prepared = {
            **self.payload("", "claude"),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "SYMPHONY_ROLE: assessor\nAssess the task",
                "subagent_type": self.claude_assessor_type,
            },
        }
        handle(prepared, self.environ)
        started = {
            **self.payload("", "claude"),
            "hook_event_name": "SubagentStart",
            "agent_id": "assessor-1",
            "agent_type": self.claude_assessor_type,
        }
        handle(started, self.environ)
        assessment = json.dumps(
            {
                "size": "small",
                "complexity": "simple",
                "risk": "normal",
                "rationale": "bounded task",
                "topology": "direct",
            }
        )

        stopped = handle(
            {
                **started,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": f"SYMPHONY_ASSESSMENT: {assessment}",
            },
            self.environ,
        )
        parent = handle(
            {
                **self.payload("", "claude"),
                "hook_event_name": "PostToolUse",
                "tool_name": "Agent",
            },
            self.environ,
        )

        self.assertEqual(stopped.stdout, "")
        self.assertIn("accepted", self.context(parent).lower())

    def test_legacy_provider_data_is_imported_once(self):
        legacy_root = self.root / "plugin-data"
        from plugins.symphony.symphony.store import legacy_project_keys

        legacy = legacy_root / "projects" / f"{legacy_project_keys(self.project)[0]}.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"schema_version": 0, "enabled": True, "configuration": {"x": 1}}))
        environ = {**self.environ, "PLUGIN_DATA": str(legacy_root)}

        handle(self.payload("$symphony:symphony status"), environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertTrue(state.enabled)
        self.assertEqual(state.configuration, {"x": 1})
        self.assertTrue(list(legacy.parent.glob(legacy.name + ".pre-1.0-*")))

    def test_compact_delegations_prioritizes_failed_then_active_then_recent(self):
        delegations = (
            Delegation("done-old", "worker", "", "completed", "economy", "low", "2026-01-01"),
            Delegation("wait", "worker", "", "waiting", "economy", "low", "2026-01-06"),
            Delegation("done-new", "worker", "", "completed", "economy", "low", "2026-01-05"),
            Delegation("working", "worker", "", "working", "economy", "low", "2026-01-04"),
            Delegation("failed", "worker", "", "failed", "economy", "low", "2026-01-03"),
            Delegation("done-mid", "worker", "", "completed", "economy", "low", "2026-01-02"),
        )
        state = ProjectState(active_run=RunState("run", "task", delegations=delegations))

        rows = compact_delegations(state)

        self.assertEqual(len(rows), 5)
        self.assertEqual([row.identity for row in rows[:3]], ["failed", "working", "wait"])
        self.assertNotIn("done-old", [row.identity for row in rows])

    def test_missing_metrics_are_omitted(self):
        row = Delegation("w1", "worker", "work", "working", "balanced", "medium")
        rendered = format_delegation(row)
        self.assertNotIn("token", rendered.lower())
        self.assertNotIn("duration", rendered.lower())
        self.assertNotIn("not exposed", rendered.lower())

    def test_delegation_label_includes_observed_model_and_effort(self):
        row = Delegation("lead-1", "lead", "work", "working", "gpt-5", "high")
        self.assertIn("lead [gpt-5/high]", format_delegation(row))

    def test_native_lead_completion_replay_cannot_hide_late_worker_failure(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                environ = self.claude_environ if provider == "claude" else self.environ
                choice = route_choice(provider=provider)
                self.seed_run(RunState("run", "task", lead_identity="lead", assessment={
                    "size": "small", "complexity": "simple",
                }, delegations=(
                    Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"]),
                    Delegation("worker", "worker", "widget", "working", "model", "high"),
                )), provider)
                completion = {**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "lead",
                              "status": "completed", "last_assistant_message": "Integrated the task."}
                handle({**self.payload("", provider), "hook_event_name": "SessionStart"}, environ)
                handle(completion, environ)
                handle({**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "worker",
                        "status": "failed"}, environ)
                handle(completion, environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertIsNotNone(state.active_run)
                self.assertEqual(state.active_run.status, "recovering")
                self.assertIsNone(state.active_run.outcome)
                self.assertEqual(state.recent_runs, ())
                replacement = {**self.payload("", provider), "provider": provider,
                               "agent_id": "fresh-lead", "role": "lead", "model": choice["model"],
                               "model_reasoning_effort": choice["effort"]}
                handle({**replacement, "hook_event_name": "SubagentStart"}, environ)
                handle({**replacement, "hook_event_name": "SubagentStop", "status": "completed",
                        "last_assistant_message": "Recovered the failed worker and integrated its result."}, environ)
                finished = StateStore(self.state_root).load(self.project)
                self.assertEqual(finished.active_run.status, "completing")
                handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                finished = StateStore(self.state_root).load(self.project)
                self.assertIsNone(finished.active_run)
                self.assertEqual(finished.recent_runs[-1].status, "completed")

    def test_replayed_deferred_lead_result_cannot_hide_late_worker_failure(self):
        choice = route_choice()
        self.seed_run(RunState("run", "task", lead_identity="lead", status="active",
                               assessment={"size": "small", "complexity": "simple"},
                               delegations=(
                                   Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"]),
                                   Delegation("worker", "worker", "task", "working", "model", "high"),
                                   Delegation("consultant", "consultant", "decision", "working", "model", "high"),
                               )))
        stopped = {**self.payload(""), "hook_event_name": "SubagentStop"}
        handle({**stopped, "agent_id": "consultant", "last_assistant_message": "Unclassified advice"}, self.environ)
        lead = {**stopped, "agent_id": "lead", "last_assistant_message": "Integrated the task"}
        handle(lead, self.environ)
        handle({**stopped, "agent_id": "worker", "last_assistant_message": "Failed", "status": "failed"}, self.environ)
        handle(lead, self.environ)
        handle({**stopped, "agent_id": "consultant", "last_assistant_message":
                'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}'}, self.environ)

        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertIsNone(state.active_run.outcome)

    def test_failed_assessor_spawn_removes_only_its_pending_intent(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.open_run("Task", provider)
                model = CODEX_STRONGEST if provider == "codex" else CLAUDE_STRONGEST
                tool_input = {"message": "SYMPHONY_ROLE: assessor\nTask", "model": model, "reasoning_effort": "high"}
                if provider == "claude":
                    tool_input = {"prompt": "SYMPHONY_ROLE: assessor\nTask", "subagent_type": self.claude_assessor_type}
                failure = {**self.payload("", provider), "hook_event_name": "PostToolUseFailure" if provider == "claude" else "PostToolUse",
                           "tool_response": {"is_error": True}, "tool_name": "spawn_agent" if provider == "codex" else "Agent",
                           "tool_input": tool_input, "error": "model rejected"}
                handle(failure, self.environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertNotIn("_pending_delegations", state.active_run.assessment)
                stop = handle({**self.payload("", provider), "hook_event_name": "Stop"}, self.environ)
                self.assertNotEqual(self.output(stop).get("decision"), "block")

    def test_native_failure_replay_cannot_remove_a_second_pending_launch(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                model = CODEX_STRONGEST if provider == "codex" else CLAUDE_STRONGEST
                pending = {"role": "assessor", "model": model, "effort": "high", "objective": "Task"}
                self.seed_run(RunState("run", "Task", assessment={"_pending_delegations": [pending, pending]},
                                      delegations=(Delegation("live-worker", "worker", "Task", "working", model, "high"),)), provider)
                values = {"message": "SYMPHONY_ROLE: assessor\nTask", "model": model, "reasoning_effort": "high"}
                if provider == "claude":
                    values = {"prompt": "SYMPHONY_ROLE: assessor\nTask", "subagent_type": self.claude_assessor_type}
                failure = {**self.payload("", provider), "hook_event_name": "PostToolUseFailure" if provider == "claude" else "PostToolUse",
                           "tool_response": {"is_error": True}, "tool_name": "spawn_agent" if provider == "codex" else "Agent",
                           "tool_use_id": "failed-native-call", "tool_input": values, "error": "rejected"}
                handle(failure, self.environ)
                handle(failure, self.environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertEqual(len(state.active_run.assessment["_pending_delegations"]), 1)
                self.assertEqual(state.active_run.delegations[0].state, "working")

    def test_quoted_controls_in_bug_reports_cannot_stop_or_disable_a_run(self):
        for provider in ("codex", "claude"):
            for prompt in (
                "I got 2 feedbacks from my colleagues: 1. Symphony stop is blocked for this project: "
                "no lead has returned an outcome yet. Let the tracked agents finish, wait for the host "
                "stop timeout, or run $symphony:symphony stop --force to end the run and record what "
                "was not reconciled. Keep protocol markers out of the final answer, and report completion "
                "only after durable status confirms it. I get things like this quite often - which has "
                "made him wonder if something is wrong 2. They want to be able to have a way to boost up "
                "the effort in a way, like to make the assessor use max or ultra efforts on the flagship "
                "models on both providers 3. When the plugin auto updates after a release, their agent "
                "break. They keep trying to run the previous version's hooks that does not exist anymore "
                "and usually stop since the agent has encountered a failuare Fix this for both agents, "
                "review, test on both agents on both linux and windows, when everuthing looked correct "
                "and solved, bump the version to 1.5.0 and make a release",
                'Bug report: `$symphony:symphony stop --force` returned no active work.',
                '```text\n$symphony:symphony stop --force\n```',
                '> $symphony:symphony disable',
                'Please review this command:\nSYMPHONY_CONTROL: disable',
                '"/symphony:stop --force"',
                '$symphony:symphony stop --force is mentioned in this bug report',
            ):
                with self.subTest(provider=provider, prompt=prompt):
                    run = self.seed_run(RunState("run", "task", delegations=(
                        Delegation("worker", "worker", "task", "working", "model", "high"),
                    )), provider, enabled=True)
                    result = handle(self.payload(prompt, provider), self.environ)
                    current = StateStore(self.state_root).load(self.project)
                    self.assertEqual(current.active_run.run_id, run.run_id)
                    self.assertTrue(current.enabled)
                    self.assertNotIn("no active work", self.context(result))

    def test_stop_roster_reconciles_ended_work_without_inventing_an_outcome(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.seed_run(RunState("run", "task", lead_identity="lead", delegations=(
                    Delegation("lead", "lead", "task", "working", "model", "high"),
                )), provider)
                result = handle({**self.payload("", provider), "hook_event_name": "Stop", "active_agent_ids": []}, self.environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertEqual(state.active_run.status, "recovering")
                self.assertEqual(state.active_run.delegations[0].state, "interrupted")
                self.assertIsNone(state.active_run.outcome)
                self.assertEqual(self.output(result)["decision"], "block")

    def test_missing_lead_start_is_reconciled_from_native_terminal_event(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                environ = self.claude_environ if provider == "claude" else self.environ
                choice = route_choice(provider=provider)
                self.seed_run(RunState("run", "task", assessment={"size": "small", "complexity": "simple"}), provider)
                handle({**self.payload("", provider), "hook_event_name": "SessionStart"}, environ)
                agent_type = (codex_agent_type("lead", choice["model"], choice["effort"]) if provider == "codex"
                              else claude_agent_type("lead", choice))
                handle({**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "lead",
                        "agent_type": agent_type, "status": "completed", "provider": provider, "model": choice["model"],
                        "model_reasoning_effort": choice["effort"]}, environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertEqual(state.active_run.status, "completing")
                handle({**self.payload("", provider), "hook_event_name": "Stop"}, environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertIsNone(state.active_run)
                self.assertEqual(state.recent_runs[-1].outcome, {"status": "completed"})

    def test_explicit_invalid_or_blocked_outcome_does_not_complete_native_lead(self):
        for provider in ("codex", "claude"):
            for marker in ('SYMPHONY_OUTCOME: {broken', 'SYMPHONY_OUTCOME: {}',
                           'SYMPHONY_OUTCOME: {"status":"blocked"}'):
                with self.subTest(provider=provider, marker=marker):
                    choice = route_choice(provider=provider)
                    self.seed_run(RunState("run", "task", lead_identity="lead", assessment={
                        "size": "small", "complexity": "simple",
                    }, delegations=(Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"]),)), provider)
                    handle({**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "lead",
                            "status": "completed", "last_assistant_message": marker}, self.environ)
                    state = StateStore(self.state_root).load(self.project)
                    self.assertIsNotNone(state.active_run)
                    self.assertEqual(state.active_run.status, "recovering")
                    self.assertIsNone(state.active_run.outcome)

    def test_single_word_codex_task_starts_a_one_shot_run(self):
        result = handle(self.payload("$symphony:symphony summarize"), self.environ)
        self.assertIn("assess", self.context(result).lower())
        self.assertIn("summarize", self.context(result))

        self.open_run("summarize")
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.task, "summarize")

    def test_identical_claude_task_can_run_again_after_completion(self):
        prompt = "SYMPHONY_CONTROL: start\nARGUMENTS: Repeat me"
        handle(self.payload(prompt, "claude"), self.environ)
        self.open_run("Repeat me", "claude")
        store = StateStore(self.state_root)
        state = store.load(self.project)
        store.save(
            self.project,
            ProjectState(
                enabled=state.enabled,
                activation=state.activation,
                recent_runs=(state.active_run,),
                event_history=state.event_history,
            ),
        )

        handle(self.payload(prompt, "claude"), self.environ)
        self.open_run("Repeat me", "claude")

        self.assertEqual(store.load(self.project).active_run.task, "Repeat me")


    def test_enabled_prompt_alone_cannot_hold_the_session(self):
        handle(self.payload("$symphony:symphony enable"), self.environ)
        handle(self.payload("Just answer this directly"), self.environ)

        stop = {**self.payload(""), "hook_event_name": "Stop"}
        result = handle(stop, self.environ)

        self.assertEqual(result.stdout, "", "a prompt that spawned nothing must not block Stop")
        self.assertIsNone(StateStore(self.state_root).load(self.project).active_run)

    def test_repeated_stop_preserves_a_session_whose_child_never_reported(self):
        self.open_run("Ship it")
        handle(
            {
                **self.payload(""),
                "hook_event_name": "SubagentStart",
                "agent_id": "assessor-1",
                "agent_type": codex_agent_type("assessor", CODEX_STRONGEST, "high"),
            },
            self.environ,
        )
        stop = {**self.payload(""), "hook_event_name": "Stop"}

        blocked = self.output(handle(stop, self.environ))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("--force", blocked["reason"])
        self.assertIn("assessor", blocked["reason"])

        released = handle({**stop, "stop_hook_active": True}, self.environ)

        self.assertEqual(released.stdout, "", "a turn retry must render an empty Stop response")
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNotNone(state.active_run)
        self.assertEqual(state.recent_runs, ())
        self.assertEqual(state.active_run.delegations[0].identity, "assessor-1")

    def test_quiet_owner_is_not_adopted_by_another_session(self):
        self.open_run("Ship it")
        handle(
            {
                **self.payload(""),
                "hook_event_name": "SubagentStart",
                "agent_id": "worker-1",
                "agent_type": codex_agent_type("worker", self.simple["model"], self.simple["effort"]),
            },
            self.environ,
        )

        path = next(self.state_root.glob("*.v2.json"))
        document = json.loads(path.read_text())
        document["active_runs"]["codex:codex-session"]["owner_seen_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(document))

        resumed = {
            **self.payload(""),
            "session_id": "codex-session-2",
            "hook_event_name": "SessionStart",
            "source": "resume",
        }
        handle(resumed, self.environ)

        run = StateStore(self.state_root).load(self.project).active_run
        self.assertEqual(run.session_id, "codex-session")
        self.assertEqual(run.status, "assessing")
        self.assertEqual({item.state for item in run.delegations}, {"working"})

    def test_version_reports_the_build_actually_running(self):
        """Installed and running differ until the host restarts, which is the
        whole reason to ask."""
        from plugins.symphony.symphony import PLUGIN_VERSION

        text = self.context(handle(self.payload("/symphony:version", "claude"), self.environ))

        self.assertIn(PLUGIN_VERSION, text)
        self.assertIn("running", text.lower())

    def test_version_names_the_directory_the_code_was_loaded_from(self):
        environ = {**self.environ, "SYMPHONY_PLUGIN_ROOT": "/cache/symphony/9.9.9"}
        text = self.context(handle(self.payload("/symphony:version", "claude"), environ))

        self.assertIn("/cache/symphony/9.9.9", text)

    def test_version_flags_a_newer_build_waiting_for_a_restart(self):
        """The trap we hit twice: `plugin update` reports a version the open
        session is not running."""
        cache = self.root / "cache" / "symphony"
        (cache / "1.1.1").mkdir(parents=True)
        (cache / "1.2.0").mkdir()
        environ = {**self.environ, "SYMPHONY_PLUGIN_ROOT": str(cache / "1.1.1")}

        text = self.context(handle(self.payload("$symphony:symphony version"), environ))

        self.assertIn("1.2.0", text)
        self.assertIn("restart", text.lower())

    def test_version_orders_builds_numerically_not_alphabetically(self):
        """0.10.1 is newer than 0.9.0, and a string sort says the opposite."""
        cache = self.root / "cache" / "symphony"
        (cache / "0.9.0").mkdir(parents=True)
        (cache / "0.10.1").mkdir()
        environ = {**self.environ, "SYMPHONY_PLUGIN_ROOT": str(cache / "0.9.0")}

        text = self.context(handle(self.payload("$symphony:symphony version"), environ))

        self.assertIn("0.10.1", text)

    def test_version_does_not_offer_a_restart_onto_an_older_build(self):
        """The same comparison in the other direction, where a string sort lies."""
        cache = self.root / "cache" / "symphony"
        (cache / "0.9.0").mkdir(parents=True)
        (cache / "0.10.1").mkdir()
        environ = {**self.environ, "SYMPHONY_PLUGIN_ROOT": str(cache / "0.10.1")}

        text = self.context(handle(self.payload("$symphony:symphony version"), environ))

        self.assertNotIn("restart", text.lower())

    def test_repeated_control_in_one_claude_session_is_not_swallowed(self):
        handle(self.payload("/symphony:enable", "claude"), self.environ)
        handle(self.payload("/symphony:disable", "claude"), self.environ)
        result = handle(self.payload("/symphony:enable", "claude"), self.environ)

        self.assertIn("enabled", self.context(result).lower())
        self.assertTrue(StateStore(self.state_root).load(self.project).enabled)

    def test_retried_consultant_clears_the_earlier_classification_block(self):
        self.open_run("Ship it")
        lead = {
            **self.payload(""),
            "hook_event_name": "SubagentStart",
            "agent_id": "lead-1",
            "agent_type": codex_agent_type("lead", self.simple["model"], self.simple["effort"]),
            "model": self.simple["model"],
            "model_reasoning_effort": self.simple["effort"],
        }
        handle(lead, self.environ)

        def consultant(identity, message):
            spawn = {
                **self.payload(""),
                "hook_event_name": "SubagentStart",
                "agent_id": identity,
                "agent_type": codex_agent_type("consultant", CODEX_STRONGEST, "high"),
                "task": "Pick the cache strategy",
            }
            handle(spawn, self.environ)
            handle(
                {
                    **spawn,
                    "hook_event_name": "SubagentStop",
                    "status": "completed",
                    "last_assistant_message": message,
                },
                self.environ,
            )

        consultant("consultant-1", "no classification here")
        blocked = StateStore(self.state_root).load(self.project)
        self.assertIn("consultant-1", blocked.active_run.assessment["_invalid_consultants"])

        decision = json.dumps({"size": "small", "complexity": "simple"})
        consultant("consultant-2", f"SYMPHONY_DECISION: {decision}")

        cleared = StateStore(self.state_root).load(self.project)
        self.assertNotIn("_invalid_consultants", cleared.active_run.assessment)

    def test_host_payload_prose_is_never_written_to_disk(self):
        secret = "ghp_examplevaluethatmustnotpersist"
        handle(self.payload(f"$symphony:symphony enable"), self.environ)
        handle(self.payload(f"Deploy using {secret} right now"), self.environ)
        self.open_run("Ship it")
        handle(
            {
                **self.payload(""),
                "hook_event_name": "SubagentStart",
                "agent_id": "lead-1",
                "agent_type": codex_agent_type("lead", self.simple["model"], self.simple["effort"]),
            },
            self.environ,
        )
        handle(
            {
                **self.payload(""),
                "hook_event_name": "SubagentStop",
                "agent_id": "lead-1",
                "agent_type": codex_agent_type("lead", self.simple["model"], self.simple["effort"]),
                "status": "completed",
                "last_assistant_message": f"the token is {secret}",
                "transcript_path": "/tmp/transcript.jsonl",
            },
            self.environ,
        )
        handle(
            {
                **self.payload(""),
                "hook_event_name": "Stop",
                "stop_hook_active": True,
                "last_assistant_message": f"and again {secret}",
            },
            self.environ,
        )

        written = "\n".join(
            path.read_text() for path in self.state_root.rglob("*.json")
        )
        self.assertNotIn(secret, written)
        self.assertNotIn("transcript", written)

    def test_hook_fault_is_recorded_and_surfaced_once(self):
        from plugins.symphony.symphony import runtime

        runtime._record_fault(RuntimeError("boom"), self.environ)
        self.assertTrue((self.state_root.parent / "faults.log").exists())

        status = self.context(handle(self.payload("$symphony:symphony status"), self.environ))

        self.assertIn("RuntimeError", status)
        self.assertFalse((self.state_root.parent / "faults.log").exists())
        repeated = self.context(handle(self.payload("$symphony:symphony status"), self.environ))
        self.assertNotIn("RuntimeError", repeated)

    def test_opt_in_stop_decision_receipt_omits_reason_text(self):
        destination = self.root / "hook decisions"
        payload = {**self.payload(""), "hook_event_name": "Stop"}
        reason = "Symphony stop is blocked: an unresolved child result for secret-agent"
        runtime_module._record_hook_decision(
            payload, HookResult(json.dumps({"decision": "block", "reason": reason})),
            {"SYMPHONY_HOOK_DECISIONS_DIR": str(destination)},
        )
        receipts = list(destination.glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["category"], "pending_child")
        self.assertEqual(receipt["session_id"], "codex-session")
        self.assertNotIn("secret-agent", receipts[0].read_text())
        runtime_module._record_hook_decision(payload, HookResult(), {})
        self.assertEqual(len(list(destination.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
