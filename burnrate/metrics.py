"""Prometheus exposition.

Everything the alerting decides is also published as a metric, so the same
numbers can be scraped by Prometheus, pushed to Grafana Cloud, or read by a
human with curl. If the dashboard and the alert ever disagree, it is a bug.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Gauge, start_http_server

from .alerts import AlertEngine
from .config import Config
from .models import ProbeResult, Severity, SliKind
from .storage import Storage


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.up = Gauge(
            "burnrate_target_up",
            "1 if the most recent probe of this target succeeded",
            ["target"],
            registry=self.registry,
        )
        self.latency = Gauge(
            "burnrate_probe_latency_ms",
            "Latency of the most recent probe in milliseconds",
            ["target"],
            registry=self.registry,
        )
        self.sli = Gauge(
            "burnrate_sli_ratio",
            "Good events divided by valid events over a window",
            ["target", "sli", "window"],
            registry=self.registry,
        )
        self.samples = Gauge(
            "burnrate_window_samples",
            "Number of probes in a window, the denominator behind the SLI",
            ["target", "sli", "window"],
            registry=self.registry,
        )
        self.burn = Gauge(
            "burnrate_burn_rate",
            "Error budget burn rate over a rule's long window",
            ["target", "sli", "rule"],
            registry=self.registry,
        )
        self.alert_open = Gauge(
            "burnrate_alert_open",
            "1 while an alert of this severity is open",
            ["target", "sli", "severity"],
            registry=self.registry,
        )

    def serve(self, port: int) -> None:
        start_http_server(port, registry=self.registry)

    def observe_probe(self, result: ProbeResult) -> None:
        self.up.labels(target=result.target).set(1 if result.ok else 0)
        if result.latency_ms is not None:
            self.latency.labels(target=result.target).set(result.latency_ms)

    def observe_evaluation(
        self, config: Config, storage: Storage, engine: AlertEngine, now: float
    ) -> None:
        for target in config.targets:
            for sli in (SliKind.AVAILABILITY, SliKind.LATENCY):
                for window, label in ((3600, "1h"), (86400, "24h")):
                    stat = storage.window_stat(target.name, sli, window, now, target.slo.latency_ms)
                    ratio = (stat.good / stat.valid) if stat.valid else float("nan")
                    self.sli.labels(target=target.name, sli=sli.value, window=label).set(ratio)
                    self.samples.labels(target=target.name, sli=sli.value, window=label).set(stat.valid)

                for ev in engine.last_evaluations.get((target.name, sli), []):
                    self.burn.labels(
                        target=target.name, sli=sli.value, rule=ev.rule.label
                    ).set(ev.long_burn)

                for severity in (Severity.PAGE, Severity.TICKET):
                    is_open = storage.open_alert_id(target.name, sli, severity) is not None
                    self.alert_open.labels(
                        target=target.name, sli=sli.value, severity=severity.value
                    ).set(1 if is_open else 0)
