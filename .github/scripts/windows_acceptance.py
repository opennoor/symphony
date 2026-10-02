"""Run the Windows lifecycle matrix with a short standard-user command line."""

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TESTS = (
    "plugins.symphony.tests.test_package.PackageContractTests.test_codex_windows_hooks_run_without_a_working_py_launcher",
    "plugins.symphony.tests.test_package.PackageContractTests.test_windows_relay_accepts_a_slow_working_interpreter_probe",
    "plugins.symphony.tests.test_package.PackageContractTests.test_windows_relay_does_not_hide_other_interpreters_behind_broken_python_entries",
    "plugins.symphony.tests.test_package.PackageContractTests.test_windows_discovery_timeout_preserves_candidates_without_running_the_hook_twice",
    "plugins.symphony.tests.test_package.PackageContractTests.test_native_windows_discovery_preserves_unicode_and_metacharacters_without_cwd_search",
    "plugins.symphony.tests.test_package.PackageContractTests.test_windows_launcher_propagates_runtime_failure_without_reexecuting_hook",
    "plugins.symphony.tests.test_package.PackageContractTests.test_claude_hooks_select_available_python_with_a_quoted_plugin_path",
    "plugins.symphony.tests.test_store.StateStoreTests.test_updates_from_separate_processes_preserve_both_runs",
    "plugins.symphony.tests.test_store.StateStoreTests.test_empty_windows_lockfile_contends_and_recovers_after_process_exit",
    "plugins.symphony.tests.test_store.StateStoreTests.test_windows_project_key_collapses_case_and_short_path_aliases",
    "plugins.symphony.tests.test_store.StateStoreTests.test_owner_scan_coordinates_with_project_writer_on_windows",
    "plugins.symphony.tests.test_store.StateStoreTests.test_windows_owner_snapshot_waits_for_a_normal_parallel_transaction",
    "plugins.symphony.tests.test_store.StateStoreTests.test_owner_scan_defers_on_busy_unrelated_windows_project",
    "plugins.symphony.tests.test_store.StateStoreTests.test_new_duplicate_owner_after_prior_lookup_is_not_hidden",
    "plugins.symphony.tests.test_store.StateStoreTests.test_snapshot_scan_and_atomic_replace_remain_compatible",
    "plugins.symphony.tests.test_concurrent_sessions",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_same_lead_followup_recovers_without_another_start_hook",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_same_text_result_after_a_new_start_is_a_new_lifecycle",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_identical_no_id_restart_keeps_outcome_unreconciled",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_identified_old_result_cannot_replay_into_a_new_child_turn",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_terminal_retry_can_correct_explicit_role_evidence",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_claude_pre_run_unmarked_callbacks_do_not_block_managed_stop",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_claude_pre_run_managed_child_conflict_still_blocks_stop",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_claude_pre_run_marked_child_conflict_still_blocks_stop",
    "plugins.symphony.tests.test_runtime.RuntimeTests.test_claude_pre_run_malformed_agent_type_is_not_discarded",
    "plugins.symphony.tests.test_reducer.LifecycleReducerTests.test_replacement_lead_cannot_archive_ambiguous_old_start",
    "plugins.symphony.tests.test_runtime_retention",
    "plugins.symphony.tests.test_boost",
    "plugins.symphony.tests.test_fast_route",
    "plugins.symphony.tests.test_native_harness_snapshot.CandidateRetainedProfileTests.test_claude_live_update_allows_one_original_turn_to_finish_just_before_removal",
)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
