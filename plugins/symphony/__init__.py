"""Hermes host for Symphony.

Hermes cannot pin a model and effort on its own subagents, so here Symphony
always works through the launcher: the session stays a shallow root and the
assessor, the lead and every agent run as headless Codex or Claude Code
sessions. Nothing below imports Hermes until Hermes calls `register`.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any


_HELP = (
    "Symphony on Hermes runs tasks through headless full agents on Codex and Claude Code.\n"
    "/symphony on      orchestrate tasks in this project\n"
    "/symphony off     stop orchestrating\n"
    "/symphony status  mode and the latest run\n"
    "/symphony agents  every agent of the latest run\n"
    "/symphony catalog what each provider can launch here\n"
    "With the mode on, type the task: this session stays a shallow root, the strongest available "
    "assessor sizes the task and a lead delegates it. The session's own model and effort are yours to pick."
)
# Sessions that already received the full steps; later turns get one line.
_briefed: set[str] = set()
_BRIEFED_LIMIT = 512


def _project(**context: Any) -> Path:
    explicit = context.get("cwd")
    if explicit:
        return Path(str(explicit))
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # provided by Hermes

        return Path(resolve_agent_cwd())
    except Exception:  # noqa: BLE001 - any host without that API uses the process directory
        return Path(os.getcwd())


def _run(arguments: list[str], project: Path) -> str:
    from .symphony import orchestrator_cli

    out, err = io.StringIO(), io.StringIO()
    orchestrator_cli.main(["--cwd", str(project), *arguments], os.environ, out, err)
    return (out.getvalue() + err.getvalue()).strip()


def command(raw_args: str = "", **context: Any) -> str:
    """The /symphony slash command. Returns its text; Hermes shows it."""
    from .symphony import orchestrator

    action = (raw_args or "").strip().lower()
    project = _project(**context)
    try:
        if action in {"on", "off"}:
            orchestrator.set_mode(project, os.environ, action == "on", "hermes")
            _briefed.clear()
            if action == "off":
                return "Symphony orchestrator mode is off for this project."
            return ("Symphony orchestrator mode is on for this project. Type the task: this session stays a "
                    "shallow root at its own model and effort, and the work runs through headless full "
                    "agents on Codex and Claude Code.\n" + _run(["catalog"], project))
        if action in {"", "status"}:
            state = "on" if orchestrator.mode(project, os.environ, "hermes") else "off"
            latest = _run(["status"], project)
            return f"Symphony orchestrator mode: {state}\n{latest}"
        if action == "agents":
            return _run(["status"], project)
        if action == "catalog":
            return _run(["catalog"], project)
    except Exception as error:  # noqa: BLE001 - a command must answer, not crash the gateway
        return f"Symphony could not do that: {type(error).__name__}: {error}"
    return _HELP


def guidance(session_id: str = "", **context: Any) -> dict[str, str] | None:
    """Per-turn guidance while the mode is on; silent otherwise."""
    try:
        from .symphony import orchestrator

        project = _project(**context)
        if not orchestrator.mode(project, os.environ, "hermes"):
            return None
        launcher = orchestrator.launcher_command(os.environ)
        if session_id and session_id in _briefed:
            return {"context": orchestrator.root_reminder(launcher)
                    + " The full steps are in the skill `symphony:orchestrator`."}
        if session_id:
            if len(_briefed) >= _BRIEFED_LIMIT:
                _briefed.clear()
            _briefed.add(session_id)
        return {"context": orchestrator.root_guidance(launcher)}
    except Exception:  # noqa: BLE001 - guidance is best effort and never blocks a turn
        return None


def register(ctx: Any) -> None:
    root = Path(__file__).resolve().parent
    ctx.register_skill("orchestrator", root / "hermes" / "SKILL.md")
    ctx.register_command(
        "symphony", command,
        description="Symphony orchestrator mode: on, off, status, agents, catalog",
        args_hint="[on|off|status|agents|catalog]",
    )
    ctx.register_hook("pre_llm_call", guidance)
