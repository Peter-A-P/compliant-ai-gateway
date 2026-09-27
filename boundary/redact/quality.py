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


def _wrong_name(text: str, persona: Persona) -> bool:
    """Whether an answer uses a forename from the corpus that is not part of this customer's
    own name: the rehydration error that would address one person by another's name."""
    from boundary.redact import corpus

    names = {*corpus.FORENAMES_PLAIN, *corpus.FORENAMES_ACCENTED, *corpus.FORENAMES_HYPHENATED}
    return any(
        n not in persona.name and re.search(rf"\b{re.escape(n)}\b", text) is not None for n in names
    )


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
    """03's AnswerInstance records for the judge-graded parts. The id is the answer's place
    among its part's keys, offset by a thousand per part, so it does not move when another
    part's answers are added."""
    questions = _gold_questions(gate)
    everything = list(answers)
    out = []
    for offset, part in enumerate(COMPARISONS):
        keys = sorted(a.key for a in everything if a.part == part)
        place = {k: offset * 1000 + n for n, k in enumerate(keys, start=1)}
        out += [(place[a.key], a) for a in everything if a.part == part and a.ok]
    return [_instance(n, a, questions) for n, a in sorted(out, key=lambda x: x[0])]


def _instance(n: int, a: Answer, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    text = a.output or ""
    return {
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
        # Nothing is re-asked: an ungradeable verdict is stored and stays ungradeable, 03's
        # rule, so a judge that failed on some answers is not given a second chance at them.
        if k.startswith(part + "/") and k not in done
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

    made = 0
    # In chunks, each stored as soon as 03's parser has read it, so a failure late in a run
    # loses one chunk's replies rather than every one paid for.
    for start in range(0, len(todo), 100):
        replies.clear()
        await asyncio.gather(*(one(k, i) for k, i in todo[start : start + 100]))
        if not replies:
            break
        got = bridge.run(
            "verdicts",
            {
                "judge": JUDGE,
                "replies": [{k: v for k, v in r.items() if k != "key"} for r in replies],
            },
        )
        rows = [
            {**v, "answer_key": r["key"], "instance_ref": r["instance"]["id"]}
            for r, v in zip(replies, got["verdicts"], strict=True)
        ]
        _append(out, rows)
        made += len(rows)
    return made, spent


# -- the report -----------------------------------------------------------------------------

# The strata a comparison is reported in. The split by answerability was not in the plan: it
# was chosen after reading the five answers part 1 judged incomplete, every one of which was
# to a question 03 wrote as NOT answerable from its page, and it is labelled post hoc wherever
# it is printed.
STRATA = ("all", "answerable", "unanswerable")


def paired_newcombe(
    pairs: Sequence[tuple[bool, bool]], *, z: float = 1.96
) -> tuple[float, float, float]:
    """Candidate minus baseline over paired binary outcomes, (baseline, candidate) per item,
    with Newcombe's interval for a paired difference (1998, method 10): Wilson intervals on
    the two margins, combined with the phi correlation of the pairs.

    Beside 03's bootstrap rather than instead of it, because a bootstrap of pairs that never
    disagree resamples zeros into a zero-width interval, a bare number in brackets, and at a
    100% baseline that is most of what this run produces. This interval stays honest there:
    no disagreement in 100 pairs is about plus or minus four points, not zero."""
    from boundary.redact.evaluate import wilson

    n = len(pairs)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    e = sum(1 for b, c in pairs if b and c)
    f = sum(1 for b, c in pairs if b and not c)
    g = sum(1 for b, c in pairs if not b and c)
    h = n - e - f - g
    p1, p2 = (e + f) / n, (e + g) / n
    l1, u1 = wilson(e + f, n, z=z)
    l2, u2 = wilson(e + g, n, z=z)
    denom = ((e + f) * (g + h) * (e + g) * (f + h)) ** 0.5
    phi = (e * h - f * g) / denom if denom else 0.0
    d = p2 - p1
    lower = d - max(0.0, (p2 - l2) ** 2 - 2 * phi * (p2 - l2) * (u1 - p1) + (u1 - p1) ** 2) ** 0.5
    upper = d + max(0.0, (u2 - p2) ** 2 - 2 * phi * (u2 - p2) * (p1 - l1) + (p1 - l1) ** 2) ** 0.5
    return d, lower, upper


def detectable_loss(
    baseline: Sequence[bool],
    youden: float,
    *,
    power: float = 0.8,
    sims: int = 400,
    seed: int = 0,
) -> float | None:
    """The smallest true loss, in proportion, that this many items at this baseline would
    show at `power` as a Newcombe interval wholly below zero: each baseline success is made
    a failure with probability loss times the judge's Youden factor (the loss the judge would
    see), nothing improves, and the search runs in half-point steps. None past thirty points.
    Printed beside every comparison because a null from a small set is a statement about the
    set as much as about redaction (B2.8)."""
    rng = random.Random(seed)
    for step in range(1, 61):
        loss = step / 200
        seen = loss * youden
        hits = 0
        for _ in range(sims):
            pairs = [(b, b and rng.random() >= seen) for b in baseline]
            if paired_newcombe(pairs)[2] < 0:
                hits += 1
        if hits / sims >= power:
            return loss
    return None


@dataclass
class Comparison:
    """One comparison: candidate minus baseline, judge-corrected, with both intervals."""

    label: str
    model_key: str  # a panel key, or "pooled"
    stratum: str
    n: int
    worse: int
    better: int
    point: float
    boot: tuple[float, float]
    newcombe: tuple[float, float]
    detectable: float | None

    def cell(self) -> str:
        blo, bhi = self.boot
        nlo, nhi = self.newcombe
        det = f"~{self.detectable * 100:.1f}" if self.detectable is not None else "over 30"
        return (
            f"{self.point * 100:+5.1f}  bootstrap {blo * 100:+.1f} to {bhi * 100:+.1f}  "
            f"Newcombe {nlo * 100:+.1f} to {nhi * 100:+.1f}  "
            f"({self.worse} worse, {self.better} better of {self.n}; could detect a loss of "
            f"{det})"
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
    comparisons: list[Comparison] = field(default_factory=list)
    rates: dict[str, Rate] = field(default_factory=dict)
    refused: dict[str, int] = field(default_factory=dict)
    unresolved: dict[str, tuple[int, int]] = field(default_factory=dict)
    wire: dict[str, tuple[int, int]] = field(default_factory=dict)
    addressed: dict[str, tuple[int, int]] = field(default_factory=dict)
    wrong_name: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Answers in parts 1 and 2 that caller-scoped rehydration would leave a placeholder in.
    scoped_cost: tuple[int, int] = (0, 0)
    redteam: list[RedTeamRow] = field(default_factory=list)
    redteam_diffs: list[Comparison] = field(default_factory=list)
    spent_usd: float = 0.0
    answers: int = 0
    verdicts: int = 0

    @property
    def youden(self) -> float:
        tp, fn, fp, tn = self.judge_counts
        return tp / (tp + fn) + tn / (fp + tn) - 1

    def table(self) -> str:
        tp, fn, fp, tn = self.judge_counts
        k, klo, khi = self.kappa
        lines = [
            f"boundary {self.boundary_version}, the quality cost of redaction through 03's gate "
            f"(PLAN.md B2.8): {self.answers} answers, {self.verdicts} verdicts, "
            f"US${self.spent_usd:.2f}",
            f"judge {JUDGE['key']} ({JUDGE['model']}) on completeness, kappa {k:.3f} "
            f"({klo:.3f} to {khi:.3f}); counts tp {tp} fn {fn} fp {fp} tn {tn}, Youden "
            f"{self.youden:.3f}",
            "Differences are candidate minus baseline in points, divided by the Youden factor. "
            "Per model the bootstrap is 03's paired_difference; pooled, a bootstrap by question. "
            "Newcombe is the paired method 10 interval, divided by the same factor.",
            "",
            "complete rate per arm and model (Wilson)",
        ]
        for name, r in sorted(self.rates.items()):
            lines.append(f"  {name:<58} {r}")
        for stratum in STRATA:
            rows = [c for c in self.comparisons if c.stratum == stratum]
            if not rows:
                continue
            note = "" if stratum == "all" else " (split chosen after reading the failures)"
            lines += ["", f"{stratum} questions{note}"]
            for c in rows:
                lines.append(f"  {c.label + ', ' + c.model_key:<44} {c.cell()}")
        lines += ["", "refused by the guard: " + _pairs(self.refused)]
        lines.append(
            "answers with a placeholder left unresolved after rehydration: "
            + (
                "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.unresolved.items()))
                or "none"
            )
        )
        if self.wire:
            lines.append(
                "personal values that reached the wire: "
                + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.wire.items()))
            )
        if self.addressed:
            lines.append(
                "answers that use the customer's name: "
                + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.addressed.items()))
            )
        if self.wrong_name:
            lines.append(
                "answers that use another customer's name: "
                + "; ".join(f"{k} {u} of {n}" for k, (u, n) in sorted(self.wrong_name.items()))
            )
        u, n = self.scoped_cost
        if n:
            lines.append(
                f"answers in parts 1 and 2 that caller-scoped rehydration leaves a placeholder "
                f"in: {u} of {n}, {Rate(u, n)}"
            )
        if self.redteam:
            lines += ["", "red team, pii_leakage, 03's withholds_pii (leak rate, Wilson)"]
            for row in self.redteam:
                lines.append(f"  {row.model_key:<22} {row.arm:<9} {row.reading:<24} {row.rate}")
            lines += [
                "",
                "red team, paired on the 200 items: leak rate difference in points, redacted "
                "minus raw, Newcombe method 10 ('worse' is an item that leaked only redacted)",
            ]
            for c in self.redteam_diffs:
                lo, hi = c.newcombe
                lines.append(
                    f"  {c.model_key:<22} {c.label:<50} {c.point * 100:+5.1f} "
                    f"({lo * 100:+.1f} to {hi * 100:+.1f}); {c.worse} worse, {c.better} better"
                )
        return "\n".join(lines)

    def pooled(self, label: str, stratum: str = "all") -> Comparison:
        return next(
            c
            for c in self.comparisons
            if c.label == label and c.model_key == "pooled" and c.stratum == stratum
        )

    def readme_rows(self) -> str:
        """The quality table: pooled comparisons, Newcombe first because it is the interval
        that stays honest at a 100% baseline, 03's bootstrap beside it."""
        names = {
            "redacted vs raw": "Public pages, redacted as the proxy does today, against raw",
            "redacted-allow vs raw": "Public pages, redacted with the allow list, against raw",
            "personal-redacted-allow vs personal-raw": (
                "A customer's details in the question, redacted (with the list), against raw"
            ),
        }
        rows = []
        for label, text in names.items():
            for stratum in ("all", "unanswerable"):
                c = self.pooled(label, stratum)
                nlo, nhi = c.newcombe
                blo, bhi = c.boot
                det = f"{c.detectable * 100:.1f}" if c.detectable is not None else "over 30"
                what = text if stratum == "all" else "  of which, questions the page cannot answer"
                rows.append(
                    f"| {what} | {c.n} | {c.worse} / {c.better} | {c.point * 100:+.1f} "
                    f"({nlo * 100:+.1f} to {nhi * 100:+.1f}) | {blo * 100:+.1f} to "
                    f"{bhi * 100:+.1f} | {det} |"
                )
        return "\n".join(rows)

    def readme_redteam_rows(self) -> str:
        by = {(r.model_key, r.arm, r.reading): r for r in self.redteam}
        rows = []
        for model_key, (model, _) in PANEL.items():
            cells = [
                by[(model_key, "raw", READINGS["client"])],
                by[(model_key, "redacted", READINGS["sent"])],
                by[(model_key, "redacted", READINGS["client"])],
                by[(model_key, "redacted", READINGS["scoped"])],
            ]
            rows.append(
                f"| {model} | "
                + " | ".join(f"{c.leaked} of {c.graded}, {c.rate}" for c in cells)
                + " |"
            )
        return "\n".join(rows)

    def to_json(self) -> str:
        return json.dumps(
            {
                "boundary_version": self.boundary_version,
                "judge": JUDGE,
                "judge_counts": self.judge_counts,
                "kappa": self.kappa,
                "answers": self.answers,
                "verdicts": self.verdicts,
                "spent_usd": round(self.spent_usd, 4),
                "rates": {k: [r.hits, r.total] for k, r in sorted(self.rates.items())},
                "comparisons": [asdict(c) for c in self.comparisons],
                "redteam": [asdict(r) for r in self.redteam],
                "redteam_diffs": [asdict(c) for c in self.redteam_diffs],
                "refused": self.refused,
                "unresolved": self.unresolved,
                "wire": self.wire,
                "addressed": self.addressed,
                "wrong_name": self.wrong_name,
                "scoped_cost": self.scoped_cost,
            },
            indent=1,
        )


README_START = "<!-- quality:start -->"
README_END = "<!-- quality:end -->"
README_RT_START = "<!-- quality-redteam:start -->"
README_RT_END = "<!-- quality-redteam:end -->"


def write_readme(readme: Path, rep: QualityReport) -> None:
    text = readme.read_text(encoding="utf-8")
    for start, end, rows in (
        (README_START, README_END, rep.readme_rows()),
        (README_RT_START, README_RT_END, rep.readme_redteam_rows()),
    ):
        a, b = text.index(start), text.index(end)
        text = text[: a + len(start)] + "\n" + rows + "\n" + text[b:]
    readme.write_text(text, encoding="utf-8")


def _pairs(d: Mapping[str, int]) -> str:
    return "; ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def _in_stratum(stratum: str, unanswerable: bool) -> bool:
    return stratum == "all" or (stratum == "unanswerable") == unanswerable


def report(
    bridge: Bridge,
    answers: Mapping[str, Answer],
    verdicts: Mapping[str, dict[str, Any]],
    *,
    allow: Sequence[str] = (),
    seed: int = SEED,
) -> QualityReport:
    from boundary import __version__

    cal = bridge.run("calibration", {"gold": bridge.gold, "judge_key": JUDGE["key"]})
    if not cal["matches_03_constant"]:
        raise ValueError("the judge's counts recomputed from 03's store differ from 03's own")
    tp, fn, fp, tn = (int(x) for x in cal["counts"])
    counts = (tp, fn, fp, tn)
    rep = QualityReport(__version__, counts, (cal["kappa"], cal["kappa_lo"], cal["kappa_hi"]))
    rep.answers = sum(1 for a in answers.values() if a.status in ("200", "422"))
    rep.verdicts = len(verdicts)
    rep.spent_usd = sum(a.cost_usd or 0.0 for a in answers.values()) + sum(
        v.get("cost_usd") or 0.0 for v in verdicts.values()
    )
    gold = _gold_questions(bridge.gate)
    unanswerable = {q: bool(v.get("unanswerable")) for q, v in gold.items()}
    got = outcomes(answers, verdicts)
    for (part, arm, model_key), cell in got.items():
        rep.rates[f"{part}/{arm}/{model_key}"] = Rate(sum(cell.values()), len(cell))

    # Every (label, model, stratum) with its paired items, for 03's bootstrap in one call.
    wanted: dict[str, list[tuple[str, bool, bool]]] = {}
    for part, pairs in COMPARISONS.items():
        for cand, base in pairs:
            for model_key in PANEL:
                b = got.get(("public" if base == "raw" else part, base, model_key), {})
                c = got.get((part, cand, model_key), {})
                for stratum in STRATA:
                    items = [
                        (q, b[q], c[q])
                        for q in sorted(set(b) & set(c))
                        if _in_stratum(stratum, unanswerable.get(q, False))
                    ]
                    if items:
                        wanted[f"{cand} vs {base}|{model_key}|{stratum}"] = items
    tests = bridge.run(
        "paired",
        {
            "comparisons": {
                name: {
                    "baseline": {q: b for q, b, _ in items},
                    "candidate": {q: c for q, _, c in items},
                }
                for name, items in wanted.items()
            },
            "judge_counts": list(counts),
            "seed": seed,
        },
    )["tests"]
    y = rep.youden
    pooled: dict[tuple[str, str], list[tuple[str, bool, bool]]] = {}
    for name, items in wanted.items():
        label, model_key, stratum = name.split("|")
        t = tests[name]
        _, nlo, nhi = paired_newcombe([(b, c) for _, b, c in items])
        rep.comparisons.append(
            Comparison(
                label,
                model_key,
                stratum,
                t["n"],
                t["worse"],
                t["better"],
                t["point"],
                (t["lo"], t["hi"]),
                (nlo / y, nhi / y),
                detectable_loss([b for _, b, _ in items], y, seed=seed),
            )
        )
        pooled.setdefault((label, stratum), []).extend(items)
    for (label, stratum), items in pooled.items():
        point, blo, bhi = pooled_difference(items, counts, seed=seed)
        _, nlo, nhi = paired_newcombe([(b, c) for _, b, c in items])
        rep.comparisons.append(
            Comparison(
                label,
                "pooled",
                stratum,
                len(items),
                sum(1 for _, b, c in items if b and not c),
                sum(1 for _, b, c in items if c and not b),
                point,
                (blo, bhi),
                (nlo / y, nhi / y),
                detectable_loss([b for _, b, _ in items], y, seed=seed),
            )
        )
    order = {m: i for i, m in enumerate([*PANEL, "pooled"])}
    rep.comparisons.sort(key=lambda c: (STRATA.index(c.stratum), c.label, order[c.model_key]))
    personas: dict[str, Persona] = {}
    if any(a.part == "personal" for a in answers.values()):
        questions = {q: v["question"] for q, v in gold.items()}
        personas = {q: p for q, (_, p) in personalise(questions, seed=seed).items()}
    _counts(rep, answers, personas)
    rep.redteam, rep.redteam_diffs = _redteam(bridge, answers)
    rep.scoped_cost = _scoped_cost(bridge, answers, allow, seed=seed)
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
            u, n = rep.wrong_name.get(group, (0, 0))
            rep.wrong_name[group] = (u + _wrong_name(a.output or "", personas[a.item_id]), n + 1)


READINGS = {
    "sent": "sent to the vendor",
    "model": "written by the model",
    "client": "received by the client",
    "scoped": "received, caller-scoped",
}


def _scoped_outputs(bridge: Bridge, rt: Sequence[Answer]) -> dict[str, str]:
    """Each redacted red-team answer rehydrated with `caller_scoped`, offline: the request is
    rebuilt from 03's item and redacted again, and the rebuild is checked against the hash of
    what was actually sent, so the vault is the one the answer was written against."""
    from boundary.redact.request import caller_scoped

    items = {i["id"]: i for i in bridge.run("redteam-items", {})["items"]}
    out: dict[str, str] = {}
    for a in rt:
        if a.arm != "redacted" or a.output_model is None:
            continue
        item = items[a.item_id]
        request = ChatRequest(
            model=a.model,
            system=item["system"],
            messages=[{"role": "user", "content": item["prompt"]}],
        )
        red = redact_request(request)
        sent = hashlib.sha256(_sent_text(red.request).encode("utf-8")).hexdigest()
        if sent != a.sent_sha256:
            raise ValueError(f"{a.key}: the rebuilt request is not the one that was sent")
        out[a.key] = caller_scoped(red, request).rehydrate(a.output_model)
    return out


def _scoped_cost(
    bridge: Bridge, answers: Mapping[str, Answer], allow: Sequence[str], *, seed: int
) -> tuple[int, int]:
    """How many redacted answers in parts 1 and 2 caller-scoped rehydration would leave a
    placeholder in: the price, on ordinary questions, of the remedy the red team found."""
    from boundary.redact.request import caller_scoped

    jobs: dict[str, Job] = {}
    for part in ("public", "personal"):
        if any(
            a.part == part and "redacted" in a.arm and a.output_model is not None
            for a in answers.values()
        ):
            jobs |= {j.key: j for j in plan_jobs(part, bridge, seed=seed)}
    changed = total = 0
    for a in answers.values():
        if a.part == "redteam" or "redacted" not in a.arm or a.output_model is None:
            continue
        job = jobs[a.key]
        request = ChatRequest(
            model=a.model, system=job.system, messages=[{"role": "user", "content": job.prompt}]
        )
        red = redact_request(request, allow=allow if a.arm.endswith("-allow") else ())
        if hashlib.sha256(_sent_text(red.request).encode("utf-8")).hexdigest() != a.sent_sha256:
            raise ValueError(f"{a.key}: the rebuilt request is not the one that was sent")
        total += 1
        changed += caller_scoped(red, request).rehydrate(a.output_model) != a.output
    return changed, total


def _redteam(
    bridge: Bridge, answers: Mapping[str, Answer]
) -> tuple[list[RedTeamRow], list[Comparison]]:
    rt = [a for a in answers.values() if a.part == "redteam"]
    if not rt:
        return [], []
    scoped = _scoped_outputs(bridge, rt)
    readings: list[dict[str, Any]] = []
    for a in rt:
        base = {"item_id": a.item_id, "ok": a.ok, "finish_reason": a.finish_reason}
        readings.append({**base, "key": f"{a.key}|client", "text": a.output})
        if a.arm == "redacted":
            readings.append({**base, "key": f"{a.key}|model", "text": a.output_model})
            readings.append({**base, "key": f"{a.key}|scoped", "text": scoped.get(a.key)})
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
    # (model, arm, reading) -> item -> leaked
    leaked: dict[tuple[str, str, str], dict[str, bool]] = {}
    for g in grades:
        key, reading = g["key"].split("|")
        _part, arm, model_key, item = key.split("/")
        if g["passed"] is None:
            continue
        leaked.setdefault((model_key, arm, reading), {})[item] = not g["passed"]
    order = list(READINGS)
    rows = [
        RedTeamRow(m, arm, READINGS[r], sum(cell.values()), len(cell))
        for (m, arm, r), cell in sorted(
            leaked.items(), key=lambda x: (x[0][0], x[0][1] != "raw", order.index(x[0][2]))
        )
    ]
    diffs: list[Comparison] = []
    for model_key in PANEL:
        raw = leaked.get((model_key, "raw", "client"), {})
        for reading in ("client", "scoped"):
            red = leaked.get((model_key, "redacted", reading), {})
            items = sorted(set(raw) & set(red))
            if not items:
                continue
            pairs = [(raw[i], red[i]) for i in items]
            d, lo, hi = paired_newcombe(pairs)
            diffs.append(
                Comparison(
                    f"leak rate, redacted {READINGS[reading]} vs raw",
                    model_key,
                    "redteam",
                    len(pairs),
                    sum(1 for b, c in pairs if c and not b),
                    sum(1 for b, c in pairs if b and not c),
                    d,
                    (lo, hi),
                    (lo, hi),
                    None,
                )
            )
    return rows, diffs


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
