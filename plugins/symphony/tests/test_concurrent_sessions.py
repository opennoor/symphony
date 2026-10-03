"""Two things Symphony decided correctly and then failed to act on.

A user reported leads multiplying and heartbeats doubling. The lifecycle
reducer turned out to be right every time: it kept one lead and refused the
rest. What it did not do was tell anyone, because the renderer silently drops
any action kind it has no branch for. Separately, any session heartbeat from a
second terminal in the same project was taken as proof the owning process had
died, which killed a live lead and rewrote the run's session to the stranger.
"""

import json
import multiprocessing
import re
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from plugins.symphony.symphony import runtime as runtime_module
from plugins.symphony.symphony.model import Action, Delegation, ProjectState, RunState
from plugins.symphony.symphony.runtime import _handle_core as handle
from plugins.symphony.symphony.routing import profiles_for, snapshot_for
from plugins.symphony.symphony.store import StateStore

FULL_SIMPLE = snapshot_for("codex", "full").matrix["small/simple"]
BASE_SIMPLE = snapshot_for("codex", "base").matrix["small/simple"]
FULL_MATRIX = snapshot_for("codex", "full").matrix
BASE_MATRIX = snapshot_for("codex", "base").matrix
DRIFT_CELL = next(cell for cell in FULL_MATRIX if FULL_MATRIX[cell] != BASE_MATRIX[cell])
DRIFT_SIZE, DRIFT_COMPLEXITY = DRIFT_CELL.split("/")
FULL_DRIFT = FULL_MATRIX[DRIFT_CELL]
BASE_DRIFT = BASE_MATRIX[DRIFT_CELL]
CODEX_STRONGEST = profiles_for("codex")[0]["tiers"]["strongest"]

MARKER = json.dumps(
    {"size": DRIFT_SIZE, "complexity": DRIFT_COMPLEXITY, "risk": "normal",
     "rationale": "bounded", "topology": "direct"}
)


def _foreign_heartbeats(project: str, state_dir: str, provider: str, profile: str,
                        session: str, progress=None):
    env = {"SYMPHONY_STATE_DIR": state_dir, "SYMPHONY_PROFILE": profile}
    for index in range(105):
        handle({"provider": provider, "session_id": session, "cwd": project,
                "hook_event_name": "SessionStart", "turn_id": str(index)}, env)
        if progress is not None:
            progress.value = index + 1


class ActionCoverageTests(unittest.TestCase):
    """No action the reducer emits may vanish on the way to the host."""

    def test_every_emitted_action_is_rendered_or_declared_internal(self):
        root = Path(__file__).resolve().parents[1] / "symphony"
        emitted = set()
        for name in ("reducer.py", "runtime.py"):
            emitted |= set(re.findall(r'Action\(\s*"([a-z_]+)"', (root / name).read_text()))
        # A populated payload, so a branch renders real values rather than the
        # literal word None, and so the catch-all is distinguishable from a
        # real branch. Asserting only "something came back" proves nothing:
        # the catch-all guarantees that for every kind, invented ones included.
        payload = {
            "identity": "agent-1", "session_id": "session-1", "run_id": "run-1",
            "active": ("agent-2",), "unreachable": (), "unreconciled": ("agent-3",),
            "reason": "outcome_missing", "task": "ship it", "owner_generation": 2,
        }
        unhandled, leaky = [], []
        for kind in sorted(emitted - set(runtime_module.INTERNAL_ACTIONS)):
            produced = runtime_module._render_actions(
                (Action(kind, payload),), ProjectState(), "codex", ""
            )
            if not produced:
                unhandled.append(kind)
                continue
            text = produced[0].payload.get("text") or produced[0].payload.get("reason") or ""
            if "has no message for" in text:
                unhandled.append(kind)
            if "None" in text:
                leaky.append(kind)
        self.assertEqual(
            [], unhandled,
            f"emitted actions with no real message, so the host never learns of them: {unhandled}",
        )
        self.assertEqual([], leaky, f"these render a literal None into their text: {leaky}")

    def test_the_catch_all_is_what_makes_an_unknown_action_visible(self):
        """Guards the guard: an invented kind must still surface loudly."""
        produced = runtime_module._render_actions(
            (Action("totally_made_up_kind", {}),), ProjectState(), "codex", ""
        )
        self.assertTrue(produced)
        self.assertIn("has no message for", produced[0].payload["text"])


class PacketCompletenessTests(unittest.TestCase):
    """A lead spawned without the conversation only gets what the packet says."""

    def test_guidance_tells_the_root_to_relay_every_part_of_the_task(self):
        guidance = runtime_module._assessment_guidance("fix a, b, c, d and e", "codex")
        self.assertIn("in full", guidance)
        self.assertIn("every part", guidance)
        self.assertIn("fix a, b, c, d and e", guidance)


class GovernanceLabelTests(unittest.TestCase):
    """A one-shot run and a governed one look identical until they diverge.

    A user who ran `start` believes the project is enabled, works for hours,
    and never sees that every prompt after the first was ungoverned.
    """

    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.environ = {"SYMPHONY_STATE_DIR": str(root / "state"), "SYMPHONY_PROFILE": "full"}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, prompt):
        return {"session_id": "s1", "cwd": str(self.project), "hook_event_name": "UserPromptSubmit",
                "prompt": prompt, "turn_id": "t", "model": "m"}

    def text(self, result):
        out = json.loads(result.stdout) if result.stdout else {}
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def open_lead(self):
        handle({**self.payload(""), "hook_event_name": "SessionStart"}, self.environ)
        for role, model, effort in (("assessor", CODEX_STRONGEST, "high"), ("lead", FULL_SIMPLE["model"], FULL_SIMPLE["effort"])):
            body = f"SYMPHONY_ROLE: {role}\n" + (f"SYMPHONY_ROUTE: {MARKER}\n" if role == "lead" else "")
            handle({**self.payload(""), "hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                    "tool_input": {"message": body + "Ship it", "model": model,
                                   "reasoning_effort": effort}}, self.environ)
        handle({**self.payload(""), "hook_event_name": "SubagentStart", "agent_id": "lead-1",
                "agent_type": "symphony_lead_x_medium", "model": FULL_SIMPLE["model"],
                "model_reasoning_effort": FULL_SIMPLE["effort"]}, self.environ)

    def test_a_one_shot_run_labels_its_lead_transactional(self):
        handle(self.payload("$symphony:symphony start Ship it"), self.environ)
        self.open_lead()

        status = self.text(handle(self.payload("$symphony:symphony status"), self.environ))

        self.assertIn("[transactional]", status)
        self.assertNotIn("[enabled]", status)

    def test_an_enabled_project_labels_its_lead_enabled(self):
        handle(self.payload("$symphony:symphony enable"), self.environ)
        self.open_lead()

        status = self.text(handle(self.payload("$symphony:symphony status"), self.environ))

        self.assertIn("[enabled]", status)
        self.assertNotIn("[transactional]", status)

    def test_a_transactional_run_says_the_next_prompt_is_ungoverned(self):
        handle(self.payload("$symphony:symphony start Ship it"), self.environ)
        self.open_lead()

        status = self.text(handle(self.payload("$symphony:symphony status"), self.environ))

        self.assertIn("ungoverned", status.lower())
        self.assertIn("enable", status.lower())

    def test_a_live_run_speaks_even_when_the_project_is_not_enabled(self):
        """Silence here let the root do the work beside a lead it never saw."""
        handle(self.payload("$symphony:symphony start Ship it"), self.environ)
        self.open_lead()

        guidance = self.text(handle(self.payload("keep going on the other thing"), self.environ))

        self.assertTrue(guidance, "a live run produced no guidance at all")
        self.assertIn("remains active", guidance)

    def test_the_label_reaches_the_guidance_a_live_run_injects(self):
        handle(self.payload("$symphony:symphony start Ship it"), self.environ)
        self.open_lead()

        guidance = self.text(handle(self.payload("keep going on the other thing"), self.environ))

        self.assertIn("[transactional]", guidance)


class ConcurrentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.state_root = root / "state"
        self.environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROFILE": "full"}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, session, event="UserPromptSubmit", **extra):
        base = {
            "session_id": session, "cwd": str(self.project), "hook_event_name": event,
            "prompt": "", "turn_id": "turn-1", "model": "codex-model",
        }
        base.update(extra)
        return base

    def out(self, result):
        return json.loads(result.stdout) if result.stdout else {}

    def text(self, result):
        o = self.out(result)
        return o.get("hookSpecificOutput", {}).get("additionalContext", "") or o.get("reason", "")

    def spawn_in(self, session, env, role, model, effort, marker=""):
        body = f"SYMPHONY_ROLE: {role}\n" + (f"SYMPHONY_ROUTE: {marker}\n" if marker else "")
        return handle(self.payload(session, "PreToolUse", tool_name="spawn_agent",
                                   tool_input={"message": body + "Ship it", "model": model,
                                               "reasoning_effort": effort}), env)

    def spawn(self, session, role, model, effort, marker=""):
        body = f"SYMPHONY_ROLE: {role}\n" + (f"SYMPHONY_ROUTE: {marker}\n" if marker else "")
        return handle(self.payload(session, "PreToolUse", tool_name="spawn_agent",
                                   tool_input={"message": body + "Ship it", "model": model,
                                               "reasoning_effort": effort}), self.environ)

    def start_agent(self, session, agent_id, role, model, effort):
        return handle(self.payload(session, "SubagentStart", agent_id=agent_id,
                                   agent_type=f"symphony_{role}_{model.replace('.','_').replace('-','_')}_{effort}",
                                   model=model, model_reasoning_effort=effort), self.environ)

    def stop_agent(self, session, agent_id, message="done"):
        return handle(self.payload(session, "SubagentStop", agent_id=agent_id,
                                   last_assistant_message=message), self.environ)

    def run_with_live_lead(self, session="root-a"):
        handle(self.payload(session, "SessionStart"), self.environ)
        self.spawn(session, "assessor", CODEX_STRONGEST, "high")
        self.start_agent(session, "assessor-1", "assessor", CODEX_STRONGEST, "high")
        self.spawn(session, "lead", FULL_SIMPLE["model"], FULL_SIMPLE["effort"], MARKER)
        self.start_agent(session, "lead-1", "lead", FULL_SIMPLE["model"], FULL_SIMPLE["effort"])

    def state(self):
        path = next(self.state_root.glob("*.v2.json"))
        return json.loads(path.read_text())

    # ---- the renderer must speak ------------------------------------------
    def test_a_second_lead_is_told_it_is_not_the_lead(self):
        self.run_with_live_lead()
        spoken = self.text(self.start_agent("root-a", "lead-2", "lead", FULL_SIMPLE["model"], FULL_SIMPLE["effort"]))
        self.assertTrue(spoken, "Symphony rejected a second lead and said nothing")
        self.assertIn("lead", spoken.lower())

    def test_a_stale_completion_is_not_silently_swallowed(self):
        self.run_with_live_lead()
        self.start_agent("root-a", "lead-2", "lead", FULL_SIMPLE["model"], FULL_SIMPLE["effort"])
        self.stop_agent("root-a", "lead-2")
        spoken = self.text(handle(self.payload("root-a"), self.environ))
        self.assertTrue(spoken, "a completion from a non-lead was ignored silently")

    # ---- a second terminal must not kill a live run -----------------------
    def test_a_concurrent_session_does_not_declare_a_live_lead_dead(self):
        self.run_with_live_lead("root-a")
        spoken = self.text(handle(self.payload("other-b", "SessionStart"), self.environ))

        run = self.state()["active_run"]
        self.assertEqual("root-a", run["session_id"], "a stranger took ownership of the run")
        self.assertNotIn("unavailable", spoken.lower())
        self.assertNotIn("replacement", spoken.lower())
        statuses = {a.get("status") for a in (run.get("agent_records") or {}).values()}
        self.assertNotIn("interrupted", statuses, "a live lead was declared dead")

    def test_foreign_resume_or_empty_roster_cannot_reconcile_a_live_owner(self):
        self.run_with_live_lead("root-a")
        path = next(self.state_root.glob("*.v2.json"))
        original = path.read_text()
        for evidence in ({"source": "resume"}, {"active_agent_ids": []}):
            with self.subTest(evidence=evidence):
                path.write_text(original)
                handle(self.payload("other-b", "SessionStart", **evidence), self.environ)
                run = self.state()["active_run"]
                self.assertEqual("root-a", run["session_id"])
                self.assertEqual("active", run["status"])
                self.assertEqual("working", next(
                    item["state"] for item in run["delegations"] if item["identity"] == "lead-1"
                ))

    def test_a_concurrent_session_does_not_destroy_an_accepted_clamp(self):
        environ = {**self.environ, "SYMPHONY_PROFILE": "base"}
        handle(self.payload("root-a", "SessionStart"), environ)
        handle({**self.payload("root-a"), "prompt": "$symphony:symphony proceed"}, environ)
        handle(self.payload("other-b", "SessionStart"), environ)

        accepted = (self.state()["activation"]["codex"].get("accepted") or {})
        self.assertIn("root-a", accepted, "a stranger's heartbeat erased this session's consent")
        self.assertTrue(accepted["root-a"].get("profile"))

    def test_consent_recorded_before_it_was_keyed_by_session_survives(self):
        """An upgraded machine carries records with one unkeyed slot."""
        handle(self.payload("old-s", "SessionStart"), self.environ)
        path = next(self.state_root.glob("*.v2.json"))
        document = json.loads(path.read_text())
        document["activation"]["codex"] = {
            "session_id": "old-s", "profile": "base", "accepted_profile": "base",
            "accepted_route": "gpt-5.5/medium", "plugin_version": "1.1.0",
        }
        path.write_text(json.dumps(document))

        handle(self.payload("old-s"), self.environ)
        handle(self.payload("stranger", "SessionStart"), self.environ)

        accepted = self.state()["activation"]["codex"].get("accepted") or {}
        self.assertEqual("base", accepted.get("old-s", {}).get("profile"))

    def test_a_naive_timestamp_does_not_transfer_another_sessions_run(self):
        self.run_with_live_lead("root-a")
        path = next(self.state_root.glob("*.v2.json"))
        document = json.loads(path.read_text())
        document["active_runs"]["codex:root-a"]["owner_seen_at"] = "2020-01-01T00:00:00"
        path.write_text(json.dumps(document))

        handle(self.payload("later-c", "SessionStart"), self.environ)

        self.assertEqual("root-a", self.state()["active_run"]["session_id"])

    def test_consent_still_holds_at_the_gate_after_a_stranger_heartbeats(self):
        """The gate runs on a spawn, which fires no heartbeat of its own.

        Carrying consent correctly is useless if the check that blocks the
        spawn reads a different field, which is what it did.
        """
        env = {**self.environ, "SYMPHONY_PROFILE": "base"}
        handle(self.payload("root-a", "SessionStart"), env)
        self.spawn_in("root-a", env, "assessor", CODEX_STRONGEST, "high")
        blocked = self.text(self.spawn_in("root-a", env, "lead", BASE_DRIFT["model"], BASE_DRIFT["effort"], MARKER))
        self.assertIn("proceed", blocked)

        handle({**self.payload("root-a"), "prompt": "$symphony:symphony proceed"}, env)
        handle(self.payload("stranger", "SessionStart"), env)

        after = self.text(self.spawn_in("root-a", env, "lead", BASE_DRIFT["model"], BASE_DRIFT["effort"], MARKER))
        self.assertNotIn(
            "proceed", after, "a stranger's heartbeat undid the clamp this session accepted"
        )

    def test_stale_run_remains_with_its_original_session(self):
        self.run_with_live_lead("root-a")
        path = next(self.state_root.glob("*.v2.json"))
        document = json.loads(path.read_text())
        document["active_runs"]["codex:root-a"]["owner_seen_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(document))

        handle(self.payload("later-c", "SessionStart"), self.environ)
        self.assertEqual("root-a", self.state()["active_run"]["session_id"])

        spoken = self.text(handle(self.payload("fourth-d", "SessionStart"), self.environ))
        self.assertEqual("root-a", self.state()["active_run"]["session_id"])
        self.assertNotIn("root-a", spoken)

    def test_a_second_session_is_not_told_to_reconcile_a_foreign_run(self):
        self.run_with_live_lead("root-a")
        spoken = self.text(handle(self.payload("other-b", "SessionStart"), self.environ))

        self.assertNotIn("root-a", spoken)
        self.assertNotIn("Reconcile observed agents", spoken)
        self.assertNotIn("--force", spoken)

    # A timeout is not proof that another root may claim or cancel a run.
    def test_a_quiet_run_is_not_adopted_by_a_foreign_root(self):
        self.run_with_live_lead("root-a")
        path = next(self.state_root.glob("*.v2.json"))
        document = json.loads(path.read_text())
        document["active_runs"]["codex:root-a"]["owner_seen_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(document))

        handle(self.payload("later-c", "SessionStart"), self.environ)

        run = self.state()["active_run"]
        self.assertEqual("root-a", run["session_id"])

    def test_foreign_churn_cannot_replay_a_recovered_lead_failure(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                route = snapshot_for(provider, profile).matrix["small/simple"]
                run = RunState(
                    "run-a", "task", session_id="root-a", provider=provider, lead_identity="lead-a",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(
                        Delegation("lead-a", "lead", "task", "working", route["model"], route["effort"]),
                        Delegation("worker-a", "worker", "task", "working", route["model"], route["effort"]),
                    ),
                )
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                store.save(self.project, ProjectState(active_run=run, active_runs={f"{provider}:root-a": run}))
                failed = self.payload("root-a", "SubagentStop", provider=provider,
                                      agent_id="lead-a", status="completed",
                                      last_assistant_message='SYMPHONY_OUTCOME: {"status":"blocked"}')
                handle(failed, env)
                handle({**failed, "status": "completed", "last_assistant_message": "recovered"}, env)
                self.assertEqual({"status": "completed"}, store.load(self.project).active_runs[f"{provider}:root-a"].outcome)

                context = multiprocessing.get_context("spawn")
                progresses = [context.Value("i", 0, lock=False) for _ in range(2)]
                processes = [context.Process(target=_foreign_heartbeats, args=(
                    str(self.project), env["SYMPHONY_STATE_DIR"], provider, profile,
                    session, progress
                )) for session, progress in zip(("root-b", "root-c"), progresses)]
                started = time.monotonic()
                try:
                    for process in processes:
                        process.start()
                    for session, process, progress in zip(("root-b", "root-c"), processes, progresses):
                        process.join(timeout=20)
                        if process.is_alive():
                            process.terminate()
                            process.join()
                        self.assertEqual(0, process.exitcode,
                                         f"{provider}/{session}: {progress.value}/105 heartbeats completed "
                                         f"in {time.monotonic() - started:.1f}s")
                finally:
                    # A failure for one process must not leave its sibling
                    # writing into a temporary directory that tearDown removes.
                    for process in processes:
                        if process.pid is not None:
                            if process.is_alive():
                                process.terminate()
                            process.join()
                handle({**failed, "stop_hook_active": True}, env)
                handle(self.payload("root-a", "SubagentStop", provider=provider,
                                    agent_id="worker-a", status="completed"), env)
                stop = self.out(handle(self.payload("root-a", "Stop", provider=provider), env))

                self.assertNotEqual("block", stop.get("decision"), stop.get("reason"))
                self.assertNotIn(f"{provider}:root-a", store.load(self.project).active_runs)

    def test_shared_agent_id_uses_root_session_and_rejects_foreign_parent(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                runs = {}
                for session, lead in (("root-a", "lead-a"), ("root-b", "lead-b")):
                    runs[f"{provider}:{session}"] = RunState(
                        session, "task", session_id=session, provider=provider, lead_identity=lead,
                        assessment={"size": "small", "complexity": "simple"},
                        delegations=(Delegation(lead, "lead", "task", "working", "", ""),
                                     Delegation("shared-worker", "worker", "task", "working", "", "")),
                    )
                store.save(self.project, ProjectState(active_run=runs[f"{provider}:root-a"], active_runs=runs))
                base = {"provider": provider, "cwd": str(self.project), "agent_id": "shared-worker"}
                handle({**base, "session_id": "root-b", "hook_event_name": "SubagentStop",
                        "status": "failed", "last_assistant_message": "failed"}, env)
                state = store.load(self.project)
                worker = lambda session: next(item for item in state.active_runs[f"{provider}:{session}"].delegations
                                              if item.identity == "shared-worker")
                self.assertEqual("working", worker("root-a").state)
                self.assertEqual("failed", worker("root-b").state)

                handle({**base, "session_id": "root-b", "hook_event_name": "SubagentStart",
                        "agent_id": "nested-b", "parent_thread_id": "shared-worker",
                        "agent_type": "symphony_worker"}, env)
                state = store.load(self.project)
                self.assertIn("nested-b", {item.identity for item in state.active_runs[f"{provider}:root-b"].delegations})
                self.assertNotIn("nested-b", {item.identity for item in state.active_runs[f"{provider}:root-a"].delegations})

                handle({**base, "session_id": "root-b", "hook_event_name": "SubagentStart",
                        "agent_id": "foreign-child", "parent_thread_id": "lead-a",
                        "agent_type": "symphony_worker"}, env)
                handle({**base, "session_id": "lead-b", "hook_event_name": "SubagentStart",
                        "agent_id": "foreign-child", "parent_thread_id": "lead-a",
                        "agent_type": "symphony_worker"}, env)
                state = store.load(self.project)
                self.assertNotIn("foreign-child", {item.identity for run in state.active_runs.values()
                                                   for item in run.delegations})

    def test_child_worktree_hooks_update_the_session_owner_project(self):
        child = self.project / ".claude" / "worktrees" / "agent-lead-a"
        child.mkdir(parents=True)
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                route = snapshot_for(provider, profile).matrix["small/simple"]
                run = RunState(
                    "run-a", "task", session_id="root-a", provider=provider, lead_identity="lead-a",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead-a", "lead", "task", "working",
                                            route["model"], route["effort"]),),
                )
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                store.save(self.project, ProjectState(active_run=run,
                                                      active_runs={f"{provider}:root-a": run}))
                handle({"provider": provider, "session_id": "root-a", "cwd": str(child),
                        "hook_event_name": "SubagentStop", "agent_id": "lead-a",
                        "status": "completed", "last_assistant_message":
                        'SYMPHONY_OUTCOME: {"status":"completed"}'}, env)
                state = store.load(self.project)
                self.assertEqual("completing", state.active_runs[f"{provider}:root-a"].status)
                handle({"provider": provider, "session_id": "root-a", "cwd": str(self.project),
                        "hook_event_name": "Stop"}, env)
                state = store.load(self.project)
                self.assertNotIn(f"{provider}:root-a", state.active_runs)
                self.assertEqual("completed", state.recent_runs[-1].status)
                self.assertEqual("root-a", state.recent_runs[-1].session_id)
                self.assertFalse(store._path(child).exists(), "child cwd must not create a second project state")
                handle({"provider": provider, "session_id": "root-a", "cwd": str(child),
                        "hook_event_name": "SubagentStop", "agent_id": "lead-a",
                        "status": "completed", "last_assistant_message":
                        'SYMPHONY_OUTCOME: {"status":"completed"}'}, env)
                self.assertFalse(store._path(child).exists(), "late child replay must not create a project state")

    def test_duplicate_root_session_owners_block_stop_and_explain_controls(self):
        other = self.project.parent / "other-project"
        other.mkdir()
        unknown_cwd = self.project.parent / "child-worktree"
        unknown_cwd.mkdir()
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                run = RunState("run", "task", session_id="root-a", provider=provider,
                               lead_identity="lead-a")
                state = ProjectState(active_run=run, active_runs={f"{provider}:root-a": run})
                store.save(self.project, state)
                store.save(other, state)
                stop = self.out(handle(self.payload("root-a", "Stop", provider=provider,
                                                    cwd=str(unknown_cwd)), env))
                self.assertEqual("block", stop.get("decision"))
                self.assertIn("multiple project states", stop.get("reason", ""))
                status = self.text(handle(self.payload("root-a", provider=provider,
                                                       cwd=str(unknown_cwd),
                                                       prompt="$symphony:symphony status"), env))
                self.assertIn("multiple project states", status)
                self.assertEqual("active", store.load(self.project).active_runs[f"{provider}:root-a"].status)
                self.assertEqual("active", store.load(other).active_runs[f"{provider}:root-a"].status)

    def test_child_worktree_finds_older_top_level_active_run(self):
        run = RunState("legacy", "task", session_id="root-a", provider="codex",
                       lead_identity="lead-a",
                       delegations=(Delegation("lead-a", "lead", "task", "working", "", ""),))
        store = StateStore(self.state_root)
        store.save(self.project, ProjectState(active_run=run,
                                              active_runs={"codex:root-a": run}))
        path = store._path(self.project)
        document = json.loads(path.read_text())
        document["active_runs"] = {}
        path.write_text(json.dumps(document))
        child = self.project / ".claude" / "worktrees" / "agent-lead-a"
        child.mkdir(parents=True)
        handle({"provider": "codex", "session_id": "root-a", "cwd": str(child),
                "hook_event_name": "SubagentStop", "agent_id": "lead-a", "status": "failed"},
               self.environ)
        state = store.load(self.project)
        self.assertEqual("recovering", state.active_runs["codex:root-a"].status)
        self.assertFalse(store._path(child).exists())

    def test_known_child_session_assessor_does_not_open_phantom_root(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                run = RunState(
                    "run", "task", session_id="root", provider=provider, lead_identity="lead-child",
                    assessment={"size": "small", "complexity": "simple"},
                    delegations=(Delegation("lead-child", "lead", "task", "working", "", ""),
                                 Delegation("assessor-same", "assessor", "task", "working", "", "")),
                )
                store.save(self.project, ProjectState(active_run=run, active_runs={f"{provider}:root": run}))
                handle({"provider": provider, "session_id": "lead-child", "cwd": str(self.project),
                        "hook_event_name": "SubagentStart", "agent_id": "assessor-same",
                        "agent_type": "symphony_assessor"}, env)
                state = store.load(self.project)
                self.assertEqual({f"{provider}:root"}, set(state.active_runs))
                self.assertEqual("root", state.active_runs[f"{provider}:root"].session_id)

    def test_worktree_and_branch_changes_do_not_mix_session_runs(self):
        def git(*args):
            subprocess.run(["git", *args], cwd=self.project, check=True,
                           capture_output=True, text=True)

        git("init")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (self.project / "README").write_text("test\n")
        git("add", "README")
        git("commit", "-m", "initial")
        other = self.project.parent / "other worktree"
        git("worktree", "add", "--detach", str(other))
        git("switch", "-c", "changed-after-run-started")
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            with self.subTest(provider=provider):
                env = {**self.environ, "SYMPHONY_STATE_DIR": str(self.state_root / provider),
                       "SYMPHONY_PROFILE": profile}
                store = StateStore(Path(env["SYMPHONY_STATE_DIR"]))
                route = snapshot_for(provider, profile).matrix["small/simple"]
                for project, session, agent in ((self.project, "root-a", "lead-a"),
                                                (other, "root-b", "lead-b")):
                    run = RunState(
                        session, "task", session_id=session, provider=provider, lead_identity=agent,
                        assessment={"size": "small", "complexity": "simple"},
                        delegations=(Delegation(agent, "lead", "task", "working",
                                                route["model"], route["effort"]),),
                    )
                    store.save(project, ProjectState(active_run=run, active_runs={f"{provider}:{session}": run}))

                handle(self.payload("root-a", "SubagentStop", provider=provider,
                                    agent_id="lead-a", status="completed",
                                    last_assistant_message='SYMPHONY_OUTCOME: {"status":"completed"}'), env)
                self.assertNotEqual("block", self.out(handle(
                    self.payload("root-a", "Stop", provider=provider), env)).get("decision"))
                blocked = self.out(handle(self.payload("root-b", "Stop", provider=provider,
                                                       cwd=str(other)), env))
                self.assertEqual("block", blocked.get("decision"))
                self.assertEqual("root-b", store.load(other).active_run.session_id)
                self.assertIsNone(store.load(self.project).active_run)


class ConcurrentFixtureCleanupTests(unittest.TestCase):
    def test_failed_first_churn_process_reaps_its_sibling_before_temp_cleanup(self):
        processes = []

        class StalledProcess:
            def __init__(self, *, target, args):
                self.state_dir = Path(args[1])
                self.pid = None
                self.exitcode = None
                self.reaped_with_state_present = False
                processes.append(self)

            def start(self):
                self.pid = len(processes)

            def is_alive(self):
                return self.pid is not None and self.exitcode is None

            def terminate(self):
                self.exitcode = -15

            def join(self, timeout=None):
                if not self.is_alive():
                    self.reaped_with_state_present = self.state_dir.is_dir()

        context = SimpleNamespace(Value=lambda *args, **kwargs: SimpleNamespace(value=0),
                                  Process=StalledProcess)
        case = ConcurrentSessionTests("test_foreign_churn_cannot_replay_a_recovered_lead_failure")
        result = unittest.TestResult()
        with patch.object(multiprocessing, "get_context", return_value=context):
            case.run(result)

        self.assertEqual([], result.errors)
        self.assertEqual(2, len(result.failures))
        self.assertEqual(4, len(processes))
        self.assertTrue(all(process.reaped_with_state_present for process in processes))
        self.assertFalse(any(process.is_alive() for process in processes))
        self.assertFalse(case.project.exists())


if __name__ == "__main__":
    unittest.main()
