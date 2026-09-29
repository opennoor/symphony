import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from plugins.symphony.symphony.routing import (
    Assessment,
    profiles_for,
    resolve_tier,
    route_for,
    snapshot_for,
)
from plugins.symphony.symphony.runtime import handle
from plugins.symphony.symphony import PLUGIN_VERSION
from plugins.symphony.symphony.model import RunState
from plugins.symphony.symphony.store import StateStore

CODEX_FULL_SNAPSHOT = snapshot_for("codex", "full")
CODEX_BASE_SNAPSHOT = snapshot_for("codex", "base")
CODEX_DRIFT_CELL = next(
    cell for cell in CODEX_FULL_SNAPSHOT.matrix
    if CODEX_FULL_SNAPSHOT.matrix[cell] != CODEX_BASE_SNAPSHOT.matrix[cell]
)
CODEX_DRIFT_SIZE, CODEX_DRIFT_COMPLEXITY = CODEX_DRIFT_CELL.split("/")
CODEX_FULL_ROUTE = CODEX_FULL_SNAPSHOT.matrix[CODEX_DRIFT_CELL]
CODEX_BASE_ROUTE = CODEX_BASE_SNAPSHOT.matrix[CODEX_DRIFT_CELL]


def roster(*slugs, hidden=()):
    models = [{"slug": slug, "visibility": "list"} for slug in slugs]
    models += [{"slug": slug, "visibility": "hide"} for slug in hidden]
    return {"models": models}


class ProfileDataTests(unittest.TestCase):
    OFFICIAL_EFFORTS = {
        "codex": {"none", "low", "medium", "high", "xhigh", "max", "ultra"},
        "claude": {"low", "medium", "high", "xhigh", "max"},
    }

    def test_every_profile_covers_all_four_tiers(self):
        for provider in ("codex", "claude"):
            for profile in profiles_for(provider):
                with self.subTest(provider=provider, profile=profile["id"]):
                    self.assertEqual(
                        set(profile["tiers"]),
                        {"economy", "balanced", "capable", "strongest"},
                    )

    def test_every_tier_model_declares_its_efforts(self):
        for provider in ("codex", "claude"):
            for profile in profiles_for(provider):
                for tier, model in profile["tiers"].items():
                    with self.subTest(provider=provider, profile=profile["id"], tier=tier):
                        self.assertIn(model, profile["efforts"])
                        self.assertTrue(profile["efforts"][model])
                self.assertEqual(len(profile.get("matrix", {})), 9)
                for cell, choice in profile.get("matrix", {}).items():
                    with self.subTest(provider=provider, profile=profile["id"], cell=cell):
                        self.assertIn(choice["model"], profile["efforts"])
                        self.assertIn(choice["effort"], profile["efforts"][choice["model"]])

    def test_shipped_efforts_are_provider_supported(self):
        for provider in ("codex", "claude"):
            for profile in profiles_for(provider):
                for model, efforts in profile["efforts"].items():
                    with self.subTest(provider=provider, profile=profile["id"], model=model):
                        self.assertTrue(set(efforts) <= self.OFFICIAL_EFFORTS[provider])

    def test_the_last_profile_is_an_unconditional_floor(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                floor = profiles_for(provider)[-1]
                self.assertFalse(floor.get("requires_all"))
                self.assertFalse(floor.get("requires_any"))

    def test_an_unprobed_account_routes_through_the_floor(self):
        route = route_for(Assessment("small", "complex"))
        floor = profiles_for("codex")[-1]
        resolved = resolve_tier(route, snapshot_for("codex", None))
        self.assertEqual(resolved["lead_model"], floor["matrix"]["small/complex"]["model"])


class EntitlementProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state_root = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def heartbeat(self, environ, provider="codex", **extra):
        payload = {
            "session_id": f"{provider}-session",
            "cwd": str(self.project),
            "hook_event_name": "SessionStart",
            **extra,
        }
        if provider == "codex":
            payload.update({"turn_id": "turn-1", "model": "codex-model"})
        handle(payload, environ)
        return StateStore(self.state_root).load(self.project).activation.get(provider, {})

    def codex_home(self, payload):
        home = self.root / "codex-home"
        home.mkdir(exist_ok=True)
        (home / "models_cache.json").write_text(json.dumps(payload))
        return {"SYMPHONY_STATE_DIR": str(self.state_root), "CODEX_HOME": str(home)}

    def seed_claude_owner(self, assessment=None):
        run = RunState(
            run_id="run-1", task="fix it", session_id="owner", provider="claude",
            owner_seen_at=datetime.now(timezone.utc).isoformat(),
            assessment=assessment or {},
        )
        StateStore(self.state_root).update(self.project, lambda state: (
            replace(state, active_run=run, active_runs={**state.active_runs, "claude:owner": run}), ()
        ))

    def test_a_complete_roster_selects_the_full_profile(self):
        required = profiles_for("codex")[0].get("requires_all", [])
        environ = self.codex_home(roster(*required))
        self.assertEqual(self.heartbeat(environ).get("profile"), "full")

    def test_a_missing_model_falls_back_to_the_base_profile(self):
        required = profiles_for("codex")[0].get("requires_all", [])
        environ = self.codex_home(roster(*required[:-1]))
        self.assertEqual(self.heartbeat(environ).get("profile"), "base")

    def test_luna_and_sol_roster_keeps_cheaper_full_policy_cells(self):
        available = {"gpt-6-luna", "gpt-6-sol"}
        environ = self.codex_home(roster(*available))
        profile = self.heartbeat(environ).get("profile")
        self.assertEqual(profile, "luna-sol")
        snapshot = snapshot_for("codex", profile)
        full = snapshot_for("codex", "full")
        for cell, choice in full.matrix.items():
            size, complexity = cell.split("/")
            expected_model = "gpt-6-sol" if choice["model"] == "gpt-6-astra" else choice["model"]
            with self.subTest(cell=cell):
                resolved = resolve_tier(route_for(Assessment(size, complexity)), snapshot)
                self.assertEqual((resolved["lead_model"], resolved["lead_effort"]),
                                 (expected_model, choice["effort"]))
                self.assertIn(resolved["lead_model"], available)
                self.assertFalse(resolved["degraded"])
        self.assertEqual(snapshot.matrix["small/simple"]["model"], "gpt-6-luna")
        self.assertEqual(snapshot.matrix["small/complex"]["model"], "gpt-6-sol")

    def test_a_sol_only_roster_routes_every_cell_to_sol(self):
        environ = self.codex_home(roster("gpt-6-sol"))
        profile = self.heartbeat(environ).get("profile")
        self.assertEqual(profile, "sol")
        snapshot = snapshot_for("codex", profile)
        for size in ("small", "medium", "large"):
            for complexity in ("simple", "mixed", "complex"):
                with self.subTest(size=size, complexity=complexity):
                    resolved = resolve_tier(route_for(Assessment(size, complexity)), snapshot)
                    self.assertEqual(resolved["lead_model"], "gpt-6-sol")
                    self.assertFalse(resolved["degraded"])

        payload = {"session_id": "codex-session", "cwd": str(self.project),
                   "turn_id": "turn-2", "model": "codex-model", "hook_event_name": "PreToolUse",
                   "tool_name": "spawn_agent"}
        assessor = handle({**payload, "tool_input": {
            "message": "SYMPHONY_ROLE: assessor\nShip it", "model": "gpt-6-sol",
            "reasoning_effort": "high",
        }}, environ)
        self.assertNotEqual(json.loads(assessor.stdout or "{}").get("decision"), "block")
        route = json.dumps({"size": "small", "complexity": "simple", "risk": "normal"})
        lead = handle({**payload, "tool_input": {
            "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {route}\nShip it",
            "model": "gpt-6-sol", "reasoning_effort": "low",
        }}, environ)
        self.assertNotEqual(json.loads(lead.stdout or "{}").get("decision"), "block")

    def test_a_known_unsupported_roster_discloses_and_blocks_launch(self):
        environ = self.codex_home(roster("unrelated-model"))
        self.assertEqual(self.heartbeat(environ).get("profile"), "unavailable")
        payload = {
            "session_id": "codex-session", "cwd": str(self.project),
            "turn_id": "turn-2", "model": "codex-model",
        }
        prompt = handle({**payload, "hook_event_name": "UserPromptSubmit",
                         "prompt": "$symphony:symphony start ship it"}, environ)
        self.assertIn("no launchable route", prompt.stdout)
        spawn = handle({**payload, "hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                        "tool_input": {"message": "SYMPHONY_ROLE: assessor\nShip it",
                                       "model": "unrelated-model", "reasoning_effort": "high"}}, environ)
        self.assertEqual(json.loads(spawn.stdout)["decision"], "block")
        self.assertIn("no launchable route", spawn.stdout)

    def test_a_hidden_model_does_not_count_as_entitlement(self):
        required = profiles_for("codex")[0].get("requires_all", [])
        environ = self.codex_home(
            roster(*required[:-1], hidden=required[-1:])
        )
        self.assertEqual(self.heartbeat(environ).get("profile"), "base")

    def test_an_unreadable_roster_records_no_profile(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "CODEX_HOME": str(self.root / "absent")}
        profile = self.heartbeat(environ).get("profile")
        self.assertFalse(profile)
        self.assertEqual(snapshot_for("codex", profile).tiers["strongest"], "gpt-6-luna")

    def test_a_pinned_profile_skips_the_probe_entirely(self):
        environ = {
            "SYMPHONY_STATE_DIR": str(self.state_root),
            "CODEX_HOME": str(self.root / "absent"),
            "SYMPHONY_PROFILE": "full",
        }
        self.assertEqual(self.heartbeat(environ).get("profile"), "full")

    def test_the_probe_runs_once_and_is_reused_within_a_session(self):
        required = profiles_for("codex")[0].get("requires_all", [])
        environ = self.codex_home(roster(*required))
        self.heartbeat(environ)
        # Remove the roster: a second heartbeat in the same session must not
        # re-probe, so the recorded answer survives.
        (Path(environ["CODEX_HOME"]) / "models_cache.json").unlink()
        self.assertEqual(self.heartbeat(environ).get("profile"), "full")

    def test_an_old_claude_profile_is_not_reused_after_upgrade(self):
        store = StateStore(self.state_root)
        store.update(self.project, lambda state: (
            replace(state, activation={"claude": {
                "session_id": "claude-session", "plugin_version": "1.3.10", "profile": "opus",
                "claude_probe_attempted": True,
            }}), ()
        ))
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        self.assertFalse(self.heartbeat(environ, "claude", source="resume").get("profile"))
        with patch("plugins.symphony.symphony.runtime._claude_accepts", return_value=True) as probe:
            handle({
                "session_id": "claude-session", "cwd": str(self.project),
                "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start ship it",
            }, environ)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(store.load(self.project).activation["claude"]["profile"], "opus")

    def test_a_current_claude_profile_is_reused_within_its_session(self):
        store = StateStore(self.state_root)
        store.update(self.project, lambda state: (
            replace(state, activation={"claude": {
                "session_id": "claude-session", "plugin_version": PLUGIN_VERSION, "profile": "opus"
            }}), ()
        ))
        self.assertEqual(self.heartbeat({"SYMPHONY_STATE_DIR": str(self.state_root)}, "claude")["profile"], "opus")

    def test_a_claude_plan_without_model_access_uses_the_floor(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        completed = unittest.mock.Mock(stdout=json.dumps({"subscriptionType": "team"}))
        with patch("subprocess.run", return_value=completed) as run:
            profile = self.heartbeat(environ, "claude").get("profile")
        self.assertFalse(profile)
        run.assert_not_called()

    def test_explicit_claude_model_access_selects_only_usable_profiles(self):
        for index, (models, expected) in enumerate((
            ("claude-sonnet-5,claude-opus-5-5", "opus"),
            ("claude-sonnet-5,claude-opus-5-5,claude-fable-5-1", "fable"),
            ("claude-fable-5-1", "unavailable"),
        )):
            with self.subTest(models=models):
                self.state_root = self.root / f"state-{index}"
                environ = {
                    "SYMPHONY_STATE_DIR": str(self.state_root),
                    "SYMPHONY_CLAUDE_AVAILABLE_MODELS": models,
                }
                self.assertEqual(self.heartbeat(environ, "claude").get("profile"), expected)

    def test_first_claude_task_probes_the_active_login_once_for_an_opus_route(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}

        def probe(argv, **_kwargs):
            model = argv[argv.index("--model") + 1]
            self.assertIn('{"disableAllHooks":true}', argv)
            self.assertIn("--max-budget-usd", argv)
            self.assertEqual(argv[argv.index("--disallowedTools") + 1], "mcp__*")
            return unittest.mock.Mock(returncode=0, stdout=json.dumps({
                "modelUsage": {model: {"inputTokens": 1}}, "is_error": False,
            }))

        payload = {"session_id": "claude-session", "cwd": str(self.project),
                   "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start fix it"}
        with patch("plugins.symphony.symphony.runtime.shutil.which", return_value="claude"), \
             patch("plugins.symphony.symphony.runtime.subprocess.run", side_effect=probe) as run:
            handle(payload, environ)
            self.assertEqual(StateStore(self.state_root).load(self.project).activation["claude"]["profile"], "opus")
            handle(payload, environ)
        self.assertEqual(run.call_count, 2)

    def test_session_model_is_not_proof_of_access(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROVIDER": "claude"}
        handle({"session_id": "claude-session", "cwd": str(self.project),
                "hook_event_name": "SessionStart", "model": "claude-sonnet-5"}, environ)
        payload = {"session_id": "claude-session", "cwd": str(self.project),
                   "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start fix it"}
        def probe(argv, **_kwargs):
            model = argv[argv.index("--model") + 1]
            return unittest.mock.Mock(returncode=0, stdout=json.dumps({
                "modelUsage": {model: {"inputTokens": 1}}, "is_error": False,
            }))

        with patch("plugins.symphony.symphony.runtime.shutil.which", return_value="claude"), \
             patch("plugins.symphony.symphony.runtime.subprocess.run", side_effect=probe) as run:
            handle(payload, environ)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(StateStore(self.state_root).load(self.project).activation["claude"]["profile"], "opus")

    def test_claude_probe_rejects_a_substituted_model_and_does_not_retry(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        payload = {"session_id": "claude-session", "cwd": str(self.project),
                   "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start fix it"}

        def probe(argv, **_kwargs):
            model = argv[argv.index("--model") + 1]
            actual = "claude-sonnet-5"
            return unittest.mock.Mock(returncode=0, stdout=json.dumps({
                "modelUsage": {actual: {"inputTokens": 1}}, "is_error": False,
            }))

        with patch("plugins.symphony.symphony.runtime.shutil.which", return_value="claude"), \
             patch("plugins.symphony.symphony.runtime.subprocess.run", side_effect=probe) as run:
            handle(payload, environ)
            activation = StateStore(self.state_root).load(self.project).activation["claude"]
            self.assertEqual(activation["profile"], "sonnet")
            self.assertTrue(activation["claude_probe_attempted"])
            handle(payload, environ)
        self.assertEqual(run.call_count, 2)

    def test_claude_probe_timeout_uses_visible_fallback_once(self):
        import subprocess

        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        payload = {"session_id": "claude-session", "cwd": str(self.project),
                   "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start fix it"}
        with patch("plugins.symphony.symphony.runtime.shutil.which", return_value="claude"), \
             patch("plugins.symphony.symphony.runtime.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("claude", 15)) as run:
            result = handle(payload, environ)
            handle(payload, environ)
        self.assertEqual(run.call_count, 2)
        self.assertIn("Sonnet fallback", result.stdout)
        activation = StateStore(self.state_root).load(self.project).activation["claude"]
        self.assertFalse(activation.get("profile"))
        self.assertTrue(activation["claude_probe_attempted"])

    def test_pending_claude_root_does_not_reprobe_after_other_starts(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        with patch("plugins.symphony.symphony.runtime._claude_accepts", return_value=True) as probe:
            for index in range(6):
                payload = {"session_id": f"starting-{index}", "cwd": str(self.project),
                           "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:start fix it"}
                handle(payload, environ)
            oldest = {"session_id": "starting-0", "cwd": str(self.project),
                      "hook_event_name": "UserPromptSubmit", "prompt": "Continue the task"}
            handle(oldest, environ)

        self.assertEqual(probe.call_count, 12)
        activation = StateStore(self.state_root).load(self.project).activation["claude"]
        self.assertIn("starting-0", activation["pending_sessions"])
        self.assertEqual(
            next(item for item in activation["session_profiles"]
                 if item["session_id"] == "starting-0")["profile"], "opus",
        )

    def test_other_session_does_not_replace_active_owner_profile(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        owner = {"session_id": "owner", "cwd": str(self.project)}
        with patch("plugins.symphony.symphony.runtime._claude_accepts", return_value=True) as probe:
            handle({**owner, "hook_event_name": "UserPromptSubmit",
                    "prompt": "/symphony:start fix it"}, environ)
            store = StateStore(self.state_root)
            self.seed_claude_owner()
            handle({"session_id": "other", "cwd": str(self.project),
                    "hook_event_name": "SessionStart"}, environ)
            handle({**owner, "hook_event_name": "UserPromptSubmit",
                    "prompt": "Continue the active run"}, environ)
        self.assertEqual(probe.call_count, 2)
        activation = store.load(self.project).activation["claude"]
        self.assertEqual(activation["profile"], "opus")
        self.assertEqual({item["session_id"]: item["profile"] for item in activation["session_profiles"]}["owner"], "opus")

    def test_one_shot_owner_reprobe_persists_boolean_attempt(self):
        store = StateStore(self.state_root)
        self.seed_claude_owner()
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        with patch("plugins.symphony.symphony.runtime._claude_accepts", return_value=True) as probe:
            handle({"session_id": "owner", "cwd": str(self.project),
                    "hook_event_name": "UserPromptSubmit", "prompt": "Continue the active run"}, environ)
        self.assertEqual(probe.call_count, 2)
        activation = store.load(self.project).activation["claude"]
        self.assertIs(activation["claude_probe_attempted"], True)
        self.assertEqual(activation["profile"], "opus")

    def test_owner_lead_spawn_uses_its_profile_without_another_prompt(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root)}
        owner = {"session_id": "owner", "cwd": str(self.project)}
        with patch("plugins.symphony.symphony.runtime._claude_accepts", return_value=True):
            handle({**owner, "hook_event_name": "UserPromptSubmit",
                    "prompt": "/symphony:start fix it"}, environ)
        store = StateStore(self.state_root)
        self.seed_claude_owner({"size": "small", "complexity": "complex", "risk": "normal"})
        for index in range(6):
            handle({"session_id": f"other-{index}", "cwd": str(self.project),
                    "hook_event_name": "SessionStart"}, environ)
        records = store.load(self.project).activation["claude"]["session_profiles"]
        self.assertLessEqual(len(records), 5)
        self.assertIn("owner", {item["session_id"] for item in records})
        spawn = handle({**owner, "hook_event_name": "PreToolUse", "tool_name": "Agent",
                        "tool_input": {"subagent_type": "symphony:symphony-lead-claude-sonnet-5-xhigh",
                                       "prompt": "SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: "
                                                 '{"size":"small","complexity":"complex","risk":"normal"}'}},
                       environ)
        self.assertEqual(store.load(self.project).activation["claude"]["profile"], "opus")
        self.assertIn("claude-opus-5-5", spawn.stdout)

    def test_probe_child_cannot_reenter_symphony_when_hook_disabling_is_ignored(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_CLAUDE_PROBE": "1"}
        result = handle({"session_id": "child", "cwd": str(self.project),
                         "hook_event_name": "UserPromptSubmit", "prompt": "Reply ok."}, environ)
        self.assertEqual(result.stdout, "")
        self.assertEqual(StateStore(self.state_root).load(self.project).activation, {})

    def test_proceed_does_not_transfer_consent_to_another_session(self):
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROVIDER": "claude",
                   "SYMPHONY_PROFILE": "sonnet"}
        owner = {"session_id": "owner", "cwd": str(self.project)}
        handle({**owner, "hook_event_name": "SessionStart"}, environ)
        handle({**owner, "hook_event_name": "UserPromptSubmit", "prompt": "/symphony:proceed"}, environ)
        other = {"session_id": "other", "cwd": str(self.project)}
        handle({**other, "hook_event_name": "SessionStart"}, environ)
        handle({**other, "hook_event_name": "PreToolUse", "tool_name": "Agent",
                "tool_input": {}}, environ)
        activation = StateStore(self.state_root).load(self.project).activation["claude"]
        self.assertEqual(set(activation["accepted"]), {"owner"})
        self.assertEqual(activation["accepted"]["owner"]["profile"], "sonnet")
        self.assertFalse(activation.get("accepted_profile"))

    def test_legacy_flat_consent_migrates_to_original_session(self):
        store = StateStore(self.state_root)
        store.update(self.project, lambda state: (
            replace(state, activation={"claude": {
                "session_id": "owner", "plugin_version": PLUGIN_VERSION,
                "profile": "sonnet", "accepted_profile": "sonnet",
                "accepted_route": "small/complex",
            }}), ()
        ))
        environ = {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROVIDER": "claude"}
        handle({"session_id": "other", "cwd": str(self.project),
                "hook_event_name": "SessionStart"}, environ)
        handle({"session_id": "other", "cwd": str(self.project),
                "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {}}, environ)
        activation = store.load(self.project).activation["claude"]
        self.assertEqual(set(activation["accepted"]), {"owner"})
        self.assertEqual(activation["accepted"]["owner"]["route"], "small/complex")
        self.assertFalse(activation.get("accepted_profile"))

    def test_an_explicit_claude_profile_pin_is_an_opt_in(self):
        environ = {
            "SYMPHONY_STATE_DIR": str(self.state_root),
            "SYMPHONY_PROFILE": "fable",
        }
        self.assertEqual(self.heartbeat(environ, "claude").get("profile"), "fable")


if __name__ == "__main__":
    unittest.main()


class AgentFileContractTests(unittest.TestCase):
    """The Claude agent filenames are part of the routing contract.

    A profile naming a model with no matching file blocks every spawn of that
    role permanently, and the bad value is persisted into the recorded route,
    so it survives retries. This is the check that stops that shipping.
    """

    def generator(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "generate_agents.py"
        spec = importlib.util.spec_from_file_location("generate_agents", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_every_selectable_route_has_a_packaged_agent(self):
        generator = self.generator()
        agents = Path(__file__).resolve().parents[1] / "agents"
        present = {path.name for path in agents.glob("*.md")}
        required = {
            f"symphony-{role}-{model}-{effort}.md"
            for role, model, effort in generator.required_agents()
        }
        self.assertEqual(required - present, set(), "a profile names a model with no agent file")
        self.assertEqual(present - required, set(), "an agent file no profile can select")

    def test_each_agent_declares_the_model_and_effort_its_name_promises(self):
        generator = self.generator()
        agents = Path(__file__).resolve().parents[1] / "agents"
        problems = [
            problem
            for path in sorted(agents.glob("*.md"))
            for problem in generator.contract_errors(path.name, path.read_text())
        ]
        self.assertEqual(problems, [])

    def test_the_floor_profile_can_spawn_an_assessor(self):
        # The assessor opens the run, so a floor-profile account that cannot
        # spawn one is an account Symphony can never govern at all.
        agents = Path(__file__).resolve().parents[1] / "agents"
        floor = profiles_for("claude")[-1]
        expected = f"symphony-assessor-{floor['tiers']['strongest']}-high.md"
        self.assertTrue((agents / expected).is_file(), f"{expected} is missing")

    def test_reference_refresh_detects_profile_meaning_changes(self):
        generator = self.generator()
        routing_module = sys.modules[generator.profiles_for.__module__]
        original = generator.REFERENCE.read_text(encoding="utf-8")
        document = json.loads(routing_module.PROFILES_PATH.read_text(encoding="utf-8"))
        profiles = document["providers"]["codex"]["profiles"]
        cell = next(cell for cell in profiles[0]["matrix"] if profiles[0]["matrix"][cell] != profiles[-1]["matrix"][cell])
        profiles[0]["matrix"][cell] = profiles[-1]["matrix"][cell]
        with TemporaryDirectory() as directory:
            changed_profiles = Path(directory) / "profiles.json"
            changed_profiles.write_text(json.dumps(document), encoding="utf-8")
            generated_reference = Path(directory) / "capability-routing.md"
            generated_reference.write_text(original, encoding="utf-8")
            with patch.object(routing_module, "PROFILES_PATH", changed_profiles), patch.object(generator, "REFERENCE", generated_reference):
                routing_module._profiles.cache_clear()
                try:
                    with patch.object(sys, "argv", ["generate_agents.py", "--check"]), redirect_stdout(StringIO()):
                        self.assertEqual(generator.main(), 1)
                    with patch.object(sys, "argv", ["generate_agents.py"]):
                        self.assertEqual(generator.main(), 0)
                    changed = generated_reference.read_text(encoding="utf-8")
                    self.assertNotEqual(changed, original)
                    self.assertEqual(changed, generator.reference_text(changed))
                    with patch.object(sys, "argv", ["generate_agents.py", "--check"]):
                        self.assertEqual(generator.main(), 0)
                finally:
                    routing_module._profiles.cache_clear()


class ClampGateTests(unittest.TestCase):
    """A weaker model must not be substituted silently; a weaker effort may be."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state_root = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def environ(self, profile):
        return {"SYMPHONY_STATE_DIR": str(self.state_root), "SYMPHONY_PROFILE": profile}

    def payload(self, **extra):
        return {
            "session_id": "codex-session",
            "cwd": str(self.project),
            "turn_id": "turn-1",
            "model": "codex-model",
            **extra,
        }

    def send(self, environ, **extra):
        return handle(self.payload(**extra), environ)

    def output(self, result):
        return json.loads(result.stdout) if result.stdout else {}

    def open_and_spawn_lead(self, profile, size=CODEX_DRIFT_SIZE, complexity=CODEX_DRIFT_COMPLEXITY):
        environ = self.environ(profile)
        snapshot = snapshot_for("codex", profile)
        self.send(environ, hook_event_name="SessionStart")
        self.send(
            environ,
            hook_event_name="PreToolUse",
            tool_name="spawn_agent",
            tool_input={
                "message": "SYMPHONY_ROLE: assessor\nShip it",
                "model": snapshot.tiers["strongest"],
                "reasoning_effort": "high",
            },
        )
        marker = json.dumps(
            {"size": size, "complexity": complexity, "risk": "normal", "rationale": "x", "topology": "direct"}
        )
        return environ, self.send(
            environ,
            hook_event_name="PreToolUse",
            tool_name="spawn_agent",
            tool_input={
                "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                "model": snapshot.matrix[CODEX_DRIFT_CELL]["model"],
                "reasoning_effort": snapshot.matrix[CODEX_DRIFT_CELL]["effort"],
            },
        )

    def test_a_fully_entitled_account_is_never_gated(self):
        _, result = self.open_and_spawn_lead("full")
        self.assertNotEqual(self.output(result).get("decision"), "block")

    def test_a_tier_clamp_blocks_and_names_the_control(self):
        _, result = self.open_and_spawn_lead("base")
        output = self.output(result)
        self.assertEqual(output.get("decision"), "block")
        self.assertIn(CODEX_BASE_ROUTE["model"], output["reason"])
        self.assertIn(CODEX_FULL_ROUTE["model"], output["reason"])
        self.assertIn("$symphony:symphony proceed", output["reason"])

    def test_accepting_the_clamp_unblocks_the_rest_of_the_session(self):
        environ, blocked = self.open_and_spawn_lead("base")
        self.assertEqual(self.output(blocked).get("decision"), "block")

        accepted = self.send(
            environ, hook_event_name="UserPromptSubmit", prompt="$symphony:symphony proceed"
        )
        self.assertIn("accepted", self.output(accepted)["hookSpecificOutput"]["additionalContext"].lower())

        marker = json.dumps(
            {"size": CODEX_DRIFT_SIZE, "complexity": CODEX_DRIFT_COMPLEXITY,
             "risk": "normal", "rationale": "x", "topology": "direct"}
        )
        retried = self.send(
            environ,
            hook_event_name="PreToolUse",
            tool_name="spawn_agent",
            tool_input={
                "message": f"SYMPHONY_ROLE: lead\nSYMPHONY_ROUTE: {marker}\nShip it",
                "model": CODEX_BASE_ROUTE["model"],
                "reasoning_effort": CODEX_BASE_ROUTE["effort"],
            },
        )
        self.assertNotIn("decision", self.output(retried))

    def test_acceptance_does_not_survive_into_a_new_session(self):
        environ, _ = self.open_and_spawn_lead("base")
        self.send(environ, hook_event_name="UserPromptSubmit", prompt="$symphony:symphony proceed")

        store = StateStore(self.state_root)
        self.assertEqual(store.load(self.project).activation["codex"]["accepted_profile"], "base")

        handle(
            {**self.payload(session_id="codex-session-2"), "hook_event_name": "SessionStart"},
            environ,
        )
        self.assertFalse(
            store.load(self.project).activation["codex"].get("accepted_profile"),
            "a clamp accepted in one session must be re-asked in the next",
        )

    def test_proceed_without_a_clamped_route_says_so(self):
        environ = self.environ("full")
        result = self.send(
            environ, hook_event_name="UserPromptSubmit", prompt="$symphony:symphony proceed"
        )
        self.assertIn("no clamped route", self.output(result)["hookSpecificOutput"]["additionalContext"])
