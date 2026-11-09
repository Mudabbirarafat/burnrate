"""The blackbox probe itself.

Deliberately dumb: one request, one verdict, one row. Everything clever about
this project happens later, over the rows this produces.
"""

from __future__ import annotations

import asyncio
import ssl
import time

import httpx

from .config import ProbeSettings
from .models import ErrorKind, ProbeResult, Target


def classify(exc: Exception) -> ErrorKind:
    """Map a transport failure onto an error class.

    httpx reports DNS and refused connections through the same ConnectError,
    so the underlying message is the only thing that separates "the name does
    not resolve" from "the port is shut". That is fragile by nature, which is
    why UNKNOWN exists rather than a wrong guess.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ErrorKind.TIMEOUT
    if isinstance(exc, httpx.TooManyRedirects):
        # A redirect loop is a real and common way for a site to be down while
        # every individual hop returns a perfectly healthy 301.
        return ErrorKind.REDIRECT_LOOP
    if isinstance(exc, ssl.SSLError):
        return ErrorKind.TLS
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        if "ssl" in text or "certificate" in text or "tls" in text:
            return ErrorKind.TLS
        dns_markers = (
            "getaddrinfo",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "no address associated",
            "11001",  # WSAHOST_NOT_FOUND
        )
        if any(m in text for m in dns_markers):
            return ErrorKind.DNS
        return ErrorKind.CONNECT
    if isinstance(exc, httpx.TransportError):
        return ErrorKind.CONNECT
    return ErrorKind.UNKNOWN


async def probe_once(
    client: httpx.AsyncClient,
    target: Target,
    settings: ProbeSettings,
    now: float | None = None,
) -> ProbeResult:
    """Probe one target exactly once. Never raises."""
    ts = time.time() if now is None else now
    started = time.perf_counter()
    try:
        response = await client.request(
            target.method,
            target.url,
            timeout=settings.timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=settings.follow_redirects,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 500:
            kind = ErrorKind.HTTP_5XX
        elif response.status_code >= 400:
            kind = ErrorKind.HTTP_4XX
        else:
            kind = ErrorKind.NONE
        return ProbeResult(
            target=target.name,
            ts=ts,
            ok=response.status_code < 400,
            status=response.status_code,
            latency_ms=elapsed_ms,
            error_kind=kind,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never take the loop down
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return ProbeResult(
            target=target.name,
            ts=ts,
            ok=False,
            status=None,
            # Latency of a failed probe is recorded for diagnosis but is never
            # a good event: see ProbeResult.is_fast.
            latency_ms=elapsed_ms,
            error_kind=classify(exc),
        )


async def probe_all(targets: tuple[Target, ...], settings: ProbeSettings) -> list[ProbeResult]:
    """Probe every target concurrently and return results in target order."""
    limits = httpx.Limits(max_connections=max(4, len(targets)), max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [probe_once(client, t, settings) for t in targets]
        return list(await asyncio.gather(*tasks))
