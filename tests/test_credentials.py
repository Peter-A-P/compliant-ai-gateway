"""Minted tokens: when they are refreshed, how they are fetched, and what leaks.

Vertex is the only provider here whose credential expires, so it is the only one that needs
this. The tests never import `google-auth` and never reach a network: the credentials object
is injected, the clock is injected, and the one piece of real HTTP is mocked. That is
deliberate rather than convenient, because `google-auth` is an optional dependency and the
behaviour that matters when it is absent is a clear error rather than an ImportError from
three frames down.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
import respx

from boundary.credentials import (
    DEFAULT_SKEW_S,
    GoogleADCToken,
    GoogleCredentials,
    HttpxAuthRequest,
    as_utc,
    default_loader,
    needs_refresh,
)
from boundary.errors import ConfigError
from boundary.gateway import Gateway
from boundary.types import ChatRequest

T0 = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.UTC)


class FakeCredentials:
    """The three members of google.auth.credentials.Credentials this library touches."""

    def __init__(
        self,
        *,
        token: str | None = None,
        expiry: dt.datetime | None = None,
        lifetime_s: float = 3600.0,
        fails: Exception | None = None,
        clock: list[dt.datetime] | None = None,
    ) -> None:
        self.token = token
        self.expiry = expiry
        self._lifetime_s = lifetime_s
        self._fails = fails
        self._clock = clock
        self.refreshes = 0
        self.last_request: Any = None

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.last_request = request
        if self._fails is not None:
            raise self._fails
        now = self._clock[0] if self._clock else T0
        self.token = f"ya29.token-{self.refreshes}"
        if self._lifetime_s:
            self.expiry = now + dt.timedelta(seconds=self._lifetime_s)


class RecordingLoader:
    """Stands in for Application Default Credentials, and remembers what it was asked for."""

    def __init__(self, creds: FakeCredentials, project: str | None = "pap-portfolio") -> None:
        self.creds = creds
        self.project = project
        self.scopes: tuple[str, ...] = ()
        self.calls = 0

    def __call__(self, scopes: Sequence[str]) -> tuple[GoogleCredentials, str | None]:
        self.scopes = tuple(scopes)
        self.calls += 1
        return self.creds, self.project


def _loader(creds: FakeCredentials, project: str | None = "pap-portfolio") -> RecordingLoader:
    return RecordingLoader(creds, project)


def _source(creds: FakeCredentials, clock: list[dt.datetime], **kw: Any) -> GoogleADCToken:
    return GoogleADCToken(
        client=httpx.Client(),
        loader=_loader(creds),
        now=lambda: clock[0],
        **kw,
    )


# -- when to refresh ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "expected", "why"),
    [
        (None, False, "credentials that never expire are never refreshed"),
        (T0 + dt.timedelta(hours=1), False, "an hour left is plenty"),
        (T0 + dt.timedelta(seconds=DEFAULT_SKEW_S + 1), False, "just outside the margin"),
        (T0 + dt.timedelta(seconds=DEFAULT_SKEW_S), True, "exactly at the margin refreshes"),
        (T0 + dt.timedelta(seconds=60), True, "inside the margin"),
        (T0 - dt.timedelta(seconds=1), True, "already expired"),
    ],
)
def test_needs_refresh(expiry: dt.datetime | None, expected: bool, why: str) -> None:
    assert needs_refresh(expiry, T0) is expected, why


def test_a_naive_expiry_is_read_as_utc_not_as_local_time() -> None:
    """The trap, pinned.

    google-auth sets `expiry` to a naive datetime meaning UTC. Comparing it with an aware
    `now` raises TypeError, and the obvious fix, making `now` naive too, reads the expiry in
    local time instead. On this laptop, at UTC-2:30, that would call a token with fifty
    minutes left expired, and in the other direction it would call an expired token live and
    send it.
    """
    naive = dt.datetime(2026, 9, 15, 13, 0, 0)  # one hour after T0, in UTC
    assert as_utc(naive) == T0 + dt.timedelta(hours=1)
    assert needs_refresh(naive, T0) is False
    assert needs_refresh(dt.datetime(2026, 9, 15, 12, 1, 0), T0) is True


# -- the token source --------------------------------------------------------------------


def test_the_first_call_loads_and_refreshes_once() -> None:
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    src = _source(creds, clock)
    assert src.token() == "ya29.token-1"
    assert creds.refreshes == 1
    assert src.mints == 1
    assert src.project == "pap-portfolio"


def test_a_live_token_is_reused_rather_than_minted_again() -> None:
    """The reason this class exists: a run of many calls mints a handful of tokens."""
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    src = _source(creds, clock)
    first = src.token()
    for _ in range(1000):
        assert src.token() == first
    assert creds.refreshes == 1
    assert src.mints == 1


def test_a_token_near_expiry_is_replaced_before_it_fails() -> None:
    """Refresh happens on the margin, not on a 401.

    A call that fails on an expired token has already written a ledger row and spent an
    attempt, so the expiry is anticipated rather than discovered.
    """
    clock = [T0]
    creds = FakeCredentials(clock=clock, lifetime_s=3600)
    src = _source(creds, clock)
    assert src.token() == "ya29.token-1"

    clock[0] = T0 + dt.timedelta(minutes=50)  # ten minutes left, outside the five-minute margin
    assert src.token() == "ya29.token-1", "still fresh enough"
    assert creds.refreshes == 1

    clock[0] = T0 + dt.timedelta(minutes=57)  # three minutes left, inside the margin
    assert src.token() == "ya29.token-2"
    assert creds.refreshes == 2
    assert src.mints == 2


def test_credentials_that_never_expire_are_refreshed_once_and_then_left_alone() -> None:
    """A service account using a self-signed JWT reports no expiry. Not a reason to churn."""
    clock = [T0]
    creds = FakeCredentials(clock=clock, lifetime_s=0)
    src = _source(creds, clock)
    assert src.token() == "ya29.token-1"
    clock[0] = T0 + dt.timedelta(days=30)
    assert src.token() == "ya29.token-1"
    assert creds.refreshes == 1


def test_a_failing_refresh_is_a_config_error_that_says_which_kind_of_problem_it_is() -> None:
    """Found credentials that will not refresh is not the same as no credentials, and the
    message says so, because the two have completely different fixes."""
    clock = [T0]
    creds = FakeCredentials(clock=clock, fails=RuntimeError("clock skew too large"))
    src = _source(creds, clock)
    with pytest.raises(ConfigError, match="rather than a missing login"):
        src.token()


def test_credentials_that_refresh_without_a_token_are_refused() -> None:
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    creds.refresh = lambda request: None  # type: ignore[method-assign]
    src = _source(creds, clock)
    with pytest.raises(ConfigError, match="without producing a token"):
        src.token()


def test_the_scope_is_cloud_platform() -> None:
    """A minted token has to carry the same rights as a pasted one, or a smoke call proves
    something the drift run cannot repeat."""
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    loader = _loader(creds)
    src = GoogleADCToken(client=httpx.Client(), loader=loader, now=lambda: clock[0])
    src.token()
    assert loader.scopes == ("https://www.googleapis.com/auth/cloud-platform",)


# -- the transport -----------------------------------------------------------------------


def test_the_token_request_goes_through_this_librarys_client() -> None:
    """Not requests, not urllib3.

    Both of google-auth's own transports verify TLS against a bundled certificate list, and
    this library verifies against the operating system's store so that a TLS-inspecting
    proxy is trusted the same way the browser trusts it. Two clients would mean two answers
    to which certificates we trust, and the failure would look like bad credentials.
    """
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t"}, headers={"x-h": "1"})
        )
        request = HttpxAuthRequest(httpx.Client())
        response = request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            body=b"grant_type=refresh_token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    assert response.status == 200
    assert response.headers["x-h"] == "1"
    assert b"access_token" in response.data
    sent = route.calls[0].request
    assert sent.content == b"grant_type=refresh_token"
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"


def test_the_request_object_handed_to_google_auth_is_ours() -> None:
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    src = _source(creds, clock)
    src.token()
    assert isinstance(creds.last_request, HttpxAuthRequest)


# -- the optional dependency -------------------------------------------------------------


def test_the_missing_dependency_names_the_extra_to_install() -> None:
    """google-auth is deliberately absent from the development environment, so this is the
    real failure rather than a simulated one. An ImportError three frames down would not
    tell anyone what to do about it."""
    try:
        import google.auth  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:  # pragma: no cover - only when the extra is installed
        pytest.skip("google-auth is installed, so the import guard cannot be reached")
    with pytest.raises(ConfigError, match=r"boundary\[vertex\]"):
        default_loader(["https://www.googleapis.com/auth/cloud-platform"])


# -- through the gateway -----------------------------------------------------------------


def _vertex_url(gw: Gateway, model: str = "claude-opus-5") -> str:
    """The rawPredict URL for the checked-in vertex entry.

    Derived rather than pasted. These tests are about where the token comes from and where it
    ends up, not about the URL, and a hardcoded one turns a deliberate configuration change
    (the region moving to global when Model Garden turned out to offer no Canadian location)
    into three unrelated failures.
    """
    pc = gw.config.providers["vertex"]
    return (
        f"{pc.base_url}/v1/projects/{pc.project}/locations/{pc.region}"
        f"/publishers/anthropic/models/{model}:rawPredict"
    )


VERTEX_OK = {
    "id": "msg_01Vertex",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 7, "output_tokens": 2},
}


def _vertex_call(gw: Gateway) -> None:
    gw.chat(
        ChatRequest(
            model="vertex/claude-opus-5",
            messages=[{"role": "user", "content": "Q?"}],
            max_tokens=8,
        ),
        purpose="dev",
    )


def test_a_pasted_token_wins_and_nothing_is_minted(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How CI works: a token from an earlier step, on a runner with no gcloud.

    The provider entry says `google_adc`, and the variable being set is the operator saying
    "use this one". Nothing is minted, so a runner without the optional dependency installed
    still makes the call.
    """
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "ya29.pasted-by-ci")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_vertex_url(gw)).mock(return_value=httpx.Response(200, json=VERTEX_OK))
        _vertex_call(gw)
    assert route.calls[0].request.headers["authorization"] == "Bearer ya29.pasted-by-ci"
    assert gw._token_sources == {}, "nothing was minted"


def test_an_unset_variable_mints_a_token_and_sends_it(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How a laptop works: gcloud application-default login and no variable."""
    monkeypatch.delenv("GOOGLE_VERTEX_ACCESS_TOKEN", raising=False)
    clock = [T0]
    creds = FakeCredentials(clock=clock)
    monkeypatch.setattr(
        "boundary.gateway.GoogleADCToken",
        lambda **kw: GoogleADCToken(loader=_loader(creds), now=lambda: clock[0], **kw),
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_vertex_url(gw)).mock(return_value=httpx.Response(200, json=VERTEX_OK))
        _vertex_call(gw)
    assert route.calls[0].request.headers["authorization"] == "Bearer ya29.token-1"
    assert creds.refreshes == 1


def test_the_minted_token_is_not_written_to_the_ledger_or_the_raw_store(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token is a credential wherever it came from, and minting it does not change that."""
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "ya29.should-never-appear")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_vertex_url(gw)).mock(return_value=httpx.Response(200, json=VERTEX_OK))
        _vertex_call(gw)
    rows = gw.ledger.rows()
    assert "ya29.should-never-appear" not in str(dict(rows[0]))


# -- against the real library, when the extra is installed -------------------------------


def test_google_auth_accepts_our_transport_and_returns_a_naive_expiry() -> None:
    """The only test here that touches `google-auth`, and the only one that can prove the
    two load-bearing assumptions in `boundary/credentials.py` are true of the real library.

    Skipped unless the `vertex` extra is installed, which CI does in a step of its own so
    that the rest of the suite still runs with the dependency absent and exercises the
    import guard.

    Assumption one: google-auth's `refresh` accepts a duck-typed
    `google.auth.transport.Request`, so the token can be fetched through this library's own
    client rather than through `requests`.

    Assumption two: the `expiry` it sets is naive. That is not a hypothetical. Without
    `as_utc`, comparing it against an aware `now` raises TypeError on the first refresh, and
    the obvious fix reads it in local time.
    """
    pytest.importorskip("google.auth", reason="install the `vertex` extra to run this")
    from google.oauth2.credentials import Credentials as RealCredentials

    creds = RealCredentials(
        token=None,
        refresh_token="rt",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="cs",
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://oauth2.googleapis.com/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "ya29.real-refresh",
                    "expires_in": 3599,
                    "token_type": "Bearer",
                },
            )
        )
        src = GoogleADCToken(client=httpx.Client(), loader=lambda scopes: (creds, "pap-portfolio"))
        assert src.token() == "ya29.real-refresh"

    assert src.mints == 1
    assert creds.expiry is not None
    assert creds.expiry.tzinfo is None, "google-auth still returns a naive expiry"
    # And the freshly minted token, read through as_utc, is not immediately stale.
    assert needs_refresh(creds.expiry, dt.datetime.now(dt.UTC)) is False
