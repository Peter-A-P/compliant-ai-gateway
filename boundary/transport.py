"""One pinned httpx client, sync and async. Sends exactly the bytes an adapter built.

The transport makes one attempt per call. Retries are a policy of standard mode and live in
the gateway, so that pass-through can never retry by accident.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from boundary.config import Timeouts
from boundary.providers.base import BuiltRequest

# Exceptions that mean "no response arrived". Their class name becomes the ledger's
# error_type and the response's status.
TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.NetworkError)


@dataclass(frozen=True, slots=True)
class HttpResult:
    """What came back: status, headers in full, body bytes, wall time."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: float


class Transport:
    """Pinned client settings: explicit timeouts, no redirects, HTTP/1.1, no retries."""

    def __init__(
        self,
        timeouts: Timeouts,
        *,
        sync_client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        t = httpx.Timeout(
            connect=timeouts.connect_s,
            read=timeouts.read_s,
            write=timeouts.read_s,
            pool=timeouts.connect_s,
        )
        self._sync = sync_client or httpx.Client(timeout=t, follow_redirects=False)
        self._async = async_client or httpx.AsyncClient(timeout=t, follow_redirects=False)
        # The exact bytes of the last request body handed to httpx. The pass-through
        # byte-equality test compares this with what the adapter built.
        self.last_sent_body: bytes | None = None

    def _build(
        self, client: httpx.Client | httpx.AsyncClient, built: BuiltRequest
    ) -> httpx.Request:
        req = client.build_request(
            built.method, built.url, headers=dict(built.headers), content=built.body
        )
        self.last_sent_body = bytes(req.content)
        return req

    def send(self, built: BuiltRequest) -> HttpResult:
        req = self._build(self._sync, built)
        t0 = time.perf_counter()
        resp = self._sync.send(req)
        elapsed = (time.perf_counter() - t0) * 1000.0
        return HttpResult(resp.status_code, dict(resp.headers.items()), resp.content, elapsed)

    async def asend(self, built: BuiltRequest) -> HttpResult:
        req = self._build(self._async, built)
        t0 = time.perf_counter()
        resp = await self._async.send(req)
        elapsed = (time.perf_counter() - t0) * 1000.0
        return HttpResult(resp.status_code, dict(resp.headers.items()), resp.content, elapsed)

    def close(self) -> None:
        self._sync.close()

    async def aclose(self) -> None:
        await self._async.aclose()
        self._sync.close()
