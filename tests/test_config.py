from __future__ import annotations

from pathlib import Path

import pytest

from burnrate.config import DEFAULT_RULES, ProbeSettings, load_config
from burnrate.models import Severity

GOOD = """
probe:
  interval_seconds: 30
targets:
  - name: a
    url: https://a.test/
    slo:
      availability: 0.995
      latency_ms: 800
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "targets.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_targets_and_applies_defaults(tmp_path: Path):
    config = load_config(write(tmp_path, GOOD))
    assert len(config.targets) == 1
    t = config.targets[0]
    assert t.name == "a"
    assert t.slo.availability == 0.995
    assert t.slo.latency_objective == 0.95  # default
    assert config.probe.interval_seconds == 30
    assert config.rules == DEFAULT_RULES


def test_missing_file_is_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_a_config_with_no_targets_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="no targets"):
        load_config(write(tmp_path, "probe:\n  interval_seconds: 60\n"))


def test_duplicate_target_names_are_rejected(tmp_path: Path):
    text = "targets:\n  - name: a\n    url: https://a.test/\n  - name: a\n    url: https://b.test/\n"
    with pytest.raises(ValueError, match="duplicate"):
        load_config(write(tmp_path, text))


def test_an_objective_of_one_is_rejected(tmp_path: Path):
    """A 100% SLO has a zero error budget, so every burn rate is infinite."""
    text = "targets:\n  - name: a\n    url: https://a.test/\n    slo:\n      availability: 1.0\n"
    with pytest.raises(ValueError, match="between 0 and 1"):
        load_config(write(tmp_path, text))


def test_unknown_slo_keys_are_rejected_rather_than_ignored(tmp_path: Path):
    """A typo in an SLO must not silently leave the default in place."""
    text = "targets:\n  - name: a\n    url: https://a.test/\n    slo:\n      availabilty: 0.99\n"
    with pytest.raises(ValueError, match="unknown slo keys"):
        load_config(write(tmp_path, text))


def test_a_bad_url_is_caught_at_load_time(tmp_path: Path):
    with pytest.raises(ValueError, match="http"):
        load_config(write(tmp_path, "targets:\n  - name: a\n    url: a.test\n"))


def test_an_impolite_probe_interval_is_rejected():
    with pytest.raises(ValueError, match="interval"):
        ProbeSettings(interval_seconds=1)


def test_default_rules_cover_both_severities():
    severities = {r.severity for r in DEFAULT_RULES}
    assert severities == {Severity.PAGE, Severity.TICKET}
    for rule in DEFAULT_RULES:
        assert rule.short_window_seconds < rule.long_window_seconds
