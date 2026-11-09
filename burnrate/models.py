"""Core value types.

Kept free of I/O on purpose: everything here is comparable, hashable where it
needs to be, and safe to construct in a test without a network or a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SliKind(str, Enum):
    """Which service level indicator a measurement or alert refers to."""

    AVAILABILITY = "availability"
    LATENCY = "latency"


class Severity(str, Enum):
    """Alert severity, in the Google SRE sense.

    PAGE means a human should be woken up; TICKET means it should be looked at
    during working hours. The distinction is the whole point of using several
    burn-rate windows rather than one threshold.
    """

    PAGE = "page"
    TICKET = "ticket"


class ErrorKind(str, Enum):
    """How a probe failed.

    Classifying failures matters more than counting them: a DNS failure and a
    500 are both "down" for the SLI, but they send you to different places.
    """

    NONE = "none"
    DNS = "dns"
    CONNECT = "connect"
    TIMEOUT = "timeout"
    TLS = "tls"
    REDIRECT_LOOP = "redirect_loop"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Slo:
    """The objective for one target.

    availability: the fraction of valid probes that must succeed.
    latency_ms:   the threshold that separates a fast probe from a slow one.
    latency_objective: the fraction of valid probes that must come in under it.

    Both indicators are good-events / valid-events ratios. Neither is an
    average: an average latency hides exactly the tail an SLO exists to govern.
    """

    availability: float = 0.99
    latency_ms: float = 1500.0
    latency_objective: float = 0.95

    def objective_for(self, kind: SliKind) -> float:
        return self.availability if kind is SliKind.AVAILABILITY else self.latency_objective

    def error_budget_for(self, kind: SliKind) -> float:
        """The share of valid events allowed to be bad. 99% SLO -> 1% budget."""
        return 1.0 - self.objective_for(kind)


@dataclass(frozen=True)
class Target:
    """One probed endpoint."""

    name: str
    url: str
    method: str = "GET"
    slo: Slo = Slo()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("target name must not be empty")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"{self.name}: url must be http(s), got {self.url!r}")


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of a single probe.

    `ok` is the availability SLI's good/bad decision, decided once at probe
    time so that replaying history can never silently reinterpret it.
    """

    target: str
    ts: float
    ok: bool
    status: int | None = None
    latency_ms: float | None = None
    error_kind: ErrorKind = ErrorKind.NONE

    def is_fast(self, threshold_ms: float) -> bool:
        """Good event for the latency SLI.

        A failed probe is never fast. Counting a connection refused as "0 ms,
        very quick" is the classic way to report a latency SLO that looks
        healthy during an outage.
        """
        if not self.ok or self.latency_ms is None:
            return False
        return self.latency_ms <= threshold_ms


@dataclass(frozen=True)
class WindowStat:
    """Good/valid counts over one time window, plus the derived ratio."""

    window_seconds: int
    valid: int
    good: int

    @property
    def bad(self) -> int:
        return self.valid - self.good

    @property
    def bad_ratio(self) -> float:
        """Fraction of valid events that were bad. Zero valid events -> 0.0.

        Returning 0.0 for an empty window is deliberate: "no data" must not
        read as "everything is broken". The sample-count guard in burn.py is
        what stops an empty or thin window from being trusted at all.
        """
        if self.valid == 0:
            return 0.0
        return self.bad / self.valid


@dataclass(frozen=True)
class BurnRule:
    """One row of the multi-window multi-burn-rate table.

    A rule fires only when the long window and the short window agree. The
    long window is what makes the signal trustworthy; the short window is what
    makes it current, so an alert resolves soon after the burn stops instead
    of smouldering for the rest of the long window.
    """

    severity: Severity
    long_window_seconds: int
    short_window_seconds: int
    threshold: float
    label: str

    def __post_init__(self) -> None:
        if self.short_window_seconds >= self.long_window_seconds:
            raise ValueError(f"{self.label}: short window must be shorter than long window")
        if self.threshold <= 0:
            raise ValueError(f"{self.label}: threshold must be positive")


@dataclass(frozen=True)
class RuleEvaluation:
    """What one rule saw at one moment. Carried into alerts for the record."""

    rule: BurnRule
    long_burn: float
    short_burn: float
    long_samples: int
    short_samples: int
    firing: bool
    suppressed_reason: str | None = None
