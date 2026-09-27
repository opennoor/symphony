import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("native_effort_smoke", Path(__file__).parents[1] / "scripts/native_effort_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class NativeEffortSmokeTests(unittest.TestCase):
    def test_native_evidence_and_bounded_shell_free_calls(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                def invoke(argv, **kwargs):
                    self.assertFalse(kwargs["shell"])
                    self.assertGreater(kwargs["timeout"], 0)
                    self.assertLessEqual(kwargs["timeout"], 90)
                    self.assertNotIn("env", kwargs)
                    self.assertTrue(Path(kwargs["cwd"]).is_dir())
                    if argv[-1] == "--version":
                        return subprocess.CompletedProcess(argv, 0, "native-version", "")
                    if provider == "codex":
                        model = argv[argv.index("--model") + 1]
                        return subprocess.CompletedProcess(argv, 0, "OK\n", f"model: {model}\nreasoning effort: ultra\n")
                    self.assertIn("symphony:symphony-assessor-claude-opus-5-5-max", argv)
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"modelUsage": {"claude-opus-5-5": {}},
                        "result": "SYMPHONY_ASSESSMENT: {}", "is_error": False}), "")
                with patch.object(smoke.shutil, "which", return_value=provider), patch.object(smoke.subprocess, "run", side_effect=invoke):
                    report = smoke.check(provider, Path(__file__).parents[1])
                self.assertEqual(report["cli_version"], "native-version")
                self.assertEqual(len(report["accepted"]), 2 if provider == "codex" else 1)

    def test_missing_native_banner_fails(self):
        with patch.object(smoke.shutil, "which", return_value="codex"), patch.object(smoke.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, "OK", "")):
            with self.assertRaisesRegex(RuntimeError, "not reported"):
                smoke.check("codex", Path(__file__).parents[1])
