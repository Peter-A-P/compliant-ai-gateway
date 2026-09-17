# Setting up Microsoft Foundry, Amazon Bedrock and Google Vertex

Three adapters, written and tested. **One of them has been called.** Bedrock answered a live
request from `ca-central-1` on 2026-09-15; Foundry is blocked on a quota grant and Vertex is
waiting on a Google Cloud project. This is what has to be created, where, and in what order,
and what to bring back into this repository afterwards.

Read the warnings at the bottom before you start. The first one decides which region you build
in on every platform, and on two of the three a Canadian region will not work, so read it
first.

If you only want one platform working today, it is Bedrock. It is the only one that answered.

---

## Microsoft Foundry

You already have the Azure subscription (`PAP-POCs`) and a budget on it (`portfolio-monthly`,
CA$50 a month, alerts at 50, 80 and 100 percent). Foundry needs a resource and a deployment
inside it.

### 1. Create a Foundry resource

In the [Foundry portal](https://ai.azure.com/), create a Foundry resource, or create a Foundry
project which creates one for you. A **resource** holds the security and billing configuration;
**deployments** inside it are what you actually call.

**The region matters, and Canada will not work.** A resource in Canada Central or Canada East
offers no Anthropic model at all; see the residency section below, which is the finding rather
than an aside. Use `eastus2` for the widest Claude coverage, or `eastus` or `centralus` for
`claude-haiku-4-5`.

Note the **resource name**. It becomes the host: `https://{resource}.services.ai.azure.com`.

### 2. Give yourself a role that can use it

An Azure RBAC role on the resource: **Foundry User** (formerly Azure AI User) or **Cognitive
Services User**. Without one you get `403 Forbidden` at call time, not at setup time.

### 3. Deploy a Claude model

In the portal: **Discover** > **Models**, search for a Claude model, open it, **Deploy**, then
**Custom settings** rather than Default settings.

Four choices here, and three of them are permanent or cost money:

- **Marketplace terms.** On your first Claude deployment you accept the Azure Marketplace
  terms and pick an industry. This is the step that makes billing real.
- **Deployment name.** Defaults to the model id. **It cannot be changed after creation**, and
  it is what goes in the request and lands in the ledger, so it is also the price file key.
  Keep the default (`claude-haiku-4-5`) unless you have a reason not to: the price file
  already has entries for the default names, and a custom name needs its own entry or its
  calls are uncosted.
- **Region scope.** `Global`, or `Data Zone` for models hosted on Azure. **The only data
  zone is the United States**: Europe and Asia Pacific read "Not available" and there is no
  Canadian one. US Data Zone keeps inference in the United States and costs 1.1x. It is not
  in the price file, so if you pick it, it needs its own entry with its own numbers.
- **Model version, which is the hosting option and matters more than its name suggests.**
  Version 1 is Hosted on Anthropic: the model runs on Anthropic's own infrastructure,
  **outside Azure**. Version 2 is Hosted on Azure: it runs on Azure infrastructure end to
  end, prompts and completions stay within Azure, and only usage metadata and
  safety-flagged content egress to Anthropic. Version 2 is the stronger compliance
  position and it supports fewer features.

  Nothing in this library cares which you deploy. Both are `POST /anthropic/v1/messages`
  with the same body, the same `api-key` header and the same standard rates billed in CCUs,
  so one price entry serves both. What changes is the residency claim, and **nothing on the
  wire records which version is behind a deployment name**: the ledger cannot tell you, so
  if a residency claim depends on the hosting version, something has to record it at
  configuration time.

  The version does **not** decide whether you can deploy. See the quota section below, which
  corrects an earlier reading of this.

For a first smoke call, deploy **claude-haiku-4-5**, Global Standard, either version. It is
the cheapest thing to prove the path with. Whether it deploys at all depends on the
subscription, not on any of these four choices.

### 3a. If the deployment is refused for quota, read the allocation

This is the wall, and it is not where it looks like it is. The heading of this section named
the wrong cause twice before the number was looked up; the answer is below and the record of
getting there is kept deliberately.

Deploying `claude-haiku-4-5` Global Standard in `eastus2` on 2026-09-14 was refused with
*"Insufficient quota ... cannot be deployed to your current project"*, on **both** offered
model versions. The first reading of that, recorded here and then corrected, was that
Azure-hosted capacity is rationed per region and the Anthropic-hosted version escapes it.
Both halves of that are wrong, and Microsoft's quota table says so
([Claude model quotas and rate limits](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models-quotas-limits),
read 2026-09-14):

| Subscription type | `claude-haiku-4-5`, Global Standard | Every other Claude model |
|---|---|---|
| Pay-as-you-go | 80 RPM, 80,000 ITPM, 16,000 OTPM | 40 to 80 RPM, except the Fable line at 0 |
| **Free Trial** | **0 RPM, 0 ITPM, 0 OTPM** | **0 across the board** |
| Enterprise and MCA-E | 10,000 RPM | 4,000 to 10,000 RPM |

Two corrections fall out of that table:

- **Quota is per subscription, not per region.** "Resources and regions share quota instead
  of receiving separate allocations": every Global Standard deployment of a model draws from
  one pool across all regions. Moving the resource to another region changes nothing.
- **The hosting version is not the gate.** Both versions are marked quota-allocatable for
  haiku on pay-as-you-go, with the same default. Neither is privileged.

**Answered 2026-09-14 by asking the API instead of reading the error.** The published
default and the allocated default are not the same number. Microsoft's table says a
pay-as-you-go subscription gets 80 RPM for `claude-haiku-4-5` on Global Standard. What a real
pay-as-you-go subscription actually has is **zero, for every Claude model, in every region
that offers them**.

Reproduce it in one command, which is the point of writing this down:

```
az cognitiveservices usage list -l eastus2 \
  --query "[?contains(to_string(name.value),'laude')].{quota:name.value, limit:limit}" -o table
```

What that returned here, on subscription quota id `PayAsYouGo_2014-09-01` with the spending
limit off:

| Check | Result |
|---|---|
| Claude quota entries in `eastus2` | 19 |
| ...of which non-zero | **0** |
| `claude-haiku-4-5` in `eastus`, `centralus`, `swedencentral` | limit 0, both versions |
| `claude-haiku-4-5` in `canadacentral` | **no quota entry at all** |
| Non-Claude AIServices quotas in `eastus2` | **159 non-zero** (`OpenAI.Standard.gpt-35-turbo` at 200, and so on) |

The last row is what makes the rest meaningful. The subscription is provisioned normally and
has working allocations for everything else in the same service. Only the Anthropic models are
at zero, and they are at zero everywhere, on both hosting versions: the API exposes them
separately as `AIServices.GlobalStandard.claude-haiku-4-5` and
`...claude-haiku-4-5.Azure`, and both read 0.

So the earlier readings were wrong for an interesting reason rather than a careless one. This
is not a Free Trial, not a regional shortage, and not a property of the hosting option. **The
documented "default" describes a ceiling you may request, not an allowance you receive.** On
this platform Claude quota starts at zero and stays there until somebody grants it, through
[the request form](https://aka.ms/oai/stuquotarequest), which is "evaluated individually and
aren't guaranteed to be approved".

The `canadacentral` row is worth noticing on its own: the region has no Claude quota *entries*,
where the others have entries set to zero. That is the region-availability finding confirmed
from a second, independent source, the resource provider rather than the documentation table.

**Why a compliance project writes this down.** Three separate readings of the same failure
were wrong before the number was looked up, and each wrong reading was plausible, actionable
and would have wasted a day: change the hosting version, change the subscription, change the
region. The error message supports all three and none of them is the cause. A team evaluating
this platform on a schedule will burn that time, and the honest thing to put in a procurement
note is that Claude on Foundry has a gate before the first call which is neither technical nor
priced: an approval, with an unknown lead time and no guarantee. That belongs in a go-live
plan, not in a troubleshooting appendix.

**What it means for this repository.** The Foundry adapter stays written, tested against
goldens, and uncalled. It owes one live row and cannot have one until the quota request is
granted. That is recorded as the reason rather than left as an unticked box.

### 4. Collect the two values

**Build** > **Models** > your deployment > **Details** tab gives you:

- **Target URI** - the endpoint. Should look like `https://{resource}.services.ai.azure.com`.
- **Key** - the API key.

### 5. Put them in this repository

In `.env` (gitignored, never committed):

```
AZURE_FOUNDRY_API_KEY=<the Key from the Details tab>
```

In `config/boundary.yaml`, replace the placeholder in the `foundry` provider entry:

```yaml
  foundry:
    kind: azure_foundry
    base_url: https://YOUR-RESOURCE.services.ai.azure.com/anthropic
```

Keep the `/anthropic` suffix. The adapter appends `/v1/messages` to it.

### 6. Make the call, not from this laptop

Vendor calls from the work network go through the employer's TLS inspection proxy (decision
2026-09-10). Run it from GitHub Actions or a non-inspected network:

```
boundary smoke foundry
```

That writes one ledger row. It should be `costed = 1`, because the price file has the default
deployment names. If it comes back uncosted, the deployment name is not one of the five in
`boundary/prices/2026-09-14.yaml` and needs an entry.

---

## Amazon Bedrock

This is the one that works. Done on 2026-09-15, signup to answered call in about an hour, and
every number below was read from the account rather than from a documentation table.

Bedrock needs an account, a one-time Anthropic form, and a key. There is no resource to
create, no deployment to name, and no capacity to request.

### 1. Open the account

[portal.aws.amazon.com/billing/signup](https://portal.aws.amazon.com/billing/signup). Email,
card, phone verification.

The free plan gives US$100 of credit immediately and up to US$100 more earned, and **the
account closes itself six months after opening** unless you convert it to the paid plan.
Converting costs nothing: there is no account fee, Bedrock has no standing charge, and you pay
only for usage. So the six-month clock is a diary risk rather than a bill. Set a reminder to
convert about a month before the date and the risk is gone.

### 2. Stop using the root user

Console, top right, **Security credentials**: turn on MFA for the root user, create no access
keys for it, and make yourself an administrator identity to use instead, through
[IAM Identity Center](https://console.aws.amazon.com/singlesignon) or a plain IAM user with
`AdministratorAccess`. Do everything below as that identity.

### 3. Set the budget before the first call

[Billing, Budgets](https://console.aws.amazon.com/billing/home#/budgets), **Create budget**,
cost budget, monthly, alerts at 50, 80 and 100 percent. PLAN.md section 5.1 item 9 makes this a
precondition rather than a nicety, and it is the step that is annoying to do after you need it.

### 4. Submit the Anthropic use-case form

This is Bedrock's one gate, and unlike Foundry's it opens immediately.

Model access is on by default for everything else, but **Anthropic models need a one-time
First Time Use form per account**. [Bedrock console](https://console.aws.amazon.com/bedrock),
switch to **Canada (Central) ca-central-1**, **Model catalog**, choose **Claude Haiku 4.5**,
and it prompts you. It asks for company name, website, industry, intended users and use cases;
AWS says in writing that an individual developer may give a GitHub profile or project URL
instead of a company site. Your IAM identity needs `aws-marketplace:Subscribe`, `Unsubscribe`
and `ViewSubscriptions`, which `AdministratorAccess` covers.

> Access to the model is granted immediately after use case details are successfully
> submitted.

There is a CLI route as well, `aws bedrock put-use-case-for-model-access --form-data
<base64 json>`, if the console form fights you.

### 5. Check what you actually got, rather than what is documented

Two commands, and they are the point of this section. Do not infer an allocation from an error
message; that mistake is what the Foundry section below is an apology for.

```
aws bedrock get-foundation-model-availability \
  --model-id anthropic.claude-haiku-4-5-20251001-v1:0 --region ca-central-1

aws service-quotas list-service-quotas --service-code bedrock --region ca-central-1 \
  --query "Quotas[?contains(QuotaName,'Haiku')].{q:QuotaName,v:Value}" --output table
```

Note that the quota names are spaced and capitalised (`... for Anthropic Claude Haiku 4.5`),
not hyphenated like the model id. A filter written in model-id spelling silently returns `[]`,
which reads like "no quota" and is not. That cost a round trip here.

The first command should return all four fields green:

```
"agreementAvailability": { "status": "AVAILABLE" },
"authorizationStatus": "AUTHORIZED",
"entitlementAvailability": "AVAILABLE",
"regionAvailability": "AVAILABLE"
```

The second returns the finding. See [what a new account is actually allocated](#what-a-new-account-is-actually-allocated) below, because the numbers are not the
published defaults and the difference decides what you can schedule.

### 6. Generate an API key

[Bedrock console, API keys, long-term, create](https://console.aws.amazon.com/bedrock/home#/api-keys/long-term/create).
Set an expiry, 90 days is plenty, and copy the value once because it is shown once.

AWS labels long-term keys "for exploration only" and prefers short-term ones, which last up to
12 hours and need a token-generator library to refresh. That is the same shape of problem
Vertex already has, and it is deferred for the same reason: one key in `.env` proves the path,
and the refreshing credential layer gets written when something needs to run for longer than a
key lasts.

It goes in `.env` as `AWS_BEARER_TOKEN_BEDROCK`. Not into git, not into a ledger row, and not
pasted into CloudShell, whose home directory persists.

### 7. Make the call, not from this laptop

```
boundary smoke bedrock
```

To test the endpoint by hand instead, note that Bedrock is the only one of the three platforms
whose auth header is the same as the direct vendor's:

```
curl -sS -X POST https://bedrock-runtime.ca-central-1.amazonaws.com/anthropic/v1/messages \
  -H "x-api-key: $AWS_BEARER_TOKEN_BEDROCK" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"us.anthropic.claude-haiku-4-5-20251001-v1:0","max_tokens":16,"messages":[{"role":"user","content":"Reply with the word ok."}]}'
```

On Windows that is a PowerShell command and PowerShell will break it three ways: `\` is not a
line continuation (use a backtick, or one line), `$NAME` is not an environment variable (it is
`$env:NAME`), and a value in `.env` is not in the environment at all until something loads it.
An empty `x-api-key` header produces a confusing error rather than an auth failure.

That writes one ledger row, and it will be **uncosted**, because AWS publishes its own rates
and they are not in the price files. See [prices.md](prices.md).

### 8. What goes in this repository

The provider entry is already in `config/boundary.yaml`, with `residency: geo` declared against
a `us.` model identifier. Read the comment above it before changing either, because those two
fields check each other and that is the only check there is.

---

## Google Vertex

You already have a Google Cloud project with billing linked (the Gemini key's project, moved
off the free tier on 2026-09-10) and a US$15 a month budget with alerts. Vertex needs the API
enabled, the partner model enabled, a role, and a token.

**Run the commands in [Cloud Shell](https://shell.cloud.google.com)**, not on this laptop.
It is a browser terminal with `gcloud` installed and already authenticated as you, the same
shape of thing as AWS CloudShell, reachable from the `>_` icon in the top right of any Google
Cloud console page. Nothing to install, and it runs inside Google, so it is also the answer to
the work network's TLS inspection.

### 1. Note the project id

Not the project *name* and not the number. The id, which is what goes in the URL.

### 2. Enable the API

```
gcloud services enable aiplatform.googleapis.com --project=YOUR-PROJECT-ID
```

### 3. Enable the Claude model in Model Garden

In the Google Cloud console, Model Garden, find the Claude model and **Enable** it. This
accepts Anthropic's terms through Google and is a separate step from enabling the API.
Availability varies by region, so check the region you intend to use at the same time. See the
warning about regions below, because this is where it bites.

### 4. Grant a role

**Vertex AI User** (`roles/aiplatform.user`) on the project, to whichever identity will make
the call: your own account for a local test, or a service account for Actions.

### 5. Get a token

Vertex authenticates with a Google OAuth bearer token, not a long-lived API key.

For a one-off call as yourself:

```
gcloud auth application-default login
gcloud auth print-access-token
```

Put the result in `.env`:

```
GOOGLE_VERTEX_ACCESS_TOKEN=ya29....
```

**That token expires in about an hour.** See the second warning below.

### 6. Fill in the provider entry

Already filled in, and worth knowing why it says what it says:

```yaml
  vertex:
    kind: gcp_vertex
    base_url: https://aiplatform.googleapis.com
    region: global
    project: gen-lang-client-0915051085
```

**`global`, because Canada is not on offer.** See the region finding below. The four
alternatives each cost 10 percent more and none of them is Canadian, so there is no residency
here worth paying a premium to preserve.

The host and the region must name the same place, and the adapter refuses the request if they
do not rather than sending data to the wrong geography. So both lines change together. For the
US multi-region, which is the one residency lever on offer:

```yaml
    base_url: https://aiplatform.us.rep.googleapis.com
    region: us
```

**The model id carries an `@`**: `claude-haiku-4-5@20251001`, which the Model Garden page calls
the Version name. It goes into the URL unencoded, because `@` is a legal path character and
Google's own example shows it literally. This library percent-encoded it until 2026-09-16, which
would have come back as a model-not-found on the first call and sent you looking at the model
name, the Model Garden enablement and the region, none of which would have been wrong.

### 7. Write the price file before the call, or accept an uncosted row

There are **no Vertex rates in the price file**, on purpose: Google sets its own prices for
Claude and publishes them itself, and a rate nobody has read is a rate nobody may write. So a
Vertex call today writes an **uncosted** row.

When the project exists, read the Claude rates from
<https://cloud.google.com/vertex-ai/generative-ai/pricing> (the Claude models section) or from
your own billing console, and write them into a new file dated that day, in
`boundary/prices/`. Copy `2026-09-14.yaml` and add a `vertex:` block.

Remember the endpoint premium when you do: **regional and multi-region endpoints cost 10
percent more than global** for Sonnet 4.5, Haiku 4.5, Opus 4.5 and later. Montreal is not the
global rate, so it is its own entry and not a copy of one.

### 8. Make the call, not from this laptop

```
boundary smoke vertex
```

---

## Three things worth reading before you build anything

Two of them are warnings about what these platforms will and will not promise you. The
third used to be a warning and is now a note about a thing that works.

### What "Canadian residency" can and cannot mean

Tried on 2026-09-14, and the answer is narrower than the phrase suggests and narrower again
than this document first claimed.

**On Foundry, for Claude, there is no Canadian option at all.** A Foundry resource created in
Canada Central offers no Anthropic model to deploy. That is not a quirk of one subscription:
Microsoft's own region table for partner models shows `-` against `canadacentral` and
`canadaeast` for every `claude-*` row, in both hosting versions
([Region availability by deployment type](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-from-partners#region-availability-by-deployment-type),
read 2026-09-14). The partner models that *are* offered in the Canadian regions are Mistral,
Codestral, Ministral and Llama-4-Scout, none of which speak the Anthropic Messages shape this
adapter is built for.

Where Claude can be deployed on Global Standard in the Americas: `centralus`, `eastus`,
`eastus2`, `northcentralus`, `southcentralus`, `westcentralus`, `westus`, `westus3`. Not
`canadacentral`, not `canadaeast`, not `brazilsouth`, not `westus2`. In Europe the single
region is `swedencentral`.

And the narrower deployment type does not help: **Data Zone Standard exists only for the
United States.** Europe and Asia Pacific both read "Not available", and there is no Canadian
data zone to ask for. So Foundry offers a Canadian buyer exactly two choices for Claude,
global routing or the US data zone, and neither is Canada.

**The general shape, across platforms.** Deployment region and processing region are
different things and they fail differently:

| | Deployment in Canada | Processing guaranteed in Canada |
|---|---|---|
| Foundry, Claude, Hosted on Azure (v2) | **not offered** | inside Azure, but not inside Canada |
| Foundry, Claude, Hosted on Anthropic (v1) | **not offered** | no, it runs outside Azure |
| Foundry, other partner models | Canada Central and Canada East | no, Global Standard routes anywhere |
| **Bedrock, Claude, geo profile (`us.`)** | **`ca-central-1`, and it works** | **no: may be served from three US regions** |
| **Bedrock, Claude, global profile** | `ca-central-1` | no, routes to any commercial region |
| **Bedrock, Claude, In-Region** | **not offered in Canada** | yes, but only in seven non-Canadian regions |
| **Vertex, Claude** | **not offered** | no, and there is no Canadian multi-region either |

**On Bedrock, Canada is a real region and still not a residency answer.** Verified against the
Claude Haiku 4.5 model card and a live call on 2026-09-15. `ca-central-1` supports Geo and
Global inference profiles and **not** In-Region. The `bedrock-mantle` endpoint, which is the
only route to guaranteed single-region processing, serves Claude in seven regions and none of
them is Canadian: us-east-1, us-east-2, us-west-2, eu-north-1, eu-west-1, ap-northeast-1,
ap-southeast-4.

AWS states the trade-off itself, which is worth quoting because it is the vendor's own wording
rather than this project's reading of it:

> Geo and global inference profiles can route requests outside the source Region and don't
> provide single-Region data residency. For single-Region inference, use the `bedrock-mantle`
> endpoint with the bare model ID.

And then the sharpest thing found on any of the three platforms: **the geography AWS calls
"US" contains Canada.** Calling `us.anthropic.claude-haiku-4-5-20251001-v1:0` from
`ca-central-1` routes to one of `ca-central-1`, `us-east-1`, `us-east-2` or `us-west-2`. So the
profile named after one country is a four-region pool spanning two, and a buyer who reads "US
geo" as "United States only" has it backwards in both directions at once: it is not only the
US, and it is not only Canada.

Nothing in the response says which of the four served it. The model identifier comes back with
the routing prefix stripped (`us.anthropic.claude-haiku-4-5-20251001-v1:0` in,
`anthropic.claude-haiku-4-5-20251001-v1:0` out) and no region is reported at all. That is
pinned as a test in `tests/test_bedrock.py`.

**And Vertex has no Canadian region either.** Checked 2026-09-16 in Model Garden, on the
`claude-haiku-4-5` pricing panel, which is the vendor's own location list rather than a
documentation table. The complete set of locations offered:

```
asia-east1 · europe-west1 · global · us · us-east5
```

Five, and none Canadian. No `northamerica-northeast1`, no `northamerica-northeast2`, and the
multi-region identifiers are `us` and `eu` only, so there is no Canadian multi-region to ask
for. An earlier version of this document said Montreal existed for older models; the console
says otherwise for the model anyone would actually deploy, and the console wins.

The four non-global options each carry a documented 10 percent premium. So the only residency
lever Vertex sells a Canadian buyer is **United States** residency, at a 10 percent premium,
which is a real product aimed at the wrong country.

So all three platforms hide the same thing in three different places: Foundry hides the hosting
version, Vertex hides the processing location, Bedrock hides the routing profile. On each of
them, residency is a configuration fact or it is not a fact. That is why `residency` is a
declared field on a provider entry rather than something parsed from a response, and why the
Bedrock adapter refuses a configuration whose model identifier and declaration disagree.

Even where a Canadian deployment exists, it is commonly still served by a global deployment,
so the tokens may be processed elsewhere. Dedicated, guaranteed in-country processing is the
rare and expensive exception, and for Claude on Foundry it is not on the menu at any price.

Microsoft states the distinction plainly in the deployment dialog itself, and it is worth
quoting because it is the vendor's own wording rather than this project's reading of it:

> Global Standard: Pay per API call with the highest rate limits. Data might be processed
> globally, outside the resource's Azure geography, but data storage remains in the AI
> resource's Azure geography.

**Processing is global; storage is regional.** A residency requirement that is really about
where data is stored can be met. One about where it is processed cannot, on this deployment
type. Those are different obligations and they are commonly written as if they were one, which
is exactly where a compliance product earns its keep or fails quietly.

There is a second gate behind that one, and it sits earlier than expected: on a Free Trial or
credits-only subscription the quota for every Claude model is zero, so neither hosting option
is reachable and the residency question never arises. See step 3a. The choice between keeping
processing inside Azure and sending it to Anthropic only becomes available once the
subscription is pay-as-you-go, at which point both are.

So the claim this gateway can support is, at most, **"deployed in Canada"**, and for some
platform and model pairs not even that. It is never **"processed only in Canada"**. Those are
different claims and a compliance product that let a reader slide from the first to the second
would be doing the thing this project exists to stop.

The practical consequences, which Part B is written against:

- `region` in the ledger is **where the request was sent**. It is the thing the gateway
  controls and the thing a routing policy can enforce. It is not evidence of where inference
  ran, and nothing in this repository should be read as claiming it is.
- A residency policy that fails closed will, on real vendor availability, sometimes refuse the
  work rather than find a compliant route. That is the correct behaviour and it is also the
  honest finding: for a Canadian buyer wanting Claude on Azure today, the compliant set is
  empty. Part B's Canadian worked example has to show that outcome rather than design around
  it.
- Where a platform does offer a narrower inference geography it is a separate, priced thing
  and worth taking. Foundry's US Data Zone keeps inference in the United States at 1.1x, the
  same lever as `inference_geo: "us"` on the Claude API. Its Canadian counterpart does not
  exist, and that absence is the measurement.

For the smoke call, then: create the Foundry resource in a region that actually serves Claude.
`eastus2` has the widest coverage; `eastus` or `centralus` will serve `claude-haiku-4-5`,
which is the cheapest way to prove the path. Keep the Canada Central resource if you like. It
costs nothing idle and it is the evidence.

### What a new account is actually allocated

Both platforms publish a default quota, and on both of them a new account gets something
else. The shapes of the difference are not the same, and the difference between the shapes is
the procurement finding.

Measured 2026-09-15 and 2026-09-16, from the accounts rather than from documentation:

| | Published default | Actually allocated | Callable on day one | Route to more |
|---|---|---|---|---|
| **Foundry**, `claude-haiku-4-5`, `eastus2` | 80 RPM | **0** | **No** | A form, evaluated individually, not guaranteed |
| **Bedrock**, Claude Haiku 4.5, `ca-central-1` | 10,000 RPM | **10** | **Yes** | Standard quota increase request |
| **Vertex**, `claude-haiku-4-5`, `global` | not published per model | **0 usable** | **No** | Quota increase request |

**All three.** Three vendors, three brand new accounts, three refusals or near-refusals of
Claude, and on none of them does the published figure describe what a new customer receives.
That is not a coincidence about this portfolio's luck. It is what partner-operated frontier
models look like to a new buyer, and it is invisible from the outside because every vendor's
documentation describes the steady state.

The shapes differ, and the difference is what a buyer actually needs to know:

- **Foundry is a wall.** Zero for every Claude model in every region that offers them, and the
  only way through is a form with no published lead time and no guarantee.
- **Bedrock is a throttle.** Ten requests a minute against a published ten thousand, which is
  enough to prove a path in an afternoon and not enough to run a panel. You can start.
- **Vertex is a wall that looks like a throttle.** The first request ever made on the account
  returned 429 `Quota exceeded for global_online_prediction_requests_per_base_model`. Not
  rate limiting, because there was no rate: one request, from a project whose total lifetime
  Claude usage was zero.

Vertex's is the most misleading of the three. A 429 is the status code for "slow down", every
sensible client retries it, and this library did: four attempts, three retries, all refused.
A team seeing that in a load test would tune concurrency, add backoff, and conclude their
client was too aggressive, when the allocation is zero and no amount of slowing down reaches
it. Azure at least says "Insufficient quota" in words.

**What this costs a project that does not know.** Between them these three platforms cost this
one about two days, spread across three wrong diagnoses on Azure, a spelling mistake in a quota
filter on AWS, and a 429 on Google that means something other than what 429 means everywhere
else. None of that time was spent on anything a reader of the vendors' documentation could have
anticipated. A procurement note for any of these platforms should carry one line: **budget for
a quota grant with an unknown lead time before the first call, on all three.**

Reproduce the Vertex half by making one call, which is the point: there is no quota page to
read first, and the first request is the diagnostic.

```
gcloud alpha services quota list \
  --service=aiplatform.googleapis.com --consumer=projects/YOUR-PROJECT-ID \
  --filter="global_online_prediction_requests_per_base_model"
```

The increase request is at
[Quotas and system limits](https://cloud.google.com/vertex-ai/docs/generative-ai/quotas-genai),
which is the URL the 429 body itself names.

Reproduce the Bedrock half in one command:

```
aws service-quotas list-service-quotas --service-code bedrock --region ca-central-1 \
  --query "Quotas[?contains(QuotaName,'Haiku')].{q:QuotaName,v:Value}" --output table
```

against `list-aws-default-service-quotas`, which is a different call and returns the published
defaults. Two details in the comparison are worth more than the headline:

- **Only the request rate is reduced. Token throughput is untouched.** Cross-region tokens per
  minute is 5,000,000 on this account and 5,000,000 by default. So a new account may push five
  million tokens a minute across ten requests, which is a fraud-and-abuse posture rather than a
  capacity one, and it means the constraint binds hardest on exactly the workload this
  portfolio runs: many small calls.
- **The reduction factor is per model, not per account.** Haiku 4.5 is cut 1000x (10 against
  10,000). Claude 3 Haiku is cut 100x (8 against 800 cross-region, 4 against 400 on-demand).
  There is no single new-account multiplier to reason about.

**What it means for scheduling.** 10 requests per minute is fine for a smoke call and a costed
row. It is not fine for a panel: 03's first drift run was 16,800 calls, which at this
allocation is 28 hours. Anything larger than a smoke test on Bedrock needs the quota increase
requested first, and that is a lead time to put in a plan rather than discover in a run.

### The Vertex token expires hourly, and now something refreshes it

`gcloud auth print-access-token` gives you a token that lasts about an hour. That is fine for
one smoke call and useless for a drift run, and it was the last code gap in the three
hyperscaler adapters. **Built 2026-09-15** (`boundary/credentials.py`).

Two ways to give Vertex a credential, and one checked-in provider entry serves both:

| | What you do | What happens |
|---|---|---|
| **Laptop** | `gcloud auth application-default login`, and leave `GOOGLE_VERTEX_ACCESS_TOKEN` unset | A token is minted from Application Default Credentials and refreshed five minutes before it expires |
| **CI** | Set `GOOGLE_VERTEX_ACCESS_TOKEN` to a token an earlier step minted | That token is used as-is; nothing is minted, and the runner needs no `gcloud` and no optional dependency |

The precedence is "a set variable wins", documented on the `credentials` field rather than
left to be discovered. Setting the variable is how an operator says "use this one"; leaving
it unset is how a laptop says "mint it".

Minting needs the optional dependency:

```
uv sync --extra vertex      # or: pip install 'boundary[vertex]'
```

It is optional on purpose. Projects 02 and 03 call Anthropic, OpenAI, Google and an
open-weights host, and install none of it.

**One design note worth keeping**, because it is the kind of thing that only bites on this
laptop. `google-auth` ships transports built on `requests` and on `urllib3`, and both verify
TLS against a bundled certificate list. This library verifies against the operating system's
trust store instead, so that the work network's inspecting proxy is trusted the same way the
browser trusts it. Using google-auth's own transport would mean the token call and the vendor
call trusted different certificates: the token mint would fail on the work network while every
vendor call succeeded, and the error would read like bad credentials rather than a trust
problem. So the token is fetched through this library's own pinned client, which is about
fifteen lines and removes a dependency rather than adding one.

The adapter is unchanged and still pure: it receives a token string and knows nothing about
where it came from, so it is still testable against goldens with no credentials and no
network.

---

## What lands in the ledger either way

All three adapters record `region` on every row, so "which region was this request sent to" is
a query against the ledger rather than a promise. That is the point of doing this before Part B
rather than during it. One of the three has actually written such a row: Bedrock, from
`ca-central-1`, on 2026-09-15.

Bedrock adds a second column's worth of meaning, because it is the platform where the
configuration carries information the wire does not. `residency` on the provider entry says
whether the request was allowed to leave its region, and the adapter refuses to send anything
whose model identifier contradicts it. That is not a stronger claim than the vendors support;
it is the same weak claim, written down where it can be audited instead of assumed.

It is worth being precise about what that column is worth. It records where the request was
**sent**, which is the thing the gateway controls and the thing a routing policy can enforce.
It does not record where the tokens were **processed**, which the vendor controls and mostly
does not guarantee. The ledger should never be read as evidence of the second, and Part B's
residency claim is written against the first.
