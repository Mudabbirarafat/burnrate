from __future__ import annotations

from burnrate.models import ErrorKind, ProbeResult, Severity, SliKind
from burnrate.storage import Storage

from .conftest import NOW, fill


def test_window_counts_only_what_is_inside_it(storage: Storage):
    fill(storage, "svc", count=120, now=NOW, interval=60.0)  # two hours of probes
    hour = storage.window_stat("svc", SliKind.AVAILABILITY, 3600, NOW, 1000.0)
    assert hour.valid == 60


def test_window_is_half_open_so_no_probe_is_counted_twice(storage: Storage):
    """(now - window, now]: the newest edge is in, the oldest edge is out."""
    storage.record_probe(ProbeResult(target="svc", ts=NOW, ok=True, status=200, latency_ms=10.0))
    storage.record_probe(ProbeResult(target="svc", ts=NOW - 300, ok=True, status=200, latency_ms=10.0))
    stat = storage.window_stat("svc", SliKind.AVAILABILITY, 300, NOW, 1000.0)
    assert stat.valid == 1


def test_availability_sli_counts_failures(storage: Storage):
    fill(storage, "svc", count=60, bad=6, now=NOW)
    stat = storage.window_stat("svc", SliKind.AVAILABILITY, 3600, NOW, 1000.0)
    assert (stat.valid, stat.good) == (60, 54)
    assert stat.bad_ratio == 0.1


def test_latency_sli_counts_slow_successes_as_bad(storage: Storage):
    """Slow but successful responses burn the latency budget, not the availability one."""
    fill(storage, "svc", count=60, slow=12, slow_latency_ms=5000.0, now=NOW)
    avail = storage.window_stat("svc", SliKind.AVAILABILITY, 3600, NOW, 1000.0)
    latency = storage.window_stat("svc", SliKind.LATENCY, 3600, NOW, 1000.0)
    assert avail.bad == 0
    assert latency.bad == 12


def test_a_failed_probe_is_bad_for_latency_too(storage: Storage):
    """It cannot be under the threshold if it never answered."""
    fill(storage, "svc", count=10, bad=10, bad_latency_ms=1.0, now=NOW)
    latency = storage.window_stat("svc", SliKind.LATENCY, 3600, NOW, 1000.0)
    assert latency.good == 0


def test_windows_are_scoped_to_one_target(storage: Storage):
    fill(storage, "a", count=10, now=NOW)
    fill(storage, "b", count=10, bad=10, now=NOW)
    assert storage.window_stat("a", SliKind.AVAILABILITY, 3600, NOW, 1000.0).bad == 0
    assert storage.window_stat("b", SliKind.AVAILABILITY, 3600, NOW, 1000.0).bad == 10


def test_latency_samples_exclude_failures_and_come_back_sorted(storage: Storage):
    fill(storage, "svc", count=10, bad=3, bad_latency_ms=9999.0, now=NOW)
    samples = storage.latency_samples("svc", NOW - 3600, NOW)
    assert len(samples) == 7
    assert samples == sorted(samples)
    assert 9999.0 not in samples


def test_alert_lifecycle_and_deduplication(storage: Storage):
    assert storage.open_alert_id("svc", SliKind.AVAILABILITY, Severity.PAGE) is None
    alert_id = storage.open_alert("svc", SliKind.AVAILABILITY, Severity.PAGE, "r", NOW, 20.0, 18.0)
    assert storage.open_alert_id("svc", SliKind.AVAILABILITY, Severity.PAGE) == alert_id
    # A different severity for the same target is a separate alert.
    assert storage.open_alert_id("svc", SliKind.AVAILABILITY, Severity.TICKET) is None
    storage.close_alert(alert_id, NOW + 600)
    assert storage.open_alert_id("svc", SliKind.AVAILABILITY, Severity.PAGE) is None


def test_peak_burn_only_ever_rises(storage: Storage):
    alert_id = storage.open_alert("svc", SliKind.AVAILABILITY, Severity.PAGE, "r", NOW, 20.0, 18.0)
    storage.bump_peak(alert_id, 50.0, 40.0)
    storage.bump_peak(alert_id, 5.0, 4.0)
    row = storage.alerts()[0]
    assert row["peak_long_burn"] == 50.0
    assert row["peak_short_burn"] == 40.0


def test_closing_an_already_closed_alert_keeps_the_first_time(storage: Storage):
    alert_id = storage.open_alert("svc", SliKind.AVAILABILITY, Severity.PAGE, "r", NOW, 20.0, 18.0)
    storage.close_alert(alert_id, NOW + 60)
    storage.close_alert(alert_id, NOW + 600)
    assert storage.alerts()[0]["closed_ts"] == NOW + 60


def test_prune_removes_only_old_probes(storage: Storage):
    """Prune is strictly older-than, so the probe sitting on the cutoff survives.

    100 hourly probes ending at NOW means the oldest is NOW-99h. Cutting at
    NOW-10h drops NOW-99h..NOW-11h, which is 89 rows, and keeps the 11 from
    NOW-10h onwards.
    """
    fill(storage, "svc", count=100, now=NOW, interval=3600.0)
    removed = storage.prune(NOW - 10 * 3600)
    assert removed == 89
    assert storage.probe_count() == 11


def test_error_kind_survives_a_round_trip(storage: Storage):
    storage.record_probe(
        ProbeResult(target="svc", ts=NOW, ok=False, status=None, latency_ms=5.0, error_kind=ErrorKind.DNS)
    )
    assert storage.probes_between("svc", NOW - 1, NOW + 1)[0].error_kind is ErrorKind.DNS
