from __future__ import annotations

import pytest

from burnrate.models import BurnRule, ErrorKind, ProbeResult, Severity, SliKind, Slo, Target, WindowStat


def test_error_budget_is_the_complement_of_the_objective():
    slo = Slo(availability=0.99, latency_objective=0.95)
    assert slo.error_budget_for(SliKind.AVAILABILITY) == pytest.approx(0.01)
    assert slo.error_budget_for(SliKind.LATENCY) == pytest.approx(0.05)


def test_failed_probe_is_never_fast():
    """A connection refused returns in 3 ms. That is not a fast page."""
    failed = ProbeResult(target="svc", ts=0.0, ok=False, latency_ms=3.0, error_kind=ErrorKind.CONNECT)
    assert failed.is_fast(1000.0) is False


def test_fast_is_inclusive_of_the_threshold():
    exactly = ProbeResult(target="svc", ts=0.0, ok=True, status=200, latency_ms=1000.0)
    just_over = ProbeResult(target="svc", ts=0.0, ok=True, status=200, latency_ms=1000.1)
    assert exactly.is_fast(1000.0) is True
    assert just_over.is_fast(1000.0) is False


def test_probe_with_no_latency_is_not_fast():
    assert ProbeResult(target="svc", ts=0.0, ok=True, latency_ms=None).is_fast(10.0) is False


def test_empty_window_reads_as_zero_bad_not_all_bad():
    """No data must not look like a total outage."""
    assert WindowStat(window_seconds=300, valid=0, good=0).bad_ratio == 0.0


def test_window_stat_ratios():
    stat = WindowStat(window_seconds=3600, valid=50, good=45)
    assert stat.bad == 5
    assert stat.bad_ratio == pytest.approx(0.1)


def test_target_rejects_a_non_http_url():
    with pytest.raises(ValueError, match="http"):
        Target(name="svc", url="ftp://example.test/")


def test_target_rejects_an_empty_name():
    with pytest.raises(ValueError, match="name"):
        Target(name="", url="https://example.test/")


def test_burn_rule_rejects_a_short_window_that_is_not_shorter():
    with pytest.raises(ValueError, match="shorter"):
        BurnRule(Severity.PAGE, long_window_seconds=300, short_window_seconds=300, threshold=14.4, label="bad")


def test_burn_rule_rejects_a_non_positive_threshold():
    with pytest.raises(ValueError, match="positive"):
        BurnRule(Severity.PAGE, 3600, 300, 0.0, "bad")
