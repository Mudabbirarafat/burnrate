"""SQLite persistence for probes and alerts.

One file, WAL mode, an explicit schema version. A monitoring tool that loses
its own history during an incident is worse than no monitoring tool, so the
write path is deliberately boring.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import ErrorKind, ProbeResult, Severity, SliKind, WindowStat

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS probes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target     TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    ok         INTEGER NOT NULL,
    status     INTEGER,
    latency_ms REAL,
    error_kind TEXT    NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_probes_target_ts ON probes(target, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    target         TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    severity       TEXT    NOT NULL,
    rule_label     TEXT    NOT NULL,
    opened_ts      REAL    NOT NULL,
    closed_ts      REAL,
    peak_long_burn REAL    NOT NULL,
    peak_short_burn REAL   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(target, kind, severity, closed_ts);
CREATE INDEX IF NOT EXISTS idx_alerts_opened ON alerts(opened_ts);
"""


class Storage:
    """Thread-safe enough: one connection, one lock.

    The probe loop writes from the event loop thread and the metrics endpoint
    reads from the HTTP server thread, which is exactly the case a shared
    sqlite3 connection needs protecting from.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        is_memory = self.path == ":memory:"
        if not is_memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if not is_memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path}: schema version {row['version']} but this build expects "
                    f"{SCHEMA_VERSION}; migrate or start a new database file"
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- probes

    def record_probe(self, result: ProbeResult) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO probes (target, ts, ok, status, latency_ms, error_kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    result.target,
                    result.ts,
                    1 if result.ok else 0,
                    result.status,
                    result.latency_ms,
                    result.error_kind.value,
                ),
            )
            self._conn.commit()

    def window_stat(
        self,
        target: str,
        kind: SliKind,
        window_seconds: int,
        now: float,
        latency_threshold_ms: float,
    ) -> WindowStat:
        """Good/valid counts for one SLI over the window ending at `now`.

        The window is half-open: (now - window, now]. Probes exactly at the
        boundary belong to the newer window, so a probe can never be counted
        twice by two adjacent evaluations.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS valid,
                       COALESCE(SUM(ok), 0) AS up,
                       COALESCE(SUM(CASE WHEN ok = 1 AND latency_ms IS NOT NULL
                                          AND latency_ms <= ? THEN 1 ELSE 0 END), 0) AS fast
                FROM probes
                WHERE target = ? AND ts > ? AND ts <= ?
                """,
                (latency_threshold_ms, target, now - window_seconds, now),
            ).fetchone()
        good = row["up"] if kind is SliKind.AVAILABILITY else row["fast"]
        return WindowStat(window_seconds=window_seconds, valid=row["valid"], good=good)

    def probes_between(self, target: str, start: float, end: float) -> list[ProbeResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM probes WHERE target = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (target, start, end),
            ).fetchall()
        return [
            ProbeResult(
                target=r["target"],
                ts=r["ts"],
                ok=bool(r["ok"]),
                status=r["status"],
                latency_ms=r["latency_ms"],
                error_kind=ErrorKind(r["error_kind"]),
            )
            for r in rows
        ]

    def latency_samples(self, target: str, start: float, end: float) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT latency_ms FROM probes"
                " WHERE target = ? AND ts > ? AND ts <= ? AND ok = 1 AND latency_ms IS NOT NULL"
                " ORDER BY latency_ms",
                (target, start, end),
            ).fetchall()
        return [r["latency_ms"] for r in rows]

    def probe_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS n FROM probes").fetchone()["n"]

    def first_probe_ts(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(ts) AS t FROM probes").fetchone()
        return row["t"]

    def prune(self, older_than_ts: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM probes WHERE ts < ?", (older_than_ts,))
            self._conn.commit()
            return cur.rowcount

    # ---------------------------------------------------------------- alerts

    def open_alert_id(self, target: str, kind: SliKind, severity: Severity) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM alerts"
                " WHERE target = ? AND kind = ? AND severity = ? AND closed_ts IS NULL"
                " ORDER BY opened_ts DESC LIMIT 1",
                (target, kind.value, severity.value),
            ).fetchone()
        return row["id"] if row else None

    def open_alert(
        self,
        target: str,
        kind: SliKind,
        severity: Severity,
        rule_label: str,
        opened_ts: float,
        long_burn: float,
        short_burn: float,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (target, kind, severity, rule_label, opened_ts,"
                " peak_long_burn, peak_short_burn) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target, kind.value, severity.value, rule_label, opened_ts, long_burn, short_burn),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def bump_peak(self, alert_id: int, long_burn: float, short_burn: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET peak_long_burn = MAX(peak_long_burn, ?),"
                " peak_short_burn = MAX(peak_short_burn, ?) WHERE id = ?",
                (long_burn, short_burn, alert_id),
            )
            self._conn.commit()

    def close_alert(self, alert_id: int, closed_ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET closed_ts = ? WHERE id = ? AND closed_ts IS NULL",
                (closed_ts, alert_id),
            )
            self._conn.commit()

    def alerts(self, include_closed: bool = True, since: float | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM alerts"
        clauses: list[str] = []
        params: list[object] = []
        if not include_closed:
            clauses.append("closed_ts IS NULL")
        if since is not None:
            clauses.append("opened_ts >= ?")
            params.append(since)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_ts"
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())
