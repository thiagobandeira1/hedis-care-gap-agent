"""Validator: the structured call (headers first, fail closed on garbage), the
``verify_verdict`` downgrade matrix, and the ``resolve_candidate`` table."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from caregap.agents.packet import (
    EvidencePacket,
    PacketEvidence,
    build_packet,
    ordered_rule_elements,
    packet_categories,
    render_packet,
    validator_case_key,
)
from caregap.agents.prompts import VALIDATOR_PROMPT_SHA, VALIDATOR_SYSTEM, request_header
from caregap.agents.schemas import ValidationVerdict
from caregap.agents.validator import (
    FINAL_ENGINE_VERDICTS,
    resolve_candidate,
    validate_candidate,
    verify_verdict,
)
from caregap.fakes import ReplayChatModel, parse_headers, scripted_model
from caregap.measures.context import MeasurementContext
from caregap.measures.engine import MeasureEngine
from caregap.measures.ids import MeasureId
from caregap.measures.models import MeasureEvaluation, Section, TriResult
from caregap.measures.rule_text import load_rule_text
from caregap.measures.rules import all_rules
from caregap.measures.tri import Tri, Verdict
from caregap.measures.value_sets import load_value_sets
from caregap.p6.client import mask_as_of
from caregap.p6.models import PatientRecord
from caregap.structured import AgentOutputError, StructuredCaller
from tests.factories import (
    EVAL_AS_OF,
    bp_panel,
    build_record,
    condition,
    medication,
    patient,
    procedure,
)

VALUE_SETS = load_value_sets()
ENGINE = MeasureEngine(all_rules(), VALUE_SETS)
CTX = MeasurementContext.for_(EVAL_AS_OF, date(1975, 6, 15))

HTN = "59621000"
ESRD = "46177005"
ASCVD = "414545008"
HOSPICE = "385763009"
ATORVASTATIN_20 = "617310"  # moderate

CASE_KEY = "validator:p1:CBP:2025-12-31:0"
VALID_JSON = json.dumps(
    {
        "measure_id": "CBP",
        "decision": "confirm_open",
        "exclusion_category": None,
        "evidence_ids": ["c1"],
        "rule_citation": "cbp/numerator/controlled",
        "confidence": "high",
        "rationale": "The abatement date looks like a coding artefact; the gap stands.",
    }
)
GARBAGE = "I cannot help with that."


# --- packet fixtures built by hand ---------------------------------------------------------


def row(
    event_id: str,
    section: Section,
    code: str | None,
    system: str | None,
    day: date | None,
    tags: list[str],
    *,
    status: str | None = None,
) -> PacketEvidence:
    return PacketEvidence(
        event_id=event_id,
        section=section,
        code=code,
        code_system=system,
        display=None,
        event_date=day,
        status=status,
        tags=tags,
    )


ROWS: list[PacketEvidence] = [
    row(
        "c1",
        "conditions",
        HTN,
        "SNOMED",
        date(2020, 1, 1),
        ["hypertension_snomed"],
        status="active",
    ),
    row("c2", "conditions", ESRD, "SNOMED", date(2021, 4, 1), ["esrd_snomed"], status="active"),
    row("o1", "observations", "85354-9", "LOINC", date(2025, 3, 15), ["bp_loinc"]),
    row("pr1", "procedures", HOSPICE, "SNOMED", date(2024, 11, 15), ["hospice_snomed"]),
    row("pr2", "procedures", HOSPICE, "SNOMED", date(2025, 2, 1), ["hospice_snomed"]),
    row("e1", "encounters", None, None, date(2025, 3, 15), [], status="AMB"),
]


def tri(value: Tri, *, subtype: str | None = None) -> TriResult:
    return TriResult(value=value, subtype=subtype, window_start=CTX.my_start, window_end=CTX.as_of)


def make_packet(
    *,
    engine_verdict: Verdict = "needs_review",
    denominator: Tri = "yes",
    numerator: Tri = "no",
    measure_id: MeasureId = "CBP",
    evidence: list[PacketEvidence] | None = None,
) -> EvidencePacket:
    return EvidencePacket(
        measure_id=measure_id,
        patient_id="p1",
        as_of=CTX.as_of,
        age_at_my_end=CTX.age_at_my_end,
        sex="female",
        my_start=CTX.my_start,
        my_end=CTX.my_end,
        retrospective=CTX.retrospective,
        engine_verdict=engine_verdict,
        denominator=tri(denominator),
        numerator=tri(numerator),
        rule_elements=ordered_rule_elements(load_rule_text(measure_id).elements),
        categories=packet_categories(measure_id, CTX),
        numerator_value_set_ids=["bp_loinc"],
        numerator_window_start=CTX.my_start,
        numerator_window_end=CTX.as_of,
        evidence=ROWS if evidence is None else evidence,
    )


def verdict(**overrides: Any) -> ValidationVerdict:
    fields: dict[str, Any] = {
        "measure_id": "CBP",
        "decision": "confirm_open",
        "exclusion_category": None,
        "evidence_ids": ["c1"],
        "rule_citation": "cbp/denominator/age",
        "confidence": "high",
        "rationale": "because",
    }
    fields.update(overrides)
    return ValidationVerdict.model_validate(fields)


# --- structured call -------------------------------------------------------------------------


class SpyModel(GenericFakeChatModel):
    """Scripted fake that records the exact prompt of every call."""

    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def spy(*outputs: str) -> SpyModel:
    return SpyModel(messages=iter(outputs))


def test_validate_candidate_returns_the_parsed_verdict_and_traces_the_case_key() -> None:
    packet = make_packet()
    caller = StructuredCaller()
    result = validate_candidate(packet, model=spy(VALID_JSON), caller=caller, model_id="fake")
    assert result.decision == "confirm_open"
    assert result.evidence_ids == ["c1"]
    assert result.verified is False  # only verify_verdict sets it
    assert [(t.schema, t.attempts, t.model_id, t.case_key) for t in caller.traces] == [
        ("ValidationVerdict", 1, "fake", CASE_KEY)
    ]


def test_request_header_lines_open_the_human_message() -> None:
    packet = make_packet()
    model = spy(VALID_JSON)
    validate_candidate(packet, model=model, caller=StructuredCaller(), model_id="fake")
    assert len(model.seen) == 1
    system, human = model.seen[0]
    assert isinstance(system, SystemMessage)
    assert system.content == VALIDATOR_SYSTEM
    assert isinstance(human, HumanMessage)
    assert isinstance(human.content, str)
    lines = human.content.splitlines()
    assert lines[0] == f"CASE_KEY:{CASE_KEY}"
    assert lines[1] == f"PROMPT_SHA:{VALIDATOR_PROMPT_SHA}"
    assert lines[2].startswith("VALIDATION PACKET: CBP")
    assert parse_headers(model.seen[0]) == (CASE_KEY, VALIDATOR_PROMPT_SHA)
    assert human.content == request_header(CASE_KEY, VALIDATOR_PROMPT_SHA) + render_packet(packet)


def test_garbage_twice_propagates_agent_output_error() -> None:
    caller = StructuredCaller()
    model = spy(GARBAGE, GARBAGE, VALID_JSON)
    with pytest.raises(AgentOutputError) as info:
        validate_candidate(make_packet(), model=model, caller=caller, model_id="fake")
    assert (info.value.schema, info.value.attempts) == ("ValidationVerdict", 2)
    assert len(model.seen) == 2  # the third, valid output is never consumed
    assert caller.traces[0].attempts == 2


def test_replay_model_keys_on_the_header_of_the_real_prompt(tmp_path: Path) -> None:
    packet = make_packet()
    path = tmp_path / "validator.jsonl"
    path.write_text(
        json.dumps(
            {"case_key": CASE_KEY, "prompt_sha": VALIDATOR_PROMPT_SHA, "response": VALID_JSON}
        )
        + "\n",
        encoding="utf-8",
    )
    model = ReplayChatModel(recordings_path=path, mode="strict")
    result = validate_candidate(packet, model=model, caller=StructuredCaller(), model_id="replay")
    assert result.decision == "confirm_open"
    assert (model.fallback_count, model.sha_drift_count) == (0, 0)


def test_case_key_follows_patient_measure_and_as_of() -> None:
    packet = make_packet(measure_id="SPC").model_copy(update={"patient_id": "abc"})
    caller = StructuredCaller()
    validate_candidate(
        packet,
        model=scripted_model([VALID_JSON.replace('"CBP"', '"SPC"')]),
        caller=caller,
        model_id="fake",
    )
    assert caller.traces[0].case_key == validator_case_key("abc", "SPC", EVAL_AS_OF)
    assert caller.traces[0].case_key == "validator:abc:SPC:2025-12-31:0"


# --- verify_verdict matrix -------------------------------------------------------------------

CLOSED = make_packet(engine_verdict="closed", numerator="yes")
NUMERATOR_YES = make_packet(engine_verdict="needs_review", numerator="yes")

MATRIX: list[tuple[str, EvidencePacket, ValidationVerdict, str, bool, str]] = [
    # id, packet, verdict, expected decision, expected verified, note fragment
    (
        "confirm_open_ok",
        make_packet(),
        verdict(confidence="medium"),
        "confirm_open",
        True,
        "verified",
    ),
    (
        "unknown_evidence_id",
        make_packet(),
        verdict(evidence_ids=["c1", "zzz"]),
        "needs_human",
        False,
        "unresolved evidence ids: zzz",
    ),
    (
        "exclude_verified",
        make_packet(),
        verdict(decision="exclude", exclusion_category="esrd", evidence_ids=["c2"]),
        "exclude",
        True,
        "verified",
    ),
    (
        "exclude_out_of_window",
        make_packet(),
        verdict(
            decision="exclude",
            exclusion_category="hospice_during_measurement_period",
            evidence_ids=["pr1"],
        ),
        "needs_human",
        False,
        "no cited event tagged hospice_snomed dated inside 2025-01-01..2025-12-31",
    ),
    (
        "exclude_in_window_hospice",
        make_packet(),
        verdict(
            decision="exclude",
            exclusion_category="hospice_during_measurement_period",
            evidence_ids=["pr1", "pr2"],
        ),
        "exclude",
        True,
        "verified",
    ),
    (
        "exclude_wrong_category",
        make_packet(),
        verdict(decision="exclude", exclusion_category="palliative_care", evidence_ids=["c2"]),
        "needs_human",
        False,
        "'palliative_care' is not a packet category",
    ),
    (
        "exclude_category_missing",
        make_packet(),
        verdict(decision="exclude", exclusion_category=None, evidence_ids=["c2"]),
        "needs_human",
        False,
        "None is not a packet category",
    ),
    (
        "exclude_untagged_event",
        make_packet(),
        verdict(decision="exclude", exclusion_category="esrd", evidence_ids=["c1"]),
        "needs_human",
        False,
        "no cited event tagged esrd_snomed",
    ),
    (
        "exclude_without_evidence",
        make_packet(),
        verdict(decision="exclude", exclusion_category="esrd", evidence_ids=[]),
        "needs_human",
        False,
        "no cited event tagged esrd_snomed",
    ),
    (
        "low_confidence",
        make_packet(),
        verdict(confidence="low"),
        "needs_human",
        False,
        "confidence low",
    ),
    (
        "closed_candidate_confirm_open",
        CLOSED,
        verdict(),
        "needs_human",
        False,
        "engine verdict closed admits only needs_human",
    ),
    (
        "closed_candidate_numerator_met",
        CLOSED,
        verdict(decision="numerator_met", evidence_ids=["o1"]),
        "needs_human",
        False,
        "engine verdict closed admits only needs_human",
    ),
    (
        "closed_candidate_needs_human",
        CLOSED,
        verdict(decision="needs_human", evidence_ids=[]),
        "needs_human",
        True,
        "needs_human as issued",
    ),
    (
        "numerator_met_untagged_event",
        NUMERATOR_YES,
        verdict(decision="numerator_met", evidence_ids=["e1"]),
        "needs_human",
        False,
        "no cited event tagged with a numerator value set",
    ),
    (
        "numerator_met_verified",
        NUMERATOR_YES,
        verdict(decision="numerator_met", evidence_ids=["o1"]),
        "numerator_met",
        True,
        "verified",
    ),
    (
        "numerator_met_engine_numerator_no",
        make_packet(numerator="no"),
        verdict(decision="numerator_met", evidence_ids=["o1"]),
        "needs_human",
        False,
        "numerator_met requires the engine numerator to be yes (it is no)",
    ),
    (
        "numerator_met_engine_numerator_unknown",
        make_packet(numerator="unknown"),
        verdict(decision="numerator_met", evidence_ids=["o1"]),
        "needs_human",
        False,
        "numerator_met requires the engine numerator to be yes (it is unknown)",
    ),
    (
        "measure_id_mismatch",
        make_packet(),
        verdict(measure_id="EED", rule_citation="eed/denominator/age"),
        "needs_human",
        False,
        "measure_id EED does not match packet CBP",
    ),
    (
        "confirm_open_denominator_unknown",
        make_packet(denominator="unknown"),
        verdict(),
        "needs_human",
        False,
        "confirm_open requires the engine denominator to be yes (it is unknown)",
    ),
    (
        "confirm_open_engine_numerator_yes",
        NUMERATOR_YES,
        verdict(),
        "needs_human",
        False,
        "confirm_open requires the engine numerator to be no (it is yes)",
    ),
    (
        "needs_human_as_issued",
        make_packet(),
        verdict(decision="needs_human", confidence="low", evidence_ids=["nope"]),
        "needs_human",
        True,
        "unresolved evidence ids: nope",
    ),
    (
        "citation_prefix_is_a_note_not_a_downgrade",
        make_packet(),
        verdict(rule_citation="C14 controlled"),
        "confirm_open",
        True,
        "rule_citation 'C14 controlled' does not start with 'cbp'",
    ),
    (
        "citation_unknown_element_is_a_note",
        make_packet(),
        verdict(rule_citation="cbp/exclusions/esrd"),
        "confirm_open",
        True,
        "is not a listed rule element id",
    ),
]


@pytest.mark.parametrize(
    ("packet", "given", "decision", "verified", "fragment"),
    [pytest.param(*case[1:], id=case[0]) for case in MATRIX],
)
def test_verify_verdict_matrix(
    packet: EvidencePacket,
    given: ValidationVerdict,
    decision: str,
    verified: bool,
    fragment: str,
) -> None:
    result = verify_verdict(given, packet)
    assert (result.decision, result.verified) == (decision, verified)
    assert result.verification_note is not None
    assert fragment in result.verification_note
    # The model's claim is preserved for the reviewer; only decision/verified/note change.
    assert result.evidence_ids == given.evidence_ids
    assert result.exclusion_category == given.exclusion_category
    assert result.rationale == given.rationale
    assert result.confidence == given.confidence
    assert given.verified is False and given.verification_note is None  # input untouched


def test_final_engine_verdicts_are_exactly_the_non_candidates() -> None:
    assert set(FINAL_ENGINE_VERDICTS) == {"closed", "excluded", "not_eligible"}


def test_multiple_problems_are_all_reported() -> None:
    result = verify_verdict(
        verdict(
            decision="exclude", exclusion_category="esrd", evidence_ids=["zzz"], confidence="low"
        ),
        make_packet(engine_verdict="excluded"),
    )
    assert result.decision == "needs_human"
    note = result.verification_note or ""
    for fragment in (
        "unresolved evidence ids: zzz",
        "confidence low",
        "engine verdict excluded admits only needs_human",
        "no cited event tagged esrd_snomed",
    ):
        assert fragment in note


# --- resolve_candidate table -----------------------------------------------------------------


def evaluation_with(verdict_value: Verdict, measure_id: MeasureId = "CBP") -> MeasureEvaluation:
    return MeasureEvaluation(
        measure_id=measure_id,
        rule_version="cbp-v1",
        denominator=tri("yes"),
        numerator=tri("no"),
        verdict=verdict_value,
    )


@pytest.mark.parametrize(
    ("engine_verdict", "given", "expected"),
    [
        ("gap_open", verdict(verified=True), "open"),
        ("needs_review", verdict(verified=True), "open"),
        ("needs_review", verdict(verified=False), "open"),  # confirm_open never needs verification
        (
            "needs_review",
            verdict(decision="exclude", exclusion_category="esrd", verified=True),
            "excluded",
        ),
        (
            "needs_review",
            verdict(decision="exclude", exclusion_category="esrd", verified=False),
            "needs_review",
        ),
        ("needs_review", verdict(decision="numerator_met", verified=True), "closed"),
        ("needs_review", verdict(decision="numerator_met", verified=False), "needs_review"),
        ("needs_review", verdict(decision="needs_human", verified=True), "needs_review"),
        ("needs_review", verdict(decision="needs_human", verified=False), "needs_review"),
        ("closed", verdict(verified=True), "needs_review"),  # can never add a gap
        ("not_eligible", verdict(decision="numerator_met", verified=True), "needs_review"),
        ("needs_review", verdict(measure_id="EED", verified=True), "needs_review"),
    ],
)
def test_resolve_candidate_table(
    engine_verdict: Verdict, given: ValidationVerdict, expected: str
) -> None:
    assert resolve_candidate(evaluation_with(engine_verdict), given) == expected


# --- end to end over engine-built packets --------------------------------------------------


def packet_from(
    record: PatientRecord, measure: MeasureId
) -> tuple[MeasureEvaluation, EvidencePacket]:
    masked = mask_as_of(record, CTX.as_of)
    evaluation = ENGINE.evaluate_one(masked, CTX, measure)
    packet = build_packet(
        evaluation,
        masked,
        CTX,
        patient_id="p1",
        rule_text=load_rule_text(measure),
        value_sets=VALUE_SETS,
    )
    return evaluation, packet


def test_e6_candidate_confirmed_open_end_to_end() -> None:
    record = build_record(
        patient_header=patient(birth_date=date(1975, 6, 15)),
        conditions=[condition(HTN, onset=date(2020, 1, 1), abatement=date(2025, 6, 1))],
        observations=bp_panel(effective=date(2025, 3, 15), sbp=150.0, dbp=95.0),
    )
    evaluation, packet = packet_from(record, "CBP")
    assert evaluation.verdict == "needs_review"
    raw = validate_candidate(
        packet, model=scripted_model([VALID_JSON]), caller=StructuredCaller(), model_id="fake"
    )
    checked = verify_verdict(raw, packet)
    assert (checked.decision, checked.verified) == ("confirm_open", True)
    assert resolve_candidate(evaluation, checked) == "open"


def test_e3_candidate_with_qualifying_statin_closes_only_with_a_tagged_in_window_citation() -> None:
    record = build_record(
        patient_header=patient(birth_date=date(1975, 6, 15)),
        conditions=[condition(ASCVD, onset=date(2019, 1, 1))],
        medications=[
            medication(ATORVASTATIN_20, authored=date(2025, 2, 1), status="active"),
            medication(ATORVASTATIN_20, authored=date(2025, 5, 1), status="stopped"),
        ],
    )
    evaluation, packet = packet_from(record, "SPC")
    assert evaluation.verdict == "needs_review"
    assert evaluation.numerator.value == "yes"
    assert {flag.kind for flag in evaluation.escalations} == {"E3"}

    def spc_verdict(*ids: str) -> ValidationVerdict:
        return verdict(
            measure_id="SPC",
            decision="numerator_met",
            evidence_ids=list(ids),
            rule_citation="spc/numerator/statin_moderate_or_high",
        )

    closed = verify_verdict(spc_verdict("m1"), packet)
    assert (closed.decision, closed.verified) == ("numerator_met", True)
    assert resolve_candidate(evaluation, closed) == "closed"

    # Citing the stopped request still passes the tag/window check (the engine numerator is
    # yes); citing an id that is not in the packet does not.
    assert verify_verdict(spc_verdict("m2"), packet).verified is True
    hallucinated = verify_verdict(spc_verdict("m3"), packet)
    assert (hallucinated.decision, hallucinated.verified) == ("needs_human", False)
    assert resolve_candidate(evaluation, hallucinated) == "needs_review"


def test_low_intensity_statin_cannot_be_closed_by_the_validator() -> None:
    simvastatin_10 = next(
        c.code for c in VALUE_SETS["statin_intensity"].codes if c.intensity == "low"
    )
    record = build_record(
        patient_header=patient(birth_date=date(1975, 6, 15)),
        conditions=[condition(ASCVD, onset=date(2019, 1, 1))],
        medications=[
            medication(simvastatin_10, authored=date(2025, 2, 1), status="active"),
            medication(simvastatin_10, authored=date(2025, 5, 1), status="stopped"),
        ],
    )
    evaluation, packet = packet_from(record, "SPC")
    assert evaluation.verdict == "needs_review"
    assert (evaluation.numerator.value, evaluation.numerator.subtype) == (
        "no",
        "low_intensity_only",
    )
    result = verify_verdict(
        verdict(
            measure_id="SPC",
            decision="numerator_met",
            evidence_ids=["m1"],
            rule_citation="spc/numerator/intensity",
        ),
        packet,
    )
    assert (result.decision, result.verified) == ("needs_human", False)
    assert "requires the engine numerator to be yes (it is no)" in (result.verification_note or "")
    assert resolve_candidate(evaluation, result) == "needs_review"


def test_prior_hospice_cannot_be_cited_as_hospice_during_the_my() -> None:
    record = build_record(
        patient_header=patient(birth_date=date(1975, 6, 15)),
        conditions=[condition(HTN, onset=date(2020, 1, 1))],
        observations=bp_panel(effective=date(2025, 3, 15), sbp=150.0, dbp=95.0),
        procedures=[procedure(HOSPICE, performed=date(2024, 11, 15))],
    )
    evaluation, packet = packet_from(record, "CBP")
    assert {flag.kind for flag in evaluation.escalations} == {"E1"}
    result = verify_verdict(
        verdict(
            decision="exclude",
            exclusion_category="hospice_during_measurement_period",
            evidence_ids=["pr1"],
            rule_citation="cbp/exclusion/hospice",
        ),
        packet,
    )
    assert (result.decision, result.verified) == ("needs_human", False)
    assert "dated inside 2025-01-01..2025-12-31" in (result.verification_note or "")
