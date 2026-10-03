#!/usr/bin/env python3
"""Check native assessor effort acceptance using CI's existing provider login."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def check(provider: str, plugin_root: Path) -> dict:
    executable = shutil.which(provider)
    if not executable:
        raise RuntimeError(f"{provider} executable missing from PATH")
    deadline = time.monotonic() + 90
    with tempfile.TemporaryDirectory(prefix="symphony-native-effort-") as scratch:
        def run(arguments):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("native effort check exceeded 90 seconds")
            result = subprocess.run(
                [executable, *arguments], cwd=scratch, input="", capture_output=True,
                text=True, shell=False, timeout=remaining,
            )
            if result.returncode:
                reason = f"{provider} command failed (exit {result.returncode})"
                try:
                    report = json.loads(result.stdout)
                    subtype = report.get("subtype") if isinstance(report, dict) else None
                    if subtype in {"error_max_budget_usd", "error_max_turns", "error_during_execution"}:
                        reason += f"; {subtype}"
                except ValueError:
                    pass
                raise RuntimeError(reason)
            return result

        version = run(["--version"]).stdout.strip()
        accepted = []
        if provider == "codex":
            for model in ("gpt-6-astra", "gpt-6-sol"):
                result = run([
                    "exec", "--ephemeral", "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox", "--model", model,
                    "-c", 'model_reasoning_effort="ultra"', "-c", "features.hooks=false",
                    "Reply only OK. Do not use tools.",
                ])
                banner = result.stdout + "\n" + result.stderr
                if f"model: {model}\n" not in banner or "reasoning effort: ultra\n" not in banner:
                    raise RuntimeError(f"{model}/ultra was not reported by the native CLI")
                if result.stdout.strip().rstrip(".") != "OK":
                    raise RuntimeError(f"{model}/ultra did not return the requested static reply")
                accepted.append({"model": model, "effort": "ultra", "evidence": "native CLI banner"})
        else:
            model = "claude-opus-5-5"
            agent = f"symphony-assessor-{model}-max"
            definition = (plugin_root / "agents" / f"{agent}.md").read_text(encoding="utf-8")
            fields = dict(line.split(":", 1) for line in definition.split("---", 2)[1].splitlines() if ":" in line)
            if fields.get("model", "").strip() != model or fields.get("effort", "").strip() != "max":
                raise RuntimeError("packaged assessor must declare exact Opus model and max effort")
            result = run([
                "--plugin-dir", str(plugin_root), "--settings", '{"disableAllHooks":true}',
                "--print", "--no-session-persistence", "--tools", "", "--disallowedTools", "mcp__*",
                "--agent", f"symphony:{agent}", "--max-budget-usd", "0.25", "--output-format", "json",
                "Assess only the task 'reply OK'. Reply only with the required SYMPHONY_ASSESSMENT line.",
            ])
            report = json.loads(result.stdout)
            if report.get("is_error") or set(report.get("modelUsage", {})) != {model}:
                raise RuntimeError("packaged max assessor did not run on the exact Opus model")
            if "SYMPHONY_ASSESSMENT:" not in str(report.get("result", "")):
                raise RuntimeError("packaged assessor did not return its assessment marker")
            accepted.append({"model": model, "effort": "max",
                             "evidence": "native packaged frontmatter accepted; exact modelUsage",
                             "effective_effort_telemetry": "not exposed by Claude"})
    return {"provider": provider, "cli_version": version, "accepted": accepted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("codex", "claude"))
    parser.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.provider, args.plugin_root.resolve())))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"provider": args.provider, "accepted": [], "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
