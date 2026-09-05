"""The outreach judge (SPEC section 6, ``--tier outreach``): a frozen ``clinevals``
faithfulness rubric applied to the drafter's provider note + patient message against the
evidence lines the plan was drafted from.

The rubric is ``faithfulness_rubric("Medicare Advantage HEDIS-aligned care-gap outreach",
name="caregap-faithfulness-v1")``; its sha256 is stamped into every artifact so drift is
visible, never silent. Judge outputs are recorded to ``evals/recorded/judge.jsonl`` (one row
per plan, keyed by ``judge:<patient>:plan:<as_of>:0``) and CI only re-parses them — the judge
itself needs a real key and runs locally. Judge-human agreement uses a stratified spot-check
sample against ``evals/gold/judge_human_spotcheck.jsonl`` when that file exists, else the
artifact says ``pending``; agreement below 0.80 stamps the run untrusted.

No model is built here: the judge arrives as a ``BaseChatModel`` (keyless fakes in tests).
"""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal

from clinevals import (
    EvalItemBase,
    EvalReport,
    GroundingVerdict,
    ItemScore,
    JudgeParseError,
    Rubric,
    Split,
    agreement_rate,
    build_judge_input,
    faithfulness_rubric,
    invoke_judge,
    parse_grounding_verdict,
    score_items,
    stratified_sample,
    verdict_metrics,
)
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from caregap.agents.schemas import CareActionPlan
from caregap.measures.ids import MEASURE_NAMES
from caregap.measures.models import OpenGap, ReviewItem

JUDGE_DOMAIN = "Medicare Advantage HEDIS-aligned care-gap outreach"
RUBRIC_NAME = "caregap-faithfulness-v1"
RUBRIC: Rubric = faithfulness_rubric(JUDGE_DOMAIN, name=RUBRIC_NAME)
UNTRUSTED_BELOW = 0.80
SPOTCHECK_N = 12
SPOTCHECK_SEED = 20260903
PARSE_FAILURE_METRIC = "judge_parse_failure"

GroundedLabel = Literal["grounded", "ungrounded"]


def judge_case_key(patient_id: str, as_of: date) -> str:
    return f"judge:{patient_id}:plan:{as_of.isoformat()}:0"


# --- judge input --------------------------------------------------------------------------


def evidence_passages(
    open_gaps: Sequence[OpenGap],
    review_items: Sequence[ReviewItem],
    *,
    as_of: date,
    clinic_name: str,
    clinic_phone: str,
) -> list[str]:
    """The numbered passages the judge may ground claims in: the drafting context and one
    line per open gap (measure, rank, subtype, evidence codes and dates — never names)."""
    passages = [
        f"Outreach context: as of {as_of.isoformat()}; the clinic is {clinic_name}, phone "
        f"{clinic_phone}; the measurement year is {as_of.year}."
    ]
    for gap in sorted(open_gaps, key=lambda g: (g.rank, g.measure_id)):
        evidence = "; ".join(
            f"{ref.section} {ref.code or '-'} ({ref.code_system or '-'}) on "
            f"{ref.event_date.isoformat() if ref.event_date else 'unknown date'}"
            for ref in sorted(gap.evidence, key=lambda r: (r.event_date or date.min, r.event_id))
        )
        passages.append(
            f"Open care gap #{gap.rank}: {MEASURE_NAMES[gap.measure_id]} ({gap.measure_id}) is "
            f"open as of {as_of.isoformat()}"
            + (f"; subtype {gap.subtype}" if gap.subtype else "")
            + (f"; evidence: {evidence}." if evidence else "; no qualifying evidence in window.")
        )
    for item in sorted(review_items, key=lambda r: (r.measure_id or "", r.scope, r.reason)):
        label = MEASURE_NAMES[item.measure_id] if item.measure_id is not None else "All measures"
        passages.append(
            f"Pending clinical review ({item.scope}): {label} — {item.reason}. No outreach may "
            "be drafted for it."
        )
    return passages


def outreach_answer(plan: CareActionPlan) -> str:
    return f"PROVIDER NOTE:\n{plan.provider_note}\n\nPATIENT MESSAGE:\n{plan.patient_message}"


def outreach_question(patient_id: str, as_of: date) -> str:
    return (
        f"Draft the provider note and patient outreach message for the open care gaps of "
        f"synthetic patient {patient_id} as of {as_of.isoformat()}."
    )


def build_outreach_judge_input(
    patient_id: str, as_of: date, plan: CareActionPlan, passages: Sequence[str]
) -> str:
    return build_judge_input(
        outreach_question(patient_id, as_of), outreach_answer(plan), list(passages)
    )


# --- records ------------------------------------------------------------------------------


class OutreachCase(BaseModel):
    """One plan to judge, with the passages it was drafted from."""

    model_config = ConfigDict(frozen=True)

    patient_id: str
    as_of: date
    split: Split
    plan: CareActionPlan
    passages: list[str] = Field(default_factory=list)


class JudgeRecord(BaseModel):
    """One recorded judge response (``evals/recorded/judge.jsonl``)."""

    model_config = ConfigDict(frozen=True)

    case_key: str
    patient_id: str
    as_of: date
    split: Split
    rubric_name: str
    rubric_sha256: str
    judge_model: str
    response: str


def judge_cases(
    cases: Sequence[OutreachCase], model: BaseChatModel, *, judge_model_id: str
) -> list[JudgeRecord]:
    """Invoke the judge once per case (no retries; parsing happens at scoring time)."""
    records: list[JudgeRecord] = []
    for case in sorted(cases, key=lambda c: c.patient_id):
        payload = build_outreach_judge_input(case.patient_id, case.as_of, case.plan, case.passages)
        records.append(
            JudgeRecord(
                case_key=judge_case_key(case.patient_id, case.as_of),
                patient_id=case.patient_id,
                as_of=case.as_of,
                split=case.split,
                rubric_name=RUBRIC.name,
                rubric_sha256=RUBRIC.sha256,
                judge_model=judge_model_id,
                response=invoke_judge(model, RUBRIC, payload),
            )
        )
    return records


def write_judge_records(records: Sequence[JudgeRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in sorted(records, key=lambda r: r.case_key):
            fh.write(
                json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                + "\n"
            )


def read_judge_records(path: Path) -> list[JudgeRecord]:
    if not path.exists():
        raise FileNotFoundError(f"no recorded judge outputs at {path}")
    return [
        JudgeRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8-sig").split("\n")
        if line.strip()
    ]


# --- scoring ------------------------------------------------------------------------------


class JudgeItem(EvalItemBase):
    record: JudgeRecord


def parse_record(record: JudgeRecord) -> GroundingVerdict | None:
    """The verdict, or ``None`` when the response carried no parseable verdict."""
    try:
        return parse_grounding_verdict(record.response)
    except JudgeParseError:
        return None


def score_judgements(records: Sequence[JudgeRecord]) -> EvalReport:
    """Faithfulness / contradiction / citation metrics per plan; a garbage response scores
    ``judge_parse_failure = 1.0`` and nothing else (never a flattering default)."""
    items = [
        JudgeItem(item_id=r.case_key, category="outreach", split=r.split, record=r)
        for r in sorted(records, key=lambda r: r.case_key)
    ]

    def score_item(item: JudgeItem) -> ItemScore | None:
        verdict = parse_record(item.record)
        if verdict is None:
            return ItemScore(metrics={PARSE_FAILURE_METRIC: 1.0})
        return ItemScore(metrics={**verdict_metrics(verdict), PARSE_FAILURE_METRIC: 0.0})

    return score_items(items, score_item)


def grounded_label(verdict: GroundingVerdict) -> GroundedLabel:
    """Item-level label a human can reproduce: every claim supported -> ``grounded``."""
    clean = verdict.claims_unsupported == 0 and verdict.claims_contradicted == 0
    return "grounded" if verdict.claims_total > 0 and clean else "ungrounded"


class SpotcheckLabel(BaseModel):
    """One human label (``evals/gold/judge_human_spotcheck.jsonl``)."""

    model_config = ConfigDict(frozen=True)

    case_key: str
    label: GroundedLabel
    reviewer: str
    note: str = ""


def read_spotcheck(path: Path) -> list[SpotcheckLabel]:
    if not path.exists():
        return []
    return [
        SpotcheckLabel.model_validate_json(line)
        for line in path.read_text(encoding="utf-8-sig").split("\n")
        if line.strip()
    ]


def spotcheck_sample(
    records: Sequence[JudgeRecord], *, n: int = SPOTCHECK_N, seed: int = SPOTCHECK_SEED
) -> list[JudgeRecord]:
    """The deterministic, split-stratified sample a human labels (``case_key`` order kept)."""
    ordered = sorted(records, key=lambda r: r.case_key)
    return stratified_sample(ordered, n=n, key=lambda r: r.split, seed=seed)


def judge_human_agreement(
    records: Sequence[JudgeRecord], labels: Sequence[SpotcheckLabel]
) -> float | None:
    """Exact-match agreement over cases both the judge and the human labeled; ``None``
    (``pending``) when no human label matches a recorded case."""
    by_key = {r.case_key: r for r in records}
    pairs: list[tuple[str, str]] = []
    for label in labels:
        record = by_key.get(label.case_key)
        if record is None:
            continue
        verdict = parse_record(record)
        judge = grounded_label(verdict) if verdict is not None else "unparsed"
        pairs.append((judge, label.label))
    return agreement_rate(pairs) if pairs else None
