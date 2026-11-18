# Orchestrator mode

Orchestrator mode runs a task through headless full agents on Codex and Claude Code instead of the host's own subagents. The root stays shallow at the model and effort the user chose for the session. The strongest available assessor sizes the task, then one lead owns it and delegates bounded packets to full agents on both providers. Each agent is an ordinary `codex exec` or `claude -p` session pinned to an exact model and effort, and it may use its own host's subagents.

## Turning it on

| Host | Standing mode | One task |
| --- | --- | --- |
| Codex | `$symphony:symphony orchestrate on` / `off` | `$symphony:symphony orchestrate <task>` |
| Claude Code | `/symphony:orchestrate on` / `off` | `/symphony:orchestrate <task>` |
| Hermes | `/symphony on` / `off` | type the task while the mode is on |

On Codex and Claude Code the standing mode changes what a governed task does: with the project enabled every task prompt is orchestrated, and `start <task>` is orchestrated either way. A native run that is already active finishes natively first. Hermes has no native Symphony route, so its mode alone decides.

## Root steps

The hook or the Hermes plugin prints the exact launcher command. The root runs four steps and nothing else on the task:

1. `assess --task-file <file> --wait 540` opens a run and starts the assessor. It prints the run id and the assessment.
2. `lead --run <run> --wait 540` starts the matrix lead for the assessed size and complexity.
3. `wait --run <run> --timeout 540` until it exits 0. Exit 3 means still running.
4. `result --run <run>` prints the lead's report; `status --run <run>` lists every agent with provider, model, effort and why it was chosen.

Exit codes: 0 done, 1 an agent ended without success, 2 the request was refused, 3 still running. Give the commands a shell timeout of at least 600 seconds; agents run detached and keep running when a call is cut short.

## How an agent's provider, model and effort are chosen

Each provider proposes its own reviewed choice: the strongest tier at high effort for the assessor, and the matrix cell of the packet's size and complexity for a lead, worker or consultant, from the profile the account is entitled to. The two proposals are then ranked by one rule:

1. An independent review runs on the other provider than the work it reviews.
2. The smaller capability shortfall against the reviewed policy wins. An account limited to a weaker model than the policy intends loses to one that is not. For the assessor this is simply the stronger model.
3. The smaller effort shortfall wins.
4. Equal candidates are spread: the provider running fewer of this run's agents, then the one given fewer so far, then a rotation derived from the run id.

`--prefer price` (or `SYMPHONY_ORCHESTRATION_PREFER=price`) places price before step 4: Codex credits are converted to dollars with the reviewed `cross_provider_cost` rate, and anything within 1.5 times the cheapest counts as equal.

A provider whose CLI is missing, or whose login answers for no model, is left out and named in the selection. `--provider`, `--model` and `--effort` override the choice and require `--why`; the reason is recorded with the agent. `select` shows the choice without starting anything and `catalog` shows what each provider can launch.

`SYMPHONY_CODEX_PROFILE` and `SYMPHONY_CLAUDE_PROFILE` pin a provider's shipped profile for the launcher. Claude entitlement comes from `SYMPHONY_CLAUDE_AVAILABLE_MODELS` when set, otherwise from one short probe per model that is cached for a week. Fable stays opt-in through that variable, as on the native route.

## Lead and agents

The lead receives the task, the assessment and the launcher commands. It starts workers and consultants with `agent --role <worker|consultant> --size <...> --complexity <...> --packet-file <file>`, waits with `wait <agent-id>...`, reads reports with `result <agent-id>`, integrates and verifies. A packet states objective, ownership, constraints, acceptance_check and return_contract. At most 8 agents run at once in a run. Agents started by a lead cannot start further Symphony agents.

Agents run with full permissions by default (`danger-full-access` on Codex, `bypassPermissions` on Claude Code) so that none stalls on a prompt nobody can answer. `--permission workspace-write` or `read-only` narrows one agent.

An orchestrated session carries `SYMPHONY_ORCHESTRATED=1`. Its own Symphony hooks stay inert, so a packet is never routed a second time.

## Records

Runs live under `~/.symphony/state/orchestration/<project>/runs/<run>/` (or `SYMPHONY_STATE_DIR`), readable only by the user. Each agent keeps its route, selection reason, state, final report and the provider CLI's output. The task text stays with the run for the lead; each agent's prompt is deleted once delivered. Finished runs are removed after 14 days.
