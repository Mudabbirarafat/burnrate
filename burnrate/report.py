"""Human-readable output: SLI tables and incident timelines.

Every number here is printed with the sample count behind it. A 100% figure
computed from four probes is not the same claim as one computed from four
thousand, and a report that hides the difference is lying politely.
"""

from __future__ import annotations

import datetime as dt

from .burn import budget_remaining, burn_rate
from .config import Config
from .models import SliKind
from .storage import Storage

HOUR = 3600
DAY = 86400


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. `sorted_values` must already be sorted."""
    if not sorted_values:
        return None
    if not 0.0 < q <= 1.0:
        raise ValueError("q must be in (0, 1]")
    rank = max(1, min(len(sorted_values), int(round(q * len(sorted_values)))))
    return sorted_values[rank - 1]


def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f} ms"


def _fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def sli_table(config: Config, storage: Storage, now: float, window: int = DAY) -> str:
    lines = [
        f"## SLIs over the last {_fmt_duration(window)}",
        "",
        "| target | availability | budget left | latency SLI | p50 | p95 | p99 | probes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in config.targets:
        avail = storage.window_stat(t.name, SliKind.AVAILABILITY, window, now, t.slo.latency_ms)
        lat = storage.window_stat(t.name, SliKind.LATENCY, window, now, t.slo.latency_ms)
        samples = storage.latency_samples(t.name, now - window, now)
        budget = t.slo.error_budget_for(SliKind.AVAILABILITY)
        left = budget_remaining(avail, budget) * 100 if avail.valid else float("nan")
        avail_pct = (avail.good / avail.valid * 100) if avail.valid else float("nan")
        lat_pct = (lat.good / lat.valid * 100) if lat.valid else float("nan")
        lines.append(
            f"| {t.name} "
            f"| {avail_pct:.2f}% (SLO {t.slo.availability * 100:.1f}%) "
            f"| {left:.0f}% "
            f"| {lat_pct:.2f}% under {t.slo.latency_ms:.0f} ms "
            f"| {_fmt_ms(percentile(samples, 0.50))} "
            f"| {_fmt_ms(percentile(samples, 0.95))} "
            f"| {_fmt_ms(percentile(samples, 0.99))} "
            f"| {avail.valid} |"
        )
    return "\n".join(lines)


def burn_table(config: Config, storage: Storage, now: float) -> str:
    lines = [
        "## Current burn rates",
        "",
        "| target | SLI | rule | long | short | firing | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in config.targets:
        for sli in (SliKind.AVAILABILITY, SliKind.LATENCY):
            budget = t.slo.error_budget_for(sli)
            for rule in config.rules:
                long_stat = storage.window_stat(t.name, sli, rule.long_window_seconds, now, t.slo.latency_ms)
                short_stat = storage.window_stat(t.name, sli, rule.short_window_seconds, now, t.slo.latency_ms)
                lb = burn_rate(long_stat, budget)
                sb = burn_rate(short_stat, budget)
                thin = long_stat.valid < config.min_samples or short_stat.valid < config.min_samples
                firing = (not thin) and lb >= rule.threshold and sb >= rule.threshold
                note = "too few probes" if thin else ""
                lines.append(
                    f"| {t.name} | {sli.value} | {rule.label} (>={rule.threshold}x) "
                    f"| {lb:.1f}x ({long_stat.valid}) | {sb:.1f}x ({short_stat.valid}) "
                    f"| {'YES' if firing else 'no'} | {note} |"
                )
    return "\n".join(lines)


def incident_timeline(storage: Storage, now: float, since: float | None = None) -> str:
    rows = storage.alerts(include_closed=True, since=since)
    if not rows:
        return "## Incidents\n\nNone recorded."
    lines = [
        "## Incidents",
        "",
        "| opened | target | SLI | severity | rule | peak burn | duration | state |",
        "|---|---|---|---|---|---|---|---|",
    ]
    durations: list[float] = []
    for r in rows:
        closed = r["closed_ts"]
        duration = (closed if closed is not None else now) - r["opened_ts"]
        if closed is not None:
            durations.append(duration)
        lines.append(
            f"| {_fmt_ts(r['opened_ts'])} | {r['target']} | {r['kind']} | {r['severity']} "
            f"| {r['rule_label']} | {r['peak_long_burn']:.1f}x | {_fmt_duration(duration)} "
            f"| {'resolved' if closed is not None else 'OPEN'} |"
        )
    if durations:
        mttr = sum(durations) / len(durations)
        lines += [
            "",
            f"Resolved incidents: {len(durations)}. "
            f"Mean time to resolution: {_fmt_duration(mttr)}. "
            f"Longest: {_fmt_duration(max(durations))}.",
            "",
            "MTTR here is time-to-alert-resolution, not time-to-human-fix: this "
            "project watches services, it does not repair them.",
        ]
    return "\n".join(lines)


def full_report(config: Config, storage: Storage, now: float) -> str:
    total = storage.probe_count()
    first = storage.first_probe_ts()
    header = [
        "# burnrate report",
        "",
        f"Generated {_fmt_ts(now)} from {total} probes"
        + (f", collected since {_fmt_ts(first)}." if first else "."),
    ]
    if first is not None and now - first < 3 * DAY:
        header += [
            "",
            f"> Only {_fmt_duration(now - first)} of history so far. The 1d and 3d "
            "burn-rate rules cannot fire meaningfully yet, and the 30-day budget "
            "figures are extrapolations from a short sample.",
        ]
    parts = [
        "\n".join(header),
        sli_table(config, storage, now, HOUR),
        sli_table(config, storage, now, DAY),
        burn_table(config, storage, now),
        incident_timeline(storage, now),
    ]
    return "\n\n".join(parts) + "\n"
