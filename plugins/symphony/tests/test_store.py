import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from plugins.symphony.symphony.model import Delegation, Event, ProjectState, RunState
from plugins.symphony.symphony.store import StateStore, _locked, _read_owner_snapshot, project_key


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data = self.root / "data"
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = StateStore(self.data)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def state_path(self) -> Path:
        return self.data / f"{project_key(self.project)}.v2.json"

    def legacy_path(self) -> Path:
        return self.data / f"{project_key(self.project)}.json"

    def test_native_escalation_receipt_roundtrip_and_strict_optional_boolean(self):
        from plugins.symphony.symphony.store import _receipt_from_dict
        receipt = {key: key for key in ('provider', 'session', 'agent', 'run_id',
                                       'turn', 'result', 'parent', 'lead')}
        legacy = _receipt_from_dict(receipt)
        self.assertNotIn('native_fast_escalation', legacy)
        self.assertNotIn('native_followup_start_id', legacy)
        for start in ('', 'a' * 64 + ':followup-start'):
            self.assertEqual(_receipt_from_dict({**receipt, 'native_followup_start_id': start})[
                'native_followup_start_id'], start)
        for malformed in (None, False, 1, [], {}):
            with self.assertRaises(ValueError):
                _receipt_from_dict({**receipt, 'native_followup_start_id': malformed})
        for flag in (False, True):
            self.store.save(self.project, ProjectState(terminal_receipts=(
                {**receipt, 'native_fast_escalation': flag},)))
            self.assertIs(self.store.load(self.project).terminal_receipts[0]['native_fast_escalation'], flag)
        for malformed in (None, 1, 0, 'true', {}, []):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                _receipt_from_dict({**receipt, 'native_fast_escalation': malformed})

    def test_round_trips_the_complete_domain_model_as_json(self):
        event = Event("event-1", "task_received", "2026-09-17T10:00:00+00:00", {"task": "ship"})
        delegation = Delegation(
            identity="agent-1",
            role="lead",
            objective="Ship the task",
            state="working",
            requested_tier="capable",
            requested_effort="high",
            updated_at="2026-09-17T10:01:00+00:00",
        )
        run = RunState(
            run_id="run-1",
            task="Ship the task",
            status="active",
            owner_generation=2,
            lead_identity="agent-1",
            assessment={"size": "medium", "complexity": "mixed"},
            delegations=(delegation,),
            started_at="2026-09-17T10:00:00+00:00",
            updated_at="2026-09-17T10:01:00+00:00",
            session_id="session-1",
            provider="codex",
        )
        state = ProjectState(
            enabled=True,
            configuration={"default_provider": "codex"},
            activation={"codex": {"state": "guarded", "session_id": "session-1"}},
            active_run=run,
            active_runs={"codex:session-1": run},
            recent_runs=(run,),
            event_history=(event,),
        )

        self.store.save(self.project, state)

        self.assertEqual(self.store.load(self.project), state)
        self.assertEqual(json.loads(self.state_path().read_text())["schema_version"], 2)

    def test_project_key_uses_the_canonical_project_path(self):
        alias = self.project / ".." / self.project.name
        other = self.root / "other"
        other.mkdir()

        self.assertEqual(project_key(alias), project_key(self.project.resolve()))
        self.assertNotEqual(project_key(other), project_key(self.project))

    def test_owner_scan_coordinates_with_project_writer_on_windows(self):
        run = RunState("run", "task", session_id="root", provider="codex")
        self.store.save(self.project, ProjectState(active_run=run,
                                                   active_runs={"codex:root": run}))
        reading = threading.Event()
        release = threading.Event()
        writing = threading.Event()
        written = threading.Event()
        errors = []
        matches = []
        original = Path.read_text

        def paused_read(path, *args, **kwargs):
            if path == self.state_path() and threading.current_thread().name == "owner-scan":
                reading.set()
                if not release.wait(5):
                    raise TimeoutError("owner scan was not released")
            return original(path, *args, **kwargs)

        def scan():
            try:
                matches.extend(self.store.active_owner_paths("codex", "root"))
            except BaseException as error:
                errors.append(error)

        def write():
            writing.set()
            try:
                self.store.update(self.project, lambda state: (state, None))
            except BaseException as error:
                errors.append(error)
            finally:
                written.set()

        with patch.object(Path, "read_text", paused_read):
            scanner = threading.Thread(target=scan, name="owner-scan")
            writer = threading.Thread(target=write, name="other-session-writer")
            scanner.start()
            self.assertTrue(reading.wait(5))
            writer.start()
            self.assertTrue(writing.wait(5))
            if os.name == "nt":
                self.assertFalse(written.wait(0.1), "Windows writer bypassed the snapshot lock")
            else:
                self.assertTrue(written.wait(1), "POSIX snapshot blocked a project writer")
            release.set()
            scanner.join(5)
            writer.join(5)
        self.assertFalse(scanner.is_alive() or writer.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual([self.state_path()], matches)
        self.assertTrue(written.is_set())

    def test_windows_owner_snapshot_waits_for_a_normal_parallel_transaction(self):
        self.store.save(self.project, ProjectState(enabled=True))
        path = self.state_path()
        reading = threading.Event()
        done = threading.Event()
        snapshots = []
        errors = []

        def read():
            reading.set()
            try:
                snapshots.append(_read_owner_snapshot(path))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        with patch("plugins.symphony.symphony.store.os", SimpleNamespace(name="nt")):
            with _locked(path):
                reader = threading.Thread(target=read)
                reader.start()
                self.assertTrue(reading.wait(5))
                self.assertFalse(done.wait(0.25), "Snapshot abandoned a normal writer transaction")
            reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual([path.read_text(encoding="utf-8")], snapshots)

    def test_owner_scan_defers_on_busy_unrelated_windows_project(self):
        run = RunState("run", "task", session_id="root", provider="codex")
        self.store.save(self.project, ProjectState(active_run=run,
                                                   active_runs={"codex:root": run}))
        other = self.root / "other"
        other.mkdir()
        self.store.save(other, ProjectState(enabled=True))
        other_path = self.store._path(other)
        held = threading.Event()
        release = threading.Event()

        def hold_other():
            with _locked(other_path):
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold_other)
        holder.start()
        try:
            self.assertTrue(held.wait(5))
            owners = self.store.active_owner_paths("codex", "root")
            self.assertEqual(None if os.name == "nt" else (self.state_path(),), owners)
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())

    def test_new_duplicate_owner_after_prior_lookup_is_not_hidden(self):
        run = RunState("run", "task", session_id="root", provider="codex")
        state = ProjectState(active_run=run, active_runs={"codex:root": run})
        self.store.save(self.project, state)
        self.assertEqual((self.state_path(),), self.store.active_owner_paths("codex", "root"))
        other = self.root / "other"
        other.mkdir()
        self.store.save(other, state)
        self.assertEqual({self.state_path(), self.store._path(other)},
                         set(self.store.active_owner_paths("codex", "root")))

    def test_owner_scan_shares_one_deadline_and_defers_incomplete_ownership(self):
        run = RunState("run", "task", session_id="root", provider="codex")
        state = ProjectState(active_run=run, active_runs={"codex:root": run})
        self.store.save(self.project, state)
        paths = [self.state_path()]
        for name in ("unrelated", "duplicate"):
            project = self.root / name
            project.mkdir()
            self.store.save(project, state if name == "duplicate" else ProjectState())
            paths.append(self.store._path(project))
        clock = [0.0]
        budgets = []

        def slow_snapshot(path, timeout=5):
            budgets.append(timeout)
            clock[0] += min(3, timeout)
            if timeout < 3:
                raise TimeoutError("snapshot budget exhausted")
            return path.read_text(encoding="utf-8")

        with patch.object(Path, "glob", return_value=iter(paths)), \
                patch("plugins.symphony.symphony.store.time.monotonic", lambda: clock[0]), \
                patch("plugins.symphony.symphony.store._read_owner_snapshot", slow_snapshot):
            self.assertIsNone(self.store.active_owner_paths("codex", "root"))
        self.assertEqual([5, 2], budgets)
        self.assertEqual(5, clock[0])

    def test_bounded_lock_shares_deadline_between_local_and_native_waits(self):
        clock = [0.0]
        released = []

        def acquire(timeout):
            clock[0] += 4
            return True

        def busy(*args):
            raise OSError("native lock held")

        def sleep(seconds):
            clock[0] += seconds

        local = SimpleNamespace(acquire=acquire, release=lambda: released.append(True))
        native = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=busy)
        with patch("plugins.symphony.symphony.store._local_lock", return_value=local), \
                patch("plugins.symphony.symphony.store.fcntl", None), \
                patch("plugins.symphony.symphony.store.msvcrt", native), \
                patch("plugins.symphony.symphony.store.time.monotonic", lambda: clock[0]), \
                patch("plugins.symphony.symphony.store.time.sleep", sleep):
            with self.assertRaises(TimeoutError):
                with _locked(self.state_path(), timeout=5):
                    self.fail("busy native lock acquired")
        self.assertLess(clock[0], 5.01)
        self.assertEqual([True], released)

    def test_snapshot_scan_and_atomic_replace_remain_compatible(self):
        run = RunState("run", "task", session_id="root", provider="codex")
        self.store.save(self.project, ProjectState(active_run=run,
                                                   active_runs={"codex:root": run}))
        errors = []
        start = threading.Event()
        stop = threading.Event()
        progress = {"writer": 0, "reader": 0, "max_write_seconds": 0.0,
                    "max_read_seconds": 0.0, "writer_inflight_since": None}

        def write():
            start.wait(5)
            try:
                for _ in range(300):
                    if stop.is_set():
                        break
                    begun = time.monotonic()
                    progress["writer_inflight_since"] = begun
                    self.store.update(self.project, lambda state: (state, None))
                    progress["writer"] += 1
                    progress["max_write_seconds"] = max(
                        progress["max_write_seconds"], time.monotonic() - begun)
                    progress["writer_inflight_since"] = None
            except BaseException as error:
                errors.append(("writer", repr(error)))

        writer = threading.Thread(target=write)
        writer.start()
        start.set()
        stalled = False
        snapshot = {}
        try:
            for _ in range(300):
                begun = time.monotonic()
                try:
                    self.assertIn(self.store.active_owner_paths("codex", "root"),
                                  (None, (self.state_path(),)))
                    progress["reader"] += 1
                    progress["max_read_seconds"] = max(
                        progress["max_read_seconds"], time.monotonic() - begun)
                except BaseException as error:
                    errors.append(("reader", repr(error)))
                    break
            writer.join(10)
            stalled = writer.is_alive()
            snapshot = progress.copy()
            if snapshot["writer_inflight_since"] is not None:
                snapshot["inflight_seconds"] = round(
                    time.monotonic() - snapshot["writer_inflight_since"], 3)
        finally:
            if stalled:
                print(f"Snapshot stress: {snapshot}, errors={errors}", flush=True)
            stop.set()
            writer.join()
        self.assertFalse(stalled, f"writer exceeded 10s after reader: {snapshot}, errors={errors}")
        self.assertFalse(errors, errors)
        self.assertEqual((self.state_path(),), self.store.active_owner_paths("codex", "root"))

    @unittest.skipUnless(os.name == "nt", "Windows path aliases")
    def test_windows_project_key_collapses_case_and_short_path_aliases(self):
        self.assertEqual(project_key(self.project), project_key(Path(str(self.project).swapcase())))

        spaced = self.root / "Project With Spaces"
        spaced.mkdir()
        import ctypes
        short_name = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(spaced), short_name, len(short_name))
        if length and length < len(short_name) and short_name.value != str(spaced):
            self.assertEqual(project_key(spaced), project_key(Path(short_name.value)))

    def test_save_atomically_replaces_a_sibling_temporary_file(self):
        self.store.save(self.project, ProjectState(enabled=False))
        real_replace = os.replace
        replacements = []

        def recording_replace(source, destination):
            replacements.append((Path(source), Path(destination)))
            real_replace(source, destination)

        with patch("plugins.symphony.symphony.store.os.replace", side_effect=recording_replace):
            self.store.save(self.project, ProjectState(enabled=True))

        self.assertEqual(len(replacements), 1)
        source, destination = replacements[0]
        self.assertEqual(source.parent, destination.parent)
        self.assertEqual(destination, self.state_path())
        self.assertFalse(source.exists())
        self.assertTrue(self.store.load(self.project).enabled)

    def test_save_retains_only_the_most_recent_twenty_runs(self):
        runs = tuple(RunState(run_id=f"run-{number}", task="task") for number in range(25))

        self.store.save(self.project, ProjectState(recent_runs=runs))

        self.assertEqual(
            [run.run_id for run in self.store.load(self.project).recent_runs],
            [f"run-{number}" for number in range(5, 25)],
        )

    def test_corrupt_state_is_preserved_without_dropping_live_work(self):
        path = self.state_path()
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "original preserved"):
            self.store.load(self.project)
        self.assertEqual(path.read_text(encoding="utf-8"), "not-json")

    def test_future_schema_is_preserved_without_replacement(self):
        path = self.state_path()
        path.parent.mkdir(parents=True)
        content = json.dumps({"schema_version": 3, "active_runs": {"unknown": {"live": True}}})
        path.write_text(content)
        with self.assertRaisesRegex(ValueError, "unsupported state schema"):
            self.store.load(self.project)
        self.assertEqual(path.read_text(), content)

    def test_schema_one_upgrade_preserves_the_active_run(self):
        path = self.legacy_path()
        path.parent.mkdir(parents=True)
        run = RunState("old-run", "unfinished", session_id="old-session")
        from dataclasses import asdict
        path.write_text(json.dumps({
            "schema_version": 1, "enabled": True, "configuration": {"policy": "keep"},
            "activation": {"codex": {"session_id": "old-session"}}, "active_run": asdict(run), "recent_runs": [],
            "event_history": [], "needs_reassessment": False,
        }))

        state = self.store.load(self.project)

        identified = RunState("old-run", "unfinished", session_id="old-session", provider="codex")
        self.assertEqual(state.active_run, identified)
        self.assertEqual(state.active_runs, {"codex:old-session": identified})
        self.assertTrue(state.enabled)
        self.assertEqual(state.configuration, {"policy": "keep"})
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
        self.assertEqual(json.loads(self.state_path().read_text())["schema_version"], 2)

    def test_old_writer_cannot_overwrite_the_imported_v2_run(self):
        path = self.legacy_path()
        path.parent.mkdir(parents=True)
        original = {
            "schema_version": 1, "enabled": True,
            "activation": {"codex": {"session_id": "owner"}},
            "active_run": {"run_id": "owner-run", "task": "Keep owner", "session_id": "owner"},
        }
        path.write_text(json.dumps(original))
        self.assertEqual(self.store.load(self.project).active_runs["codex:owner"].run_id, "owner-run")

        # Simulate a still-running 1.4.6 hook replacing its own file after import.
        path.write_text(json.dumps({**original, "active_run": None}))
        self.store.update(self.project, lambda state: (state, None))

        self.assertEqual(self.store.load(self.project).active_runs["codex:owner"].run_id, "owner-run")
        self.assertIsNone(json.loads(path.read_text())["active_run"])

    def test_ambiguous_v1_owner_is_retained_without_claiming_a_provider(self):
        path = self.legacy_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "schema_version": 1, "enabled": True, "activation": {},
            "active_run": {"run_id": "old-run", "task": "Unfinished", "session_id": "shared"},
        }))

        state = self.store.load(self.project)

        self.assertEqual(set(state.active_runs), {"unbound:shared"})
        self.assertEqual(state.active_runs["unbound:shared"].provider, "")
        self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_pre_1_0_migration_imports_only_enablement_and_configuration(self):
        path = self.legacy_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "enabled": True,
                    "configuration": {"default_provider": "claude"},
                    "activation": {"codex": {"state": "guarded"}},
                    "active_run": {"run_id": "unsafe"},
                    "recent_runs": [{"run_id": "unsafe"}],
                    "event_history": [{"id": "unsafe"}],
                }
            ),
            encoding="utf-8",
        )

        state = self.store.load(self.project)

        self.assertTrue(state.enabled)
        self.assertEqual(state.configuration, {"default_provider": "claude"})
        self.assertEqual(state.activation, {})
        self.assertIsNone(state.active_run)
        self.assertEqual(state.recent_runs, ())
        self.assertEqual(state.event_history, ())
        self.assertTrue(state.needs_reassessment)
        self.assertTrue(path.exists(), "old hooks still own the unsuffixed state file")
        self.assertTrue(self.state_path().exists())

    def test_update_serializes_parallel_read_modify_write(self):
        workers = 12

        def increment():
            def transition(state):
                configuration = dict(state.configuration)
                configuration["count"] = int(configuration.get("count", 0)) + 1
                return ProjectState(**{**state.__dict__, "configuration": configuration}), None

            self.store.update(self.project, transition)

        threads = [threading.Thread(target=increment) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(self.store.load(self.project).configuration["count"], workers)

    def test_updates_from_separate_processes_preserve_both_runs(self):
        script = """
import sys, time
from dataclasses import replace
from pathlib import Path
from plugins.symphony.symphony.model import RunState
from plugins.symphony.symphony.store import StateStore
store = StateStore(Path(sys.argv[1]))
project, session = Path(sys.argv[2]), sys.argv[3]
def add(state):
    runs = dict(state.active_runs)
    time.sleep(0.15)
    runs['codex:' + session] = RunState(session, session, session_id=session)
    return replace(state, active_runs=runs), None
store.update(project, add)
"""
        processes = [subprocess.Popen(
            [sys.executable, "-c", script, str(self.data), str(self.project), session],
            cwd=Path(__file__).resolve().parents[3],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for session in (f"worker-{index}" for index in range(8))]
        for process in processes:
            _, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(set(self.store.load(self.project).active_runs),
                         {f"codex:worker-{index}" for index in range(8)})

    @unittest.skipUnless(os.name == "nt", "Windows byte-range lock behavior")
    def test_empty_windows_lockfile_contends_and_recovers_after_process_exit(self):
        path = self.data / "fresh-state.json"
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True)
        ready = self.root / "ready"
        script = """
import msvcrt, sys, time
from pathlib import Path
with Path(sys.argv[1]).open('a+b') as handle:
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    Path(sys.argv[2]).write_text('ready')
    time.sleep(10)
"""
        holder = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(ready)],
                                  stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(500):
                if ready.exists() or holder.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(ready.exists(), holder.stderr.read() if holder.poll() is not None else "")
            with self.assertRaises(TimeoutError):
                with _locked(path, timeout=0.1):
                    pass
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=5)
        with _locked(path, timeout=5):
            self.assertEqual(0, lock_path.stat().st_size)

    def test_persistence_redacts_secret_values_and_credential_text(self):
        state = ProjectState(
            enabled=True,
            configuration={"api_key": "sk-secret", "note": "password=hunter2"},
            active_run=RunState("run", "Authorization: Bearer abcdefghijklmnop"),
        )

        self.store.save(self.project, state)

        contents = self.state_path().read_text(encoding="utf-8")
        self.assertNotIn("sk-secret", contents)
        self.assertNotIn("hunter2", contents)
        self.assertNotIn("abcdefghijklmnop", contents)
        self.assertIn("[REDACTED]", contents)

    def test_persistence_redacts_provider_credential_names_and_token_shapes(self):
        secrets = ['credential-one', 'credential-two', 'credential-three',
                   'ghp_' + 'a' * 36, 'github_pat_' + 'b' * 40,
                   'sk-ant-api03-' + 'c' * 32, 'cookie-credential', 'passphrase-credential']
        self.store.save(self.project, ProjectState(configuration={
            'authToken': secrets[0], 'providerAPIKey': secrets[1],
            'future': {'vendor.credentials': {'opaque': secrets[2]},
                       'ordinary': [*secrets[3:6], 'harmless-provider-value'],
                       'headers': {'Cookie': secrets[6]}, 'keyPassphrase': secrets[7]},
            'tokenCount': 42,
        }))
        contents = self.state_path().read_text(encoding='utf-8')
        for secret in secrets:
            self.assertNotIn(secret, contents)
        saved = self.store.load(self.project).configuration
        self.assertEqual(saved['authToken'], '[REDACTED]')
        self.assertEqual(saved['providerAPIKey'], '[REDACTED]')
        self.assertEqual(saved['future']['vendor.credentials'], '[REDACTED]')
        self.assertEqual(saved['future']['ordinary'][-1], 'harmless-provider-value')
        self.assertEqual(saved['tokenCount'], 42)


if __name__ == "__main__":
    unittest.main()
