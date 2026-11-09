"""The alert state machine.

Rules decide whether something is burning. This decides whether that is news.
Opening an alert every evaluation cycle would be a firehose; closing one the
instant a single window dips would flap. Both are failure modes of real
monitoring systems and both are handled here rather than in the rule maths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .burn import evaluate_rule
from .config import Config
from .models import RuleEvaluation, Severity, SliKind
from .storage import Storage

_StateKey = tuple[str, SliKind, Severity]


class TransitionKind(str, Enum):
    OPENED = "opened"
    CLOSED = "closed"


@dataclass(frozen=True)
class Transition:
    """A change worth telling a human about."""

    kind: TransitionKind
    target: str
    sli: SliKind
    severity: Severity
    rule_label: str
    ts: float
    long_burn: float
    short_burn: float
    alert_id: int

    def summary(self) -> str:
        verb = "FIRING" if self.kind is TransitionKind.OPENED else "RESOLVED"
        return (
            f"{verb} [{self.severity.value}] {self.target} {self.sli.value} "
            f"({self.rule_label}, burn {self.long_burn:.1f}x long / {self.short_burn:.1f}x short)"
        )


class AlertEngine:
    """Evaluates every rule for every target on each tick.

    The consecutive-clean counter lives in memory, so a restart costs an open
    alert `resolve_after` extra evaluations before it can close. That is a
    deliberate trade: persisting it would make a crash during an incident able
    to resolve the incident, which is the worse of the two failures.
    """

    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self._clean: dict[_StateKey, int] = {}
        self.last_evaluations: dict[tuple[str, SliKind], list[RuleEvaluation]] = {}

    def evaluate(self, now: float) -> list[Transition]:
        transitions: list[Transition] = []
        for target in self.config.targets:
            for sli in (SliKind.AVAILABILITY, SliKind.LATENCY):
                evaluations = self._evaluate_target(target.name, sli, now)
                self.last_evaluations[(target.name, sli)] = evaluations
                for severity in (Severity.PAGE, Severity.TICKET):
                    t = self._apply(target.name, sli, severity, evaluations, now)
                    if t is not None:
                        transitions.append(t)
        return transitions

    def _evaluate_target(self, name: str, sli: SliKind, now: float) -> list[RuleEvaluation]:
        target = self.config.target_by_name(name)
        budget = target.slo.error_budget_for(sli)
        threshold_ms = target.slo.latency_ms
        out: list[RuleEvaluation] = []
        for rule in self.config.rules:
            long_stat = self.storage.window_stat(name, sli, rule.long_window_seconds, now, threshold_ms)
            short_stat = self.storage.window_stat(name, sli, rule.short_window_seconds, now, threshold_ms)
            out.append(evaluate_rule(rule, long_stat, short_stat, budget, self.config.min_samples))
        return out

    def _apply(
        self,
        name: str,
        sli: SliKind,
        severity: Severity,
        evaluations: list[RuleEvaluation],
        now: float,
    ) -> Transition | None:
        key: _StateKey = (name, sli, severity)
        firing = [e for e in evaluations if e.rule.severity is severity and e.firing]
        open_id = self.storage.open_alert_id(name, sli, severity)

        if firing:
            self._clean[key] = 0
            worst = max(firing, key=lambda e: e.long_burn)
            if open_id is None:
                alert_id = self.storage.open_alert(
                    name, sli, severity, worst.rule.label, now, worst.long_burn, worst.short_burn
                )
                return Transition(
                    TransitionKind.OPENED, name, sli, severity, worst.rule.label,
                    now, worst.long_burn, worst.short_burn, alert_id,
                )
            self.storage.bump_peak(open_id, worst.long_burn, worst.short_burn)
            return None

        if open_id is None:
            self._clean.pop(key, None)
            return None

        clean = self._clean.get(key, 0) + 1
        self._clean[key] = clean
        if clean < self.config.resolve_after:
            return None

        self.storage.close_alert(open_id, now)
        self._clean[key] = 0
        quietest = max(
            (e for e in evaluations if e.rule.severity is severity),
            key=lambda e: e.long_burn,
            default=None,
        )
        return Transition(
            TransitionKind.CLOSED, name, sli, severity,
            quietest.rule.label if quietest else "",
            now,
            quietest.long_burn if quietest else 0.0,
            quietest.short_burn if quietest else 0.0,
            open_id,
        )
