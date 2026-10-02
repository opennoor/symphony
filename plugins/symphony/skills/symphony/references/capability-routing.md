# Capability routing

## Fixed matrix

| Size / complexity | Lead | Execution | Consultation |
|---|---|---|---|
| small / simple | capable/medium | delegated, one worker | none |
| small / mixed | capable/high | delegated, one worker | optional narrow |
| small / complex | strongest/high | delegated, one worker | required independent check |
| medium / simple | balanced/medium | selective delegation | none |
| medium / mixed | balanced/high | selective delegation | optional narrow |
| medium / complex | capable/high | selective delegation | reserve one slot |
| large / simple | economy/low | administrative delegation | none |
| large / mixed | economy/medium | administrative delegation | reserve one slot |
| large / complex | economy/medium | administrative delegation | strongest/high bounded decisions |

Risk is `normal` or `high`. `high` raises a lead effort of `low` to `medium` and requires an independent check. A different worker or consultant reviews completed substantive work and reports `SYMPHONY_REVIEW: passed` only after resolving findings. Risk never changes the reported size or complexity. Reserved consultation means an available concurrency slot, not an idle agent.

The fixed matrix and these risk rules are authoritative for topology; an assessor's recommended topology is advisory input. Assessed substantive work goes to workers or consultants. Small tasks need one worker; medium tasks use bounded worker packets. The lead coordinates, integrates, and verifies results. `mixed` means worker execution with lead integration and verification.

## Capability resolution

Before the matrix, the fast route may launch one lead at the current entitlement profile's `capable` model with `medium` effort. It is available only when that model and effort are shipped for the provider and the reviewed model policy ranks it at least 2. This excludes the economy floor without naming a particular provider model. An explicit assessor boost uses the assessor route directly. Before any changes, the fast lead checks that the WHOLE objective consists only of predetermined mechanical steps with an expected result and passes the boundedness, clarity, low-risk, tool-availability, and verification checks. Implementation, diagnosis, design, substantive review, product judgment, mixed work, or uncertainty escalates to an independent strongest/high assessor before any changes, even for a tiny feature or a run-and-fix request. The fast route does not change entitlement clamps on the later assessed route.

Resolve each cell through the selected shipped profile's model and effort choices. Profiles without cell choices fall back to the abstract tiers `economy`, `balanced`, `capable`, and `strongest`. Runtime selects a shipped profile; it does not refresh the model map. For 1.7.0, maintainers run native validation locally through installed Codex and Claude clients with existing logins. Release CI checks the shipped profiles, reviewed policy, and generated agents offline. The API-backed capability-refresh workflow is disabled for this release; these offline checks do not establish live account access to every model.

If no suitable assessor is available, disclose and use the conservative shipped route—the root does not improvise an assessment.

## Shipped resolved routes

<!-- generated routes: start -->
The tables below are generated from `profiles.json` with the runtime resolver. The last profile for each provider is the fallback when entitlement is unknown.

### Codex: `latest`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| small / mixed | `gpt-6.1-sol/medium` | `gpt-6.1-sol/high` |
| small / complex | `gpt-6-astra/high` | `gpt-6-astra/high` |
| medium / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| medium / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| medium / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |
| large / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| large / mixed | `gpt-6-luna/medium` | `gpt-6-luna/medium` |
| large / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |

### Codex: `full`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| small / mixed | `gpt-6-sol/medium` | `gpt-6-sol/high` |
| small / complex | `gpt-6-astra/high` | `gpt-6-astra/high` |
| medium / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| medium / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| medium / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |
| large / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| large / mixed | `gpt-6-luna/medium` | `gpt-6-luna/medium` |
| large / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |

### Codex: `luna-sol-6-1`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| small / mixed | `gpt-6.1-sol/medium` | `gpt-6.1-sol/high` |
| small / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |
| medium / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| medium / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| medium / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |
| large / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| large / mixed | `gpt-6-luna/medium` | `gpt-6-luna/medium` |
| large / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |

### Codex: `sol-6-1`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6.1-sol/low` | `gpt-6.1-sol/medium` |
| small / mixed | `gpt-6.1-sol/medium` | `gpt-6.1-sol/high` |
| small / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |
| medium / simple | `gpt-6.1-sol/low` | `gpt-6.1-sol/medium` |
| medium / mixed | `gpt-6.1-sol/medium` | `gpt-6.1-sol/high` |
| medium / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |
| large / simple | `gpt-6.1-sol/low` | `gpt-6.1-sol/medium` |
| large / mixed | `gpt-6.1-sol/medium` | `gpt-6.1-sol/medium` |
| large / complex | `gpt-6.1-sol/high` | `gpt-6.1-sol/high` |

### Codex: `luna-sol`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| small / mixed | `gpt-6-sol/medium` | `gpt-6-sol/high` |
| small / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |
| medium / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| medium / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| medium / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |
| large / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| large / mixed | `gpt-6-luna/medium` | `gpt-6-luna/medium` |
| large / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |

### Codex: `sol`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-sol/low` | `gpt-6-sol/medium` |
| small / mixed | `gpt-6-sol/medium` | `gpt-6-sol/high` |
| small / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |
| medium / simple | `gpt-6-sol/low` | `gpt-6-sol/medium` |
| medium / mixed | `gpt-6-sol/medium` | `gpt-6-sol/high` |
| medium / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |
| large / simple | `gpt-6-sol/low` | `gpt-6-sol/medium` |
| large / mixed | `gpt-6-sol/medium` | `gpt-6-sol/medium` |
| large / complex | `gpt-6-sol/high` | `gpt-6-sol/high` |

### Codex: `base` (fallback)

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| small / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| small / complex | `gpt-6-luna/high` | `gpt-6-luna/high` |
| medium / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| medium / mixed | `gpt-6-luna/medium` | `gpt-6-luna/high` |
| medium / complex | `gpt-6-luna/high` | `gpt-6-luna/high` |
| large / simple | `gpt-6-luna/low` | `gpt-6-luna/medium` |
| large / mixed | `gpt-6-luna/medium` | `gpt-6-luna/medium` |
| large / complex | `gpt-6-luna/high` | `gpt-6-luna/high` |

### Claude Code: `fable-5-5`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| small / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| small / complex | `claude-fable-5-1/high` | `claude-fable-5-1/high` |
| medium / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| medium / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| medium / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| large / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| large / mixed | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/medium` |
| large / complex | `claude-opus-5-5/medium` | `claude-opus-5-5/medium` |

### Claude Code: `opus-5-5`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| small / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| small / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| medium / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| medium / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| medium / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| large / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| large / mixed | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/medium` |
| large / complex | `claude-opus-5-5/medium` | `claude-opus-5-5/medium` |

### Claude Code: `sonnet-5-5`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| small / mixed | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/high` |
| small / complex | `claude-sonnet-5-5/high` | `claude-sonnet-5-5/high` |
| medium / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| medium / mixed | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/high` |
| medium / complex | `claude-sonnet-5-5/high` | `claude-sonnet-5-5/high` |
| large / simple | `claude-sonnet-5-5/low` | `claude-sonnet-5-5/medium` |
| large / mixed | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/medium` |
| large / complex | `claude-sonnet-5-5/medium` | `claude-sonnet-5-5/medium` |

### Claude Code: `fable`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| small / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| small / complex | `claude-fable-5-1/high` | `claude-fable-5-1/high` |
| medium / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| medium / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| medium / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| large / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| large / mixed | `claude-sonnet-5/medium` | `claude-sonnet-5/medium` |
| large / complex | `claude-opus-5-5/medium` | `claude-opus-5-5/medium` |

### Claude Code: `opus`

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| small / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| small / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| medium / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| medium / mixed | `claude-opus-5-5/medium` | `claude-opus-5-5/high` |
| medium / complex | `claude-opus-5-5/high` | `claude-opus-5-5/high` |
| large / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| large / mixed | `claude-sonnet-5/medium` | `claude-sonnet-5/medium` |
| large / complex | `claude-opus-5-5/medium` | `claude-opus-5-5/medium` |

### Claude Code: `sonnet` (fallback)

| Size / complexity | Normal risk | High risk |
|---|---|---|
| small / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| small / mixed | `claude-sonnet-5/medium` | `claude-sonnet-5/high` |
| small / complex | `claude-sonnet-5/high` | `claude-sonnet-5/high` |
| medium / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| medium / mixed | `claude-sonnet-5/medium` | `claude-sonnet-5/high` |
| medium / complex | `claude-sonnet-5/high` | `claude-sonnet-5/high` |
| large / simple | `claude-sonnet-5/low` | `claude-sonnet-5/medium` |
| large / mixed | `claude-sonnet-5/medium` | `claude-sonnet-5/medium` |
| large / complex | `claude-sonnet-5/medium` | `claude-sonnet-5/medium` |
<!-- generated routes: end -->

## Entitlement clamps

Each provider ships several profiles: an account routes through the best one it is entitled to, and through the conservative floor when entitlement cannot be read. A clamp is measured against the best profile, not the applied one.

A **tier clamp** means a weaker model does the work. It blocks the lead spawn and waits: the user accepts it with `/symphony:proceed` (Codex: `$symphony:symphony proceed`), which holds for the rest of that provider session and is asked again in the next one. An **effort clamp** on the same model is announced and the run continues, because effort is the dimension the matrix already trades away under risk.

## Optional phase practices

Symphony alone selects routes, delegation, lifecycle, reassessment, and completion. Apply a supporting practice only when its exact skill or tool is advertised and usable in this session, its instructions have been read, and it fits the packet and user instructions. A cache directory, plugin name, or past session proves neither availability nor use. Distinguish **advertised**, **readable/callable**, and **used with observable evidence**. If absent, disabled, failed, or incompatible, perform the native practice in the same row. Do not run every suite on every task or introduce a second scheduler.

| Phase and trigger | Compatible capability practice | Native practice and evidence | Installation benefit |
|---|---|---|---|
| Requirements need clarification | Root uses Superpowers `brainstorming`; Matt Pocock `grilling` only for explicit stress-testing or unsettled decisions. Keep interactive questions at the root. | Concise brief, material questions, agreed outcome, constraints, assumptions, success criteria. | Structured alternatives and decision testing. |
| Structural code discovery | Codebase Memory graph search, relevant trace, snippets, and index coverage for every relied-on path; read coverage gaps directly. | Targeted source search/read and paths supporting conclusions. Healthy indexing alone is not complete coverage. | Faster relationship and impact discovery. |
| Current external library/API facts | Context7 `resolve-library-id` then `query-docs` when callable; match the dependency version and inspect provenance. | Official versioned docs/source, retrieval date, source URL and conclusion; state uncertainty if inaccessible. | Focused documentation retrieval. |
| Implementation planning | Compound Engineering `ce-plan` when structured planning is needed. Ponytail checks reuse, stdlib/native features, and installed dependencies. | Bounded ordered steps, ownership, dependencies, acceptance checks; omit speculative work. | Researched planning and a consistent simplicity check. |
| Behavior change or bug fix | Compatible Superpowers `test-driven-development`; Matt Pocock `tdd` for agreed public seams or `diagnosing-bugs` for a reproducible symptom; compatible Compound Engineering `ce-work` for execution. | Repro or meaningful behavior check, observed failure when required, root-cause change, fresh passing command/result; explain a scoped testing exception. | Test sensitivity, diagnosis and execution conventions. |
| Independent review | Compatible Compound Engineering `ce-code-review` or Matt Pocock `code-review`, within the assigned route and depth. | Exact revision/diff, requirement and caller/failure-path inspection, findings, disposition and coverage limits. | Risk-directed review and standards/spec comparison. |
| Completion claim | Superpowers `verification-before-completion` when usable. | Inspect final artifacts and run the relevant fresh check; report command/result or an explicit incomplete/exception status. | Consistent verification discipline. |
| Authorized commit, release, or monitoring | Relevant Compound Engineering shipping skill when its mode fits the packet; keep monitoring under Symphony. | Repository-native git/forge/release commands, exact revision checks, delivery identity/result, and host wait/status. | Established publishing and recovery checks. |

Ponytail's simplicity check applies across design, implementation, and review; use the smallest correct change while preserving required validation, security, accessibility, and evidence. If a selected skill introduces independent model elevation, agents, approvals, shipping, or an execution engine that cannot honor the packet, classify it incompatible and use the native practice. Compound Engineering `ce-work` return-to-caller and `ce-code-review` report-only modes do not by themselves suppress nested delegation. Do not invoke a whole workflow merely because a piece of it is relevant.

For Codex, resolve the exact advertised skill name and read its `SKILL.md` and required references; no universal Skill API is assumed. For Claude Code, use an enabled, model-invocable Skill mechanism when available. Do not infer invocation from a readable plugin copy. A lead names the applicable practice and evidence check in each child packet. Record per applicable phase: exact capability/source or `native`, availability reason, practice performed, and artifact or command/result. Skill loading proves invocation only; the lead checks the returned work, and missing evidence is incomplete or an explicit exception, never verified success. Host hooks observe only supported events and lifecycle facts, not semantic quality.

Recommend a material missing capability only when its benefit improves this task's fallback. Aggregate gaps into one concise notice with capability, affected phase, benefit, native fallback, and official installation reference; aim for at most once per project and Symphony version. This is an instruction, not persisted cross-session deduplication: repeat suppression is only as reliable as observable session/project state. Distinguish absent from disabled, broken, and incompatible; do not suggest reinstalling the latter. Never auto-install or delay startup. Official references: [Superpowers](https://github.com/obra/superpowers), [Compound Engineering](https://github.com/EveryInc/compound-engineering-plugin), [Context7](https://github.com/upstash/context7), [Ponytail](https://github.com/DietrichGebert/ponytail), [Codebase Memory](https://github.com/DeusData/codebase-memory-mcp), [Matt Pocock skills](https://github.com/mattpocock/skills).

## Optional indexed memory

Enable `.symphony/context.md` only after Codebase Memory reports healthy indexing and usable coverage. Store curated goals, decisions and sources, constraints, stable architecture facts, verified outcomes, unresolved risks, and concise continuation state. Exclude transcripts, secrets, raw tool output, inferred telemetry, and routine progress. If indexing is absent or unhealthy, continue with compact lifecycle state only.
