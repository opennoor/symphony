import unittest
from datetime import datetime, timedelta

from plugins.symphony.symphony.model import CapabilitySnapshot
from plugins.symphony.symphony.routing import (
    Assessment,
    model_is_weaker,
    snapshot_for,
    resolve_tier,
    route_for,
)


class RoutingTests(unittest.TestCase):
    def test_model_comparison_uses_reviewed_capability_ranks(self):
        self.assertFalse(model_is_weaker("gpt-6-sol", "gpt-6-luna"))
        self.assertFalse(model_is_weaker("gpt-5.6-luna", "gpt-6-luna"))
        self.assertTrue(model_is_weaker("gpt-6-luna", "gpt-6-sol"))
        self.assertTrue(model_is_weaker("unknown-model", "gpt-6-luna"))

    def test_nine_cell_matrix(self):
        expected = {
            ("small", "simple"): ("capable", "medium", "delegated", "none"),
            ("small", "mixed"): ("capable", "high", "delegated", "optional"),
            ("small", "complex"): ("strongest", "high", "delegated", "independent-check"),
            ("medium", "simple"): ("balanced", "medium", "mixed", "none"),
            ("medium", "mixed"): ("balanced", "high", "mixed", "optional"),
            ("medium", "complex"): ("capable", "high", "mixed", "reserved"),
            ("large", "simple"): ("economy", "low", "delegated", "none"),
            ("large", "mixed"): ("economy", "medium", "delegated", "reserved"),
            ("large", "complex"): ("economy", "medium", "delegated", "strongest"),
        }
        actual = {}
        for axes in expected:
            route = route_for(Assessment(*axes))
            actual[axes] = (route.lead_tier, route.lead_effort, route.execution, route.consultation)
        self.assertEqual(actual, expected)

    def test_high_risk_elevates_effort_without_mutating_axes(self):
        assessment = Assessment("large", "simple", risk="high")
        route = route_for(assessment)
        self.assertEqual((assessment.size, assessment.complexity), ("large", "simple"))
        self.assertEqual(route.lead_effort, "medium")
        self.assertTrue(route.independent_review)

    def test_resolver_chooses_lowest_available_model_that_satisfies_tier(self):
        snapshot = CapabilitySnapshot(
            provider="codex",
            available_models=("cheap", "solid", "best"),
            supported_efforts={
                "cheap": ("low", "medium"),
                "solid": ("low", "medium"),
                "best": ("medium", "high"),
            },
            tiers={"economy": "cheap", "capable": "solid", "strongest": "best"},
            source="live",
            provider_version=None,
            refreshed_at="2026-09-17T00:00:00+00:00",
        )
        resolved = resolve_tier(route_for(Assessment("small", "simple")), snapshot)
        self.assertEqual((resolved["lead_model"], resolved["lead_effort"]), ("solid", "medium"))

    def test_resolver_falls_back_to_supported_effort(self):
        snapshot = CapabilitySnapshot(
            provider="claude",
            available_models=("one",),
            supported_efforts={"one": ("low", "medium")},
            tiers={"capable": "one"},
            source="live",
            provider_version=None,
            refreshed_at="2026-09-17T00:00:00+00:00",
        )
        resolved = resolve_tier(route_for(Assessment("small", "mixed")), snapshot)
        self.assertEqual(resolved["lead_effort"], "medium")
        self.assertTrue(resolved["degraded"])

    def test_provider_cell_overrides_model_and_effort_but_keeps_high_risk_floor(self):
        snapshot = CapabilitySnapshot(
            provider="codex",
            available_models=("cheap", "best"),
            supported_efforts={"cheap": ("low", "medium"), "best": ("low", "medium", "high")},
            tiers={"capable": "cheap", "strongest": "best"},
            source="profile:full",
            provider_version=None,
            refreshed_at="2026-09-17T00:00:00+00:00",
            matrix={"small/simple": {"model": "best", "effort": "low"}},
        )
        normal = resolve_tier(route_for(Assessment("small", "simple")), snapshot)
        risky = resolve_tier(route_for(Assessment("small", "simple", risk="high")), snapshot)
        self.assertEqual((normal["lead_model"], normal["lead_effort"]), ("best", "low"))
        self.assertEqual(risky["lead_effort"], "medium")

    def test_high_risk_does_not_silently_drop_below_its_floor(self):
        snapshot = CapabilitySnapshot(
            provider="codex", available_models=("cheap",),
            supported_efforts={"cheap": ("low",)}, tiers={"economy": "cheap"},
            source="profile:floor", provider_version=None, refreshed_at="",
            matrix={"large/simple": {"model": "cheap", "effort": "low"}},
        )
        resolved = resolve_tier(route_for(Assessment("large", "simple", risk="high")), snapshot)
        self.assertEqual(resolved["lead_effort"], "medium")
        self.assertTrue(resolved["degraded"])

    def test_shipped_provider_fallbacks_resolve_capable_leads(self):
        route = route_for(Assessment("small", "simple"))

        codex = resolve_tier(route, snapshot_for("codex", "full"))
        claude = resolve_tier(route, snapshot_for("claude", "opus"))
        codex_choice = snapshot_for("codex", "full").matrix["small/simple"]
        claude_choice = snapshot_for("claude", "opus").matrix["small/simple"]

        self.assertEqual((codex["lead_model"], codex["lead_effort"]), (codex_choice["model"], codex_choice["effort"]))
        self.assertEqual((claude["lead_model"], claude["lead_effort"]), (claude_choice["model"], claude_choice["effort"]))

    def test_invalid_axes_are_rejected(self):
        with self.assertRaises(ValueError):
            route_for(Assessment("gigantic", "simple"))


if __name__ == "__main__":
    unittest.main()
