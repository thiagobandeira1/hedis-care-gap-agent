"""Blind labeling worksheets (SPEC section 6): one value-set-agnostic Markdown page per patient.

::

    uv run python scripts/worksheet.py --snapshots evals/gold/snapshots --out evals/gold/worksheets

Reads the raw P6 snapshots written by ``scripts/gold.py snapshot`` (``MANIFEST.json`` plus
``<pid>/record_<as_of>.json.gz``) and renders, per (patient, as_of), a header (patient id,
as_of, sex, birth date, age at Dec 31 of the measurement year, death date if any) followed by
one table per section — conditions, procedures, medications, observations, encounters — holding
EVERY event dated on or before ``as_of``, sorted by (date, id). Nothing is tagged, filtered or
scored by value set, and no verdict hint appears: the labeler applies ``LABELING_GUIDE.md`` to
the raw facts.

Size discipline without domain filtering: conditions, procedures, medications and encounters
are always listed in full; observations dated on/after Jan 1 of the year BEFORE the
measurement year are listed in full; older observations are summarised per (code, display) as
one row (count, first date, last date) so their codes stay visible. Output is deterministic,
UTF-8, LF.

This module must never import ``caregap.measures`` (an AST guard test enforces it): the
worksheet cannot know what a value set or a rule is.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from caregap.p6.client import record_from_p6_payload
from caregap.p6.models import (
    ConditionEvent,
    EncounterEvent,
    MedicationEvent,
    ObservationEvent,
    PatientRecord,
    ProcedureEvent,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOTS = REPO_ROOT / "evals" / "gold" / "snapshots"
DEFAULT_OUT = REPO_ROOT / "evals" / "gold" / "worksheets"
MANIFEST_NAME = "MANIFEST.json"
RECORD_PREFIX = "record_"
RECORD_SUFFIX = ".json.gz"


# --- helpers ---------------------------------------------------------------------------------


def age_at_my_end(birth: date | None, as_of: date) -> int | None:
    """Age at Dec 31 of the measurement year (the calendar year of ``as_of``)."""
    if birth is None:
        return None
    my_end = date(as_of.year, 12, 31)
    return my_end.year - birth.year - ((my_end.month, my_end.day) < (birth.month, birth.day))


def observation_full_from(as_of: date) -> date:
    """Jan 1 of the year before the measurement year: observations from here on are listed in
    full (the guide's observation rules look only at the MY and the prior year)."""
    return date(as_of.year - 1, 1, 1)


def cell(value: object) -> str:
    """One Markdown table cell: empty for None, pipes and line breaks neutralised."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return lines


def _iso(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


# --- rendering -------------------------------------------------------------------------------


def render_conditions(items: Sequence[ConditionEvent], as_of: date) -> list[str]:
    rows = sorted(
        (c for c in items if c.onset_date <= as_of), key=lambda c: (c.onset_date, c.condition_id)
    )
    return table(
        ("code", "system", "display", "onset", "abatement"),
        (
            (c.code, c.code_system, c.code_display, _iso(c.onset_date), _iso(c.abatement_date))
            for c in rows
        ),
    )


def render_procedures(items: Sequence[ProcedureEvent], as_of: date) -> list[str]:
    rows = sorted(
        (p for p in items if p.performed_date <= as_of),
        key=lambda p: (p.performed_date, p.procedure_id),
    )
    return table(
        ("code", "display", "performed", "end"),
        (
            (p.code, p.code_display, _iso(p.performed_date), _iso(p.performed_end_date))
            for p in rows
        ),
    )


def render_medications(items: Sequence[MedicationEvent], as_of: date) -> list[str]:
    rows = sorted(
        (m for m in items if m.authored_date <= as_of),
        key=lambda m: (m.authored_date, m.medication_request_id),
    )
    return table(
        ("code", "display", "authored", "status"),
        ((m.code, m.code_display, _iso(m.authored_date), m.status) for m in rows),
    )


def _observation_value(o: ObservationEvent) -> object:
    if o.value_num is not None:
        return o.value_num
    return o.value_text


def render_observations(items: Sequence[ObservationEvent], as_of: date) -> list[str]:
    """Full rows from Jan 1 of MY-1; older observations summarised per (code, display)."""
    full_from = observation_full_from(as_of)
    dated = [o for o in items if o.effective_date <= as_of]
    recent = sorted(
        (o for o in dated if o.effective_date >= full_from),
        key=lambda o: (o.effective_date, o.observation_id),
    )
    older = [o for o in dated if o.effective_date < full_from]

    lines = [
        f"### Observations dated {full_from.isoformat()} .. {as_of.isoformat()} "
        f"(n={len(recent)}, listed in full)",
        "",
        *table(
            ("code", "display", "date", "value", "unit", "value_code", "parent_id", "encounter"),
            (
                (
                    o.code,
                    o.code_display,
                    _iso(o.effective_date),
                    _observation_value(o),
                    o.value_unit,
                    o.value_code,
                    o.parent_observation_id,
                    o.encounter_id,
                )
                for o in recent
            ),
        ),
        "",
        f"### Observations dated before {full_from.isoformat()} "
        f"(n={len(older)}, summarised per code)",
        "",
    ]
    groups: dict[tuple[str, str], list[date]] = {}
    for o in older:
        groups.setdefault((o.code, o.code_display or ""), []).append(o.effective_date)
    lines.extend(
        table(
            ("code", "display", "count", "first", "last"),
            (
                (code, display, len(dates), _iso(min(dates)), _iso(max(dates)))
                for (code, display), dates in sorted(groups.items())
            ),
        )
    )
    return lines


def _encounter_type(e: EncounterEvent) -> str:
    parts = [part for part in (e.type_code, e.type_display) if part]
    return " ".join(parts)


def render_encounters(items: Sequence[EncounterEvent], as_of: date) -> list[str]:
    rows = sorted(
        (e for e in items if e.start_date <= as_of), key=lambda e: (e.start_date, e.encounter_id)
    )
    return table(
        ("id", "class", "type", "start", "end"),
        (
            (
                e.encounter_id,
                e.encounter_class,
                _encounter_type(e),
                _iso(e.start_date),
                _iso(e.end_ts.date()) if e.end_ts is not None else "",
            )
            for e in rows
        ),
    )


def render_worksheet(record: PatientRecord) -> str:
    """The whole Markdown page for one patient at ``record.as_of``."""
    as_of = record.as_of
    patient = record.patient
    age = age_at_my_end(patient.birth_date, as_of)
    conditions = [c for c in record.conditions if c.onset_date <= as_of]
    procedures = [p for p in record.procedures if p.performed_date <= as_of]
    medications = [m for m in record.medications if m.authored_date <= as_of]
    observations = [o for o in record.observations if o.effective_date <= as_of]
    encounters = [e for e in record.encounters if e.start_date <= as_of]
    lines = [
        f"# Blind worksheet: patient {patient.patient_id}",
        "",
        "Raw record, every event dated on or before as_of, sorted by (date, id). No value-set "
        "tagging, no filtering by concept, no verdict. Apply LABELING_GUIDE.md to what is here.",
        "",
        "| field | value |",
        "|---|---|",
        f"| patient_id | {cell(patient.patient_id)} |",
        f"| as_of | {as_of.isoformat()} |",
        f"| measurement year | {as_of.year} (Jan 1 .. Dec 31) |",
        f"| sex | {cell(patient.sex)} |",
        f"| birth date | {_iso(patient.birth_date)} |",
        f"| age at Dec 31 {as_of.year} | {'' if age is None else age} |",
        f"| death date | {_iso(patient.death_date) or 'none recorded on or before as_of'} |",
        "",
        f"## Conditions (n={len(conditions)})",
        "",
        *render_conditions(conditions, as_of),
        "",
        f"## Procedures (n={len(procedures)})",
        "",
        *render_procedures(procedures, as_of),
        "",
        f"## Medications (n={len(medications)})",
        "",
        *render_medications(medications, as_of),
        "",
        f"## Observations (n={len(observations)})",
        "",
        *render_observations(observations, as_of),
        "",
        f"## Encounters (n={len(encounters)})",
        "",
        *render_encounters(encounters, as_of),
    ]
    return "\n".join(lines) + "\n"


# --- I/O -------------------------------------------------------------------------------------


def load_record(path: Path, as_of: date) -> PatientRecord:
    """The as_of-masked record from a ``record_<as_of>.json.gz`` snapshot file."""
    with gzip.open(path, "rb") as fh:
        payload: Any = json.loads(fh.read().decode("utf-8"))
    return record_from_p6_payload(payload, as_of)


def snapshot_records(snapshots: Path) -> list[tuple[str, date, Path]]:
    """Every (patient_id, as_of, record path) under the snapshot dir, sorted."""
    manifest_path = snapshots / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"no snapshot manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    found: list[tuple[str, date, Path]] = []
    for pid in sorted(str(x) for x in manifest.get("patient_ids", [])):
        for path in sorted((snapshots / pid).glob(f"{RECORD_PREFIX}*{RECORD_SUFFIX}")):
            stamp = path.name[len(RECORD_PREFIX) : -len(RECORD_SUFFIX)]
            found.append((pid, date.fromisoformat(stamp), path))
    return found


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def worksheet_name(patient_id: str, as_of: date) -> str:
    return f"{patient_id}_{as_of.isoformat()}.md"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worksheet.py", description=__doc__)
    parser.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        entries = snapshot_records(args.snapshots)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not entries:
        print(f"error: no record snapshots under {args.snapshots}", file=sys.stderr)
        return 2
    for pid, as_of, path in entries:
        record = load_record(path, as_of)
        target = args.out / worksheet_name(pid, as_of)
        write_text_lf(target, render_worksheet(record))
        print(f"wrote {target}")
    print(f"{len(entries)} worksheet(s) under {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
