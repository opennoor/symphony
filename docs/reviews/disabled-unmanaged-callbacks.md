# Disabled sessions and unmanaged callbacks

## Cause

The supplied 1.7.0 macOS incident contained a disabled project, two heartbeats,
no managed runs or terminal receipts, and two completed turns of the ordinary
`macos_review` child. Its terminal callbacks had verified native parent/name
metadata. The runtime queued each callback because no active run owned it,
then made that queue a Stop obligation even though Symphony was disabled.

## Fix and review

Classify ordinary root callbacks outside disabled governance under the owning
session and project locks. Preserve their original callback fields in private,
content-addressed observation files before acknowledging the inbox records.
Do not create task outcomes, managed runs, delegations, or terminal receipts.
An existing exact observation makes a crash before acknowledgment recoverable
even if the user subsequently enables Symphony. Provider, session, root project,
generation, event kind, original event ID, time, and callback payload all bind
that observation.

Enabled projects, live managed runs, recorded managed identities, reserved roles,
pending admissions, retired children, and foreign ownership remain guarded.
Other sessions' live runs are neither borrowed nor finalized. A child's worktree
does not change the project of its root's observation. Ordinary large reports
are preserved directly rather than overflowing the managed recovery inbox.
If preservation fails, a live callback is retained for retry.

This is an ownership and admission fix, not a classification of task difficulty
or a certification that an unmanaged child completed successfully. Genuine
unresolved managed results continue to require their native ownership evidence.

## Local validation

- Replayed the full attached project state and both original pending callbacks
  in temporary storage: Stop permitted, both callbacks preserved exactly, zero
  managed runs and zero terminal receipts. Original user state was not edited.
- Added a sanitized captured regression and 14 test methods covering ordinary
  native/default reviews, follow-ups, both provider formats, managed exemptions,
  independent roots in a shared checkout, separate child worktrees, generation
  and retirement guards, archive errors, large results, and crash/retry behavior.
- Real generated, installed hook commands passed the new regression for both
  providers. Codex parent/name/model metadata came from a native child fixture;
  the fixture was unchanged afterward.
- Final unit suite: 825 tests passed, 7 platform-dependent skips, 104.874 seconds.
  A temporary runner used the installed PowerShell binary directly because Snap
  confinement was unavailable, and relocated one private-clock fixture from the
  read-only home directory to `/tmp`. Assertions were unchanged. The runner used
  a main guard so multiprocessing children executed their assigned test workers.
- All ten offline installed-package lifecycle scenarios passed. Generated hook
  freshness and `git diff --check` passed.

No AI API calls, native AI inference, or CI tests were used. These are local
Linux tests with real PowerShell and synthetic provider lifecycle fixtures plus
the captured macOS replay; they are not live macOS or Windows client tests.
No new version has been published.
