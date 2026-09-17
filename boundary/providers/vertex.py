"""Claude on Google Cloud Vertex AI, via `rawPredict`.

Verified against the current documentation on 2026-09-14
(https://platform.claude.com/docs/en/api/claude-on-vertex-ai), as the adapter table in
PLAN.md section 4 requires before any adapter is written.

Two differences from the Claude API, and they are the whole adapter:

    Model     is *not* in the request body. It is in the URL. Sending it in the body as
              well is not harmless: it is an unknown field to this endpoint.
    Version   `anthropic_version` moves out of the header and into the body, and its value
              is `vertex-2023-10-16`, which is a Vertex constant and not the `2023-06-01`
              the direct API pins. Both are pinned; they are simply different pins.

The URL depends on which of three endpoint types the region names, and they are not
interchangeable:

    global            https://aiplatform.googleapis.com/v1/projects/{p}/locations/global/...
    multi-region      https://aiplatform.{r}.rep.googleapis.com/v1/projects/{p}/locations/{r}/...
                      where {r} is `us` or `eu`
    regional          https://{r}-aiplatform.googleapis.com/v1/projects/{p}/locations/{r}/...

...each followed by `publishers/anthropic/models/{model}:rawPredict`.

Which one is used is a residency decision, not a performance one, and it is the worked
example Part B's residency policy is built on. `northamerica-northeast1` is Montreal, so a
Canadian *deployment* is expressible here today, and the region is recorded on every ledger
row so that "where was this request sent" is a query rather than a promise.

Be precise about what that is worth, because the phrase "data residency" promises more than
any of these endpoints deliver. The region says where the request was **sent**. It does not
say where the tokens were **processed**: a model reached through a Canadian endpoint is
commonly still served by a global deployment, and guaranteed in-country processing is the rare
and expensive exception rather than the rule. So what this library can support is "deployed in
Canada", not "processed only in Canada", and Part B's policy enforces where a request is
allowed to go, which is the part a gateway actually controls. Logging a Canadian region and
letting a reader infer Canadian processing would be the exact failure this project exists to
prevent. See docs/hyperscaler-setup.md.

Two consequences are recorded rather than discovered later. Regional and multi-region
endpoints carry a 10 percent pricing premium over global, so a region is not free and the
price file is keyed per region rather than per model alone. And regional endpoints serve
older models only; the newest models are global or multi-region, and there is no Canadian
multi-region, so a Canadian deployment and the newest model can genuinely conflict. The
adapter does not resolve that conflict, it just refuses to hide it: an unknown region and
model pair has no price entry and writes an uncosted row.

Authentication is a Google OAuth bearer token. `google-auth` mints it, in an optional
dependency group and used for the token only, never to build a request or parse a response
(PLAN.md section 2.2). The adapter itself stays pure and simply receives the token, so it is
testable against goldens with no credentials and no network.

Batches are not offered on Vertex, so this adapter does not implement BatchAdapter, and a
batch submitted to a Vertex provider is refused by name before anything is sent.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from boundary.config import ProviderConfig, ProviderKind
from boundary.errors import ConfigError
from boundary.providers._json import dumps
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import BuiltRequest
from boundary.routes import ModelRef
from boundary.types import ChatRequest

# A Vertex constant, not the Claude API's pinned version. Configuration can override it on
# the provider entry, because a future Vertex version bump is a configuration change.
VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"

RAW_PREDICT = "rawPredict"

# Characters left alone when the model id goes into the path. `@` is the one that matters:
# every dated Vertex model id carries one (claude-haiku-4-5@20251001), it is a legal path
# character under RFC 3986, and Google's own documented URL shows it unencoded. Percent
# encoding it produced .../models/claude-haiku-4-5%4020251001:rawPredict, which is not the
# URL the vendor documents and is not a request this library should be inventing. Caught
# 2026-09-16 while writing the setup guide, before the first live call rather than after it.
MODEL_SAFE = "@"


def endpoint_host(region: str) -> str:
    """The host for a region, which differs by endpoint type.

    `global` has no region prefix; `us` and `eu` are multi-region and take the `.rep.` host;
    everything else is a specific region and takes the `{region}-aiplatform` prefix.
    """
    if region == "global":
        return "https://aiplatform.googleapis.com"
    if region in {"us", "eu"}:
        return f"https://aiplatform.{region}.rep.googleapis.com"
    return f"https://{region}-aiplatform.googleapis.com"


class VertexAdapter(AnthropicAdapter):
    """Claude on Vertex. Inherits every parser from the Anthropic adapter, because the
    response is the Messages API's response, and overrides only how a request is built."""

    kind: ClassVar[ProviderKind] = ProviderKind.GCP_VERTEX

    def vertex_body(
        self, ref: ModelRef, request: ChatRequest, provider: ProviderConfig
    ) -> dict[str, Any]:
        """The Messages body with `model` removed and `anthropic_version` added."""
        body = self.message_body(ref, request)
        body.pop("model", None)
        version = ref.api_version or provider.api_version or VERTEX_ANTHROPIC_VERSION
        # First key, so the body reads the way the documentation writes it. Order is not
        # semantic to the vendor, but the raw store is read by people.
        return {"anthropic_version": version, **body}

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        project = provider.project
        if not project:
            raise ConfigError(
                "a gcp_vertex provider needs `project` on its provider entry: the Vertex "
                "URL carries the Google Cloud project id"
            )
        region = ref.region or provider.region
        if not region:
            raise ConfigError(
                "a gcp_vertex provider needs `region` on its provider entry or route: the "
                "Vertex URL carries the location, and the location is a residency decision"
            )
        base = provider.base_url.rstrip("/")
        expected = endpoint_host(region)
        if base != expected:
            # Fail closed. The host and the location path segment both encode geography,
            # and a request whose host says one region while its path says another is
            # served by the host. Letting that through would send data somewhere the caller
            # did not choose, silently, which is the one failure a residency feature cannot
            # have. A route that pins `region` on a provider entry configured for a
            # different region is the way this happens in practice.
            raise ConfigError(
                f"vertex region {region!r} needs base_url {expected!r}, but the provider "
                f"entry says {provider.base_url!r}. The host and the location in the path "
                f"must name the same place; change one of them deliberately."
            )
        url = (
            f"{base}/v1/projects/{quote(project, safe='')}"
            f"/locations/{quote(region, safe='')}"
            f"/publishers/anthropic/models/{quote(ref.model, safe=MODEL_SAFE)}:{RAW_PREDICT}"
        )
        headers: dict[str, str] = {"content-type": "application/json", **provider.headers}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        return BuiltRequest(
            method="POST",
            url=url,
            headers=headers,
            body=dumps(self.vertex_body(ref, request, provider)),
        )
