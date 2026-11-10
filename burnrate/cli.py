"""Command line entry point.

    burnrate once      probe every target once, evaluate, exit
    burnrate run       probe on a schedule until interrupted, or --for a while
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


def _cmd_run(config: Config, serve_metrics: bool, duration: float | None = None) -> int:
    metrics = Metrics() if serve_metrics else None
    with Storage(config.database) as storage:
        engine = AlertEngine(config, storage)
        if metrics is not None:
            metrics.serve(config.metrics_port)
            print(f"metrics on http://localhost:{config.metrics_port}/metrics")
        until = "ctrl-c to stop" if duration is None else f"for {duration:g}s"
        print(
            f"probing {len(config.targets)} targets every {config.probe.interval_seconds}s "
            f"into {config.database}; {until}"
        )
        # monotonic, not wall clock: a clock step mid-run should not cut the
        # window short or extend it.
        deadline = None if duration is None else time.monotonic() + duration
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
                # Checked after the tick, so --for 0 still probes once: a
                # scheduled job that returned without probing would be a
                # silent gap in the history rather than an error.
                if deadline is not None and time.monotonic() >= deadline:
                    break
                wait = config.probe.interval_seconds - (time.monotonic() - started)
                if deadline is not None:
                    wait = min(wait, deadline - time.monotonic())
                time.sleep(max(0.0, wait))
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


def _positive_seconds(value: str) -> float:
    """A duration argparse will reject rather than pass on as nonsense.

    Zero is allowed and means one probe: `run --for 0` and `once` differ only
    in that `run` keeps the metrics server and the prune schedule.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of seconds") from None
    if seconds < 0:
        raise argparse.ArgumentTypeError("a duration cannot be negative")
    return seconds


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
    run.add_argument(
        "--for",
        dest="duration",
        type=_positive_seconds,
        metavar="SECONDS",
        help="stop after this long instead of running until interrupted",
    )

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
        return _cmd_run(config, serve_metrics=not args.no_metrics, duration=args.duration)
    return _cmd_report(config, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
