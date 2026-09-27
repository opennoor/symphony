"""Fixed task routing with provider-specific capability resolution."""

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path

from .model import CapabilitySnapshot


TIERS = ("economy", "balanced", "capable", "strongest")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class Assessment:
    size: str
    complexity: str
    risk: str = "normal"
    rationale: str = ""
    topology: str = ""


@dataclass(frozen=True)
class Route:
    lead_tier: str
    lead_effort: str
    execution: str
    consultation: str
    independent_review: bool = False
    size: str = ""
    complexity: str = ""
    risk: str = "normal"


MATRIX = {
    ("small", "simple"): Route("capable", "medium", "direct", "none"),
    ("small", "mixed"): Route("capable", "high", "direct", "optional"),
    ("small", "complex"): Route("strongest", "high", "direct", "independent-check", True),
    ("medium", "simple"): Route("balanced", "medium", "mixed", "none"),
    ("medium", "mixed"): Route("balanced", "high", "mixed", "optional"),
    ("medium", "complex"): Route("capable", "high", "mixed", "reserved"),
    ("large", "simple"): Route("economy", "low", "delegated", "none"),
    ("large", "mixed"): Route("economy", "medium", "delegated", "reserved"),
    ("large", "complex"): Route("economy", "medium", "delegated", "strongest"),
}

PROFILES_PATH = Path(__file__).resolve().parent.parent / "profiles.json"
NO_PROFILE = "unavailable"


@lru_cache(maxsize=1)
def _profiles() -> dict:
    """The shipped tier-to-model profiles, maintained at release time."""
    providers = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))["providers"]
    for provider in ("codex", "claude"):
        profiles = providers[provider]["profiles"]
        if not isinstance(profiles, list) or not profiles or any(not isinstance(item, dict) for item in profiles):
            raise ValueError(f"{provider} needs a nonempty profile list")
        ids = [item.get("id") for item in profiles]
        if (any(not isinstance(pid, str) or not pid or pid == NO_PROFILE for pid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError(f"{provider} needs unique profile IDs excluding {NO_PROFILE!r}")
    return providers


def profiles_for(provider: str) -> tuple[dict, ...]:
    """Every shipped profile in preference order, unknown-entitlement floor last."""
    return tuple(_profiles()[provider]["profiles"])


def snapshot_for(provider: str, profile_id: str | None = None) -> CapabilitySnapshot:
    """The capability snapshot for one entitlement profile.

    With no profile named, use the last profile as a conservative default for
    unknown entitlement. A known roster with no matching profile is unavailable.
    """
    if profile_id == NO_PROFILE:
        return CapabilitySnapshot(provider, (), {}, {}, "profile:unavailable", None, "")
    profiles = profiles_for(provider)
    profile = next(
        (item for item in profiles if item["id"] == profile_id),
        profiles[-1],
    )
    tiers = dict(profile["tiers"])
    efforts = {model: tuple(levels) for model, levels in profile["efforts"].items()}
    matrix = dict(profile.get("matrix", {}))
    return CapabilitySnapshot(
        provider=provider,
        available_models=tuple(dict.fromkeys([*tiers.values(), *(item["model"] for item in matrix.values())])),
        supported_efforts=efforts,
        tiers=tiers,
        source=f"profile:{profile['id']}",
        provider_version=None,
        refreshed_at=_profiles_generated_at(),
        matrix=matrix,
    )


def _profiles_generated_at() -> str:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8")).get("generated_at", "")


def route_for(assessment: Assessment) -> Route:
    """Return the literal matrix route, applying only risk safeguards."""
    try:
        route = MATRIX[(assessment.size, assessment.complexity)]
    except KeyError as error:
        raise ValueError(f"unsupported assessment: {assessment.size}/{assessment.complexity}") from error
    if assessment.risk == "high":
        effort = "medium" if route.lead_effort == "low" else route.lead_effort
        route = replace(route, lead_effort=effort, independent_review=True)
    return replace(route, size=assessment.size, complexity=assessment.complexity, risk=assessment.risk)


def resolve_tier(route: Route, snapshot: CapabilitySnapshot) -> dict[str, object]:
    """Resolve a provider's cell choice, retaining tier fallback for old profiles."""
    selection = snapshot.matrix.get(f"{route.size}/{route.complexity}")
    if selection:
        model = str(selection["model"])
        requested_effort = str(selection["effort"])
    else:
        requested = TIERS.index(route.lead_tier)
        candidates = (
            snapshot.tiers[tier]
            for tier in TIERS[requested:]
            if tier in snapshot.tiers and snapshot.tiers[tier] in snapshot.available_models
        )
        model = next(candidates, snapshot.available_models[-1] if snapshot.available_models else "")
        requested_effort = route.lead_effort
    if route.risk == "high" and EFFORTS.index(route.lead_effort) > EFFORTS.index(requested_effort):
        requested_effort = route.lead_effort
    supported = snapshot.supported_efforts.get(model, ())
    effort = _supported_effort(requested_effort, supported)
    if route.risk == "high" and EFFORTS.index(effort) < EFFORTS.index(route.lead_effort):
        effort = route.lead_effort
    return {
        "lead_tier": route.lead_tier,
        "lead_effort": effort,
        "execution": route.execution,
        "consultation": route.consultation,
        "independent_review": route.independent_review,
        "lead_model": model,
        "degraded": (
            not model
            or model not in snapshot.available_models
            or effort != requested_effort
            or effort not in supported
        ),
    }


def _supported_effort(requested: str, supported: tuple[str, ...]) -> str:
    if requested in supported:
        return requested
    if not supported:
        return requested
    target = EFFORTS.index(requested) if requested in EFFORTS else len(EFFORTS)
    lower = [effort for effort in supported if effort in EFFORTS and EFFORTS.index(effort) <= target]
    return max(lower, key=EFFORTS.index) if lower else min(supported, key=EFFORTS.index)


def assessor_selection(snapshot: CapabilitySnapshot, requested: str = "off") -> dict[str, str]:
    """Keep assessor boosts inside the account's already accepted entitlement."""
    model = snapshot.tiers.get("strongest", "")
    supported = snapshot.supported_efforts.get(model, ())
    target = "high" if requested == "off" else requested
    effort = target if target in supported else ""
    return {"requested_model": model, "requested_effort": target,
            "model": model, "effort": effort}


def model_is_weaker(model: str, baseline: str) -> bool:
    """Compare reviewed capability ranks; unknown models stay conservative."""
    if model == baseline:
        return False
    try:
        models = json.loads((PROFILES_PATH.parent / "model-policy.json").read_text(encoding="utf-8"))["models"]
        return models[model]["capability_rank"] < models[baseline]["capability_rank"]
    except (OSError, ValueError, KeyError, TypeError):
        return True


def clamp_against_best(provider: str, route: Route, profile_id: str | None) -> dict[str, object]:
    """How far this account's entitlement moves a route off the matrix.

    A clamp compares the applied route with the primary policy profile. A
    different model needs consent only when its reviewed capability is lower.
    """
    best = resolve_tier(route, snapshot_for(provider, profiles_for(provider)[0]["id"]))
    actual = resolve_tier(route, snapshot_for(provider, profile_id))
    best_model = best["lead_model"]
    actual_model = actual["lead_model"]
    return {
        "tier_clamped": model_is_weaker(actual_model, best_model),
        "effort_clamped": actual["lead_effort"] != best["lead_effort"],
        "intended_model": best_model,
        "intended_effort": best["lead_effort"],
        "actual_model": actual_model,
        "actual_effort": actual["lead_effort"],
    }
