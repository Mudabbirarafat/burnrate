from __future__ import annotations

import pytest

from burnrate.burn import budget_remaining, burn_rate, evaluate_rule, time_to_exhaustion_hours
from burnrate.models import BurnRule, Severity, WindowStat

RULE = BurnRule(Severity.PAGE, 3600, 300, 14.4, "page-1h-5m")


def stat(valid: int, bad: int, window: int = 3600) -> WindowStat:
    return WindowStat(window_seconds=window, valid=valid, good=valid - bad)


def test_burn_rate_of_one_means_spending_exactly_the_budget():
    """1% bad against a 1% budget is a burn rate of 1 by definition."""
    assert burn_rate(stat(1000, 10), 0.01) == pytest.approx(1.0)


def test_burn_rate_scales_with_the_bad_ratio():
    assert burn_rate(stat(100, 20), 0.01) == pytest.approx(20.0)


def test_a_perfect_slo_has_no_budget_to_burn():
    with pytest.raises(ValueError, match="budget"):
        burn_rate(stat(10, 1), 0.0)


def test_rule_fires_only_when_both_windows_agree():
    long_hot = stat(60, 20)
    short_hot = stat(5, 2, window=300)
    short_cold = stat(5, 0, window=300)

    assert evaluate_rule(RULE, long_hot, short_hot, 0.01, min_samples=3).firing is True
    # The burn happened, but it has stopped: the short window is what keeps an
    # alert from smouldering for the rest of the hour.
    assert evaluate_rule(RULE, long_hot, short_cold, 0.01, min_samples=3).firing is False


def test_rule_does_not_fire_when_only_the_short_window_is_hot():
    assert evaluate_rule(RULE, stat(60, 0), stat(5, 3, window=300), 0.01, min_samples=3).firing is False


def test_a_thin_window_is_suppressed_rather_than_trusted():
    """One failure out of two probes is a 50% bad ratio and a burn rate of 50.

    Without the sample floor that single blip would page. This is the main
    quantisation trap of running burn-rate alerting on a 60-second prober
    rather than on a high-volume request stream.
    """
    result = evaluate_rule(RULE, stat(2, 1), stat(2, 1, window=300), 0.01, min_samples=3)
    assert result.long_burn > RULE.threshold
    assert result.firing is False
    assert result.suppressed_reason is not None
    assert "probes" in result.suppressed_reason


def test_suppression_reports_which_window_was_thin():
    result = evaluate_rule(RULE, stat(60, 30), stat(1, 1, window=300), 0.01, min_samples=3)
    assert result.firing is False
    assert "short window" in result.suppressed_reason


def test_evaluation_carries_the_numbers_it_decided_on():
    result = evaluate_rule(RULE, stat(100, 50), stat(10, 5, window=300), 0.01, min_samples=3)
    assert result.long_samples == 100
    assert result.short_samples == 10
    assert result.long_burn == pytest.approx(50.0)


def test_budget_remaining_never_goes_below_zero():
    assert budget_remaining(stat(100, 90), 0.01) == 0.0
    assert budget_remaining(stat(1000, 5), 0.01) == pytest.approx(0.5)


def test_time_to_exhaustion_is_none_when_nothing_burns():
    assert time_to_exhaustion_hours(0.0, 1.0) is None


def test_time_to_exhaustion_halves_when_burn_doubles():
    assert time_to_exhaustion_hours(1.0, 1.0, period_days=30) == pytest.approx(720.0)
    assert time_to_exhaustion_hours(2.0, 1.0, period_days=30) == pytest.approx(360.0)
