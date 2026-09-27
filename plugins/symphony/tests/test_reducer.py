from dataclasses import replace
import unittest

from plugins.symphony.symphony.model import Action, Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.reducer import reduce


NOW = "2026-09-17T12:00:00+00:00"


def event(kind, *, event_id=None, **payload):
    return Event(event_id or f"event-{kind}", kind, NOW, payload)


def delegation(identity, state="working", role="worker"):
    return Delegation(
        identity=identity,
        role=role,
        objective=f"Objective for {identity}",
        state=state,
        requested_tier="balanced",
        requested_effort="medium",
        updated_at=NOW,
    )


def running_state(*, enabled=True, status="active", lead="lead-1", delegations=(), outcome=None):
    return ProjectState(
        enabled=enabled,
        active_run=RunState(
            run_id="run-1",
            task="Ship Symphony",
            status=status,
            lead_identity=lead,
            delegations=tuple(delegations),
            outcome=outcome,
            started_at=NOW,
            updated_at=NOW,
        ),
    )


class LifecycleReducerTests(unittest.TestCase):
    def test_enablement_and_session_heartbeat_persist(self):
        state, actions = reduce(ProjectState(), event("enable"))
        state, heartbeat_actions = reduce(
            state,
            event(
                "session_heartbeat",
                provider="codex",
                session_id="session-1",
                plugin_version="1.0.0",
                plugin_root="/plugins/symphony/1.0.0",
                hook_schema_version=1,
            ),
        )

        self.assertTrue(state.enabled)
        self.assertEqual(state.activation["codex"]["state"], "guarded")
        self.assertEqual(state.activation["codex"]["session_id"], "session-1")
        self.assertEqual(state.activation["codex"]["plugin_root"], "/plugins/symphony/1.0.0")
        self.assertEqual(actions, (Action("project_enabled"),))
        self.assertEqual(heartbeat_actions, ())

    def test_active_owner_heartbeat_and_consent_survive_other_sessions(self):
        runs = {
            f"codex:owner-{index}": RunState(
                f"run-{index}", f"Task {index}", session_id=f"owner-{index}", provider="codex"
            )
            for index in range(6)
        }
        state = ProjectState(active_runs=runs)
        for index in range(12):
            session = f"owner-{index}" if index < 6 else f"visitor-{index}"
            state, _ = reduce(state, event(
                "session_heartbeat", event_id=f"heartbeat-{index}", provider="codex",
                session_id=session, plugin_version="1.4.7", plugin_root=f"/plugin/{session}",
                hook_schema_version=2, profile=f"profile-{session}",
            ))
            state, _ = reduce(state, event(
                "route_accepted", event_id=f"accepted-{index}", provider="codex",
                session_id=session, profile=f"profile-{session}", route="balanced",
            ))

        activation = state.activation["codex"]
        profiles = {item["session_id"]: item for item in activation["session_profiles"]}
        for index in range(6):
            session = f"owner-{index}"
            self.assertEqual(profiles[session]["plugin_root"], f"/plugin/{session}")
            self.assertEqual(profiles[session]["hook_schema_version"], 2)
            self.assertEqual(profiles[session]["observed_at"], NOW)
            self.assertEqual(activation["accepted"][session]["profile"], f"profile-{session}")

    def test_one_shot_managed_task_does_not_enable_project(self):
        state, actions = reduce(
            ProjectState(enabled=False),
            event("task_received", run_id="run-once", task="One task", one_shot=True),
        )

        self.assertFalse(state.enabled)
        self.assertEqual(state.active_run.run_id, "run-once")
        self.assertEqual(state.active_run.status, "assessing")
        self.assertEqual(actions, (Action("request_assessment", {"run_id": "run-once"}),))

    def test_disabled_project_ignores_an_ordinary_task(self):
        original = ProjectState(enabled=False)

        state, actions = reduce(original, event("task_received", task="Ordinary task"))

        self.assertEqual(state, original)
        self.assertEqual(actions, ())

    def test_bypass_is_inert_with_respect_to_project_and_run_state(self):
        original = running_state()

        state, actions = reduce(original, event("bypass", task="Run outside Symphony"))

        self.assertEqual(state.enabled, original.enabled)
        self.assertEqual(state.active_run, original.active_run)
        self.assertEqual(actions, (Action("execute_bypass", {"task": "Run outside Symphony"}),))

    def test_assessment_events_advance_only_the_current_run(self):
        original = running_state(status="assessing", lead=None)
        requested, request_actions = reduce(original, event("assessment_requested"))
        accepted, accepted_actions = reduce(
            requested,
            event("assessment_accepted", size="medium", complexity="mixed", risk="normal"),
        )

        self.assertEqual(request_actions, (Action("spawn_assessor", {"run_id": "run-1"}),))
        self.assertEqual(accepted.active_run.status, "assessed")
        self.assertEqual(accepted.active_run.assessment["size"], "medium")
        self.assertEqual(accepted_actions, (Action("route_run", {"run_id": "run-1"}),))

    def test_owner_generation_changes_only_for_a_safe_replacement(self):
        original = running_state(status="active")
        rejected, rejected_actions = reduce(
            original,
            event("lead_started", identity="lead-2", owner_generation=2),
        )
        interrupted, _ = reduce(original, event("interrupt", reason="host interrupt"))
        recovering, recovery_actions = reduce(
            interrupted,
            event("resume_reconciled", active_ids=[]),
        )
        replaced, replacement_actions = reduce(
            recovering,
            event("lead_started", identity="lead-2", owner_generation=2),
        )

        self.assertEqual(rejected.active_run.lead_identity, "lead-1")
        self.assertEqual(rejected.active_run.owner_generation, 1)
        self.assertEqual(rejected_actions, (Action("reject_lead_replacement", {"identity": "lead-2"}),))
        self.assertEqual(recovery_actions, (Action("replace_lead", {"owner_generation": 2}),))
        self.assertEqual(replaced.active_run.lead_identity, "lead-2")
        self.assertEqual(replaced.active_run.owner_generation, 2)
        self.assertEqual(replacement_actions, ())

    def test_delegation_updates_replace_the_identity_latest_record(self):
        original = running_state(delegations=[delegation("worker-1", "working")])

        state, actions = reduce(
            original,
            event(
                "delegation_updated",
                identity="worker-1",
                role="worker",
                objective="Objective for worker-1",
                state="completed",
                requested_tier="balanced",
                requested_effort="medium",
            ),
        )

        self.assertEqual(len(state.active_run.delegations), 1)
        self.assertEqual(state.active_run.delegations[0].state, "completed")
        self.assertEqual(actions, ())

    def test_normal_stop_is_blocked_while_a_child_is_active(self):
        original = running_state(delegations=[delegation("worker-1")])
        stop = event("stop_requested")

        state, actions = reduce(original, stop)
        replayed, replay_actions = reduce(state, stop)

        self.assertEqual(state.active_run, original.active_run)
        self.assertEqual(actions, (Action("block_stop", {"active": ["worker-1"]}),))
        self.assertEqual(replayed, state)
        self.assertEqual(replay_actions, actions)

    def test_background_stop_requires_a_tracked_active_assessor_or_lead(self):
        state = running_state(delegations=[delegation("assessor-1", role="assessor")])
        for tasks in (
            [{"id": "shell-1", "type": "shell"}],
            [{"id": "assessor-1", "type": "shell"}],
            [{"id": "other", "type": "subagent"}],
            [{"type": "subagent"}],
            [{"id": "assessor-1"}],
        ):
            with self.subTest(tasks=tasks):
                _, actions = reduce(state, event("stop_requested", background_tasks=tasks))
                self.assertEqual(actions, (Action("block_stop", {"active": ["assessor-1"]}),))

        _, actions = reduce(state, event(
            "stop_requested", background_tasks=[{"id": "assessor-1", "type": "subagent"}]
        ))
        self.assertEqual(actions, (Action("permit_stop"),))

    def test_background_stop_does_not_wait_for_worker_or_completed_lead(self):
        for tracked in (delegation("worker-1"), delegation("lead-1", "completed", "lead")):
            with self.subTest(tracked=tracked):
                state = running_state(delegations=[tracked])
                _, actions = reduce(state, event(
                    "stop_requested", background_tasks=[{"id": tracked.identity, "type": "subagent"}]
                ))
                self.assertEqual(actions[0].kind, "block_stop")

    def test_lead_completion_archives_run_and_permits_completion(self):
        original = running_state(delegations=[delegation("worker-1", "completed")])

        state, actions = reduce(
            original,
            event(
                "lead_completed",
                identity="lead-1",
                owner_generation=1,
                outcome={"summary": "Done", "verified": True},
            ),
        )

        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "completed")
        self.assertEqual(state.recent_runs[-1].outcome["summary"], "Done")
        self.assertEqual(actions, (Action("permit_completion", {"run_id": "run-1"}),))

    def test_lead_completion_waits_for_active_children(self):
        original = running_state(delegations=[delegation("worker-1")])

        state, actions = reduce(
            original,
            event(
                "lead_completed",
                identity="lead-1",
                owner_generation=1,
                outcome={"summary": "Done"},
            ),
        )

        self.assertEqual(state.active_run.status, "completing")
        self.assertEqual(state.active_run.outcome, {"summary": "Done"})
        self.assertEqual(actions, (Action("wait_for_delegations", {"active": ["worker-1"]}),))

    def test_lead_completion_requires_an_outcome(self):
        original = running_state()

        state, actions = reduce(
            original,
            event("lead_completed", identity="lead-1", owner_generation=1),
        )

        self.assertEqual(state.active_run, original.active_run)
        self.assertEqual(actions, (Action("block_completion", {"reason": "outcome_missing"}),))

    def test_replayed_event_is_idempotent(self):
        enable = event("enable", event_id="stable-event")
        state, _ = reduce(ProjectState(), enable)

        replayed, actions = reduce(state, enable)

        self.assertEqual(replayed, state)
        self.assertEqual(actions, ())

    def test_interrupt_preserves_recoverable_run_and_live_lead_on_resume(self):
        original = running_state(delegations=[delegation("lead-1", role="lead")])
        interrupted, interrupt_actions = reduce(original, event("interrupt", reason="user interrupt"))
        resumed, resume_actions = reduce(
            interrupted,
            event("resume_reconciled", active_ids=["lead-1"]),
        )

        self.assertEqual(interrupted.active_run.status, "interrupted")
        self.assertEqual(interrupt_actions, (Action("preserve_recovery_context", {"run_id": "run-1"}),))
        self.assertEqual(resumed.active_run.status, "active")
        self.assertEqual(resumed.active_run.lead_identity, "lead-1")
        self.assertEqual(resume_actions, ())

    def test_recovery_can_assign_a_new_generation_when_no_lead_was_registered(self):
        original = running_state(status="interrupted", lead=None)
        recovering, actions = reduce(original, event("resume_reconciled", active_ids=[]))

        replaced, replacement_actions = reduce(
            recovering,
            event("lead_started", identity="lead-2", owner_generation=2),
        )

        self.assertEqual(actions, (Action("replace_lead", {"owner_generation": 2}),))
        self.assertEqual(replaced.active_run.lead_identity, "lead-2")
        self.assertEqual(replaced.active_run.owner_generation, 2)
        self.assertEqual(replacement_actions, ())

    def test_reassessment_preserves_active_ownership_and_work(self):
        original = running_state(delegations=[delegation("worker-1")])

        state, actions = reduce(original, event("reassess", reason="scope drift"))

        self.assertEqual(state.active_run.lead_identity, "lead-1")
        self.assertEqual(state.active_run.delegations, original.active_run.delegations)
        self.assertTrue(state.needs_reassessment)
        self.assertEqual(actions, (Action("request_assessment", {"run_id": "run-1"}),))

    def test_reassessment_acceptance_preserves_recovery_and_lifecycle_metadata(self):
        original = running_state(status="recovering")
        original = replace(
            original,
            active_run=replace(
                original.active_run,
                assessment={
                    "size": "small",
                    "complexity": "simple",
                    "_invalid_consultants": ["consultant-1"],
                    "_pending_delegations": [{"role": "worker"}],
                },
            ),
        )

        accepted, _ = reduce(
            original,
            event("assessment_accepted", size="medium", complexity="mixed", risk="normal"),
        )

        self.assertEqual(accepted.active_run.status, "recovering")
        self.assertEqual(accepted.active_run.assessment["size"], "medium")
        self.assertEqual(
            accepted.active_run.assessment["_invalid_consultants"],
            ["consultant-1"],
        )
        self.assertEqual(
            accepted.active_run.assessment["_pending_delegations"],
            [{"role": "worker"}],
        )

    def test_disable_waits_for_observed_agents_before_archiving(self):
        original = running_state(delegations=[delegation("worker-1")])

        state, actions = reduce(original, event("disable"))

        self.assertFalse(state.enabled)
        self.assertEqual(state.active_run.status, "stopping")
        self.assertEqual(
            actions,
            (
                Action("stop_delegations", {"active": ["worker-1"]}),
                Action("project_disabled"),
            ),
        )

        stopped, stop_actions = reduce(
            state,
            event("delegation_updated", identity="worker-1", state="completed"),
        )
        self.assertIsNone(stopped.active_run)
        self.assertEqual(stopped.recent_runs[-1].status, "disabled")
        self.assertEqual(stop_actions, (Action("archive_run", {"run_id": "run-1"}),))

    def test_force_stop_archives_without_disabling_the_project(self):
        original = running_state(delegations=[delegation("worker-1"), delegation("done", "completed")])

        state, actions = reduce(original, event("force_stop"))

        self.assertTrue(state.enabled)
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs[-1].status, "force_stopped")
        self.assertEqual(state.recent_runs[-1].unreconciled, ("worker-1",))
        self.assertIsNone(state.recent_runs[-1].outcome)
        late, late_actions = reduce(state, event(
            "lead_completed", identity="lead-1", owner_generation=1,
            outcome={"summary": "late completion"},
        ))
        self.assertEqual(late, state)
        self.assertEqual(late_actions, ())
        self.assertEqual(
            actions,
            (
                Action("stop_delegations", {"active": ["worker-1"]}),
                Action("archive_run", {"run_id": "run-1"}),
                Action("permit_stop"),
            ),
        )

    def test_unknown_event_does_not_change_state(self):
        original = running_state()

        state, actions = reduce(original, event("future_event"))

        self.assertEqual(state, original)
        self.assertEqual(actions, ())


    def test_repeated_stop_releases_the_turn_and_preserves_unfinished_work(self):
        state = running_state(delegations=[delegation("w1")])

        blocked, actions = reduce(state, event("stop_requested", event_id="stop-1"))
        self.assertEqual([item.kind for item in actions], ["block_stop"])
        self.assertIsNotNone(blocked.active_run)

        released, retry_actions = reduce(
            blocked, event("stop_requested", event_id="stop-2", stop_hook_active=True)
        )

        self.assertIn("permit_stop", [item.kind for item in retry_actions])
        self.assertEqual(released.active_run, blocked.active_run)
        self.assertEqual(released.recent_runs, ())

    def test_replayed_launch_failure_removes_only_one_pending_intent(self):
        intent = {"role": "worker", "model": "model", "effort": "high"}
        state = running_state()
        state = replace(state, active_run=replace(state.active_run, assessment={"_pending_delegations": [intent, intent]}))
        failure = event("delegation_launch_failed", role="worker", requested_tier="model", requested_effort="high")
        failed, _ = reduce(state, failure)
        replayed, _ = reduce(failed, failure)
        self.assertEqual(len(replayed.active_run.assessment["_pending_delegations"]), 1)
        self.assertEqual(replayed, failed)

    def test_late_worker_failure_invalidates_earlier_lead_completion(self):
        for status in ("failed", "interrupted", "cancelled", "error"):
            with self.subTest(status=status):
                state = running_state(status="completing", delegations=[delegation("worker")], outcome={"status": "completed"})
                state = replace(state, active_run=replace(state.active_run, assessment={"_pending_lead_completion": {"outcome": {"status": "completed"}}}))
                failed, actions = reduce(state, event("delegation_updated", identity="worker", state=status))
                self.assertIsNotNone(failed.active_run)
                self.assertEqual(failed.active_run.status, "recovering")
                self.assertIsNone(failed.active_run.outcome)
                self.assertNotIn("_pending_lead_completion", failed.active_run.assessment)
                self.assertEqual(actions, (Action("replace_lead", {"owner_generation": 2}),))
                self.assertEqual(failed.recent_runs, ())

    def test_same_agent_terminal_recovery_allows_fresh_lead_completion(self):
        original = replace(delegation("old", "interrupted"), objective="Build the widget")
        state = running_state(status="recovering", delegations=[original])
        recovered, _ = reduce(state, event("delegation_updated", identity="old", role="worker", objective="Build the widget", state="completed"))
        self.assertEqual(recovered.active_run.delegations[0].state, "completed")
        registered, _ = reduce(recovered, event("lead_started", identity="fresh-lead", owner_generation=2))
        completed, _ = reduce(registered, event("lead_completed", identity="fresh-lead", owner_generation=2, outcome={"status": "completed"}))
        self.assertIsNone(completed.active_run)
        self.assertEqual(completed.recent_runs[-1].status, "completed")

    def test_retry_does_not_supersede_uncorrelated_or_live_work(self):
        old = replace(delegation("old", "interrupted"), objective="Build the widget")
        for records, role, objective, status in (
            ([old], "worker", old.objective, "completed"),
            ([old], "worker", "Different work", "completed"),
            ([replace(old, objective="")], "worker", "", "completed"),
            ([old], "consultant", old.objective, "completed"),
            ([old], "worker", old.objective, "working"),
            ([old, replace(old, identity="another")], "worker", old.objective, "completed"),
            ([old, replace(old, identity="live", state="working")], "worker", old.objective, "completed"),
        ):
            with self.subTest(records=records, role=role, objective=objective, status=status):
                state = running_state(status="recovering", delegations=records)
                retried, _ = reduce(state, event("delegation_updated", identity="retry", role=role, objective=objective, state=status))
                original = next(item for item in retried.active_run.delegations if item.identity == "old")
                self.assertEqual(original.state, "interrupted")
                completed, _ = reduce(retried, event("lead_completed", identity="lead-1", outcome={"status": "completed"}))
                self.assertIsNotNone(completed.active_run)

    def test_empty_host_roster_cannot_complete_an_unreconciled_worker(self):
        state = running_state(status="completing", delegations=[delegation("worker")], outcome={"status": "completed"})
        reconciled, _ = reduce(state, event("resume_reconciled", active_ids=[]))
        self.assertEqual(reconciled.active_run.status, "completing")
        self.assertEqual(reconciled.active_run.delegations[0].state, "interrupted")
        stopped, actions = reduce(reconciled, event("stop_requested"))
        self.assertIsNotNone(stopped.active_run)
        self.assertIn("interrupted", actions[0].payload["reason"])
        finished, _ = reduce(stopped, event("delegation_updated", identity="worker", state="completed"))
        self.assertIsNone(finished.active_run)

    def test_pending_spawn_prevents_archiving_an_unstarted_run(self):
        state = running_state(lead=None)
        state = replace(state, active_run=replace(state.active_run, assessment={
            "_pending_delegations": [{"role": "assessor"}],
        }))
        updated, actions = reduce(state, event("stop_requested"))
        self.assertEqual(updated.active_run, state.active_run)
        self.assertEqual(actions[0].kind, "block_stop")
        self.assertIn("assessor", actions[0].payload["reason"])

    def test_completion_waits_for_pending_spawns_then_last_worker_finishes(self):
        state = running_state(delegations=[delegation("lead-1", "completed", "lead")])
        state = replace(state, active_run=replace(state.active_run, assessment={
            "_pending_delegations": [{"role": "worker"}],
        }))
        waiting, actions = reduce(state, event("lead_completed", identity="lead-1", outcome={"status": "completed"}))
        self.assertEqual(waiting.active_run.status, "completing")
        self.assertEqual(actions[0].kind, "wait_for_delegations")
        waiting = replace(waiting, active_run=replace(waiting.active_run, assessment={}))
        done, _ = reduce(waiting, event("delegation_updated", identity="worker-1", state="completed"))
        self.assertIsNone(done.active_run)
        self.assertEqual(done.recent_runs[-1].status, "completed")

    def test_malformed_outcomes_cannot_complete_a_run(self):
        for outcome in ({}, [], "done", False, {"status": "blocked"}, {"status": {}}):
            with self.subTest(outcome=outcome):
                state, actions = reduce(running_state(), event("lead_completed", identity="lead-1", outcome=outcome))
                self.assertIsNotNone(state.active_run)
                self.assertEqual(actions[0].kind, "block_completion")

    def test_run_without_delegations_never_holds_the_session(self):
        state = running_state(lead=None, delegations=[])

        released, actions = reduce(state, event("stop_requested"))

        self.assertIn("permit_stop", [item.kind for item in actions])
        self.assertIsNone(released.active_run)

    def test_first_lead_after_interrupt_registers_at_the_current_generation(self):
        state = running_state(status="interrupted", lead=None)

        registered, actions = reduce(state, event("lead_started", identity="lead-1"))

        self.assertEqual([item.kind for item in actions], [])
        self.assertEqual(registered.active_run.lead_identity, "lead-1")
        self.assertEqual(registered.active_run.owner_generation, 1)
        self.assertEqual(registered.active_run.status, "active")

    def test_stale_generation_is_ignored_for_the_registered_lead(self):
        state = running_state()

        unchanged, actions = reduce(
            state, event("lead_started", identity="lead-1", owner_generation=7)
        )

        self.assertEqual([item.kind for item in actions], ["ignore_stale_owner"])
        self.assertEqual(unchanged.active_run.owner_generation, 1)

    def test_completion_from_a_stale_owner_is_ignored(self):
        state = running_state(delegations=[delegation("lead-1", state="completed", role="lead")])

        unchanged, actions = reduce(
            state,
            event("lead_completed", identity="lead-9", owner_generation=1, outcome={"status": "completed"}),
        )

        self.assertEqual([item.kind for item in actions], ["ignore_stale_owner"])
        self.assertIsNotNone(unchanged.active_run)
        self.assertIsNone(unchanged.active_run.outcome)

    def test_resume_without_observed_agents_marks_every_delegation_interrupted(self):
        state = running_state(delegations=[delegation("w1"), delegation("w2")])

        recovered, actions = reduce(state, event("resume_reconciled", active_ids=[]))

        self.assertEqual([item.kind for item in actions], ["replace_lead"])
        self.assertEqual(recovered.active_run.status, "recovering")
        self.assertEqual(
            {item.state for item in recovered.active_run.delegations}, {"interrupted"}
        )


if __name__ == "__main__":
    unittest.main()
