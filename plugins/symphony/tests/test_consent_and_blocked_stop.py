"""Stop must never hold a turn that only the user can unblock.

Three regressions observed in the field: `proceed` could never satisfy the
clamp when the entitlement probe read nothing; Stop blocked the turn while the
lead launch waited for that consent; and an honestly blocked lead re-raised
the same Stop warning on every later turn (#14).
"""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import reduce
from plugins.symphony.symphony.routing import Assessment, clamp_against_best, profiles_for, route_for, snapshot_for
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore

# A cell the conservative floor serves with a weaker model than the best profile.
CLAMPED = next(
    (size, complexity)
    for size in ("small", "medium", "large")
    for complexity in ("simple", "mixed", "complex")
    if clamp_against_best("codex", route_for(Assessment(size, complexity, "normal", "x", "direct")), None)["tier_clamped"]
)
FLOOR = snapshot_for("codex", None)
FLOOR_ROUTE = FLOOR.matrix["/".join(CLAMPED)]
MARKER = json.dumps({"size": CLAMPED[0], "complexity": CLAMPED[1], "risk": "normal",
                     "rationale": "x", "topology": "direct"})


def apply(state, kind, payload=None, index=[0]):
    index[0] += 1
    return reduce(state, Event(f"event-{index[0]}", kind, "2026-10-04T00:00:00Z", payload or {}))


class UnreadableEntitlementConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        (root / "codex-home").mkdir()
        # No pinned profile and no readable roster: entitlement is unknown.
        self.env = {"SYMPHONY_STATE_DIR": str(root / "state"), "CODEX_HOME": str(root / "codex-home"),
                    "SYMPHONY_REPORT_WORKER": "0"}

    def send(self, event, **extra):
        payload = {"session_id": "root", "cwd": str(self.project), "hook_event_name": event,
                   "turn_id": "turn-1", "model": "codex-model", "prompt": "", **extra}
        result = handle(payload, self.env)
        return json.loads(result.stdout) if result.stdout else {}

    def spawn_lead(self):
        return self.send("PreToolUse", tool_name="spawn_agent", tool_input={
            "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {MARKER}\nShip it",
            "model": FLOOR_ROUTE["model"], "reasoning_effort": FLOOR_ROUTE["effort"]})

    def test_unreadable_entitlement_never_stops_the_lead_launch(self):
        self.send("SessionStart")
        self.send("PreToolUse", tool_name="spawn_agent", tool_input={
            "message": "SYMPHONY_ROLE: assessor\nShip it",
            "model": FLOOR.tiers["strongest"], "reasoning_effort": "high"})
        launched = self.spawn_lead()
        self.assertNotEqual("block", launched.get("decision"), launched.get("reason"))

    def test_consent_to_an_unreadable_profile_does_not_carry_to_another_session(self):
        self.send("SessionStart")
        self.send("UserPromptSubmit", prompt="$symphony:symphony proceed")
        self.send("SessionStart", session_id="other")
        state = StateStore(Path(self.env["SYMPHONY_STATE_DIR"])).load(self.project)
        accepted = state.activation["codex"].get("accepted") or {}
        self.assertNotIn("other", accepted)


class ConsentPendingStopTests(unittest.TestCase):
    def assessed_run(self, **assessment):
        assessor = Delegation("assessor", "assessor", "task", "completed", "model", "high")
        return ProjectState(active_run=RunState(
            "run", "task", provider="claude", session_id="root", delegations=(assessor,),
            assessment={"size": "small", "complexity": "mixed", **assessment}))

    def test_stop_lets_the_user_answer_a_held_lead_launch(self):
        state, actions = apply(self.assessed_run(_awaiting_route_consent=True), "stop_requested",
                               {"provider": "claude", "hook_event_name": "Stop"})
        self.assertEqual([("permit_stop", {})], [(a.kind, dict(a.payload)) for a in actions])
        self.assertIsNotNone(state.active_run, "the run must survive for the consent control")

    def test_without_a_held_launch_a_missing_lead_still_blocks(self):
        _, actions = apply(self.assessed_run(), "stop_requested",
                           {"provider": "claude", "hook_event_name": "Stop"})
        self.assertEqual("block_stop", actions[0].kind)
        self.assertEqual("lead_not_started", actions[0].payload["reason"])

    def test_the_lead_start_clears_the_hold(self):
        state, _ = apply(self.assessed_run(_awaiting_route_consent=True), "lead_started",
                         {"identity": "lead"})
        self.assertNotIn("_awaiting_route_consent", state.active_run.assessment)


class ReportedBlockerStopTests(unittest.TestCase):
    """#14: a known blocked result is not missing evidence."""

    def run_with_lead(self, *extra):
        lead = Delegation("lead", "lead", "Validate", "completed", "gpt", "high")
        return ProjectState(active_run=RunState(
            "run", "Validate", lead_identity="lead", delegations=(lead, *extra),
            provider="codex", session_id="root"))

    def stop(self, state, active=False):
        return apply(state, "stop_requested", {"provider": "codex", "hook_event_name": "Stop",
                                               "stop_hook_active": active})

    def test_reported_blocker_ends_every_turn_quietly_and_stays_resumable(self):
        state, _ = apply(self.run_with_lead(), "lead_failed",
                         {"identity": "lead", "outcome": {"status": "incomplete"}, "reported_outcome": True})
        self.assertEqual("incomplete", state.active_run.assessment["_reported_nonsuccess"])
        for turn in range(3):
            state, actions = self.stop(state)
            self.assertEqual([("permit_stop", {})], [(a.kind, dict(a.payload)) for a in actions], turn)
            self.assertEqual("recovering", state.active_run.status)
            self.assertIsNone(state.active_run.outcome, "a blocker is never a success")
        # The blocker is resolved later and the same lead finishes the run (#12).
        state, _ = apply(state, "lead_started", {"identity": "lead"})
        self.assertNotIn("_reported_nonsuccess", state.active_run.assessment)
        state, _ = apply(state, "lead_completed", {"identity": "lead", "outcome": {"status": "completed"}})
        state, actions = self.stop(state)
        self.assertIsNone(state.active_run)
        self.assertEqual("completed", state.recent_runs[-1].status)

    def test_unknown_or_unchecked_outcomes_still_block(self):
        reported = {"identity": "lead", "outcome": {"status": "blocked"}, "reported_outcome": True}
        for payload in ({"identity": "lead"},
                        # A native status fallback is host state, not the lead's report.
                        {"identity": "lead", "outcome": {"status": "blocked"}},
                        {**reported, "outcome": {"status": "completed"}},
                        {**reported, "native_host_failed": True},
                        {**reported, "reason": "substantive_child_missing"}):
            with self.subTest(payload=payload):
                state, _ = apply(self.run_with_lead(), "lead_failed", payload)
                self.assertNotIn("_reported_nonsuccess", state.active_run.assessment)
                _, actions = self.stop(state)
                self.assertEqual("block_stop", actions[0].kind)

    def test_active_work_still_blocks_after_a_reported_blocker(self):
        worker = Delegation("worker", "worker", "Validate", "working", "gpt", "medium")
        state, _ = apply(self.run_with_lead(worker), "lead_failed",
                         {"identity": "lead", "outcome": {"status": "blocked"}, "reported_outcome": True})
        _, actions = self.stop(state)
        self.assertEqual("block_stop", actions[0].kind)
        self.assertEqual(["worker"], actions[0].payload["active"])

    def test_child_evidence_after_the_report_brings_back_the_resume_guard(self):
        reviewer = Delegation("reviewer", "worker", "Review", "completed", "gpt", "medium")
        state, _ = apply(self.run_with_lead(reviewer), "lead_failed",
                         {"identity": "lead", "outcome": {"status": "blocked"}, "reported_outcome": True})
        _, actions = self.stop(state)
        self.assertEqual("permit_stop", actions[0].kind)
        # The reviewer's late result lands after the lead said it was blocked:
        # the lead never saw it, so the root must resume that lead.
        run = state.active_run
        children = {"reviewer": {"reviewed": True}}
        state = replace(state, active_run=replace(run, assessment={**run.assessment,
                                                                   "_substantive_children": children}))
        _, actions = self.stop(state)
        self.assertEqual("block_stop", actions[0].kind)
        self.assertEqual("lead_outcome_missing", actions[0].payload["reason"])

    def test_a_later_unknown_failure_replaces_the_reported_blocker(self):
        state, _ = apply(self.run_with_lead(), "lead_failed",
                         {"identity": "lead", "outcome": {"status": "blocked"}, "reported_outcome": True})
        state, _ = apply(state, "lead_failed", {"identity": "lead"})
        self.assertNotIn("_reported_nonsuccess", state.active_run.assessment)


if __name__ == "__main__":
    unittest.main()


class SettledCompletionNoticeTests(unittest.TestCase):
    """A wait notice rendered under the batch hold must not outlive it."""

    def test_settled_batch_drops_the_stale_wait_notice(self):
        from plugins.symphony.symphony.model import Action
        from plugins.symphony.symphony import runtime
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        held = RunState("run", "task", lead_identity="lead", status="completing", provider="claude",
                        session_id="root", outcome={"status": "completed"}, delegations=(lead,),
                        assessment={"_batch_pending": True})
        settled = replace(held, assessment={})
        before, after = ProjectState(active_run=held), ProjectState(active_run=settled)
        stale = Action("inject_context", {"text": runtime._WAIT_NOTICE + "pending launches. Reconcile."})
        source = Event("prompt", "user_prompt", "2026-10-04T00:00:00Z", {"prompt": "<task-notification>"})
        actions = runtime._refresh_completion_guidance(
            (stale, Action("inject_context", {"text": runtime._recovery_guidance(before, "claude")})),
            before, after, source, "claude")
        texts = [action.payload["text"] for action in actions]
        self.assertFalse(any(text.startswith(runtime._WAIT_NOTICE) for text in texts))
        self.assertTrue(any("end this root turn" in text for text in texts))


class ReviewFollowupTests(unittest.TestCase):
    """Regressions found by the independent 1.8.0 reviews."""

    def blocked_run(self, *extra, **assessment):
        lead = Delegation("lead", "lead", "Validate", "completed", "gpt", "high", updated_at="t1")
        state = ProjectState(active_run=RunState(
            "run", "Validate", lead_identity="lead", delegations=(lead, *extra),
            provider="claude", session_id="root", assessment=assessment))
        state, _ = apply(state, "lead_failed",
                         {"identity": "lead", "outcome": {"status": "blocked"}, "reported_outcome": True})
        return state

    def test_the_users_stop_control_closes_a_blocked_run_as_blocked(self):
        state, actions = apply(self.blocked_run(), "stop_requested", {"control": True})
        self.assertIsNone(state.active_run)
        self.assertEqual("blocked", state.recent_runs[-1].status)
        self.assertEqual({"status": "blocked"}, state.recent_runs[-1].outcome)
        self.assertIn("archive_run", [action.kind for action in actions])

    def test_the_users_stop_control_declines_a_held_lead_launch(self):
        assessor = Delegation("assessor", "assessor", "task", "completed", "m", "high")
        held = ProjectState(active_run=RunState("run", "task", delegations=(assessor,), provider="claude",
                                                session_id="root", assessment={"_awaiting_route_consent": True}))
        state, _ = apply(held, "stop_requested", {"control": True})
        self.assertIsNone(state.active_run)
        self.assertEqual("stopped", state.recent_runs[-1].status)

    def test_explicit_stop_reports_the_closed_run(self):
        from plugins.symphony.symphony import runtime
        state, actions = apply(self.blocked_run(), "stop_requested", {"control": True})
        rendered = runtime._render_actions(actions, state, "claude", "user_prompt")
        self.assertIn("closed the run", rendered[-1].payload["text"])

    def test_consent_hold_after_fast_escalation_lets_the_turn_end(self):
        lead = Delegation("fast", "lead", "task", "completed", "m", "medium")
        run = RunState("run", "task", lead_identity="fast", delegations=(lead,), provider="codex",
                       session_id="root", status="recovering",
                       assessment={"_fast_escalated": True, "_awaiting_route_consent": True,
                                   "substantive_contract": {"version": 2}})
        _, actions = apply(ProjectState(active_run=run), "stop_requested",
                           {"provider": "codex", "hook_event_name": "Stop"})
        self.assertEqual([("permit_stop", {})], [(a.kind, dict(a.payload)) for a in actions])

    def test_a_childs_later_turn_changes_the_evidence_basis(self):
        worker = Delegation("worker", "worker", "Validate", "completed", "gpt", "medium", updated_at="t1")
        state = self.blocked_run(worker)
        run = state.active_run
        again = replace(run, delegations=tuple(replace(item, updated_at="t9") if item.identity == "worker"
                                               else item for item in run.delegations))
        _, actions = apply(replace(state, active_run=again), "stop_requested",
                           {"provider": "claude", "hook_event_name": "Stop"})
        self.assertEqual("block_stop", actions[0].kind)

    def test_an_explicit_blocker_supersedes_an_earlier_missing_child_flag(self):
        state = self.blocked_run(_substantive_child_missing=True)
        self.assertEqual("blocked", state.active_run.assessment["_reported_nonsuccess"])
        self.assertNotIn("_substantive_child_missing", state.active_run.assessment)

    def test_deferred_wait_notice_is_dropped_once_the_run_settled(self):
        from plugins.symphony.symphony import runtime
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        notice = {"kind": "inject_context", "payload": {"text": runtime._WAIT_NOTICE + "pending launches."}}
        other = {"kind": "inject_context", "payload": {"text": "Keep this."}}
        run = RunState("run", "task", lead_identity="lead", status="completing", delegations=(lead,),
                       outcome={"status": "completed"},
                       assessment={"_pending_parent_actions": [notice, other]})
        _, actions = runtime._consume_parent_actions(ProjectState(active_run=run))
        self.assertEqual(["Keep this."], [action.payload["text"] for action in actions])
        busy = replace(run, assessment={**run.assessment, "_batch_pending": True})
        _, actions = runtime._consume_parent_actions(ProjectState(active_run=busy))
        self.assertEqual(2, len(actions))

    def test_repeated_identical_blocker_is_one_report_but_repeated_success_is_not(self):
        from plugins.symphony.symphony.runtime import _fast_report_claims_credit, _repeated_nonsuccess
        blocked = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"blocked"}'
        success = 'SYMPHONY_FAST_DECISION: eligible\nSYMPHONY_OUTCOME: {"status":"completed"}'
        self.assertFalse(_fast_report_claims_credit({"last_assistant_message": blocked + "\n" + blocked}))
        self.assertTrue(_fast_report_claims_credit({"last_assistant_message": success + "\n" + success}))
        self.assertTrue(_fast_report_claims_credit({"last_assistant_message": blocked.replace(
            "eligible", "escalate").replace(": escalate", ":escalate")}))
        self.assertTrue(_fast_report_claims_credit({"last_assistant_message":
            'SYMPHONY_OUTCOME: {"status":"blocked"}\nSYMPHONY_OUTCOME: {"status":"failed"}'}))
        self.assertTrue(_repeated_nonsuccess(['SYMPHONY_OUTCOME: {"status":"blocked"}'] * 2))
        self.assertFalse(_repeated_nonsuccess(['SYMPHONY_OUTCOME: {"status":"completed"}'] * 2))

    def test_escalation_marker_tolerates_spacing_like_the_runtime_parser(self):
        from plugins.symphony.symphony.host_evidence import _archived_fast_escalation
        for spacing in (" ", "  ", "\t"):
            self.assertTrue(_archived_fast_escalation(f"Handing off.\nSYMPHONY_FAST_DECISION:{spacing}escalate"))
        self.assertFalse(_archived_fast_escalation(
            'SYMPHONY_FAST_DECISION: escalate\nSYMPHONY_OUTCOME: {"status":"completed"}'))
        self.assertFalse(_archived_fast_escalation("SYMPHONY_FAST_DECISION: escalate\n" * 2))
