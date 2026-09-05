"""The outreach judge: the frozen rubric, evidence passages built from ids / codes / dates
only, recorded judge outputs (a keyless fake model), parse failures that score as failures,
and the judge-human agreement over the spot-check sample."""

import json
from datetime import date
from pathlib import Path

from clinevals import faithfulness_rubric

from caregap.agents.schemas import CareActionPlan, GapAction, PlannedGap
from caregap.evals.rubrics import (
    JUDGE_DOMAIN,
    RUBRIC,
    RUBRIC_NAME,
    JudgeRecord,
    OutreachCase,
    SpotcheckLabel,
    build_outreach_judge_input,
    evidence_passages,
    grounded_label,
    judge_case_key,
    judge_cases,
    judge_human_agreement,
    parse_record,
    read_judge_records,
    read_spotcheck,
    score_judgements,
    spotcheck_sample,
    write_judge_records,
)
from caregap.fakes import scripted_model
from caregap.measures.models import EvidenceRef, OpenGap, ReviewItem
from tests.unit.evals.helpers import AS_OF, MEREDITH, TONY

GROUNDED = json.dumps(
    {
        "claims": [
            {"claim": "an eye exam is due", "verdict": "supported", "citation_valid": True},
            {
                "claim": "the clinic phone is 555-0100",
                "verdict": "supported",
                "citation_valid": None,
            },
        ],
        "notes": "",
    }
)
CONTRADICTED = json.dumps(
    {
        "claims": [
            {"claim": "a colonoscopy was done last week", "verdict": "contradicted"},
            {"claim": "an eye exam is due", "verdict": "supported"},
        ],
        "notes": "",
    }
)
GARBAGE = "Sorry, I cannot grade that."

PLAN = CareActionPlan(
    gaps=[PlannedGap(measure_id="EED", rank=1, urgency="routine", rationale="due")],
    actions=[
        GapAction(action_id="a1", measure_id="EED", kind="screening", detail="Book an eye exam")
    ],
    patient_message="Hello, a diabetes eye exam is due. Reply STOP to opt out.",
    provider_note="EED open; no retinal exam in the window.",
)


def case(patient_id: str, split: str) -> OutreachCase:
    return OutreachCase(
        patient_id=patient_id,
        as_of=AS_OF,
        split=split,  # type: ignore[arg-type]
        plan=PLAN,
        passages=["Outreach context: as of 2025-12-31."],
    )


def record(patient_id: str, split: str, response: str) -> JudgeRecord:
    return JudgeRecord(
        case_key=judge_case_key(patient_id, AS_OF),
        patient_id=patient_id,
        as_of=AS_OF,
        split=split,  # type: ignore[arg-type]
        rubric_name=RUBRIC.name,
        rubric_sha256=RUBRIC.sha256,
        judge_model="fake-judge",
        response=response,
    )


def test_rubric_is_the_frozen_faithfulness_rubric() -> None:
    assert RUBRIC.name == RUBRIC_NAME == "caregap-faithfulness-v1"
    assert faithfulness_rubric(JUDGE_DOMAIN, name=RUBRIC_NAME) == RUBRIC
    assert JUDGE_DOMAIN in RUBRIC.text
    assert len(RUBRIC.sha256) == 64


def test_evidence_passages_carry_codes_and_dates_never_names() -> None:
    gaps = [
        OpenGap(measure_id="EED", priority_score=2.0, rank=2),
        OpenGap(
            measure_id="COL",
            subtype=None,
            priority_score=2.5,
            rank=1,
            evidence=[
                EvidenceRef(
                    section="procedures",
                    event_id="pr9",
                    code="73761001",
                    code_system="SNOMED",
                    display="Colonoscopy (Tony Doe)",
                    event_date=date(2015, 3, 2),
                    role="numerator",
                )
            ],
        ),
    ]
    items = [ReviewItem(measure_id="SPC", scope="measure", reason="E3 medication status")]
    passages = evidence_passages(
        gaps, items, as_of=AS_OF, clinic_name="Demo Primary Care", clinic_phone="555-0100"
    )
    assert passages[0].startswith("Outreach context: as of 2025-12-31; the clinic is Demo")
    assert passages[1].startswith("Open care gap #1: Colorectal Cancer Screening (COL)")
    assert "procedures 73761001 (SNOMED) on 2015-03-02" in passages[1]
    assert passages[2].startswith("Open care gap #2: Eye Exam for Patients With Diabetes (EED)")
    assert "no qualifying evidence in window" in passages[2]
    assert "Pending clinical review (measure)" in passages[3] and "SPC" not in passages[3][:20]
    assert "No outreach may be drafted" in passages[3]
    assert "Tony" not in " ".join(passages), "display strings never reach the judge"
    payload = build_outreach_judge_input(TONY, AS_OF, PLAN, passages)
    assert payload.startswith("PASSAGES:\n[1] Outreach context")
    assert "PROVIDER NOTE:" in payload and "PATIENT MESSAGE:" in payload


def test_judge_cases_records_the_fake_judge_in_patient_order(tmp_path: Path) -> None:
    model = scripted_model([GROUNDED, GARBAGE])
    records = judge_cases(
        [case(TONY, "test"), case(MEREDITH, "test")], model, judge_model_id="fake-judge"
    )
    assert [r.patient_id for r in records] == [MEREDITH, TONY], "sorted by patient id"
    assert records[0].response == GROUNDED and records[1].response == GARBAGE
    assert {r.rubric_sha256 for r in records} == {RUBRIC.sha256}
    assert records[1].case_key == f"judge:{TONY}:plan:2025-12-31:0"
    path = tmp_path / "recorded" / "judge.jsonl"
    write_judge_records(records, path)
    assert read_judge_records(path) == records
    assert b"\r" not in path.read_bytes()


def test_score_judgements_marks_parse_failures_without_flattering_defaults() -> None:
    records = [
        record(TONY, "test", GROUNDED),
        record(MEREDITH, "test", GARBAGE),
        record("dev-1", "dev", CONTRADICTED),
    ]
    report = score_judgements(records)
    by_id = {item.item_id: item.metrics for item in report.per_item}
    assert by_id[judge_case_key(TONY, AS_OF)] == {
        "citation_valid_ratio": 1.0,
        "contradiction_rate": 0.0,
        "faithfulness": 1.0,
        "judge_parse_failure": 0.0,
    }
    assert by_id[judge_case_key(MEREDITH, AS_OF)] == {"judge_parse_failure": 1.0}
    assert report.test_overall["judge_parse_failure"] == 0.5
    assert report.test_overall["faithfulness"] == 1.0
    assert report.per_split["dev"]["contradiction_rate"] == 0.5
    assert parse_record(records[1]) is None


def test_grounded_label_and_agreement(tmp_path: Path) -> None:
    grounded = parse_record(record(TONY, "test", GROUNDED))
    contradicted = parse_record(record(TONY, "test", CONTRADICTED))
    assert grounded is not None and grounded_label(grounded) == "grounded"
    assert contradicted is not None and grounded_label(contradicted) == "ungrounded"

    records = [record(TONY, "test", GROUNDED), record(MEREDITH, "test", CONTRADICTED)]
    assert judge_human_agreement(records, []) is None
    labels = [
        SpotcheckLabel(case_key=records[0].case_key, label="grounded", reviewer="rn"),
        SpotcheckLabel(case_key=records[1].case_key, label="grounded", reviewer="rn"),
        SpotcheckLabel(case_key="judge:ghost:plan:2025-12-31:0", label="grounded", reviewer="rn"),
    ]
    assert judge_human_agreement(records, labels) == 0.5
    assert judge_human_agreement([record(TONY, "test", GARBAGE)], labels[:1]) == 0.0

    path = tmp_path / "spotcheck.jsonl"
    assert read_spotcheck(path) == []
    path.write_text(
        "\n".join(json.dumps(label.model_dump()) for label in labels) + "\n", encoding="utf-8"
    )
    assert read_spotcheck(path) == labels


def test_spotcheck_sample_is_deterministic_and_stratified() -> None:
    records = [record(f"t{i}", "test", GROUNDED) for i in range(20)]
    records += [record(f"d{i}", "dev", GROUNDED) for i in range(10)]
    sample = spotcheck_sample(records)
    assert len(sample) == 12
    assert [r.case_key for r in sample] == [r.case_key for r in spotcheck_sample(records)]
    assert {r.split for r in sample} == {"dev", "test"}
    assert spotcheck_sample(records, n=3, seed=1) != spotcheck_sample(records, n=3, seed=2)
