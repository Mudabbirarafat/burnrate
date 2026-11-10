"""The bits of the command line that a scheduled job depends on.

`run --for` is what lets the same loop serve a long-lived process and a CI
job that is only allowed to exist for a few minutes, so the two properties
that matter are that it always probes at least once and that it never sleeps
past its own deadline. A job that returned without probing would leave a gap
in the history and still exit 0, which is the worst kind of monitoring bug.
"""

from __future__ import annotations

import argparse
import dataclasses
import time

import pytest

from burnrate import cli
from burnrate.config import Config, ProbeSettings


def test_for_accepts_a_duration() -> None:
    assert cli._positive_seconds("300") == 300.0
    assert cli._positive_seconds("0") == 0.0
    assert cli._positive_seconds("1.5") == 1.5


@pytest.mark.parametrize("bad", ["-1", "-0.5", "forever", ""])
def test_for_rejects_what_is_not_a_duration(bad: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._positive_seconds(bad)


@pytest.fixture
def quick(config: Config) -> Config:
    """The real config with a probe interval long enough to prove it is capped."""
    return dataclasses.replace(
        config,
        probe=ProbeSettings(interval_seconds=3600, timeout_seconds=5.0),
    )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count probe rounds without making any."""
    rounds: list[int] = []

    async def fake_probe_all(targets, probe):  # noqa: ANN001, ANN202
        rounds.append(len(targets))
        return []

    monkeypatch.setattr(cli, "probe_all", fake_probe_all)
    return rounds


def test_for_zero_still_probes_once(quick: Config, no_network: list[int]) -> None:
    assert cli._cmd_run(quick, serve_metrics=False, duration=0) == 0
    assert len(no_network) == 1


def test_never_sleeps_past_its_deadline(
    quick: Config, no_network: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    real_sleep = time.sleep

    def recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        real_sleep(min(seconds, 0.05))

    monkeypatch.setattr(cli.time, "sleep", recording_sleep)

    started = time.monotonic()
    assert cli._cmd_run(quick, serve_metrics=False, duration=0.2) == 0
    elapsed = time.monotonic() - started

    # The interval is an hour. Without the cap the first sleep would be 3600s.
    # The tolerance is for the float arithmetic in `deadline - monotonic()`,
    # which lands a few nanoseconds either side of the budget.
    assert all(s <= 0.2 + 1e-6 for s in slept), slept
    # Each sleep is capped to what is left, so the budget only ever shrinks.
    assert slept == sorted(slept, reverse=True), slept
    assert elapsed < 5.0
    assert len(no_network) >= 1
