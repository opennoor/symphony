#!/usr/bin/env python3
"""Copy the launcher into the Hermes package.

Hermes installs one directory and scans all of it. The Codex and Claude Code
plugin carries hook launchers and a hook runtime that Hermes never runs, and
its install scan refuses the encoded Windows hook command. The Hermes package
is therefore its own small directory, and the launcher it needs is copied here
at release time, byte for byte, so there is one source and no drift.
"""

import argparse
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]
HERMES = PLUGIN.parent / "symphony-hermes"
# The launcher and exactly what it imports or reads.
COPIED = (
    "symphony/__init__.py",
    "symphony/model.py",
    "symphony/routing.py",
    "symphony/orchestrator.py",
    "symphony/orchestrator_cli.py",
    "scripts/symphony_agent.py",
    "profiles.json",
    "model-policy.json",
)
# Written by hand in the package itself.
OWN = ("plugin.yaml", "__init__.py", "skills/orchestrator/SKILL.md")


def stale(source=PLUGIN, target=HERMES):
    """Relative paths whose copy is missing, different, or not expected at all."""
    wrong = [name for name in COPIED
             if not (target / name).is_file() or (target / name).read_bytes() != (source / name).read_bytes()]
    present = {path.relative_to(target).as_posix() for path in target.rglob("*")
               if path.is_file() and "__pycache__" not in path.parts}
    return sorted({*wrong, *(present - set(COPIED) - set(OWN)), *(set(OWN) - present)})


def write(source=PLUGIN, target=HERMES):
    for name in COPIED:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        write()
    wrong = stale()
    for name in wrong:
        print(f"::error::symphony-hermes/{name} is stale or unexpected; run scripts/generate_hermes.py")
    if not wrong:
        print(f"Hermes package matches the launcher ({len(COPIED)} copied files)")
    return int(bool(wrong))


if __name__ == "__main__":
    raise SystemExit(main())
