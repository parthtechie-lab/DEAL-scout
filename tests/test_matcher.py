"""
tests/test_matcher.py — pytest suite for matcher.py

Run: pytest tests/ -v
"""

import sys
from pathlib import Path

# Add scripts/ to path so we can import matcher directly
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pytest
from matcher import extract_deal_info, score_deal, load_weights, deduplicate_title


# ── extract_deal_info ─────────────────────────────────────────────────────────

class TestExtractPrice:
    def test_basic_rupee_symbol(self):
        info = extract_deal_info("Get boAt Rockerz at ₹799!")
        assert info["price"] == 799

    def test_rs_prefix(self):
        info = extract_deal_info("Rs. 499 only today")
        assert info["price"] == 499

    def test_price_with_commas(self):
        info = extract_deal_info("Laptop at ₹45,999")
        assert info["price"] == 45999

    def test_picks_lower_of_two_valid_prices(self):
        # ₹799 (deal) and ₹2000 (MRP) → should pick ₹799
        info = extract_deal_info("MRP ₹2000. Now at ₹799.")
        assert info["price"] == 799

    def test_filters_min_order_price(self):
        # "min order ₹399" should be filtered out
        info = extract_deal_info("₹150 off on min order ₹399. Use SAVE150.")
        assert info["price"] != 399, "Should not pick min-order threshold as deal price"

    def test_filters_above_context(self):
        info = extract_deal_info("Valid on orders above ₹499. Get ₹100 off.")
        assert info["price"] != 499, "Should not pick 'above ₹499' as deal price"

    def test_filters_cart_value_context(self):
        info = extract_deal_info("Cart value ₹299. Deal price ₹599.")
        assert info["price"] != 299

    def test_filters_below_minimum(self):
        # ₹5 is below the minimum threshold (₹49), so it should be filtered
        # regardless of context
        info = extract_deal_info("₹5 extra on cart. Product at ₹1299.")
        assert info["price"] == 1299, f"Expected ₹1299, got {info['price']}"

    def test_no_price_returns_none(self):
        info = extract_deal_info("Great deal! Flash sale today!")
        assert info["price"] is None

    def test_inr_prefix(self):
        info = extract_deal_info("INR 999 limited time")
        assert info["price"] == 999


class TestExtractDiscount:
    def test_basic_percent_off(self):
        info = extract_deal_info("Flat 60% off today!")
        assert info["discount_pct"] == 60

    def test_picks_highest_discount(self):
        # If two discounts in text, take max
        info = extract_deal_info("10% off on first item, 40% off on second. Extra 5% discount.")
        assert info["discount_pct"] == 40

    def test_no_discount_returns_none(self):
        info = extract_deal_info("Great product at ₹999")
        assert info["discount_pct"] is None

    def test_discount_with_word_discount(self):
        info = extract_deal_info("30% discount on all orders")
        assert info["discount_pct"] == 30


class TestExtractCoupon:
    def test_use_code(self):
        info = extract_deal_info("Use code BOAT60 at checkout")
        assert info["coupon_code"] == "BOAT60"

    def test_apply_code(self):
        info = extract_deal_info("Apply SAVE150 at cart page")
        assert info["coupon_code"] == "SAVE150"

    def test_quoted_code(self):
        info = extract_deal_info('Use "FLAT200" for discount')
        assert info["coupon_code"] == "FLAT200"

    def test_blacklisted_word_skipped(self):
        info = extract_deal_info("Apply TERMS and conditions. Use BOAT60.")
        assert info["coupon_code"] == "BOAT60"

    def test_no_coupon_returns_none(self):
        info = extract_deal_info("Great deal at ₹499, 50% off!")
        assert info["coupon_code"] is None

    def test_code_too_short_skipped(self):
        # Text with no uppercase trigger sequences — no code should be extracted
        info = extract_deal_info("great price at ₹999, check the product description")
        assert info["coupon_code"] is None


class TestExtractExpiry:
    def test_valid_till(self):
        info = extract_deal_info("Valid till 10 July")
        assert info["expiry"] is not None
        assert "10" in info["expiry"]

    def test_expires(self):
        info = extract_deal_info("Offer expires on 15 August")
        assert info["expiry"] is not None

    def test_no_expiry(self):
        info = extract_deal_info("₹799 off today")
        assert info["expiry"] is None


# ── score_deal ────────────────────────────────────────────────────────────────

class TestScoreDeal:
    def test_high_discount_high_reliability(self):
        score = score_deal(70, 9, 4, None, None)
        assert score >= 50, f"Expected ≥ 50, got {score}"

    def test_zero_discount_still_scores_from_reliability(self):
        score = score_deal(0, 10, 0, None, None)
        assert score > 0

    def test_price_below_target_boosts_score(self):
        score_with    = score_deal(50, 7, 2, 1000, 500)
        score_without = score_deal(50, 7, 2, None, None)
        assert score_with > score_without

    def test_score_bounded_0_to_100(self):
        score = score_deal(90, 10, 5, 1000, 1)
        assert 0 <= score <= 100

    def test_custom_weights(self):
        # Heavily weight discount — high discount should dominate
        weights = {"discount_percent": 0.9, "source_reliability": 0.033,
                   "keyword_match_count": 0.033, "price_below_target": 0.033}
        score = score_deal(80, 1, 0, None, None, weights=weights)
        assert score >= 70, f"With 80% discount and 0.9 weight, expected ≥ 70, got {score}"

    def test_none_discount_treated_as_zero(self):
        score = score_deal(None, 5, 3, None, None)
        assert isinstance(score, int)


# ── load_weights ──────────────────────────────────────────────────────────────

class TestLoadWeights:
    def test_loads_from_watchlist(self):
        wl = {"matching_rules": {"priority_weights": {
            "discount_percent": 0.5, "source_reliability": 0.2,
            "keyword_match_count": 0.2, "price_below_target": 0.1,
        }}}
        w = load_weights(wl)
        assert w["discount_percent"] == 0.5

    def test_fallback_on_missing_key(self):
        w = load_weights({})
        assert "discount_percent" in w
        assert w["discount_percent"] == 0.35

    def test_fallback_on_partial_weights(self):
        # If only some keys present, use defaults (incomplete config = ignore)
        wl = {"matching_rules": {"priority_weights": {"discount_percent": 0.5}}}
        w = load_weights(wl)
        assert w["discount_percent"] == 0.35  # falls back to default


# ── deduplicate_title ─────────────────────────────────────────────────────────

class TestDeduplicateTitle:
    def test_exact_duplicate_detected(self):
        assert deduplicate_title("boAt Rockerz deal", ["boAt Rockerz deal"]) is True

    def test_similar_title_detected(self):
        assert deduplicate_title(
            "boAt Rockerz 450 deal today",
            ["boAt Rockerz 450 deal available now"],
        ) is True

    def test_different_titles_not_flagged(self):
        assert deduplicate_title(
            "Sony WH-1000XM5 headphones",
            ["boAt Rockerz 450 deal today"],
        ) is False

    def test_empty_existing_not_flagged(self):
        assert deduplicate_title("anything", []) is False
