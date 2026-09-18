# -*- coding: utf-8 -*-

"""
Unit tests for the published credit table (model_costs.py).

Covers single-rate models, the two-tier GPT-5.6 family, and the tier boundary
itself: the rate must only change for a request strictly above the threshold.
"""

from kiro import model_costs
from kiro.model_costs import (
    GPT_5_6_LONG_THRESHOLD,
    MODEL_COSTS,
    ModelCost,
    cost_for,
    credits_for,
    multiplier_for,
    table,
)

# =============================================================================
# Tests for ModelCost.multiplier_at
# =============================================================================


class TestMultiplierAt:
    """Tier selection on the dataclass itself."""

    def test_single_rate_model_ignores_token_count(self):
        """
        What it does: Verifies a model with no long tier returns its one rate.
        Purpose: Ensure adding the optional fields did not change existing entries.
        """
        print("Setup: Single-rate entry...")
        entry = ModelCost(1.3, 200_000)

        print("Action: Asking for the rate at 0, None and 5M tokens...")
        rates = [entry.multiplier_at(None), entry.multiplier_at(0), entry.multiplier_at(5_000_000)]

        print(f"Comparing rates: Expected all 1.3, Got {rates}")
        assert rates == [1.3, 1.3, 1.3]

    def test_two_tier_model_below_threshold_uses_short_rate(self):
        """
        What it does: Verifies a request under the threshold bills at the short rate.
        Purpose: Ensure the cheaper tier is not skipped.
        """
        print("Setup: Two-tier entry, 4.4x short and 8.8x long above 272000...")
        entry = ModelCost(4.4, 1_000_000, 8.8, 272_000)

        print("Action: Asking for the rate at 271999 tokens...")
        rate = entry.multiplier_at(271_999)

        print(f"Comparing rate: Expected 4.4, Got {rate}")
        assert rate == 4.4

    def test_two_tier_model_at_threshold_uses_short_rate(self):
        """
        What it does: Verifies the threshold itself still bills at the short rate.
        Purpose: The published rule is "above 272K", so the boundary is inclusive
                 of the cheaper tier.
        """
        print("Setup: Two-tier entry...")
        entry = ModelCost(4.4, 1_000_000, 8.8, 272_000)

        print("Action: Asking for the rate at exactly 272000 tokens...")
        rate = entry.multiplier_at(272_000)

        print(f"Comparing rate: Expected 4.4, Got {rate}")
        assert rate == 4.4

    def test_two_tier_model_above_threshold_uses_long_rate(self):
        """
        What it does: Verifies one token past the threshold switches tiers.
        Purpose: Ensure the long-context rate is actually reachable.
        """
        print("Setup: Two-tier entry...")
        entry = ModelCost(4.4, 1_000_000, 8.8, 272_000)

        print("Action: Asking for the rate at 272001 tokens...")
        rate = entry.multiplier_at(272_001)

        print(f"Comparing rate: Expected 8.8, Got {rate}")
        assert rate == 8.8

    def test_two_tier_model_without_token_count_uses_short_rate(self):
        """
        What it does: Verifies an unknown token count falls back to the short rate.
        Purpose: A caller with no count gets the tier most requests land in
                 rather than nothing at all.
        """
        print("Setup: Two-tier entry...")
        entry = ModelCost(4.4, 1_000_000, 8.8, 272_000)

        print("Action: Asking for the rate with no token count...")
        rate = entry.multiplier_at()

        print(f"Comparing rate: Expected 4.4, Got {rate}")
        assert rate == 4.4

    def test_threshold_without_long_rate_stays_single_rate(self):
        """
        What it does: Verifies a threshold with no long rate changes nothing.
        Purpose: Ensure no rate is invented for a half-filled entry.
        """
        print("Setup: Entry with a threshold but no long multiplier...")
        entry = ModelCost(2.0, 500_000, None, 272_000)

        print("Action: Asking for the rate well above the threshold...")
        rate = entry.multiplier_at(900_000)

        print(f"Comparing rate: Expected 2.0, Got {rate}")
        assert rate == 2.0


# =============================================================================
# Tests for the published GPT-5.6 entries
# =============================================================================


class TestGpt56Entries:
    """The three GPT-5.6 models carry both published rates."""

    def test_long_threshold_is_272k(self):
        """
        What it does: Verifies the shared threshold constant.
        Purpose: Pin the published boundary in one place.
        """
        print(f"Comparing threshold: Expected 272000, Got {GPT_5_6_LONG_THRESHOLD}")
        assert GPT_5_6_LONG_THRESHOLD == 272_000

    def test_each_model_doubles_above_the_threshold(self):
        """
        What it does: Verifies sol/terra/luna store the published rate pairs.
        Purpose: Ensure the long rate is double the short one, as announced.
        """
        print("Setup: Published pairs from Kiro's changelog...")
        expected = {
            "gpt-5.6-sol": (4.4, 8.8),
            "gpt-5.6-terra": (2.2, 4.4),
            "gpt-5.6-luna": (1.1, 2.2),
        }

        for model, (short, long_rate) in expected.items():
            entry = MODEL_COSTS[model]
            print(f"Checking {model}: {entry.multiplier}x / {entry.long_multiplier}x")
            assert entry.multiplier == short
            assert entry.long_multiplier == long_rate
            assert entry.long_threshold == GPT_5_6_LONG_THRESHOLD

    def test_each_model_has_the_1m_window(self):
        """
        What it does: Verifies the context window raised to 1M.
        Purpose: The long tier only matters because the window allows it.
        """
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            print(f"Checking {model}: {MODEL_COSTS[model].context_tokens}")
            assert MODEL_COSTS[model].context_tokens == 1_000_000

    def test_no_other_model_declares_a_long_tier(self):
        """
        What it does: Verifies only the GPT-5.6 family is two-tier.
        Purpose: No tier is invented for a model that publishes none.
        """
        print("Action: Collecting models with a long multiplier...")
        two_tier = sorted(model for model, entry in MODEL_COSTS.items() if entry.long_multiplier is not None)

        print(f"Comparing set: Got {two_tier}")
        assert two_tier == ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"]


# =============================================================================
# Tests for multiplier_for and credits_for
# =============================================================================


class TestMultiplierFor:
    """Lookup by name, with the tier applied."""

    def test_unknown_model_returns_none(self):
        """
        What it does: Verifies an unknown model yields None.
        Purpose: Callers show nothing rather than a fabricated rate.
        """
        print("Action: Looking up a model that does not exist...")
        assert multiplier_for("not-a-model") is None

    def test_none_model_returns_none(self):
        """
        What it does: Verifies a missing model name yields None.
        Purpose: Request logs hold rows with no model at all.
        """
        assert multiplier_for(None) is None

    def test_normalized_alias_resolves(self):
        """
        What it does: Verifies a dashed alias finds the dotted entry.
        Purpose: Clients send claude-sonnet-4-5 for claude-sonnet-4.5.
        """
        print("Action: Looking up claude-sonnet-4-5...")
        assert multiplier_for("claude-sonnet-4-5") == MODEL_COSTS["claude-sonnet-4.5"].multiplier

    def test_tier_applies_through_the_lookup(self):
        """
        What it does: Verifies the token count reaches the tier selection.
        Purpose: Ensure the keyword is not silently dropped by the wrapper.
        """
        print("Action: Looking up gpt-5.6-sol at both tiers...")
        short = multiplier_for("gpt-5.6-sol", 100_000)
        long_rate = multiplier_for("gpt-5.6-sol", 500_000)

        print(f"Comparing rates: Expected 4.4 and 8.8, Got {short} and {long_rate}")
        assert (short, long_rate) == (4.4, 8.8)


class TestCreditsFor:
    """Scaling a baseline figure by the applicable rate."""

    def test_unknown_model_returns_none(self):
        """
        What it does: Verifies an unknown model produces no estimate.
        Purpose: Never guess a credit figure.
        """
        assert credits_for("not-a-model", 10.0) is None

    def test_baseline_model_is_unchanged(self):
        """
        What it does: Verifies auto scales by 1.0x.
        Purpose: auto is the baseline the whole table is relative to.
        """
        print("Action: Scaling 10 credits on auto...")
        assert credits_for(model_costs.BASELINE_MODEL, 10.0) == 10.0

    def test_short_tier_scales_by_short_rate(self):
        """
        What it does: Verifies a small request uses the short rate.
        Purpose: The common case must not be overcharged.
        """
        print("Action: Scaling 10 credits on gpt-5.6-terra at 1000 tokens...")
        result = credits_for("gpt-5.6-terra", 10.0, 1_000)

        print(f"Comparing result: Expected 22.0, Got {result}")
        assert result == 22.0

    def test_long_tier_scales_by_long_rate(self):
        """
        What it does: Verifies a request past the threshold doubles.
        Purpose: This is the drift the tracked issue was about.
        """
        print("Action: Scaling 10 credits on gpt-5.6-terra at 300000 tokens...")
        result = credits_for("gpt-5.6-terra", 10.0, 300_000)

        print(f"Comparing result: Expected 44.0, Got {result}")
        assert result == 44.0

    def test_missing_token_count_uses_short_tier(self):
        """
        What it does: Verifies omitting the count bills at the short rate.
        Purpose: Documented behaviour: it understates rather than refuses.
        """
        print("Action: Scaling 10 credits on gpt-5.6-terra with no count...")
        assert credits_for("gpt-5.6-terra", 10.0) == 22.0


# =============================================================================
# Tests for table()
# =============================================================================


class TestTable:
    """The dashboard payload."""

    def test_every_model_is_listed(self):
        """
        What it does: Verifies the table covers the whole dict.
        Purpose: A dropped row hides a model's price from operators.
        """
        rows = table()
        print(f"Comparing count: Expected {len(MODEL_COSTS)}, Got {len(rows)}")
        assert len(rows) == len(MODEL_COSTS)

    def test_rows_are_ordered_by_descending_multiplier(self):
        """
        What it does: Verifies the most expensive model comes first.
        Purpose: The panel renders the list as-is.
        """
        multipliers = [row["multiplier"] for row in table()]
        print(f"Result: {multipliers}")
        assert multipliers == sorted(multipliers, reverse=True)

    def test_two_tier_row_exposes_both_rates(self):
        """
        What it does: Verifies the long rate and threshold reach the payload.
        Purpose: The dashboard shows the tier split instead of one number.
        """
        row = next(row for row in table() if row["model"] == "gpt-5.6-sol")

        print(f"Result: {row}")
        assert row["multiplier"] == 4.4
        assert row["longMultiplier"] == 8.8
        assert row["longThresholdTokens"] == 272_000

    def test_single_rate_row_reports_null_long_fields(self):
        """
        What it does: Verifies single-rate models carry explicit nulls.
        Purpose: A consumer can tell "no second tier" from a matching one.
        """
        row = next(row for row in table() if row["model"] == "claude-opus-5")

        print(f"Result: {row}")
        assert row["longMultiplier"] is None
        assert row["longThresholdTokens"] is None

    def test_zero_context_is_reported_as_null(self):
        """
        What it does: Verifies auto's 0 window becomes null.
        Purpose: Pre-existing behaviour the panel renders as a dash.
        """
        row = next(row for row in table() if row["model"] == "auto")

        print(f"Result: {row}")
        assert row["contextTokens"] is None


# =============================================================================
# Tests for cost_for
# =============================================================================


class TestCostFor:
    """Entry lookup."""

    def test_returns_the_entry_for_a_known_model(self):
        """
        What it does: Verifies a known model yields its ModelCost.
        Purpose: The tier-aware helpers all go through this.
        """
        entry = cost_for("gpt-5.6-luna")

        print(f"Result: {entry}")
        assert entry is MODEL_COSTS["gpt-5.6-luna"]

    def test_is_case_insensitive(self):
        """
        What it does: Verifies an uppercase model name resolves.
        Purpose: Clients are not consistent about casing.
        """
        assert cost_for("GPT-5.6-Luna") is MODEL_COSTS["gpt-5.6-luna"]

    def test_empty_model_returns_none(self):
        """
        What it does: Verifies an empty name yields None.
        Purpose: Guard the falsy path explicitly.
        """
        assert cost_for("") is None
