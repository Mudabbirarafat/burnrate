from __future__ import annotations

import ssl

import httpx
import pytest

from burnrate.config import ProbeSettings
from burnrate.models import ErrorKind, Target
from burnrate.prober import classify, probe_once

SETTINGS = ProbeSettings(interval_seconds=60, timeout_seconds=5.0)
TARGET = Target(name="svc", url="https://example.test/")


def client_returning(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_200_is_a_good_probe():
    async with client_returning(lambda r: httpx.Response(200)) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is True
    assert result.status == 200
    assert result.error_kind is ErrorKind.NONE
    assert result.latency_ms is not None and result.latency_ms >= 0


async def test_a_500_is_a_bad_probe():
    async with client_returning(lambda r: httpx.Response(503)) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is False
    assert result.error_kind is ErrorKind.HTTP_5XX


async def test_a_404_is_bad_for_a_homepage_probe():
    """We probe pages we own. If the homepage 404s, the service is broken."""
    async with client_returning(lambda r: httpx.Response(404)) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is False
    assert result.error_kind is ErrorKind.HTTP_4XX


async def test_a_redirect_is_followed_to_the_real_answer():
    """www -> apex redirects are normal; the probe should judge the destination."""

    def redirect_once(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "https://example.test/home"})
        return httpx.Response(200)

    async with client_returning(redirect_once) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is True
    assert result.status == 200


async def test_a_redirect_loop_is_an_outage_not_a_mystery():
    """Every hop returns a healthy 301 and the site is still unreachable."""

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://example.test/"})

    async with client_returning(loop) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is False
    assert result.error_kind is ErrorKind.REDIRECT_LOOP


async def test_a_timeout_is_recorded_not_raised():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with client_returning(boom) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is False
    assert result.status is None
    assert result.error_kind is ErrorKind.TIMEOUT


async def test_an_unexpected_exception_still_yields_a_row():
    """The loop must survive anything a transport can do to it."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("something nobody predicted")

    async with client_returning(boom) as client:
        result = await probe_once(client, TARGET, SETTINGS)
    assert result.ok is False
    assert result.error_kind is ErrorKind.UNKNOWN


async def test_probe_uses_the_configured_timestamp_when_given_one():
    async with client_returning(lambda r: httpx.Response(200)) as client:
        result = await probe_once(client, TARGET, SETTINGS, now=123.0)
    assert result.ts == 123.0


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ConnectTimeout("x"), ErrorKind.TIMEOUT),
        (httpx.ReadTimeout("x"), ErrorKind.TIMEOUT),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), ErrorKind.DNS),
        (httpx.ConnectError("Temporary failure in name resolution"), ErrorKind.DNS),
        (httpx.ConnectError("certificate verify failed"), ErrorKind.TLS),
        (httpx.ConnectError("Connection refused"), ErrorKind.CONNECT),
        (httpx.TooManyRedirects("looping"), ErrorKind.REDIRECT_LOOP),
        (ssl.SSLError("handshake failure"), ErrorKind.TLS),
        (ValueError("not a transport problem"), ErrorKind.UNKNOWN),
    ],
)
def test_classify_maps_failures_to_useful_classes(exc: Exception, expected: ErrorKind):
    assert classify(exc) is expected
