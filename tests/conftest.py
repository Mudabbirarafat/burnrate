from __future__ import annotations

import pytest

from burnrate.config import Config, ProbeSettings
from burnrate.models import BurnRule, ErrorKind, ProbeResult, Severity, Slo, Target
from burnrate.storage import Storage

NOW = 1_800_000_000.0  # a fixed instant; no test may read the wall clock


@pytest.fixture
def storage() -> Storage:
    with Storage(":memory:") as s:
        yield s


@pytest.fixture
def target() -> Target:
    return Target(
        name="svc",
        url="https://example.test/",
        slo=Slo(availability=0.99, latency_ms=1000.0, latency_objective=0.95),
    )


@pytest.fixture
def config(target: Target) -> Config:
    return Config(
        probe=ProbeSettings(interval_seconds=60, timeout_seconds=5.0),
        targets=(target,),
        rules=(
            BurnRule(Severity.PAGE, 3600, 300, 14.4, "page-1h-5m"),
            BurnRule(Severity.TICKET, 86400, 7200, 3.0, "ticket-1d-2h"),
        ),
        database=":memory:",
        min_samples=3,
        resolve_after=2,
    )


def fill(
    storage: Storage,
    target: str,
    *,
    count: int,
    bad: int = 0,
    now: float = NOW,
    interval: float = 60.0,
    latency_ms: float = 100.0,
    bad_latency_ms: float | None = None,
    slow: int = 0,
    slow_latency_ms: float = 5000.0,
) -> None:
    """Write `count` probes ending at `now`, oldest first.

    `bad` of them fail outright and `slow` of them succeed slowly. Failures and
    slow responses are placed at the most recent end, which is where a real
    incident starts from the point of view of a window that ends now.
    """
    for i in range(count):
        ts = now - (count - 1 - i) * interval
        age_rank = count - 1 - i  # 0 is the newest
        is_bad = age_rank < bad
        is_slow = not is_bad and age_rank < bad + slow
        storage.record_probe(
            ProbeResult(
                target=target,
                ts=ts,
                ok=not is_bad,
                status=500 if is_bad else 200,
                latency_ms=(bad_latency_ms if is_bad else (slow_latency_ms if is_slow else latency_ms)),
                error_kind=ErrorKind.HTTP_5XX if is_bad else ErrorKind.NONE,
            )
        )
