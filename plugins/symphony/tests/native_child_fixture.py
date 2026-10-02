"""Modeled Claude native launch evidence for deterministic lifecycle tests."""
from dataclasses import asdict
from plugins.symphony.scripts.package_smoke import _write_claude_child_launch


def write_claude_child_launch(home, project, run, identity, role, model, effort, turn, *, purpose='substantive'):
    return _write_claude_child_launch(home, project, asdict(run), identity, role, model, effort, turn, purpose=purpose)
