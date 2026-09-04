"""Integrity tests for the committed value sets (P1 sets + vendored P6 sets).

* the loader reads every ``value_sets/*.json`` without duplicate ids; file stems match ids;
* every code in a P1 set is grounded in ``SCAN.md`` unless flagged ``untested_by_data``;
* sources stay inside the allowlist (P6 provenance only through ``p6_vendored.json``);
* ``statin_intensity`` covers every vendored ``statin_rxnorm`` code with a dose-derived intensity
  that agrees with an independent reading of the public ACC/AHA table;
* the vendored P6 sets equal the installed ``fhir_features`` package's members, set by set;
* ``scripts/scan_codes.py`` keeps / drops rows the way ``SCAN.md`` documents.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest

from caregap.measures.value_sets import (
    StatinIntensity,
    ValueSets,
    ValueSetSource,
    load_value_sets,
)

VALUE_SETS_DIR = resources.files("caregap.measures") / "value_sets"
SCAN_PATH = Path(__file__).resolve().parents[3] / "scripts" / "scan_codes.py"
P6_SOURCE = "p6-valuesets-2026.08"
P6_CODE_SYSTEMS = frozenset({"SNOMED", "LOINC", "RXNORM", "CVX"})

P1_SET_IDS = frozenset(
    {
        "hospice_snomed",
        "esrd_snomed",
        "dialysis_snomed",
        "kidney_transplant_snomed",
        "colorectal_cancer_snomed",
        "colon_ambiguous_snomed",
        "pregnancy_snomed",
        "pregnancy_trap_snomed",
        "dementia_snomed",
        "dementia_meds_rxnorm",
        "diabetic_retinopathy_snomed",
        "retinopathy_negative_loinc",
        "prediabetes_trap_snomed",
        "sdoh_screening_loinc",
        "tobacco_answers_snomed",
        "statin_intensity",
        # not_representable stubs (one file each; the loader reads one set per file)
        "mastectomy_bilateral",
        "total_colectomy",
        "cirrhosis",
        "myopathy",
        "palliative",
    }
)

# --- SCAN.md parsing ----------------------------------------------------------------------


def _split_row(line: str) -> list[str]:
    body = line.strip()
    assert body.startswith("|") and body.endswith("|"), line
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", body[1:-1])]


def _parse_scan(text: str) -> dict[tuple[str, str], int]:
    """``{(system, code): total count}`` over every data row of a scan table."""
    counts: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = _split_row(line)
        if len(cells) != 5 or cells[0] in {"system", "---"}:
            continue
        key = (cells[0], cells[1])
        counts[key] = counts.get(key, 0) + int(cells[4])
    return counts


@pytest.fixture(scope="module")
def value_sets() -> ValueSets:
    return load_value_sets()


@pytest.fixture(scope="module")
def scan() -> dict[tuple[str, str], int]:
    return _parse_scan((VALUE_SETS_DIR / "SCAN.md").read_text(encoding="utf-8"))


# --- loader -------------------------------------------------------------------------------


def test_loader_reads_every_file_with_unique_ids(value_sets: ValueSets) -> None:
    names = sorted(e.name for e in VALUE_SETS_DIR.iterdir() if e.name.endswith(".json"))
    single = [n for n in names if n != "p6_vendored.json"]
    assert single, "no P1 value-set files"
    for name in single:
        raw = json.loads((VALUE_SETS_DIR / name).read_text(encoding="utf-8"))
        assert raw["id"] == name.removesuffix(".json"), f"{name}: id must equal the file stem"
    vendored = json.loads((VALUE_SETS_DIR / "p6_vendored.json").read_text(encoding="utf-8"))
    all_ids = [s["id"] for s in vendored["sets"]] + [n.removesuffix(".json") for n in single]
    assert len(all_ids) == len(set(all_ids)), "duplicate value-set id across files"
    assert set(value_sets.sets) == set(all_ids)
    assert value_sets.version == "2026.08"


def test_every_p1_set_is_present(value_sets: ValueSets) -> None:
    assert set(value_sets.sets) >= P1_SET_IDS, P1_SET_IDS - set(value_sets.sets)


def test_no_duplicate_codes_within_a_set(value_sets: ValueSets) -> None:
    for vs in value_sets.sets.values():
        codes = [c.code for c in vs.codes]
        assert len(codes) == len(set(codes)), vs.id


def test_code_systems_use_p6_tokens(value_sets: ValueSets) -> None:
    for vs in value_sets.sets.values():
        assert vs.code_system in P6_CODE_SYSTEMS, (vs.id, vs.code_system)


def test_unknown_set_id_raises(value_sets: ValueSets) -> None:
    with pytest.raises(KeyError, match="unknown value set"):
        value_sets["no_such_set"]


# --- scan grounding -----------------------------------------------------------------------


def test_scan_table_is_present_and_non_trivial(scan: dict[tuple[str, str], int]) -> None:
    assert len(scan) > 50
    assert scan[("SNOMED", "385763009")] > 0  # hospice procedure
    assert scan[("LOINC", "72166-2")] > 0  # tobacco status
    assert scan[("RXNORM", "314231")] > 0  # simvastatin 10


def test_every_grounded_p1_code_appears_in_scan(
    value_sets: ValueSets, scan: dict[tuple[str, str], int]
) -> None:
    ungrounded = [
        (vs.id, vs.code_system, c.code)
        for vs in value_sets.sets.values()
        if vs.source != P6_SOURCE
        for c in vs.codes
        if not c.untested_by_data and (vs.code_system, c.code) not in scan
    ]
    assert ungrounded == []


def test_untested_or_empty_sets_carry_a_note(value_sets: ValueSets) -> None:
    for vs in value_sets.sets.values():
        if vs.source == P6_SOURCE:
            continue
        if not vs.codes or any(c.untested_by_data for c in vs.codes):
            assert vs.note, f"{vs.id}: untested / empty set needs a note"


def test_stubs_are_untested_only(value_sets: ValueSets) -> None:
    for set_id in (
        "mastectomy_bilateral",
        "total_colectomy",
        "cirrhosis",
        "myopathy",
        "palliative",
    ):
        vs = value_sets[set_id]
        assert vs.source == "public-cms"
        assert all(c.untested_by_data for c in vs.codes), set_id


# --- sources ------------------------------------------------------------------------------


def test_sources_are_allowlisted(value_sets: ValueSets) -> None:
    allowed = set(get_args(ValueSetSource))
    vendored = json.loads((VALUE_SETS_DIR / "p6_vendored.json").read_text(encoding="utf-8"))
    vendored_ids = {s["id"] for s in vendored["sets"]}
    for vs in value_sets.sets.values():
        assert vs.source in allowed, (vs.id, vs.source)
        if vs.id in vendored_ids:
            assert vs.source == P6_SOURCE, vs.id
        else:
            assert vs.source != P6_SOURCE, f"{vs.id}: P6 provenance only via p6_vendored.json"
    assert value_sets["statin_intensity"].source == "public-acc-aha"


# --- statin intensity ---------------------------------------------------------------------

_STATIN_DOSE = re.compile(
    r"(?P<drug>simvastatin|atorvastatin|rosuvastatin|pravastatin|lovastatin|pitavastatin"
    r"|fluvastatin)\D+?(?P<mg>\d+(?:\.\d+)?) MG",
    re.IGNORECASE,
)
# (highest low-intensity dose, lowest high-intensity dose) per the public ACC/AHA table.
_DOSE_BANDS: dict[str, tuple[float, float]] = {
    "simvastatin": (10, 80),
    "atorvastatin": (0, 40),
    "rosuvastatin": (0, 20),
    "pravastatin": (20, math.inf),
    "lovastatin": (20, math.inf),
    "pitavastatin": (0, math.inf),
    "fluvastatin": (40, math.inf),
}


def _expected_intensity(display: str) -> StatinIntensity:
    match = _STATIN_DOSE.search(display)
    assert match, f"not a statin display: {display!r}"
    low_max, high_min = _DOSE_BANDS[match["drug"].lower()]
    mg = float(match["mg"])
    if mg <= low_max:
        return "low"
    if mg >= high_min:
        return "high"
    return "moderate"


def test_statin_intensity_covers_vendored_statin_set(value_sets: ValueSets) -> None:
    intensity = value_sets["statin_intensity"]
    statins = value_sets["statin_rxnorm"]
    assert intensity.code_system == statins.code_system == "RXNORM"
    assert statins.code_set - intensity.code_set == frozenset()
    vendored_display = {c.code: c.display for c in statins.codes}
    for c in intensity.codes:
        assert c.display, c.code
        assert c.intensity is not None, c.code
        assert c.intensity == _expected_intensity(c.display), (c.code, c.display, c.intensity)
        if c.code in vendored_display:
            assert c.display == vendored_display[c.code], c.code


def test_statin_intensity_lookups(value_sets: ValueSets) -> None:
    intensity = value_sets["statin_intensity"]
    assert intensity.intensity_of("314231") == "low"  # simvastatin 10
    assert intensity.intensity_of("312961") == "moderate"  # simvastatin 20
    assert intensity.intensity_of("259255") == "high"  # atorvastatin 80
    assert intensity.intensity_of("476350") == "moderate"  # ezetimibe / simvastatin 40
    assert intensity.intensity_of("not-a-code") is None
    assert value_sets["esrd_snomed"].intensity_of("46177005") is None


@pytest.mark.parametrize(
    ("display", "expected"),
    [
        ("simvastatin 10 MG Oral Tablet", "low"),
        ("simvastatin 80 MG Oral Tablet", "high"),
        ("atorvastatin 10 MG Oral Tablet [Lipitor]", "moderate"),
        ("rosuvastatin calcium 20 MG Oral Tablet [Crestor]", "high"),
        ("pravastatin sodium 80 MG Oral Tablet", "moderate"),
        ("lovastatin 20 MG Oral Tablet", "low"),
        ("pitavastatin calcium 2 MG Oral Tablet [Livalo]", "moderate"),
        ("fluvastatin 80 MG Extended Release Oral Tablet", "moderate"),
        ("ezetimibe 10 MG / simvastatin 20 MG Oral Tablet", "moderate"),
    ],
)
def test_acc_aha_oracle(display: str, expected: StatinIntensity) -> None:
    assert _expected_intensity(display) == expected


# --- P6 parity ----------------------------------------------------------------------------


def test_vendored_p6_sets_match_installed_package() -> None:
    from fhir_features.features.value_sets import load_value_sets as load_p6

    bundle = load_p6()
    raw = json.loads((VALUE_SETS_DIR / "p6_vendored.json").read_text(encoding="utf-8"))
    assert raw["version"] == bundle.version
    installed: dict[str, set[tuple[str, str, str]]] = {}
    for member in bundle.members:
        installed.setdefault(member.value_set_id, set()).add(
            (member.code_system, member.code, member.display)
        )
    vendored: dict[str, set[tuple[str, str, str]]] = {}
    for spec in raw["sets"]:
        assert spec["source"] == P6_SOURCE, spec["id"]
        vendored[spec["id"]] = {
            (spec["code_system"], c["code"], c["display"]) for c in spec["codes"]
        }
    assert set(vendored) == set(installed)
    for set_id, members in installed.items():
        assert vendored[set_id] == members, set_id


# --- scan script --------------------------------------------------------------------------


def _load_scan_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_codes", SCAN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _concept(system: str, code: str, display: str) -> dict[str, object]:
    return {"coding": [{"system": system, "code": code, "display": display}], "text": display}


SNOMED_URI = "http://snomed.info/sct"
LOINC_URI = "http://loinc.org"
RXNORM_URI = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _bundle() -> dict[str, object]:
    resources_ = [
        {"resourceType": "Condition", "code": _concept(SNOMED_URI, "46177005", "End-stage renal")},
        {"resourceType": "Condition", "code": _concept(SNOMED_URI, "10509002", "Acute bronchitis")},
        {"resourceType": "Procedure", "code": _concept(SNOMED_URI, "385763009", "Hospice care")},
        {
            "resourceType": "MedicationRequest",
            "medicationCodeableConcept": _concept(RXNORM_URI, "314231", "simvastatin 10 MG"),
        },
        {
            "resourceType": "Observation",
            "code": _concept(LOINC_URI, "71490-7", "Left eye Diabetic retinopathy severity"),
            "valueCodeableConcept": _concept(LOINC_URI, "LA18643-9", "No"),
        },
        {
            "resourceType": "Observation",
            "code": _concept(LOINC_URI, "93025-5", "PRAPARE"),
            "component": [
                {
                    "code": _concept(LOINC_URI, "71802-3", "Housing status"),
                    "valueCodeableConcept": _concept(LOINC_URI, "LA30189-7", "I have housing"),
                }
            ],
        },
        {
            "resourceType": "Observation",
            "code": _concept(LOINC_URI, "8302-2", "Body Height"),
            "valueQuantity": {"value": 170},
        },
        {
            "resourceType": "Encounter",
            "type": [_concept(SNOMED_URI, "305336008", "Admission to hospice")],
        },
        {"resourceType": "Patient", "id": "p1"},
    ]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"resource": r} for r in resources_],
    }


def test_scan_script_keeps_keyword_rows_and_answer_context(tmp_path: Path) -> None:
    scan_codes = _load_scan_script()
    bundle_path = tmp_path / "one.json"
    bundle_path.write_text(json.dumps(_bundle()), encoding="utf-8")
    bundle_path.with_name("two.json").write_text(json.dumps(_bundle()), encoding="utf-8")
    pattern = re.compile("|".join(scan_codes.KEYWORDS), re.IGNORECASE)
    counts = scan_codes.scan([bundle_path, bundle_path.with_name("two.json")], pattern)
    rows = {(system, code, resource): n for (system, code, _d, resource), n in counts.items()}
    assert rows == {
        ("SNOMED", "46177005", "Condition"): 2,
        ("SNOMED", "385763009", "Procedure"): 2,
        ("RXNORM", "314231", "MedicationRequest"): 2,
        ("LOINC", "71490-7", "Observation"): 2,
        ("LOINC", "LA18643-9", "Observation.value"): 2,  # kept via the parent's display
        ("LOINC", "93025-5", "Observation"): 2,
        ("LOINC", "71802-3", "Observation.component"): 2,
        ("LOINC", "LA30189-7", "Observation.component.value"): 2,
        ("SNOMED", "305336008", "Encounter.type"): 2,
    }
    text = scan_codes.render(counts, directory=tmp_path, n_bundles=2, filtered=True)
    parsed = _parse_scan(text)
    assert parsed[("SNOMED", "385763009")] == 2
    assert parsed[("LOINC", "LA18643-9")] == 2
    assert ("SNOMED", "10509002") not in parsed
    assert "over 2 Synthea FHIR R4 bundles" in text


def test_scan_script_main_writes_out_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scan_codes = _load_scan_script()
    (tmp_path / "one.json").write_text(json.dumps(_bundle()), encoding="utf-8")
    out = tmp_path / "SCAN.md"
    assert scan_codes.main(["--dir", str(tmp_path), "--out", str(out), "--no-filter"]) == 0
    parsed = _parse_scan(out.read_text(encoding="utf-8"))
    assert ("SNOMED", "10509002") in parsed  # unfiltered keeps the bronchitis row
    assert ("LOINC", "8302-2") in parsed
    assert "Unfiltered." in out.read_text(encoding="utf-8")
    assert scan_codes.main(["--dir", str(tmp_path / "empty")]) == 2
    assert "no *.json bundles" in capsys.readouterr().err
