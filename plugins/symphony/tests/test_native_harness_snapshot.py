"""The native upgrade receipt accepts only the reviewed retained candidate."""

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from plugins.symphony.symphony.store import _locked


REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "symphony"
HARNESS = REPO / ".github" / "scripts" / "native_managed_concurrency.py"
SPEC = importlib.util.spec_from_file_location("native_managed_concurrency", HARNESS)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class CandidateRetainedProfileTests(unittest.TestCase):
    def test_codex_native_hook_gate_holds_second_distinct_default_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "capture.py"
            script.write_text(native.CODEX_HOOK_CAPTURE)
            gate, capture = root / "gate", root / "capture"
            first = subprocess.run(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "codex"],
                input=json.dumps({"session_id": "root-a", "agent_id": "assessor-a",
                                  "agent_type": "default"}),
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(first.returncode, 0)
            self.assertFalse((gate / "a.ready").exists())
            self.assertEqual(json.loads((gate / "a.first.json").read_text())["agent_id"],
                             "assessor-a")
            process = subprocess.Popen(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "codex"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"})
            try:
                process.stdin.write(json.dumps({
                    "session_id": "root-a", "agent_id": "lead-a",
                    "agent_type": "default"}))
                process.stdin.close()
                deadline = time.monotonic() + 5
                while not (gate / "a.ready").is_file():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                self.assertIsNone(process.poll())
                self.assertEqual(json.loads((gate / "a.ready").read_text())["agent_id"], "lead-a")
                (gate / "release").touch()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                (gate / "release").touch()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()

    def test_native_hook_gate_holds_distinct_leads_until_external_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "capture.py"
            script.write_text(native.CODEX_HOOK_CAPTURE)
            gate, capture = root / "gate", root / "capture"
            assessor = subprocess.run(
                [sys.executable, "-I", str(script), "SubagentStart", str(capture), "claude"],
                input=json.dumps({"session_id": "root-a", "agent_id": "assessor",
                                  "agent_type": "symphony:symphony-assessor-claude-sonnet-5-high"}),
                env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                     "SYMPHONY_NATIVE_GATE_LABEL": "a"},
                text=True, capture_output=True, timeout=5)
            self.assertEqual(assessor.returncode, 0)
            self.assertFalse(gate.exists())
            processes = {}
            try:
                for label in ("a", "b"):
                    processes[label] = subprocess.Popen(
                        [sys.executable, "-I", str(script), "SubagentStart", str(capture), "claude"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True,
                        env={**os.environ, "SYMPHONY_NATIVE_GATE_DIR": str(gate),
                             "SYMPHONY_NATIVE_GATE_LABEL": label})
                    processes[label].stdin.write(json.dumps({
                        "session_id": f"root-{label}", "agent_id": f"lead-{label}",
                        "agent_type": "symphony:symphony-lead-claude-sonnet-5-low"}))
                    processes[label].stdin.close()
                deadline = time.monotonic() + 5
                while not all((gate / f"{label}.ready").is_file() for label in processes):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                for label, process in processes.items():
                    self.assertIsNone(process.poll())
                    self.assertEqual(json.loads((gate / f"{label}.ready").read_text())
                                     ["session_id"], f"root-{label}")
                (gate / "release").touch()
                for process in processes.values():
                    self.assertEqual(process.wait(timeout=5), 0)
            finally:
                (gate / "release").touch()
                for process in processes.values():
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    process.stderr.close()

    def test_claude_second_wake_requires_original_reconciled_run_and_empty_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            run = {"run_id": "original", "lead_identity": "lead-1", "status": "completing",
                   "outcome": {"status": "completed"},
                   "delegations": [{"role": "lead", "identity": "lead-1", "state": "completed"}]}
            document = {"active_runs": {"claude:root-a": run}}
            ready = lambda doc: native.claude_finalization_ready(
                doc, "root-a", "original", "lead-1", state_dir)
            self.assertTrue(ready(document))
            self.assertFalse(ready({"active_runs": {"claude:root-a":
                                    {**run, "status": "active"}}}))
            self.assertFalse(ready({"active_runs": {"claude:root-a":
                                    {**run, "delegations": [{"role": "lead", "identity": "lead-1",
                                                           "state": "working"}]}}}))
            pending = state_dir / ".session-child.json"
            pending.write_text(json.dumps({"pending": [{"kind": "subagent_stopped"}],
                                           "overflow": False}))
            self.assertFalse(ready(document))
            pending.write_text(json.dumps({"pending": [], "overflow": False}))
            self.assertTrue(ready(document))
            for changed in ({**run, "run_id": "new-run"},
                            {**run, "lead_identity": "new-lead"},
                            {**run, "delegations": [*run["delegations"],
                                                      {"role": "lead", "identity": "replacement",
                                                       "state": "completed"}]}):
                with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                    ready({"active_runs": {"claude:root-a": changed}})

    def test_failure_keeps_full_artifact_without_flooding_ci_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = {"provider": "claude", "cases": [{"native_trace": "x" * 100000}]}
            stderr = io.StringIO()
            with patch.object(native, "prepare_baseline_capture",
                              side_effect=RuntimeError("native case failed")), \
                 patch.object(native, "failure_state", return_value=report), \
                 patch.object(sys, "argv", ["native_managed_concurrency.py", "--provider", "claude"]), \
                 patch.object(sys, "stderr", stderr), \
                 patch.dict(native.os.environ, {"SYMPHONY_NATIVE_DIAGNOSTICS_DIR": temporary}):
                self.assertEqual(native.main(), 1)
            saved = json.loads((Path(temporary) / "native-managed-claude-failure.json").read_text())
            self.assertEqual(len(saved["cases"][0]["native_trace"]), 100000)
            self.assertLess(len(stderr.getvalue()), 1000)

    def test_success_keeps_full_hook_trace_in_artifact_without_flooding_ci_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = {"case": "same-worktree", "completed": ["a", "b"],
                      "native_hook_capture": {"records": ["x" * 100000]}}
            stdout = io.StringIO()
            with patch.object(native, "prepare_baseline_capture", return_value={}), \
                 patch.object(native, "check_case", return_value=result), \
                 patch.object(sys, "argv", ["native_managed_concurrency.py", "--provider", "claude"]), \
                 patch.object(sys, "stdout", stdout), \
                 patch.dict(native.os.environ, {"SYMPHONY_NATIVE_DIAGNOSTICS_DIR": temporary}):
                self.assertEqual(native.main(), 0)
            saved = json.loads((Path(temporary) / "native-managed-claude-receipt.json").read_text())
            self.assertEqual(len(saved["native_managed"][0]["native_hook_capture"]["records"][0]),
                             100000)
            self.assertEqual(json.loads(stdout.getvalue())["native_managed"][0]["completed"],
                             ["a", "b"])
            self.assertLess(len(stdout.getvalue()), 1000)

    def test_opaque_transport_cannot_replace_durable_recovery_events(self):
        def document(kinds, identity="original"):
            return {"event_history": [{"kind": kind, "payload": {"identity": identity}}
                                      for kind in kinds]}
        native.require_recovered_lead_events(document(["lead_failed", "lead_completed"]), "original")
        native.require_recovered_lead_events(
            document(["lead_failed", "lead_failed", "lead_completed", "lead_completed"]), "original")
        for kinds in ([], ["lead_completed"], ["lead_failed"], ["lead_completed", "lead_failed"]):
            with self.subTest(kinds=kinds), self.assertRaises(RuntimeError):
                native.require_recovered_lead_events(document(kinds), "original")
        with self.assertRaises(RuntimeError):
            native.require_recovered_lead_events(document(["lead_failed", "lead_completed"], "replacement"),
                                                  "original")

    def test_recovery_events_required_only_for_codex_update_original_lead(self):
        plain_completion = {"event_history": [
            {"kind": "lead_completed", "payload": {"identity": "original"}}]}
        for provider, update, label in (("codex", None, "a"),
                                        ("codex", {"old_version": "1.5.1"}, "b"),
                                        ("claude", {"old_version": "1.5.1"}, "a")):
            with self.subTest(provider=provider, update=bool(update), label=label):
                native.require_case_recovery_events(
                    plain_completion, "original", provider, update, label)
        with self.assertRaisesRegex(RuntimeError, "ordered durable failed and completed"):
            native.require_case_recovery_events(
                plain_completion, "original", "codex", {"old_version": "1.5.1"}, "a")
        native.require_case_recovery_events(
            {"event_history": [
                {"kind": "lead_failed", "payload": {"identity": "original"}},
                {"kind": "lead_completed", "payload": {"identity": "original"}}]},
            "original", "codex", {"old_version": "1.5.1"}, "a")

    def test_native_launch_pins_current_provider_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            for provider, profile in (("codex", "base"), ("claude", "sonnet-5-5")):
                root = Path(temporary) / provider
                project = root / "project"
                project.mkdir(parents=True)
                with self.subTest(provider=provider), \
                        patch.object(native, "projects", return_value=(project, project)), \
                        patch.object(native, "launch", side_effect=RuntimeError("fixture launch boundary")) as launch:
                    with self.assertRaisesRegex(RuntimeError, "fixture launch boundary"):
                        native.check_case(provider, root, False, 1, 3)
                    self.assertEqual(profile, launch.call_args.args[4]["SYMPHONY_PROFILE"])

    def test_codex_child_packet_contains_first_turn_outcome_without_root_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for recover, status in ((True, "blocked"), (False, "completed")):
                with self.subTest(recover=recover), \
                        patch.object(native.shutil, "which", return_value="codex"), \
                        patch.object(native.subprocess, "Popen") as popen:
                    native.launch("codex", project, "a", recover, {}, project, 3, "root")
                    command = popen.call_args.args[0]
                    self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
                    self.assertNotIn("-c", command)
                    root_prompt = command[-1]
                    packet_line = re.search(r"^LEAD_SPAWN_PACKET: (.+)$", root_prompt, re.MULTILINE)
                    self.assertIsNotNone(packet_line, "lead instructions must be an explicit relay packet")
                    packet = json.loads(packet_line.group(1))
                    self.assertIn("Pass LEAD_SPAWN_PACKET verbatim as spawn_agent arguments", root_prompt)
                    self.assertIn("Never replace the lead or rerun the gate", root_prompt)
                    child_message = packet["message"]
                    self.assertIn('SYMPHONY_ROLE: lead\n', child_message)
                    self.assertIn(f'SYMPHONY_OUTCOME: {{"status":"{status}"}}', child_message)
                    self.assertLess(child_message.index(f'SYMPHONY_OUTCOME: {{"status":"{status}"}}'),
                                    child_message.index("native SubagentStart hook"))
                    self.assertNotIn("report success", child_message)
                    if recover:
                        self.assertIn("deliberately blocked", child_message)
                        self.assertIn("even after the native hook releases", child_message)
                    self.assertNotIn('SYMPHONY_OUTCOME: {"status":"completed"}' if recover
                                     else 'SYMPHONY_OUTCOME: {"status":"blocked"}', child_message)
                    self.assertIn("native SubagentStart hook holds your first turn", child_message)
                    self.assertIn("GATE_RELEASED\nSYMPHONY_OUTCOME", child_message)
                    self.assertNotIn("gate.py", child_message)
                    self.assertEqual("none", packet["fork_turns"])
                    self.assertEqual(("gpt-6-luna", "low"),
                                     (packet["model"], packet["reasoning_effort"]))
                    sessions = project / "sessions"
                    sessions.mkdir(exist_ok=True)
                    for message, expected in (
                        ("gAAAAA_fixture_opaque_not_a_real_token", True),
                        (child_message, True),
                        ("", False),
                        ({"text": child_message}, False),
                    ):
                        # Root instructions alone cannot satisfy child relay acceptance.
                        records = [
                            {"type": "response_item", "payload": {"type": "message",
                             "role": "user", "content": root_prompt}},
                            {"type": "response_item", "payload": {"type": "function_call",
                             "name": "spawn_agent", "call_id": "spawn-lead",
                             "arguments": json.dumps({**packet, "message": message})}},
                            {"type": "event_msg", "payload": {"type": "item_completed",
                             "item": {"type": "SubAgentActivity", "id": "spawn-lead",
                                      "kind": "started", "agent_thread_id": "lead"}}},
                        ]
                        (sessions / "root.jsonl").write_text(
                            "\n".join(json.dumps(record) for record in records))
                        trace = native.codex_host_trace(project, "root", "lead", project / "a.errors")
                        self.assertTrue(trace["spawn_calls"][0]["same_lead_id"])
                        self.assertEqual(expected, trace["spawn_calls"][0]["lead_packet_metadata_matches"])
                        spawn = trace["spawn_calls"][0]
                        self.assertEqual(sorted(packet), spawn["argument_keys"])
                        shape = spawn["task_text_fields"]["message"]
                        self.assertEqual("str" if isinstance(message, str) else "dict", shape["type"])
                        self.assertEqual(len(message) if isinstance(message, str) else None, shape["chars"])
                        self.assertNotIn("SYMPHONY_ROLE", json.dumps(shape))
                        self.assertNotIn("hash", shape)

    def test_codex_resume_keeps_recovery_on_original_lead(self):
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            session = "11111111-1111-1111-1111-111111111111"
            lead = "22222222-2222-2222-2222-222222222222"
            task = "original_lead_custom_name"

            def launch(command, **kwargs):
                kwargs["stderr"].write(f"session id: {session}\n")
                return Mock(returncode=0, poll=Mock(return_value=0))

            with patch.object(native.shutil, "which", return_value="codex"), \
                    patch.object(native.subprocess, "Popen", side_effect=launch) as popen:
                native.resume_codex({}, logs, session, logs, time.monotonic() + 30,
                                    lead_id=lead, lead_task_name=task)
            command = popen.call_args.args[0]
            self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
            self.assertNotIn("-c", command)
            self.assertEqual(session, command[-2])
            prompt = command[-1]
            self.assertIn(lead, prompt)
            self.assertIn(f"target {task!r}", prompt)
            self.assertIn("Do not spawn a replacement", prompt)
            self.assertIn("If gate markers or exit code are missing", prompt)
            self.assertIn("followup_task", prompt)
            self.assertIn("Do not rerun the gate", prompt)
            self.assertIn("native Stop hook", prompt)
            self.assertTrue(json.loads((logs / "a.resume.status.json").read_text())["same_session"])

            # Status-only and exact Stop probes must not become recovery requests.
            with patch.object(native.shutil, "which", return_value="codex"), \
                    patch.object(native.subprocess, "Popen", side_effect=launch) as popen:
                for options in ({"already_completed": True}, {"direct_stop": True}):
                    native.resume_codex({}, logs, session, logs, time.monotonic() + 30,
                                        lead_id=lead, lead_task_name=task, **options)
                    command = popen.call_args.args[0]
                    self.assertEqual("gpt-6-luna", command[command.index("--model") + 1])
                    self.assertNotIn("-c", command)
                    prompt = command[-1]
                    if options.get("already_completed"):
                        self.assertIn("Do not start or stop work, delegate", prompt)
                    else:
                        self.assertEqual("$symphony:symphony stop", prompt)

    def test_native_capture_recognizes_disposable_baseline_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "claude-baseline-home"
            home.mkdir()
            capture = root / "claude-hook-capture"
            capture.mkdir()
            invocation = "capture-one"
            (capture / f"{invocation}-entry.json").write_text(json.dumps({
                "invocation_id": invocation, "event": "Stop", "session_id": "root-a",
                "agent_id": None, "native_home": str(home), "started_ns": 123,
                "prompt_id_hash": "prompt-hash", "payload_keys": ["session_id"],
            }))
            (capture / f"{invocation}-exit.json").write_text(json.dumps({
                "invocation_id": invocation, "exit_code": 0, "finished_ns": 125,
            }))
            summary = native.codex_hook_capture_summary(root, "claude")
            self.assertTrue(summary["configured"])
            self.assertEqual(1, summary["event_counts"]["Stop"])
            self.assertTrue(summary["records"][0]["native_home_matches_expected"])
            self.assertEqual("prompt-hash", summary["records"][0]["prompt_id_hash"])

    def test_claude_trace_joins_root_agent_call_and_new_child_turn_without_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            session = "11111111-1111-1111-1111-111111111111"
            lead = "a1234567890123456"
            root = home / "projects" / "project" / session
            child = root / "subagents" / f"agent-{lead}.jsonl"
            child.parent.mkdir(parents=True)
            agent_type = "symphony:symphony-lead-claude-sonnet-5-5-low"
            (child.with_suffix(".meta.json")).write_text(json.dumps({
                "agentType": agent_type, "toolUseId": "first-tool"}))
            parent_rows = [
                {"type": "user", "uuid": "root-prompt-one", "timestamp": "2026-09-30T00:00:00Z",
                 "message": {"content": "private root instructions"}},
                {"type": "assistant", "uuid": "assistant-one", "timestamp": "2026-09-30T00:00:01Z",
                 "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "first-tool",
                                          "input": {"subagent_type": agent_type,
                                                    "prompt": "private lead task"}}]}},
                {"type": "user", "uuid": "tool-result", "timestamp": "2026-09-30T00:00:02Z",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "first-tool",
                                          "content": "private result"}]}},
                {"type": "user", "uuid": "root-prompt-two", "timestamp": "2026-09-30T00:00:03Z",
                 "message": {"content": "private second instructions"}},
                {"type": "assistant", "uuid": "assistant-two", "timestamp": "2026-09-30T00:00:04Z",
                 "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "second-tool",
                                          "input": {"subagent_type": agent_type,
                                                    "resume": lead,
                                                    "prompt": "private follow-up"}}]}},
                {"type": "assistant", "uuid": "assistant-three", "timestamp": "2026-09-30T00:00:05Z",
                 "message": {"content": [{"type": "tool_use", "name": "SendMessage",
                                          "id": "message-tool", "input": {"to": lead,
                                          "message": "private recovery instructions"}}]}},
            ]
            child_rows = [
                {"type": "user", "uuid": "child-prompt-one", "timestamp": "2026-09-30T00:00:01Z",
                 "message": {"content": "private first child task"}},
                {"type": "assistant", "uuid": "child-terminal-one",
                 "timestamp": "2026-09-30T00:00:02Z",
                 "message": {"stop_reason": "end_turn", "content": [
                     {"type": "text", "text": 'SYMPHONY_OUTCOME: {"status":"completed"}'}]}},
                {"type": "user", "uuid": "child-prompt-two", "timestamp": "2026-09-30T00:00:04Z",
                 "message": {"content": "private new child task"}},
                {"type": "assistant", "uuid": "child-terminal-two",
                 "timestamp": "2026-09-30T00:00:05Z",
                 "message": {"stop_reason": "end_turn", "content": [
                     {"type": "text", "text": "private markerless result"}]}},
            ]
            root.with_suffix(".jsonl").write_text("\n".join(map(json.dumps, parent_rows)) + "\n")
            child.write_text("\n".join(map(json.dumps, child_rows)) + "\n")
            trace = native.claude_host_trace(home, session, lead)
            self.assertEqual(2, len(trace["root_agent_calls"]))
            self.assertEqual(2, len(trace["child_turns"]))
            self.assertTrue(trace["root_agent_calls"][0]["matches_lead_meta"])
            self.assertFalse(trace["root_agent_calls"][1]["matches_lead_meta"])
            self.assertNotEqual(trace["root_agent_calls"][0]["root_prompt_id_hash"],
                                trace["root_agent_calls"][1]["root_prompt_id_hash"])
            self.assertIsNotNone(trace["root_agent_calls"][1]["resume_id_hash"])
            self.assertEqual(1, len(trace["root_message_calls"]))
            self.assertTrue(trace["root_message_calls"][0]["to_matches_original_lead"])
            self.assertEqual("completed", trace["child_turns"][0]["marker"])
            self.assertIsNone(trace["child_turns"][1]["marker"])
            self.assertNotIn("private", json.dumps(trace))
            for malformed, shape in (("[]", "list"), ("{}", "dict")):
                with self.subTest(malformed=malformed):
                    child_rows[-1]["message"]["content"][0]["text"] = (
                        'SYMPHONY_OUTCOME: {"status":' + malformed + "}")
                    child.write_text("\n".join(map(json.dumps, child_rows)) + "\n")
                    invalid = native.claude_host_trace(home, session, lead)
                    self.assertEqual("invalid", invalid["child_turns"][1]["marker"])
                    self.assertEqual(shape, invalid["child_turns"][1]["marker_status_shape"])

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
