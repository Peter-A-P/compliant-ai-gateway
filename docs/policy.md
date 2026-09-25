# The data policy

`boundary.enforce` (0.12) decides which provider a call may reach from the class of data its
caller declared. It is the library half of PLAN.md B2.2, pulled forward from Part B because
it needs no proxy: every call already declares a class (0.4) and every provider entry its
residency (0.2.1). Until 0.12 both were recorded and nothing acted on them.

**Opt-in.** A gateway enforces only when it is given a policy, by `policy: policy.yaml` in
`boundary.yaml` or `Gateway(..., policy=load_policy(path))`. The checked-in `boundary.yaml`
names none, so the pinned 02 and 03 runs, the smoke workflow and pass-through behave exactly
as before. The proxy (0.13, docs/server.md) turns it on for every request, refuses to start
without one, and substitutes `personal` for an absent `X-Data-Class` header at the door.

## The file

[config/policy.yaml](../config/policy.yaml), one rule per class:

| Key | Meaning |
|---|---|
| `undeclared` | The class a call that declared nothing is judged as. `personal`: absent means personal |
| `max_residency` | The widest residency the class tolerates: `single-region`, `geo` or `global` |
| `regions` | The regions a call may be **sent** to. Compared without regard to case |
| `providers` | The provider entries the class may use |
| `cache` | Whether the development cache may store and serve these calls. Off unless said |
| `redacted_as` | The class a call of this class is judged as once redacted (0.15). Must name a listed class that has none of its own. The proxy redacts every request of such a class; a library caller gets it only by passing `redacted=True` |

## Every rule fails closed

- A call with no class is judged as `undeclared`, and the ledger row still records that the
  caller declared nothing, so `ledger report --data-class undeclared` finds it.
- A class the policy does not list is refused, not let through on a default.
- A provider entry that declares no residency fails any residency limit, including
  `global`, because null is no claim rather than the weakest claim. So does a residency value
  a later version added, which this version cannot place.
- A region limit refuses a call whose region is unset.
- A malformed class (`PERSONAL`, ` personal`, `secret`) is refused before the policy is
  consulted, by the closed vocabulary that has been in place since 0.4.

## A refusal

Raises `PolicyRefused` with the class it was judged as, the provider, the reason and a
ledger row id. **Nothing is sent, no key is read and no body is built**, because the check
runs straight after the model is resolved. The row is written and completed at once with
`error_type = 'policy_refused'` and a cost of zero, so an attempted violation is on the
record and not only in a caller's exception log; a refused batch writes one row per request.
Every entry point is held to it: `chat` in both modes, the streaming calls, `batch_submit`
and `raw`.

## The worked example: the compliant set is one provider

The checked-in policy lets personal data go only where single-region processing in a
Canadian region can be declared. On this configuration that is the local model server and
nothing else: Bedrock in `ca-central-1` is `geo` (Canada or the United States, unspecified),
the Canada Central Foundry deployment is `global`, and Vertex has no Canadian region for
Claude. A test asserts the set is exactly `["local"]`, so the day a hosted vendor offers
something narrower is a failing test rather than an unnoticed change. PLAN.md B2.2 asked for
the refusal to be shown rather than designed around, and this is it enforced rather than
described.

**Redacted, personal data is judged as internal (0.15, decided by Peter on 2026-09-25).**
The personal rule names `redacted_as: internal`, so a personal call whose payload was
redacted is judged by the internal rule instead: any declared residency. On this
configuration that opens the Canada Central Foundry deployment, Bedrock in `ca-central-1`
and Vertex beside the local model, and still refuses the direct Anthropic, OpenAI, Google and
Together entries, because they declare no residency and null is no claim. A test asserts
both sets, raw and redacted. `sensitive` names no `redacted_as`, so redaction unlocks nothing
for it. The cache stays closed to a redacted personal call because it needs both rules to
allow it, and the personal rule does not.

The library cannot tell a redacted body from a raw one, so for a library caller `redacted`
is a statement, like `data_class`, and is recorded as one: a row carries `data_class =
personal` and `redacted = 1`. The proxy makes the statement only when it redacted the
payload itself and the guard vouched for it (docs/server.md).

## Measured: the adversarial suite

`boundary policy eval` builds its cases from the configuration: every provider entry by its
explicit name and every alias, which is how a call arrives at a provider it never named;
the four classes, no class, and five malformed spellings; and all five entry points. Each
case goes through a real gateway against an in-process upstream that counts what reaches it,
and the expected answer comes from an oracle that reads the policy file as plain YAML and
applies the rules with set arithmetic, sharing no code with `boundary.enforce`. It is written
by the same hand, which is the limit of an independent check inside one repository.

At 0.12.0: **650 cases, 550 forbidden, none sent** (0.0%, 0.0 to 0.7), **no false refusal**
among the 100 allowed, and every policy refusal on the ledger. Of the 550 forbidden, 325
were stopped by the closed vocabulary (the malformed classes), 209 by the policy, and 16 by
pass-through refusing an alias before the policy was asked. The suite exits 1 on any
violation or false refusal, and it runs in CI on every push.

**Through the proxy, since 0.13.1: `boundary policy eval --proxy`.** The same targets over
HTTP through `boundary serve` (docs/server.md), where the class is an `X-Data-Class` header
and the door has rules of its own: absent or blank is `personal`, case and surrounding space
are forgiven, any other word is a 400. The oracle models those rules separately (`door` in
`boundary.enforce_eval`), so a proxy that stopped failing closed would show up as
violations, and a test makes it do exactly that to prove the suite notices. Thirteen header
values and two entry points, a plain call and a stream: **338 cases, 262 forbidden, none
sent** (0.0%, 0.0 to 1.4), **no false refusal** among the 76 allowed, every allowed case
reached the upstream, and all 210 policy refusals are on the ledger. The other 52 forbidden
cases are the two refused words, stopped with a 400 before the policy was asked.

At 0.15, with redaction, the proxy's oracle also routes a class naming a `redacted_as` as
that class, which is what the proxy does: **338 cases, 220 forbidden, none sent** (0.0 to
1.7), no false refusal among the 118 allowed, every refusal on the ledger. The 42 cases that
moved from forbidden to allowed are the seven header values judged `personal`, on the three
hosted entries that declare a residency, through both entry points.

## What it does not do

- **It enforces the declaration, not the vendor.** A clean run means every call went to an
  endpoint whose declared residency fits its class. No vendor reports where a request was
  processed, so nothing here can say the vendor kept to it.
- **It does not check redaction.** The library cannot tell a redacted body from a raw one
  without reading content, which it does not do, so `redacted=True` is taken on the caller's
  word and recorded as such. The proxy (0.15) redacts every request of a class naming a
  `redacted_as` itself, and passes `redacted=True` only after its guard vouched for the
  result.
- **It is opt-in for a library caller**, because turning it on for the pinned downstream
  runs would change what those runs measure. Through the proxy (0.13) it is always on.
