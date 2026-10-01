"""A run is held to its tier, and only a real weakening asks the user.

The 1.0 design pinned a run to the concrete model its assessment resolved to
and blocked every later spawn that did not match. That turns a model leaving
the map into a run nobody can ever advance. These tests hold the replacement
to its two halves: the tier is what a run is pinned to, and the stored model
survives only to answer whether re-resolving that tier made the work weaker.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.runtime import _route_drift, handle
from plugins.symphony.symphony.routing import profiles_for, snapshot_for
from plugins.symphony.symphony.store import StateStore

FULL_PROFILE = snapshot_for("codex", "full")
BASE_PROFILE = snapshot_for("codex", "base")
DRIFT_CELL = next(
    cell for cell in FULL_PROFILE.matrix
    if FULL_PROFILE.matrix[cell] != BASE_PROFILE.matrix[cell]
)
DRIFT_SIZE, DRIFT_COMPLEXITY = DRIFT_CELL.split("/")
FULL_ROUTE = FULL_PROFILE.matrix[DRIFT_CELL]
BASE_ROUTE = BASE_PROFILE.matrix[DRIFT_CELL]
LATEST_PROFILE = profiles_for("codex")[0]["id"]
LATEST_ROUTE = snapshot_for("codex", LATEST_PROFILE).matrix[DRIFT_CELL]
FULL_STRONGEST = profiles_for("codex")[0]["tiers"]["strongest"]

MARKER = json.dumps(
    {
        "size": DRIFT_SIZE,
        "complexity": DRIFT_COMPLEXITY,
        "risk": "normal",
        "rationale": "bounded task",
        "topology": "direct",
    }
)


class RouteDriftTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.state_root = root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def env(self, profile: str) -> dict:
        return {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROFILE": profile}

    def payload(self, session: str, event: str = "UserPromptSubmit") -> dict:
        return {
            "session_id": session,
            "cwd": str(self.project),
            "hook_event_name": event,
            "prompt": "",
            "turn_id": "turn-1",
            "model": "codex-model",
        }

    def complete_worker(self, lead: dict, profile: str):
        worker = {**lead, "agent_id": "route-worker", "agent_type": "worker",
                  "parent_thread_id": lead["agent_id"], "task": "SYMPHONY_ROLE: worker",
                  "turn_id": "worker-turn"}
        handle(worker, self.env(profile))
        handle({**worker, "hook_event_name": "SubagentStop", "status": "completed"}, self.env(profile))

    def output(self, result) -> dict:
        return json.loads(result.stdout) if result.stdout else {}

    def start(self, profile: str, session: str) -> None:
        handle({**self.payload(session, "SessionStart")}, self.env(profile))

    def spawn(self, profile, session, role, model, effort, marker=""):
        body = f"SYMPHONY_ROLE: {role}\n"
        if marker:
            body += f"SYMPHONY_ROUTE: {marker}\n"
        return handle(
            {
                **self.payload(session, "PreToolUse"),
                "tool_name": "spawn_agent",
                "tool_input": {
                    "message": body + "Ship it",
                    "model": model,
                    "reasoning_effort": effort,
                },
            },
            self.env(profile),
        )

    def proceed(self, profile: str, session: str) -> None:
        handle(
            {**self.payload(session), "prompt": "$symphony:symphony proceed"}, self.env(profile)
        )

    def accept_under(self, profile: str, session: str, model: str, effort: str) -> None:
        """Open a run and let its lead spawn record the accepted route.

        A weaker entitlement trips the tier clamp before drift is even in play,
        so that consent is given first. This isolates what these tests are about.
        """
        self.start(profile, session)
        self.spawn(profile, session, "assessor", FULL_STRONGEST, "high")
        if profile != LATEST_PROFILE:
            self.proceed(profile, session)
        result = self.spawn(profile, session, "lead", model, effort, MARKER)
        self.assertNotEqual(
            self.output(result).get("decision"), "block", self.output(result).get("reason")
        )

    def test_standing_assessment_that_weakened_waits_for_the_user(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])

        # The same root resumes with a weaker entitlement: the priced route is gone.
        self.start("base", "session-1")
        output = self.output(self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER))

        self.assertEqual(output["decision"], "block")
        self.assertIn("would now run", output["reason"])
        self.assertIn(FULL_ROUTE["model"], output["reason"])
        self.assertIn("proceed", output["reason"])

    def test_proceed_accepts_the_weaker_route_and_the_spawn_goes_through(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        self.start("base", "session-1")
        blocked = self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER)
        self.assertEqual(self.output(blocked)["decision"], "block")

        self.proceed("base", "session-1")
        output = self.output(self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER))

        self.assertNotEqual(output.get("decision"), "block", output.get("reason"))

    def test_a_fresh_assessment_never_gates(self):
        """Nothing was priced before, so nothing was taken away."""
        self.start("base", "session-1")
        self.spawn("base", "session-1", "assessor", BASE_PROFILE.tiers["strongest"], "high")
        self.proceed("base", "session-1")
        output = self.output(
            self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER)
        )

        self.assertNotEqual(output.get("decision"), "block", output.get("reason"))

    def test_an_entitlement_upgrade_discloses_instead_of_gating(self):
        """Moving to a better map is not a degradation and needs no consent."""
        self.accept_under("base", "session-1", BASE_ROUTE["model"], BASE_ROUTE["effort"])

        self.start(LATEST_PROFILE, "session-1")
        output = self.output(
            self.spawn(LATEST_PROFILE, "session-1", "lead", LATEST_ROUTE["model"], LATEST_ROUTE["effort"], MARKER)
        )

        self.assertNotEqual(output.get("decision"), "block", output.get("reason"))

    def test_drift_uses_model_capability_and_effort_not_profile_order(self):
        luna = {"route": {"lead_model": "gpt-6-luna", "lead_effort": "medium"}}
        sol = {"route": {"lead_model": "gpt-6-sol", "lead_effort": "medium"}}
        self.assertFalse(_route_drift(luna, "gpt-6-sol", "medium")["weaker"])
        self.assertTrue(_route_drift(sol, "gpt-6-luna", "medium")["weaker"])
        self.assertTrue(_route_drift(sol, "gpt-6-sol", "low")["weaker"])

    def test_the_tier_not_the_stored_model_is_what_a_spawn_must_match(self):
        """The 1.0 deadlock: a stored model that no longer exists.

        Under the old rule the stored model was the required model, so once it
        left the map every lead spawn was refused and the run was stuck. The
        run must now be advanceable by spawning what the tier resolves to today.
        """
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        self.start("base", "session-1")
        self.proceed("base", "session-1")

        stale = self.output(self.spawn("base", "session-1", "lead", "removed-model", "low", MARKER))
        current = self.output(self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER))

        self.assertEqual(stale["decision"], "block")
        self.assertNotEqual(current.get("decision"), "block", current.get("reason"))

    def test_re_resolved_lead_can_complete_after_accepted_entitlement_change(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        original = {
            **self.payload("session-1", "SubagentStart"),
            "agent_id": "original-lead",
            "agent_type": "symphony_lead_" + FULL_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + FULL_ROUTE["effort"],
            "model": FULL_ROUTE["model"],
            "model_reasoning_effort": FULL_ROUTE["effort"],
        }
        handle(original, self.env("full"))
        handle({**original, "hook_event_name": "SubagentStop", "status": "failed"}, self.env("full"))
        self.start("base", "session-1")
        self.proceed("base", "session-1")
        result = self.spawn("base", "session-1", "lead", BASE_ROUTE["model"], BASE_ROUTE["effort"], MARKER)
        self.assertNotEqual(self.output(result).get("decision"), "block")
        # Another session's heartbeat must not erase the route this spawn passed.
        self.start("full", "stranger")

        lead = {
            **self.payload("session-1", "SubagentStart"),
            "agent_id": "lead-1",
            "agent_type": "symphony_lead_" + BASE_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + BASE_ROUTE["effort"],
            "model": BASE_ROUTE["model"],
            "model_reasoning_effort": BASE_ROUTE["effort"],
        }
        handle(lead, self.env("base"))
        self.complete_worker(lead, "base")
        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done"}, self.env("base"))
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "completing")
        handle(self.payload("session-1", "Stop"), self.env("base"))
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")

    def test_stranger_profile_does_not_change_running_lead_completion_route(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        lead = {
            **self.payload("session-1", "SubagentStart"),
            "agent_id": "lead-1",
            "agent_type": "symphony_lead_" + FULL_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + FULL_ROUTE["effort"],
            "model": FULL_ROUTE["model"],
            "model_reasoning_effort": FULL_ROUTE["effort"],
        }
        handle(lead, self.env("full"))
        self.start("base", "stranger")
        self.assertEqual(StateStore(self.state_root).load(self.project).active_run.session_id, "session-1")

        self.complete_worker(lead, "full")

        handle({**lead, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done"}, self.env("full"))
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "completing")
        handle(self.payload("session-1", "Stop"), self.env("full"))
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")

    def test_native_replacement_uses_its_session_profile_without_pretool(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        original = {
            **self.payload("session-1", "SubagentStart"), "agent_id": "original-lead",
            "agent_type": "symphony_lead_" + FULL_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + FULL_ROUTE["effort"],
            "model": FULL_ROUTE["model"], "model_reasoning_effort": FULL_ROUTE["effort"],
        }
        handle(original, self.env("full"))
        handle({**original, "hook_event_name": "SubagentStop", "status": "failed"}, self.env("full"))
        for index in range(4):
            self.start("full", f"z{index}")
        self.start("base", "session-1")
        self.proceed("base", "session-1")
        self.start("full", "z5")

        replacement = {
            **self.payload("session-1", "SubagentStart"), "agent_id": "replacement-lead",
            "agent_type": "symphony_lead_" + BASE_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + BASE_ROUTE["effort"],
            "model": BASE_ROUTE["model"], "model_reasoning_effort": BASE_ROUTE["effort"],
        }
        handle(replacement, self.env("base"))
        self.complete_worker(replacement, "base")
        handle({**replacement, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done"}, self.env("base"))
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "completing")
        handle(self.payload("session-1", "Stop"), self.env("base"))
        state = StateStore(self.state_root).load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")

    def test_native_weaker_replacement_still_requires_proceed(self):
        self.accept_under("full", "session-1", FULL_ROUTE["model"], FULL_ROUTE["effort"])
        original = {
            **self.payload("session-1", "SubagentStart"), "agent_id": "original-lead",
            "agent_type": "symphony_lead_" + FULL_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + FULL_ROUTE["effort"],
            "model": FULL_ROUTE["model"], "model_reasoning_effort": FULL_ROUTE["effort"],
        }
        handle(original, self.env("full"))
        handle({**original, "hook_event_name": "SubagentStop", "status": "failed"}, self.env("full"))
        self.start("base", "session-1")

        replacement = {
            **self.payload("session-1", "SubagentStart"), "agent_id": "replacement-lead",
            "agent_type": "symphony_lead_" + BASE_ROUTE["model"].replace("-", "_").replace(".", "_") + "_" + BASE_ROUTE["effort"],
            "model": BASE_ROUTE["model"], "model_reasoning_effort": BASE_ROUTE["effort"],
        }
        handle(replacement, self.env("base"))
        handle({**replacement, "hook_event_name": "SubagentStop", "status": "completed",
                "last_assistant_message": "Done"}, self.env("base"))
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.status, "recovering")
        self.assertIsNone(state.active_run.outcome)
        blocked = self.output(handle({**self.payload("session-1", "Stop")}, self.env("base")))
        self.assertIn("proceed", blocked["reason"])


if __name__ == "__main__":
    unittest.main()
