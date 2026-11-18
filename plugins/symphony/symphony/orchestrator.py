"""Launch and track headless full agents on either provider.

Orchestrator mode keeps the root shallow. The strongest available assessor
sizes the task, then one lead owns it and delegates bounded packets to full
Codex or Claude Code agents. Every agent is an ordinary headless session of its
provider's own CLI, pinned to an exact model and effort and free to use that
host's subagents.

Provider, model and effort come from the same reviewed profiles and matrix the
native routes use. Both providers are ranked by one rule, so neither the host
the root happens to run on nor the order of this file decides who does the work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Mapping

from .routing import (
    Assessment,
    EFFORTS,
    NO_PROFILE,
    PROFILES_PATH,
    assessor_selection,
    profiles_for,
    resolve_tier,
    route_for,
    snapshot_for,
)


PROVIDERS = ("codex", "claude")
ROLES = ("assessor", "lead", "worker", "consultant")
PURPOSES = ("substantive", "independent_review")
SIZES = ("small", "medium", "large")
COMPLEXITIES = ("simple", "mixed", "complex")
RISKS = ("normal", "high")
PERMISSIONS = ("full-auto", "workspace-write", "read-only")
TERMINAL = frozenset({"completed", "failed", "timeout", "stopped", "lost"})

# A lead launches agents; those agents use their own host's subagents instead
# of launching further Symphony agents, so one task cannot fan out without bound.
MAX_DEPTH = 2
MAX_PARALLEL = 8
# Candidates within this factor of the cheapest count as equally priced.
COST_BAND = 1.5
# How two right-sized candidates are separated: spread the work, or buy cheapest.
PREFERENCES = ("balance", "price")
HEARTBEAT_SECONDS = 15
LOST_AFTER_SECONDS = 90
RETENTION_DAYS = 14
ROLE_TIMEOUT_MINUTES = {"assessor": 45, "lead": 480, "worker": 240, "consultant": 120}
_CLAUDE_PROBE_TTL = 7 * 24 * 3600
_CLAUDE_FAILED_PROBE_TTL = 3600
_MARKERS = ("SYMPHONY_ASSESSMENT", "SYMPHONY_OUTCOME", "SYMPHONY_DECISION")
RUN_ID = re.compile(r"r\d{14}-[0-9a-f]{6}")
_EXECUTION_RULE = {
    "small": "Delegate the substantive work to one worker.",
    "medium": "Delegate bounded worker packets; keep coordination, integration and verification yourself.",
    "large": "Delegate all project work in bounded packets; keep coordination, integration and verification yourself.",
}


class OrchestrationError(Exception):
    """A request the launcher refuses, with a message fit to show the agent."""


# --------------------------------------------------------------------------
# Paths and small file helpers


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("SYMPHONY_STATE_DIR") or Path.home() / ".symphony" / "state")


def project_key(project: Path) -> str:
    # The same canonical form the hook state uses, shortened for path limits.
    canonical = os.path.normcase(str(Path(project).resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def ledger_root(project: Path, environ: Mapping[str, str]) -> Path:
    return state_root(environ) / "orchestration" / project_key(project)


def launcher_script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "symphony_agent.py"


def launcher_command(environ: Mapping[str, str] = os.environ) -> str:
    """The launcher as one shell-neutral command prefix."""
    python = next((name for name in ("python3", "python") if shutil.which(name, path=environ.get("PATH"))), "")
    if not python:
        python = '"' + sys.executable + '"'
    return f'{python} "{environ.get("SYMPHONY_AGENT_CLI") or launcher_script()}"'


def _private_dir(path: Path) -> Path:
    # mkdir's mode covers only the last directory; each level made here is private.
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    return path


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_private(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------
# Catalog: what each provider can launch


def _policy() -> dict[str, Any]:
    return _read_json(PROFILES_PATH.parent / "model-policy.json")


def model_rank(model: str) -> int:
    rank = _policy().get("models", {}).get(model, {}).get("capability_rank")
    return rank if isinstance(rank, int) else -1


def model_cost(model: str) -> float | None:
    """Blended USD per million tokens, three parts input to one part output.

    Codex publishes credits and Claude publishes dollars. The reviewed policy
    states what one credit costs, so both land on one scale; a model with no
    reviewed rate has no cost and never wins on price.
    """
    policy = _policy()
    entry = policy.get("models", {}).get(model, {})
    usd = entry.get("api_usd_per_million_tokens")
    scale = 1.0
    if not isinstance(usd, Mapping):
        usd = entry.get("codex_credits_per_million_tokens")
        scale = policy.get("cross_provider_cost", {}).get("codex_credit_usd")
    try:
        return (3 * float(usd["input"]) + float(usd["output"])) / 4 * float(scale)
    except (TypeError, ValueError, KeyError):
        return None


def cli_path(provider: str, environ: Mapping[str, str] = os.environ) -> str:
    override = environ.get(f"SYMPHONY_{provider.upper()}_CLI")
    return override or shutil.which(provider, path=environ.get("PATH")) or ""


def _claude_probe_cache(environ: Mapping[str, str]) -> Path:
    return state_root(environ) / "orchestration" / "claude-entitlement.json"


# The launcher reads entitlement the way the hook runtime does, from the same
# sources. It keeps its own copy of these few lines so that it runs without the
# hook runtime, which the Hermes package does not ship.
_CLAUDE_PROBE_MODELS = ("claude-sonnet-5", "claude-sonnet-5-5", "claude-opus-5-5")


def _codex_roster(environ: Mapping[str, str]) -> set[str] | None:
    home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    roster = _read_json(home / "models_cache.json")
    if not roster:
        return None
    return {str(item.get("slug")) for item in roster.get("models", ())
            if isinstance(item, Mapping) and item.get("visibility") == "list" and item.get("slug")}


def _claude_accepts(model: str, environ: Mapping[str, str]) -> bool:
    """Ask this Claude Code login, then verify the model that actually served."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed arguments, the user's own CLI
            [cli_path("claude", environ), "--settings", '{"disableAllHooks":true}', "--print",
             "--no-session-persistence", "--tools", "", "--disallowedTools", "mcp__*",
             "--system-prompt", "Reply ok.", "--model", model, "--max-budget-usd", "0.25",
             "--output-format", "json", "Reply ok."],
            capture_output=True, text=True, timeout=30,
            env={**environ, "SYMPHONY_CLAUDE_PROBE": "1", "SYMPHONY_ORCHESTRATED": "1"},
        )
        if completed.returncode:
            return False
        result = json.loads(completed.stdout)
        return (isinstance(result, dict) and not result.get("is_error")
                and set(result.get("modelUsage", {})) == {model})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return False


def entitlement(provider: str, environ: Mapping[str, str], *, probe: bool = False) -> set[str] | None:
    """The models this account grants, or None when nothing can tell."""
    if provider == "codex":
        return _codex_roster(environ)
    explicit = {model.strip() for model in environ.get("SYMPHONY_CLAUDE_AVAILABLE_MODELS", "").split(",")
                if model.strip()}
    if explicit:
        return explicit
    cached = _read_json(_claude_probe_cache(environ))
    models = cached.get("models")
    checked = cached.get("checked_at")
    if isinstance(models, list) and isinstance(checked, (int, float)):
        ttl = _CLAUDE_PROBE_TTL if models else _CLAUDE_FAILED_PROBE_TTL
        if time.time() - checked < ttl:
            return {str(model) for model in models}
    if not probe or environ.get("SYMPHONY_ORCHESTRATOR_NO_PROBE") == "1" or not cli_path("claude", environ):
        return None
    # One short call per model, with this login; Fable stays opt-in through
    # SYMPHONY_CLAUDE_AVAILABLE_MODELS exactly as on the native route.
    found = {model for model in _CLAUDE_PROBE_MODELS if _claude_accepts(model, environ)}
    try:
        _private_dir(_claude_probe_cache(environ).parent)
        _write_json(_claude_probe_cache(environ), {"checked_at": time.time(), "models": sorted(found)})
    except OSError:
        pass
    return found


def applied_profile(provider: str, environ: Mapping[str, str], *, probe: bool = False) -> str:
    """The shipped profile this account can run; '' is the unknown-entitlement floor."""
    pinned = environ.get(f"SYMPHONY_{provider.upper()}_PROFILE")
    if pinned:
        return pinned
    entitled = entitlement(provider, environ, probe=probe)
    if entitled is None:
        return ""
    for profile in profiles_for(provider):
        required_all = {str(item) for item in profile.get("requires_all", ())}
        required_any = {str(item) for item in profile.get("requires_any", ())}
        selected = {str(choice["model"]) for choice in profile.get("matrix", {}).values()}
        if required_all and not required_all <= entitled:
            continue
        if required_any and not required_any & entitled:
            continue
        if not selected <= entitled:
            continue
        return str(profile["id"])
    return NO_PROFILE


def catalog(environ: Mapping[str, str] = os.environ, *, probe: bool = False) -> list[dict[str, Any]]:
    """Each provider's launchable state and applied profile."""
    entries = []
    for provider in PROVIDERS:
        executable = cli_path(provider, environ)
        profile = applied_profile(provider, environ, probe=probe) if executable else NO_PROFILE
        entry: dict[str, Any] = {"provider": provider, "cli": executable, "profile": profile or "(floor)",
                                 "available": bool(executable) and profile != NO_PROFILE, "models": []}
        if not executable:
            entry["reason"] = f"the {provider} CLI is not on PATH"
        elif profile == NO_PROFILE:
            entry["reason"] = ("no model answered this login; is the CLI signed in?" if provider == "claude"
                               else "this account's model roster matches no shipped profile")
        else:
            snapshot = snapshot_for(provider, profile or None)
            entry["tiers"] = dict(snapshot.tiers)
            entry["models"] = [
                {"model": model, "rank": model_rank(model), "usd_per_million_blended": model_cost(model),
                 "efforts": list(snapshot.supported_efforts.get(model, ()))}
                for model in snapshot.available_models
            ]
        entries.append(entry)
    return entries


# --------------------------------------------------------------------------
# Selection: one rule for both providers


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    effort: str
    profile: str
    rank: int
    cost: float | None
    shortfall: int = 0
    effort_gap: int = 0
    running: int = 0
    launched: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _effort_gap(requested: str, actual: str) -> int:
    if requested not in EFFORTS or actual not in EFFORTS:
        return 1 if requested != actual else 0
    return max(0, EFFORTS.index(requested) - EFFORTS.index(actual))


def _candidate(provider: str, role: str, assessment: Assessment | None, tier: str, boost: str,
               environ: Mapping[str, str], probe: bool) -> Candidate | str:
    """This provider's own choice for the request, or why it has none."""
    if not cli_path(provider, environ):
        return f"the {provider} CLI is not on PATH"
    profile = applied_profile(provider, environ, probe=probe)
    if profile == NO_PROFILE:
        return ("no Claude model answered this login" if provider == "claude"
                else "the account roster matches no shipped profile")
    snapshot = snapshot_for(provider, profile or None)
    best = snapshot_for(provider, profiles_for(provider)[0]["id"])
    if role == "assessor" or tier == "strongest":
        requested = boost if role == "assessor" and boost not in {"", "off"} else "high"
        chosen = assessor_selection(snapshot, requested if role == "assessor" else "off")
        model = chosen["model"]
        supported = snapshot.supported_efforts.get(model, ())
        effort = chosen["effort"] or ("high" if "high" in supported else (supported[-1] if supported else ""))
        intended = best.tiers.get("strongest", "")
    else:
        if assessment is None:
            return "a size and complexity are required for this role"
        route = route_for(assessment)
        resolved = resolve_tier(route, snapshot)
        model, effort = str(resolved["lead_model"]), str(resolved["lead_effort"])
        requested = str(resolve_tier(route, best)["lead_effort"])
        intended = str(resolve_tier(route, best)["lead_model"])
    if not model or not effort:
        return "the applied profile names no model for this request"
    reasons = [f"profile {profile or 'floor'}"]
    shortfall = max(0, model_rank(intended) - model_rank(model))
    if shortfall:
        reasons.append(f"account limits this to {model} where the policy intends {intended}")
    gap = _effort_gap(requested, effort)
    if gap:
        reasons.append(f"effort {effort} where {requested} was requested")
    return Candidate(provider=provider, model=model, effort=effort, profile=profile or "floor",
                     rank=model_rank(model), cost=model_cost(model), shortfall=shortfall,
                     effort_gap=gap, reasons=tuple(reasons))


def preference(environ: Mapping[str, str], requested: str = "") -> str:
    chosen = requested or environ.get("SYMPHONY_ORCHESTRATION_PREFER") or PREFERENCES[0]
    if chosen not in PREFERENCES:
        raise OrchestrationError(f"unknown preference {chosen!r}; use one of {', '.join(PREFERENCES)}")
    return chosen


def select(role: str, *, assessment: Assessment | None = None, tier: str = "", boost: str = "off",
           avoid_provider: str = "", running: Mapping[str, int] | None = None,
           launched: Mapping[str, int] | None = None, rotation: str = "", prefer: str = "",
           environ: Mapping[str, str] = os.environ, probe: bool = False) -> dict[str, Any]:
    """Rank both providers for one request by the same criteria.

    Order: an independent reviewer sits on the other provider than the work it
    reviews; then the smallest capability shortfall against the reviewed policy
    (for the assessor, simply the strongest model); then the smallest effort
    shortfall. Each provider's matrix already names its right-sized model for
    the cell, so candidates that survive this far are equals. They are
    separated by spreading the work: the provider running fewer of this run's
    agents, then the one given fewer so far, then a rotation derived from the
    run, so the first choice does not favour one name. With the `price`
    preference, price comes before spreading, and anything within COST_BAND of
    the cheapest counts as equal.
    """
    if role not in ROLES:
        raise OrchestrationError(f"unknown role {role!r}; use one of {', '.join(ROLES)}")
    prefer = preference(environ, prefer)
    running = running or {}
    launched = launched or {}
    found: list[Candidate] = []
    unavailable: dict[str, str] = {}
    for provider in PROVIDERS:
        outcome = _candidate(provider, role, assessment, tier, boost, environ, probe)
        if isinstance(outcome, str):
            unavailable[provider] = outcome
        else:
            found.append(Candidate(**{**asdict(outcome), "running": int(running.get(provider, 0)),
                                      "launched": int(launched.get(provider, 0)),
                                      "reasons": outcome.reasons}))
    if not found:
        raise OrchestrationError("no provider can launch this agent: " + "; ".join(
            f"{provider}: {reason}" for provider, reason in unavailable.items()))
    priced = [item.cost for item in found if item.cost]
    cheapest = min(priced) if priced else None

    def band(item: Candidate) -> int:
        if not cheapest or not item.cost:
            return 0 if not priced else 99
        return int(math.log(max(item.cost / cheapest, 1.0), COST_BAND) + 1e-9)

    def turn(item: Candidate) -> str:
        return hashlib.sha256(f"{rotation}:{role}:{item.provider}".encode()).hexdigest()

    def criteria(item: Candidate) -> list[tuple[str, Any]]:
        fit = -item.rank if role == "assessor" else item.shortfall
        ordered_criteria: list[tuple[str, Any]] = [
            ("independent of the reviewed provider", item.provider == avoid_provider),
            ("capability fit", fit), ("effort fit", item.effort_gap)]
        if prefer == "price":
            ordered_criteria.append(("price", band(item)))
        ordered_criteria += [("fewer agents running", item.running),
                             ("fewer agents given so far", item.launched),
                             ("rotation between equal candidates", turn(item))]
        return ordered_criteria

    def key(item: Candidate):
        return tuple(value for _, value in criteria(item))

    ordered = sorted(found, key=key)
    chosen = ordered[0]
    why = list(chosen.reasons)
    if len(ordered) > 1:
        other = ordered[1]
        decided = next((label for (label, mine), (_, theirs) in zip(criteria(chosen), criteria(other))
                        if mine != theirs), "rotation between equal candidates")
        why.append(f"chosen over {other.provider} {other.model}/{other.effort} on {decided}")
    else:
        why.append("only available provider (" + "; ".join(
            f"{provider}: {reason}" for provider, reason in unavailable.items()) + ")")
    return {
        "provider": chosen.provider, "model": chosen.model, "effort": chosen.effort,
        "profile": chosen.profile, "why": "; ".join(why), "prefer": prefer,
        "candidates": [{**asdict(item), "reasons": list(item.reasons)} for item in ordered],
        "unavailable": unavailable,
    }


def explicit_selection(provider: str, model: str, effort: str, why: str, role: str, *,
                       assessment: Assessment | None, tier: str, boost: str,
                       environ: Mapping[str, str], probe: bool) -> dict[str, Any]:
    """A provider or route named by the caller, recorded with its reason."""
    if provider not in PROVIDERS:
        raise OrchestrationError(f"unknown provider {provider!r}; use codex or claude")
    if not why.strip():
        raise OrchestrationError("--why is required when the provider, model or effort is named explicitly")
    outcome = _candidate(provider, role, assessment, tier, boost, environ, probe)
    if isinstance(outcome, str):
        raise OrchestrationError(f"{provider} cannot launch this agent: {outcome}")
    snapshot = snapshot_for(provider, None if outcome.profile == "floor" else outcome.profile)
    chosen_model = model or outcome.model
    if chosen_model not in snapshot.available_models:
        raise OrchestrationError(f"{chosen_model} is not in this account's {provider} profile "
                                 f"({', '.join(snapshot.available_models)})")
    supported = snapshot.supported_efforts.get(chosen_model, ())
    chosen_effort = effort or (outcome.effort if chosen_model == outcome.model else "")
    if chosen_effort not in supported:
        raise OrchestrationError(f"{chosen_model} supports efforts {', '.join(supported)}; name one with --effort")
    return {"provider": provider, "model": chosen_model, "effort": chosen_effort, "profile": outcome.profile,
            "why": "explicit: " + why.strip(), "candidates": [], "unavailable": {}}


# --------------------------------------------------------------------------
# Prompts


def root_reminder(command: str) -> str:
    """The same route in one line, for a host that repeats guidance every turn."""
    return ("Symphony orchestrator mode is on: keep low-token work yourself and route every other task "
            f"through the launcher as before (`{command} assess --task-file <file> --wait 540`, then "
            "`lead --run <run> --wait 540`, `wait --run <run> --timeout 540` until exit 0, `result --run <run>`).")


def root_guidance(command: str, task: str = "") -> str:
    """What a root in orchestrator mode does with a task."""
    text = (
        "Symphony orchestrates this task through headless full agents. Stay a shallow root: keep low-token "
        "work (a question, one command, a quick lookup) yourself, and for everything else do not inspect, "
        "edit or run project work. Clarify unclear intent with the user first; agents cannot ask.\n"
        f"1. Assess: write the full request and acceptance checks to a file, then run `{command} assess "
        "--task-file <file> --wait 540`. It starts the strongest available assessor across Codex and Claude Code "
        "and prints the run id with the assessment.\n"
        f"2. Lead: run `{command} lead --run <run> --wait 540`. It starts the matrix lead for the assessed size "
        "and complexity. That lead delegates bounded packets to full agents on both providers, integrates and "
        "verifies.\n"
        f"3. Wait: exit code 3 means still running; repeat `{command} wait --run <run> --timeout 540` until it "
        "exits 0. Never poll faster or guess at progress.\n"
        f"4. Report: `{command} result --run <run>` prints the lead's outcome and `{command} status --run <run>` "
        "lists every agent with provider, model and effort. Relay the outcome and disclose anything unfinished.\n"
        "Give these commands a shell timeout of at least 600 seconds; agents keep running if a call is cut short.\n"
        "The launcher chooses provider, model and effort by one rule for both providers. Name --provider, "
        "--model or --effort only on the user's instruction, with --why."
    )
    if task:
        text += "\n\nTask reminder (full original request governs if clipped):\n" + task
    return text


def assessor_prompt(task: str) -> str:
    return (
        "SYMPHONY_ROLE: assessor\n"
        "You are the independent assessor of a Symphony orchestrated run. Assess the bounded task yourself: "
        "inspect the project read-only as far as sizing needs, but do not execute the task, edit files or "
        "start agents.\n"
        "Return size (small, medium or large), complexity (simple, mixed or complex), risk (normal or high), "
        "a short rationale, and an advisory topology: the bounded packets a lead should delegate and which "
        "of them are independent.\n"
        "End with exactly one JSON line, with your actual values, in this format:\n"
        'SYMPHONY_ASSESSMENT: {"size":"small","complexity":"simple","risk":"normal",'
        '"rationale":"bounded task","topology":"delegated"}\n\n'
        "Task:\n" + task
    )


def lead_prompt(task: str, run_id: str, assessment: Mapping[str, Any], route: Mapping[str, Any],
                command: str) -> str:
    size = str(assessment.get("size", ""))
    review = ("An independent review by a consultant is required before you report success."
              if route.get("independent_review") else
              f"Consultation is {route.get('consultation', 'none')}; request an independent review when the "
              "evidence leaves doubt.")
    return (
        "SYMPHONY_ROLE: lead\n"
        f"You are the lead of Symphony orchestrated run {run_id}. You own this task end to end: plan it, "
        "delegate bounded packets to full agents, integrate their results, run the acceptance checks "
        "yourself, and report.\n\n"
        f"Assessment: size {size}, complexity {assessment.get('complexity', '')}, risk "
        f"{assessment.get('risk', 'normal')}. Rationale: {assessment.get('rationale', '')}\n"
        f"Advisory topology: {assessment.get('topology', '')}\n"
        f"Execution: {_EXECUTION_RULE.get(size, _EXECUTION_RULE['medium'])} {review}\n\n"
        "Agents are full headless sessions on Codex or Claude Code. Start them only with the launcher, "
        "which chooses provider, model and effort for each packet by one rule for both providers and "
        "prints why. Your host's own subagents remain yours for read-only exploration.\n"
        f"- Worker: `{command} agent --role worker --purpose substantive --size <small|medium|large> "
        "--complexity <simple|mixed|complex> --packet-file <file> --name <short-name>`\n"
        f"- Reviewer: `{command} agent --role consultant --purpose independent_review --reviewing <agent-id> "
        "--size <...> --complexity <...> --packet-file <file>`; it runs on the other provider than the work.\n"
        f"- Hard question: add `--tier strongest` to a consultant for the strongest model.\n"
        f"- Wait: `{command} wait <agent-id>... --timeout 540`; exit 3 means still running, so call it again. "
        "Never poll faster. Give it a shell timeout of at least 600 seconds; agents keep running if a call "
        "is cut short.\n"
        f"- Read: `{command} result <agent-id>`; overview: `{command} status`.\n"
        "Size and complexity describe the packet, not the whole task. A packet file states objective, "
        "ownership (the files or area it may change), constraints, acceptance_check and return_contract. "
        f"Run independent packets in parallel (at most {MAX_PARALLEL}); run packets that touch the same files "
        "or need another's result in order. Name --provider, --model or --effort only for a stated reason, "
        "with --why.\n"
        "After agents return, integrate and verify with your own tools; an agent's own check cannot replace "
        "yours. Relaunch or follow up on missing work instead of doing a different agent's packet silently.\n"
        'End with exactly one line: SYMPHONY_OUTCOME: {"status":"completed"} when verified, otherwise '
        '{"status":"blocked"} or {"status":"failed"} with what is missing.\n\n'
        "Task:\n" + task
    )


def agent_prompt(role: str, purpose: str, run_id: str, packet: str) -> str:
    if role == "consultant":
        duty = ("Review only the supplied work. Do not change it. Return findings with evidence, what you "
                "could not check, and whether the work meets its acceptance checks."
                if purpose == "independent_review" else
                "Decide only the supplied question. Return a recommendation with evidence, uncertainty and "
                "consequences.")
    else:
        duty = ("Do the packet completely within its ownership and constraints, then run its acceptance "
                "check. Report what changed, the commands you ran with their results, and anything left "
                "unfinished.")
    return (
        f"SYMPHONY_ROLE: {role}\npurpose: {purpose}\n"
        f"You are a full agent working one bounded packet for the lead of Symphony orchestrated run {run_id}. "
        f"{duty} You may use your host's own subagents. Do not start Symphony agents; you cannot ask the user.\n"
        'End with exactly one line: SYMPHONY_OUTCOME: {"status":"completed"}, or {"status":"blocked"} or '
        '{"status":"failed"} with the reason.\n\n'
        "Packet:\n" + packet
    )


# --------------------------------------------------------------------------
# Ledger


def new_run(project: Path, environ: Mapping[str, str], task: str, host: str = "") -> dict[str, Any]:
    root = _private_dir(ledger_root(project, environ) / "runs")
    prune(project, environ)
    run_id = "r" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    directory = _private_dir(root / run_id)
    _private_dir(directory / "agents")
    # The lead needs the whole request after the assessor returns. It stays
    # with the run, readable only by this user, and leaves with the run.
    _write_private(directory / "task.md", task)
    label = next((line.strip() for line in task.splitlines() if line.strip()), "")[:120]
    run = {"run_id": run_id, "created_at": now(), "project": str(Path(project).resolve()),
           "task_label": label, "host": host, "assessment": {}, "assessor": "", "lead": ""}
    _write_json(directory / "run.json", run)
    _write_private(root / "LATEST", run_id)
    return run


def run_dir(project: Path, environ: Mapping[str, str], run_id: str = "") -> Path:
    root = ledger_root(project, environ) / "runs"
    run_id = run_id or environ.get("SYMPHONY_ORCHESTRATION_RUN") or ""
    if not run_id or run_id == "latest":
        try:
            run_id = (root / "LATEST").read_text(encoding="utf-8").strip()
        except OSError:
            run_id = ""
    if not RUN_ID.fullmatch(run_id or ""):
        raise OrchestrationError("no orchestrated run found for this project; start one with `assess`")
    directory = root / run_id
    if not (directory / "run.json").is_file():
        raise OrchestrationError(f"run {run_id} does not exist in this project")
    return directory


def load_run(directory: Path) -> dict[str, Any]:
    return _read_json(directory / "run.json")


def update_run(directory: Path, **changes: Any) -> dict[str, Any]:
    run = {**load_run(directory), **changes}
    _write_json(directory / "run.json", run)
    return run


def agent_meta(directory: Path, agent_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z]+-[a-z]+-[0-9a-f]{8}", agent_id or ""):
        raise OrchestrationError(f"{agent_id!r} is not an agent id from this run")
    meta = _read_json(directory / "agents" / agent_id / "meta.json")
    if not meta:
        raise OrchestrationError(f"agent {agent_id} does not exist in run {directory.name}")
    return _settle(directory / "agents" / agent_id, meta)


def _settle(agent_directory: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Report a supervisor that vanished instead of a run that never ends."""
    if meta.get("state") in TERMINAL:
        return meta
    beat = meta.get("heartbeat_at") or meta.get("created_at")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(beat))).total_seconds()
    except ValueError:
        age = LOST_AFTER_SECONDS + 1
    if age > LOST_AFTER_SECONDS:
        meta = {**meta, "state": "lost", "finished_at": now(),
                "error": "the supervising process stopped reporting; the agent's fate is unknown"}
        try:
            _write_json(agent_directory / "meta.json", meta)
        except OSError:
            pass
    return meta


def agents(directory: Path) -> list[dict[str, Any]]:
    found = []
    for path in sorted((directory / "agents").glob("*/meta.json")):
        meta = _read_json(path)
        if meta:
            found.append(_settle(path.parent, meta))
    return sorted(found, key=lambda item: str(item.get("created_at", "")))


def prune(project: Path, environ: Mapping[str, str]) -> None:
    """Drop finished runs past retention; agent output is not kept forever."""
    root = ledger_root(project, environ) / "runs"
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        candidates = [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError:
        return
    for directory in candidates:
        try:
            if directory.stat().st_mtime > cutoff:
                continue
            if any(item.get("state") not in TERMINAL for item in agents(directory)):
                continue
            shutil.rmtree(directory)
        except OSError:
            continue


# --------------------------------------------------------------------------
# Launching


def agent_command(meta: Mapping[str, Any], agent_directory: Path) -> list[str]:
    """The provider CLI invocation for one agent; the prompt arrives on stdin."""
    provider, model, effort = meta["provider"], meta["model"], meta["effort"]
    permission = meta.get("permission") or "full-auto"
    executable = meta.get("cli") or provider
    if provider == "codex":
        sandbox = {"full-auto": "danger-full-access", "workspace-write": "workspace-write",
                   "read-only": "read-only"}[permission]
        return [executable, "exec", "--skip-git-repo-check", "-m", model,
                # Unquoted: Codex takes a non-TOML value literally, and no shell or
                # Windows command shim has quotes to re-parse.
                "-c", f"model_reasoning_effort={effort}", "-C", meta["cwd"], "-s", sandbox,
                "--json", "-o", str(agent_directory / "last_message.txt"), "-"]
    mode = {"full-auto": "bypassPermissions", "workspace-write": "acceptEdits", "read-only": "plan"}[permission]
    return [executable, "-p", "--model", model, "--effort", effort, "--output-format", "json",
            "--permission-mode", mode]


def launch(project: Path, environ: Mapping[str, str], directory: Path, *, role: str, selection: Mapping[str, Any],
           prompt: str, purpose: str = "", name: str = "", permission: str = "full-auto",
           timeout_minutes: float = 0, reviewing: str = "", spawn: bool = True) -> dict[str, Any]:
    """Record one agent and hand it to a detached supervisor."""
    if permission not in PERMISSIONS:
        raise OrchestrationError(f"unknown permission {permission!r}; use one of {', '.join(PERMISSIONS)}")
    depth = int(environ.get("SYMPHONY_ORCHESTRATION_DEPTH") or 0)
    limit = int(environ.get("SYMPHONY_ORCHESTRATION_MAX_DEPTH") or MAX_DEPTH)
    if depth >= limit:
        raise OrchestrationError(
            "this agent cannot start Symphony agents; use your host's own subagents, or report the "
            "need to the lead")
    active = [item for item in agents(directory) if item.get("state") not in TERMINAL]
    cap = int(environ.get("SYMPHONY_ORCHESTRATION_MAX_PARALLEL") or MAX_PARALLEL)
    if len(active) >= cap:
        raise OrchestrationError(f"{len(active)} agents are already running in this run (limit {cap}); "
                                 "wait for one to finish")
    provider = str(selection["provider"])
    agent_id = f"{role}-{provider}-{secrets.token_hex(4)}"
    agent_directory = _private_dir(directory / "agents" / agent_id)
    meta = {
        "id": agent_id, "run_id": directory.name, "role": role, "purpose": purpose,
        "name": re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:60], "provider": provider,
        "model": selection["model"], "effort": selection["effort"], "profile": selection.get("profile", ""),
        "selection": selection.get("why", ""), "permission": permission, "reviewing": reviewing,
        "cwd": str(Path(project).resolve()), "cli": cli_path(provider, environ) or provider,
        "depth": depth + 1, "parent": environ.get("SYMPHONY_ORCHESTRATION_AGENT", ""),
        "timeout_minutes": float(timeout_minutes or ROLE_TIMEOUT_MINUTES.get(role, 240)),
        "state": "starting", "created_at": now(), "heartbeat_at": now(),
    }
    _write_json(agent_directory / "meta.json", meta)
    _write_private(agent_directory / "prompt.md", prompt)
    if spawn:
        _spawn_supervisor(agent_directory, environ)
    return meta


def _spawn_supervisor(agent_directory: Path, environ: Mapping[str, str]) -> None:
    argv = [sys.executable, str(launcher_script()), "_supervise", str(agent_directory)]
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                               "stderr": subprocess.DEVNULL, "env": dict(environ), "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                                    | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        options["start_new_session"] = True
    subprocess.Popen(argv, **options)  # noqa: S603 - fixed interpreter and script


def child_environment(meta: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    """Mark the session as orchestrated so its own Symphony hooks stay inert."""
    child = dict(environ)
    child.update({
        "SYMPHONY_ORCHESTRATED": "1",
        "SYMPHONY_ORCHESTRATION_RUN": str(meta["run_id"]),
        "SYMPHONY_ORCHESTRATION_AGENT": str(meta["id"]),
        "SYMPHONY_ORCHESTRATION_DEPTH": str(meta.get("depth", 1)),
        "SYMPHONY_ORCHESTRATION_PROJECT": str(meta["cwd"]),
        "SYMPHONY_AGENT_CLI": str(launcher_script()),
    })
    return child


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=20)  # noqa: S603,S607
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def markers(text: str) -> dict[str, Any]:
    """The last assessment and outcome, and every decision, an agent reported."""
    found: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip().strip("`")
        for marker in _MARKERS:
            if not stripped.startswith(marker + ":"):
                continue
            try:
                value = json.loads(stripped[len(marker) + 1:].strip())
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            if marker == "SYMPHONY_DECISION":
                found.setdefault(marker, []).append(value)
            else:
                found[marker] = value
    return found


def valid_assessment(value: Any) -> Assessment | None:
    if not isinstance(value, Mapping):
        return None
    size, complexity, risk = value.get("size"), value.get("complexity"), value.get("risk", "normal")
    if size not in SIZES or complexity not in COMPLEXITIES or risk not in RISKS:
        return None
    return Assessment(str(size), str(complexity), str(risk), str(value.get("rationale", ""))[:2000],
                      str(value.get("topology", ""))[:4000])


def _collect(meta: dict[str, Any], agent_directory: Path, exit_code: int | None) -> dict[str, Any]:
    """Read what the provider CLI left behind into the agent's record."""
    result = ""
    details: dict[str, Any] = {}
    try:
        output = (agent_directory / "stdout.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    failed = exit_code != 0
    if meta["provider"] == "codex":
        try:
            result = (agent_directory / "last_message.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            result = ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                details["session"] = str(event["thread_id"])
            elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                details["usage"] = event["usage"]
            elif event.get("type") in {"error", "turn.failed"}:
                message = event.get("message") or (event.get("error") or {}).get("message")
                if message:
                    details["error"] = str(message)[:1000]
    else:
        try:
            document = json.loads(output)
        except ValueError:
            document = {}
        if isinstance(document, dict):
            result = str(document.get("result") or "")
            failed = failed or bool(document.get("is_error"))
            for source, target in (("session_id", "session"), ("usage", "usage"),
                                   ("total_cost_usd", "cost_usd"), ("modelUsage", "model_usage")):
                if document.get(source) is not None:
                    details[target] = document[source]
            if document.get("is_error") and result:
                details["error"] = result[:1000]
        else:
            result = output
        if result:
            try:
                _write_private(agent_directory / "last_message.txt", result)
            except OSError:
                pass
    if failed and "error" not in details:
        try:
            tail = (agent_directory / "stderr.log").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            tail = ""
        details["error"] = (tail or result.strip() or output.strip())[-1000:] or f"exit code {exit_code}"
    return {**meta, **details, "markers": markers(result), "exit_code": exit_code,
            "state": "failed" if failed else "completed"}


def supervise(agent_directory: Path, environ: Mapping[str, str] = os.environ) -> int:
    """Run one agent to its end and record what happened. Runs detached."""
    agent_directory = Path(agent_directory)
    meta_path = agent_directory / "meta.json"
    meta = _read_json(meta_path)
    if not meta or meta.get("state") != "starting":
        return 2
    prompt_path = agent_directory / "prompt.md"
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except OSError:
        _write_json(meta_path, {**meta, "state": "failed", "finished_at": now(),
                                "error": "the agent's prompt could not be read"})
        return 1
    options: dict[str, Any] = {"stdin": subprocess.PIPE, "cwd": meta["cwd"],
                               "env": child_environment(meta, environ)}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        with open(agent_directory / "stdout.log", "wb") as stdout, \
                open(agent_directory / "stderr.log", "wb") as stderr:
            for log in (agent_directory / "stdout.log", agent_directory / "stderr.log"):
                try:
                    log.chmod(0o600)
                except OSError:
                    pass
            process = subprocess.Popen(agent_command(meta, agent_directory), stdout=stdout, stderr=stderr,
                                       **options)  # noqa: S603 - argv built from validated selection
            meta = {**meta, "state": "running", "pid": process.pid, "started_at": now(), "heartbeat_at": now()}
            _write_json(meta_path, meta)
            try:
                if process.stdin is not None:
                    process.stdin.write(prompt.encode("utf-8"))
                    process.stdin.close()
            except OSError:
                pass
            # The prompt carries the user's request; it is not kept once delivered.
            try:
                prompt_path.unlink()
            except OSError:
                pass
            deadline = time.monotonic() + float(meta.get("timeout_minutes") or 240) * 60
            beat = time.monotonic()
            ended = ""
            while True:
                try:
                    process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if (agent_directory / "stop.request").exists():
                    ended = "stopped"
                elif time.monotonic() > deadline:
                    ended = "timeout"
                if ended:
                    _kill_tree(process)
                    break
                if time.monotonic() - beat >= HEARTBEAT_SECONDS:
                    beat = time.monotonic()
                    meta = {**meta, "heartbeat_at": now()}
                    _write_json(meta_path, meta)
    except OSError as error:
        _write_json(meta_path, {**meta, "state": "failed", "finished_at": now(),
                                "error": f"could not start {meta.get('cli')}: {error}"})
        return 1
    final = _collect(meta, agent_directory, process.returncode)
    if ended:
        final["state"] = ended
        final["error"] = ("stopped on request" if ended == "stopped"
                          else f"exceeded its {meta.get('timeout_minutes'):g} minute limit")
    final["finished_at"] = now()
    final["heartbeat_at"] = final["finished_at"]
    _write_json(meta_path, final)
    return 0 if final["state"] == "completed" else 1


def request_stop(directory: Path, agent_id: str) -> dict[str, Any]:
    meta = agent_meta(directory, agent_id)
    if meta.get("state") in TERMINAL:
        return meta
    _write_private(directory / "agents" / agent_id / "stop.request", now())
    return meta


def wait(directory: Path, agent_ids: list[str], timeout: float) -> list[dict[str, Any]]:
    """Block until the named agents (or the whole run) finish, or time runs out."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        current = ([agent_meta(directory, agent_id) for agent_id in agent_ids] if agent_ids
                   else agents(directory))
        if all(item.get("state") in TERMINAL for item in current) or time.monotonic() >= deadline:
            return current
        time.sleep(1.0)


def result_text(directory: Path, agent_id: str) -> str:
    agent_meta(directory, agent_id)
    try:
        return (directory / "agents" / agent_id / "last_message.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def running_by_provider(directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in agents(directory):
        if item.get("state") not in TERMINAL:
            counts[str(item.get("provider"))] = counts.get(str(item.get("provider")), 0) + 1
    return counts


def launched_by_provider(directory: Path, role: str = "") -> dict[str, int]:
    """How many of this run's agents each provider was given, optionally for one role."""
    counts: dict[str, int] = {}
    for item in agents(directory):
        if not role or item.get("role") == role:
            counts[str(item.get("provider"))] = counts.get(str(item.get("provider")), 0) + 1
    return counts


def describe(meta: Mapping[str, Any]) -> str:
    """One status line per agent, from recorded facts only."""
    label = f"{meta.get('role')} [{meta.get('provider')} {meta.get('model')}/{meta.get('effort')}]"
    line = f"{meta.get('id')}  {label}  {meta.get('state')}"
    if meta.get("name"):
        line += f"  {meta['name']}"
    outcome = (meta.get("markers") or {}).get("SYMPHONY_OUTCOME")
    if isinstance(outcome, Mapping) and outcome.get("status"):
        line += f"  outcome={outcome['status']}"
    if meta.get("error") and meta.get("state") != "completed":
        line += "  error=" + str(meta["error"]).splitlines()[-1][:160]
    return line


# --------------------------------------------------------------------------
# Mode


def _hermes_flag(project: Path, environ: Mapping[str, str]) -> Path:
    return ledger_root(project, environ) / "hermes.json"


def hermes_mode(project: Path, environ: Mapping[str, str]) -> bool:
    """Whether Hermes sessions in this project are orchestrated.

    Codex and Claude Code keep their switch in the project's hook state. Hermes
    runs no Symphony hooks, so its switch lives beside the runs it produces.
    """
    return _read_json(_hermes_flag(project, environ)).get("enabled") is True


def set_hermes_mode(project: Path, environ: Mapping[str, str], enabled: bool) -> bool:
    flag = _hermes_flag(project, environ)
    if enabled:
        _private_dir(flag.parent)
        _write_json(flag, {"enabled": True, "project": str(Path(project).resolve()), "since": now()})
    else:
        try:
            flag.unlink()
        except FileNotFoundError:
            pass
    return enabled


def with_mode(configuration: Mapping[str, Any], enabled: bool) -> dict[str, Any]:
    """A hook host's project configuration with the standing mode set or cleared."""
    updated = dict(configuration)
    if enabled:
        updated["orchestrator"] = {"enabled": True}
    else:
        updated.pop("orchestrator", None)
    return updated
