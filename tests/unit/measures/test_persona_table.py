"""Persona-level expectations over the committed P6 snapshots (SPEC sections 2, 5, 9).

Every assertion here was grounded in the snapshot evidence first (see the golden files):

* Kayce (b. 1953-06-29, d. 1954-05-08) died decades before either MY -> ``not_eligible`` on
  all eight measures at both anchors (global death rule, quoted).
* Tony (male, b. 1950-08-03; hypertension since 2000; T2DM onset 2025-12-25; simvastatin 10 mg
  authored 2025-01-03; no ASCVD dx): CBP closed on the 123/83 panel of 2025-12-25, EED open
  (no retinal exam), SPD closed on any-intensity therapy, SPC not eligible (no ASCVD) even
  though only low-intensity statin is present.
* Meredith (female, b. 1958-09-05): BCS open (no mammogram in the 27-month window), COL closed
  on colonoscopies in 2018 and 2023.
* Anti-leakage: at ``2024-12-31`` no evaluation may carry evidence dated after the anchor, and
  Tony's diabetes (onset 2025-12-25) must not exist yet, so EED / SPD are ``not_eligible``.
"""

from datetime import date

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import default_engine
from caregap.measures.ids import ALL_MEASURES, MeasureId
from caregap.measures.models import EvidenceRef, MeasureEvaluation
from caregap.p6.snapshot import SnapshotP6Client
from tests.unit.measures.test_goldens import ANCHORS, PERSONAS, snapshot_client

EVAL_AS_OF = date(2025, 12, 31)
PRIOR_AS_OF = date(2024, 12, 31)


def evaluate(
    client: SnapshotP6Client, patient_id: str, as_of: date
) -> dict[MeasureId, MeasureEvaluation]:
    record = client.get_record(patient_id, to=as_of)
    ctx = MeasurementContext.for_(as_of, record.patient.birth_date)
    return {e.measure_id: e for e in default_engine().evaluate(record, ctx)}


def all_evidence(evaluation: MeasureEvaluation) -> list[EvidenceRef]:
    refs = [*evaluation.denominator.evidence, *evaluation.numerator.evidence]
    for hit in evaluation.exclusions:
        refs.extend(hit.evidence)
    for flag in evaluation.escalations:
        refs.extend(flag.evidence)
    return refs


@pytest.fixture(scope="module")
def client() -> SnapshotP6Client:
    return snapshot_client()


# --- Kayce: deceased before the MY ---------------------------------------------------------


@pytest.mark.parametrize("as_of", ANCHORS, ids=[a.isoformat() for a in ANCHORS])
def test_kayce_not_eligible_everywhere(client: SnapshotP6Client, as_of: date) -> None:
    record = client.get_record(PERSONAS["Kayce"], to=as_of)
    assert record.patient.death_date == date(1954, 5, 8)
    evaluations = evaluate(client, PERSONAS["Kayce"], as_of)
    assert {m: e.verdict for m, e in evaluations.items()} == dict.fromkeys(
        ALL_MEASURES, "not_eligible"
    )
    for evaluation in evaluations.values():
        assert evaluation.denominator.value == "no"
        assert "died before the measurement year" in evaluation.denominator.reasons
        assert evaluation.exclusions == []
        assert evaluation.escalations == []
        assert evaluation.priority_score == 0.0


# --- Tony: CBP / EED / SPD ------------------------------------------------------------------


def test_tony_cbp_closed_on_controlled_panel(client: SnapshotP6Client) -> None:
    cbp = evaluate(client, PERSONAS["Tony"], EVAL_AS_OF)["CBP"]
    assert cbp.verdict == "closed"
    assert cbp.denominator.value == "yes"
    assert cbp.numerator.value == "yes"
    assert cbp.numerator.subtype is None
    codes = {ref.code for ref in cbp.numerator.evidence}
    assert codes == {"85354-9", "8480-6", "8462-4"}
    assert {ref.event_date for ref in cbp.numerator.evidence} == {date(2025, 12, 25)}
    assert cbp.priority_score == 0.0


def test_tony_eed_gap_open_without_retinal_exam(client: SnapshotP6Client) -> None:
    eed = evaluate(client, PERSONAS["Tony"], EVAL_AS_OF)["EED"]
    assert eed.verdict == "gap_open"
    assert eed.denominator.value == "yes"
    assert eed.numerator.value == "no"
    assert eed.numerator.evidence == []
    diabetes = [ref for ref in eed.denominator.evidence if ref.section == "conditions"]
    assert [(ref.code, ref.event_date) for ref in diabetes] == [("44054006", date(2025, 12, 25))]
    assert eed.priority_score == pytest.approx(2.0)


def test_tony_spd_closed_and_spc_not_eligible(client: SnapshotP6Client) -> None:
    evaluations = evaluate(client, PERSONAS["Tony"], EVAL_AS_OF)
    spd, spc = evaluations["SPD"], evaluations["SPC"]
    assert spd.verdict == "closed"
    assert spd.numerator.value == "yes"
    assert [(ref.code, ref.event_date) for ref in spd.numerator.evidence] == [
        ("314231", date(2025, 1, 3))
    ]
    # No ASCVD dx: SPC denominator is "no" (so SPD, the no-double-outreach measure, applies);
    # the numerator still records that only low-intensity therapy exists.
    assert spc.verdict == "not_eligible"
    assert spc.denominator.value == "no"
    assert spc.numerator.subtype == "low_intensity_only"


# --- Meredith: BCS / COL ---------------------------------------------------------------------


def test_meredith_bcs_open_and_col_closed(client: SnapshotP6Client) -> None:
    evaluations = evaluate(client, PERSONAS["Meredith"], EVAL_AS_OF)
    bcs, col = evaluations["BCS"], evaluations["COL"]
    assert bcs.verdict == "gap_open"
    assert bcs.numerator.value == "no"
    assert bcs.numerator.window_start == date(2023, 10, 1)
    assert bcs.numerator.evidence == []
    assert bcs.priority_score == pytest.approx(2.0)
    assert col.verdict == "closed"
    assert col.numerator.window_start == date(2016, 1, 1)
    assert [(ref.code, ref.event_date) for ref in col.numerator.evidence] == [
        ("73761001", date(2018, 9, 12)),
        ("73761001", date(2023, 9, 12)),
    ]


# --- Anti-leakage at the prior anchor ------------------------------------------------------


@pytest.mark.parametrize("name", list(PERSONAS), ids=list(PERSONAS))
def test_prior_anchor_carries_no_evidence_after_as_of(client: SnapshotP6Client, name: str) -> None:
    record = client.get_record(PERSONAS[name], to=PRIOR_AS_OF)
    assert record.as_of == PRIOR_AS_OF
    assert record.patient.death_date is None or record.patient.death_date <= PRIOR_AS_OF
    for evaluation in evaluate(client, PERSONAS[name], PRIOR_AS_OF).values():
        assert evaluation.numerator.window_end is None or (
            evaluation.numerator.window_end <= PRIOR_AS_OF
        )
        late = [
            ref
            for ref in all_evidence(evaluation)
            if ref.event_date is not None and ref.event_date > PRIOR_AS_OF
        ]
        assert late == [], f"{name}/{evaluation.measure_id} leaks evidence after {PRIOR_AS_OF}"


def test_tony_diabetes_onset_in_2025_is_invisible_at_2024(client: SnapshotP6Client) -> None:
    prior = evaluate(client, PERSONAS["Tony"], PRIOR_AS_OF)
    assert prior["EED"].verdict == "not_eligible"
    assert prior["SPD"].verdict == "not_eligible"
    assert all(ref.code != "44054006" for ref in prior["EED"].denominator.evidence)
    current = evaluate(client, PERSONAS["Tony"], EVAL_AS_OF)
    assert (current["EED"].verdict, current["SPD"].verdict) == ("gap_open", "closed")
