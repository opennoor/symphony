"""Command line for orchestrated agents: assess, lead, agent, wait, result.

Exit codes: 0 done, 1 an agent ended without success, 2 the request was
refused, 3 still running (call `wait` again).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, TextIO

from . import orchestrator as orch
from .routing import Assessment, route_for


DONE, UNSUCCESSFUL, REFUSED, RUNNING = 0, 1, 2, 3
STARTED = -1  # internal: launched without waiting; reported to the shell as 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony_agent.py",
        description="Start and track Symphony's headless full agents on Codex and Claude Code.")
    parser.add_argument("--cwd", default="", help="project directory (default: the current directory)")
    commands = parser.add_subparsers(dest="command", required=True)

    def route_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--provider", default="", choices=("", *orch.PROVIDERS))
        command.add_argument("--model", default="")
        command.add_argument("--effort", default="")
        command.add_argument("--why", default="", help="required with --provider, --model or --effort")
        command.add_argument("--prefer", default="", choices=("", *orch.PREFERENCES),
                             help="how equal candidates are separated (default: balance)")
        command.add_argument("--permission", default="full-auto", choices=orch.PERMISSIONS)
        command.add_argument("--timeout-min", type=float, default=0)
        command.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                             help="wait this long for the agent before returning")
        command.add_argument("--json", action="store_true")

    def cell_options(command: argparse.ArgumentParser, required: bool) -> None:
        command.add_argument("--size", choices=orch.SIZES, required=required)
        command.add_argument("--complexity", choices=orch.COMPLEXITIES, required=required)
        command.add_argument("--risk", choices=orch.RISKS, default="normal")

    catalog = commands.add_parser("catalog", help="what each provider can launch on this machine")
    catalog.add_argument("--json", action="store_true")

    select = commands.add_parser("select", help="show the route an agent would get, without starting it")
    select.add_argument("--role", required=True, choices=orch.ROLES)
    cell_options(select, False)
    select.add_argument("--tier", default="", choices=("", "strongest"))
    select.add_argument("--boost", default="off")
    select.add_argument("--reviewing", default="")
    select.add_argument("--run", default="")
    select.add_argument("--prefer", default="", choices=("", *orch.PREFERENCES))
    select.add_argument("--json", action="store_true")

    assess = commands.add_parser("assess", help="open a run and start its assessor")
    task = assess.add_mutually_exclusive_group(required=True)
    task.add_argument("--task-file")
    task.add_argument("--task")
    assess.add_argument("--boost", default="off", help="assessor effort: xhigh, max, ultra, or off")
    assess.add_argument("--host", default="", help="the host the root runs on, for the record")
    route_options(assess)

    lead = commands.add_parser("lead", help="start the matrix lead for an assessed run")
    lead.add_argument("--run", default="")
    cell_options(lead, False)
    route_options(lead)

    agent = commands.add_parser("agent", help="start a worker or consultant for the lead's packet")
    agent.add_argument("--run", default="")
    agent.add_argument("--role", required=True, choices=("worker", "consultant"))
    agent.add_argument("--purpose", default="substantive", choices=orch.PURPOSES)
    cell_options(agent, True)
    agent.add_argument("--tier", default="", choices=("", "strongest"))
    agent.add_argument("--reviewing", default="", help="the agent whose work an independent review covers")
    packet = agent.add_mutually_exclusive_group(required=True)
    packet.add_argument("--packet-file")
    packet.add_argument("--packet")
    agent.add_argument("--name", default="")
    route_options(agent)

    wait = commands.add_parser("wait", help="wait for agents, or for the whole run")
    wait.add_argument("agents", nargs="*")
    wait.add_argument("--run", default="")
    wait.add_argument("--timeout", type=float, default=540)
    wait.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="every agent of a run")
    status.add_argument("--run", default="")
    status.add_argument("--json", action="store_true")

    result = commands.add_parser("result", help="an agent's final report (default: the run's lead)")
    result.add_argument("agent", nargs="?", default="")
    result.add_argument("--run", default="")

    stop = commands.add_parser("stop", help="stop agents")
    stop.add_argument("agents", nargs="*")
    stop.add_argument("--all", action="store_true")
    stop.add_argument("--run", default="")

    mode = commands.add_parser("hermes-mode", help="orchestrator mode for Hermes sessions in this project")
    mode.add_argument("state", nargs="?", default="status", choices=("on", "off", "status"))

    supervise = commands.add_parser("_supervise")
    supervise.add_argument("directory")
    return parser


def _project(args: argparse.Namespace, environ: Mapping[str, str]) -> Path:
    return Path(args.cwd or environ.get("SYMPHONY_ORCHESTRATION_PROJECT") or os.getcwd()).resolve()


def _run_directory(project: Path, environ: Mapping[str, str], run_id: str) -> Path:
    # An agent inside a run carries its run id; a root names it. Either still
    # finds the run after changing directory.
    run_id = run_id or environ.get("SYMPHONY_ORCHESTRATION_RUN") or ""
    try:
        return orch.run_dir(project, environ, run_id)
    except orch.OrchestrationError:
        if orch.RUN_ID.fullmatch(run_id):
            found = [path.parent for path in
                     (orch.state_root(environ) / "orchestration").glob(f"*/runs/{run_id}/run.json")]
            if len(found) == 1:
                return found[0]
        raise


def _read_text(inline: str | None, file: str | None, what: str) -> str:
    if file:
        try:
            text = Path(file).read_text(encoding="utf-8")
        except OSError as error:
            raise orch.OrchestrationError(f"cannot read the {what} file: {error}") from error
    else:
        text = inline or ""
    if not text.strip():
        raise orch.OrchestrationError(f"the {what} is empty")
    return text


def _selection(args: argparse.Namespace, role: str, environ: Mapping[str, str], *,
               assessment: Assessment | None = None, tier: str = "", boost: str = "off",
               avoid: str = "", directory: Path | None = None) -> dict[str, Any]:
    if args.provider or args.model or args.effort:
        if not args.provider:
            raise orch.OrchestrationError("name --provider together with --model or --effort")
        return orch.explicit_selection(args.provider, args.model, args.effort, args.why, role,
                                       assessment=assessment, tier=tier, boost=boost,
                                       environ=environ, probe=True)
    running = orch.running_by_provider(directory) if directory else {}
    launched = orch.launched_by_provider(directory, role) if directory else {}
    return orch.select(role, assessment=assessment, tier=tier, boost=boost, avoid_provider=avoid,
                       running=running, launched=launched, rotation=directory.name if directory else "",
                       prefer=args.prefer, environ=environ, probe=True)


def _after_launch(args: argparse.Namespace, directory: Path, meta: dict[str, Any], out: TextIO) -> tuple[dict[str, Any], int]:
    """Report the launch; a start nobody asked to wait for has succeeded."""
    if args.wait > 0:
        meta = orch.wait(directory, [meta["id"]], args.wait)[0]
    code = _code([meta])
    if code == RUNNING and args.wait <= 0:
        code = STARTED
    if not args.json:
        out.write(f"run {directory.name}\n{orch.describe(meta)}\nselection: {meta.get('selection', '')}\n")
    return meta, code


def _code(items: list[dict[str, Any]]) -> int:
    if any(item.get("state") not in orch.TERMINAL for item in items):
        return RUNNING
    return DONE if all(item.get("state") == "completed" for item in items) else UNSUCCESSFUL


def _record_assessment(directory: Path) -> Assessment | None:
    """Carry a finished assessor's sizing into the run."""
    run = orch.load_run(directory)
    recorded = orch.valid_assessment(run.get("assessment"))
    if recorded:
        return recorded
    if not run.get("assessor"):
        return None
    meta = orch.agent_meta(directory, str(run["assessor"]))
    assessment = orch.valid_assessment((meta.get("markers") or {}).get("SYMPHONY_ASSESSMENT"))
    if assessment and meta.get("state") == "completed":
        orch.update_run(directory, assessment={
            "size": assessment.size, "complexity": assessment.complexity, "risk": assessment.risk,
            "rationale": assessment.rationale, "topology": assessment.topology, "source": "assessor"})
        return assessment
    return None


def _assess(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    project = _project(args, environ)
    task = _read_text(args.task, args.task_file, "task")
    boost = "off" if args.boost in {"", "off", "reset"} else args.boost
    if boost != "off" and boost not in {"xhigh", "max", "ultra"}:
        raise orch.OrchestrationError("--boost accepts xhigh, max, ultra or off")
    selection = _selection(args, "assessor", environ, boost=boost)
    run = orch.new_run(project, environ, task, args.host)
    directory = orch.run_dir(project, environ, run["run_id"])
    meta = orch.launch(project, environ, directory, role="assessor", selection=selection,
                       prompt=orch.assessor_prompt(task), permission=args.permission,
                       timeout_minutes=args.timeout_min)
    orch.update_run(directory, assessor=meta["id"])
    meta, code = _after_launch(args, directory, meta, out)
    command = orch.launcher_command(environ)
    assessment = _record_assessment(directory) if code == DONE else None
    if args.json:
        out.write(json.dumps({"run": directory.name, "agent": meta, "assessment": orch.load_run(directory)
                              .get("assessment") or None}, indent=2) + "\n")
        return max(code, DONE) if code != DONE or assessment else UNSUCCESSFUL
    if code in {RUNNING, STARTED}:
        out.write(f"{'still running' if code == RUNNING else 'started'}: "
                  f"{command} wait --run {directory.name} --timeout 540\n")
    elif code == UNSUCCESSFUL:
        out.write(f"the assessor did not finish: {command} result {meta['id']} --run {directory.name}\n")
    elif assessment:
        out.write(f"assessment: size={assessment.size} complexity={assessment.complexity} "
                  f"risk={assessment.risk}\nrationale: {assessment.rationale}\n"
                  f"topology: {assessment.topology}\nnext: {command} lead --run {directory.name} --wait 540\n")
    else:
        out.write("the assessor returned no valid SYMPHONY_ASSESSMENT line.\n"
                  f"read it: {command} result {meta['id']} --run {directory.name}\n"
                  "then assess again, or start the lead with --size, --complexity and --why.\n")
        return UNSUCCESSFUL
    return max(code, DONE)


def _lead(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    project = _project(args, environ)
    directory = _run_directory(project, environ, args.run)
    run = orch.load_run(directory)
    if run.get("lead"):
        existing = orch.agent_meta(directory, str(run["lead"]))
        if existing.get("state") not in orch.TERMINAL:
            raise orch.OrchestrationError(f"lead {existing['id']} is already running in run {directory.name}")
    if args.size or args.complexity:
        if not (args.size and args.complexity and args.why.strip()):
            raise orch.OrchestrationError("a root-supplied assessment needs --size, --complexity and --why")
        assessment = Assessment(args.size, args.complexity, args.risk, "root-supplied: " + args.why.strip())
        orch.update_run(directory, assessment={
            "size": assessment.size, "complexity": assessment.complexity, "risk": assessment.risk,
            "rationale": assessment.rationale, "topology": "", "source": "root"})
    else:
        assessment = _record_assessment(directory)
        if not assessment:
            raise orch.OrchestrationError(
                f"run {directory.name} has no accepted assessment yet; wait for the assessor, "
                "or pass --size, --complexity and --why")
    route = route_for(assessment)
    project = Path(run.get("project") or project)
    try:
        task = (directory / "task.md").read_text(encoding="utf-8")
    except OSError as error:
        raise orch.OrchestrationError(f"the run's task is no longer readable: {error}") from error
    selection = _selection(args, "lead", environ, assessment=assessment, directory=directory)
    recorded = orch.load_run(directory)["assessment"]
    prompt = orch.lead_prompt(task, directory.name, recorded, {
        "consultation": route.consultation, "independent_review": route.independent_review,
    }, orch.launcher_command(environ))
    meta = orch.launch(project, environ, directory, role="lead", selection=selection, prompt=prompt,
                       permission=args.permission, timeout_minutes=args.timeout_min)
    orch.update_run(directory, lead=meta["id"])
    meta, code = _after_launch(args, directory, meta, out)
    if args.json:
        out.write(json.dumps({"run": directory.name, "agent": meta}, indent=2) + "\n")
    elif code in {RUNNING, STARTED}:
        out.write(f"{'still running' if code == RUNNING else 'started'}: "
                  f"{orch.launcher_command(environ)} wait --run {directory.name} --timeout 540\n")
    else:
        out.write(f"report: {orch.launcher_command(environ)} result --run {directory.name}\n")
    return max(code, DONE)


def _agent(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    project = _project(args, environ)
    directory = _run_directory(project, environ, args.run)
    packet = _read_text(args.packet, args.packet_file, "packet")
    assessment = Assessment(args.size, args.complexity, args.risk)
    avoid = ""
    if args.reviewing:
        avoid = str(orch.agent_meta(directory, args.reviewing).get("provider") or "")
    elif args.purpose == "independent_review":
        # With no named work, stay independent of the lead that asked.
        parent = environ.get("SYMPHONY_ORCHESTRATION_AGENT") or ""
        if parent:
            avoid = str(orch.agent_meta(directory, parent).get("provider") or "")
    selection = _selection(args, args.role, environ, assessment=assessment, tier=args.tier,
                           avoid=avoid if args.purpose == "independent_review" else "", directory=directory)
    project = Path(orch.load_run(directory).get("project") or project)
    meta = orch.launch(project, environ, directory, role=args.role, selection=selection,
                       prompt=orch.agent_prompt(args.role, args.purpose, directory.name, packet),
                       purpose=args.purpose, name=args.name, permission=args.permission,
                       timeout_minutes=args.timeout_min, reviewing=args.reviewing)
    meta, code = _after_launch(args, directory, meta, out)
    if args.json:
        out.write(json.dumps({"run": directory.name, "agent": meta}, indent=2) + "\n")
    elif code in {RUNNING, STARTED}:
        out.write(f"wait: {orch.launcher_command(environ)} wait {meta['id']} --timeout 540\n")
    else:
        out.write(f"read: {orch.launcher_command(environ)} result {meta['id']}\n")
    return max(code, DONE)


def _wait(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    directory = _run_directory(_project(args, environ), environ, args.run)
    items = orch.wait(directory, list(args.agents), args.timeout)
    _record_assessment(directory)
    if args.json:
        out.write(json.dumps({"run": directory.name, "agents": items}, indent=2) + "\n")
    else:
        out.write(f"run {directory.name}\n" + "".join(orch.describe(item) + "\n" for item in items))
        if not items:
            out.write("no agents in this run yet\n")
    return _code(items)


def _status(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    directory = _run_directory(_project(args, environ), environ, args.run)
    run = orch.load_run(directory)
    items = orch.agents(directory)
    if args.json:
        out.write(json.dumps({"run": run, "agents": items}, indent=2) + "\n")
        return DONE
    out.write(f"run {directory.name}  {run.get('task_label', '')}\n")
    assessment = run.get("assessment") or {}
    if assessment:
        out.write(f"assessment: size={assessment.get('size')} complexity={assessment.get('complexity')} "
                  f"risk={assessment.get('risk')}\n")
    for item in items:
        out.write(orch.describe(item) + "\n    selection: " + str(item.get("selection", "")) + "\n")
    return DONE


def _result(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    directory = _run_directory(_project(args, environ), environ, args.run)
    run = orch.load_run(directory)
    agent_id = args.agent or run.get("lead") or run.get("assessor") or ""
    if not agent_id:
        raise orch.OrchestrationError(f"run {directory.name} has no agents yet")
    meta = orch.agent_meta(directory, str(agent_id))
    out.write(orch.describe(meta) + "\n")
    if meta.get("state") not in orch.TERMINAL:
        return RUNNING
    text = orch.result_text(directory, str(agent_id))
    out.write("\n" + (text.rstrip() or "(the agent left no final message)") + "\n")
    return DONE if meta.get("state") == "completed" else UNSUCCESSFUL


def _stop(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    directory = _run_directory(_project(args, environ), environ, args.run)
    targets = list(args.agents)
    if args.all:
        targets = [str(item["id"]) for item in orch.agents(directory) if item.get("state") not in orch.TERMINAL]
    if not targets:
        raise orch.OrchestrationError("name the agents to stop, or pass --all")
    for agent_id in targets:
        orch.request_stop(directory, agent_id)
    for item in orch.wait(directory, targets, 30):
        out.write(orch.describe(item) + "\n")
    return DONE


def _catalog(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    entries = orch.catalog(environ, probe=True)
    if args.json:
        out.write(json.dumps(entries, indent=2) + "\n")
        return DONE
    for entry in entries:
        if not entry["available"]:
            out.write(f"{entry['provider']}: unavailable ({entry.get('reason', '')})\n")
            continue
        out.write(f"{entry['provider']}: profile {entry['profile']}\n")
        for model in entry["models"]:
            cost = model["usd_per_million_blended"]
            price = f"${cost:.2f}/M blended" if isinstance(cost, (int, float)) else "no reviewed rate"
            out.write(f"  {model['model']}  rank {model['rank']}  {price}  efforts {', '.join(model['efforts'])}\n")
    return DONE


def _select(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    assessment = Assessment(args.size, args.complexity, args.risk) if args.size and args.complexity else None
    directory = None
    avoid = ""
    if args.run or args.reviewing:
        directory = _run_directory(_project(args, environ), environ, args.run)
        if args.reviewing:
            avoid = str(orch.agent_meta(directory, args.reviewing).get("provider") or "")
    choice = orch.select(args.role, assessment=assessment, tier=args.tier, boost=args.boost,
                         avoid_provider=avoid, running=orch.running_by_provider(directory) if directory else {},
                         launched=orch.launched_by_provider(directory, args.role) if directory else {},
                         rotation=directory.name if directory else "", prefer=args.prefer,
                         environ=environ, probe=True)
    if args.json:
        out.write(json.dumps(choice, indent=2) + "\n")
        return DONE
    out.write(f"{choice['provider']} {choice['model']}/{choice['effort']}\nwhy: {choice['why']}\n")
    for provider, reason in choice["unavailable"].items():
        out.write(f"unavailable: {provider}: {reason}\n")
    return DONE


def _mode(args: argparse.Namespace, environ: Mapping[str, str], out: TextIO) -> int:
    project = _project(args, environ)
    if args.state != "status":
        orch.set_hermes_mode(project, environ, args.state == "on")
    out.write("Symphony orchestrator mode: " + ("on" if orch.hermes_mode(project, environ) else "off") + "\n")
    return DONE


_COMMANDS = {"assess": _assess, "lead": _lead, "agent": _agent, "wait": _wait, "status": _status,
             "result": _result, "stop": _stop, "catalog": _catalog, "select": _select, "hermes-mode": _mode}


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    output: TextIO = out or sys.stdout
    errors: TextIO = err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:
        return REFUSED if stop.code else DONE
    if args.command == "_supervise":
        return orch.supervise(Path(args.directory), environ)
    try:
        return _COMMANDS[args.command](args, environ, output)
    except orch.OrchestrationError as error:
        errors.write(f"symphony: {error}\n")
        return REFUSED
    except (OSError, ValueError) as error:
        errors.write(f"symphony: {type(error).__name__}: {error}\n")
        return REFUSED
