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
generation, event kind, original event ID, and complete callback payload bind
replay identity. The original observation time remains in the saved evidence;
it is excluded from replay identity because provider retries receive new times.
An alternate private recovery directory handles primary archive
write failures without introducing managed inbox limits or overflow flags.

Full acknowledged report copies retain at most 100 files and 16 MiB per root
scope. Pending reports remain protected. Small content-hash facts persist for
the resumable session lifetime, independently of full-report retention. Facts
are committed before inbox ACK; pruning follows successful ACK and also checks
the remaining pending IDs. Native result files are unchanged.

Enabled projects, live managed runs, recorded managed identities, reserved roles,
pending admissions, retired children, and foreign ownership remain guarded.
Other sessions' live runs are neither borrowed nor finalized. A child's worktree
does not change the project of its root's observation. Ordinary large reports
are preserved directly rather than overflowing the managed recovery inbox.
Primary archive failures preserve callbacks in the separate recovery path.

This is an ownership and admission fix, not a classification of task difficulty
or a certification that an unmanaged child completed successfully. Genuine
unresolved managed results continue to require their native ownership evidence.

## Local validation

- Replayed the full attached project state and both original pending callbacks
  in temporary storage: Stop permitted, both callbacks preserved exactly, zero
  managed runs and zero terminal receipts. Original user state was not edited.
- Added a sanitized captured regression and 19 test methods covering ordinary
  native/default reviews, follow-ups, both provider formats, managed exemptions,
  independent roots in a shared checkout, separate child worktrees, generation
  and retirement guards, archive errors, large results, and crash/retry behavior.
- Real generated, installed hook commands passed the new regression for both
  providers. Codex parent/name/model metadata came from a native child fixture;
  the fixture was unchanged afterward.
- Final unit suite after the Copilot fixes: 830 tests passed, seven
  platform-dependent skips, 106.503 seconds.
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

## Copilot feedback

The first review correctly identified oversized failed writes entering the
bounded managed inbox and the absence of a full-report retention policy. Added
failing regressions for a large report combined with archive failure, bounded
report count/bytes, protected pending evidence, and replay after report pruning
and re-enablement. The fixes use the separate recovery directory and small
scope-bound hash facts described above; they do not grant managed task credit.
The ACK failure regression also verifies that committing hash facts cannot
allow retention to remove a report still present in the durable pending inbox.

The second review identified that a native provider retry has a stable callback
ID but a new observation timestamp. Reproduced the failure through both provider
adapters after pruning and re-enabling. Replay now excludes only the local
observation time from identity; it still binds the full callback payload and
owning scope. A changed report with the same event ID remains unresolved.
Also verified retry after a crash between the full archive write and the hash
fact commit: the original report and observation time remain unchanged.

The third review found that the managed inbox field projection omitted ordinary
and future provider fields from unmanaged evidence. The unmanaged archive now
preserves the complete normalized payload, with the existing secret redaction,
excluding only the runtime's synthetic owner-conflict and verified-alias flags.
Both providers' regressions preserve hook name, cwd, stop flags, an arbitrary
nested provider field, and verified native metadata; live retries after enable
continue to match the same complete representation.
