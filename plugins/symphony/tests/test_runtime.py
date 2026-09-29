import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

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
            "SYMPHONY_PROFILE": "full",
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
            "session_id": f"{provider}-session",
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
        state = store.load(self.project)
        self.assertEqual([item.identity for item in state.active_runs["codex:first-session"].delegations],
                         ["worker-one"])
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
        self.assertEqual(after.activation["codex"]["profile"], "full")
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
        expected = resolve_tier(route_for(Assessment("small", "mixed")), snapshot_for("codex", "full"))
        guidance = self.flush()
        self.assertIn("Selected codex lead (full profile)", guidance)
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
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Avalon task done"}, self.environ)
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
                            handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                                    "last_assistant_message": "Completed"}, environ)
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
        stop = {**self.payload(""), "hook_event_name": "Stop"}
        self.assertEqual(self.output(handle(stop, self.environ))["decision"], "block")

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
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].outcome, {"status": "completed"})

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
        self.assertIsNone(reconciled.active_run)
        self.assertEqual(reconciled.recent_runs[-1].outcome, {"status": "completed"})
        handle(
            {
                **lead,
                "hook_event_name": "SubagentStop",
                "status": "completed",
                "last_assistant_message": "Done",
            },
            self.environ,
        )
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
        self.assertIn("replacement", self.flush().lower())

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
                choice = route_choice(provider=provider)
                self.seed_run(RunState("run", "task", lead_identity="lead", assessment={
                    "size": "small", "complexity": "simple",
                }, delegations=(
                    Delegation("lead", "lead", "task", "working", choice["model"], choice["effort"]),
                    Delegation("worker", "worker", "widget", "working", "model", "high"),
                )), provider)
                completion = {**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "lead",
                              "status": "completed", "last_assistant_message": "Integrated the task."}
                handle(completion, self.environ)
                handle({**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "worker",
                        "status": "failed"}, self.environ)
                handle(completion, self.environ)
                state = StateStore(self.state_root).load(self.project)
                self.assertIsNotNone(state.active_run)
                self.assertEqual(state.active_run.status, "recovering")
                self.assertIsNone(state.active_run.outcome)
                self.assertEqual(state.recent_runs, ())
                replacement = {**self.payload("", provider), "provider": provider,
                               "agent_id": "fresh-lead", "role": "lead", "model": choice["model"],
                               "model_reasoning_effort": choice["effort"]}
                handle({**replacement, "hook_event_name": "SubagentStart"}, self.environ)
                handle({**replacement, "hook_event_name": "SubagentStop", "status": "completed",
                        "last_assistant_message": "Recovered the failed worker and integrated its result."}, self.environ)
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
                choice = route_choice(provider=provider)
                self.seed_run(RunState("run", "task", assessment={"size": "small", "complexity": "simple"}), provider)
                agent_type = (codex_agent_type("lead", choice["model"], choice["effort"]) if provider == "codex"
                              else claude_agent_type("lead", choice))
                handle({**self.payload("", provider), "hook_event_name": "SubagentStop", "agent_id": "lead",
                        "agent_type": agent_type, "status": "completed", "provider": provider, "model": choice["model"],
                        "model_reasoning_effort": choice["effort"]}, self.environ)
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

        path = next(self.state_root.glob("*.json"))
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


if __name__ == "__main__":
    unittest.main()
