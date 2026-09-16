"""Tokens that are minted rather than pasted, for providers that need one.

Every other provider in this library authenticates with a key that a person copies into
`.env` once and that does not expire. Vertex does not: a Google OAuth access token lasts
about an hour, which is fine for a smoke call and useless for a drift run. PLAN.md section
2.2 allows exactly one dependency for this, `google-auth`, "used for the token only", and
this module is where that exception lives and stops.

The shape that matters is that the adapter never sees any of it. `VertexAdapter` receives a
token string and stays pure, so it is still testable against goldens with no credentials and
no network. This module hands the gateway a string; nothing below the gateway knows where
the string came from.

## Why the token is fetched with this library's own HTTP client

`google-auth` ships transports built on `requests` and on `urllib3`, and using either would
mean the token call and the vendor call verify TLS against different trust stores. This
library deliberately verifies against the operating system's store rather than a bundled
list (see boundary/transport.py), because the laptop this is developed on sits behind a
proxy that inspects TLS, and only the OS store knows that proxy's certificate. A token
minted through `requests`' bundled certifi list would fail on that network while every
vendor call succeeded, or the reverse after a change, and the failure would look like a
credentials problem rather than a trust problem.

So `HttpxAuthRequest` below implements `google.auth.transport.Request` over the same client
type the rest of the library uses. It is about fifteen lines, it removes a dependency rather
than adding one, and it means there is exactly one answer in this process to "which
certificates do we trust".

## Refresh

A token is refreshed before it expires rather than after it fails, because a call that fails
on an expired token has already written a ledger row and consumed an attempt. `needs_refresh`
is a pure function of an expiry, a clock and a safety margin, so the interesting part is
testable without credentials, without network and without `google-auth` installed.

The default margin is five minutes, which is longer than any single call's read timeout, so
a token that passes the check at the start of a call cannot expire during it.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from boundary.errors import ConfigError

# Vertex needs cloud-platform. It is the scope Google's own documentation uses for
# `gcloud auth print-access-token`, so a pasted token and a minted one carry the same rights
# and a smoke call proves the same thing either way.
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_SCOPES: tuple[str, ...] = (CLOUD_PLATFORM,)

# Refresh this long before the token actually expires. Longer than the default read timeout
# (120 s), so a token that is fresh when a call starts is still fresh when it finishes.
DEFAULT_SKEW_S = 300.0

_INSTALL_HINT = (
    "minting a Google token needs the `google-auth` package, which is an optional "
    "dependency: install `boundary[vertex]`. Alternatively paste a token into the "
    "environment variable named by `api_key_env` and set `credentials: env`, which is "
    "enough for one short-lived call."
)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(when: dt.datetime) -> dt.datetime:
    """Read a naive datetime as UTC.

    `google-auth` sets `expiry` to a naive datetime that means UTC. Comparing that with an
    aware `now` raises TypeError, and the tempting fix, making `now` naive too, silently
    reads the expiry in local time. On a machine at UTC-2:30 that makes a live token look
    expired, or worse, an expired one look live.
    """
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def needs_refresh(
    expiry: dt.datetime | None, now: dt.datetime, skew_s: float = DEFAULT_SKEW_S
) -> bool:
    """Whether a token expiring at `expiry` should be replaced now.

    `None` means the credentials never expire, which is what a service account with a
    self-signed JWT reports, and is not a reason to refresh anything.
    """
    if expiry is None:
        return False
    return now + dt.timedelta(seconds=skew_s) >= as_utc(expiry)


@dataclass(frozen=True, slots=True)
class AuthResponse:
    """What `google.auth.transport.Request` must return: status, headers, body bytes."""

    status: int
    headers: Mapping[str, str]
    data: bytes


class HttpxAuthRequest:
    """`google.auth.transport.Request` over httpx, so the token call trusts what we trust.

    Duck-typed rather than subclassed on purpose: this class must be constructible and
    testable when `google-auth` is not installed, and inheriting from its abstract base
    would make importing this module require it.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> AuthResponse:
        response = self._client.request(
            method,
            url,
            content=body,
            headers=dict(headers) if headers else None,
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        return AuthResponse(
            status=response.status_code, headers=response.headers, data=response.content
        )


class GoogleCredentials(Protocol):
    """The part of `google.auth.credentials.Credentials` this module uses."""

    token: str | None
    expiry: dt.datetime | None

    def refresh(self, request: Any) -> None: ...


# (credentials, project_id). Injectable so the tests never import google-auth.
Loader = Callable[[Sequence[str]], tuple[GoogleCredentials, str | None]]


def default_loader(scopes: Sequence[str]) -> tuple[GoogleCredentials, str | None]:
    """Application Default Credentials, discovered the way every Google tool discovers them.

    In order: `GOOGLE_APPLICATION_CREDENTIALS` pointing at a service account file, then the
    `gcloud auth application-default login` file, then the metadata server on a Google VM.
    That ordering is Google's, not this library's, which is the point of using their loader
    rather than reading a file here.
    """
    try:
        import google.auth
    except ModuleNotFoundError as e:  # pragma: no cover - exercised by the import guard test
        raise ConfigError(_INSTALL_HINT) from e
    try:
        creds, project = google.auth.default(scopes=list(scopes))
    except Exception as e:
        raise ConfigError(
            f"no Google Application Default Credentials found: {e}. Run "
            f"`gcloud auth application-default login`, or point "
            f"GOOGLE_APPLICATION_CREDENTIALS at a service account key file."
        ) from e
    return creds, project


class GoogleADCToken:
    """A cloud-platform access token from Application Default Credentials, kept fresh.

    One instance per provider entry, held by the gateway for its lifetime, so a run of ten
    thousand calls mints a handful of tokens rather than ten thousand. Locked, because the
    async path can have several calls in flight and a double refresh would be two token
    requests where one would do.
    """

    def __init__(
        self,
        *,
        client: httpx.Client,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        skew_s: float = DEFAULT_SKEW_S,
        loader: Loader = default_loader,
        now: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        self._request = HttpxAuthRequest(client)
        self._scopes = tuple(scopes)
        self._skew_s = skew_s
        self._loader = loader
        self._now = now
        self._lock = threading.Lock()
        self._creds: GoogleCredentials | None = None
        self.project: str | None = None
        # Counted rather than logged: a token is a credential and its value never goes to a
        # log, a span or a ledger row. How many were minted is safe and is the number you
        # want when a run behaves as though it is re-authenticating on every call.
        self.mints = 0

    def token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds, self.project = self._loader(self._scopes)
            creds = self._creds
            if creds.token is None or needs_refresh(creds.expiry, self._now(), self._skew_s):
                try:
                    creds.refresh(self._request)
                except ConfigError:
                    raise
                except Exception as e:
                    raise ConfigError(
                        f"could not refresh the Google access token: {e}. The credentials "
                        f"were found but the refresh failed, so this is a permissions, "
                        f"clock or network problem rather than a missing login."
                    ) from e
                self.mints += 1
            if not creds.token:
                raise ConfigError(
                    "Google credentials refreshed without producing a token; the "
                    "credentials were found but are not usable for Vertex."
                )
            return creds.token
