#!/usr/bin/env python3
"""Start and track Symphony's headless full agents."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from symphony.orchestrator_cli import main  # noqa: E402


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        # Agent reports are UTF-8 whatever the console's legacy code page is.
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
