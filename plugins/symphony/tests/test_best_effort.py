"""Symphony is a best-effort helper: its own problems never stop the user.

Rule 2: a Stop is held only while the user's delegated work is really
running, or an assessed task's lead was never launched. Rule 6: everything
else (bookkeeping, unverifiable evidence, ownership) is counted privately.
A settled run never captures the next prompt.
"""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.model import Delegation, ProjectState, RunState
from plugins.symphony.symphony.routing import profiles_for
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


class BestEffortStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.store = StateStore(root / "state")
        self.env = {"SYMPHONY_STATE_DIR": str(self.store.root), "SYMPHONY_PROVIDER": "codex",
                    "SYMPHONY_PROFILE": profiles_for("codex")[0]["id"], "SYMPHONY_REPORT_WORKER": "0"}

    def seed(self, *delegations, status="active", lead="lead", **assessment):
        run = RunState("run", "task", session_id="root", provider="codex", status=status,
                       lead_identity=lead, delegations=delegations, assessment=assessment)
        self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                   active_runs={"codex:root": run}))

    def send(self, event, **fields):
        return handle({"session_id": "root", "cwd": str(self.project), "hook_event_name": event,
                       "turn_id": "turn", "model": "m", **fields}, self.env)

    def diagnostics(self):
        return [json.loads(path.read_text()) for path in (self.store.root / "diagnostics").glob("*.json")]

    def test_symphony_bookkeeping_never_holds_the_turn(self):
        # The lead ended without a reconciled outcome: a Symphony evidence
        # problem, not unfinished user work.
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering")
        result = self.send("Stop")
        self.assertEqual("", result.stdout)
        self.assertIn("lead_outcome_missing", {item["signal"] for item in self.diagnostics()})

    def test_running_delegated_work_is_held_once_per_turn(self):
        lead = Delegation("lead", "lead", "task", "working", "m", "medium")
        self.seed(lead)
        first = self.send("Stop")
        self.assertEqual("block", json.loads(first.stdout)["decision"])
        self.assertEqual("", self.send("Stop", stop_hook_active=True).stdout)

    def test_a_new_prompt_closes_a_settled_run_instead_of_replaying_it(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering", _reported_nonsuccess="blocked")
        result = self.send("UserPromptSubmit", prompt="Now rename the config file")
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        # Recorded honestly: the lead's own blocker, never a completion.
        self.assertEqual("blocked", state.recent_runs[-1].status)
        text = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("remains", text)
        self.assertIn("Now rename the config file", text)

    def test_a_closed_runs_queued_notes_never_reach_the_next_task(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        stale = {"kind": "inject_context", "payload": {"text": "STALE blocked-run note: send stop first"}}
        self.seed(lead, status="recovering", _reported_nonsuccess="blocked",
                  _pending_parent_actions=[stale])
        result = self.send("UserPromptSubmit", prompt="Add a .gitignore and commit it")
        self.assertNotIn("STALE", result.stdout)
        self.assertNotIn("$symphony:symphony stop", result.stdout)
        self.assertIn("Add a .gitignore", result.stdout)  # routed as the new task
        self.assertEqual("blocked", self.store.load(self.project).recent_runs[-1].status)

    def test_running_work_is_not_superseded_by_a_prompt(self):
        lead = Delegation("lead", "lead", "task", "working", "m", "medium")
        self.seed(lead)
        self.send("UserPromptSubmit", prompt="How is it going?")
        self.assertIsNotNone(self.store.load(self.project).active_run)

    def test_a_completing_run_is_left_for_stop_to_verify(self):
        # A completed lead may still have a newer native turn running whose
        # callback is late; only the Stop path checks that.
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="completing")
        self.send("UserPromptSubmit", prompt="How is it going?")
        self.assertIsNotNone(self.store.load(self.project).active_run)

    def test_a_finished_run_symphony_cannot_archive_yields_to_the_next_task(self):
        # lexi, 2026-10-10: the lead reported, but Symphony could not verify
        # its native turn (and two launches never confirmed), so the run stayed
        # completing and refused every later lead until the user typed stop.
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        stale = [{"role": "worker", "model": "m", "effort": "medium", "objective": "never started"}]
        for provider, pending, closed in (("codex", (), True), ("claude", stale, True),
                                          ("codex", stale, True), ("claude", (), True)):
            with self.subTest(provider=provider, pending=bool(pending)):
                run = RunState("run", "task", session_id="root", provider=provider, status="completing",
                               lead_identity="lead", delegations=(lead,), outcome={"status": "completed"},
                               assessment={"_pending_delegations": pending} if pending else {})
                self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                           active_runs={f"{provider}:root": run}))
                env = {**self.env, "SYMPHONY_PROVIDER": provider,
                       "SYMPHONY_PROFILE": profiles_for(provider)[0]["id"]}
                result = handle({"session_id": "root", "cwd": str(self.project), "turn_id": "t", "model": "m",
                                 "hook_event_name": "UserPromptSubmit", "prompt": "Port the Babylon fix"}, env)
                state = self.store.load(self.project)
                self.assertEqual(closed, state.active_run is None)
                if closed:
                    # Closed honestly: Symphony never verified this completion.
                    self.assertEqual("superseded", state.recent_runs[-1].status)
                    self.assertIn("Port the Babylon fix", result.stdout)  # routed as the new task
                    self.assertNotIn("accepted the assessed route", result.stdout)

    def test_a_lead_visibly_in_a_newer_turn_keeps_its_run(self):
        from unittest.mock import patch
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        for provider, target, value in (
                ("codex", "codex_lead_turn_running", True),
                ("claude", "claude_lead_turn_running", True)):
            with self.subTest(provider=provider):
                run = RunState("run", "task", session_id="root", provider=provider, status="completing",
                               lead_identity="lead", delegations=(lead,), outcome={"status": "completed"})
                self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                           active_runs={f"{provider}:root": run}))
                env = {**self.env, "SYMPHONY_PROVIDER": provider,
                       "SYMPHONY_PROFILE": profiles_for(provider)[0]["id"]}
                with patch(f"plugins.symphony.symphony.runtime.{target}", return_value=value):
                    handle({"session_id": "root", "cwd": str(self.project), "turn_id": "t", "model": "m",
                            "hook_event_name": "UserPromptSubmit", "prompt": "How is it going?"}, env)
                self.assertIsNotNone(self.store.load(self.project).active_run)

    def test_start_control_also_yields_a_finished_run(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        stale = {"kind": "inject_context", "payload": {"text": "STALE: reconcile this work first"}}
        self.seed(lead, status="completing", _pending_parent_actions=[stale])
        run = self.store.load(self.project).active_run
        self.store.save(self.project, ProjectState(enabled=True, active_run=replace(
            run, outcome={"status": "completed"}), active_runs={"codex:root": replace(
            run, outcome={"status": "completed"})}))
        result = self.send("UserPromptSubmit", prompt="$symphony:symphony start Port the Babylon fix")
        state = self.store.load(self.project)
        self.assertIsNone(state.active_run)
        self.assertEqual("superseded", state.recent_runs[-1].status)
        self.assertNotIn("remains completing", result.stdout)
        self.assertNotIn("STALE", result.stdout)  # the closed run's notes stay with it

    def test_the_users_stop_waits_for_a_lead_visibly_mid_turn(self):
        from unittest.mock import patch
        lead = Delegation("a2ff9fa5055697d4f", "lead", "task", "completed", "m", "medium")
        for live in (True, False):
            with self.subTest(live=live):
                run = RunState("run", "task", session_id="root", provider="claude", status="completing",
                               lead_identity=lead.identity, delegations=(lead,), outcome={"status": "completed"})
                self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                           active_runs={"claude:root": run}))
                env = {**self.env, "SYMPHONY_PROVIDER": "claude",
                       "SYMPHONY_PROFILE": profiles_for("claude")[0]["id"]}
                with patch("plugins.symphony.symphony.runtime.claude_completing_lead_turn",
                           return_value=("unknown", None)), \
                        patch("plugins.symphony.symphony.runtime.claude_lead_turn_running", return_value=live):
                    handle({"session_id": "root", "cwd": str(self.project), "hook_event_name": "UserPromptSubmit",
                            "prompt": "/symphony:stop"}, env)
                state = self.store.load(self.project)
                self.assertEqual(live, state.active_run is not None)
                if not live:
                    self.assertEqual("stopped", state.recent_runs[-1].status)

    def test_lead_liveness_is_read_from_its_own_transcript(self):
        from plugins.symphony.symphony.host_evidence import claude_lead_turn_running
        home = Path(self.temp.name) / "claude-home"
        agents = home / "projects" / "-p" / "root" / "subagents"
        agents.mkdir(parents=True)
        lead = Delegation("a2ff9fa5055697d4f", "lead", "task", "completed", "m", "medium")
        run = RunState("run", "task", session_id="root", provider="claude", status="completing",
                       lead_identity=lead.identity, delegations=(lead,), outcome={"status": "completed"})
        state = ProjectState(active_run=run)
        env = {"CLAUDE_CONFIG_DIR": str(home)}
        path = agents / f"agent-{lead.identity}.jsonl"
        done = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}}
        prompt = {"type": "user", "message": {"content": "One more thing"}}
        reminder = {"type": "user", "isMeta": True, "message": {"content": "handback reminder"}}
        attachment = {"type": "attachment"}
        rows = lambda *items: "".join(json.dumps(item) + "\n" for item in items)
        self.assertFalse(claude_lead_turn_running(state, "root", self.project, env))  # no transcript
        streamed = {"type": "assistant", "message": {"stop_reason": None, "content": [{"type": "text", "text": "ok"}]}}
        calling = {"type": "assistant", "message": {"stop_reason": None,
                                                    "content": [{"type": "tool_use", "id": "t", "name": "Bash"}]}}
        for text, live in ((rows(prompt, done, attachment, reminder), False),
                           (rows(prompt, streamed), False),
                           (rows(prompt, {"type": "assistant", "message": {
                               "stop_reason": "error", "content": [{"type": "text", "text": "API error"}]}}), False),
                           (rows(prompt, calling), True),
                           (rows(done, {**reminder, "origin": {"kind": "coordinator"}}), True),
                           (rows(prompt, {"type": "user", "message": {"content": [
                               {"type": "tool_result", "tool_use_id": "t", "content": "x" * 300_000}]}}), True),
                           (rows(prompt, {"type": "user", "message": {"content": [
                               {"type": "tool_result", "tool_use_id": "t", "content": "x" * 300_000}]}},
                                 reminder, attachment), True),
                           (rows(prompt, {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
                               {"type": "text", "text": "y" * 300_000}]}}), False),
                           (rows(done, prompt), True),
                           (rows(done, prompt) + '{"type": "assist', True),
                           (rows(done) + "not json\n", False)):
            path.write_text(text)
            self.assertEqual(live, claude_lead_turn_running(state, "root", self.project, env), text)

    def test_codex_lead_liveness_ignores_the_route_of_its_newer_turn(self):
        from unittest.mock import patch
        from plugins.symphony.symphony import host_evidence
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        run = RunState("run", "task", session_id="root", provider="codex", status="completing",
                       lead_identity="lead", delegations=(lead,), outcome={"status": "completed"})
        state = ProjectState(active_run=run)
        for turn, live in (({"started": True, "model": "other", "effort": "high"}, True),
                           ({"started": True, "completed_at": 1}, False),
                           ({"started": True, "failed": True}, False), ({}, False)):
            with patch.object(host_evidence, "_native_lead_turns",
                              return_value=(run, {"new": turn}, ["old", "new"], "new", 0)):
                self.assertEqual(live, host_evidence.codex_lead_turn_running(state, "root", {}))
        with patch.object(host_evidence, "_native_lead_turns", return_value=None):
            self.assertFalse(host_evidence.codex_lead_turn_running(state, "root", {}))

    def test_the_users_codex_stop_waits_for_a_lead_visibly_mid_turn(self):
        from unittest.mock import patch
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        for live, freshness in ((True, "unknown"), (False, "unknown"), (True, "running"), (False, "running")):
            with self.subTest(live=live, freshness=freshness):
                run = RunState("run", "task", session_id="root", provider="codex", status="completing",
                               lead_identity="lead", delegations=(lead,), outcome={"status": "completed"})
                self.store.save(self.project, ProjectState(enabled=True, active_run=run,
                                                           active_runs={"codex:root": run}))
                # "running" without a live turn: the newer turn aborted.
                with patch("plugins.symphony.symphony.runtime.codex_completing_lead_turn",
                           return_value=(freshness, None)), \
                        patch("plugins.symphony.symphony.runtime.codex_lead_turn_running", return_value=live):
                    self.send("UserPromptSubmit", prompt="$symphony:symphony stop")
                state = self.store.load(self.project)
                self.assertEqual(live, state.active_run is not None)

    def test_automatic_host_prompts_never_supersede(self):
        lead = Delegation("lead", "lead", "task", "completed", "m", "medium")
        self.seed(lead, status="recovering")
        self.send("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>")
        self.assertIsNotNone(self.store.load(self.project).active_run)


class SizedTaskContinuationTests(unittest.TestCase):
    """Reported 2026-10-08: sizing finished, then every implementer launch was
    refused with "Spawn the Symphony assessor first", and re-sizing looped.

    In an interactive session the assessor's hand-back, or the user's "go
    ahead", arrives as a prompt between sizing and the lead launch.
    """

    MARKER = json.dumps({"size": "small", "complexity": "simple", "risk": "normal",
                         "rationale": "bounded", "topology": "delegated"})

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.store = StateStore(root / "state")

    def env(self, provider):
        return {"SYMPHONY_STATE_DIR": str(self.store.root), "SYMPHONY_PROVIDER": provider,
                "SYMPHONY_PROFILE": profiles_for(provider)[0]["id"], "SYMPHONY_REPORT_WORKER": "0"}

    def send(self, provider, event, **fields):
        extra = {"turn_id": "t", "model": "m"} if provider == "codex" else {}
        return handle({"session_id": "root", "cwd": str(self.project), "hook_event_name": event,
                       **extra, **fields}, self.env(provider))

    def spawn(self, provider, role, choice, marker=""):
        body = f"SYMPHONY_ROLE: {role}\n" + (f"SYMPHONY_ROUTE: {marker}\n" if marker else "") + "Ship it"
        if provider == "claude":
            kind = f"symphony:symphony-{role}-{choice['model']}-{choice['effort']}"
            self.launches = getattr(self, "launches", 0) + 1
            return self.send(provider, "PreToolUse", tool_name="Agent", tool_use_id=f"launch-{role}-{self.launches}",
                             tool_input={"subagent_type": kind, "prompt": body})
        return self.send(provider, "PreToolUse", tool_name="spawn_agent", tool_input={
            "message": body, "model": choice["model"], "reasoning_effort": choice["effort"]})

    def size(self, provider):
        from plugins.symphony.symphony.routing import assessor_selection, snapshot_for
        snapshot = snapshot_for(provider, profiles_for(provider)[0]["id"])
        assessor = assessor_selection(snapshot, "off")
        self.send(provider, "SessionStart")
        self.send(provider, "UserPromptSubmit", prompt="/symphony:enable" if provider == "claude"
                  else "$symphony:symphony enable")
        self.send(provider, "UserPromptSubmit", prompt="Add a slugify helper with tests")
        self.assertNotIn("block", self.spawn(provider, "assessor", assessor).stdout)
        kind = (f"symphony:symphony-assessor-{assessor['model']}-{assessor['effort']}" if provider == "claude"
                else f"symphony_assessor_{assessor['model'].replace('-', '_').replace('.', '_')}_{assessor['effort']}")
        child = {"agent_id": "assessor-1", "agent_type": kind, "parent_thread_id": "root"}
        if provider == "codex":
            child.update(model=assessor["model"], model_reasoning_effort=assessor["effort"])
        self.send(provider, "SubagentStart", **child)
        self.send(provider, "SubagentStop", status="completed",
                  last_assistant_message=f"SYMPHONY_ASSESSMENT: {self.MARKER}", **child)
        return snapshot.matrix["small/simple"]

    def test_go_ahead_after_sizing_keeps_the_run_and_launches_the_lead(self):
        for provider in ("codex", "claude"):
            for between in ("Go ahead and implement it.",
                            "Another Claude session sent a message: [Subagent hand-back] sizing done"):
                with self.subTest(provider=provider, between=between):
                    self.tearDown() if False else None
                    self.setUp()
                    lead = self.size(provider)
                    run = self.store.load(self.project).active_run
                    self.assertTrue(run.assessment.get("size"), run.assessment)
                    # Waiting for the user after sizing never holds the turn.
                    stop = self.send(provider, "Stop")
                    self.assertNotIn('"decision": "block"', stop.stdout)
                    prompt = self.send(provider, "UserPromptSubmit", prompt=between)
                    self.assertIsNotNone(self.store.load(self.project).active_run)
                    self.assertIn("launch its lead", prompt.stdout)
                    launched = self.spawn(provider, "lead", lead, self.MARKER)
                    self.assertNotIn("Spawn the Symphony assessor first", launched.stdout)
                    self.assertNotIn('"decision": "block"', launched.stdout)
                    self.assertNotIn('"deny"', launched.stdout)

    def test_a_lead_with_a_valid_route_is_never_refused_for_a_missing_run(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.setUp()
                from plugins.symphony.symphony.routing import snapshot_for
                lead = snapshot_for(provider, profiles_for(provider)[0]["id"]).matrix["small/simple"]
                self.send(provider, "SessionStart")
                launched = self.spawn(provider, "lead", lead, self.MARKER)
                self.assertNotIn('"deny"', launched.stdout)
                self.assertNotIn('"decision": "block"', launched.stdout)
                run = self.store.load(self.project).active_run
                self.assertEqual(("small", "simple"), (run.assessment.get("size"), run.assessment.get("complexity")))

    def test_a_new_sizing_replaces_a_sized_run_that_never_got_its_lead(self):
        from plugins.symphony.symphony.routing import assessor_selection, snapshot_for
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.setUp()
                self.size(provider)
                first = self.store.load(self.project).active_run.run_id
                assessor = assessor_selection(snapshot_for(provider, profiles_for(provider)[0]["id"]), "off")
                if provider == "claude":
                    self.spawn(provider, "assessor", assessor)
                else:
                    # Codex reports a spawn only when the child starts.
                    kind = f"symphony_assessor_{assessor['model'].replace('-', '_').replace('.', '_')}_{assessor['effort']}"
                    self.send(provider, "SubagentStart", agent_id="assessor-2", agent_type=kind,
                              parent_thread_id="root", model=assessor["model"],
                              model_reasoning_effort=assessor["effort"])
                state = self.store.load(self.project)
                self.assertNotEqual(first, state.active_run.run_id)
                self.assertEqual("superseded", state.recent_runs[-1].status)


class AgentLaunchedSessionTests(unittest.TestCase):
    """A `codex exec` or `claude -p` started by another agent is that agent's
    tool call: Symphony stays out and never touches the project's state."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.state = root / "state"
        self.transcript = root / "rollout.jsonl"

    def run_hook(self, provider, originator="codex_exec", **env):
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "thread-1", "session_id": "thread-1", "originator": originator}}) + "\n")
        payload = {"session_id": "thread-1" if provider == "codex" else "root", "cwd": str(self.project),
                   "hook_event_name": "UserPromptSubmit", "prompt": "Implement the importer",
                   "transcript_path": str(self.transcript)}
        if provider == "codex":
            payload.update(turn_id="t", model="m")
        handle(payload, {"SYMPHONY_STATE_DIR": str(self.state), "SYMPHONY_PROVIDER": provider,
                         "SYMPHONY_REPORT_WORKER": "0", **env})
        return self.state.exists() and any(self.state.iterdir())

    def test_agent_launched_headless_sessions_stand_down(self):
        cases = [("codex", "codex_exec", {"CLAUDECODE": "1"}),
                 ("codex", "codex_exec", {"CODEX_THREAD_ID": "outer-thread"}),
                 ("claude", "", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_CODE_EXECPATH": "/claude"}),
                 ("claude", "", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CODEX_THREAD_ID": "outer-thread"})]
        for provider, originator, env in cases:
            with self.subTest(provider=provider, env=env):
                self.setUp()
                self.assertFalse(self.run_hook(provider, originator, **env))

    def test_interactive_human_and_opted_in_sessions_are_still_routed(self):
        cases = [("codex", "codex_cli_rs", {"CLAUDECODE": "1"}),  # interactive Codex in an agent's tmux
                 ("codex", "codex_exec", {}),  # a person's own codex exec
                 ("codex", "codex_exec", {"CODEX_THREAD_ID": "thread-1"}),  # Codex's own thread
                 ("codex", "codex_exec", {"CLAUDECODE": "1", "SYMPHONY_AGENT_SESSIONS": "1"}),
                 ("claude", "", {"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_EXECPATH": "/claude"}),
                 ("claude", "", {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})]
        for provider, originator, env in cases:
            with self.subTest(provider=provider, originator=originator, env=env):
                self.setUp()
                self.assertTrue(self.run_hook(provider, originator, **env))


if __name__ == "__main__":
    unittest.main()
