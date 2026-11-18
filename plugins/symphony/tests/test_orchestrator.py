"""Orchestrator mode: one selection rule for both providers, and agents that
really run, finish, fail, stop and report through the launcher."""

import io
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import textwrap
import time
import unittest

from plugins.symphony.symphony import orchestrator as orch
from plugins.symphony.symphony import orchestrator_cli
from plugins.symphony.symphony.routing import Assessment, profiles_for


CODEX_BEST = profiles_for("codex")[0]["id"]
CLAUDE_BEST = profiles_for("claude")[0]["id"]

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    """Stands in for a provider CLI: records how it was called, answers by role."""
    import json, os, sys, time
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_RECORD")
    if record:
        with open(record, "a") as handle:
            handle.write(json.dumps({{"cli": name, "argv": argv, "prompt": prompt, "cwd": os.getcwd(),
                                     "env": {{key: value for key, value in os.environ.items()
                                             if key.startswith("SYMPHONY_")}}}}) + "\\n")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    if os.environ.get("FAKE_FAIL") == name:
        sys.stderr.write("provider refused the model\\n")
        raise SystemExit(7)
    if "SYMPHONY_ROLE: assessor" in prompt:
        text = os.environ.get("FAKE_ASSESSMENT", 'Sized it.\\nSYMPHONY_ASSESSMENT: {{"size":"medium",'
                              '"complexity":"complex","risk":"normal","rationale":"two parts","topology":"two packets"}}')
    else:
        text = "done by " + name + '\\nSYMPHONY_OUTCOME: {{"status":"completed"}}'
    if name == "codex":
        print(json.dumps({{"type": "thread.started", "thread_id": "thread-1"}}))
        with open(argv[argv.index("-o") + 1], "w") as handle:
            handle.write(text)
        print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
    else:
        print(json.dumps({{"result": text, "session_id": "session-1", "total_cost_usd": 0.01,
                          "is_error": os.environ.get("FAKE_CLAUDE_ERROR") == "1"}}))
''')


def selection_environ(root: Path, *, codex=CODEX_BEST, claude=CLAUDE_BEST, **extra) -> dict:
    """Both CLIs present and each account pinned to a profile, with no probing."""
    environ = {"SYMPHONY_STATE_DIR": str(root / "state"), "PATH": "", "CODEX_HOME": str(root / "no-codex-home"),
               "SYMPHONY_ORCHESTRATOR_NO_PROBE": "1", **extra}
    if codex:
        environ.update({"SYMPHONY_CODEX_CLI": "/fake/codex", "SYMPHONY_CODEX_PROFILE": codex})
    if claude:
        environ.update({"SYMPHONY_CLAUDE_CLI": "/fake/claude", "SYMPHONY_CLAUDE_PROFILE": claude})
    return environ


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def choose(self, role, size="", complexity="", environ=None, **options):
        assessment = Assessment(size, complexity) if size else None
        return orch.select(role, assessment=assessment, environ=environ or selection_environ(self.root),
                           **options)

    def test_the_assessor_is_the_strongest_model_on_either_provider(self):
        codex_stronger = self.choose("assessor", environ=selection_environ(self.root, claude="opus-5-5"))
        self.assertEqual((codex_stronger["provider"], codex_stronger["effort"]), ("codex", "high"))
        self.assertEqual(codex_stronger["model"], profiles_for("codex")[0]["tiers"]["strongest"])
        self.assertIn("capability fit", codex_stronger["why"])
        claude_stronger = self.choose("assessor", environ=selection_environ(self.root, codex="base"))
        self.assertEqual(claude_stronger["provider"], "claude")
        self.assertEqual(claude_stronger["model"], profiles_for("claude")[0]["tiers"]["strongest"])

    def test_equal_candidates_do_not_always_go_to_the_same_provider(self):
        chosen = {self.choose("assessor", rotation=f"run-{index}")["provider"] for index in range(40)}
        self.assertEqual(chosen, {"codex", "claude"})

    def test_equal_candidates_are_spread_by_load_then_by_what_each_was_given(self):
        for rotation in ("run-a", "run-b", "run-c"):
            with self.subTest(rotation=rotation):
                busy = self.choose("worker", "small", "complex", rotation=rotation, running={"claude": 1})
                self.assertEqual(busy["provider"], "codex")
                self.assertIn("fewer agents running", busy["why"])
                given = self.choose("worker", "small", "complex", rotation=rotation, launched={"codex": 2, "claude": 1})
                self.assertEqual(given["provider"], "claude")
                self.assertIn("fewer agents given so far", given["why"])

    def test_price_separates_equals_only_when_asked(self):
        # Sol and Opus are both the reviewed medium/complex choice; Sol costs half.
        priced = self.choose("worker", "medium", "complex", prefer="price", running={"codex": 3})
        self.assertEqual((priced["provider"], priced["prefer"]), ("codex", "price"))
        self.assertIn("on price", priced["why"])
        balanced = self.choose("worker", "medium", "complex", running={"codex": 3})
        self.assertEqual((balanced["provider"], balanced["prefer"]), ("claude", "balance"))
        from_environment = self.choose("worker", "medium", "complex", running={"codex": 3},
                                       environ=selection_environ(self.root, SYMPHONY_ORCHESTRATION_PREFER="price"))
        self.assertEqual(from_environment["provider"], "codex")
        with self.assertRaisesRegex(orch.OrchestrationError, "unknown preference"):
            self.choose("worker", "medium", "complex", prefer="cheapest")

    def test_an_account_limited_below_the_policy_model_loses_to_one_that_is_not(self):
        limited = selection_environ(self.root, codex="base")
        choice = self.choose("worker", "small", "complex", environ=limited, running={"claude": 5})
        self.assertEqual(choice["provider"], "claude")
        self.assertIn("capability fit", choice["why"])
        codex = next(item for item in choice["candidates"] if item["provider"] == "codex")
        self.assertGreater(codex["shortfall"], 0)
        self.assertTrue(any("account limits this" in reason for reason in codex["reasons"]))

    def test_an_independent_review_sits_on_the_other_provider_than_the_work(self):
        for reviewed in orch.PROVIDERS:
            with self.subTest(reviewed=reviewed):
                choice = self.choose("consultant", "small", "simple", avoid_provider=reviewed,
                                     running={provider: 4 for provider in orch.PROVIDERS if provider != reviewed})
                self.assertNotEqual(choice["provider"], reviewed)
                self.assertIn("independent of the reviewed provider", choice["why"])

    def test_cell_roles_use_each_provider_matrix_cell(self):
        for provider, other in (("codex", "claude"), ("claude", "codex")):
            for cell, expected in profiles_for(provider)[0]["matrix"].items():
                size, complexity = cell.split("/")
                with self.subTest(provider=provider, cell=cell):
                    choice = self.choose("worker", size, complexity, avoid_provider=other)
                    self.assertEqual((choice["provider"], choice["model"], choice["effort"]),
                                     (provider, expected["model"], expected["effort"]))

    def test_the_strongest_tier_can_be_asked_for_a_hard_question(self):
        choice = self.choose("consultant", "small", "simple", tier="strongest", avoid_provider="claude")
        self.assertEqual((choice["model"], choice["effort"]),
                         (profiles_for("codex")[0]["tiers"]["strongest"], "high"))

    def test_a_missing_cli_leaves_the_other_provider_and_says_why(self):
        choice = self.choose("lead", "medium", "mixed", environ=selection_environ(self.root, claude=""))
        self.assertEqual(choice["provider"], "codex")
        self.assertIn("claude", choice["unavailable"])
        self.assertIn("only available provider", choice["why"])
        with self.assertRaisesRegex(orch.OrchestrationError, "no provider can launch"):
            self.choose("lead", "medium", "mixed", environ=selection_environ(self.root, codex="", claude=""))

    def test_a_login_that_answers_for_no_model_is_unavailable(self):
        environ = selection_environ(self.root, claude="")
        environ["SYMPHONY_CLAUDE_CLI"] = "/fake/claude"
        cache = orch._claude_probe_cache(environ)
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"checked_at": time.time(), "models": []}))
        self.assertEqual(orch.applied_profile("claude", environ), "unavailable")
        choice = self.choose("assessor", environ=environ)
        self.assertEqual(choice["provider"], "codex")
        self.assertIn("no Claude model answered", choice["unavailable"]["claude"])

    def test_unknown_entitlement_uses_the_conservative_floor(self):
        environ = selection_environ(self.root, codex="", claude="")
        environ.update({"SYMPHONY_CODEX_CLI": "/fake/codex", "SYMPHONY_CLAUDE_CLI": "/fake/claude"})
        entries = {entry["provider"]: entry for entry in orch.catalog(environ)}
        self.assertEqual(entries["codex"]["profile"], "(floor)")
        self.assertEqual([item["model"] for item in entries["codex"]["models"]],
                         [profiles_for("codex")[-1]["tiers"]["strongest"]])
        self.assertTrue(entries["claude"]["available"])

    def test_an_explicit_route_needs_a_reason_and_stays_inside_the_account(self):
        environ = selection_environ(self.root)

        def explicit(provider, model="", effort="", why="the user asked"):
            return orch.explicit_selection(provider, model, effort, why, "worker",
                                           assessment=Assessment("small", "simple"), tier="", boost="off",
                                           environ=environ, probe=False)

        with self.assertRaisesRegex(orch.OrchestrationError, "--why is required"):
            explicit("claude", why=" ")
        with self.assertRaisesRegex(orch.OrchestrationError, "not in this account"):
            explicit("claude", model="gpt-6-luna")
        with self.assertRaisesRegex(orch.OrchestrationError, "supports efforts"):
            explicit("claude", effort="ultra")
        chosen = explicit("claude")
        cell = profiles_for("claude")[0]["matrix"]["small/simple"]
        self.assertEqual((chosen["model"], chosen["effort"]), (cell["model"], cell["effort"]))
        self.assertEqual(chosen["why"], "explicit: the user asked")

    def test_codex_credits_and_claude_dollars_share_one_scale(self):
        policy = json.loads((Path(orch.PROFILES_PATH).parent / "model-policy.json").read_text())
        self.assertEqual(policy["cross_provider_cost"]["codex_credit_usd"], 0.04)
        # (3 * 200 + 1000) / 4 credits at $0.04, and (3 * 8 + 40) / 4 dollars.
        self.assertAlmostEqual(orch.model_cost("gpt-6.2-astra"), 16.0)
        self.assertAlmostEqual(orch.model_cost("claude-fable-5-2"), 16.0)
        self.assertIsNone(orch.model_cost("not-a-model"))


class PromptTests(unittest.TestCase):
    def test_markers_take_the_last_assessment_and_every_decision(self):
        found = orch.markers(
            'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple"}\n'
            'prose mentioning SYMPHONY_ASSESSMENT: not json\n'
            '`SYMPHONY_ASSESSMENT: {"size":"large","complexity":"mixed","risk":"high"}`\n'
            'SYMPHONY_DECISION: {"size":"small","complexity":"simple"}\n'
            'SYMPHONY_DECISION: {"size":"medium","complexity":"mixed"}\n'
            'SYMPHONY_OUTCOME: ["not", "an", "object"]\n')
        self.assertEqual(found["SYMPHONY_ASSESSMENT"]["size"], "large")
        self.assertEqual(len(found["SYMPHONY_DECISION"]), 2)
        self.assertNotIn("SYMPHONY_OUTCOME", found)

    def test_only_a_complete_assessment_is_accepted(self):
        self.assertEqual(orch.valid_assessment({"size": "small", "complexity": "mixed"}),
                         Assessment("small", "mixed", "normal", "", ""))
        for broken in (None, {}, {"size": "tiny", "complexity": "simple"},
                       {"size": "small", "complexity": "simple", "risk": "extreme"}, "small/simple"):
            with self.subTest(broken=broken):
                self.assertIsNone(orch.valid_assessment(broken))

    def test_the_lead_is_told_how_to_delegate_and_what_its_size_requires(self):
        prompt = orch.lead_prompt("Build it", "r20260101000000-abcdef",
                                  {"size": "small", "complexity": "complex", "risk": "high",
                                   "rationale": "risky", "topology": "one packet"},
                                  {"consultation": "independent-check", "independent_review": True}, "AGENT")
        self.assertTrue(prompt.startswith("SYMPHONY_ROLE: lead\n"))
        self.assertIn("Delegate the substantive work to one worker.", prompt)
        self.assertIn("An independent review by a consultant is required", prompt)
        for command in ("AGENT agent --role worker", "AGENT agent --role consultant --purpose independent_review",
                        "AGENT wait", "AGENT result"):
            self.assertIn(command, prompt)
        self.assertTrue(prompt.endswith("Task:\nBuild it"))

    def test_agents_are_told_their_role_and_not_to_start_more(self):
        worker = orch.agent_prompt("worker", "substantive", "run", "objective: x")
        self.assertTrue(worker.startswith("SYMPHONY_ROLE: worker\npurpose: substantive\n"))
        self.assertIn("Do not start Symphony agents", worker)
        self.assertIn("You may use your host's own subagents", worker)
        review = orch.agent_prompt("consultant", "independent_review", "run", "review x")
        self.assertIn("Do not change it", review)
        self.assertIn("SYMPHONY_OUTCOME", orch.assessor_prompt("t").replace("ASSESSMENT", "OUTCOME"))

    def test_root_guidance_names_the_four_steps_in_order(self):
        text = orch.root_guidance("AGENT", "Build it")
        positions = [text.index(step) for step in ("AGENT assess --task-file", "AGENT lead --run",
                                                   "AGENT wait --run", "AGENT result --run")]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(text.endswith("Task reminder (full original request governs if clipped):\nBuild it"))
        self.assertLess(len(orch.root_guidance("AGENT")), 2048)
        self.assertLess(len(orch.root_reminder("AGENT")), 512)


@unittest.skipIf(os.name == "nt", "the stand-in provider CLIs are POSIX scripts")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        binaries = self.root / "bin"
        binaries.mkdir()
        for name in ("codex", "claude"):
            path = binaries / name
            path.write_text(FAKE_CLI.format(python=sys.executable))
            path.chmod(0o755)
        self.record = self.root / "calls.jsonl"
        self.environ = {
            "SYMPHONY_STATE_DIR": str(self.root / "state"),
            "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
            "CODEX_HOME": str(self.root / "no-codex-home"),
            "SYMPHONY_ORCHESTRATOR_NO_PROBE": "1",
            "SYMPHONY_CODEX_PROFILE": CODEX_BEST, "SYMPHONY_CLAUDE_PROFILE": CLAUDE_BEST,
            "FAKE_RECORD": str(self.record),
        }

    def tearDown(self):
        # Let any supervisor still writing its record finish before removal.
        for _ in range(100):
            runs = (self.root / "state" / "orchestration").glob("*/runs/*")
            if all(item.get("state") in orch.TERMINAL
                   for run in runs if run.is_dir() for item in orch.agents(run)):
                break
            time.sleep(0.1)
        self.temp.cleanup()

    def run_cli(self, *arguments, environ=None, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        code = orchestrator_cli.main(["--cwd", str(cwd or self.project), *arguments],
                                     environ or self.environ, out, err)
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def run_directory(self):
        return orch.run_dir(self.project, self.environ)

    def test_a_whole_run_from_assessment_to_the_leads_report(self):
        code, out, err = self.run_cli("assess", "--task", "Build the thing\nand verify it", "--wait", "30")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("assessment: size=medium complexity=complex risk=normal", out)
        directory = self.run_directory()
        self.assertIn(f"lead --run {directory.name}", out)
        run = orch.load_run(directory)
        self.assertEqual(run["task_label"], "Build the thing")
        self.assertEqual(run["assessment"]["source"], "assessor")

        code, out, err = self.run_cli("lead", "--wait", "30")
        self.assertEqual((code, err), (0, ""))
        code, out, _ = self.run_cli("agent", "--role", "worker", "--size", "small", "--complexity", "simple",
                                    "--packet", "objective: part one", "--name", "part one!", "--wait", "30")
        self.assertEqual(code, 0)
        records = {item["role"]: item for item in orch.agents(directory)}
        self.assertEqual(set(records), {"assessor", "lead", "worker"})
        worker = records["worker"]
        self.assertEqual(worker["name"], "part-one-")
        code, out, _ = self.run_cli("agent", "--role", "consultant", "--purpose", "independent_review",
                                    "--reviewing", worker["id"], "--size", "small", "--complexity", "simple",
                                    "--packet", "review part one", "--wait", "30")
        self.assertEqual(code, 0)
        reviewer = next(item for item in orch.agents(directory) if item["role"] == "consultant")
        self.assertNotEqual(reviewer["provider"], worker["provider"])
        self.assertEqual(reviewer["reviewing"], worker["id"])

        code, out, _ = self.run_cli("wait", "--timeout", "5")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("result")
        self.assertEqual(code, 0)
        self.assertIn(records["lead"]["id"], out)
        self.assertIn('SYMPHONY_OUTCOME: {"status":"completed"}', out)
        code, out, _ = self.run_cli("status")
        self.assertEqual(out.count("selection: "), 4)
        self.assertIn("outcome=completed", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(len(json.loads(out)["agents"]), 4)

        calls = self.calls()
        self.assertEqual(len(calls), 4)
        lead_call = next(call for call in calls if call["prompt"].startswith("SYMPHONY_ROLE: lead"))
        self.assertIn("Build the thing\nand verify it", lead_call["prompt"])
        self.assertIn("size medium, complexity complex", lead_call["prompt"])
        for call in calls:
            self.assertEqual(call["env"]["SYMPHONY_ORCHESTRATED"], "1")
            self.assertEqual(call["env"]["SYMPHONY_ORCHESTRATION_RUN"], directory.name)
            self.assertEqual(call["env"]["SYMPHONY_ORCHESTRATION_DEPTH"], "1")
            self.assertEqual(call["cwd"], str(self.project.resolve()))

    def test_each_provider_is_called_with_the_exact_model_effort_and_full_permissions(self):
        for provider in orch.PROVIDERS:
            self.run_cli("assess", "--task", "t", "--provider", provider, "--why", "test", "--wait", "30")
        calls = {call["cli"]: call["argv"] for call in self.calls()}
        codex, claude = calls["codex"], calls["claude"]
        strongest = profiles_for("codex")[0]["tiers"]["strongest"]
        self.assertEqual(codex[:5], ["exec", "--skip-git-repo-check", "-m", strongest, "-c"])
        self.assertIn("model_reasoning_effort=high", codex)
        self.assertEqual(codex[codex.index("-s") + 1], "danger-full-access")
        self.assertEqual(codex[codex.index("-C") + 1], str(self.project.resolve()))
        self.assertEqual(codex[-1], "-")
        self.assertEqual(claude[:5], ["-p", "--model", profiles_for("claude")[0]["tiers"]["strongest"],
                                      "--effort", "high"])
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(claude[claude.index("--output-format") + 1], "json")

    def test_permissions_can_be_narrowed_for_one_agent(self):
        self.run_cli("assess", "--task", "t", "--provider", "codex", "--why", "test",
                     "--permission", "read-only", "--wait", "30")
        self.run_cli("assess", "--task", "t", "--provider", "claude", "--why", "test",
                     "--permission", "workspace-write", "--wait", "30")
        calls = {call["cli"]: call["argv"] for call in self.calls()}
        self.assertEqual(calls["codex"][calls["codex"].index("-s") + 1], "read-only")
        self.assertEqual(calls["claude"][calls["claude"].index("--permission-mode") + 1], "acceptEdits")

    def test_records_are_private_and_the_prompt_is_not_kept(self):
        self.run_cli("assess", "--task", "a secret request", "--wait", "30")
        directory = self.run_directory()
        ledger = self.root / "state" / "orchestration"
        for path in [ledger, *ledger.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(path=str(path.relative_to(self.root))):
                self.assertEqual(mode & 0o077, 0)
        agent = orch.agents(directory)[0]
        self.assertFalse((directory / "agents" / agent["id"] / "prompt.md").exists())
        self.assertEqual((directory / "task.md").read_text(), "a secret request")
        self.assertEqual(agent["session"], "session-1" if agent["provider"] == "claude" else "thread-1")
        self.assertNotIn("a secret request", json.dumps(agent))

    def test_a_started_agent_reports_running_until_it_finishes(self):
        slow = {**self.environ, "FAKE_SLEEP": "3"}
        code, out, _ = self.run_cli("assess", "--task", "t", environ=slow)
        self.assertEqual(code, 0)
        self.assertIn("started: ", out)
        code, out, _ = self.run_cli("wait", "--timeout", "0.2")
        self.assertEqual(code, 3)
        code, out, _ = self.run_cli("result")
        self.assertEqual(code, 3)
        code, _, err = self.run_cli("lead")
        self.assertEqual(code, 2)
        self.assertIn("no accepted assessment yet", err)
        code, out, _ = self.run_cli("wait", "--timeout", "30")
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli("lead", "--wait", "30")
        self.assertEqual(code, 0)

    def test_a_failed_agent_is_reported_with_the_providers_words(self):
        failing = {**self.environ, "FAKE_FAIL": "codex"}
        code, out, _ = self.run_cli("assess", "--task", "t", "--provider", "codex", "--why", "test",
                                    "--wait", "30", environ=failing)
        self.assertEqual(code, 1)
        agent = orch.agents(self.run_directory())[0]
        self.assertEqual((agent["state"], agent["exit_code"]), ("failed", 7))
        self.assertIn("provider refused the model", agent["error"])
        self.assertIn("error=provider refused the model", out)
        code, out, _ = self.run_cli("result")
        self.assertEqual(code, 1)

    def test_a_claude_error_result_is_a_failure_despite_exit_zero(self):
        code, _, _ = self.run_cli("assess", "--task", "t", "--provider", "claude", "--why", "test",
                                  "--wait", "30", environ={**self.environ, "FAKE_CLAUDE_ERROR": "1"})
        self.assertEqual(code, 1)
        self.assertEqual(orch.agents(self.run_directory())[0]["state"], "failed")

    def test_a_cli_that_cannot_start_fails_instead_of_hanging(self):
        broken = {**self.environ, "SYMPHONY_CODEX_CLI": str(self.root / "missing-codex")}
        code, _, _ = self.run_cli("assess", "--task", "t", "--provider", "codex", "--why", "test",
                                  "--wait", "30", environ=broken)
        self.assertEqual(code, 1)
        agent = orch.agents(self.run_directory())[0]
        self.assertEqual(agent["state"], "failed")
        self.assertIn("could not start", agent["error"])

    def test_an_assessor_without_a_valid_line_blocks_the_lead_until_the_root_sizes_it(self):
        vague = {**self.environ, "FAKE_ASSESSMENT": "It looks medium to me."}
        code, out, _ = self.run_cli("assess", "--task", "t", "--wait", "30", environ=vague)
        self.assertEqual(code, 1)
        self.assertIn("no valid SYMPHONY_ASSESSMENT line", out)
        code, _, err = self.run_cli("lead", "--wait", "30")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("lead", "--size", "small", "--complexity", "simple", "--wait", "30")
        self.assertEqual(code, 2)
        self.assertIn("--why", err)
        code, _, _ = self.run_cli("lead", "--size", "small", "--complexity", "simple",
                                  "--why", "assessor gave no line", "--wait", "30")
        self.assertEqual(code, 0)
        assessment = orch.load_run(self.run_directory())["assessment"]
        self.assertEqual((assessment["source"], assessment["size"]), ("root", "small"))

    def test_agents_started_by_a_lead_cannot_start_more(self):
        self.run_cli("assess", "--task", "t", "--wait", "30")
        directory = self.run_directory()
        lead_environment = {**self.environ, "SYMPHONY_ORCHESTRATION_RUN": directory.name,
                            "SYMPHONY_ORCHESTRATION_DEPTH": "1", "SYMPHONY_ORCHESTRATION_PROJECT": str(self.project)}
        arguments = ("agent", "--role", "worker", "--size", "small", "--complexity", "simple", "--packet", "p")
        code, _, _ = self.run_cli(*arguments, "--wait", "30", environ=lead_environment, cwd=self.root)
        self.assertEqual(code, 0)
        worker = next(item for item in orch.agents(directory) if item["role"] == "worker")
        self.assertEqual(worker["depth"], 2)
        code, _, err = self.run_cli(*arguments, environ={**lead_environment, "SYMPHONY_ORCHESTRATION_DEPTH": "2"})
        self.assertEqual(code, 2)
        self.assertIn("cannot start Symphony agents", err)

    def test_a_run_refuses_more_agents_than_its_limit(self):
        capped = {**self.environ, "FAKE_SLEEP": "3", "SYMPHONY_ORCHESTRATION_MAX_PARALLEL": "1"}
        self.run_cli("assess", "--task", "t", environ=capped)
        code, _, err = self.run_cli("agent", "--role", "worker", "--size", "small", "--complexity", "simple",
                                    "--packet", "p", environ=capped)
        self.assertEqual(code, 2)
        self.assertIn("already running in this run (limit 1)", err)

    def test_stop_ends_a_running_agent(self):
        self.run_cli("assess", "--task", "t", environ={**self.environ, "FAKE_SLEEP": "60"})
        directory = self.run_directory()
        deadline = time.monotonic() + 20
        while orch.agents(directory)[0]["state"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        code, out, _ = self.run_cli("stop", "--all")
        self.assertEqual(code, 0)
        agent = orch.agents(directory)[0]
        self.assertEqual((agent["state"], agent["error"]), ("stopped", "stopped on request"))
        code, _, err = self.run_cli("stop")
        self.assertEqual(code, 2)

    def test_an_agent_past_its_limit_is_ended_and_says_so(self):
        code, _, _ = self.run_cli("assess", "--task", "t", "--timeout-min", "0.02", "--wait", "30",
                                  environ={**self.environ, "FAKE_SLEEP": "60"})
        self.assertEqual(code, 1)
        agent = orch.agents(self.run_directory())[0]
        self.assertEqual(agent["state"], "timeout")
        self.assertIn("minute limit", agent["error"])

    def test_a_supervisor_that_vanished_is_reported_lost(self):
        run = orch.new_run(self.project, self.environ, "t")
        directory = orch.run_dir(self.project, self.environ, run["run_id"])
        selection = orch.select("assessor", environ=self.environ)
        meta = orch.launch(self.project, self.environ, directory, role="assessor", selection=selection,
                           prompt="p", spawn=False)
        path = directory / "agents" / meta["id"] / "meta.json"
        stale = {**meta, "state": "running", "heartbeat_at": "2026-01-01T00:00:00+00:00"}
        path.write_text(json.dumps(stale))
        settled = orch.agent_meta(directory, meta["id"])
        self.assertEqual(settled["state"], "lost")
        self.assertEqual(json.loads(path.read_text())["state"], "lost")
        code, _, _ = self.run_cli("wait", "--timeout", "1")
        self.assertEqual(code, 1)

    def test_a_run_is_found_by_its_id_from_another_directory(self):
        self.run_cli("assess", "--task", "t", "--wait", "30")
        run_id = self.run_directory().name
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        code, out, _ = self.run_cli("status", "--run", run_id, cwd=elsewhere)
        self.assertEqual(code, 0)
        self.assertIn(run_id, out)
        code, _, err = self.run_cli("status", cwd=elsewhere)
        self.assertEqual(code, 2)
        self.assertIn("no orchestrated run found", err)
        code, _, err = self.run_cli("result", "../../etc/passwd", "--run", run_id)
        self.assertEqual(code, 2)
        self.assertIn("is not an agent id", err)

    def test_finished_runs_leave_after_retention(self):
        self.run_cli("assess", "--task", "old", "--wait", "30")
        old = self.run_directory()
        ancient = time.time() - (orch.RETENTION_DAYS + 1) * 86400
        os.utime(old, (ancient, ancient))
        self.run_cli("assess", "--task", "new", "--wait", "30")
        self.assertFalse(old.exists())
        self.assertTrue(self.run_directory().exists())

    def test_the_request_must_not_be_empty(self):
        code, _, err = self.run_cli("assess", "--task", "  ")
        self.assertEqual(code, 2)
        self.assertIn("the task is empty", err)
        code, _, err = self.run_cli("assess", "--task-file", str(self.root / "missing.md"))
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])

    def test_catalog_and_select_start_nothing(self):
        code, out, _ = self.run_cli("catalog")
        self.assertEqual(code, 0)
        self.assertIn(f"codex: profile {CODEX_BEST}", out)
        self.assertIn(f"claude: profile {CLAUDE_BEST}", out)
        code, out, _ = self.run_cli("select", "--role", "worker", "--size", "large", "--complexity", "simple", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["candidates"]), 2)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "state" / "orchestration").exists())


class ModeTests(unittest.TestCase):
    def test_each_host_has_its_own_switch_in_the_projects_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environ = {"SYMPHONY_STATE_DIR": str(root / "state")}
            project = root / "project"
            project.mkdir()
            self.assertFalse(orch.mode(project, environ))
            orch.set_mode(project, environ, True, "hermes")
            self.assertTrue(orch.mode(project, environ, "hermes"))
            self.assertFalse(orch.mode(project, environ, "enabled"))
            orch.set_mode(project, environ, True)
            orch.set_mode(project, environ, False, "hermes")
            self.assertTrue(orch.mode(project, environ))
            self.assertFalse(orch.mode(project, environ, "hermes"))
            out = io.StringIO()
            self.assertEqual(orchestrator_cli.main(["--cwd", str(project), "mode", "off"], environ, out), 0)
            self.assertEqual(out.getvalue(), "Symphony orchestrator mode: off\n")
            self.assertEqual(orch.with_mode({"orchestrator": {"enabled": True}, "kept": 1}, False), {"kept": 1})


if __name__ == "__main__":
    unittest.main()
