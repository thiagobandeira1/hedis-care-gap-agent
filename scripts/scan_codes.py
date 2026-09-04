"""Scan Synthea FHIR R4 bundles and print a keyword-filtered code-frequency table.

Every code in a P1 value set (``src/caregap/measures/value_sets/*.json``) must either appear in
the committed output of this script (``value_sets/SCAN.md``) or be marked ``untested_by_data``.
The scan is deliberately dumb: it counts raw codings by (system, code, display, resource) across
Condition, Procedure, MedicationRequest, Observation (code / value / component) and
Encounter.type, keeping only rows whose display matches one of the value-set keywords.

Usage::

    uv run python scripts/scan_codes.py [--dir DIR] [--out SCAN.md] [--no-filter]

Synthea bundles never leave the machine; only the aggregate table is committed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DIR = Path("C:/Users/Thiago/Documents/My Github Portfolio/_synthea/panel_strat")

KEYWORDS: tuple[str, ...] = (
    "hospice",
    "dialys",
    "renal",
    "end stage",
    "kidney transplant",
    "colorectal|colon|rectum",
    "pregnan",
    "dementia|alzheimer",
    "donepezil|memantine|galantamine|rivastigmine",
    "retinopath",
    "prediabet",
    "tobacco|smok",
    "PRAPARE|housing|food insecurity|transportation",
    "simvastatin|atorvastatin|rosuvastatin|pravastatin|lovastatin|pitavastatin|fluvastatin"
    "|ezetimibe",
    "mastectomy",
    "colectomy",
    "cirrhosis",
    "myopathy|myositis|rhabdo",
    "palliative",
    "frailty",
)

SYSTEM_TOKENS: dict[str, str] = {
    "http://snomed.info/sct": "SNOMED",
    "http://loinc.org": "LOINC",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RXNORM",
    "http://hl7.org/fhir/sid/cvx": "CVX",
}

Row = tuple[str, str, str, str]
"""(system, code, display, resource)."""


def normalize_system(uri: object) -> str:
    if not isinstance(uri, str) or not uri:
        return "OTHER:"
    return SYSTEM_TOKENS.get(uri, f"OTHER:{uri}")


def codings_of(concept: object) -> Iterator[tuple[str, str, str]]:
    """Yield (system, code, display) for every coding of a CodeableConcept-shaped object."""
    if not isinstance(concept, dict):
        return
    text = concept.get("text")
    fallback = text if isinstance(text, str) else ""
    codings = concept.get("coding")
    if not isinstance(codings, list):
        return
    for coding in codings:
        if not isinstance(coding, dict):
            continue
        code = coding.get("code")
        if not isinstance(code, str) or not code:
            continue
        display = coding.get("display")
        yield (
            normalize_system(coding.get("system")),
            code,
            display if isinstance(display, str) else fallback,
        )


def rows_of_resource(resource: dict[str, object]) -> Iterator[tuple[Row, str]]:
    """Yield (row, context_display) pairs for the codings this scan cares about.

    ``context_display`` is the display of the *parent* code for value / component rows so an
    answer code like LA18643-9 ("No") is kept when its question matches a keyword.
    """
    rtype = resource.get("resourceType")
    if rtype in ("Condition", "Procedure"):
        assert isinstance(rtype, str)
        for system, code, display in codings_of(resource.get("code")):
            yield (system, code, display, rtype), display
    elif rtype == "MedicationRequest":
        for system, code, display in codings_of(resource.get("medicationCodeableConcept")):
            yield (system, code, display, "MedicationRequest"), display
    elif rtype == "Observation":
        parent_display = ""
        for system, code, display in codings_of(resource.get("code")):
            parent_display = parent_display or display
            yield (system, code, display, "Observation"), display
        for system, code, display in codings_of(resource.get("valueCodeableConcept")):
            yield (system, code, display, "Observation.value"), parent_display
        components = resource.get("component")
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict):
                    continue
                comp_display = ""
                for system, code, display in codings_of(component.get("code")):
                    comp_display = comp_display or display
                    yield (system, code, display, "Observation.component"), parent_display
                for system, code, display in codings_of(component.get("valueCodeableConcept")):
                    yield (system, code, display, "Observation.component.value"), comp_display
    elif rtype == "Encounter":
        types = resource.get("type")
        if isinstance(types, list):
            for concept in types:
                for system, code, display in codings_of(concept):
                    yield (system, code, display, "Encounter.type"), display


def iter_resources(bundle: object) -> Iterator[dict[str, object]]:
    if not isinstance(bundle, dict):
        return
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if isinstance(entry, dict):
            resource = entry.get("resource")
            if isinstance(resource, dict):
                yield resource


def scan(paths: Iterable[Path], pattern: re.Pattern[str] | None) -> Counter[Row]:
    counts: Counter[Row] = Counter()
    for path in paths:
        with path.open(encoding="utf-8") as fh:
            bundle = json.load(fh)
        for resource in iter_resources(bundle):
            for row, context in rows_of_resource(resource):
                if pattern is None or pattern.search(row[2]) or pattern.search(context):
                    counts[row] += 1
    return counts


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def render(counts: Counter[Row], *, directory: Path, n_bundles: int, filtered: bool) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    keyword_line = (
        "Keyword filter: " + ", ".join(f"`{k}`" for k in KEYWORDS) if filtered else "Unfiltered."
    )
    lines = [
        "# Synthea code scan",
        "",
        f"Generated by `scripts/scan_codes.py` on {stamp} over {n_bundles} Synthea FHIR R4 "
        f"bundles (`{directory.name}/`). Rows are raw coding occurrences by "
        "(system, code, display, resource); `Observation.value` / `Observation.component` rows "
        "are answer / component codings kept when their parent observation matches a keyword.",
        "",
        keyword_line,
        "",
        "| system | code | display | resource | count |",
        "|---|---|---|---|---|",
    ]
    ordered = sorted(counts.items(), key=lambda kv: (kv[0][3], -kv[1], kv[0][0], kv[0][1]))
    lines.extend(
        f"| {_cell(system)} | {_cell(code)} | {_cell(display)} | {_cell(resource)} | {n} |"
        for (system, code, display, resource), n in ordered
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan Synthea bundles for value-set codes.")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="directory of bundles")
    parser.add_argument("--out", type=Path, default=None, help="write markdown here (else stdout)")
    parser.add_argument("--no-filter", action="store_true", help="keep every coding")
    args = parser.parse_args(argv)
    directory: Path = args.dir
    paths = sorted(directory.glob("*.json"))
    if not paths:
        print(f"no *.json bundles under {directory}", file=sys.stderr)
        return 2
    pattern = None if args.no_filter else re.compile("|".join(KEYWORDS), re.IGNORECASE)
    counts = scan(paths, pattern)
    text = render(counts, directory=directory, n_bundles=len(paths), filtered=pattern is not None)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {len(counts)} rows from {len(paths)} bundles to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
