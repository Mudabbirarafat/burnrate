"""Burn-rate arithmetic and rule evaluation.

Burn rate is the ratio of how fast you are spending error budget to the rate
that would spend exactly all of it over the SLO period. A burn rate of 1 means
you finish the period having used the budget precisely; 14.4 means you would
burn a month's budget in roughly two days, and 2% of it in the past hour.

    burn_rate = observed_bad_ratio / error_budget

There is no SLO period in that expression, and that is the point: the period
cancels out, which is why the same thresholds work for a 30-day SLO and a
7-day one.
"""

from __future__ import annotations

from .models import BurnRule, RuleEvaluation, WindowStat


def burn_rate(stat: WindowStat, error_budget: float) -> float:
    """Burn rate implied by one window's good/valid counts."""
    if error_budget <= 0:
        raise ValueError("error budget must be positive; a 100% SLO has no budget to burn")
    return stat.bad_ratio / error_budget


def evaluate_rule(
    rule: BurnRule,
    long_stat: WindowStat,
    short_stat: WindowStat,
    error_budget: float,
    min_samples: int,
) -> RuleEvaluation:
    """Decide whether one rule is firing right now.

    Both windows must exceed the threshold. The long window establishes that
    the burn is real; the short window establishes that it is still happening.
    Either window holding too few probes suppresses the rule outright rather
    than letting a one-probe blip speak for a whole window.
    """
    long_burn = burn_rate(long_stat, error_budget)
    short_burn = burn_rate(short_stat, error_budget)

    reason: str | None = None
    if long_stat.valid < min_samples:
        reason = f"long window has {long_stat.valid} probes, need {min_samples}"
    elif short_stat.valid < min_samples:
        reason = f"short window has {short_stat.valid} probes, need {min_samples}"

    firing = reason is None and long_burn >= rule.threshold and short_burn >= rule.threshold

    return RuleEvaluation(
        rule=rule,
        long_burn=long_burn,
        short_burn=short_burn,
        long_samples=long_stat.valid,
        short_samples=short_stat.valid,
        firing=firing,
        suppressed_reason=reason,
    )


def budget_remaining(stat: WindowStat, error_budget: float) -> float:
    """Fraction of the error budget left, given the bad ratio over a window.

    Clamped at zero: a budget cannot go more than empty, and reporting -340%
    reads as a bug rather than as an outage.
    """
    if error_budget <= 0:
        return 0.0
    return max(0.0, 1.0 - stat.bad_ratio / error_budget)


def time_to_exhaustion_hours(current_burn: float, remaining_fraction: float, period_days: int = 30) -> float | None:
    """How long the remaining budget lasts if the current burn rate holds.

    Returns None when nothing is burning, because "never" is not a number and
    pretending it is one puts infinities on a dashboard.
    """
    if current_burn <= 0:
        return None
    return (remaining_fraction * period_days * 24.0) / current_burn
