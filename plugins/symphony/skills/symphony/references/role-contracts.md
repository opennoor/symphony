# Role contracts

Roles exchange explicit packets. Lifecycle changes come from reducer state and host events, never magic prose or completion receipts.

Every managed spawn must request an explicit model and effort; never inherit the assessor's settings. Put exactly one of these first-class lines in the task packet so the lifecycle hook can classify the host event even when the provider reports a generic agent type:

```text
SYMPHONY_ROLE: assessor
SYMPHONY_ROLE: lead
SYMPHONY_ROLE: worker
SYMPHONY_ROLE: consultant
```

Claude's Agent `PreToolUse` hook blocks unmarked spawns before launch. On Symphony's currently supported Codex collaboration path, observed events detect a mis-routed or unmarked spawn after the child starts; the cost of that spawn has already been incurred. Codex documentation also advertises `PreToolUse` for ordinary `spawn_agent`; extending Symphony's guarantee requires a real-host check of this exact collaboration path. An assessor normally uses high effort or above; an explicit session effort override may select low or medium and must match the reported effective route. A lead must use the matrix-selected effort, and workers and consultants cannot start until a lead is registered.

Provider binding is mechanical:

- Claude Code: select a packaged `symphony-<role>-<model>-<effort>` agent type. The agent definition pins both settings because Claude's Agent call does not expose per-call effort. Reference it with the plugin prefix, `symphony:symphony-<role>-<model>-<effort>`. Symphony names the exact assessor and lead types in the root's guidance and the worker types in the lead's start context. Agents run in the background, and a background agent's result reaches Symphony through its `SubagentHandback` report.
- Codex: pass `model` and `reasoning_effort`, use `fork_turns="none"`, and use `symphony_<role>_<model>_<effort>` for assessor, assessed lead, worker, and consultant task names. The initial fast lead uses exactly `symphony_lead_fast_<model>_<effort>` instead; its reserved name must not be replaced by the assessed-lead name. Relay a bounded packet explicitly; never fork the root history into an assessor or lead.

Compact status shows at most five latest delegation records, ordered failed, active/waiting, then recently completed. `agents --all` shows every retained latest record, not every transition.

Use the host role name `symphony_<role>_<model>_<effort>` for assessed work when custom names are supported; keep the fast lead's distinct reserved name before escalation. Every visible delegation line includes `role [model/effort]`, the host-observed identity, and its bounded objective. Omit model, effort, tokens, or duration when the host does not expose them; never infer them.

## Assessment

For a new task, the hook may offer one fast lead at the account profile's capable model and medium effort. The root only relays the full task; it does not judge eligibility at its own effort. The lead packet has `SYMPHONY_ROLE: lead` and `SYMPHONY_FAST_ROUTE: lead` on separate lines, with the complete objective and acceptance check. On Codex, name it `symphony_lead_fast_<model>_<effort>` with model punctuation replaced by underscores so a native Start can identify the route even when PreToolUse is absent. Before any changes, the lead checks that the WHOLE objective consists only of predetermined mechanical steps with an expected result, scope bounded, requirements clear, risk low, required tools (including browser or computer control when needed) available, and verification concrete. A supplied bash/git command or reading a specified browser page through known steps can qualify. Implementation, diagnosis, design, substantive review, product judgment, mixed work, or uncertainty escalates before any changes, even for a tiny feature. A run-and-fix request escalates as a whole. Only an eligible mechanical objective works directly and returns both `SYMPHONY_FAST_DECISION: eligible` and `SYMPHONY_OUTCOME: {"status":"completed"}`. Otherwise it makes no changes and returns `SYMPHONY_FAST_DECISION: escalate`. The root waits for that native terminal, spawns an independent assessor for the original task, and follows the accepted matrix route. Missing, malformed, failed, or interrupted fast decisions remain recoverable. The fast lead cannot spawn descendants before deciding.

Run substantive or uncertain work as a bounded assessment on the strongest model in the accepted account profile, normally at `high` effort. A session assessor boost uses native levels above high: Codex xhigh/max/ultra, Claude xhigh/max. Unsupported account/model levels are rejected, never mapped to another effort. Boosts apply only to subsequent assessor spawns. The assessor chooses needs, not provider model names, and does not become the lead implicitly.

Required fields:

```yaml
size: small | medium | large
complexity: simple | mixed | complex
risk: normal | high
rationale: <concise evidence-based reason>
topology: <advisory recommendation; the matrix fixes delegated | mixed>
abstract_role_routes:
  lead: <tier/effort>
  workers: <tier/effort requirements for substantive work>
  consultants: <tier/effort requirements, capacity, or none>
```

The assessor's final response must include the same fields on one exact machine-readable line so Codex can accept the route from its native `SubagentStop` event:

```text
SYMPHONY_ASSESSMENT: {"size":"medium","complexity":"mixed","risk":"normal","rationale":"...","topology":"mixed"}
```

Before spawning the selected lead, put its role and the accepted fields on exact first-class lines in the lead task so the lifecycle hook can register the route before the host starts it:

```text
SYMPHONY_ROLE: lead
SYMPHONY_ROUTE: {"size":"medium","complexity":"mixed","risk":"normal","rationale":"...","topology":"mixed"}
```

The JSON values must use the matrix vocabulary above. The accepted topology always comes from the matrix, even when the assessor recommends a different one. Claude can reject malformed Agent spawn markers before launch. On the currently observed Codex collaboration path, Symphony correlates task name and settings from the native child transcript and accepts the route from the assessor's final lifecycle message; no pre-spawn rejection is established for that path.

## Actionable work packet

Every lead, worker, and consultant receives only the bounded context needed for its assignment. Every packet has these exact fields:

```yaml
objective: <one bounded outcome or decision>
ownership: <files, subsystem, or decision authority>
evidence: <relevant facts and sources>
constraints: <safety, compatibility, and process limits>
acceptance_check: <observable done condition>
return_contract: <result and evidence to return>
size: small | medium | large
complexity: simple | mixed | complex
```

**Bounded means scoped, never abridged.** A lead spawned with `fork_turns="none"` cannot see the request the user actually made, so the packet is the only copy. If the user asked for five things, `objective` states all five and `acceptance_check` is satisfied only when every one of them is met. Dropping items to make an objective read as a single sentence loses work silently: nothing downstream compares what was asked against what was done, and the completion gate checks only that no agent is still running. Split a request across several packets when the items are genuinely independent, and say so in each, but never narrow the request to fit the field.

`size` and `complexity` are local to the packet. Each consultant decision is classified separately; a consultant may split one question into multiple packets.

For applicable phases, put the exact advertised capability to try (or `native`), compatibility limits, and required observable practice in `constraints` and `acceptance_check`. Ask for a compact phase record in `return_contract`: exact capability/source or native fallback, availability reason (`usable`, `absent`, `disabled`, `failed`, or `incompatible`), practice performed, and artifact or fresh command/result. A child can discover a different actual state; report that state and use the native fallback. Missing evidence is `incomplete` or a scoped exception, never a verified success claim. The lead checks returned artifacts and results before integrating; a skill invocation alone proves no practice was completed.

Symphony's hooks enforce supported routing and lifecycle events. They do not observe every Skill invocation: Claude's packaged hooks match `Agent` calls, not `Skill` calls, and Symphony does not claim host-enforced optional-practice compliance. Agent reports and lead inspection provide the practice evidence; label them separately from host-observed lifecycle facts.

## Lead

- Coordinate execution, integration, verification, and user communication for the task.
- Assign substantive implementation, diagnosis, design, review tasks, and product judgment to workers or consultants. Review integration and verify returned results as the lead.
- A small lead assigns substantive work to one worker, then integrates and verifies its result.
- A medium lead assigns substantive work to bounded worker packets, then integrates and verifies the results.
- A large lead is an inexpensive administrator: delegate project work and reserve consultant capacity for narrow decisions.
- After verifying the integrated result and receiving all child results, native successful assessed completion is sufficient. An optional `SYMPHONY_OUTCOME` report must be one valid JSON line; use `blocked` or `failed` when work remains. Duplicate, conflicting or malformed reports keep the run recoverable. Fast and archived followup completion retain their explicit marker requirements.
- Newly accepted delegated and mixed routes require successful substantive worker or classified consultant evidence from the current assessment, lead and owner generation. Missing evidence holds completion; resume the same lead to delegate the remaining work, integrate and verify its result, then report completion again. Older route records without this contract retain their recovery behavior.
- Scoped child evidence preserves its admitted Start role and exact lead ownership. Claude credit requires a fresh child with one native prompt and an exact Agent launch in the canonical lead's transcript. Preserve reused child history and repair invalid consultant decisions; delegate fresh bounded work when multiple prompts prevent assigning that child to this invocation.
- Archived followup only reconciles the same bounded task and retains its valid child evidence. A new or substantive objective starts a fresh assessment and delegation scope; previous-task workers cannot satisfy it. Agents judge that semantic boundary; marker strings alone cannot prove that a request is a continuation.
- On Claude, root `SendMessage` can continue the archived assessed lead using its exact original agent ID and a literal message. Each delivery, native child turn and retained callback must agree in order. Intermediate successful native turns acknowledge their reports; only the final valid completed outcome can complete the task. Competing deliveries, uncertain child ownership or missing original-run proof remain pending. Repeated Stop retries cannot supply that proof; inspect status and preserve the original session's evidence while recovering the same owner.
- Preserve active ownership across reassessment. Replace the lead only at a safe boundary or when unavailable or materially incapable.
- When required consultation is unavailable, a large lead uses the disclosed conservative fallback instead of absorbing specialist reasoning silently.
- Native successful `SubagentStop` events reconcile a lead even when its start event was missed. If the lead includes `SYMPHONY_OUTCOME: {"status":"completed"}`, the marker stays internal; malformed or non-success reports keep the run recoverable even when the host says the agent ended. Host termination alone cannot override an explicit blocked or failed outcome. Pending launches, active descendants, and unclassified consultant results still prevent run completion.
- Ending the root turn permits waiting for host results. Once the lead and tracked work reconcile, finish the Codex turn for its native Stop hook, or invoke `/symphony:stop` on Claude. An assistant-written Codex control is not a new user prompt. A repeated Stop releases that turn while retaining unfinished work; it never proves task completion or agent abandonment. Report completion only after durable status confirms it.
- If a worker ends unsuccessfully after the lead reported completion, that earlier outcome is invalidated and the run returns to lead recovery. A fresh registered lead must integrate the failure and return a new outcome. Replayed native completion events cannot restore the old outcome.
- An interrupted worker remains unresolved until its own host identity reports a terminal recovery result. Resume or follow up with that existing agent when the host supports it, then register a fresh lead to integrate the result. A new agent with the same role or objective does not prove that the interrupted agent ended; Symphony does not infer replacement from prose or objective similarity.

## Worker

- Own only the packet objective and acceptance check.
- Work directly; agent depth ends at worker/consultant.
- Return the requested result and evidence to the lead. Do not claim lifecycle completion.

## Consultant

- Own a bounded decision, not implementation or orchestration.
- Return the recommendation, rationale, evidence, uncertainty, and consequences requested by the packet.
- Classify each decision with decision-local size and complexity.
- Put one `SYMPHONY_DECISION` line per actionable decision in the final result; also include it in the Claude spawn packet so that provider can reject a missing classification before launch:

```text
SYMPHONY_ROLE: consultant
SYMPHONY_DECISION: {"size":"small","complexity":"mixed"}
```

## Reassessment boundaries

Reassess on an explicit request, new task, approved plan, completed worker wave, resume or compaction, interruption, or reported scope/risk drift. Skip an unchanged evidence fingerprint. Route changes apply to subsequent work and never duplicate active work.
