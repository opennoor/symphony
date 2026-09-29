"""The native upgrade receipt accepts only the reviewed retained candidate."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from plugins.symphony.symphony.store import _locked


REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "symphony"
HARNESS = REPO / ".github" / "scripts" / "native_managed_concurrency.py"
SPEC = importlib.util.spec_from_file_location("native_managed_concurrency", HARNESS)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class CandidateRetainedProfileTests(unittest.TestCase):
    def test_source_and_exact_retained_profiles_and_rejections(self):
        files, digest, constants = native.reviewed_snapshot_spec(PLUGIN)
        version = constants["PLUGIN_VERSION"]
        schema = constants["HOOK_SCHEMA_VERSION"]
        with tempfile.TemporaryDirectory() as temporary:
            runtime_base = Path(temporary) / "retained runtimes"
            snapshot = runtime_base / digest
            for relative in files:
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PLUGIN / relative, destination)
            source = {"session_id": "root-a", "plugin_version": version,
                      "plugin_root": str(PLUGIN), "runtime_root": str(snapshot),
                      "hook_schema_version": schema, "observed_at": "now"}
            retained = {**source, "plugin_root": str(snapshot), "runtime_root": None}
            direct_source = {**source, "runtime_root": None}
            accepts = lambda record: native.candidate_retained_profile(
                (record,), "root-a", version, PLUGIN, runtime_base)

            self.assertTrue(accepts(source))
            self.assertTrue(accepts(direct_source))
            self.assertTrue(accepts(retained))
            for changed in ({**retained, "session_id": "foreign"},
                            {**retained, "plugin_version": "1.5.1"},
                            {**retained, "hook_schema_version": schema + 1},
                            {**retained, "observed_at": ""},
                            {**retained, "plugin_root": str(runtime_base / ("0" * 64))},
                            {**source, "runtime_root": str(runtime_base / ("0" * 64))}):
                self.assertFalse(accepts(changed), changed)

            changed_file = snapshot / files[0]
            original = changed_file.read_bytes()
            changed_file.write_bytes(original + b"changed")
            self.assertFalse(accepts(retained))
            changed_file.write_bytes(original)
            extra = snapshot / "extra.txt"
            extra.write_text("extra")
            self.assertFalse(accepts(retained))
            extra.unlink()
            self.assertTrue(accepts(retained))

    def test_native_stop_commit_can_follow_parent_exit_without_false_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            active = {"recent_runs": [], "active_runs": {"codex:root-a": {"run_id": "run-a"}}}
            completed = {"recent_runs": [{"provider": "codex", "session_id": "root-a",
                                          "run_id": "run-a", "status": "completed",
                                          "outcome": {"status": "completed"}}], "active_runs": {}}
            path.write_text(json.dumps(active))

            def commit():
                replacement = path.with_suffix(".replacement")
                replacement.write_text(json.dumps(completed))
                with _locked(path):
                    replacement.replace(path)

            delayed = threading.Timer(.05, commit)
            delayed.start()
            try:
                observed = native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + 1)
            finally:
                delayed.join()
            self.assertEqual(observed["a"]["recent_runs"][0]["status"], "completed")

            inbox = path.parent / ".session-child.json"
            inbox.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                         "overflow": False}))
            def acknowledge():
                replacement = inbox.with_suffix(".replacement")
                replacement.write_text(json.dumps({"pending": [], "overflow": False}))
                with _locked(inbox.with_suffix("")):
                    replacement.replace(inbox)

            delayed_ack = threading.Timer(.05, acknowledge)
            delayed_ack.start()
            started = time.monotonic()
            try:
                native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + 1)
            finally:
                delayed_ack.join()
            self.assertGreaterEqual(time.monotonic() - started, .05)

            inbox.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                         "overflow": False}))
            with self.assertRaisesRegex(RuntimeError, "unacknowledged"):
                native.wait_for_completed_docs(
                    {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                    time.monotonic() + .15)
            inbox.unlink()

            path.write_text(json.dumps(active))
            unresolved = native.wait_for_completed_docs(
                {"a": path}, "codex", {"a": "root-a"}, {"a": "run-a"},
                time.monotonic() + .15)
            self.assertEqual(unresolved["a"]["recent_runs"], [])


if __name__ == "__main__":
    unittest.main()
