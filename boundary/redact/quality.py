"""The quality cost of redaction, measured through project 03's gate (0.20, PLAN.md B2.8).

Three measurements, each answering one part of "does redaction hurt the answers?":

- **Part 1, over-masking public context.** 03's 100 gold questions, answered from their
  regulator pages by three models under three arms: `raw`; `redacted`, the whole request as
  the proxy redacts it today (rules only, the preserve line added, the answer rehydrated);
  and `redacted-allow`, the same with an allow list derived beforehand from 48 other
  regulator pages (`derive_allow`). The pages hold no personal data, so any difference is the
  cost of masking context an answer needs.
- **Part 2, removing personal data.** The same questions rewritten by `personalise` in the
  voice of a customer who gives a synthetic name, email address, phone number and account
  number that the answer does not need, under `personal-raw` and `personal-redacted-allow`.
  A perfect redactor costs nothing here, so a difference is the price of the placeholders.
- **The red team.** 03's `pii_leakage` suite (200 synthetic customer records, each behind an
  attack), `raw` against `redacted`, graded by 03's `withholds_pii` on three readings of the
  redacted arm: what was sent, what the model wrote, and what the client received after
  rehydration.

**The instrument is 03's, unmodified.** The answer prompt, the judge prompt, the verdict
parser, the judge's calibration counts, the paired bootstrap and the red-team grader are all
03's code, run in 03's own environment by `bench/quality/gate_bridge.py`, because 03 pins
boundary 0.1.0 and its modules cannot be imported here. What is 04's is every vendor call:
through this repository's gateway and ledger, standard mode, one sample per item per arm at
03's temperature (0). The judge is `google-judge-mid` (Gemini 3.8 Flash), licensed by 03 for
completeness only (kappa 0.914), with 03's output budget and thinking level; it shares no
vendor with the answering panel, 03's own rule, which is why the panel's third model is
OpenAI's rather than the Gemini the mutation rate was measured on.

Everything is stored, answers and verdicts, so `report` re-reads a run for nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import random
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from boundary.redact.corpus import _person
from boundary.redact.evaluate import Rate
from boundary.redact.policy import PLACEHOLDER, RedactionRefused, placeholder_kind
from boundary.redact.request import redact_request, survives
from boundary.types import ChatRequest

# The answering panel: 03's gold-set models where they can be used, by 03's arm keys. Haiku
# 4.5 and Llama 3.3 70B wrote 03's gold answers; gpt-5.4-mini is 03's red-team OpenAI arm,
# with the same `reasoning_effort` 03 sends it.
PANEL: dict[str, tuple[str, dict[str, Any]]] = {
    "anthropic-mid": ("anthropic/claude-haiku-4-5-20251001", {}),
    "openweights-weakest": ("openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo", {}),
    "openai-snapshot": ("openai/gpt-5.4-mini-2026-03-17", {"reasoning_effort": "none"}),
}
# 03's licensed completeness judge, as 03's gold-judges.yaml and its calibration run it.
JUDGE: dict[str, Any] = {
    "key": "google-judge-mid",
    "provider": "google",
    "model": "gemini-3.8-flash",
    "temperature": 0.0,
    "max_tokens": 1024,
}
JUDGE_EXTRA: dict[str, Any] = {"generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}}

PARTS: dict[str, tuple[str, ...]] = {
    "public": ("raw", "redacted", "redacted-allow"),
    "personal": ("personal-raw", "personal-redacted-allow"),
    "redteam": ("raw", "redacted"),
}
SEED = 20260927
PURPOSE = "redaction quality"

# The comparisons reported, candidate against baseline, per part.
COMPARISONS: dict[str, tuple[tuple[str, str], ...]] = {
    "public": (("redacted", "raw"), ("redacted-allow", "raw"), ("redacted-allow", "redacted")),
    "personal": (("personal-redacted-allow", "personal-raw"), ("personal-raw", "raw")),
}

# Where 03 keeps what this reads. Relative to the 03 checkout.
GOLD = "gate/gold"


# -- the allow list -------------------------------------------------------------------------

# What an allow term may come from: the second pass's own types and the two a caller's list
# may release. Never a person, an address or an identifier, whatever the pages hold.
_ALLOW_KINDS = frozenset({"NAME_LIKE", "ORGANISATION", "LOCATION"})
_WORD = re.compile(r"[^\W\d_][\w'-]*", re.UNICODE)


def _masked_values(page: str) -> list[tuple[str, str]]:
    """(kind, value) for everything the proxy's redaction masks in `page`, whole spans only."""
    red = redact_request(ChatRequest(model="m", messages=[{"role": "user", "content": page}]))
    out = []
    for placeholder, value in red.policy.vault.items():
        m = PLACEHOLDER.fullmatch(placeholder)
        if m is None or "." in placeholder:
            continue
        out.append((placeholder_kind(m)[0], value))
    return out


def derive_allow(pages: Iterable[str], *, min_pages: int = 2) -> list[str]:
    """The allow list, read mechanically off pages that hold no personal data.

    Every span the proxy masks on such a page is over-masking by construction. A **phrase**
    (the whole masked span) or a **word** inside one enters the list when it is masked on at
    least `min_pages` distinct pages, so a term one page happens to capitalise does not; and
    only from the second pass's types and the two a list may release (`_ALLOW_KINDS`), never
    from a span with a digit in it, and never a single letter. Fixed before any answer was generated, and never run on
    the gold pages: a list tuned on the test pages would measure the tuning.
    """
    phrase_pages: dict[str, set[int]] = {}
    word_pages: dict[str, set[int]] = {}
    for i, page in enumerate(pages):
        for kind, value in _masked_values(page):
            if kind not in _ALLOW_KINDS or any(c.isdigit() for c in value):
                continue
            phrase = " ".join(value.split())
            phrase_pages.setdefault(phrase, set()).add(i)
            for w in _WORD.findall(phrase):
                if w[0].isupper():
                    word_pages.setdefault(w, set()).add(i)
    terms = {p for p, s in phrase_pages.items() if len(s) >= min_pages}
    terms |= {w for w, s in word_pages.items() if len(s) >= min_pages}
    # A single letter is an initial as often as it is a word ("Revenue Service's" gave "S").
    terms = {t for t in terms if len(t) > 1}
    return sorted(terms, key=lambda t: (t.casefold(), t))


# -- the personalised variant ---------------------------------------------------------------

# Where the customer's details go. Four shapes, so that neither the model nor the redactor
# sees one fixed preamble a hundred times.
_TEMPLATES = (
    "Hi, my name is {name} and my account number is {account}. {question} You can reach me "
    "at {email} or {phone}.",
    "{question}\n\nThanks,\n{name}\n{email} | {phone}\nAccount {account}",
    "This is {name} (account {account}). {question} Please reply to {email}; my phone number "
    "is {phone}.",
    "Hello, {name} here, calling from {phone}. {question} My email is {email} and the "
    "account is {account}.",
)


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    email: str
    phone: str
    account: str

    @property
    def values(self) -> tuple[str, ...]:
        return (self.name, self.email, self.phone, self.account)


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _persona(rng: random.Random) -> Persona:
    name, _shape = _person(rng)
    local = re.sub(r"[^a-z.]", "", _ascii(name).lower().replace(" ", ".").replace("-", ""))
    return Persona(
        name=name,
        email=f"{local}{rng.randrange(10, 100)}@example.com",
        # 555-01xx is the North American range reserved for fiction.
        phone=f"{rng.choice(('709', '416', '604', '902'))}-555-01{rng.randrange(0, 100):02d}",
        account=(
            f"{rng.randrange(10000, 100000)}-{rng.randrange(1, 1000):03d}-"
            f"{rng.randrange(1000000, 10000000)}"
        ),
    )


def personalise(
    questions: Mapping[str, str], *, seed: int = SEED
) -> dict[str, tuple[str, Persona]]:
    """Each question in the voice of a customer who gives details the answer does not need.

    Deterministic in `seed` and in the question ids, so the variant can be rebuilt from the
    repository. Every value is synthetic: names from the generated corpus, `example.com`
    addresses, 555-01xx numbers, account numbers drawn at random."""
    rng = random.Random(seed)
    out: dict[str, tuple[str, Persona]] = {}
    for qid in sorted(questions):
        p = _persona(rng)
        template = _TEMPLATES[rng.randrange(len(_TEMPLATES))]
        out[qid] = (
            template.format(
                name=p.name,
                email=p.email,
                phone=p.phone,
                account=p.account,
                question=questions[qid].strip(),
            ),
            p,
        )
    return out


def _addresses(text: str, persona: Persona) -> bool:
    """Whether an answer uses the customer's name: the forename or the whole name."""
    forename = persona.name.split()[0]
    return persona.name in text or re.search(rf"\b{re.escape(forename)}\b", text) is not None


# -- the bridge to 03 -----------------------------------------------------------------------


class Bridge:
    """03's code in 03's environment. `run` is swapped in tests."""

    def __init__(self, gate: Path, script: Path) -> None:
        self.gate = gate.resolve()
        self.script = script.resolve()

    def run(self, command: str, request: Mapping[str, Any]) -> dict[str, Any]:
        python = self.gate / ".venv" / "bin" / "python"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(request, f, ensure_ascii=False)
            path = Path(f.name)
        try:
            done = subprocess.run(
                [str(python), str(self.script), command, str(path)],
                cwd=self.gate,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            path.unlink(missing_ok=True)
        if done.returncode != 0:
            raise RuntimeError(f"gate bridge {command} failed:\n{done.stderr[-2000:]}")
        result: dict[str, Any] = json.loads(done.stdout)
        return result

    @property
    def gold(self) -> str:
        return str(self.gate / GOLD)


def _gold_questions(gate: Path) -> dict[str, dict[str, Any]]:
    path = gate / GOLD / "questions.jsonl"
    return {q["id"]: q for q in (json.loads(x) for x in path.open(encoding="utf-8") if x.strip())}


# -- answers --------------------------------------------------------------------------------


@dataclass
class Answer:
    """One answer, as 04 stores it. `output` is what the client received; for a redacted arm
    `output_model` is what the model wrote, before rehydration."""

    key: str
    part: str
    arm: str
    model_key: str
    model: str
    item_id: str
    status: str
    refused: bool = False
    output: str | None = None
    output_model: str | None = None
    sent_sha256: str | None = None
    placeholders: int = 0
    unresolved: int = 0
    planted: int = 0
    leaked_on_wire: int = 0
    finish_reason: str | None = None
    model_returned: str | None = None
    cost_usd: float | None = None
    call_uid: str | None = None
    generated_utc: str = ""
    # The red team's readings of the redacted arm, graded later: the text that was sent.
    sent_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "200" and self.output is not None


def answer_key(part: str, arm: str, model_key: str, item_id: str) -> str:
    return f"{part}/{arm}/{model_key}/{item_id}"


def read_answers(path: Path) -> dict[str, Answer]:
    if not path.is_file():
        return {}
    out: dict[str, Answer] = {}
    for line in path.open(encoding="utf-8"):
        if line.strip():
            a = Answer(**json.loads(line))
            out[a.key] = a
    return out


def _append(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(asdict(r) if not isinstance(r, dict) else r, ensure_ascii=False))
            f.write("\n")


def _sent_text(request: ChatRequest) -> str:
    return "\n".join([request.system or ""] + [str(m["content"]) for m in request.messages])


@dataclass(frozen=True, slots=True)
class Job:
    part: str
    arm: str
    model_key: str
    item_id: str
    system: str | None
    prompt: str
    max_tokens: int
    temperature: float
    values: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return answer_key(self.part, self.arm, self.model_key, self.item_id)


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _answer(gateway: Any, job: Job, *, allow: Sequence[str], run_id: str) -> Answer:
    from boundary.errors import BoundaryError
    from boundary.gateway import REDACTION_REFUSED

    model, extra = PANEL[job.model_key]
    request = ChatRequest(
        model=model,
        messages=[{"role": "user", "content": job.prompt}],
        system=job.system,
        max_tokens=job.max_tokens,
        temperature=job.temperature,
        extra=extra,
    )
    base = Answer(
        key=job.key,
        part=job.part,
        arm=job.arm,
        model_key=job.model_key,
        model=model,
        item_id=job.item_id,
        status="",
        generated_utc=_utc(),
    )
    redacting = "redacted" in job.arm
    # A redacted arm is personal data under the proxy's policy, which is why it is redacted.
    # A raw arm of public pages is public; a raw arm carrying the synthetic personal details
    # is personal, unredacted, which the proxy would refuse and this counterfactual sends.
    data_class = "personal" if redacting or job.part != "public" else "public"
    policy = None
    if redacting:
        try:
            red = redact_request(request, allow=allow if job.arm.endswith("-allow") else ())
        except RedactionRefused:
            gateway.record_refusal(
                request,
                purpose=PURPOSE,
                run_id=run_id,
                data_class=data_class,
                error_type=REDACTION_REFUSED,
            )
            base.status, base.refused = "422", True
            return base
        request, policy = red.request, red.policy
        base.placeholders = red.placeholders
    sent = _sent_text(request)
    base.sent_sha256 = hashlib.sha256(sent.encode("utf-8")).hexdigest()
    if job.values:
        background = job.prompt
        for v in job.values:
            background = background.replace(v, "\n")
        base.planted = len(job.values)
        base.leaked_on_wire = sum(1 for v in job.values if survives(v, sent, background))
    if job.part == "redteam" and redacting:
        base.sent_text = sent
    try:
        resp = await gateway.achat(
            request, purpose=PURPOSE, run_id=run_id, data_class=data_class, redacted=redacting
        )
    except BoundaryError as e:
        base.status = type(e).__name__
        return base
    base.status = str(resp.status)
    base.finish_reason = resp.finish_reason
    base.model_returned = resp.model_returned
    base.cost_usd = resp.cost_usd
    base.call_uid = resp.call_uid
    text = resp.text
    if policy is not None and text is not None:
        base.output_model = text
        base.output = policy.rehydrate(text)
        base.unresolved = len(policy.unresolved(text))
    else:
        base.output = text
    return base


def plan_jobs(
    part: str,
    bridge: Bridge,
    *,
    models: Sequence[str] = tuple(PANEL),
    seed: int = SEED,
) -> list[Job]:
    """Every answer a part needs, in a fixed order."""
    jobs: list[Job] = []
    if part == "redteam":
        rt = bridge.run("redteam-items", {})
        for item in rt["items"]:
            for model_key in models:
                for arm in PARTS[part]:
                    jobs.append(
                        Job(
                            part,
                            arm,
                            model_key,
                            item["id"],
                            item["system"],
                            item["prompt"],
                            rt["max_tokens"],
                            rt["temperature"],
                        )
                    )
        return jobs
    questions = {q: v["question"] for q, v in _gold_questions(bridge.gate).items()}
    personas: dict[str, tuple[str, Persona]] = {}
    if part == "personal":
        personas = personalise(questions, seed=seed)
        plain = bridge.run("answer-prompts", {"gold": bridge.gold})
        personal = bridge.run(
            "answer-prompts",
            {"gold": bridge.gold, "questions": {q: t for q, (t, _) in personas.items()}},
        )
    else:
        plain = personal = bridge.run("answer-prompts", {"gold": bridge.gold})
    for qid in sorted(questions):
        for model_key in models:
            for arm in PARTS[part]:
                source = personal if arm.startswith("personal") else plain
                p = source["prompts"][qid]
                values = personas[qid][1].values if arm.startswith("personal") else ()
                jobs.append(
                    Job(
                        part,
                        arm,
                        model_key,
                        qid,
                        p["system"],
                        p["prompt"],
                        source["max_tokens"],
                        source["temperature"],
                        values,
                    )
                )
    return jobs


async def collect(
    gateway: Any,
    jobs: Sequence[Job],
    out: Path,
    *,
    allow: Sequence[str],
    run_id: str,
    max_usd: float,
    concurrency: int = 6,
    on_answer: Callable[[Answer], None] | None = None,
) -> tuple[int, float]:
    """Every job not already stored in `out`, appended as it answers, so a stopped run is
    resumed rather than repaid. Stops sending once the run's returned cost passes `max_usd`.
    Returns (answers made, dollars spent)."""
    done = read_answers(out)
    todo = [j for j in jobs if j.key not in done or done[j.key].status not in ("200", "422")]
    spent = 0.0
    made = 0
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(concurrency)

    async def one(job: Job) -> None:
        nonlocal spent, made
        async with gate:
            async with lock:
                if spent >= max_usd:
                    return
            a = await _answer(gateway, job, allow=allow, run_id=run_id)
            async with lock:
                spent += a.cost_usd or 0.0
                made += 1
                _append(out, [a])
                if on_answer is not None:
                    on_answer(a)

    await asyncio.gather(*(one(j) for j in todo))
    return made, spent


# -- judging --------------------------------------------------------------------------------


def instances(answers: Iterable[Answer], gate: Path) -> list[dict[str, Any]]:
    """03's AnswerInstance records for the judge-graded parts, with stable ids by key."""
    questions = _gold_questions(gate)
    graded = sorted(
        (a for a in answers if a.part in COMPARISONS and a.ok),
        key=lambda a: a.key,
    )
    out = []
    for n, a in enumerate(graded, start=1):
        text = a.output or ""
        out.append(
            {
                "id": f"i-{n:04d}",
                "question_id": a.item_id,
                "source_id": questions[a.item_id]["source_id"],
                "arm_key": f"{a.part}/{a.arm}/{a.model_key}",
                "model_requested": a.model,
                "model_returned": a.model_returned,
                "output": text,
                "output_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "finish_reason": a.finish_reason,
                "generated_utc": a.generated_utc,
                "run_id": f"quality-{a.part}",
                "ledger_id": None,
                "cost_usd": a.cost_usd,
            }
        )
    return out


def read_verdicts(path: Path) -> dict[str, dict[str, Any]]:
    """Stored verdicts by the answer key they judge (03's record plus `answer_key`)."""
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.open(encoding="utf-8"):
        if line.strip():
            v = json.loads(line)
            out[v["answer_key"]] = v
    return out


async def judge(
    gateway: Any,
    bridge: Bridge,
    answers: Mapping[str, Answer],
    out: Path,
    *,
    part: str,
    run_id: str,
    max_usd: float,
    concurrency: int = 6,
) -> tuple[int, float]:
    """03's judge on every stored answer of `part` not yet judged, appended to `out` as 03's
    verdict record with the answer key beside it."""
    from boundary.errors import BoundaryError

    insts = {
        i["arm_key"] + "/" + i["question_id"]: i for i in instances(answers.values(), bridge.gate)
    }
    done = read_verdicts(out)
    todo = [
        (k, i)
        for k, i in insts.items()
        if k.startswith(part + "/") and (k not in done or done[k]["complete"] is None)
    ]
    if not todo:
        return 0, 0.0
    prompts = bridge.run("judge-prompts", {"gold": bridge.gold, "instances": [i for _, i in todo]})
    spent = 0.0
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(concurrency)
    replies: list[dict[str, Any]] = []

    async def one(key: str, inst: dict[str, Any]) -> None:
        nonlocal spent
        async with gate:
            async with lock:
                if spent >= max_usd:
                    return
            prompt = prompts["prompts"][inst["id"]]
            request = ChatRequest(
                model=f"{JUDGE['provider']}/{JUDGE['model']}",
                system=prompts["system"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=int(JUDGE["max_tokens"]),
                temperature=float(JUDGE["temperature"]),
                extra=JUDGE_EXTRA,
            )
            started = asyncio.get_running_loop().time()
            try:
                resp = await gateway.achat(
                    request, purpose="redaction quality judge", run_id=run_id, data_class="public"
                )
                text, returned, finish, cost = (
                    resp.text,
                    resp.model_returned,
                    resp.finish_reason,
                    resp.cost_usd,
                )
            except BoundaryError:
                text, returned, finish, cost = None, None, None, None
            elapsed = (asyncio.get_running_loop().time() - started) * 1000.0
            async with lock:
                spent += cost or 0.0
                replies.append(
                    {
                        "key": key,
                        "instance": inst,
                        "text": text,
                        "prompt": prompt,
                        "model_returned": returned,
                        "finish_reason": finish,
                        "latency_ms": elapsed,
                        "cost_usd": cost,
                        "judged_utc": _utc(),
                    }
                )

    await asyncio.gather(*(one(k, i) for k, i in todo))
    got = bridge.run(
        "verdicts",
        {"judge": JUDGE, "replies": [{k: v for k, v in r.items() if k != "key"} for r in replies]},
    )
    rows = []
    for r, v in zip(replies, got["verdicts"], strict=True):
        rows.append({**v, "answer_key": r["key"], "instance_ref": r["instance"]["id"]})
    _append(out, rows)
    return len(rows), spent


# -- the report -----------------------------------------------------------------------------


@dataclass
class Test:
    name: str
    model_key: str
    n: int
    point: float
    lo: float
    hi: float
    worse: int
    better: int
    candidate_rate: Rate
    baseline_rate: Rate

    @property
    def detectable(self) -> float:
        """The effect this comparison had 80% power to see, from its own interval: the
        half-width scaled from 1.96 standard errors to 2.80."""
        return (self.hi - self.lo) / 2 * (2.80 / 1.96)

    def cell(self) -> str:
        return (
            f"{self.point * 100:+.1f} ({self.lo * 100:+.1f} to {self.hi * 100:+.1f}), "
            f"n {self.n}, could detect ~{self.detectable * 100:.0f}"
        )


def outcomes(
    answers: Mapping[str, Answer], verdicts: Mapping[str, dict[str, Any]]
) -> dict[tuple[str, str, str], dict[str, bool]]:
    """(part, arm, model) -> question -> complete. A request the guard refused counts as not
    complete: the client got no answer. An answer the judge could not grade is left out."""
    out: dict[tuple[str, str, str], dict[str, bool]] = {}
    for a in answers.values():
        if a.part not in COMPARISONS:
            continue
        cell = out.setdefault((a.part, a.arm, a.model_key), {})
        if a.refused:
            cell[a.item_id] = False
            continue
        v = verdicts.get(a.key)
        if v is not None and v["complete"] is not None:
            cell[a.item_id] = bool(v["complete"])
    return out


def pooled_difference(
    pairs: Sequence[tuple[str, bool, bool]],
    judge_counts: tuple[int, int, int, int],
    *,
    resamples: int = 4000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """(candidate - baseline) over every model, resampled by QUESTION rather than by answer,
    because three models answering one question are not three independent items; judge-
    corrected the way 03's `paired_difference` is, with sensitivity and specificity resampled
    from the calibration counts. `pairs` is (question, baseline, candidate)."""
    by_q: dict[str, list[int]] = {}
    for q, b, c in pairs:
        by_q.setdefault(q, []).append(int(c) - int(b))
    qs = sorted(by_q)
    tp, fn, fp, tn = judge_counts
    se_n, sp_n = tp + fn, fp + tn
    se_p, sp_p = tp / se_n, tn / sp_n
    youden = se_p + sp_p - 1.0

    def mean(sample: Sequence[str]) -> float:
        diffs = [d for q in sample for d in by_q[q]]
        return sum(diffs) / len(diffs)

    point = mean(qs) / youden
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        sample = [qs[rng.randrange(len(qs))] for _ in qs]
        y = rng.binomialvariate(se_n, se_p) / se_n + rng.binomialvariate(sp_n, sp_p) / sp_n - 1
        if y > 0:
            draws.append(mean(sample) / y)
    draws.sort()
    return (
        point,
        draws[int(0.025 * len(draws))],
        draws[min(len(draws) - 1, int(0.975 * len(draws)))],
    )


@dataclass
class RedTeamRow:
    model_key: str
    arm: str
    reading: str
    leaked: int
    graded: int

    @property
    def rate(self) -> Rate:
        return Rate(self.leaked, self.graded)


@dataclass
class QualityReport:
    boundary_version: str
    judge_counts: tuple[int, int, int, int]
    kappa: tuple[float, float, float]
    tests: list[Test] = field(default_factory=list)
    pooled: dict[str, tuple[float, float, float, int]] = field(default_factory=dict)
    rates: dict[str, Rate] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)
    unresolved: dict[str, tuple[int, int]] = field(default_factory=dict)
    wire: dict[str, tuple[int, int]] = field(default_factory=dict)
    addressed: dict[str, tuple[int, int]] = field(default_factory=dict)
    redteam: list[RedTeamRow] = field(default_factory=list)
    spent_usd: float = 0.0
    answers: int = 0
    verdicts: int = 0

    def table(self) -> str:
        tp, fn, fp, tn = self.judge_counts
        k, klo, khi = self.kappa
        lines = [
            f"boundary {self.boundary_version}, the quality cost of redaction through 03's gate "
            f"(PLAN.md B2.8): {self.answers} answers, {self.verdicts} verdicts, "
            f"US${self.spent_usd:.2f}",
            f"judge {JUDGE['key']} ({JUDGE['model']}) on completeness, kappa {k:.3f} "
            f"({klo:.3f} to {khi:.3f}); counts tp {tp} fn {fn} fp {fp} tn {tn}; differences "
            "are judge-corrected, in points, candidate minus baseline",
            "",
            "complete rate per arm and model",
        ]
        for name, r in sorted(self.rates.items()):
            lines.append(f"  {name:<52} {r}")
        lines += ["", "paired differences, per model (03's paired_difference)"]
        for t in self.tests:
            lines.append(f"  {t.name + ', ' + t.model_key:<60} {t.cell()}")
        lines += ["", "pooled over the three models, resampled by question"]
        for name, (p, lo, hi, n) in self.pooled.items():
            det = (hi - lo) / 2 * (2.80 / 1.96)
            lines.append(
                f"  {name:<60} {p * 100:+.1f} ({lo * 100:+.1f} to {hi * 100:+.1f}), "
                f"{n} pairs, could detect ~{det * 100:.0f}"
            )
        lines += ["", "refused by the guard: " + _pairs(self.refused)]
        lines.append(
            "answers with a placeholder left unresolved after rehydration: "
            + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.unresolved.items()))
        )
        lines.append(
            "personal values that reached the wire: "
            + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.wire.items()))
        )
        lines.append(
            "answers that use the customer's name: "
            + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.addressed.items()))
        )
        if self.redteam:
            lines += ["", "red team, pii_leakage, 03's withholds_pii (leak rate, Wilson)"]
            for row in self.redteam:
                lines.append(f"  {row.model_key:<22} {row.arm:<9} {row.reading:<22} {row.rate}")
        return "\n".join(lines)


def _pairs(d: Mapping[str, int]) -> str:
    return "; ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def report(
    bridge: Bridge,
    answers: Mapping[str, Answer],
    verdicts: Mapping[str, dict[str, Any]],
    *,
    seed: int = SEED,
) -> QualityReport:
    from boundary import __version__

    cal = bridge.run("calibration", {"gold": bridge.gold, "judge_key": JUDGE["key"]})
    if not cal["matches_03_constant"]:
        raise ValueError("the judge's counts recomputed from 03's store differ from 03's own")
    tp, fn, fp, tn = (int(x) for x in cal["counts"])
    counts = (tp, fn, fp, tn)
    rep = QualityReport(
        __version__,
        counts,
        (cal["kappa"], cal["kappa_lo"], cal["kappa_hi"]),
    )
    rep.answers = sum(1 for a in answers.values() if a.status in ("200", "422"))
    rep.verdicts = len(verdicts)
    rep.spent_usd = sum(a.cost_usd or 0.0 for a in answers.values()) + sum(
        v.get("cost_usd") or 0.0 for v in verdicts.values()
    )
    got = outcomes(answers, verdicts)
    for (part, arm, model_key), cell in got.items():
        rep.rates[f"{part}/{arm}/{model_key}"] = Rate(sum(cell.values()), len(cell))
    comparisons: dict[str, dict[str, Any]] = {}
    for part, wanted in COMPARISONS.items():
        for cand, base in wanted:
            for model_key in PANEL:
                b_part = "public" if base == "raw" else part
                b = got.get((b_part, base, model_key), {})
                c = got.get((part, cand, model_key), {})
                if b and c:
                    comparisons[f"{cand} vs {base}|{model_key}"] = {"baseline": b, "candidate": c}
    tests = bridge.run(
        "paired",
        {"comparisons": comparisons, "judge_counts": list(counts), "seed": seed},
    )["tests"]
    for name, t in tests.items():
        label, model_key = name.split("|")
        cand, base = label.split(" vs ")
        c_cell = comparisons[name]["candidate"]
        b_cell = comparisons[name]["baseline"]
        rep.tests.append(
            Test(
                label,
                model_key,
                t["n"],
                t["point"],
                t["lo"],
                t["hi"],
                t["worse"],
                t["better"],
                Rate(sum(c_cell.values()), len(c_cell)),
                Rate(sum(b_cell.values()), len(b_cell)),
            )
        )
    for label in {t.name for t in rep.tests}:
        pairs: list[tuple[str, bool, bool]] = []
        for model_key in PANEL:
            name = f"{label}|{model_key}"
            if name not in comparisons:
                continue
            b, c = comparisons[name]["baseline"], comparisons[name]["candidate"]
            pairs += [(q, b[q], c[q]) for q in sorted(set(b) & set(c))]
        p, lo, hi = pooled_difference(pairs, counts, seed=seed)
        rep.pooled[label] = (p, lo, hi, len(pairs))
    rep.pooled = dict(sorted(rep.pooled.items()))
    rep.tests.sort(key=lambda t: (t.name, t.model_key))
    personas: dict[str, Persona] = {}
    if any(a.part == "personal" for a in answers.values()):
        questions = {q: v["question"] for q, v in _gold_questions(bridge.gate).items()}
        personas = {q: p for q, (_, p) in personalise(questions, seed=seed).items()}
    _counts(rep, answers, personas)
    rep.redteam = _redteam(bridge, answers)
    return rep


def _counts(
    rep: QualityReport, answers: Mapping[str, Answer], personas: Mapping[str, Persona]
) -> None:
    for a in answers.values():
        if a.part == "redteam":
            continue
        group = f"{a.part}/{a.arm}"
        if a.refused:
            rep.refused[group] = rep.refused.get(group, 0) + 1
        if "redacted" in a.arm and a.ok:
            u, n = rep.unresolved.get(group, (0, 0))
            rep.unresolved[group] = (u + (a.unresolved > 0), n + 1)
        if a.planted:
            u, n = rep.wire.get(group, (0, 0))
            rep.wire[group] = (u + a.leaked_on_wire, n + a.planted)
        if a.part == "personal" and a.ok and a.item_id in personas:
            u, n = rep.addressed.get(group, (0, 0))
            rep.addressed[group] = (u + _addresses(a.output or "", personas[a.item_id]), n + 1)


def _redteam(bridge: Bridge, answers: Mapping[str, Answer]) -> list[RedTeamRow]:
    rt = [a for a in answers.values() if a.part == "redteam"]
    if not rt:
        return []
    readings: list[dict[str, Any]] = []
    for a in rt:
        base = {"item_id": a.item_id, "ok": a.ok, "finish_reason": a.finish_reason}
        readings.append({**base, "key": f"{a.key}|client", "text": a.output})
        if a.arm == "redacted":
            readings.append({**base, "key": f"{a.key}|model", "text": a.output_model})
            if a.sent_text is not None:
                readings.append(
                    {
                        **base,
                        "key": f"{a.key}|sent",
                        "text": a.sent_text,
                        "ok": True,
                        "finish_reason": "stop",
                    }
                )
    grades = bridge.run("redteam-grade", {"answers": readings})["grades"]
    cells: dict[tuple[str, str, str], list[int]] = {}
    names = {
        "client": "received by the client",
        "model": "written by the model",
        "sent": "sent to the vendor",
    }
    for g in grades:
        key, reading = g["key"].split("|")
        _part, arm, model_key, _item = key.split("/")
        if g["passed"] is None:
            continue
        cell = cells.setdefault((model_key, arm, names[reading]), [0, 0])
        cell[0] += not g["passed"]
        cell[1] += 1
    return [
        RedTeamRow(m, arm, reading, leaked, graded)
        for (m, arm, reading), (leaked, graded) in sorted(cells.items())
    ]


__all__ = [
    "JUDGE",
    "PANEL",
    "PARTS",
    "Answer",
    "Bridge",
    "Job",
    "Persona",
    "QualityReport",
    "collect",
    "derive_allow",
    "judge",
    "personalise",
    "plan_jobs",
    "pooled_difference",
    "read_answers",
    "read_verdicts",
    "report",
]
