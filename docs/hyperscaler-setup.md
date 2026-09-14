# Setting up Microsoft Foundry and Google Vertex

The two adapters are written and tested. Neither has been called, because neither account
exists. This is what has to be created, where, and in what order, and what to bring back into
this repository afterwards.

Read the two warnings at the bottom before you start. The first one decides which Azure region
you create the Foundry resource in, and a Canadian one will not work, so read it first.

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

### 3a. If the deployment is refused for quota, check the subscription type

This is the wall, and it is not where it looks like it is.

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

**The cause here is not yet established, and this section has been wrong twice.** The
observation is: a **pay-as-you-go** subscription, a resource in `eastus2`, `claude-haiku-4-5`
on Global Standard, both offered model versions (`2` and `20251011`) refused with
"Insufficient quota". That contradicts the table directly, which gives pay-as-you-go 80 RPM
for exactly that model and deployment type on either version.

Two explanations were written here and both were wrong: that the Azure-hosted version was
rationed while the Anthropic-hosted one was not (the table marks both allocatable with the
same default), and that the subscription must be a Free Trial (it is not). Both were inferred
from the error message. Neither was checked against the number the portal will simply show
you. So the rest of this section is the diagnosis rather than an answer.

**Read the actual allocation first.** The Quota page in the Foundry portal, or the "Manage
quota" button in the deployment dialog, shows the quota this subscription really has per model
and deployment type. Everything below depends on what that number is, and nothing should be
concluded before reading it.

- **If it shows the documented default (80 RPM for haiku)** then the deployment is failing for
  a reason the message misattributes, and the next thing to check is the project rather than
  the subscription. The dialog says "cannot be deployed to your current **project**" and
  offers "Select another project" as its first remedy. A Foundry project belongs to a region,
  and a project left in Canada Central would refuse every Claude model, because none is offered
  there. Confirm which region the *selected project* is in, not which region was being browsed.
- **If it shows zero** then the documented default has not been applied to this subscription,
  which is a quota request rather than a configuration problem. The
  [request form](https://aka.ms/oai/stuquotarequest) is the route, and requests are "evaluated
  individually and aren't guaranteed to be approved".
- **Either way, do not change the region to work around it.** Quota is allocated per
  subscription and shared across regions for Global Standard, so moving the resource cannot
  change the number.

Worth also confirming once, because both are prerequisites that fail late rather than early:
the Azure Marketplace terms were accepted on the first Claude deployment, and the
`Microsoft.SaaS` resource provider is registered on the subscription.

**Why this is in a compliance project's notes at all.** The failure presents as a
per-deployment capacity message. That reads like a transient regional shortage and invites
changing the region, the model or the version, and on this platform quota is per subscription,
so none of those can move it. Whatever the cause turns out to be here, the error points away
from it. That is a procurement observation worth having before a pilot is scheduled around
this platform, and it is the part of this section that has survived being wrong twice.

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

## Google Vertex

You already have a Google Cloud project with billing linked (the Gemini key's project, moved
off the free tier on 2026-09-10) and a US$15 a month budget with alerts. Vertex needs the API
enabled, the partner model enabled, a role, and a token.

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

In `config/boundary.yaml`:

```yaml
  vertex:
    kind: gcp_vertex
    base_url: https://northamerica-northeast1-aiplatform.googleapis.com
    region: northamerica-northeast1
    project: YOUR-PROJECT-ID
```

The host and the region must name the same place. The adapter refuses the request if they do
not, rather than sending data to the wrong geography. To use the global endpoint instead, both
lines change together:

```yaml
    base_url: https://aiplatform.googleapis.com
    region: global
```

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

## Two warnings, both worth reading before you build anything

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
| Vertex, Claude | `northamerica-northeast1` exists, older models only | no |

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

### The Vertex token expires hourly, and nothing here refreshes it

`GOOGLE_VERTEX_ACCESS_TOKEN` is read from the environment like any other key. A
`gcloud auth print-access-token` token lasts about an hour, which is fine for one smoke call
and useless for a drift run.

The plan (section 5.1, item 8) says the token comes from `google-auth`, in an optional
dependency group, used for the token only. **That part is not built.** The adapter takes a
token and stays pure, which is the right shape for it, but the minting layer that would call
`google.auth.default()` and refresh on expiry does not exist yet.

So: one smoke call works today with a pasted token. Anything longer needs the google-auth
credential layer written first. It is perhaps half a day, and nothing needs it before the
smoke call proves the endpoint shape is right.

---

## What lands in the ledger either way

Both adapters record `region` on every row, so after the two smoke calls "which region was
this request sent to" is a query against the ledger rather than a promise. That is the point of
doing this before Part B rather than during it.

It is worth being precise about what that column is worth. It records where the request was
**sent**, which is the thing the gateway controls and the thing a routing policy can enforce.
It does not record where the tokens were **processed**, which the vendor controls and mostly
does not guarantee. The ledger should never be read as evidence of the second, and Part B's
residency claim is written against the first.
