"""Run the Windows lifecycle matrix with a short standard-user command line."""

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TESTS = (
    "plugins.symphony.tests.test_package.PackageContractTests.test_codex_windows_hooks_run_without_a_working_py_launcher",
    "plugins.symphony.tests.test_package.PackageContractTests.test_claude_hooks_select_available_python_with_a_quoted_plugin_path",
    "plugins.symphony.tests.test_store.StateStoreTests.test_updates_from_separate_processes_preserve_both_runs",
    "plugins.symphony.tests.test_store.StateStoreTests.test_windows_project_key_collapses_case_and_short_path_aliases",
    "plugins.symphony.tests.test_concurrent_sessions",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_same_lead_followup_recovers_without_another_start_hook",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_same_text_result_after_a_new_start_is_a_new_lifecycle",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_identical_no_id_restart_keeps_outcome_unreconciled",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_identified_old_result_cannot_replay_into_a_new_child_turn",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_terminal_retry_can_correct_explicit_role_evidence",
    "plugins.symphony.tests.test_reducer.LifecycleReducerTests.test_replacement_lead_cannot_archive_ambiguous_old_start",
    "plugins.symphony.tests.test_runtime_retention",
    "plugins.symphony.tests.test_boost",
)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
