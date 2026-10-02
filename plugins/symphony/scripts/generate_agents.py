#!/usr/bin/env python3
"""Generate the packaged Claude agents and routing reference from shipped profiles.

Claude cannot pin reasoning effort on an Agent call, so Symphony encodes the
model and effort in the agent type name and the runtime parses them back out.
That makes the filenames part of the routing contract: a profile naming a model
with no matching file blocks every spawn of that role, permanently, and the bad
value is persisted into the recorded route. Generating the files from the same
profiles the router reads keeps the two sides from drifting.

Codex needs none of this. It accepts a model and effort directly on the spawn,
so no file has to exist for a route to be spawnable there.

    python3 scripts/generate_agents.py           # write the files
    python3 scripts/generate_agents.py --check   # fail if they are stale
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from symphony.routing import (  # noqa: E402
    Assessment,
    MATRIX,
    profiles_for,
    resolve_tier,
    route_for,
    snapshot_for,
)

AGENTS = Path(__file__).resolve().parents[1] / "agents"
REFERENCE = Path(__file__).resolve().parents[1] / "skills/symphony/references/capability-routing.md"
PROVIDER = "claude"
START = "<!-- generated routes: start -->"
END = "<!-- generated routes: end -->"

DESCRIPTIONS = {
    ("lead", "low"): "Administers large simple Symphony work through bounded delegation.",
    ("lead", "medium"): "Leads Symphony work at the matrix-selected route.",
    ("lead", "high"): "Leads difficult Symphony work at the matrix-selected route.",
    ("worker", "low"): "Completes a cheap bounded mechanical Symphony task.",
    ("worker", "medium"): "Completes a bounded routine Symphony task.",
    ("worker", "high"): "Completes a bounded difficult Symphony task.",
    ("assessor", "high"): "Classifies a bounded Symphony task without executing it.",
    ("consultant", "high"): "Resolves a bounded Symphony decision and returns its local classification.",
}

WAITING = (
    "Agents you spawn run in the background: after spawning, end your turn and you are woken with "
    "each result. Never wait by polling output files with Bash, sleep, or Monitor."
)

PRACTICES = (
    "When performing an applicable phase, use one relevant capability advertised and callable in this session "
    "only when its instructions fit this packet; read its SKILL.md and required references first. "
    "A cache copy or previous session does not establish availability. If absent, disabled, failed, "
    "or incompatible, use the native practice in "
    "`../skills/symphony/references/capability-routing.md` without delaying work. "
    "Symphony alone owns route, delegation, lifecycle, and completion; a supporting skill's "
    "caller-return or report-only mode does not itself disable nested agents or shipping. "
    "In your return, name applicable phase, exact selected capability or native fallback, "
    "availability reason, practice, and artifact or fresh command/result. Mark missing evidence "
    "incomplete or explain a scoped exception; loading a skill is not proof that its practice ran."
)

BODIES = {
    "lead": (
        "If the packet has `SYMPHONY_FAST_ROUTE: lead`, decide before any changes whether the "
        "WHOLE objective consists only of predetermined mechanical steps with an expected result, "
        "scope bounded, requirements clear, risk low, required tools (including browser or computer "
        "control when needed) available, and verification concrete. Eligible examples: run a supplied "
        "bash/git command and report its result, or read a specified browser page through known steps. "
        "Implementation, diagnosis, design, substantive review, product judgment, mixed work, or uncertainty "
        "requires escalation before any changes, even for a tiny feature. A run-and-fix request escalates "
        "as a whole. A tool name, short task, or supplied command alone does not establish eligibility. "
        "If ineligible, make no changes and end with `SYMPHONY_FAST_DECISION: escalate`; "
        "the root will hand the original task to an independent assessor. Do not spawn children "
        "on this route. If eligible, work directly and end with `SYMPHONY_FAST_DECISION: eligible` "
        "and `SYMPHONY_OUTCOME: {\"status\":\"completed\"}`.\n\n"
        "For assessed packets, the matrix fixes your topology. Assign substantive implementation, diagnosis, "
        "design, review tasks, and product judgment to workers or consultants. Delegate implementation before editing; "
        "your edits integrate returned worker changes. Coordinate, review integration, verify, "
        "and communicate the results.\n\n"
        "- small: assign the substantive work to one worker; integrate and verify its result.\n"
        "- medium: assign substantive work to bounded worker packets; integrate and verify the results.\n"
        "- large: administer. Delegate all project work to workers and keep only planning, "
        "integration, and verification.\n"
        "- When the route calls for an independent check (high risk, or small/complex), a separate "
        "consultant or worker performs the review. Never review your own work.\n\n"
        "Spawn each child as `symphony:symphony-<role>-<model>-<effort>`, choosing the type for the "
        "packet's own size/complexity from the table Symphony gives you at start. Put "
        "`SYMPHONY_ROLE: <role>` on the first line, then objective, ownership, evidence, constraints, "
        "acceptance_check, return_contract, size, and complexity. A consultant packet also needs one "
        "`SYMPHONY_DECISION: {\"size\":\"...\",\"complexity\":\"...\"}` line. Name the "
        "applicable capability and evidence check in each child packet. " + WAITING + "\n\n"
        "For planning use compatible `ce-plan` or bounded steps. For implementation use compatible "
        "`ce-work`, behavior checks (Superpowers TDD when usable), and Ponytail's reuse/native "
        "check. For independent review use compatible `ce-code-review` or a requirement-and-diff "
        "review; verify the final tree before success claims. Shipping skills apply only when "
        "authorized. " + PRACTICES + " Verify the integrated "
        "result before you report. Once verified and all children have returned, native successful assessed "
        'completion is sufficient. If reporting an outcome, use one JSON line: `SYMPHONY_OUTCOME: {"status":"completed"}`; '
        "use blocked or failed when work remains. Archived followup only reconciles the same bounded task; "
        "a new or substantive objective starts a fresh assessment and delegation scope.\n\n"
        "You cannot ask the user questions: record open decisions and assumptions in your result."
    ),
    "worker": (
        "Complete only the supplied objective and acceptance check. Return evidence to the lead. "
        "For structural discovery use covered Codebase Memory or targeted source reads; for "
        "external library facts use Context7 or dated official sources. For behavior changes use "
        "Superpowers TDD or the smallest meaningful native check; for bugs use a compatible "
        "diagnosing-bugs skill or reproduce and fix the cause. Use Ponytail's reuse/native check. "
        "Use any compatible skill the packet names, and verify your result before you report.\n\n"
        + PRACTICES
    ),
    "assessor": (
        "Assess only. Return size, complexity, risk, rationale, topology, and abstract role "
        "routes. End with exactly one `SYMPHONY_ASSESSMENT: "
        '{"size":"small|medium|large","complexity":"simple|mixed|complex","risk":"normal|high",'
        '"rationale":"...","topology":"..."}` line. Do not become the lead. '
        "Use only those literal axis values. The matrix fixes execution topology: substantive small work "
        "uses one worker; medium work uses bounded worker packets. The lead coordinates, reviews integration, "
        "and verifies results. Your topology recommendation is advisory. "
        "Identify applicable phase practices, their current availability, native fallbacks, and "
        "evidence needed in the lead packet; do not execute them. " + PRACTICES
    ),
    "consultant": (
        "Decide only the supplied question. Return recommendation, evidence, uncertainty, and "
        "consequences. Include one `SYMPHONY_DECISION: "
        '{"size":"small|medium|large","complexity":"simple|mixed|complex"}` line per actionable '
        "decision. For an independent review use compatible `ce-code-review` or Matt Pocock "
        "`code-review`, or compare the exact diff with requirements and affected callers. "
        "For external facts use Context7 or dated official sources. When asked for a review, "
        "review independently and do not fix the code.\n\n" + PRACTICES
    ),
}


def required_agents() -> dict[tuple[str, str, str], None]:
    """Every role, model and effort the matrix can actually select."""
    wanted: dict[tuple[str, str, str], None] = {}
    for profile in profiles_for(PROVIDER):
        snapshot = snapshot_for(PROVIDER, profile["id"])
        for size, complexity in MATRIX:
            for risk in ("normal", "high"):
                route = route_for(Assessment(size, complexity, risk))
                resolved = resolve_tier(route, snapshot)
                model = str(resolved["lead_model"])
                effort = str(resolved["lead_effort"])
                # A worker takes its own packet-local classification, so it can
                # land on any cell the lead can.
                wanted[("lead", model, effort)] = None
                wanted[("worker", model, effort)] = None
        strongest = snapshot.tiers["strongest"]
        wanted[("assessor", strongest, "high")] = None
        for effort in snapshot.supported_efforts.get(strongest, ()):
            if effort in {"xhigh", "max"}:
                wanted[("assessor", strongest, effort)] = None
        wanted[("consultant", strongest, "high")] = None
    return wanted


def resolved_routes() -> str:
    """Show the actual model and effort selected for every shipped profile."""
    lines = [
        "The tables below are generated from `profiles.json` with the runtime resolver. "
        "The last profile for each provider is the fallback when entitlement is unknown.",
    ]
    for provider, label in (("codex", "Codex"), ("claude", "Claude Code")):
        profiles = profiles_for(provider)
        for profile in profiles:
            suffix = " (fallback)" if profile is profiles[-1] else ""
            lines.extend((
                "",
                f"### {label}: `{profile['id']}`{suffix}",
                "",
                "| Size / complexity | Normal risk | High risk |",
                "|---|---|---|",
            ))
            snapshot = snapshot_for(provider, profile["id"])
            for size, complexity in MATRIX:
                choices = []
                for risk in ("normal", "high"):
                    resolved = resolve_tier(route_for(Assessment(size, complexity, risk)), snapshot)
                    choices.append(f"`{resolved['lead_model']}/{resolved['lead_effort']}`")
                lines.append(f"| {size} / {complexity} | {choices[0]} | {choices[1]} |")
    return "\n".join(lines) + "\n"


def reference_text(current: str) -> str:
    if current.count(START) != 1 or current.count(END) != 1:
        raise ValueError("capability routing reference needs one generated-routes marker pair")
    before, rest = current.split(START, 1)
    _, after = rest.split(END, 1)
    return before + START + "\n" + resolved_routes() + END + after


def render(role: str, model: str, effort: str) -> str:
    description = DESCRIPTIONS.get(
        (role, effort), f"Completes bounded Symphony {role} work."
    )
    return (
        "---\n"
        f"name: symphony-{role}-{model}-{effort}\n"
        f"description: {description}\n"
        f"model: {model}\n"
        f"effort: {effort}\n"
        "---\n"
        "\n"
        f"{BODIES[role]}\n"
    )


def frontmatter(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def contract_errors(name: str, text: str) -> list[str]:
    """Check only what the runtime parses back out of the filename."""
    role, model, effort = name[len("symphony-") : -len(".md")].split("-", 2)
    model, _, effort = f"{model}-{effort}".rpartition("-")
    fields = frontmatter(text)
    problems = []
    if fields.get("name") != name[: -len(".md")]:
        problems.append(f"{name}: frontmatter name is {fields.get('name')!r}")
    if fields.get("model") != model:
        problems.append(f"{name}: declares model {fields.get('model')!r}, filename says {model!r}")
    if fields.get("effort") != effort:
        problems.append(f"{name}: declares effort {fields.get('effort')!r}, filename says {effort!r}")
    if not fields.get("description"):
        problems.append(f"{name}: no description")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args()

    wanted = {
        f"symphony-{role}-{model}-{effort}.md": (role, model, effort)
        for role, model, effort in required_agents()
    }
    present = {path.name: path.read_text(encoding="utf-8") for path in AGENTS.glob("*.md")}

    missing = sorted(set(wanted) - set(present))
    extra = sorted(set(present) - set(wanted))
    broken = [
        problem
        for name in sorted(set(wanted) & set(present))
        for problem in contract_errors(name, present[name])
    ]
    current_reference = REFERENCE.read_text(encoding="utf-8")
    expected_reference = reference_text(current_reference)

    if args.check:
        problems = (
            [f"no agent file for a route the profiles can select: {name}" for name in missing]
            + [f"no profile can select this agent: {name}" for name in extra]
            + broken
            + (["capability routing reference is stale"] if current_reference != expected_reference else [])
            + [
                f"agent file is stale: {name}"
                for name in sorted(set(wanted) & set(present))
                if present[name] != render(*wanted[name])
            ]
        )
        for problem in problems:
            print(f"::error::run scripts/generate_agents.py -- {problem}")
        if problems:
            return 1
        print(f"agent files and routing reference cover every selectable route ({len(wanted)} files)")
        return 0

    for name in extra:
        (AGENTS / name).unlink()
    for name in wanted:
        (AGENTS / name).write_text(render(*wanted[name]), encoding="utf-8")
    if current_reference != expected_reference:
        REFERENCE.write_text(expected_reference, encoding="utf-8")
    print(f"agents: {len(wanted)} required, {len(missing)} added, {len(extra)} removed")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
