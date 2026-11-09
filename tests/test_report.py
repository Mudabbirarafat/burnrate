from __future__ import annotations

import pytest

from burnrate.config import Config
from burnrate.report import full_report, incident_timeline, percentile
from burnrate.models import Severity, SliKind
from burnrate.storage import Storage

from .conftest import NOW, fill


def test_percentile_of_an_empty_sample_is_none():
    assert percentile([], 0.95) is None


def test_percentile_uses_nearest_rank():
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(values, 0.50) == 50.0
    assert percentile(values, 0.95) == 95.0
    assert percentile(values, 1.0) == 100.0


def test_percentile_rejects_a_nonsense_quantile():
    with pytest.raises(ValueError):
        percentile([1.0], 1.5)


def test_report_states_the_sample_count_behind_each_number(config: Config, storage: Storage):
    fill(storage, "svc", count=60, bad=6, now=NOW)
    text = full_report(config, storage, NOW)
    assert "probes" in text
    assert "| 60 |" in text  # the denominator is printed, not just the ratio


def test_report_warns_while_history_is_too_short(config: Config, storage: Storage):
    fill(storage, "svc", count=10, now=NOW)
    assert "burn-rate rules cannot fire meaningfully yet" in full_report(config, storage, NOW)


def test_timeline_reports_mttr_over_resolved_incidents_only(storage: Storage):
    closed = storage.open_alert("svc", SliKind.AVAILABILITY, Severity.PAGE, "r", NOW, 20.0, 18.0)
    storage.close_alert(closed, NOW + 600)
    storage.open_alert("svc", SliKind.LATENCY, Severity.TICKET, "r", NOW + 100, 4.0, 4.0)

    text = incident_timeline(storage, NOW + 900)
    assert "Resolved incidents: 1" in text
    assert "10m 0s" in text
    assert "OPEN" in text


def test_timeline_is_honest_about_what_mttr_means(storage: Storage):
    alert_id = storage.open_alert("svc", SliKind.AVAILABILITY, Severity.PAGE, "r", NOW, 20.0, 18.0)
    storage.close_alert(alert_id, NOW + 60)
    assert "it does not repair them" in incident_timeline(storage, NOW + 120)


def test_empty_timeline_says_so(storage: Storage):
    assert "None recorded" in incident_timeline(storage, NOW)
