"""Blind worksheets (SPEC section 6) are value-set-agnostic BY CONSTRUCTION.

``scripts/worksheet.py`` may not import anything from ``caregap.measures`` (AST guard: the
renderer cannot know what a value set or a rule is), it must list every event dated on or
before ``as_of`` — nothing tagged, nothing filtered by concept — and it must never leak a
value-set id or a verdict word onto the page.
"""

import ast
import gzip
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from caregap.p6.models import PatientRecord
from tests import factories
from tests.factories import EVAL_AS_OF

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "worksheet.py"
VALUE_SET_DIR = REPO / "src" / "caregap" / "measures" / "value_sets"

VERDICT_WORDS: tuple[str, ...] = (
    "gap_open",
    "needs_review",
    "not_eligible",
    "excluded",
    "escalate",
    "closed",
    "numerator",
    "denominator",
    "value set",
    "value_set",
    "eligible",
)


def load_worksheet_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worksheet_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def value_set_ids() -> set[str]:
    """Every id a value-set file declares (top-level ``id`` or ``sets[*].id``) plus file stems."""
    ids: set[str] = set()
    for path in sorted(VALUE_SET_DIR.glob("*.json")):
        ids.add(path.stem)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw.get("id"), str):
            ids.add(raw["id"])
        for entry in raw.get("sets", []):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.add(entry["id"])
    assert len(ids) > 10
    return ids


@pytest.fixture(scope="module")
def worksheet() -> ModuleType:
    return load_worksheet_module()


def sample_record() -> PatientRecord:
    """A small synthetic patient touching every section, with events the renderer must keep
    (on/before as_of), summarise (old observations) and drop (after as_of)."""
    panel = factories.bp_panel(effective=date(2025, 3, 15), encounter_id="enc-amb")
    return factories.build_record(
        patient_header=factories.patient(
            patient_id="ws-001", birth_date=date(1960, 6, 15), sex="female"
        ),
        as_of=EVAL_AS_OF,
        conditions=[
            factories.condition(
                "59621000",
                onset=date(2018, 3, 1),
                display="Essential hypertension (disorder)",
                condition_id="cond-htn",
            ),
            factories.condition(
                "44054006",
                onset=date(2010, 5, 5),
                abatement=date(2025, 6, 1),
                display="Diabetes mellitus type 2 (disorder)",
                condition_id="cond-dm",
            ),
            factories.condition(
                "999",
                onset=date(2020, 1, 1),
                display="Display with | a pipe",
                condition_id="cond-pipe",
            ),
        ],
        procedures=[
            factories.procedure(
                "71651007",
                performed=date(2024, 11, 2),
                display="Mammography (procedure)",
                procedure_id="proc-mammo",
            ),
            factories.procedure(
                "73761001",
                performed=date(2017, 4, 4),
                performed_end=date(2017, 4, 4),
                display="Colonoscopy (procedure)",
                procedure_id="proc-colo",
            ),
        ],
        medications=[
            factories.medication(
                "312961",
                authored=date(2025, 2, 1),
                display="simvastatin 20 MG Oral Tablet",
                status="stopped",
                medication_request_id="med-statin",
            )
        ],
        observations=[
            *panel,
            factories.observation(
                "72166-2",
                effective=date(2025, 3, 15),
                display="Tobacco smoking status",
                value_code="266919005",
                value_code_system="SNOMED",
                observation_id="obs-tobacco",
            ),
            factories.observation(
                "29463-7",
                effective=date(2018, 5, 5),
                display="Body Weight",
                value_num=70.5,
                unit="kg",
                observation_id="obs-old-1",
            ),
            factories.observation(
                "29463-7",
                effective=date(2019, 5, 5),
                display="Body Weight",
                value_num=71.0,
                unit="kg",
                observation_id="obs-old-2",
            ),
            factories.observation(
                "29463-7",
                effective=date(2026, 1, 15),
                display="Body Weight",
                value_num=72.0,
                unit="kg",
                observation_id="obs-future",
            ),
        ],
        encounters=[
            factories.encounter(
                start=date(2025, 3, 15),
                encounter_id="enc-amb",
                encounter_class="AMB",
                type_code="162673000",
                type_display="General examination of patient (procedure)",
                end_ts=datetime(2025, 3, 15, 10, 30),
            ),
            factories.encounter(
                start=date(2025, 7, 1),
                encounter_id="enc-imp",
                encounter_class="IMP",
                type_code="32485007",
                type_display="Hospital admission (procedure)",
            ),
        ],
    )


# --- import guard ---------------------------------------------------------------------------


def imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_worksheet_script_imports_nothing_from_caregap_measures() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        name
        for name in imported_modules(tree)
        if name == "caregap.measures" or name.startswith("caregap.measures.")
    ]
    assert offenders == [], f"worksheet.py must stay value-set-agnostic: {offenders}"


def test_worksheet_script_does_not_mention_value_set_ids_in_source() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    leaked = sorted(vs for vs in value_set_ids() if vs in source)
    assert leaked == []


# --- rendering ------------------------------------------------------------------------------


def test_rendered_worksheet_lists_every_event_and_no_value_set_id(worksheet: ModuleType) -> None:
    record = sample_record()
    text: str = worksheet.render_worksheet(record)

    # header
    for token in ("ws-001", EVAL_AS_OF.isoformat(), "female", "1960-06-15", "| 65 |"):
        assert token in text
    assert "none recorded" in text  # no death date

    # conditions / procedures / medications / encounters in full (codes, displays, dates)
    for token in (
        "59621000",
        "Essential hypertension (disorder)",
        "2018-03-01",
        "44054006",
        "2010-05-05",
        "2025-06-01",
        "Display with \\| a pipe",
        "71651007",
        "Mammography (procedure)",
        "2024-11-02",
        "73761001",
        "2017-04-04",
        "312961",
        "simvastatin 20 MG Oral Tablet",
        "2025-02-01",
        "stopped",
        "enc-amb",
        "enc-imp",
        "AMB",
        "IMP",
        "162673000 General examination of patient (procedure)",
        "2025-07-01",
    ):
        assert token in text, token

    # recent observations in full, BP components with their parent id and unit
    panel_parent = next(o for o in record.observations if o.code == factories.BP_PANEL)
    for token in (
        "85354-9",
        "8480-6",
        "8462-4",
        "128",
        "82",
        "mm[Hg]",
        panel_parent.observation_id,
    ):
        assert token in text, token
    assert "72166-2" in text and "266919005" in text

    # old observations summarised per (code, display): count, first, last
    assert "| 29463-7 | Body Weight | 2 | 2018-05-05 | 2019-05-05 |" in text
    assert "obs-old-1" not in text  # summarised rows carry no ids

    # nothing after as_of
    assert "obs-future" not in text
    assert "2026-01-15" not in text

    # no value-set ids, no verdict vocabulary
    lowered = text.lower()
    assert [vs for vs in value_set_ids() if vs.lower() in lowered] == []
    assert [w for w in VERDICT_WORDS if w in lowered] == []


def test_rendered_worksheet_is_deterministic_and_lf(worksheet: ModuleType) -> None:
    first: str = worksheet.render_worksheet(sample_record())
    factories.reset_ids()
    second: str = worksheet.render_worksheet(sample_record())
    assert first == second
    assert "\r" not in first
    assert first.endswith("\n")


def test_section_counts_match_events_on_or_before_as_of(worksheet: ModuleType) -> None:
    text: str = worksheet.render_worksheet(sample_record())
    assert "## Conditions (n=3)" in text
    assert "## Procedures (n=2)" in text
    assert "## Medications (n=1)" in text
    assert "## Observations (n=6)" in text  # 3 panel rows + tobacco + 2 old; future dropped
    assert "## Encounters (n=2)" in text
    assert "### Observations dated 2024-01-01 .. 2025-12-31 (n=4, listed in full)" in text
    assert "### Observations dated before 2024-01-01 (n=2, summarised per code)" in text


def test_main_renders_one_file_per_snapshot(worksheet: ModuleType, tmp_path: Path) -> None:
    record = sample_record()
    snapshots = tmp_path / "snapshots"
    folder = snapshots / record.patient.patient_id
    folder.mkdir(parents=True)
    payload: dict[str, Any] = factories.p6_payload(record)
    payload["patient"]["death_date"] = "2026-02-02"  # after as_of: masked at the boundary
    with gzip.open(folder / f"record_{EVAL_AS_OF.isoformat()}.json.gz", "wb") as fh:
        fh.write(json.dumps(payload).encode("utf-8"))
    (snapshots / "MANIFEST.json").write_text(
        json.dumps({"patient_ids": [record.patient.patient_id]}), encoding="utf-8"
    )
    out = tmp_path / "worksheets"
    assert worksheet.main(["--snapshots", str(snapshots), "--out", str(out)]) == 0
    written = sorted(out.glob("*.md"))
    assert [p.name for p in written] == [f"ws-001_{EVAL_AS_OF.isoformat()}.md"]
    text = written[0].read_bytes().decode("utf-8")
    assert "\r" not in text
    assert "2026-02-02" not in text and "none recorded" in text
    assert "Essential hypertension (disorder)" in text


def test_main_refuses_a_missing_manifest(worksheet: ModuleType, tmp_path: Path) -> None:
    assert worksheet.main(["--snapshots", str(tmp_path), "--out", str(tmp_path / "o")]) == 2
