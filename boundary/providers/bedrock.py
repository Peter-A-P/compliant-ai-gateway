"""Claude on Amazon Bedrock, through the Anthropic-native Messages route.

Verified against the current documentation on 2026-09-15, as the adapter table in PLAN.md
section 4 requires before any adapter is written:
    https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html
and confirmed against a live call from `ca-central-1` on the same day.

PLAN.md section 5.1 item 9 assumed this adapter would need SigV4 and the botocore signer.
It does not, and the plan is corrected in the same commit. Bedrock now serves the Anthropic
Messages API at an Anthropic-shaped path, authenticated by a Bedrock API key in the same
`x-api-key` header the direct vendor uses. So the whole adapter is a path and a guard:

    URL      POST {base_url}/anthropic/v1/messages
    Auth     `x-api-key: {key}`, an AWS-issued Bedrock API key read from
             AWS_BEARER_TOKEN_BEDROCK. Inherited unchanged from the Anthropic adapter.
    Version  `anthropic-version: 2023-06-01`, the same pin, in the same header.
    Body     identical, `model` included, where the model identifier is an inference
             profile id rather than a bare model name. See below, because that identifier
             is the residency decision.

That removes the one vendor-SDK exception this adapter was going to carry. No botocore, no
SigV4, no credential chain: the adapter stays pure and testable against goldens with no
credentials and no network.

## The model identifier is the residency decision

Bedrock has two endpoints that both speak this API, and they split exactly along the line a
compliance product cares about.

    bedrock-runtime.{region}.amazonaws.com   geo and global profiles; routes out of region
    bedrock-mantle.{region}.api.aws          bare model id; single region, guaranteed

AWS says so itself, on the Haiku 4.5 model card: "Geo and global inference profiles can
route requests outside the source Region and don't provide single-Region data residency.
For single-Region inference, use the bedrock-mantle endpoint with the bare model ID."

Which one you get is encoded in the model identifier's prefix, and nowhere else:

    anthropic.claude-haiku-4-5                        single region   mantle only
    us.anthropic.claude-haiku-4-5-20251001-v1:0       geo             runtime only
    global.anthropic.claude-haiku-4-5-20251001-v1:0   global          runtime only

So a one-word edit to a model string silently changes where a request may be processed,
and the two halves of that decision live in two different configuration fields. That is the
shape of mistake this library exists to catch, so `build_request` refuses every combination
that does not agree with itself. See the guard below.

## What Canada can and cannot have

`ca-central-1` supports Geo and Global on `bedrock-runtime`, and **not** In-Region. The
`bedrock-mantle` endpoint, which is the only way to get guaranteed single-region
processing, exists in seven regions and none of them is Canadian: us-east-1, us-east-2,
us-west-2, eu-north-1, eu-west-1, ap-northeast-1, ap-southeast-4.

Worse, and worth stating plainly because it reads like a contradiction: the geography AWS
calls "US" contains Canada. Calling `us.anthropic.claude-haiku-4-5-20251001-v1:0` from
`ca-central-1` routes to one of ca-central-1, us-east-1, us-east-2 or us-west-2, and the
response does not say which. So a Canadian buyer can have "processed in Canada or the
United States, unspecified", and cannot have "processed in Canada".

That is still the best Canadian posture of the three platforms: Foundry offers no Canadian
region for Claude at all, and Vertex offers `northamerica-northeast1` for older models
only. It is not data residency, and this adapter does not let a configuration imply it is.

## What the response does not tell you

The response echoes the bare model id even when a profile id was requested: send
`us.anthropic.claude-haiku-4-5-20251001-v1:0` and `model` comes back as
`anthropic.claude-haiku-4-5-20251001-v1:0`. The routing profile is stripped, and the region
that actually served the request is never on the wire.

That is the third platform in a row where the wire says nothing about residency: Foundry
hides the hosting version, Vertex hides the processing location, Bedrock hides the profile.
It has to be recorded at configuration time or it is not recorded at all, which is why
`region` and `residency` are ledger-visible configuration here rather than parsed values.

## Batches

Bedrock has batch inference, and it is not this API. `CreateModelInvocationJob` takes its
input and writes its output as JSON Lines objects in S3, so it needs an object store, a
bucket policy and an IAM service role before it can run at all. It shares no shape with the
Message Batches API the Anthropic adapter implements.

So this adapter deliberately does not implement BatchAdapter, and a batch aimed at a
bedrock provider is refused by name in boundary.providers before anything is sent. Building
it is a separate piece of work with a separate dependency, not an override on this class.

## Prices

There are none in the price files, on purpose. Bedrock is a partner-operated platform: AWS
sets and publishes its own rates, and Anthropic's pricing page defers to them rather than
restating them. Those rates were not readable from the published page on 2026-09-15, and
the rule is that an unknown price writes an uncosted row and never an estimate. So a
Bedrock call is uncosted today.

When the rates are copied in, from https://aws.amazon.com/bedrock/pricing/ or from the
account's own Cost Explorer, remember two things. Geo and regional endpoints carry a 10
percent premium over global for Sonnet 4.5, Haiku 4.5, Opus 4.5 and later, so the Canadian
routing is not the global rate and needs its own entry. And Bedrock charges appear in Cost
Explorer under the model provider rather than under Bedrock, which will otherwise cost an
hour of the monthly invoice check. See docs/prices.md.
"""

from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlsplit

from boundary.config import ProviderConfig, ProviderKind, Residency
from boundary.errors import ConfigError
from boundary.providers._json import dumps
from boundary.providers.anthropic import AnthropicAdapter
from boundary.providers.base import BuiltRequest
from boundary.routes import ModelRef
from boundary.types import ChatRequest

# Bedrock serves the Messages API one path segment deeper than the direct vendor does.
MESSAGES_PATH = "/anthropic/v1/messages"

# The two hosts, each carrying its region in the name. Anchored, because the whole point of
# reading the host is to know which region and which endpoint class a request is aimed at,
# and a pattern that matched a suffix would accept a look-alike host.
_RUNTIME_HOST = re.compile(r"^bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com$")
_MANTLE_HOST = re.compile(r"^bedrock-mantle\.([a-z0-9-]+)\.api\.aws$")

# The geography prefixes AWS defines for Claude. A model id starting with one of these is a
# geo cross-region inference profile. `global` is its own thing and is handled separately.
GEO_PREFIXES = frozenset({"us", "eu", "au", "jp"})

GLOBAL_PREFIX = "global"

# The regions where bedrock-mantle serves Claude, which is the only route to guaranteed
# single-region processing. Read from the Haiku 4.5 model card on 2026-09-15. Recorded here
# for the error message only: the adapter does not refuse an unlisted region, because this
# list will grow and a stale constant must not be the thing that blocks a working call.
MANTLE_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-north-1",
    "eu-west-1",
    "ap-northeast-1",
    "ap-southeast-4",
)


def residency_of(model: str) -> Residency:
    """Which residency class a Bedrock model identifier asks for.

    The prefix is the whole signal. `us.`, `eu.`, `au.` and `jp.` are geographic
    cross-region inference profiles; `global.` routes to any commercial region; anything
    else is a bare model id, which only the mantle endpoint accepts and which is the only
    form that stays in one region.
    """
    prefix, _, rest = model.partition(".")
    if not rest:
        return Residency.SINGLE_REGION
    if prefix == GLOBAL_PREFIX:
        return Residency.GLOBAL
    if prefix in GEO_PREFIXES:
        return Residency.GEO
    return Residency.SINGLE_REGION


def split_host(base_url: str) -> tuple[bool, str]:
    """(is_mantle, region) read out of a Bedrock base URL.

    Raises ConfigError for anything that is not one of the two Bedrock hosts, rather than
    guessing. A host this function does not recognise is a host whose residency behaviour
    this library cannot describe, and describing it is the entire job.
    """
    host = urlsplit(base_url).hostname or ""
    runtime = _RUNTIME_HOST.match(host)
    if runtime:
        return False, runtime.group(1)
    mantle = _MANTLE_HOST.match(host)
    if mantle:
        return True, mantle.group(1)
    raise ConfigError(
        f"base_url {base_url!r} is not a Bedrock endpoint. Expected "
        f"https://bedrock-runtime.{{region}}.amazonaws.com for geo and global inference "
        f"profiles, or https://bedrock-mantle.{{region}}.api.aws for single-region "
        f"inference with a bare model id."
    )


class BedrockAdapter(AnthropicAdapter):
    """Claude on Amazon Bedrock. Inherits every parser from the Anthropic adapter, because
    the response is the Messages API's response, and overrides only how a request is built.

    One parser for the direct vendor and all three platforms means a change to how usage is
    read cannot drift between them, which is the property the monthly invoice check depends
    on.
    """

    kind: ClassVar[ProviderKind] = ProviderKind.AWS_BEDROCK

    def build_request(
        self,
        ref: ModelRef,
        request: ChatRequest,
        provider: ProviderConfig,
        api_key: str | None,
    ) -> BuiltRequest:
        self.check_residency(ref, provider)
        return BuiltRequest(
            method="POST",
            url=provider.base_url.rstrip("/") + MESSAGES_PATH,
            headers=self._headers(provider, api_key, ref.api_version),
            body=dumps(self.message_body(ref, request)),
        )

    def check_residency(self, ref: ModelRef, provider: ProviderConfig) -> None:
        """Refuse any request whose three statements about geography disagree.

        A Bedrock call says where it may be processed in three separate places: the host,
        the model identifier's prefix, and the `residency` field on the provider entry.
        Nothing on the wire reconciles them and the response reports none of them, so a
        disagreement is invisible after the fact. Each check below is one way a request
        could be processed somewhere the caller did not choose, so each one fails closed.
        """
        is_mantle, host_region = split_host(provider.base_url)
        wanted = residency_of(ref.model)

        # 1. The endpoint and the model identifier have to be the same kind of thing. AWS
        #    rejects the mismatched pairs too, but with a validation error that names the
        #    model rather than the residency consequence, which is the part that matters.
        if is_mantle and wanted is not Residency.SINGLE_REGION:
            raise ConfigError(
                f"model {ref.model!r} is a {wanted.value} inference profile and the mantle "
                f"endpoint takes a bare model id only. Either use the bare id for "
                f"single-region inference in {host_region!r}, or send this profile to "
                f"https://bedrock-runtime.{host_region}.amazonaws.com and accept that it "
                f"may be processed outside {host_region!r}."
            )
        if not is_mantle and wanted is Residency.SINGLE_REGION:
            raise ConfigError(
                f"model {ref.model!r} is a bare model id and the bedrock-runtime endpoint "
                f"requires a geo or global inference profile for on-demand throughput. "
                f"Prefix it (us., eu., au., jp. or global.) and accept the routing that "
                f"names, or move to https://bedrock-mantle.{host_region}.api.aws for "
                f"single-region inference. Mantle serves Claude in "
                f"{', '.join(MANTLE_REGIONS)}."
            )

        # 2. A region pinned on the provider entry or the route has to be the region in the
        #    host. Both encode geography, the host wins on the wire, and a route that pins
        #    a region on a provider entry configured for a different one is exactly how a
        #    request ends up somewhere nobody chose.
        region = ref.region or provider.region
        if region is not None and region != host_region:
            raise ConfigError(
                f"bedrock region {region!r} does not match base_url "
                f"{provider.base_url!r}, whose host names {host_region!r}. The host decides "
                f"where the request is sent; change one of them deliberately."
            )

        # 3. The provider entry's declared intent has to match what the model id will
        #    actually do. This is the check that survives an edit: swapping a model string
        #    from `global.` to `us.` is one character of diff and silently changes the
        #    residency, and this is the only place that notices.
        if provider.residency is not None and provider.residency is not wanted:
            raise ConfigError(
                f"provider entry declares residency {provider.residency.value!r} but model "
                f"{ref.model!r} is a {wanted.value} identifier. The declaration and the "
                f"model id must agree; change whichever one is wrong."
            )
