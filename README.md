# burnrate

Blackbox SLO monitoring for services I actually run, with the multi-window
multi-burn-rate error-budget alerting from the Google SRE Workbook.

Probing a URL and shouting when it returns a 500 is a one-afternoon script.
The hard part of monitoring is the opposite problem: deciding when a signal
deserves a human. This project is about that decision, and about being honest
regarding how much a 60-second prober is entitled to conclude.

## What it watches

Six services I built and operate: two client sites in production, three
deployed side projects, and my portfolio. Every target has its own objectives,
because a free-tier container and a CDN-hosted static site do not deserve the
same ones.

| target | what it is | availability SLO | latency SLO |
|---|---|---|---|
| `icsf` | awards-event site, live for a client | 99.5% | 95% under 2000 ms |
| `radiant` | institute site, live for a client | 99.5% | 95% under 2000 ms |
| `scholarly` | semantic search over arXiv abstracts | 99% | 95% under 1500 ms |
| `depwatch` | dependency-advisory dashboard | 99% | 90% under 3000 ms |
| `cyberrun` | browser game | 99% | 95% under 1500 ms |
| `portfolio` | personal site | 99% | 95% under 1500 ms |

## What it found in its first three probes

```
scholarly                ok                    725ms  http 200
depwatch                 FAIL (timeout)      10080ms  http -
cyberrun                 ok                    142ms  http 200
portfolio                ok                   1498ms  http 200
icsf                     ok                    156ms  http 200
radiant                  ok                    367ms  http 200
```

`depwatch` runs on Render's free tier, which stops the container when it is
idle. The first two probes timed out at the 10-second limit; the third, once
the container was awake, answered in 354 ms. That is a 28x difference between
a cold and a warm request to the same URL, and it is the single largest
reliability fact about that service — invisible to anyone who only ever
clicks the link twice in a row.

The alert did not fire on the first failed probe, or the second. It fired on
the third, once the window held enough samples to be worth believing. That
restraint is the feature.

## How the alerting works

**The SLIs are ratios, not averages.** Availability is successful probes over
valid probes. Latency is probes faster than a threshold over valid probes —
not mean latency, which hides exactly the tail an SLO exists to govern. A
failed probe is never counted as fast: a connection refused comes back in
3 ms, and calling that a quick page is how a latency dashboard stays green
through an outage.

**Burn rate** is how fast error budget is being spent, relative to spending
exactly all of it over the SLO period:

```
burn_rate = observed_bad_ratio / error_budget
```

A 99% availability SLO leaves a 1% budget. Ten percent of probes failing is a
burn rate of 10: at that pace a month's budget is gone in three days. The SLO
period does not appear in the formula, which is why the same thresholds work
regardless of whether the period is 30 days or 7.

**Every rule uses two windows,** a long one and a short one, and fires only
when both exceed the threshold:

| severity | long window | short window | threshold | burns |
|---|---|---|---|---|
| page | 1 h | 5 m | 14.4x | 2% of a 30-day budget in an hour |
| page | 6 h | 30 m | 6x | 5% in six hours |
| ticket | 1 d | 2 h | 3x | 10% in a day |
| ticket | 3 d | 6 h | 1x | 10% in three days |

The long window establishes that the burn is real. The short window
establishes that it is still happening, so an alert resolves shortly after
recovery instead of smouldering for the rest of the long window. A page means
wake someone; a ticket means look at it on Monday.

A consequence worth stating plainly: after an hour-long outage the page
resolves once traffic is healthy, and the 1-day ticket stays open, because the
day's budget really is spent. Recovery is not absolution. There is a test
named after that.

## Quantisation, and what this prober cannot tell you

Burn-rate alerting was designed for high-volume request streams. A 60-second
blackbox prober is not one. At that interval a 5-minute window holds about
five samples, so a single failed probe is a 20% bad ratio and a burn rate of
20 against a 99% SLO — enough to page on one blip.

The guard is a minimum sample count: a window holding fewer than `min_samples`
probes is suppressed rather than trusted, and the report prints the
denominator next to every ratio so a 100% figure computed from four probes
cannot be mistaken for one computed from four thousand.

This does not make the problem disappear, it makes it visible. Three samples
is still a thin basis for a page, and in the run above the alert fired on
exactly that. The real fixes are a shorter interval, several probe locations,
or a service that reports its own request metrics. All three are out of scope
for something that must run on a laptop with no RAM to spare.

Other things it deliberately does not do: it does not repair anything, so the
MTTR it reports is time-to-alert-resolution, not time-to-fix. It probes from
one location, so it cannot separate "the site is down" from "my network is
down". It is a blackbox: it sees what a visitor sees, and nothing else.

## Running it

```bash
pip install -e .
python -m burnrate.cli run                 # probe on a schedule, serve /metrics
python -m burnrate.cli once                # a single cycle, for cron or CI
python -m burnrate.cli report -o report.md # SLIs, burn rates, incident timeline
```

Metrics are exposed on `:9105/metrics` in the Prometheus text format:
`burnrate_sli_ratio`, `burnrate_burn_rate`, `burnrate_window_samples`,
`burnrate_alert_open`, `burnrate_target_up`, `burnrate_probe_latency_ms`.
Every number the alerting decides on is also published, so if the dashboard
and the alert ever disagree, that is a bug.

Footprint is one Python process and one SQLite file. There is no cluster, no
message broker and no time-series database, because the machine this was
written on cannot host any of them.

## Tests

```bash
pytest -q     # 77 tests, 0.3s, no network and no sleeping
```

Time is a parameter everywhere it matters, and the probe layer is exercised
through a mock transport, so the alerting state machine is tested by handing
it a history and a timestamp rather than by waiting for one. Failure
classification is table-driven: DNS, connect, timeout, TLS, redirect loop,
4xx, 5xx. The redirect-loop class exists because a site can be entirely
unreachable while every individual hop returns a healthy 301.

## Layout

```
burnrate/
  models.py     value types; no I/O
  config.py     YAML loading, validation, the default rule table
  prober.py     one request, one verdict, one row
  storage.py    SQLite, WAL, windowed good/valid counts
  burn.py       burn-rate arithmetic and rule evaluation
  alerts.py     open/close state machine, deduplication, flap suppression
  report.py     SLI tables, latency percentiles, incident timeline
  metrics.py    Prometheus exposition
  cli.py        once / run / report
```

## License

MIT.
