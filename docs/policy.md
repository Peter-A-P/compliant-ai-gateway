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
violation or false refusal, so it can run in CI.

## What it does not do

- **It enforces the declaration, not the vendor.** A clean run means every call went to an
  endpoint whose declared residency fits its class. No vendor reports where a request was
  processed, so nothing here can say the vendor kept to it.
- **It does not check redaction.** B2.2 has `personal` require redaction. The library cannot
  tell a redacted body from a raw one without reading content, which it does not do; Part B's
  proxy redacts as the policy requires and then passes the call on.
- **It is opt-in for a library caller**, because turning it on for the pinned downstream
  runs would change what those runs measure. Through the proxy (0.13) it is always on.
