"""The blind panel draw (SPEC section 6) is a pure function of descriptive facts + seed.

``scripts/gold.py select`` computes facts from the raw record only (no engine, no value set),
excludes patients over the labeling-feasibility cap, then draws a seeded stratified sample whose
strata counts and shortfalls are recorded next to the selection.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests import factories
from tests.factories import EVAL_AS_OF

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "gold.py"


def load_gold_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gold_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gold() -> ModuleType:
    return load_gold_module()


def synthetic_facts(gold: ModuleType, size: int = 150) -> dict[str, Any]:
    """Deterministic pseudo-panel: every field is a function of the index."""
    facts: dict[str, Any] = {}
    for i in range(size):
        age = 50 + (i * 7) % 45
        deceased = i % 40 == 0
        facts[f"p{i:03d}"] = gold.PatientFacts(
            patient_id=f"p{i:03d}",
            sex="female" if i % 2 else "male",
            birth_date=f"{2025 - age}-06-15",
            death_date="2025-05-01" if deceased else None,
            age=age,
            deceased=deceased,
            event_count=300 + (i * 137) % 4000,
            hypertension=i % 3 == 0,
            diabetes=i % 4 == 0,
            ascvd=i % 5 == 0,
            statin=i % 5 == 0,
            statin_stopped=i % 25 == 0,
            mammogram=i % 6 == 0,
            colonoscopy=i % 2 == 0,
            fobt_fit=i % 10 == 0,
            eye_exam=i % 8 == 0,
            hospice=i % 9 == 0,
            dementia=i % 11 == 0,
            esrd_dialysis=i % 30 == 0,
            bp_panel_in_my=i % 7 != 0,
            tobacco_screen_in_my=True,
            prapare_in_my=i % 13 != 0,
            encounter_in_my=True,
            inpatient_or_ed_in_my=i % 4 == 1,
        )
    return facts


# --- the draw --------------------------------------------------------------------------------


def test_selection_is_a_pure_function_of_facts_and_seed(gold: ModuleType) -> None:
    facts = synthetic_facts(gold)
    first = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=20260903)
    second = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=20260903)
    assert first == second
    assert json.dumps(first.to_json(), sort_keys=True) == json.dumps(
        second.to_json(), sort_keys=True
    )
    # input dict order is irrelevant: ids are sorted before the draw
    reversed_facts = dict(reversed(list(facts.items())))
    assert gold.select_patients(reversed_facts, as_of=EVAL_AS_OF, n=60, seed=20260903) == first
    # another seed draws another panel
    other = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=1)
    assert {p.patient_id for p in other.patients} != {p.patient_id for p in first.patients}


def test_selection_honours_n_cap_and_strata_minima(gold: ModuleType) -> None:
    facts = synthetic_facts(gold)
    cap = 2500
    selection = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=20260903, cap=cap)
    ids = [p.patient_id for p in selection.patients]
    assert len(ids) == 60 and ids == sorted(ids) and len(set(ids)) == 60
    assert selection.excluded_over_cap == sorted(
        pid for pid, f in facts.items() if f.event_count > cap
    )
    assert not set(ids) & set(selection.excluded_over_cap)
    assert selection.panel_size == len(facts)

    strata = {s.name: s for s in gold.STRATA}
    assert list(selection.strata) == list(strata)
    for name, report in selection.strata.items():
        members = [p for p in selection.patients if strata[name].member(p.facts)]
        assert report.selected == len(members)
        assert report.shortfall == max(0, report.minimum - report.selected)
        if report.available >= report.minimum:
            assert report.shortfall == 0, name
    for patient in selection.patients:
        assert patient.stratum == gold.FILLER or strata[patient.stratum].member(patient.facts)
    assert selection.filler_count == sum(1 for p in selection.patients if p.stratum == gold.FILLER)


def test_selection_records_shortfalls_when_the_panel_is_small(gold: ModuleType) -> None:
    facts = synthetic_facts(gold, size=12)
    selection = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=7)
    inside_cap = [pid for pid, f in facts.items() if f.event_count <= gold.DEFAULT_CAP]
    assert [p.patient_id for p in selection.patients] == sorted(inside_cap)
    assert any("panel allows only" in note for note in selection.notes)
    assert any(report.shortfall > 0 for report in selection.strata.values())
    assert any("< minimum" in note for note in selection.notes)


def test_selection_json_layout(gold: ModuleType) -> None:
    facts = synthetic_facts(gold, size=80)
    payload = gold.select_patients(facts, as_of=EVAL_AS_OF, n=20, seed=3).to_json()
    assert payload["as_of"] == "2025-12-31" and payload["seed"] == 3 and payload["n"] == 20
    assert set(payload) >= {"strata", "patients", "excluded_over_cap", "cap", "notes"}
    for item in payload["patients"]:
        assert set(item) == {"patient_id", "stratum", "facts"}
        assert item["facts"]["patient_id"] == item["patient_id"]
        assert gold.PatientFacts(**item["facts"]) == facts[item["patient_id"]]
    for report in payload["strata"].values():
        assert set(report) >= {"target", "minimum", "available", "drawn", "selected", "shortfall"}


def test_feasibility_report_mentions_cap_strata_and_shortfalls(gold: ModuleType) -> None:
    facts = synthetic_facts(gold, size=40)
    selection = gold.select_patients(facts, as_of=EVAL_AS_OF, n=60, seed=5)
    text: str = gold.render_feasibility(facts, selection)
    assert "\r" not in text
    for token in ("Labeling-feasibility cap", "## Strata", "## Shortfalls", "escalation_carrier"):
        assert token in text
    for note in selection.notes:
        assert note in text
    assert text == gold.render_feasibility(facts, selection)


# --- descriptive facts ---------------------------------------------------------------------


def test_age_at_my_end_uses_dec_31_of_the_measurement_year(gold: ModuleType) -> None:
    assert gold.age_at_my_end(date(1960, 6, 15), date(2025, 12, 31)) == 65
    assert gold.age_at_my_end(date(1960, 6, 15), date(2026, 6, 30)) == 66
    assert gold.age_at_my_end(date(2008, 1, 1), date(2025, 12, 31)) == 17
    assert gold.age_at_my_end(None, date(2025, 12, 31)) is None


def test_facts_from_record_reads_display_strings_only(gold: ModuleType) -> None:
    record = factories.build_record(
        patient_header=factories.patient(
            patient_id="fx-1", birth_date=date(1958, 2, 10), sex="female"
        ),
        conditions=[
            factories.condition("44054006", display="Diabetes mellitus type 2 (disorder)"),
            factories.condition("59621000", display="Essential hypertension (disorder)"),
            factories.condition("22298006", display="Myocardial infarction (disorder)"),
            factories.condition("26929004", display="Alzheimer's disease (disorder)"),
        ],
        procedures=[
            factories.procedure("385763009", display="Hospice care (regime/therapy)"),
            factories.procedure("71651007", display="Mammography (procedure)"),
            factories.procedure(
                "104435004", display="Screening for occult blood in feces (procedure)"
            ),
            factories.procedure("722161008", display="Diabetic retinal eye exam (procedure)"),
            factories.procedure("265764009", display="Renal dialysis (procedure)"),
            factories.procedure(
                "73761001", display="Colonoscopy (procedure)", performed=date(2026, 3, 3)
            ),
        ],
        medications=[
            factories.medication(
                "312961", display="simvastatin 20 MG Oral Tablet", status="stopped"
            )
        ],
        observations=[
            *factories.bp_panel(effective=date(2025, 4, 1)),
            factories.observation("72166-2", effective=date(2024, 4, 1), value_code="8517006"),
        ],
        encounters=[factories.encounter(start=date(2025, 4, 1), encounter_class="EMER")],
    )
    payload = factories.p6_payload(record)
    payload["patient"]["death_date"] = "2026-01-10"  # after as_of: not deceased for this run
    facts = gold.facts_from_record(payload, EVAL_AS_OF)
    assert facts.patient_id == "fx-1" and facts.sex == "female" and facts.age == 67
    assert facts.deceased is False and facts.death_date is None
    assert facts.event_count == 4 + 5 + 1 + 4 + 1  # the 2026 colonoscopy is not counted
    assert facts.diabetes and facts.hypertension and facts.ascvd and facts.dementia
    assert facts.hospice and facts.mammogram and facts.fobt_fit and facts.eye_exam
    assert facts.esrd_dialysis and facts.statin and facts.statin_stopped
    assert facts.colonoscopy is False  # dated after as_of
    assert facts.bp_panel_in_my and facts.encounter_in_my and facts.inpatient_or_ed_in_my
    assert facts.tobacco_screen_in_my is False  # 2024, not the MY
    assert facts.prapare_in_my is False
    assert gold.is_escalation_carrier(facts)


def test_facts_ignore_prediabetes_and_see_death_on_or_before_as_of(gold: ModuleType) -> None:
    record = factories.build_record(
        patient_header=factories.patient(
            patient_id="fx-2", birth_date=date(1950, 1, 1), death_date=date(2025, 8, 8)
        ),
        conditions=[factories.condition("714628002", display="Prediabetes (finding)")],
        medications=[
            factories.medication("312961", display="simvastatin 20 MG Oral Tablet", status="active")
        ],
    )
    facts = gold.facts_from_record(factories.p6_payload(record), EVAL_AS_OF)
    assert facts.diabetes is False
    assert facts.deceased is True and facts.death_date == "2025-08-08"
    assert facts.statin is True and facts.statin_stopped is False
    assert facts.bp_panel_in_my is False and facts.encounter_in_my is False
    assert gold.is_edge_case(facts)
    assert gold.is_escalation_carrier(facts) is False
