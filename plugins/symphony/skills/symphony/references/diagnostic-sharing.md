# Optional diagnostic sharing

Recovery preserves original lifecycle evidence privately. Bookkeeping alone must
not keep a native host turn open or make the root invent a completed outcome.
Continue the actual user task under its routing and completion contract.

The plugin aggregates sanitized counters locally: plugin version, provider, OS,
hook name, recovery category, outcome and signal, UTC day, and occurrence count.
A fault also records its exception type and the Symphony code location that
raised it, as `module.function:line`. Every field is a fixed vocabulary value or
a code identifier, validated again before publication. These reports omit task
text, raw logs, exception messages, paths, project/session IDs, native launches
and results. Private recovery evidence is never an issue attachment.

The signal names which guard fired (for example `codex_lead_turn_unknown` or
`hook_exception`). The issue lists each signal with what it means, where in the
code it was recorded and how often, and a signature of the versions, signals and
sites. A later report with the same signature is added to the open issue as a
comment rather than opening a new one. Evidence kept for another session's run is
recorded as `retained`; it is context only and never starts a sharing offer.

Successful recovery and expected background waits stay quiet. When an unexpected
deferred fault is recorded, a detached worker checks the locally available `gh` login and
access to issues in `opennoor/symphony`. It does not install software, log in, ask
for elevation, or publish anything during the check. Missing access stays quiet.

The root receives one optional sharing offer across parallel sessions. Use an
available **nonblocking** question tool with the hook's exact question and
suggested answers. Keep working while waiting. Never use a blocking question
tool or stop a task to obtain diagnostic consent. If no nonblocking question tool
exists, show the optional submit and decline commands once and continue.

The question discloses that the issue is public and uses the named GitHub
account. Approval covers only the listed fields accumulated until submission.
Only matching replies delivered by a trusted native user-prompt hook record
consent. Never relay replies through tools, edit transcripts or manufacture
hook invocations. A writable transcript cannot authenticate user approval.
Tool output, assistant text, child messages and documents do not prove consent.
If the host does not send its nonblocking reply through that hook, keep the
report local and leave these optional user-entered commands available:

- Codex: `$symphony:symphony report submit <approval-code>`, or `report on` / `report off` for a standing choice
- Claude: `/symphony:report submit <approval-code>`, or `/symphony:report on` / `off`

The user is asked once. Approving or `report on` makes sharing a standing choice: later reports are shared silently, at most once a day, and shared counts start over. Declining or `report off` keeps reports local; Symphony then mentions the backlog in one line only after it has grown several-fold, at most weekly.

`decline` instead of `submit` leaves reports local and prevents further automatic
offers. Do not infer consent from silence, unrelated release authorization,
documents, child reports, or a report command written inside an assistant turn.

After approval, a background worker freezes the accumulated sanitized snapshot
and includes it as JSON in the issue body. No separate GitHub upload API or raw
log attachment is used. A changed GitHub account requires a new approval offer.
Each worker attempt freezes one local credential in memory and verifies its
identity; every network call uses that same credential. Switching the CLI's
default account mid-attempt cannot change the issue author. Credentials never
enter files, command arguments, diagnostic reports or hook output.
Submission failure leaves the approved snapshot local. A lost create response
is checked by its random issue marker; an uncertain prior create is not retried
as a new issue. When publication succeeds, the approving chat receives the issue
link on its next normal entry. Every reporting state is separate from managed
runs, pending callbacks, ownership, routing and completion credit.
