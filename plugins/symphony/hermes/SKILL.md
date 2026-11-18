---
name: orchestrator
description: Use when Symphony orchestrator mode is on or the user asks Symphony to run a task. Route the task through headless full agents on Codex and Claude Code.
---

# Symphony orchestrator on Hermes

Hermes is the root. It stays shallow at the model and effort the user chose for this session. The work runs in headless full agents on Codex and Claude Code, each pinned to an exact model and effort. Hermes subagents are not used for the task: `delegate_task` cannot pin a model and effort per task.

`/symphony on` turns the mode on for the current project, `/symphony off` turns it off, `/symphony status` shows the mode and the latest run.

## Steps

The per-turn guidance prints the exact launcher command. It is `python3 "<plugin>/scripts/symphony_agent.py"`; below it is written `AGENT`.

1. Keep low-token work yourself: a question, one command, a quick lookup. Clarify unclear intent with the user before starting; agents cannot ask.
2. Write the full request and its acceptance checks to a file, then run `AGENT assess --task-file <file> --wait 540`. It opens a run, starts the strongest available assessor across both providers, and prints the run id and the assessment.
3. Run `AGENT lead --run <run> --wait 540`. It starts the matrix lead for the assessed size and complexity. The lead delegates bounded packets to full agents on both providers, integrates and verifies.
4. Exit code 3 means still running. Repeat `AGENT wait --run <run> --timeout 540` until it exits 0. Do not poll faster and do not do the task yourself meanwhile.
5. Run `AGENT result --run <run>` and relay the lead's outcome. Disclose anything it reports unfinished. `AGENT status --run <run>` lists every agent with provider, model, effort and why it was chosen.

Exit codes: 0 done, 1 an agent ended without success, 2 the request was refused, 3 still running. Run these commands in the foreground with a 600 second timeout; agents run detached and keep running when a call is cut short.

## Rules

- The launcher chooses provider, model and effort by one rule for both providers. Pass `--provider`, `--model` or `--effort` only on the user's instruction, with `--why`.
- `AGENT catalog` shows what each provider can launch here. A provider whose CLI is missing or not signed in is left out; say so when it matters.
- Agents run with full permissions by default so that none stalls on a prompt. Use `--permission workspace-write` or `read-only` when the user asks for less.
- `AGENT stop --all --run <run>` stops a run's agents.
- Report only what the launcher recorded: provider, model, effort, state and the agents' own reports.
