import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from plugins.symphony.scripts.package_smoke import _payload, run_smoke
from plugins.symphony.symphony.host_evidence import _packet_purpose


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "plugins" / "symphony" / "scripts" / "package_smoke.py"


class PackageSmokeTests(unittest.TestCase):
    def test_child_spawn_packets_declare_substantive_purpose(self):
        plugin = ROOT / "plugins" / "symphony"
        for event in ("PreToolUse", "PostToolUse"):
            for role in ("worker", "consultant", "lead", "assessor"):
                with self.subTest(event=event, role=role):
                    packet = _payload(plugin, "claude", event, ROOT, "session", role)["tool_input"]["prompt"]
                    self.assertTrue(packet.startswith(f"SYMPHONY_ROLE: {role}\n"))
                    self.assertEqual(_packet_purpose(packet),
                                     "substantive" if role in {"worker", "consultant"} else "")

    def make_candidate(
        self,
        root: Path,
        *,
        broken: bool = False,
        absolute: bool = False,
        heartbeat: bool = True,
    ) -> Path:
        plugin = root / "plugins" / "symphony"
        for relative in (".codex-plugin", ".claude-plugin", "hooks", "scripts"):
            (plugin / relative).mkdir(parents=True, exist_ok=True)

        manifest = {"name": "symphony", "version": "1.0.0"}
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({**manifest, "hooks": "./hooks/codex.json"})
        )
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))

        command = 'python3 "${PLUGIN_ROOT}/scripts/symphony_hook.py"'
        if absolute:
            command = 'python3 "/tmp/plugin-cache/1.0.0/scripts/symphony_hook.py"'
        codex = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": command}]}]
                for event in (
                    "SessionStart",
                    "UserPromptSubmit",
                    "SubagentStart",
                    "SubagentStop",
                    "Stop",
                    "Interrupt",
                )
            }
        }
        claude_command = command.replace("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}")
        claude = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": claude_command}]}]
                for event in (
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "SubagentStart",
                    "SubagentStop",
                    "PostToolUse",
                    "Stop",
                )
            }
        }
        (plugin / "hooks" / "codex.json").write_text(json.dumps(codex))
        (plugin / "hooks" / "hooks.json").write_text(json.dumps(claude))

        if not broken:
            (plugin / "scripts" / "symphony_hook.py").write_text(
                textwrap.dedent(
                    """\
                    import json, os, pathlib, sys
                    payload = json.load(sys.stdin)
                    root = pathlib.Path(os.environ["SYMPHONY_STATE_DIR"])
                    root.mkdir(parents=True, exist_ok=True)
                    path = root / "fake-state.json"
                    state = json.loads(path.read_text()) if path.exists() else {"events": []}
                    state["events"].append(payload["hook_event_name"])
                    event = payload["hook_event_name"]
                    if event == "SubagentStart":
                        state.setdefault("active_run", {"objective": "assessor spawn"})
                        state["active_children"] = [payload["agent_id"]]
                        state["delegations"] = {payload["agent_id"]: "working"}
                    elif event == "SubagentStop":
                        state["active_children"] = []
                        if payload.get("agent_id") == "fake-lead":
                            state["active_run"] = None
                    elif event == "SessionStart" and state.get("active_run"):
                        state["delegations"] = {
                            key: "interrupted" for key in state.get("delegations", {})
                        }
                        state["active_children"] = []
                    if __HEARTBEAT__:
                        provider = os.environ["SYMPHONY_SMOKE_PROVIDER"]
                        state.setdefault("activation", {})[provider] = {
                            "state": "guarded",
                            "session_id": payload["session_id"],
                            "plugin_version": os.environ["SYMPHONY_PLUGIN_VERSION"],
                            "plugin_root": os.environ["SYMPHONY_PLUGIN_ROOT"],
                            "hook_schema_version": 1,
                            "observed_at": "2026-09-17T00:00:00+00:00",
                        }
                    path.write_text(json.dumps(state))
                    if event == "PreToolUse" and "SYMPHONY_ROLE" not in json.dumps(
                        payload.get("tool_input", {})
                    ):
                        print(json.dumps({"hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "Add exactly one SYMPHONY_ROLE line.",
                        }}))
                        raise SystemExit(0)
                    blocked = (
                        event == "Stop"
                        and not payload.get("stop_hook_active")
                        and (state.get("active_children") or state.get("active_run"))
                    )
                    print(json.dumps({"continue": not bool(blocked)}))
                    """
                ).replace("__HEARTBEAT__", repr(heartbeat))
            )
        (plugin / "profiles.json").write_text(json.dumps({
            "providers": {
                provider: {"profiles": [{
                    "id": "full",
                    "tiers": {"strongest": f"{provider}-strong"},
                    "matrix": {"small/simple": {"model": f"{provider}-lead", "effort": "medium"}},
                }]}
                for provider in ("codex", "claude")
            }
        }))
        return root

    def test_activation_materializes_and_executes_candidate_after_trust(self):
        """Catches validating the source tree without running the installed copy."""
        with tempfile.TemporaryDirectory() as candidate_dir, tempfile.TemporaryDirectory() as home_dir:
            candidate = self.make_candidate(Path(candidate_dir))
            result = run_smoke("codex", candidate, "activation", Path(home_dir))

            self.assertTrue(result["ok"])
            self.assertEqual(["needs_review", "guarded"], result["activation"])
            self.assertEqual(["UserPromptSubmit"], result["events"])
            install_root = Path(result["install_root"])
            self.assertTrue((install_root / "scripts" / "symphony_hook.py").is_file())
            self.assertNotEqual(candidate / "plugins" / "symphony", install_root)

    def test_all_scenarios_run_for_both_provider_protocols(self):
        """Catches a scenario silently accepting a provider whose lifecycle was not exercised."""
        managed = [
            "UserPromptSubmit",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "SubagentStart",
            "Stop",
            "Stop",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "SubagentStop",
            "Stop",
        ]
        # Only Claude reports a spawn before launch, and only Claude can carry
        # deferred guidance back on a post-tool event.
        managed_claude = managed[:2] + ["PreToolUse"] + managed[2:4] + ["PostToolUse"] + managed[4:]
        expected = {
            "codex": {
                "managed-run": managed,
                "interrupt-resume": [
                    "UserPromptSubmit",
                    "SubagentStart",
                    "Interrupt",
                    "SessionStart",
                ],
                "unmarked-spawn": ["UserPromptSubmit"],
            },
            "claude": {
                "managed-run": managed_claude,
                "interrupt-resume": ["UserPromptSubmit", "SubagentStart", "SessionStart"],
                "unmarked-spawn": ["UserPromptSubmit", "PreToolUse"],
            },
        }
        with tempfile.TemporaryDirectory() as candidate_dir:
            candidate = self.make_candidate(Path(candidate_dir))
            for provider, scenarios in expected.items():
                for scenario, sequence in scenarios.items():
                    with self.subTest(provider=provider, scenario=scenario), tempfile.TemporaryDirectory() as home:
                        result = run_smoke(provider, candidate, scenario, Path(home))
                        self.assertTrue(result["ok"], result.get("error"))
                        self.assertEqual(sequence, result["events"])

    def test_upgrade_reloads_new_materialized_version(self):
        """Catches reusing a removed versioned cache path after an upgrade."""
        with tempfile.TemporaryDirectory() as candidate_dir:
            candidate = self.make_candidate(Path(candidate_dir))
            for provider in ("codex", "claude"):
                with self.subTest(provider=provider), tempfile.TemporaryDirectory() as home_dir:
                    result = run_smoke(provider, candidate, "upgrade", Path(home_dir))

                    self.assertTrue(result["ok"], result.get("error"))
                    self.assertEqual(["1.0.0", "1.0.1"], result["heartbeat_versions"])
                    self.assertNotEqual(*result["loaded_roots"])
                    self.assertFalse(Path(result["loaded_roots"][0]).exists())
                    self.assertTrue(result["loaded_roots"][1].endswith("/1.0.1"))
                    self.assertNotEqual(0, result["stale_hook_exit"])

    def test_missing_hook_executable_is_a_fault(self):
        """Catches reporting trust-pending when the packaged command target is absent."""
        with tempfile.TemporaryDirectory() as candidate_dir, tempfile.TemporaryDirectory() as home_dir:
            candidate = self.make_candidate(Path(candidate_dir), broken=True)
            result = run_smoke("codex", candidate, "activation", Path(home_dir))

            self.assertFalse(result["ok"])
            self.assertEqual("faulted", result["activation"][-1])
            self.assertIn("missing hook executable", result["error"])

    def test_activation_rejects_unrelated_json_without_a_guarded_heartbeat(self):
        with tempfile.TemporaryDirectory() as candidate_dir, tempfile.TemporaryDirectory() as home_dir:
            candidate = self.make_candidate(Path(candidate_dir), heartbeat=False)
            result = run_smoke("codex", candidate, "activation", Path(home_dir))

            self.assertFalse(result["ok"])
            self.assertIn("complete guarded heartbeat", result["error"])

    def test_absolute_cache_command_is_rejected(self):
        """Catches publishing hooks pinned to the developer's cache path."""
        with tempfile.TemporaryDirectory() as candidate_dir, tempfile.TemporaryDirectory() as home_dir:
            candidate = self.make_candidate(Path(candidate_dir), absolute=True)
            result = run_smoke("codex", candidate, "activation", Path(home_dir))

            self.assertFalse(result["ok"])
            self.assertIn("plugin-root placeholder", result["error"])

    def test_cli_emits_one_json_result(self):
        """Catches human log lines corrupting the smoke artifact consumed by CI."""
        with tempfile.TemporaryDirectory() as candidate_dir:
            candidate = self.make_candidate(Path(candidate_dir))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--provider",
                    "claude",
                    "--candidate",
                    str(candidate),
                    "--scenario",
                    "activation",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])
            self.assertEqual("", completed.stderr)


if __name__ == "__main__":
    unittest.main()
