"""One pinned httpx client, sync and async. Sends exactly the bytes an adapter built.

The transport makes one attempt per call. Retries are a policy of standard mode and live in
the gateway, so that pass-through can never retry by accident.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx
import truststore

from boundary.config import Timeouts
from boundary.providers.base import BuiltRequest

# Exceptions that mean "no response arrived". Their class name becomes the ledger's
# error_type and the response's status.
TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.NetworkError)

# Connection pool limits, the same for both clients. Project 06's load tests hold at least 64
# streams open at once against one host, so the pool must admit that many connections
# without queueing behind its own limit: a client whose pool was the bottleneck would report
# a time to first token that was the pool's and not the server's. httpx's default of 100 is
# already enough; this makes the number explicit and asserted by a test rather than
# inherited from a default that may move.
POOL_LIMITS = httpx.Limits(max_connections=128, max_keepalive_connections=64)

# Called with each chunk of a streamed body and the wall time since the request was sent.
OnChunk = Callable[[bytes, float], None]
AsyncOnChunk = Callable[[bytes, float], Awaitable[None]]


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
        # Verify against the operating system's trust store. certifi's bundle does not know a
        # workplace proxy's inspection certificate; the OS store does, and so does the browser.
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._sync = sync_client or httpx.Client(
            timeout=t, follow_redirects=False, verify=ctx, limits=POOL_LIMITS
        )
        self._async = async_client or httpx.AsyncClient(
            timeout=t, follow_redirects=False, verify=ctx, limits=POOL_LIMITS
        )
        # The exact bytes of the last request body handed to httpx. The pass-through
        # byte-equality test compares this with what the adapter built.
        self.last_sent_body: bytes | None = None

    @property
    def sync_client(self) -> httpx.Client:
        """The pinned synchronous client, for the one thing that is not a model call.

        `boundary.credentials` mints Google access tokens through it, so the token request
        and the vendor request verify TLS against the same store. Exposed rather than
        rebuilt, because two clients with two trust configurations is precisely the bug this
        avoids.
        """
        return self._sync

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

    # -- streaming (0.3) ------------------------------------------------------------------

    def stream(self, built: BuiltRequest, on_chunk: OnChunk) -> HttpResult:
        """Send, and hand the body to `on_chunk` as it arrives instead of after it has.

        Still one attempt and still the adapter's exact bytes. The result carries the whole
        body, so the row's response hash covers every byte the host sent, and the elapsed
        time runs to the last of them. A non-2xx status is read to the end without calling
        `on_chunk`: an error body is not a stream, whatever the request asked for.
        """
        req = self._build(self._sync, built)
        t0 = time.perf_counter()
        resp = self._sync.send(req, stream=True)
        try:
            parts: list[bytes] = []
            if 200 <= resp.status_code < 300:
                for chunk in resp.iter_bytes():
                    parts.append(chunk)
                    on_chunk(chunk, (time.perf_counter() - t0) * 1000.0)
            else:
                parts.append(resp.read())
        finally:
            resp.close()
        elapsed = (time.perf_counter() - t0) * 1000.0
        return HttpResult(resp.status_code, dict(resp.headers.items()), b"".join(parts), elapsed)

    async def astream(self, built: BuiltRequest, on_chunk: AsyncOnChunk) -> HttpResult:
        req = self._build(self._async, built)
        t0 = time.perf_counter()
        resp = await self._async.send(req, stream=True)
        try:
            parts: list[bytes] = []
            if 200 <= resp.status_code < 300:
                async for chunk in resp.aiter_bytes():
                    parts.append(chunk)
                    await on_chunk(chunk, (time.perf_counter() - t0) * 1000.0)
            else:
                parts.append(await resp.aread())
        finally:
            await resp.aclose()
        elapsed = (time.perf_counter() - t0) * 1000.0
        return HttpResult(resp.status_code, dict(resp.headers.items()), b"".join(parts), elapsed)

    def close(self) -> None:
        self._sync.close()

    async def aclose(self) -> None:
        await self._async.aclose()
        self._sync.close()
