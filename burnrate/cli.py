"""Command line entry point.

    burnrate once      probe every target once, evaluate, exit
    burnrate run       probe on a schedule until interrupted
    burnrate report    print the markdown report from stored history

`once` exists so the same code can run from cron or a scheduled CI job on a
machine that has no business keeping a daemon alive.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from .alerts import AlertEngine
from .config import Config, load_config
from .metrics import Metrics
from .prober import probe_all
from .report import full_report
from .storage import Storage


async def _tick(config: Config, storage: Storage, engine: AlertEngine, metrics: Metrics | None) -> int:
    """One probe-and-evaluate cycle. Returns the number of failed probes."""
    results = await probe_all(config.targets, config.probe)
    now = time.time()
    failed = 0
    for r in results:
        storage.record_probe(r)
        if metrics is not None:
            metrics.observe_probe(r)
        if not r.ok:
            failed += 1
        state = "ok" if r.ok else f"FAIL ({r.error_kind.value})"
        latency = f"{r.latency_ms:.0f}ms" if r.latency_ms is not None else "-"
        status = r.status if r.status is not None else "-"
        print(f"  {r.target:<24} {state:<18} {latency:>8}  http {status}")

    for t in engine.evaluate(now):
        print(f"  !! {t.summary()}")
    if metrics is not None:
        metrics.observe_evaluation(config, storage, engine, now)
    return failed


def _cmd_once(config: Config) -> int:
    with Storage(config.database) as storage:
        engine = AlertEngine(config, storage)
        failed = asyncio.run(_tick(config, storage, engine, None))
    return 1 if failed else 0


def _cmd_run(config: Config, serve_metrics: bool) -> int:
    metrics = Metrics() if serve_metrics else None
    with Storage(config.database) as storage:
        engine = AlertEngine(config, storage)
        if metrics is not None:
            metrics.serve(config.metrics_port)
            print(f"metrics on http://localhost:{config.metrics_port}/metrics")
        print(
            f"probing {len(config.targets)} targets every {config.probe.interval_seconds}s "
            f"into {config.database}; ctrl-c to stop"
        )
        ticks = 0
        try:
            while True:
                started = time.monotonic()
                print(f"[{time.strftime('%H:%M:%S')}]")
                asyncio.run(_tick(config, storage, engine, metrics))
                ticks += 1
                if ticks % 60 == 0 and config.retention_days > 0:
                    removed = storage.prune(time.time() - config.retention_days * 86400)
                    if removed:
                        print(f"  pruned {removed} probes older than {config.retention_days}d")
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, config.probe.interval_seconds - elapsed))
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def _cmd_report(config: Config, out: str | None) -> int:
    with Storage(config.database) as storage:
        text = full_report(config, storage, time.time())
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="burnrate", description=__doc__)
    parser.add_argument("-c", "--config", default="targets.yaml", help="config file (default: targets.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    # Accept --config on either side of the subcommand. SUPPRESS matters: with
    # a normal default the subparser would overwrite whatever was given before
    # the subcommand with its own default, silently ignoring the flag.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub.add_parser("once", parents=[common], help="probe every target once, evaluate, exit")

    run = sub.add_parser("run", parents=[common], help="probe on a schedule until interrupted")
    run.add_argument("--no-metrics", action="store_true", help="do not start the /metrics server")

    rep = sub.add_parser("report", parents=[common], help="print the markdown report")
    rep.add_argument("-o", "--out", help="write to this file instead of stdout")

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "once":
        return _cmd_once(config)
    if args.command == "run":
        return _cmd_run(config, serve_metrics=not args.no_metrics)
    return _cmd_report(config, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
