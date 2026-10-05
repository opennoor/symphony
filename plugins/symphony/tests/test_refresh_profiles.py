import importlib.util
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory


def load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "refresh_profiles.py"
    spec = importlib.util.spec_from_file_location("refresh_profiles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def roster(*slugs, efforts=("low", "medium", "high")):
    return [
        {
            "slug": slug,
            "visibility": "list",
            "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
        }
        for slug in slugs
    ]


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.refresh = load()

    def test_a_hidden_model_is_not_available(self):
        entries = roster("gpt-6-luna")
        entries.append({"slug": "hidden", "visibility": "hide"})
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "models_cache.json").write_text(json.dumps({"models": entries}))
            visible = {entry["slug"] for entry in self.refresh.codex_roster(home)}
        self.assertEqual(visible, {"gpt-6-luna"})

    def test_a_missing_roster_stops_rather_than_shipping_a_guess(self):
        with TemporaryDirectory() as directory:
            with patch.object(self.refresh, "_app_server_roster", return_value=roster("gpt-6-luna")):
                self.assertEqual(
                    self.refresh.codex_roster(Path(directory) / "absent"),
                    roster("gpt-6-luna"),
                )

    def test_retiring_codex_models_are_not_selectable_from_the_roster(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "models_cache.json").write_text(json.dumps({
                "models": [
                    {"slug": "gpt-6-luna", "visibility": "list"},
                    {"slug": "gpt-5.5", "visibility": "list"},
                    {"slug": "gpt-5.6-terra", "visibility": "list"},
                ]
            }))
            self.assertEqual(
                [item["slug"] for item in self.refresh.codex_roster(home)],
                ["gpt-6-luna", "gpt-5.6-terra"],
            )

    def test_cache_and_app_server_honor_policy_and_explicit_lifecycle(self):
        entries = roster("gpt-6-luna", "gpt-5.5", "gpt-7")
        entries.append({**roster("gpt-6-sol")[0], "lifecycle": "legacy"})
        with TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "models_cache.json").write_text(json.dumps({"models": entries}))
            self.assertEqual([item["slug"] for item in self.refresh.codex_roster(home)], ["gpt-6-luna"])
            (home / "models_cache.json").unlink()
            with patch.object(self.refresh, "_app_server_roster", return_value=entries):
                self.assertEqual([item["slug"] for item in self.refresh.codex_roster(home)], ["gpt-6-luna"])

    def test_prompts_include_provider_specific_rates_and_capability_order(self):
        codex = self.refresh._agent_prompt("codex", [{"matrix": {}}], roster("gpt-6-luna"))
        claude = self.refresh._agent_prompt("claude", [{"matrix": {}}], [{"id": "claude-sonnet-5"}])
        self.assertIn("codex_credits_per_million_tokens", codex)
        self.assertNotIn("api_usd_per_million_tokens", codex)
        self.assertIn("api_usd_per_million_tokens", claude)
        self.assertIn("cache_write_5m", claude)
        self.assertIn('"least capable model"', codex)

    def test_a_missing_roster_and_failed_live_query_stops_instead_of_guessing(self):
        with TemporaryDirectory() as directory:
            with patch.object(self.refresh, "_app_server_roster", side_effect=RuntimeError("offline")):
                with self.assertRaises(SystemExit):
                    self.refresh.codex_roster(Path(directory) / "absent")

    def test_claude_agent_bootstraps_from_the_strongest_rostered_model(self):
        current = json.loads(self.refresh.PROFILES.read_text())["providers"]["claude"]["profiles"]
        declared = {model for profile in current for model in profile["efforts"]}
        sonnet, opus, fable = (
            next((model for model in sorted(declared)
                  if self.refresh._model_rank("claude", model) == rank), f"claude-{family}-future")
            for family, rank in (("sonnet", 1), ("opus", 2), ("fable", 3))
        )
        for ids, expected in (
            ((sonnet, opus), opus),
            ((sonnet,), sonnet),
            ((sonnet, opus, fable), fable),
        ):
            with self.subTest(ids=ids):
                result = {"profiles": []}
                completed = unittest.mock.Mock(returncode=0, stdout=json.dumps(result))
                with patch.object(self.refresh.subprocess, "run", return_value=completed) as run, \
                     patch.object(self.refresh, "validate_matrix", return_value=result):
                    self.refresh._run_provider_agent("claude", current, [{"id": model} for model in ids])
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--model") + 1], expected)
                effort = argv[argv.index("--effort") + 1]
                self.assertIn(effort, self.refresh.efforts_by_model(current, expected))

    def test_claude_agent_requires_a_rankable_rostered_model(self):
        current = json.loads(self.refresh.PROFILES.read_text())["providers"]["claude"]["profiles"]
        for entries in ([], [{"id": "unranked-model"}], [{"id": "claude-opus-future"}]):
            with self.subTest(entries=entries), patch.object(self.refresh.subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "no supported Claude model"):
                    self.refresh._run_provider_agent("claude", current, entries)
                run.assert_not_called()

    def test_claude_generation_rejects_a_sonnet_only_opus_profile(self):
        current = json.loads(self.refresh.PROFILES.read_text())["providers"]["claude"]["profiles"]
        profiles = [{"id": item["id"], "matrix": json.loads(json.dumps(item["matrix"]))} for item in current]
        next(item for item in profiles if item["id"] == "opus")["matrix"] = json.loads(json.dumps(profiles[-1]["matrix"]))
        models = ("claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")
        decision = {
            "profiles": profiles,
            "model_order": list(models),
            "model_efforts": current[0]["efforts"],
        }
        completed = unittest.mock.Mock(returncode=0, stdout=json.dumps(decision))
        with patch.object(self.refresh.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(SystemExit, "gated profile must improve a model"):
                self.refresh._run_provider_agent("claude", current, [{"id": model} for model in models])

    def test_codex_agent_prefers_newer_sol_at_equal_capability(self):
        current = json.loads(self.refresh.PROFILES.read_text())["providers"]["codex"]["profiles"]
        result = {"profiles": []}
        completed = unittest.mock.Mock(returncode=0, stdout=json.dumps(result))
        with patch.object(self.refresh.subprocess, "run", return_value=completed) as run, \
             patch.object(self.refresh, "validate_matrix", return_value=result):
            self.refresh._run_provider_agent(
                "codex", current, roster("gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna")
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-sol")


class ShippedProfileTests(unittest.TestCase):
    def test_new_models_have_reviewed_rates_and_supported_efforts(self):
        refresh = load()
        policy = refresh._policy()
        profiles = json.loads(refresh.PROFILES.read_text())["providers"]
        self.assertEqual(policy["reviewed_at"], "2026-09-29")
        self.assertEqual(policy["models"]["gpt-6.1-sol"]["codex_credits_per_million_tokens"],
                         {"input": 50, "cached_input": 2.5, "output": 250})
        self.assertEqual(policy["models"]["claude-sonnet-5-5"]["api_usd_per_million_tokens"],
                         {"input": 2, "cached_input": 0.2, "output": 10,
                          "cache_write_5m": 2.5, "cache_write_1h": 4})
        self.assertEqual(profiles["codex"]["profiles"][0]["efforts"]["gpt-6.1-sol"],
                         ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertEqual(profiles["claude"]["profiles"][0]["efforts"]["claude-sonnet-5-5"],
                         ["low", "medium", "high", "xhigh", "max"])

    def test_shipped_matrices_cover_each_cell_and_back_the_tier_summary(self):
        refresh = load()
        document = json.loads(refresh.PROFILES.read_text())
        for provider, block in document["providers"].items():
            for profile in block["profiles"]:
                self.assertEqual(set(profile["matrix"]), set(refresh.CELLS))
                self.assertEqual(
                    profile["tiers"],
                    {tier: profile["matrix"][cell]["model"] for tier, cell in refresh.TIER_CELLS.items()},
                )
                for choice in profile["matrix"].values():
                    self.assertIn(choice["effort"], profile["efforts"][choice["model"]])

    def test_claude_restricted_gates_cover_every_selected_model(self):
        refresh = load()
        profiles = json.loads(refresh.PROFILES.read_text())["providers"]["claude"]["profiles"]
        for profile in profiles[:-1]:
            with self.subTest(profile=profile["id"]):
                self.assertEqual(
                    set(profile["requires_all"]),
                    {choice["model"] for choice in profile["matrix"].values()},
                )

    def test_refresh_updates_gated_models_and_available_roster_with_matrix(self):
        refresh = load()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            document = json.loads(refresh.PROFILES.read_text())
            path.write_text(json.dumps(document))

            def decision(provider, current, roster):
                profiles = [{"id": item["id"], "matrix": dict(item["matrix"])} for item in current]
                if provider == "claude":
                    profiles[0]["matrix"]["small/complex"] = {
                        "model": "claude-opus-5-5", "effort": "xhigh"
                    }
                else:
                    next(item for item in profiles if item["id"] == "sol")["matrix"]["small/simple"] = {
                        "model": "gpt-6-luna", "effort": "low"
                    }
                return {"profiles": profiles, "model_efforts": current[0]["efforts"], "rationale": "test"}

            with patch.object(refresh, "PROFILES", path), \
                 patch.object(refresh, "codex_roster", return_value=roster("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra")), \
                 patch.object(refresh, "claude_roster", return_value=[{"id": "claude-sonnet-5"}, {"id": "claude-sonnet-5-5"}, {"id": "claude-opus-5-5"}]), \
                 patch.object(refresh, "_run_provider_agent", side_effect=decision):
                self.assertTrue(refresh.agent_probe(Path(directory)))
            fable = json.loads(path.read_text())["providers"]["claude"]["profiles"][0]
            self.assertEqual(fable["requires_all"], ["claude-opus-5-5", "claude-sonnet-5-5"])
            updated = json.loads(path.read_text())["providers"]
            self.assertEqual(updated["codex"]["available_models"], ["gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol"])
            self.assertEqual(updated["claude"]["available_models"], ["claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5-5"])
            sol = next(profile for profile in updated["codex"]["profiles"] if profile["id"] == "sol")
            self.assertEqual(sol["requires_all"], ["gpt-6-luna", "gpt-6-sol"])


class SemanticMatrixTests(unittest.TestCase):
    def setUp(self):
        self.refresh = load()

    def result(self):
        order = ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")
        effort_by_cell = {
            "small": ("medium", "high", "high"),
            "medium": ("low", "medium", "high"),
            "large": ("low", "low", "medium"),
        }
        ranks = {
            "small": (1, 1, 2),
            "medium": (1, 1, 2),
            "large": (0, 0, 1),
        }
        matrix = {}
        for size, models in ranks.items():
            for complexity, model_rank in zip(("simple", "mixed", "complex"), models):
                effort = effort_by_cell[size][("simple", "mixed", "complex").index(complexity)]
                matrix[f"{size}/{complexity}"] = {"model": order[model_rank], "effort": effort}
        fallback = {cell: {"model": "gpt-6-luna", "effort": "low"} for cell in matrix}
        return {
            "model_order": list(order),
            "model_efforts": {model: ["low", "medium", "high"] for model in order},
            "profiles": [
                {"id": "full", "matrix": matrix},
                {"id": "base", "matrix": fallback},
            ],
            "rationale": "trade capability for cost by task cell",
        }

    def test_semantic_matrix_accepts_complete_monotonic_provider_data(self):
        entries = roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", efforts=("low", "medium", "high"))
        result = self.refresh.validate_matrix("codex", self.result(), [{"id": "full"}, {"id": "base"}], entries)
        self.assertEqual(len(result["profiles"][0]["matrix"]), 9)

    def test_semantic_matrix_rejects_a_missing_cell(self):
        result = self.result()
        del result["profiles"][0]["matrix"]["small/simple"]
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix("codex", result, [{"id": "full"}, {"id": "base"}], roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"))

    def test_semantic_matrix_rejects_nonmonotonic_capability(self):
        result = self.result()
        result["profiles"][0]["matrix"]["small/complex"]["model"] = "gpt-6-luna"
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix("codex", result, [{"id": "full"}, {"id": "base"}], roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"))

    def test_codex_effort_support_is_taken_from_roster_not_agent_output(self):
        result = self.result()
        result["model_efforts"]["gpt-6-luna"] = ["low"]
        entries = roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", efforts=("low", "medium", "high"))
        validated = self.refresh.validate_matrix(
            "codex", result, [{"id": "full"}, {"id": "base"}], entries
        )
        self.assertEqual(
            validated["model_efforts"]["gpt-6-luna"], ["low", "medium", "high"]
        )

    def test_semantic_matrix_rejects_effort_absent_from_codex_roster(self):
        result = self.result()
        result["profiles"][0]["matrix"]["small/complex"]["effort"] = "max"
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix(
                "codex", result, [{"id": "full"}, {"id": "base"}],
                roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", efforts=("low", "medium", "high")),
            )

    def test_semantic_matrix_rejects_self_certified_model_order(self):
        result = self.result()
        result["profiles"][0]["matrix"] = {
            cell: {"model": "gpt-6-luna", "effort": "low"} for cell in self.refresh.CELLS
        }
        result["profiles"][1]["matrix"] = {
            cell: {"model": "gpt-6-astra", "effort": "low"} for cell in self.refresh.CELLS
        }
        result["model_order"] = list(reversed(result["model_order"]))
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix(
                "codex", result, [{"id": "full"}, {"id": "base"}],
                roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"),
            )

    def test_agent_model_order_is_not_used_as_authoritative_ranking(self):
        result = self.result()
        result["model_order"] = list(reversed(result["model_order"]))
        validated = self.refresh.validate_matrix(
            "codex", result, [{"id": "full"}, {"id": "base"}],
            roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"),
        )
        self.assertEqual(validated["model_order"], ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"])

    def test_fallback_cannot_promote_beyond_its_shipped_entitlement_floor(self):
        result = self.result()
        result["profiles"][1]["matrix"] = {
            cell: {"model": "gpt-6-sol", "effort": "low"} for cell in self.refresh.CELLS
        }
        result["model_order"] = ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]
        current = [
            {"id": "full", "matrix": {}},
            {"id": "base", "matrix": {
                cell: {"model": "gpt-6-luna", "effort": "low"} for cell in self.refresh.CELLS
            }},
        ]
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix(
                "codex", result, current, roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"),
            )

    def test_future_major_codex_model_has_no_verified_rank(self):
        self.assertIsNone(self.refresh._model_rank("codex", "gpt-7"))
        self.assertIsNone(self.refresh._model_rank("codex", "gpt-7-fast"))

    def test_sol_only_profile_cannot_gain_a_luna_requirement(self):
        current = json.loads(self.refresh.PROFILES.read_text())["providers"]["codex"]["profiles"]
        result = {"profiles": json.loads(json.dumps(current)),
                  "model_order": ["gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"]}
        next(profile for profile in result["profiles"] if profile["id"] == "sol")["matrix"]["large/simple"]["model"] = "gpt-6-luna"
        with self.assertRaisesRegex(SystemExit, "preserve its gated model coverage"):
            self.refresh.validate_matrix("codex", result, current,
                                         roster("gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"))

    def test_noncurrent_model_rejected_even_in_raw_roster(self):
        result = self.result()
        result["profiles"][1]["matrix"]["large/simple"]["model"] = "gpt-5.5"
        result["model_order"].append("gpt-5.5")
        with self.assertRaises(SystemExit):
            self.refresh.validate_matrix(
                "codex", result, [{"id": "full"}, {"id": "base"}],
                roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.5"),
            )

    def test_size_checks_token_rates_separately_from_capability(self):
        result = self.result()
        result["profiles"][0]["matrix"]["large/mixed"]["model"] = "gpt-5.6-terra"
        result["model_order"].append("gpt-5.6-terra")
        entries = roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra")
        with patch.object(self.refresh, "_older_without_price_advantage", return_value=False):
            with self.assertRaisesRegex(SystemExit, "output token rate"):
                self.refresh.validate_matrix("codex", result, [{"id": "full"}, {"id": "base"}], entries)

    def test_older_model_allowed_when_all_rates_are_lower(self):
        policy = self.refresh._policy()
        policy["models"]["gpt-5.6-terra"]["codex_credits_per_million_tokens"] = {
            "input": 10, "cached_input": 1, "output": 30,
        }
        result = self.result()
        result["profiles"][0]["matrix"]["large/mixed"]["model"] = "gpt-5.6-terra"
        result["model_order"].append("gpt-5.6-terra")
        with patch.object(self.refresh, "_policy", return_value=policy):
            checked = self.refresh.validate_matrix(
                "codex", result, [{"id": "full"}, {"id": "base"}],
                roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-5.6-terra"),
            )
        self.assertEqual(checked["profiles"][0]["matrix"]["large/mixed"]["model"], "gpt-5.6-terra")

    def test_replaced_retiring_fallback_can_drop_to_luna(self):
        current = [{"id": "full"}, {"id": "base", "matrix": {
            cell: {"model": "gpt-5.5", "effort": "low"} for cell in self.refresh.CELLS
        }}]
        self.refresh.validate_matrix(
            "codex", self.result(), current, roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra")
        )

    def test_validator_rejects_duplicate_and_reserved_profile_ids(self):
        for ids in (("full", "full"), ("full", "unavailable")):
            with self.subTest(ids=ids), self.assertRaisesRegex(SystemExit, "unique profile IDs"):
                self.refresh.validate_matrix(
                    "codex", self.result(), [{"id": pid} for pid in ids],
                    roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"),
                )


class PolicyCheckTests(unittest.TestCase):
    def setUp(self):
        self.refresh = load()

    def test_shipped_policy_checks_offline(self):
        self.assertEqual(self.refresh.check_policy(), 0)

    def test_offline_check_requires_both_providers_with_profiles(self):
        document = json.loads(self.refresh.PROFILES.read_text())
        for providers in ({}, {"codex": document["providers"]["codex"]},
                          {"codex": {"profiles": []}, "claude": document["providers"]["claude"]}):
            with self.subTest(providers=tuple(providers)):
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "profiles.json"
                    path.write_text(json.dumps({"providers": providers}))
                    with patch.object(self.refresh, "PROFILES", path):
                        self.assertEqual(self.refresh.check_policy(), 1)

    def test_duplicate_or_reserved_id_cannot_reach_runtime_snapshot(self):
        from symphony import routing, runtime
        from symphony.model import ProjectState

        for duplicate in ("full", "unavailable"):
            with self.subTest(duplicate=duplicate), TemporaryDirectory() as directory:
                document = json.loads(self.refresh.PROFILES.read_text())
                document["providers"]["codex"]["profiles"][-1]["id"] = duplicate
                path = Path(directory) / "profiles.json"
                path.write_text(json.dumps(document))
                with patch.object(self.refresh, "PROFILES", path), patch.object(routing, "PROFILES_PATH", path):
                    self.assertEqual(self.refresh.check_policy(), 1)
                    routing._profiles.cache_clear()
                    try:
                        with self.assertRaisesRegex(ValueError, "unique profile IDs"):
                            routing.snapshot_for("codex", "full")
                        self.assertEqual(
                            runtime._entitlement_profile(
                                ProjectState(), "codex", "test-session", {"SYMPHONY_PROFILE": "full"}
                            ),
                            routing.NO_PROFILE,
                        )
                        response = runtime.handle({
                            "cwd": directory,
                            "session_id": "test-session",
                            "hook_event_name": "PreToolUse",
                            "tool_name": "spawn_agent",
                            "tool_input": {"message": "SYMPHONY_ROLE: assessor", "model": "gpt-6-astra",
                                           "reasoning_effort": "high"},
                        }, {"SYMPHONY_PROVIDER": "codex", "SYMPHONY_PROFILE": "full",
                            "SYMPHONY_STATE_DIR": str(Path(directory) / "state")})
                        # A broken shipped map is Symphony's problem: it never stops the agent.
                        self.assertNotEqual("block", (json.loads(response.stdout) if response.stdout else {}).get("decision"))
                    finally:
                        routing._profiles.cache_clear()

    def test_expired_current_model_is_ineligible_even_with_a_roster_entry(self):
        policy = self.refresh._policy()
        policy["models"]["gpt-6-luna"]["retirement_date"] = self.refresh._today()
        with patch.object(self.refresh, "_policy", return_value=policy):
            self.assertEqual(self.refresh.check_policy(validate_profiles=False), 1)
            self.assertFalse(self.refresh._current("gpt-6-luna"))
            with self.assertRaises(SystemExit):
                self.refresh.validate_matrix(
                    "codex", SemanticMatrixTests().result(), [{"id": "full"}, {"id": "base"}],
                    roster("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"),
                )
            with TemporaryDirectory() as directory:
                home = Path(directory)
                (home / "models_cache.json").write_text(json.dumps({"models": roster("gpt-6-luna", "gpt-6-sol")}))
                self.assertEqual([entry["slug"] for entry in self.refresh.codex_roster(home)], ["gpt-6-sol"])

    def test_offline_check_rejects_profile_local_effort_support_drift(self):
        document = json.loads(self.refresh.PROFILES.read_text())
        opus = document["providers"]["claude"]["profiles"][1]
        # Keep every route valid under its local declaration while making
        # this profile disagree with the other profiles' support snapshot.
        model = "claude-opus-5-5"
        levels = opus["efforts"][model]
        selected = {choice["effort"] for choice in opus["matrix"].values()
                    if choice["model"] == model}
        unused = next((level for level in reversed(levels)
                       if level not in selected and level != "high"), None)
        if unused:
            levels.remove(unused)
        else:
            levels.insert(0, "none")
        self.assertTrue(all(choice["effort"] in opus["efforts"][choice["model"]]
                            for choice in opus["matrix"].values()))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document))
            with patch.object(self.refresh, "PROFILES", path):
                self.assertEqual(self.refresh.check_policy(), 1)

    def test_offline_uses_selected_models_without_inventing_availability(self):
        document = json.loads(self.refresh.PROFILES.read_text())
        codex = document["providers"]["codex"]["profiles"]
        codex[:] = [codex[0], codex[-1]]
        matrix = {cell: {"model": "gpt-5.6-terra", "effort": "low"}
                  for cell in self.refresh.CELLS}
        for cell in ("small/complex", "medium/complex"):
            matrix[cell] = {"model": "gpt-6-astra", "effort": "high"}
        for cell in ("large/simple", "large/mixed"):
            matrix[cell] = {"model": "gpt-6-luna", "effort": "low"}
        fallback = {cell: {"model": "gpt-6-luna", "effort": "low"}
                    for cell in self.refresh.CELLS}
        # Keep effort monotonic and preserve the high-risk effort support floor.
        for size in ("small", "medium", "large"):
            matrix[f"{size}/mixed"]["effort"] = "medium"
            matrix[f"{size}/complex"]["effort"] = "high"
            fallback[f"{size}/mixed"]["effort"] = "medium"
            fallback[f"{size}/complex"]["effort"] = "high"
        support = {model: ["low", "medium", "high", "xhigh", "max"]
                   for model in ("gpt-6-luna", "gpt-5.6-terra", "gpt-6-astra")}
        document["providers"]["codex"]["available_models"] = sorted(support)
        for profile, choices in zip(codex, (matrix, fallback)):
            profile["matrix"] = choices
            profile["tiers"] = {tier: choices[cell]["model"] for tier, cell in self.refresh.TIER_CELLS.items()}
            profile["efforts"] = support
            profile["requires_all"] = sorted({choice["model"] for choice in choices.values()}) if profile is codex[0] else []
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(document))
            with patch.object(self.refresh, "PROFILES", path):
                self.assertEqual(self.refresh.check_policy(), 0)
                document["providers"]["codex"]["available_models"].append("gpt-6-sol")
                path.write_text(json.dumps(document))
                self.assertEqual(self.refresh.check_policy(), 1)
        result = {"profiles": [{"id": profile["id"], "matrix": profile["matrix"]} for profile in codex],
                  "model_order": list(support), "model_efforts": support}
        selected_roster = roster("gpt-6-luna", "gpt-5.6-terra", "gpt-6-astra",
                                 efforts=("low", "medium", "high", "xhigh", "max"))
        self.refresh.validate_matrix("codex", result, codex, selected_roster)
        available = roster("gpt-6-luna", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-sol",
                           efforts=("low", "medium", "high", "xhigh", "max"))
        with self.assertRaisesRegex(SystemExit, "older gpt-5.6-terra"):
            self.refresh.validate_matrix("codex", result, codex, available)

    def test_policy_age_blocks_live_probe_only(self):
        with patch.object(self.refresh, "_today", return_value="2026-11-15"):
            self.assertEqual(self.refresh.check_policy(), 0)
            self.assertEqual(self.refresh.check_policy(require_fresh=True), 1)

    def test_verify_runs_policy_check_before_provider_calls(self):
        with patch.object(self.refresh, "check_policy", return_value=1), \
             patch.object(self.refresh, "codex_roster") as roster_call:
            self.assertEqual(self.refresh.verify(), 1)
            roster_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
