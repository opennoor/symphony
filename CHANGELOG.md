# Changelog

## 1.9.5 — prepared 2026-10-08

- Fix the loop where a sized task's implementer was refused with "Spawn the Symphony assessor first" and re-sizing repeated it. A prompt arriving between sizing and the lead launch (the user's go-ahead, or an agent hand-back delivered as a prompt) no longer closes the run; only runs whose lead has ended are closed by a new prompt.
- A root that stops after sizing to wait for the user's go-ahead is no longer pushed by Stop; the next prompt's guidance names the lead to launch.
- A lead carrying a valid route is never refused because Symphony lost its run: the run is reopened from that route, and kept only if the spawn is accepted. A child Symphony cannot place runs unmanaged instead of being blocked.
- A fresh sizing or fast task replaces a sized run that never got its lead, on Claude and on Codex, and the replaced run stays in history.
- When a new request closes a blocked run, that run's queued notes are dropped instead of reaching the model; a blocked lead's note no longer tells the root to ask the user to stop the run.
- The Stop hook's status line now reads "Symphony end-of-turn check" instead of claiming active work is being reconciled.

## 1.9.0 — prepared 2026-10-05

Symphony is now a best-effort token-efficiency helper: its own problems never stop, slow or alarm the user.

- Hold a turn only while the user's delegated work is still running or an assessed task's lead was never launched. Bookkeeping, unverifiable evidence, ownership and outcome-marker problems no longer block Stop; they are counted privately.
- A new user prompt closes a run that has nothing left running instead of replaying its recovery on every later prompt. Finished runs close as `completed`, honestly blocked ones as `blocked`, others as `superseded`; active or completing runs and host notifications never close a run.
- Never block the root's own tools. Questions, single commands, quick lookups and other low-token work are done directly with no agent; routing substantive work stays guidance.
- Remove the weaker-route consent gate: the account's own model route is used without stopping for `/symphony:proceed`, which deadlocked sessions whose entitlement could not be read.
- Stop surfacing internal bookkeeping to the model: no per-prompt "retained evidence" note, interruption or stale-owner notices, or "plugin defect" messages.
- Diagnostics ask once, disclosing that approval also shares later reports. `report on` shares anonymous error reports silently, at most daily, from the approving GitHub account only (another account is asked again), and counts start over after each share; `report off` keeps them local and cancels anything queued, with a one-line reminder only when the backlog has grown several-fold, at most weekly. Declining names the way back.
- Leads run independent children in parallel (up to six at once) and dependent ones in order, and wait and retry when a host agent limit refuses a spawn.
- A user's `/symphony:stop` never blocks their own prompt; it reports what is still running instead.

## 1.8.1 — prepared 2026-10-05

- Never block a user's work on activation. When a chat gets no Symphony hook guidance (for example, Codex on Windows where the hook cannot run), the agent says so once in one line and does the task without Symphony at full quality (a low-cost root hands non-simple work to one capable agent), instead of stopping to ask whether to continue unguarded. Hook guidance in the turn is itself proof that Symphony is active; the activation checker is now a `status` diagnostic, not a gate.

## 1.8.0 — prepared 2026-10-04

- Route routine or plausibly mechanical tasks through one capable/medium fast lead. Permit bounded discovery and verified authorized git, shell, and browser work without requiring every command in advance; escalate substantive work and actual uncertainty or risk to independent assessment.
- Reconcile successful Codex child followups and delayed native terminal delivery at normal lifecycle boundaries. Require the original launch, current assessment and owner, exact completed turn, and unique native delivery before granting substantive or independent-review credit.
- Recognize Claude Skill expansions as companions of the original worker invocation only when bound to a unique native Skill call in the same prompt, session, and child. Keep genuine later prompts, ambiguous metadata, stale launches, and foreign evidence uncredited.
- Reconcile Claude fast-route hand-back and final reports with their exact native callbacks, including repeated markers and split terminal message chunks. Keep conflicting or incomplete evidence unresolved and report recovery without claiming completion.
- Require concise final summaries checked against requirements and fresh verification. Omit resolved internal wait and recovery narration, while disclosing unresolved errors and missing checks.
- Complete Claude fast-route runs on hosts without `SubagentHandback`, such as print mode. The lead's final message is then its single report, still requiring one fast decision and a completed outcome and matching its exact native callback. Previously every such run stayed open after correct work.
- Record an honest fast-lead `blocked`, `failed` or `incomplete` report without waiting for native success proof it can never have. Completion and escalation claims still require native evidence.
- Stop no longer repeats its warning after a lead explicitly reports a blocker that needs the user (#14). The run stays open and unsuccessful, the same lead can finish it once the blocker is resolved (#12), and new child evidence after the report restores the normal guard.
- Let `/symphony:proceed` hold when the account's model entitlement could not be read. Consent to the conservative route no longer depends on a non-empty profile, which previously kept the lead blocked indefinitely.
- Let the turn end while a lead launch waits for the user's consent to a weaker route, instead of blocking Stop on a lead that only the user can release.
- Tell the root to report and end its turn once a run is reconciled, on both hosts, rather than typing stop or status controls that surfaced as chatter. Drop a stale "still tracks pending launches" notice once the lifecycle batch settles.
- Let the user's own stop control close a run that is blocked on a reported blocker (recorded as `blocked`) or held for route consent (recorded as `stopped`), and say that the run was closed.
- Keep a worker that loaded a Skill verifiable at Stop: Claude's Skill expansion rows, bound to one native Skill call, are part of that turn for completion chronology as they already were for child credit.
- Treat an exact repeat of one non-success outcome as a single report, so a blocked Claude fast report whose final message repeats its handback is recorded; repeated success claims still need native proof.
- Wait for Codex children in five-minute intervals instead of one minute. `wait_agent` returns as soon as a child finishes, so the repeated "Waiting for agents" blocks in the Codex UI drop sharply.
- Make diagnostic reports actionable: each records the hook, a fixed signal naming the guard that fired, and for faults the exception type and Symphony code location. Issues explain every signal, lead with the faults, carry a signature, and repeat reports join the open issue as comments. Evidence kept for another session's run no longer prompts a sharing offer, and non-Stop hook faults are now counted.

## 1.7.5 — prepared 2026-10-03

- Correct Codex's generated Interrupt timeout to its supported three-second limit while preserving every other provider/event timeout.
- End native Stop bookkeeping faults quietly without acknowledging unresolved callbacks or granting completion credit. Bound storage-lock waits and incomplete-work Stop retries per actual user turn. Keep independent retained records from holding a proven current completion; retain native ownership, routing and admission guards.
- Add optional background GitHub diagnostic issues after local access checks and scoped user confirmation. Accumulate allowlisted version/provider/OS/recovery counters while awaiting consent, publish only the approved snapshot, and recover lost submission responses without duplicate issues. Missing access or consent never blocks work; raw evidence remains private.
- Reconcile completed Codex follow-up sequences after an archived diagnostic run, including fresh substantive workers and multiple lead turns. Require each root delivery, original child launch, exact native result, and lead handoff; preserve newer runs and foreign roots. Commit source witnesses before inbox acknowledgment so crashes and delayed retries cannot duplicate credit.
- Keep ordinary untracked child callbacks outside disabled Symphony governance on Codex and Claude. Preserve their evidence before acknowledgment without opening a run, granting completion credit, or blocking Stop. Live managed work, known identities, reserved roles, admission intents, foreign ownership, generation, and retirement checks remain guarded.
- Preserve full normalized provider payloads at inbox entry and archival, including future provider fields, with existing secret redaction and without synthetic replay flags. Bind replay to the provider, root project, session, generation, event identity, and payload; record the original observation time without using changing retry timestamps as identity.
- Recover failed primary archive writes through a separate private recovery directory without overflowing the managed inbox. Limit acknowledged report copies to 100 files and 16 MiB per root scope, protect pending evidence, and retain small hash facts for retries after pruning or re-enabling.
- Redact provider-prefixed and camel-case credential keys, nested credential containers, and recognizable token strings before durable storage. Preserve ordinary provider metadata and replay compatibility. Windows Claude smoke checks select native Git Bash explicitly and expand plugin roots through the environment.
- Cover the captured macOS incident, both provider hook formats, shared-checkout roots, child worktrees, large reports, archive and acknowledgment failures, retention, and native retries. Include the callback regression suite in Restricted PowerShell and standard-user Windows release acceptance. Release CI remains offline with no AI API tests.

## 1.7.0 — prepared 2026-10-02

- Validate the exact locally tested candidate before merge can expose a marketplace update; publish only through a manual release dispatch for the exact locally reviewed and tested `main` commit. Release CI runs deterministic Linux and Windows checks without provider installs or AI API calls; native testing uses local clients and existing logins. The separate API-backed capability refresh workflow is disabled for this release.
- Keep routing and delegation instructions ahead of task reminders for long prompts. Reuse returned Codex evidence, resume idle children explicitly, and wait only for active work in bounded intervals.
- Limit fast direct execution to wholly predetermined mechanical work. Tiny features, diagnosis, substantive review, and mixed run-and-fix requests require assessment before changes. Assessed small work uses a worker; the routing matrix controls execution topology.
- Require successful child work from the current assessment and lead generation before new delegated or mixed runs complete. Preserve the admitted child role and exact lead ownership. Claude scoped credit requires a fresh native child; retained multiple-prompt history has actionable same-lead recovery through fresh bounded delegation. A finished uncredited attempt can be explicitly superseded by a later credited child without blocking Stop. Legacy completion remains compatible.
- Bind each new child's substantive-work or independent-review purpose to its original native launch. Review findings cannot replace implementation evidence, and a substantive review requested as the deliverable still counts as work. Existing accepted contracts retain their completion rules.
- Preserve the original assessment boundary when its already-running assessor confirms the same accepted route after the lead starts. This prevents completed workers from losing credit because of delayed native callbacks; changed routes, new assessor turns, and explicit reassessment still require fresh worker evidence.
- Recover fresh leads after archived history and verified continuations of the latest archived owner through normal lifecycle entry points. Preserve exact native identity, root session, provider, model, effort, turn, and ownership checks. A new objective still requires fresh routing.
- Retain ambiguous first-arriving Claude terminals after continuation commit. New-source acknowledgments require the original transaction boundary; late Starts must be observed no later than their native terminal, and unseen Stops need an explicit matching native turn ID. Prompt context alone cannot prove a child invocation. Exact committed retries remain compatible.
- Bind new Claude fast runs to the exact accepted native launch so a parent call recorded before its hook can reconcile safely. Repeated native Stop releases the host turn while retaining unresolved work and displaying recovery guidance.
- Reconcile ordinary Claude completion across prompt-context changes and markerless resumed turns, including a first Stop held by unfinished work.
- Recognize Claude's native framed hand-back reports and Codex messages delivered after their send acknowledgement. Require exact child reports and actual parent delivery before the lead completes, so successful native work does not get trapped in repeated Stop recovery.
- Verify Claude child completion against the lead's native isolated worktree, including later lead turns resumed in the original project. Preserve exact launch ownership when checking either directory.
- Recognize Claude's exact native worker delivery after tool work when its final stop reason is null. Use the same tool-result/context boundary at admission and completion, while still requiring settled tool calls and rejecting a new user prompt.
- Ignore timestamp inversions in Claude attachment metadata when validating completion order. Keep executable calls, results, delivery, and stale-report checks strict so valid completed work does not get trapped in recovery.
- Distinguish a successful lead recovery's paired synthetic Start/completion transaction from a later native restart. Match its source, time, owner generation, outcome, and history order; genuine later starts and failures still invalidate an older result.
- Deliver the assessor's exact JSON contract directly at native Start on both providers. Unassessed leads stop before project work or child launches. Name Codex consultants explicitly and keep routing assessors at the root.
- Accept Claude's native background Agent completion notifications, including automatic backgrounding and batched notices, only when the child, launch, exact report, and delivery time agree. An early launch acknowledgement cannot complete a worker.
- Acknowledge an unadmitted preliminary Codex child only when exact native evidence and complete retained history prove its escalation finished before managed work began. Record an explicit unmanaged disposition without task or lead credit. Clarify the distinct fast and assessed lead names.
- Preserve accepted legacy risk text during route recovery while keeping new assessment validation strict.
- Reconcile committed archived fast-lead escalation callbacks after the actual 1.6.0 serializer removes newer receipt fields, using surviving exact run and native evidence for acknowledgement only. Missing or conflicting proof remains retained; reloading alone does not restore trimmed evidence.
- Supply an activation checker pinned to the verified interpreter running the hook, and probe Windows interpreter candidates before use with bounded startup timeouts. Keep later candidates with the same executable name available when earlier copies fail, within the shared probe deadline. Preserve absolute drive-root PATH entries and launcher expressions when Codex selects an outer PowerShell shell. Keep generated launchers reproducible across supported Python compression backends.
- Record obsolete tool-free Claude continuation acknowledgments as superseded only after an exactly owned completing successor. Preserve their callback identity through crash recovery without inventing successful receipts or worker credit. Tokenless Codex callbacks retain only header-bound metadata when resumed turns are ambiguous.

Native checks target Codex CLI 0.160.0 for the `latest` profile. CLI 0.158.0 needs `SYMPHONY_PROFILE=full` because its native spawn path rejects `gpt-6.1-sol`. Existing sessions retain their reviewed runtime until reload or restart; installing an update alone does not prove current-session activation.

Mixed-runtime release checks use the latest released version, 1.6.0. Finish managed work before upgrading from 1.5.1: its serializer can remove whole receipt records, leaving unresolved callbacks held after ownership changes. Repeated Stop or reload does not restore that missing proof.

## 1.6.0 — prepared 2026-09-29

- Reconcile archived child-start callbacks through verified session aliases so completed runs no longer leave another session's Stop blocked. Preserve checks for conflicting live owners and unknown starts.
- Clarify Codex root completion: the native Stop hook finalizes a completed run when the root turn ends. Native concurrency checks now record bounded Stop decisions and require gate release before lead completion.
- Add GPT-6.1 Sol to Codex routing. Preferred profiles use it for Sol work, while existing GPT-6 Sol profiles remain available to accounts without the new model.
- Add Claude Sonnet 5.5 to native account discovery and routing, and generate its packaged agents. Existing Sonnet 5 profiles remain available; Fable still requires explicit access.
- Refresh reviewed capability ranks and token rates for both providers. Keep entitlement checks and high-risk effort floors in the route validator.

Model availability is account, client, and workspace dependent. GPT-6.1 Sol supports `low` through `max` in the API; Codex can also expose `ultra` through its native roster. Claude Sonnet 5.5 supports `low` through `max`; Claude Code requires v2.1.284 or later for it. Live account entitlement and provider acceptance still require `refresh_profiles.py --verify` with suitable credentials.

Sources reviewed 2026-09-29: [OpenAI API model](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [Codex models](https://learn.chatgpt.com/docs/models), [Codex pricing](https://learn.chatgpt.com/docs/pricing), [Claude Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort), [Claude Code model configuration](https://code.claude.com/docs/en/model-config).
