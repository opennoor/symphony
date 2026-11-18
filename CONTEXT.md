# Symphony

Symphony gives a thin root agent a reliable way to complete project work through the right model, effort, and delegation topology, on both Codex and Claude Code, and from Hermes in orchestrator mode. This glossary fixes the terms that have caused ambiguity; it is not a spec.

## Language

**Tier**:
A capability rank in the routing matrix: `economy`, `balanced`, `capable`, `strongest`. Always abstract, never a provider model name.
_Avoid_: level, class, subscription tier

**Entitlement**:
The set of models and reasoning efforts a provider account actually grants. Distinct from tier: a tier is what the matrix asks for, an entitlement is what the account can supply.
_Avoid_: subscription tier, plan level, access

**Profile**:
One shipped tier-to-model matrix variant, keyed to an entitlement. Several profiles ship with each release; exactly one applies to a given account.
_Avoid_: variant, mapping, capability snapshot

**Run**:
One governed unit of work, beginning when the assessor is spawned and ending when the lead's outcome is recorded and no tracked work remains. A prompt that never produces an assessor spawn produces no run.
_Avoid_: task, session, job

**Delegation**:
One host-observed child agent within a run, in a lead, worker, consultant, or assessor role.
_Avoid_: child, subagent record, agent entry

**Guarded**:
The state of a provider session in which a Symphony hook has executed and written a matching heartbeat. Installation, discovery, and trust are not guarded.
_Avoid_: armed, active, enabled

**Degraded**:
A run whose hook activation could not be verified, so Symphony will not call it guarded.
_Avoid_: unarmed, unverified

**Abandoned**:
A run permitted to complete while tracked work was never reconciled, because the host reported no terminal event for it. Distinct from degraded, which is about activation rather than tracked work.
_Avoid_: stale, orphaned, stuck

**Host**:
The program the root session runs in: Codex, Claude Code or Hermes. Distinct from provider: Hermes is a host but supplies no agents.
_Avoid_: platform, client, frontend

**Provider**:
A source of agents with its own CLI, models and entitlement: Codex or Claude Code.
_Avoid_: vendor, backend, host

**Orchestrator mode**:
The route in which a root delegates through the launcher instead of its host's own subagents: an assessor, then a lead, then full agents on either provider.
_Avoid_: cross-provider mode, headless mode, multi-agent mode

**Full agent**:
A headless provider session started by the launcher, pinned to one model and effort, that may use its own host's subagents. Distinct from a delegation, which is a host-observed child of a native run.
_Avoid_: external agent, remote agent, sub-session

**Launcher**:
The command that starts, tracks and reports full agents, and chooses their provider, model and effort by one rule for both providers.
_Avoid_: runner, dispatcher, spawner

**Safe boundary**:
A lifecycle point at which lead ownership may transfer: completion of a worker wave, an explicit request, a new task, an approved plan, resume or compaction, interruption, or reported scope drift.
_Avoid_: checkpoint, transition point
