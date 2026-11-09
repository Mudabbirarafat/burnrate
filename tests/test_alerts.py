from __future__ import annotations

from burnrate.alerts import AlertEngine, TransitionKind
from burnrate.config import Config
from burnrate.models import Severity, SliKind
from burnrate.storage import Storage

from .conftest import NOW, fill


def test_quiet_service_produces_no_alerts(config: Config, storage: Storage):
    fill(storage, "svc", count=120, now=NOW)
    assert AlertEngine(config, storage).evaluate(NOW) == []


def test_an_outage_opens_a_page(config: Config, storage: Storage):
    """Every probe in the last hour failed: burn is 100x against a 1% budget."""
    fill(storage, "svc", count=60, bad=60, now=NOW)
    transitions = AlertEngine(config, storage).evaluate(NOW)
    opened = [t for t in transitions if t.kind is TransitionKind.OPENED]
    assert any(t.severity is Severity.PAGE and t.sli is SliKind.AVAILABILITY for t in opened)


def test_an_alert_opens_once_and_not_on_every_tick(config: Config, storage: Storage):
    fill(storage, "svc", count=60, bad=60, now=NOW)
    engine = AlertEngine(config, storage)
    first = engine.evaluate(NOW)
    second = engine.evaluate(NOW + 60)
    assert [t for t in first if t.kind is TransitionKind.OPENED]
    assert second == []
    assert len(storage.alerts()) == len([t for t in first if t.kind is TransitionKind.OPENED])


def test_recovery_needs_consecutive_clean_evaluations(config: Config, storage: Storage):
    """resolve_after is 2, so one clean tick must not close the incident."""
    fill(storage, "svc", count=60, bad=60, now=NOW)
    engine = AlertEngine(config, storage)
    engine.evaluate(NOW)

    recovered_at = NOW + 3600
    fill(storage, "svc", count=60, bad=0, now=recovered_at)

    assert engine.evaluate(recovered_at) == []  # first clean tick: still open
    closed = [t for t in engine.evaluate(recovered_at + 60) if t.kind is TransitionKind.CLOSED]
    assert {t.severity for t in closed} == {Severity.PAGE}


def test_the_ticket_stays_open_after_the_page_clears(config: Config, storage: Storage):
    """Recovery is not absolution.

    An hour of total outage is spent budget. Once traffic is healthy again the
    1h/5m page resolves, but the 1d/2h ticket keeps firing because the day's
    budget really is gone. Someone still has to look at it on Monday, which is
    the entire reason the ticket severity exists.
    """
    fill(storage, "svc", count=60, bad=60, now=NOW)
    engine = AlertEngine(config, storage)
    engine.evaluate(NOW)

    recovered_at = NOW + 3600
    fill(storage, "svc", count=60, bad=0, now=recovered_at)
    engine.evaluate(recovered_at)
    engine.evaluate(recovered_at + 60)

    still_open = {
        (row["kind"], row["severity"]) for row in storage.alerts() if row["closed_ts"] is None
    }
    assert (SliKind.AVAILABILITY.value, Severity.TICKET.value) in still_open
    assert (SliKind.AVAILABILITY.value, Severity.PAGE.value) not in still_open


def test_a_blip_inside_a_thin_window_does_not_page(config: Config, storage: Storage):
    """Two probes, one failed. 50% bad, burn of 50, and still not a page."""
    fill(storage, "svc", count=2, bad=1, now=NOW)
    assert AlertEngine(config, storage).evaluate(NOW) == []


def test_slow_but_healthy_service_burns_the_latency_budget_only(config: Config, storage: Storage):
    fill(storage, "svc", count=60, slow=60, slow_latency_ms=9000.0, now=NOW)
    transitions = AlertEngine(config, storage).evaluate(NOW)
    kinds = {t.sli for t in transitions if t.kind is TransitionKind.OPENED}
    assert SliKind.LATENCY in kinds
    assert SliKind.AVAILABILITY not in kinds


def test_severities_are_tracked_independently(config: Config, storage: Storage):
    fill(storage, "svc", count=1440, bad=1440, now=NOW)
    engine = AlertEngine(config, storage)
    opened = [t for t in engine.evaluate(NOW) if t.kind is TransitionKind.OPENED]
    severities = {t.severity for t in opened if t.sli is SliKind.AVAILABILITY}
    assert severities == {Severity.PAGE, Severity.TICKET}


def test_transition_summary_names_the_rule_and_the_burn(config: Config, storage: Storage):
    fill(storage, "svc", count=60, bad=60, now=NOW)
    opened = [t for t in AlertEngine(config, storage).evaluate(NOW) if t.kind is TransitionKind.OPENED]
    text = opened[0].summary()
    assert "FIRING" in text and "svc" in text and "burn" in text


def test_restart_does_not_instantly_resolve_an_open_incident(config: Config, storage: Storage):
    """The clean counter is in memory; a fresh engine must re-earn the resolve."""
    fill(storage, "svc", count=60, bad=60, now=NOW)
    AlertEngine(config, storage).evaluate(NOW)

    recovered_at = NOW + 3600
    fill(storage, "svc", count=60, bad=0, now=recovered_at)

    fresh = AlertEngine(config, storage)
    assert fresh.evaluate(recovered_at) == []
    assert [t for t in fresh.evaluate(recovered_at + 60) if t.kind is TransitionKind.CLOSED]
