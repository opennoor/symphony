import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from plugins.symphony.symphony.routing import assessor_selection, snapshot_for
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony.store import StateStore


class AssessorBoostTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.state_root = Path(self.temp.name) / "state"

    def send(self, provider="codex", session="a", profile="full", **payload):
        result = handle({"cwd": str(self.project), "session_id": session,
                         "hook_event_name": "UserPromptSubmit", **payload},
                        {"SYMPHONY_STATE_DIR": str(self.state_root),
                         "SYMPHONY_PROFILE": profile, "SYMPHONY_PROVIDER": provider})
        return json.loads(result.stdout) if result.stdout else {}

    def context(self, result):
        return result.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_unsupported_native_levels_are_not_clamped(self):
        self.assertEqual(assessor_selection(snapshot_for("codex", "full"), "ultra")["effort"], "ultra")
        base = assessor_selection(snapshot_for("codex", "base"), "ultra")
        self.assertEqual((base["model"], base["effort"]), ("gpt-6-luna", ""))
        claude = assessor_selection(snapshot_for("claude", "opus"), "ultra")
        self.assertEqual((claude["model"], claude["effort"]), ("claude-opus-5-5", ""))

    def test_provider_native_levels_on_both_providers(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            for public in (("xhigh", "max", "ultra") if provider == "codex" else ("xhigh", "max")):
                with self.subTest(provider=provider, public=public):
                    session = provider + public
                    identity = session + "-assessor"
                    canonical = public
                    prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                    text = self.context(self.send(provider=provider, profile=profile, session=session,
                                                  prompt=prefix + "boost " + public))
                    selected = assessor_selection(snapshot_for(provider, profile), canonical)
                    self.assertIn("Assessor boost: " + canonical, text)
                    self.assertIn(f"effective {selected['model']}/{selected['effort']}", text)
                    values = {"message": "SYMPHONY_ROLE: assessor\nDo it",
                              "model": selected["model"], "reasoning_effort": selected["effort"]}
                    if provider == "claude":
                        values = {"prompt": values["message"], "subagent_type":
                                  f"symphony:symphony-assessor-{selected['model']}-{selected['effort']}"}
                    result = self.send(provider=provider, profile=profile, session=session,
                                       hook_event_name="PreToolUse", tool_name="spawn_agent", tool_input=values)
                    self.assertNotEqual(result.get("decision"), "block")
                    self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
                    self.send(provider=provider, profile=profile, session=session,
                              hook_event_name="SubagentStart", agent_id=identity, role="assessor",
                              agent_type=f"symphony-assessor-{selected['model']}-{selected['effort']}",
                              model=selected["model"], model_reasoning_effort=selected["effort"])
                    self.send(provider=provider, profile=profile, session=session,
                              hook_event_name="SubagentStop", agent_id=identity, role="assessor",
                              last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
                    state = StateStore(self.state_root).load(self.project)
                    self.assertEqual(state.active_runs[f"{provider}:{session}"].assessment.get("size"), "small")

    def test_default_boost_persists_and_isolated_by_session_provider_project(self):
        self.assertIn("effective gpt-6-astra/ultra", self.context(self.send(prompt="$symphony:symphony boost")))
        self.assertIn("Assessor boost: ultra", self.context(self.send(prompt="$symphony:symphony status")))
        self.assertIn("Assessor boost: off", self.context(self.send(session="b", prompt="$symphony:symphony status")))
        self.assertIn("Assessor boost: ultra", self.context(self.send(prompt="$symphony:symphony status")))
        self.assertIn("Assessor boost: off", self.context(self.send(provider="claude", profile="opus", prompt="/symphony:status")))
        other = Path(self.temp.name) / "other"
        other.mkdir()
        self.project = other
        self.assertIn("Assessor boost: off", self.context(self.send(prompt="$symphony:symphony status")))

    def test_off_reset_and_invalid_controls_do_not_open_run(self):
        self.send(prompt="$symphony:symphony boost max")
        invalid = self.context(self.send(prompt="$symphony:symphony boost banana"))
        self.assertIn("boost [xhigh|max|ultra|off]", invalid)
        self.assertIn("Assessor boost: max", self.context(self.send(prompt="$symphony:symphony status")))
        self.assertIn("Assessor boost: off", self.context(self.send(prompt="$symphony:symphony boost reset")))
        self.send(prompt="$symphony:symphony boost ultra")
        self.assertIn("Assessor boost: off", self.context(self.send(prompt="$symphony:symphony boost off")))
        self.assertFalse(StateStore(self.state_root).load(self.project).active_runs)

    def test_guidance_and_spawn_enforce_effective_assessor_route(self):
        self.send(prompt="$symphony:symphony boost max")
        guidance = self.context(self.send(prompt="$symphony:symphony start implement it"))
        self.assertIn('reasoning_effort="max"', guidance)
        self.assertIn('model="gpt-6-astra"', guidance)
        spawn = {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                 "tool_input": {"message": "SYMPHONY_ROLE: assessor\nImplement it",
                                "model": "gpt-6-astra", "reasoning_effort": "high"}}
        self.assertEqual(self.send(**spawn).get("decision"), "block")
        spawn["tool_input"]["reasoning_effort"] = "max"
        self.assertNotEqual(self.send(**spawn).get("decision"), "block")

    def test_claude_native_choices_and_invalid_levels_preserve_preference(self):
        text = self.context(self.send(provider="claude", profile="opus", prompt="/symphony:boost max"))
        self.assertIn("requested claude-opus-5-5/max; effective claude-opus-5-5/max", text)
        for invalid in ("ultra", "extra", "ultracode", "low", "medium", "high"):
            text = self.context(self.send(provider="claude", profile="opus", prompt="/symphony:boost " + invalid))
            self.assertIn("boost [xhigh|max|off]", text)
            self.assertIn("Assessor boost: max", self.context(self.send(provider="claude", profile="opus", prompt="/symphony:status")))
        guidance = self.context(self.send(provider="claude", profile="opus", prompt="/symphony:start implement it"))
        self.assertIn("symphony-assessor-claude-opus-5-5-max", guidance)
        self.assertNotIn("symphony-assessor-claude-fable", guidance)
        self.assertNotIn("symphony-assessor-claude-opus-5-5-high", guidance)

    def test_account_unsupported_effort_does_not_change_preference(self):
        self.send(profile="base", prompt="$symphony:symphony boost max")
        text = self.context(self.send(profile="base", prompt="$symphony:symphony boost ultra"))
        self.assertIn("unsupported for gpt-6-luna", text)
        self.assertIn("Preference unchanged", text)
        self.assertIn("Assessor boost: max", self.context(self.send(profile="base", prompt="$symphony:symphony status")))
        for invalid in ("low", "medium", "high", "extra", "ultracode"):
            self.assertIn("boost [xhigh|max|ultra|off]", self.context(self.send(prompt="$symphony:symphony boost " + invalid)))

    def test_observed_route_mismatch_cannot_supply_boosted_assessment(self):
        self.send(prompt="$symphony:symphony boost ultra")
        self.send(hook_event_name="SubagentStart", agent_id="assessor", role="assessor",
                  model="gpt-6-astra", model_reasoning_effort="high")
        self.send(prompt="$symphony:symphony boost off")
        self.send(hook_event_name="SubagentStop", agent_id="assessor", role="assessor",
                  last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
        state = StateStore(self.state_root).load(self.project)
        self.assertTrue(state.active_run.assessment.get("_boost_assessment_pending"))
        self.assertNotIn("size", state.active_run.assessment)
        lead = self.send(hook_event_name="PreToolUse", tool_name="spawn_agent", tool_input={
            "message": 'SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {"size":"small","complexity":"simple"}',
            "model": "gpt-6-luna", "reasoning_effort": "low"})
        self.assertEqual(lead.get("decision"), "block")

    def test_observed_native_ultra_result_is_accepted(self):
        self.send(prompt="$symphony:symphony boost ultra")
        self.send(hook_event_name="SubagentStart", agent_id="assessor", role="assessor",
                  model="gpt-6-astra", model_reasoning_effort="ultra")
        self.send(hook_event_name="SubagentStop", agent_id="assessor", role="assessor",
                  last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
        state = StateStore(self.state_root).load(self.project)
        self.assertEqual(state.active_run.assessment.get("size"), "small")
        self.assertFalse(state.active_run.assessment.get("_boost_assessment_pending"))
        self.assertEqual((state.active_run.assessment["route"]["lead_model"],
                          state.active_run.assessment["route"]["lead_effort"]),
                         ("gpt-6-luna", "low"))

    def test_native_configuration_overrides_queued_boost_label(self):
        self.send(prompt="$symphony:symphony boost ultra")
        self.send(hook_event_name="PreToolUse", tool_name="spawn_agent", tool_input={
            "message": "SYMPHONY_ROLE: assessor\nDo it", "model": "gpt-6-astra",
            "reasoning_effort": "ultra"})
        transcript = Path(self.temp.name) / "child.jsonl"
        transcript.write_text(json.dumps({"type": "turn_context", "payload": {
            "model": "gpt-6-astra", "effort": "high"}}) + "\n")
        self.send(hook_event_name="SubagentStart", agent_id="assessor", role="assessor",
                  model="gpt-6-astra", model_reasoning_effort="high",
                  agent_transcript_path=str(transcript))
        self.send(hook_event_name="SubagentStop", agent_id="assessor", role="assessor",
                  last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
        state = StateStore(self.state_root).load(self.project)
        self.assertNotIn("size", state.active_run.assessment)
        self.assertEqual(state.active_run.delegations[0].requested_effort, "high")

    def test_missing_start_uses_the_boost_pinned_at_launch(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            for requested in (("xhigh", "max", "ultra") if provider == "codex" else ("xhigh", "max")):
                for matching in (True, False):
                    with self.subTest(provider=provider, requested=requested, matching=matching):
                        session = f"{provider}-{requested}-{matching}"
                        prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                        self.send(provider=provider, profile=profile, session=session, prompt=prefix + "boost " + requested)
                        selected = assessor_selection(snapshot_for(provider, profile), requested)
                        values = {"message": "SYMPHONY_ROLE: assessor\nDo it", "model": selected["model"], "reasoning_effort": selected["effort"]}
                        if provider == "claude":
                            values = {"prompt": values["message"], "subagent_type": f"symphony-assessor-{selected['model']}-{selected['effort']}"}
                        self.send(provider=provider, profile=profile, session=session,
                                  hook_event_name="PreToolUse", tool_name="Agent" if provider == "claude" else "spawn_agent", tool_input=values)
                        self.send(provider=provider, profile=profile, session=session, prompt=prefix + "boost off")
                        observed = selected["effort"] if matching else "high"
                        transcript = Path(self.temp.name) / (session + ".jsonl")
                        transcript.write_text(json.dumps({"type": "turn_context", "payload": {"model": selected["model"], "effort": observed}}) + "\n")
                        self.send(provider=provider, profile=profile, session=session, hook_event_name="SubagentStop",
                                  agent_id=session + "-assessor", role="assessor", model=selected["model"], model_reasoning_effort=observed,
                                  agent_type=f"symphony-assessor-{selected['model']}-{observed}", agent_transcript_path=str(transcript),
                                  last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
                        run = StateStore(self.state_root).load(self.project).active_runs[f"{provider}:{session}"]
                        self.assertEqual(run.assessment.get("size") == "small", matching)
                        self.assertEqual(bool(run.assessment.get("_boost_assessment_pending")), not matching)

    def test_default_launch_survives_boost_before_delayed_or_missing_start(self):
        for provider, profile in (("codex", "full"), ("claude", "opus")):
            for missing_start in (False, True):
                with self.subTest(provider=provider, missing_start=missing_start):
                    session = f"{provider}-default-{missing_start}"
                    prefix = "$symphony:symphony " if provider == "codex" else "/symphony:"
                    selected = assessor_selection(snapshot_for(provider, profile), "off")
                    values = {"message": "SYMPHONY_ROLE: assessor\nDo it", "model": selected["model"], "reasoning_effort": "high"}
                    if provider == "claude":
                        values = {"prompt": values["message"], "subagent_type": f"symphony-assessor-{selected['model']}-high"}
                    self.send(provider=provider, profile=profile, session=session,
                              hook_event_name="PreToolUse", tool_name="Agent" if provider == "claude" else "spawn_agent", tool_input=values)
                    self.send(provider=provider, profile=profile, session=session, prompt=prefix + "boost max")
                    payload = {"agent_id": session + "-assessor", "role": "assessor", "model": selected["model"],
                               "model_reasoning_effort": "high", "agent_type": f"symphony-assessor-{selected['model']}-high"}
                    if not missing_start:
                        self.send(provider=provider, profile=profile, session=session, hook_event_name="SubagentStart", **payload)
                    self.send(provider=provider, profile=profile, session=session, hook_event_name="SubagentStop", **payload,
                              last_assistant_message='SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}')
                    run = StateStore(self.state_root).load(self.project).active_runs[f"{provider}:{session}"]
                    self.assertEqual(run.assessment.get("size"), "small")
                    self.assertFalse(run.assessment.get("_boost_assessment_pending"))
