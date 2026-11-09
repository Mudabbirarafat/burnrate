"""Configuration loading and validation.

A bad config should fail at startup with a sentence you can act on, not at
three in the morning with a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import BurnRule, Severity, Slo, Target

# The multi-window multi-burn-rate table from the Google SRE Workbook,
# chapter "Alerting on SLOs". Each row says: if we keep burning the error
# budget this fast, we exhaust some meaningful share of it in some meaningful
# time, so react at this severity.
#
#   14.4x over 1h  -> 2%  of a 30-day budget gone in an hour
#    6x   over 6h  -> 5%  gone in six hours
#    3x   over 1d  -> 10% gone in a day
#    1x   over 3d  -> 10% gone in three days
DEFAULT_RULES: tuple[BurnRule, ...] = (
    BurnRule(Severity.PAGE, 3600, 300, 14.4, "page-1h-5m"),
    BurnRule(Severity.PAGE, 21600, 1800, 6.0, "page-6h-30m"),
    BurnRule(Severity.TICKET, 86400, 7200, 3.0, "ticket-1d-2h"),
    BurnRule(Severity.TICKET, 259200, 21600, 1.0, "ticket-3d-6h"),
)


@dataclass(frozen=True)
class ProbeSettings:
    interval_seconds: int = 60
    timeout_seconds: float = 10.0
    user_agent: str = "burnrate/0.1 (+https://github.com/Mudabbirarafat/burnrate)"
    follow_redirects: bool = True

    def __post_init__(self) -> None:
        if self.interval_seconds < 10:
            # Below this we are no longer a polite blackbox prober of someone's
            # production site; we are a load generator.
            raise ValueError("probe.interval_seconds must be at least 10")
        if self.timeout_seconds <= 0:
            raise ValueError("probe.timeout_seconds must be positive")


@dataclass(frozen=True)
class Config:
    probe: ProbeSettings
    targets: tuple[Target, ...]
    rules: tuple[BurnRule, ...] = DEFAULT_RULES
    database: str = "burnrate.db"
    metrics_port: int = 9105
    # A window is only trusted once it holds this many probes. With a 60s
    # interval a 5-minute window holds about 5 samples, so a single failed
    # probe is a 20% bad ratio -- a burn rate of 20 against a 99% SLO, enough
    # to page on one blip. Requiring a minimum sample count is what stops the
    # short window from turning noise into a page. See README, "Quantisation".
    min_samples: int = 3
    # Consecutive clean evaluations required before an open alert is closed.
    resolve_after: int = 2
    retention_days: int = 90
    _source: str = field(default="<memory>", compare=False)

    def target_by_name(self, name: str) -> Target:
        for t in self.targets:
            if t.name == name:
                return t
        raise KeyError(name)


def _slo_from(raw: dict[str, Any] | None, where: str) -> Slo:
    if not raw:
        return Slo()
    unknown = set(raw) - {"availability", "latency_ms", "latency_objective"}
    if unknown:
        raise ValueError(f"{where}: unknown slo keys {sorted(unknown)}")
    slo = Slo(
        availability=float(raw.get("availability", 0.99)),
        latency_ms=float(raw.get("latency_ms", 1500.0)),
        latency_objective=float(raw.get("latency_objective", 0.95)),
    )
    for name, value in (
        ("availability", slo.availability),
        ("latency_objective", slo.latency_objective),
    ):
        if not 0.0 < value < 1.0:
            raise ValueError(f"{where}: {name} must be between 0 and 1 exclusive, got {value}")
    if slo.latency_ms <= 0:
        raise ValueError(f"{where}: latency_ms must be positive")
    return slo


def load_config(path: str | Path) -> Config:
    """Read a YAML config file into a validated Config."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level of the config must be a mapping")

    probe = ProbeSettings(**(raw.get("probe") or {}))

    raw_targets = raw.get("targets") or []
    if not raw_targets:
        raise ValueError(f"{p}: no targets defined, there is nothing to probe")

    targets: list[Target] = []
    seen: set[str] = set()
    for i, rt in enumerate(raw_targets):
        where = f"{p}: targets[{i}]"
        if not isinstance(rt, dict):
            raise ValueError(f"{where}: each target must be a mapping")
        name = str(rt.get("name", "")).strip()
        if name in seen:
            raise ValueError(f"{where}: duplicate target name {name!r}")
        seen.add(name)
        targets.append(
            Target(
                name=name,
                url=str(rt.get("url", "")).strip(),
                method=str(rt.get("method", "GET")).upper(),
                slo=_slo_from(rt.get("slo"), where),
            )
        )

    return Config(
        probe=probe,
        targets=tuple(targets),
        database=str(raw.get("database", "burnrate.db")),
        metrics_port=int(raw.get("metrics_port", 9105)),
        min_samples=int(raw.get("min_samples", 3)),
        resolve_after=int(raw.get("resolve_after", 2)),
        retention_days=int(raw.get("retention_days", 90)),
        _source=str(p),
    )
