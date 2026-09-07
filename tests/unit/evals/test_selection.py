"""The blind panel draw (SPEC section 6) is a pure function of descriptive facts + seed.

``scripts/gold.py select`` computes facts from the raw record only (no engine, no value set),
excludes patients over the labeling-feasibility cap, then draws a seeded stratified sample whose
strata counts and shortfalls are recorded next to the selection.
"""

import ast
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


# --- select-escalation: the targeted slice ---------------------------------------------------


def synthetic_trigger_facts(gold: ModuleType, size: int = 120) -> dict[str, Any]:
    """Deterministic pseudo-panel of trigger facts: every trigger except E3 / E5 has carriers."""
    facts: dict[str, Any] = {}
    for i in range(size):
        age = 55 + (i * 7) % 40
        facts[f"t{i:03d}"] = gold.TriggerFacts(
            patient_id=f"t{i:03d}",
            sex="female" if i % 2 else "male",
            birth_date=f"{2025 - age}-06-15",
            death_date=None,
            age=age,
            deceased=False,
            event_count=300 + (i * 137) % 4000,
            e1_evidence=("procedure 385763009 2024-11-15",) if i % 9 == 0 else (),
            e4_evidence=(
                (
                    "condition 26929004 onset 2015-01-01 abatement none",
                    "encounter IMP start 2025-03-03",
                )
                if i % 11 == 0
                else ()
            ),
            e6_evidence=(
                ("condition 59621000 onset 2019-01-01 abatement 2025-05-05",) if i % 13 == 0 else ()
            ),
            e7_evidence=("procedure 43075005 2021-01-01",) if i % 17 == 0 else (),
            hospice_in_my_evidence=("procedure 385763009 2025-02-02",) if i % 10 == 0 else (),
        )
    return facts


def test_select_escalation_is_a_pure_function_of_facts(gold: ModuleType) -> None:
    facts = synthetic_trigger_facts(gold)
    base = ["t000", "t009", "t011", "t050"]  # three carriers and one non-carrier
    first = gold.select_escalation(facts, as_of=EVAL_AS_OF, cap=2500, exclude=base)
    second = gold.select_escalation(facts, as_of=EVAL_AS_OF, cap=2500, exclude=base)
    assert first == second
    assert json.dumps(first.to_json(), sort_keys=True) == json.dumps(
        second.to_json(), sort_keys=True
    )
    reversed_facts = dict(reversed(list(facts.items())))
    assert gold.select_escalation(reversed_facts, as_of=EVAL_AS_OF, cap=2500, exclude=base) == first
    assert gold.select_escalation(facts, as_of=EVAL_AS_OF, cap=2500, exclude=set(base)) == first

    ids = first.patient_ids
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert not set(ids) & set(base)
    assert all(facts[pid].triggers for pid in ids)
    assert all(facts[pid].event_count <= 2500 for pid in ids)
    expected = sorted(
        pid for pid, f in facts.items() if f.triggers and f.event_count <= 2500 and pid not in base
    )
    assert ids == expected
    assert first.excluded_over_cap == sorted(
        pid for pid, f in facts.items() if f.triggers and f.event_count > 2500
    )
    assert first.excluded_in_base == sorted(
        pid for pid in base if facts[pid].triggers and facts[pid].event_count <= 2500
    )
    assert first.base_size == len(base) and first.panel_size == len(facts)
    for name in gold.TRIGGER_NAMES:
        count = first.counts[name]
        assert count.panel == sum(1 for f in facts.values() if name in f.triggers)
        assert count.selected == sum(1 for pid in ids if name in facts[pid].triggers)
        assert count.inside_cap == count.in_base + count.selected
    assert first.unrepresentable == ["E3", "E5"]
    payload = first.to_json()
    assert set(payload) >= {"as_of", "triggers", "patients", "counts", "unrepresentable"}
    assert payload["as_of"] == "2025-12-31"
    assert list(payload["triggers"]) == list(gold.TRIGGER_NAMES)
    for item in payload["patients"]:
        assert set(item) == {"patient_id", "triggers", "facts"}
        assert item["triggers"] == list(facts[item["patient_id"]].triggers)
        restored = {k: tuple(v) if isinstance(v, list) else v for k, v in item["facts"].items()}
        assert gold.TriggerFacts(**restored) == facts[item["patient_id"]]
    text: str = gold.render_feasibility_escalation(facts, first)
    assert "\r" not in text
    for token in ("## Carriers per trigger", "## Triggers with zero carriers", "`E3`", "`E5`"):
        assert token in text
    assert text == gold.render_feasibility_escalation(facts, first)


def test_trigger_codes_load_reads_the_value_set_files_as_data(gold: ModuleType) -> None:
    codes = gold.TriggerCodes.load()
    assert {"385763009", "305336008", "876882001"} <= codes.hospice
    assert {"26929004", "230265002"} <= codes.dementia
    assert {"43075005", "94260004"} <= codes.colon_ambiguous
    assert "59621000" in codes.hypertension and "44054006" in codes.diabetes
    assert "617312" in codes.statin and len(codes.statin) > 20


def test_trigger_facts_from_record_detects_every_trigger(gold: ModuleType) -> None:
    codes = gold.TriggerCodes.load()
    record = factories.build_record(
        patient_header=factories.patient(
            patient_id="tf-1", birth_date=date(1958, 2, 10), sex="female"
        ),
        conditions=[
            factories.condition("26929004", onset=date(2015, 1, 1)),
            factories.condition("59621000", onset=date(2019, 1, 1), abatement=date(2025, 5, 5)),
            factories.condition("44054006", onset=date(2010, 1, 1), abatement=date(2025, 7, 7)),
        ],
        procedures=[
            factories.procedure("385763009", performed=date(2024, 11, 15)),
            factories.procedure("385763009", performed=date(2025, 2, 2)),
            factories.procedure("43075005", performed=date(2021, 1, 1)),
        ],
        medications=[
            factories.medication("617312", authored=date(2025, 2, 1), status="stopped"),
            factories.medication("617312", authored=date(2025, 3, 1), status="Cancelled"),
        ],
        observations=[
            *factories.bp_panel(effective=date(2025, 5, 1), dbp=None, panel_id="panel-no-dbp"),
            *factories.bp_panel(effective=date(2025, 6, 1), unit="kPa", panel_id="panel-kpa"),
            *factories.bp_panel(effective=date(2025, 9, 14), panel_id="panel-ok"),
        ],
        encounters=[
            factories.encounter(start=date(2025, 4, 1), encounter_class="EMER"),
            factories.encounter(
                start=date(2025, 8, 1), encounter_class="IMP", type_code="305336008"
            ),
        ],
    )
    facts = gold.trigger_facts_from_record(factories.p6_payload(record), EVAL_AS_OF, codes)
    assert facts.patient_id == "tf-1" and facts.age == 67 and facts.deceased is False
    assert facts.triggers == gold.TRIGGER_NAMES
    assert facts.e1_evidence == ("procedure 385763009 2024-11-15",)
    assert facts.hospice_in_my_evidence == (
        "encounter type 305336008 start 2025-08-01",
        "procedure 385763009 2025-02-02",
    )
    assert facts.e3_evidence == (
        "medication 617312 authored 2025-02-01 status stopped",
        "medication 617312 authored 2025-03-01 status cancelled",
    )
    assert facts.e4_evidence == (
        "condition 26929004 onset 2015-01-01 abatement none",
        "encounter EMER start 2025-04-01",
        "encounter IMP start 2025-08-01",
    )
    assert facts.e5_evidence == (
        "panel panel-kpa 2025-06-01: 8480-6 unit 'kPa'; 8462-4 unit 'kPa'",
        "panel panel-no-dbp 2025-05-01: missing 8462-4",
    )
    assert facts.e6_evidence == (
        "condition 44054006 onset 2010-01-01 abatement 2025-07-07",
        "condition 59621000 onset 2019-01-01 abatement 2025-05-05",
    )
    assert facts.e7_evidence == ("procedure 43075005 2021-01-01",)
    assert facts.event_count == 3 + 3 + 2 + 8 + 2


def test_trigger_facts_ignore_near_misses(gold: ModuleType) -> None:
    codes = gold.TriggerCodes.load()

    def triggers_for(*, birth: date = date(1950, 1, 1), **parts: Any) -> tuple[str, ...]:
        header = factories.patient(patient_id="tf-2", birth_date=birth)
        record = factories.build_record(patient_header=header, **parts)
        facts = gold.trigger_facts_from_record(factories.p6_payload(record), EVAL_AS_OF, codes)
        result: tuple[str, ...] = facts.triggers
        return result

    # hospice four months before the E1 window, or after as_of: neither E1 nor hospice_in_my
    assert (
        triggers_for(
            procedures=[
                factories.procedure("385763009", performed=date(2024, 6, 1)),
                factories.procedure("385763009", performed=date(2026, 1, 5)),
            ]
        )
        == ()
    )
    # E1 window boundaries: 2024-10-03 is in, 2024-10-02 is out
    hospice_in = factories.procedure("385763009", performed=date(2024, 10, 3))
    hospice_out = factories.procedure("385763009", performed=date(2024, 10, 2))
    assert triggers_for(procedures=[hospice_in]) == ("E1",)
    assert triggers_for(procedures=[hospice_out]) == ()
    # E4 needs an active dementia dx AND an IMP/EMER encounter in the MY AND age >= 66
    dementia = factories.condition("26929004", onset=date(2015, 1, 1))
    abated = factories.condition("26929004", onset=date(2015, 1, 1), abatement=date(2020, 1, 1))
    imp = factories.encounter(start=date(2025, 3, 3), encounter_class="IMP")
    amb = factories.encounter(start=date(2025, 3, 3), encounter_class="AMB")
    imp_prior_year = factories.encounter(start=date(2024, 12, 30), encounter_class="IMP")
    assert triggers_for(conditions=[dementia], encounters=[imp]) == ("E4",)
    assert triggers_for(conditions=[abated], encounters=[imp]) == ()
    assert triggers_for(conditions=[dementia], encounters=[amb]) == ()
    assert triggers_for(conditions=[dementia], encounters=[imp_prior_year]) == ()
    assert triggers_for(conditions=[dementia], encounters=[imp], birth=date(1960, 6, 15)) == ()
    # E3 needs the MY and a conflicting status; E6 needs abatement inside the MY
    old_stop = factories.medication("617312", authored=date(2024, 2, 1), status="stopped")
    active = factories.medication("617312", authored=date(2025, 2, 1), status="active")
    assert triggers_for(medications=[old_stop]) == ()
    assert triggers_for(medications=[active]) == ()
    old_abatement = factories.condition(
        "59621000", onset=date(2019, 1, 1), abatement=date(2024, 5, 5)
    )
    assert triggers_for(conditions=[old_abatement]) == ()
    # E5: a complete mm[Hg] panel, or a defective panel outside the MY, is not E5
    assert triggers_for(observations=factories.bp_panel(effective=date(2025, 5, 1))) == ()
    old_defect = factories.bp_panel(effective=date(2024, 5, 1), dbp=None)
    assert triggers_for(observations=old_defect) == ()
    # E7: ordinary colon findings are not ambiguous
    assert (
        triggers_for(
            conditions=[factories.condition("68496003", onset=date(2021, 1, 1))],
            procedures=[factories.procedure("76164006", performed=date(2021, 1, 1))],
        )
        == ()
    )


# --- select-escalation: never the engine -----------------------------------------------------


def _reachable_from(tree: ast.Module, entry: str) -> dict[str, ast.AST]:
    """Module-level functions and classes statically reachable from ``entry`` by name."""
    defined: dict[str, ast.AST] = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.ClassDef)
    }
    reached: dict[str, ast.AST] = {}
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in reached or name not in defined:
            continue
        reached[name] = defined[name]
        for node in ast.walk(defined[name]):
            if isinstance(node, ast.Name) and node.id in defined and node.id not in reached:
                pending.append(node.id)
    return reached


def _module_imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_select_escalation_code_path_never_imports_the_engine_or_reads_its_outcomes() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    module_imports = _module_imports(tree)
    assert not [n for n in module_imports if n.startswith("caregap.measures")], module_imports

    reached = _reachable_from(tree, "cmd_select_escalation")
    assert {
        "cmd_select_escalation",
        "trigger_facts_from_record",
        "select_escalation",
        "render_feasibility_escalation",
        "TriggerCodes",
        "TriggerFacts",
    } <= set(reached)
    assert "golden_text" not in reached and "cmd_goldens" not in reached

    offenders: list[str] = []
    for name, node in reached.items():
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                offenders.extend(f"{name}: import {a.name}" for a in inner.names)
            elif isinstance(inner, ast.ImportFrom):
                offenders.append(f"{name}: from {inner.module}")
            elif isinstance(inner, ast.Attribute) and "engine" in inner.attr.lower():
                offenders.append(f"{name}: .{inner.attr}")
            elif isinstance(inner, ast.Name) and "engine" in inner.id.lower():
                offenders.append(f"{name}: {inner.id}")
            elif (
                isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and ("engine-outcomes" in inner.value or "default_engine" in inner.value)
            ):
                offenders.append(f"{name}: {inner.value!r}")
    # the only function-level imports on this path are P6 (the record source), never measures
    engine_touches = [o for o in offenders if "caregap.measures" in o or "engine" in o.lower()]
    assert engine_touches == [], engine_touches
