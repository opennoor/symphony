#!/usr/bin/env python3
"""Refresh shipped capability profiles against a reviewed model policy.

Neither host lets a hook discover models, so the tier-to-model map is a build
artifact. This keeps that artifact honest: it reads each provider's own roster,
rewrites the profiles, and refuses to ship a map naming a model the provider
will not accept.

    refresh_profiles.py --verify   # reject any model the provider rejects

Both providers use exact model IDs. A refresh selects from each provider's
current roster and rewrites the shipped profile matrix.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import time

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles.json"
MODEL_POLICY = ROOT / "model-policy.json"

CODEX_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
sys.path.insert(0, str(ROOT))
from symphony.routing import Assessment, MATRIX as ROUTING_MATRIX, NO_PROFILE, route_for  # noqa: E402

CELLS = tuple(f"{size}/{complexity}" for size, complexity in ROUTING_MATRIX)
TIER_CELLS = {
    "economy": "large/simple",
    "balanced": "medium/simple",
    "capable": "small/mixed",
    "strongest": "small/complex",
}


def _policy() -> dict:
    try:
        return json.loads(MODEL_POLICY.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"::error::cannot read Codex model policy: {error}")


def _current(model: str) -> bool:
    if not isinstance(model, str):
        return False
    entry = _policy().get("models", {}).get(model, {})
    if entry.get("lifecycle") != "current":
        return False
    try:
        return "retirement_date" not in entry or date.fromisoformat(_today()) < date.fromisoformat(entry["retirement_date"])
    except (TypeError, ValueError):
        return False


def _selectable(entry: dict, model_key: str) -> bool:
    model = entry.get(model_key)
    rate_key = "api_usd_per_million_tokens" if model_key == "id" else "codex_credits_per_million_tokens"
    if (not isinstance(model, str) or not _current(model)
            or rate_key not in _policy()["models"][model]):
        return False
    if entry.get("lifecycle") not in (None, "current"):
        return False
    if entry.get("status") not in (None, "current", "active", "available"):
        return False
    return not any(entry.get(flag) is True for flag in (
        "retired", "deprecated", "legacy", "isRetired", "isDeprecated", "isLegacy"
    ))


def _older_without_price_advantage(provider: str, model: str, available: set[str]) -> bool:
    """Prefer newer adequate models unless the older rate vector is cheaper."""
    entries = _policy()["models"]
    choice = entries[model]
    rate_key = "codex_credits_per_million_tokens" if provider == "codex" else "api_usd_per_million_tokens"
    older_rates = choice[rate_key]
    for other in available:
        newer = entries[other]
        if (other == model or newer["family"] != choice["family"]
                or newer["generation"] <= choice["generation"]
                or newer["capability_rank"] < choice["capability_rank"]):
            continue
        newer_rates = newer[rate_key]
        components = older_rates.keys()
        if (any(older_rates[key] > newer_rates[key] for key in components)
                or all(older_rates[key] == newer_rates[key] for key in components)):
            return True
    return False


def _model_rank(provider: str, model: str) -> int | None:
    """Reviewed provider capability rank; unknown IDs fail closed."""
    entry = _policy().get("models", {}).get(model, {})
    rate_key = "codex_credits_per_million_tokens" if provider == "codex" else "api_usd_per_million_tokens"
    return entry.get("capability_rank") if rate_key in entry else None


def codex_roster(home: Path) -> list[dict]:
    """The models this account may select, including on a fresh CI home."""
    cache_path = home / "models_cache.json"
    if not cache_path.exists():
        try:
            return [entry for entry in _app_server_roster(home) if _selectable(entry, "slug")]
        except (OSError, ValueError, RuntimeError) as error:
            raise SystemExit(f"::error::no readable Codex model roster at {home}: {error}")
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"::error::no readable Codex model roster at {home}: {error}")
    return [
        item
        for item in cache.get("models", ())
        if isinstance(item, dict) and item.get("visibility") == "list"
        and _selectable(item, "slug")
    ]


def _app_server_roster(home: Path) -> list[dict]:
    """Ask Codex for its live picker roster; `codex exec` need not write a cache."""
    process = subprocess.Popen(
        ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={**os.environ, "CODEX_HOME": str(home)},
    )
    try:
        def send(message: dict) -> None:
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "symphony_refresh", "title": "Symphony Refresh", "version": "1.0.0"}
        }})
        send({"method": "initialized", "params": {}})
        send({"method": "model/list", "id": 2, "params": {"limit": 100, "includeHidden": False}})
        deadline = time.monotonic() + 30
        pending = b""
        models = []
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") != 2:
                    continue
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", "model/list failed"))
                result = response["result"]
                models.extend(result["data"])
                cursor = result.get("nextCursor")
                if cursor:
                    send({"method": "model/list", "id": 2, "params": {
                        "limit": 100, "includeHidden": False, "cursor": cursor
                    }})
                    continue
                return [
                    {
                        "slug": item["model"], "visibility": "list",
                        "supported_reasoning_levels": [
                            {"effort": level["reasoningEffort"]}
                            for level in item.get("supportedReasoningEfforts", ())
                        ],
                    }
                    for item in models
                    if not item.get("hidden") and _selectable(item, "model")
                ]
        raise RuntimeError("model/list did not return a complete roster within 30 seconds")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def efforts_of(entry: dict) -> list[str]:
    levels = entry.get("supported_reasoning_levels") or ()
    found = [
        str(level.get("effort"))
        for level in levels
        if isinstance(level, dict) and level.get("effort")
    ]
    # Preserve provider order while dropping preview-only values.
    return [effort for effort in CODEX_EFFORTS if effort in found]


def claude_roster() -> list[dict]:
    """Read the model IDs this Anthropic API key can select."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("::error::ANTHROPIC_API_KEY is required to read the model roster")
    url = "https://api.anthropic.com/v1/models?limit=100"
    models = []
    while url:
        request = urllib.request.Request(url, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page = json.load(response)
        except (OSError, ValueError) as error:
            raise SystemExit(f"::error::could not read Claude model roster: {error}")
        models.extend(page.get("data", ()))
        last_id = page.get("last_id")
        url = (
            "https://api.anthropic.com/v1/models?limit=100&after_id="
            + urllib.parse.quote(str(last_id), safe="")
            if page.get("has_more") and last_id else ""
        )
    return [item for item in models if isinstance(item, dict) and _selectable(item, "id")]


def _agent_prompt(provider: str, current: list[dict], roster: list[dict]) -> str:
    floor = current[-1].get("matrix", {})
    policy = _policy().get("models", {})
    return """Choose this provider's model/effort grid for Symphony. Optimize outcome quality against token and latency cost: use the cheapest adequate model and effort for routine cells, reserve stronger models and higher effort for work whose size/complexity benefits from them. The grid must be monotonic: complexity never lowers model capability or effort; increasing task size never raises model token rates or effort. For the full entitlement profile, each cell must be at least as capable and effortful as its fallback profile. Token rates are per million tokens, output includes reasoning, and Claude rates also include cache-write durations; effort and latency affect usage, so do not infer a fixed total cost or effort multiplier from rates alone. Prefer a newer adequate generation within the same family; select an older one only when every published rate is no greater and at least one is cheaper than every newer adequate available model.

The last profile is also the runtime safety floor for users whose entitlements cannot be detected. Do not promote any fallback cell above the model capability/family of its currently shipped floor; those users may not have access to gated models. You may still choose each fallback cell's effort semantically. Current fallback model selections: """ + json.dumps({
        cell: choice.get("model")
        for cell, choice in floor.items()
        if isinstance(choice, dict) and "model" in choice
    }) + """

Do not change the profile IDs or entitlement gates. For gated Codex profiles after the first profile, select only models in that profile's requires_all list so accounts with just those models retain a usable route. Use only model IDs in the supplied roster. For every selected model, list its supported effort levels in provider-supported order. Return exactly one JSON object and no markdown or extra text with this shape:
{"model_order":["least capable model", "...", "most capable model"],"model_efforts":{"model-id":["low","medium"]},"profiles":[{"id":"full","matrix":{"small/simple":{"model":"model-id","effort":"medium"},...}}],"rationale":"brief basis for the tradeoffs"}

Every profile must contain all nine matrix cells: small/simple, small/mixed, small/complex, medium/simple, medium/mixed, medium/complex, large/simple, large/mixed, large/complex. Every effort must be listed in that model's model_efforts entry. Return only this provider's decision; do not edit files or run commands.

Provider: """ + provider + "\nCurrent profile IDs/gates: " + json.dumps([
        {key: profile[key] for key in ("id", "requires_all", "requires_any") if key in profile}
        for profile in current
    ]) + "\nCurrent routing matrix: " + json.dumps(list(CELLS)) + "\nProvider model roster: " + json.dumps(roster) + (
        "\nReviewed model policy (capability rank, lifecycle, and "
        + ("Codex credits" if provider == "codex" else "Claude API USD")
        + " per million input, cached input, and output tokens): "
        + json.dumps({model: policy[model] for model in policy if model in {
            entry.get("slug" if provider == "codex" else "id") for entry in roster
        }})
    )


def _run_provider_agent(provider: str, current: list[dict], roster: list[dict]) -> dict:
    prompt = _agent_prompt(provider, current, roster)
    if provider == "codex":
        available = {entry["slug"]: entry for entry in roster}
        candidates = {name for name in available if _selectable(available[name], "slug")}
        ranked = sorted(
            (name for name in candidates if not _older_without_price_advantage(provider, name, candidates)),
            key=lambda name: (_model_rank(provider, name), _policy()["models"][name]["generation"]),
        )
        if not ranked:
            raise SystemExit("::error::no supported Codex model can run the refresh agent")
        model = ranked[-1]
        effort = next((item for item in reversed(CODEX_EFFORTS) if item in efforts_of(available[model])), None)
        if not effort:
            raise SystemExit(f"::error::no supported reasoning effort for Codex agent model {model}")
        argv = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--model", model, "-c", f'model_reasoning_effort="{effort}"', "-"]
        completed = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=900)
        print(f"Codex matrix agent: {model} at {effort} effort")
    else:
        candidates = {entry["id"] for entry in roster if _selectable(entry, "id")}
        model = max(
            (name for name in candidates if not _older_without_price_advantage(provider, name, candidates)),
            key=lambda name: (_model_rank("claude", name), _policy()["models"][name]["generation"], name),
            default=None,
        )
        if model is None:
            raise SystemExit("::error::no supported Claude model can run the refresh agent")
        effort = next(
            (item for item in reversed(CLAUDE_EFFORTS) if item in efforts_by_model(current, model)),
            "high",
        )
        argv = ["claude", "--bare", "--print", "--no-session-persistence", "--tools", "",
                "--model", model, "--effort", effort, "--output-format", "text", prompt]
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=900)
        print(f"Claude matrix agent: {model} at {effort} effort")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise SystemExit(f"::error::{provider} matrix agent failed: {detail[-1][:300] if detail else completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"::error::{provider} matrix agent did not return plain JSON: {error}")
    return validate_matrix(provider, result, current, roster)


def validate_matrix(provider: str, result: dict, current: list[dict], roster: list[dict]) -> dict:
    """Reject provider-agent output unless every route is complete and safe."""
    if (not isinstance(current, list) or not current
            or any(not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                   or not profile["id"] or profile["id"] == NO_PROFILE for profile in current)
            or len({profile["id"] for profile in current}) != len(current)):
        raise SystemExit(f"::error::{provider} needs nonempty unique profile IDs excluding {NO_PROFILE!r}")
    models = ({entry["slug"]: set(efforts_of(entry)) for entry in roster
               if _selectable(entry, "slug")}
              if provider == "codex" else {entry["id"]: set(CLAUDE_EFFORTS) for entry in roster
                                           if _selectable(entry, "id")})
    expected_ids = [profile["id"] for profile in current]
    if not isinstance(result, dict) or not isinstance(result.get("profiles"), list):
        raise SystemExit(f"::error::{provider} matrix output must contain a profile list")
    if any(not isinstance(profile, dict) for profile in result["profiles"]):
        raise SystemExit(f"::error::{provider} matrix profiles must be objects")
    if [p.get("id") for p in result["profiles"]] != expected_ids:
        raise SystemExit(f"::error::{provider} matrix changed profile ordering or IDs")
    model_order = result.get("model_order")
    if (not isinstance(model_order, list) or not model_order
            or any(not isinstance(model, str) for model in model_order)
            or len(set(model_order)) != len(model_order)):
        raise SystemExit(f"::error::{provider} matrix has invalid model ordering")
    if set(model_order) - set(models):
        raise SystemExit(f"::error::{provider} matrix names models outside its roster: {set(model_order) - set(models)}")
    rank = {model: _model_rank(provider, model) for model in models}
    unknown = {model for model in model_order if rank[model] is None}
    if unknown:
        raise SystemExit(f"::error::{provider} matrix uses models without a verified capability order: {sorted(unknown)}")
    declared_efforts = result.get("model_efforts", {})
    if not isinstance(declared_efforts, dict):
        raise SystemExit(f"::error::{provider} matrix effort support must be an object")
    if provider == "codex":
        # The roster is the authority for Codex effort support; the semantic
        # agent must choose a route, not restate mutable provider metadata.
        declared_efforts = {
            model: [effort for effort in CODEX_EFFORTS if effort in models[model]]
            for model in models
        }
    elif not declared_efforts:
        raise SystemExit("::error::Claude matrix must include provider-checked effort support")
    normalized_profiles = []
    used_models = set()
    for profile in result["profiles"]:
        matrix = profile.get("matrix")
        if not isinstance(matrix, dict) or set(matrix) != set(CELLS):
            raise SystemExit(f"::error::{provider} profile {profile['id']} must define exactly nine matrix cells")
        normalized = {}
        prior = next(item for item in current if item["id"] == profile["id"])
        limited_models = set(prior.get("requires_all", ())) if provider == "codex" and profile["id"] != expected_ids[0] else set()
        for cell in CELLS:
            choice = matrix[cell]
            if not isinstance(choice, dict):
                raise SystemExit(f"::error::{provider} {cell} choice must be an object")
            model, effort = choice.get("model"), choice.get("effort")
            if not isinstance(model, str) or not isinstance(effort, str):
                raise SystemExit(f"::error::{provider} {cell} must name a string model and effort")
            if model not in models or rank.get(model) is None:
                raise SystemExit(f"::error::{provider} {cell} model is absent from its roster/order: {model!r}")
            if limited_models and model not in limited_models:
                raise SystemExit(f"::error::{provider} {profile['id']} must preserve its gated model coverage")
            if _older_without_price_advantage(provider, model, set(models)):
                raise SystemExit(f"::error::{provider} {cell} selects older {model} without a token-rate advantage")
            if effort not in models[model] or effort not in declared_efforts.get(model, ()):
                raise SystemExit(f"::error::{provider} unsupported effort {effort!r} for {model}")
            size, complexity = cell.split("/")
            risk_floor = route_for(Assessment(size, complexity, risk="high")).lead_effort
            if risk_floor not in declared_efforts.get(model, ()):
                raise SystemExit(f"::error::{provider} {model} cannot preserve the high-risk {risk_floor} effort floor at {cell}")
            used_models.add(model)
            normalized[cell] = {"model": model, "effort": effort}
        for size in ("small", "medium", "large"):
            cells = [normalized[f"{size}/{complexity}"] for complexity in ("simple", "mixed", "complex")]
            if [rank[item["model"]] for item in cells] != sorted(rank[item["model"]] for item in cells):
                raise SystemExit(f"::error::{provider} model capability must not fall as complexity increases")
            if [CODEX_EFFORTS.index(item["effort"]) for item in cells] != sorted(CODEX_EFFORTS.index(item["effort"]) for item in cells):
                raise SystemExit(f"::error::{provider} effort must not fall as complexity increases")
        for complexity in ("simple", "mixed", "complex"):
            cells = [normalized[f"{size}/{complexity}"] for size in ("small", "medium", "large")]
            if provider in ("codex", "claude"):
                rates = _policy()["models"]
                rate_key = "codex_credits_per_million_tokens" if provider == "codex" else "api_usd_per_million_tokens"
                for component in rates[cells[0]["model"]][rate_key]:
                    values = [rates[item["model"]][rate_key][component] for item in cells]
                    if values != sorted(values, reverse=True):
                        raise SystemExit(f"::error::{provider} {component} token rate must not rise as task size increases")
            if [CODEX_EFFORTS.index(item["effort"]) for item in cells] != sorted((CODEX_EFFORTS.index(item["effort"]) for item in cells), reverse=True):
                raise SystemExit(f"::error::{provider} effort must not rise as task size increases")
        normalized_profiles.append({"id": profile["id"], "matrix": normalized})
    if provider == "codex":
        declared_efforts = {model: declared_efforts[model] for model in used_models}
    if used_models != set(model_order) or set(declared_efforts) != used_models:
        raise SystemExit(f"::error::{provider} matrix model order/efforts must cover exactly the selected models")
    trusted_order = sorted(used_models, key=lambda model: (rank[model], model))
    for model, levels in declared_efforts.items():
        if (model not in models or not isinstance(levels, list) or not levels
                or any(not isinstance(level, str) for level in levels)
                or len(set(levels)) != len(levels) or not set(levels) <= models[model]):
            raise SystemExit(f"::error::{provider} has invalid supported efforts for {model}")
        if levels != [effort for effort in CODEX_EFFORTS if effort in levels]:
            raise SystemExit(f"::error::{provider} effort support is not in provider order for {model}")
    if len(normalized_profiles) > 1:
        full, fallback = normalized_profiles[0]["matrix"], normalized_profiles[-1]["matrix"]
        shipped_fallback = current[-1].get("matrix", {})
        improved = False
        for cell in CELLS:
            if rank[full[cell]["model"]] < rank[fallback[cell]["model"]]:
                raise SystemExit(f"::error::{provider} full profile cannot use a weaker model than fallback at {cell}")
            if CODEX_EFFORTS.index(full[cell]["effort"]) < CODEX_EFFORTS.index(fallback[cell]["effort"]):
                raise SystemExit(f"::error::{provider} full profile cannot use lower effort than fallback at {cell}")
            prior_floor = shipped_fallback.get(cell)
            if prior_floor and _model_rank(provider, prior_floor["model"]) is None:
                raise SystemExit(f"::error::{provider} prior fallback model lacks a reviewed capability rank at {cell}")
            if prior_floor and rank[fallback[cell]["model"]] > _model_rank(provider, prior_floor["model"]):
                raise SystemExit(f"::error::{provider} fallback cannot use a more gated model at {cell}")
            improved |= rank[full[cell]["model"]] > rank[fallback[cell]["model"]] or full[cell]["effort"] != fallback[cell]["effort"]
        if not improved:
            raise SystemExit(f"::error::{provider} full profile must improve at least one cell over the fallback")
        if provider == "claude":
            for stronger, weaker in zip(normalized_profiles, normalized_profiles[1:]):
                upper, lower = stronger["matrix"], weaker["matrix"]
                if any(rank[upper[cell]["model"]] < rank[lower[cell]["model"]] for cell in CELLS):
                    raise SystemExit("::error::Claude profile capability must not fall with greater entitlement")
                if not any(rank[upper[cell]["model"]] > rank[lower[cell]["model"]] for cell in CELLS):
                    raise SystemExit("::error::Claude gated profile must improve a model over the next profile")
    return {"profiles": normalized_profiles, "model_order": trusted_order, "model_efforts": declared_efforts,
            "rationale": str(result.get("rationale", ""))[:600]}


def agent_probe(home: Path) -> bool:
    """Run both provider agents concurrently, then merge only validated JSON."""
    document = json.loads(PROFILES.read_text(encoding="utf-8"))
    current = document["providers"]
    codex_models = codex_roster(home)
    claude_models = claude_roster()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "codex": pool.submit(_run_provider_agent, "codex", current["codex"]["profiles"], codex_models),
            "claude": pool.submit(_run_provider_agent, "claude", current["claude"]["profiles"], claude_models),
        }
        decisions = {provider: future.result() for provider, future in futures.items()}
    updated = json.loads(json.dumps(document))
    for provider, decision in decisions.items():
        available = codex_models if provider == "codex" else claude_models
        model_key = "slug" if provider == "codex" else "id"
        updated["providers"][provider]["available_models"] = sorted({entry[model_key] for entry in available})
        profiles = {profile["id"]: profile for profile in decision["profiles"]}
        for profile in updated["providers"][provider]["profiles"]:
            matrix = profiles[profile["id"]]["matrix"]
            profile["matrix"] = matrix
            profile["tiers"] = {tier: matrix[cell]["model"] for tier, cell in TIER_CELLS.items()}
            profile["efforts"] = decision["model_efforts"]
            if profile.get("requires_all") is not None and (
                provider == "claude" or profile["id"] == "full" or profile["requires_all"]
            ):
                profile["requires_all"] = sorted({choice["model"] for choice in matrix.values()})
        print(f"{provider} matrix rationale: {decision['rationale'] or '(not supplied)'}")
    changed = document["providers"] != updated["providers"]
    if changed:
        updated["generated_at"] = os.environ.get("REFRESH_DATE") or _today()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=PROFILES.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(updated, indent=2) + "\n")
            temporary.replace(PROFILES)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    print("provider matrices updated" if changed else "provider matrices already match")
    return changed


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def check_policy(*, require_fresh: bool = False, validate_profiles: bool = True) -> int:
    """Check reviewed rates and shipped routes offline; selected models stand in for availability."""
    policy = _policy()
    failures = []
    try:
        reviewed = date.fromisoformat(policy["reviewed_at"])
        age = (date.fromisoformat(_today()) - reviewed).days
        if age < 0:
            failures.append("policy review date is in the future")
        if require_fresh and age > 45:
            failures.append("policy review is older than 45 days; review provider sources and refresh model-policy.json")
    except (KeyError, TypeError, ValueError):
        failures.append("policy needs an ISO reviewed_at date")
    if not isinstance(policy.get("sources"), list) or not policy["sources"] or any(
        not isinstance(source, str) or not source.startswith("https://") for source in policy["sources"]
    ):
        failures.append("policy needs reviewed HTTPS sources")
    entries = policy.get("models")
    if not isinstance(entries, dict) or not entries:
        failures.append("policy needs model entries")
        entries = {}
    for model, entry in entries.items():
        rate_key = "codex_credits_per_million_tokens" if model.startswith("gpt-") else "api_usd_per_million_tokens"
        dimensions = {"input", "cached_input", "output"} if model.startswith("gpt-") else {
            "input", "cached_input", "output", "cache_write_5m", "cache_write_1h"
        }
        rates = entry.get(rate_key, {}) if isinstance(entry, dict) else {}
        if (not isinstance(entry, dict) or type(entry.get("capability_rank")) is not int
                or entry["capability_rank"] < 0
                or not isinstance(entry.get("family"), str) or not entry["family"]
                or type(entry.get("generation")) not in (int, float)
                or not math.isfinite(entry["generation"]) or entry["generation"] <= 0
                or not (model.startswith("gpt-") or model.startswith("claude-"))
                or entry.get("lifecycle") not in ("current", "superseded", "legacy", "deprecated", "retiring", "retired")
                or not isinstance(rates, dict) or set(rates) != dimensions
                or any(type(rates.get(component)) not in (int, float) or not math.isfinite(rates[component])
                       or rates[component] <= 0
                       for component in dimensions)):
            failures.append(f"{model}: invalid capability, lifecycle, or published token rates")
        if isinstance(entry, dict) and "retirement_date" in entry:
            try:
                retirement = date.fromisoformat(entry["retirement_date"])
                if entry.get("lifecycle") == "current" and retirement <= date.fromisoformat(_today()):
                    failures.append(f"{model}: current model has reached its retirement date")
            except (TypeError, ValueError):
                failures.append(f"{model}: invalid retirement date")
    if not validate_profiles:
        for failure in failures:
            print(f"::error::model policy: {failure}")
        if failures:
            return 1
        print("reviewed policy metadata is consistent (shipped routes not checked)")
        return 0
    try:
        providers = json.loads(PROFILES.read_text(encoding="utf-8"))["providers"]
    except (OSError, ValueError, KeyError) as error:
        failures.append(f"cannot read shipped profiles: {error}")
        providers = {}
    if not isinstance(providers, dict) or set(providers) != {"codex", "claude"}:
        failures.append("shipped profiles must contain exactly codex and claude providers")
        providers = providers if isinstance(providers, dict) else {}
    for provider, block in providers.items():
        profiles = block.get("profiles") if isinstance(block, dict) else None
        if not isinstance(profiles, list) or not profiles or any(not isinstance(profile, dict) for profile in profiles):
            failures.append(f"{provider}: needs a nonempty profile list")
            continue
        ids = [profile.get("id") for profile in profiles]
        if (any(not isinstance(pid, str) or not pid or pid == NO_PROFILE for pid in ids)
                or len(set(ids)) != len(ids)):
            failures.append(f"{provider}: needs unique profile IDs excluding {NO_PROFILE!r}")
            continue
        selected = {choice.get("model") for profile in profiles for choice in profile.get("matrix", {}).values()}
        available = block.get("available_models")
        if (not isinstance(available, list) or not available
                or any(not isinstance(model, str) for model in available)
                or len(set(available)) != len(available)
                or any(not _current(model) or _model_rank(provider, model) is None for model in available)
                or not selected <= set(available)):
            failures.append(f"{provider}: needs a reviewed availability snapshot containing every selected model")
            continue
        support_by_model = {}
        for profile in profiles:
            pid = profile.get("id", "?")
            matrix = profile.get("matrix", {})
            if set(matrix) != set(CELLS):
                failures.append(f"{provider}/{pid}: matrix must cover nine cells")
                continue
            if profile.get("tiers") != {tier: matrix[cell].get("model") for tier, cell in TIER_CELLS.items()}:
                failures.append(f"{provider}/{pid}: tiers disagree with matrix")
            efforts = profile.get("efforts", {})
            for model, levels in efforts.items():
                if model in support_by_model and support_by_model[model] != levels:
                    failures.append(f"{provider}/{pid}: effort support differs for {model}")
                support_by_model[model] = levels
                if not _current(model) or _model_rank(provider, model) is None:
                    failures.append(f"{provider}/{pid}: non-current effort model {model}")
                if (not isinstance(levels, list) or not levels or len(set(levels)) != len(levels)
                        or any(level not in CODEX_EFFORTS for level in levels)
                        or levels != [level for level in CODEX_EFFORTS if level in levels]):
                    failures.append(f"{provider}/{pid}: invalid efforts for {model}")
            for cell, choice in matrix.items():
                model = choice.get("model")
                if not _current(model) or _model_rank(provider, model) is None:
                    failures.append(f"{provider}/{pid}/{cell}: non-current or unknown model {model}")
                if choice.get("effort") not in efforts.get(model, []):
                    failures.append(f"{provider}/{pid}/{cell}: effort missing from declaration")
                size, complexity = cell.split("/")
                risk_floor = route_for(Assessment(size, complexity, risk="high")).lead_effort
                if risk_floor not in efforts.get(model, []):
                    failures.append(f"{provider}/{pid}/{cell}: cannot preserve high-risk {risk_floor} effort")
            for gate in ("requires_all", "requires_any"):
                for model in profile.get(gate, []):
                    if not _current(model) or _model_rank(provider, model) is None:
                        failures.append(f"{provider}/{pid}: non-current or unknown gate model {model}")
            if pid != profiles[-1].get("id") and set(profile.get("requires_all", [])) != {choice["model"] for choice in matrix.values()}:
                failures.append(f"{provider}/{pid}: gate must cover selected models")
        if not profiles or failures:
            continue
        support = {model: efforts_by_model(profiles, model) for model in selected}
        roster = ([{"slug": model, "supported_reasoning_levels": [
            {"effort": level} for level in support.get(model, CODEX_EFFORTS)
        ]} for model in available]
                  if provider == "codex" else [{"id": model} for model in available])
        candidate = {"profiles": profiles, "model_order": sorted(selected), "model_efforts": support}
        try:
            validate_matrix(provider, candidate, profiles, roster)
        except (SystemExit, KeyError, TypeError, ValueError) as error:
            failures.append(f"{provider}: {error}")
    for failure in failures:
        print(f"::error::model policy: {failure}")
    if failures:
        return 1
    print("shipped matrices and reviewed policy are consistent (live provider support not checked)")
    return 0


def verify() -> int:
    """Reject any shipped model/effort selection the provider will not accept."""
    if check_policy(require_fresh=True):
        return 1
    document = json.loads(PROFILES.read_text(encoding="utf-8"))
    failures: list[str] = []
    for provider, block in document["providers"].items():
        models = {choice["model"] for profile in block["profiles"] for choice in profile.get("matrix", {}).values()}
        declared = {model: efforts_by_model(block["profiles"], model) for model in models}
        if provider == "codex":
            roster = {entry["slug"]: efforts_of(entry) for entry in codex_roster(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))}
            supported = {model: roster.get(model, []) for model in models}
            for model in models:
                if supported[model] != declared[model]:
                    failures.append(f"Codex {model}: declared effort support differs from provider roster")
        else:
            supported = {}
            for model in sorted(models):
                supported[model] = []
                for effort in CLAUDE_EFFORTS:
                    accepted, detail = _accepts(provider, model, effort)
                    if accepted:
                        supported[model].append(effort)
                    print(f"{provider} {model} {effort}: {'ok' if accepted else 'unsupported'} {detail}".rstrip())
                if supported[model] != declared[model]:
                    failures.append(f"Claude {model}: declared effort support differs from provider checks")
        for profile in block["profiles"]:
            for cell, choice in profile.get("matrix", {}).items():
                if choice["effort"] not in supported.get(choice["model"], []):
                    failures.append(f"{provider} {cell}: selected effort is unsupported for {choice['model']}")
        for model in sorted(models):
            if provider == "codex":
                accepted, detail = _accepts(provider, model)
                print(f"{provider} {model}: {'ok' if accepted else 'REJECTED'} {detail}".rstrip())
                if not accepted:
                    failures.append(f"{provider} {model}: {detail}")
    if failures:
        for failure in failures:
            print(f"::error::shipped profile names a model the provider rejects: {failure}")
        return 1
    print(f"every model in the shipped profiles is accepted by its provider")
    return 0


def efforts_by_model(profiles: list[dict], model: str) -> list[str]:
    return next((profile.get("efforts", {}).get(model, []) for profile in profiles if model in profile.get("efforts", {})), [])


def _accepts(provider: str, model: str, effort: str = "") -> tuple[bool, str]:
    """A minimal provider call verifies the model and selected effort."""
    if provider == "codex":
        argv = ["codex", "exec", "--ephemeral", "--model", model, "--skip-git-repo-check"]
        if effort:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        argv.append("reply with ok")
    else:
        argv = ["claude", "--bare", "--print", "--no-session-persistence", "--tools", "",
                "--model", model, "--output-format", "text"]
        if effort:
            argv.extend(["--effort", effort])
        argv.append("reply with ok")
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not run {argv[0]}: {error}"
    if completed.returncode == 0:
        return True, ""
    detail = (completed.stderr or completed.stdout).strip().splitlines()
    return False, detail[-1][:200] if detail else f"exit {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-probe", action="store_true", help="run provider matrix agents in parallel")
    parser.add_argument("--verify", action="store_true", help="check every shipped model resolves")
    parser.add_argument("--check-policy", action="store_true", help="check reviewed policy and shipped matrices offline")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
    )
    args = parser.parse_args()
    if args.check_policy and check_policy():
        return 1
    if args.agent_probe:
        if check_policy(require_fresh=True, validate_profiles=False):
            return 1
        changed = agent_probe(args.codex_home)
        if check_policy():
            return 1
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8") as stream:
                stream.write(f"changed={'true' if changed else 'false'}\n")
    if args.verify:
        return verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
