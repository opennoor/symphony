#!/usr/bin/env python3
"""Advance the plugin's patch version across every file that states it.

Six files repeat the version and all six must agree: the two provider
manifests, the Hermes manifest, the package constant the heartbeat reports
with its copy in the Hermes package, and the marketplace entry. Keeping this in a script rather than inline in a workflow means it can
be run and tested like anything else.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

PLUGIN = pathlib.Path("plugins/symphony")
MANIFESTS = (
    PLUGIN / ".claude-plugin" / "plugin.json",
    PLUGIN / ".codex-plugin" / "plugin.json",
)
INIT = PLUGIN / "symphony" / "__init__.py"
HERMES = pathlib.Path("plugins/symphony-hermes") / "plugin.yaml"
HERMES_INIT = pathlib.Path("plugins/symphony-hermes") / "symphony" / "__init__.py"
MARKETPLACE = pathlib.Path(".claude-plugin/marketplace.json")


def current() -> str:
    versions = {json.loads(path.read_text())["version"] for path in MANIFESTS}
    if len(versions) != 1:
        raise SystemExit(f"::error::provider manifests disagree on the version: {versions}")
    return versions.pop()


def next_patch(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise SystemExit(f"::error::not a release version: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def apply(version: str) -> None:
    for path in MANIFESTS:
        data = json.loads(path.read_text())
        data["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")
    INIT.write_text(
        re.sub(r'PLUGIN_VERSION = "[^"]+"', f'PLUGIN_VERSION = "{version}"', INIT.read_text())
    )
    HERMES.write_text(
        re.sub(r'(?m)^version: "[^"]+"$', f'version: "{version}"', HERMES.read_text())
    )
    # The Hermes package carries a copy of the package constant.
    HERMES_INIT.write_text(INIT.read_text())
    entry = json.loads(MARKETPLACE.read_text())
    entry["plugins"][0]["version"] = version
    MARKETPLACE.write_text(json.dumps(entry, indent=2) + "\n")


def main() -> int:
    version = next_patch(current())
    apply(version)
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
