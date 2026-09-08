"""Shared fixtures. The suite is keyless by construction (SPEC section 9): a real Anthropic
key in the environment aborts the session before a single test runs."""

import os
from collections.abc import Iterator, Sequence
from datetime import date

import pytest

from caregap.measures.context import MeasurementContext
from caregap.measures.value_sets import ValueSet, ValueSetCode, ValueSets, ValueSetSource
from tests import factories
from tests.factories import BIRTH_1960, DEMO_AS_OF, EVAL_AS_OF

FORBIDDEN_ENV: tuple[str, ...] = (
    "CAREGAP_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
)


def pytest_sessionstart(session: pytest.Session) -> None:
    present = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
    if present:
        raise pytest.UsageError(
            "the test suite is keyless (SPEC section 9): unset "
            + ", ".join(present)
            + " before running pytest; real models run only through explicit local commands"
        )


@pytest.fixture(autouse=True)
def _reset_factory_ids() -> Iterator[None]:
    """Deterministic ``c1``/``o1``/... ids inside every test, whatever ran before it."""
    factories.reset_ids()
    yield
    factories.reset_ids()


@pytest.fixture
def eval_as_of() -> date:
    return EVAL_AS_OF


@pytest.fixture
def demo_as_of() -> date:
    return DEMO_AS_OF


@pytest.fixture
def eval_ctx() -> MeasurementContext:
    """Retrospective MY2025 context for a patient aged 65 at Dec 31 2025."""
    return MeasurementContext.for_(EVAL_AS_OF, BIRTH_1960)


@pytest.fixture
def demo_ctx() -> MeasurementContext:
    """Mid-year MY2026 context for the same patient (66 at Dec 31 2026)."""
    return MeasurementContext.for_(DEMO_AS_OF, BIRTH_1960)


def _vs(
    set_id: str,
    code_system: str,
    codes: Sequence[str],
    *,
    source: ValueSetSource = "synthea-scan",
) -> ValueSet:
    return ValueSet(
        id=set_id,
        code_system=code_system,
        source=source,
        codes=[ValueSetCode(code=code) for code in codes],
    )


def build_small_value_sets() -> ValueSets:
    """A handful of codes covering every set the global rules and the core measures read.

    Set ids follow P6's vendored names (``diabetes_snomed``, ``bp_loinc``, ...) plus the P1
    sets ``global_rules.py`` consumes (``hospice_snomed``, ``dementia_snomed``,
    ``dementia_meds_rxnorm``).
    """
    p6: ValueSetSource = "p6-valuesets-2026.08"
    sets = [
        _vs("diabetes_snomed", "SNOMED", ["44054006"], source=p6),
        _vs("hypertension_snomed", "SNOMED", ["59621000"], source=p6),
        _vs("hospice_snomed", "SNOMED", ["385763009"]),
        _vs("dementia_snomed", "SNOMED", ["26929004"]),
        ValueSet(
            id="dementia_meds_rxnorm",
            code_system="RXNORM",
            source="synthea-scan",
            codes=[
                ValueSetCode(
                    code="310436", display="donepezil 10 MG oral tablet", untested_by_data=True
                )
            ],
        ),
        ValueSet(
            id="statin_rxnorm",
            code_system="RXNORM",
            source="public-acc-aha",
            codes=[
                ValueSetCode(
                    code="617310", display="atorvastatin 20 MG oral tablet", intensity="moderate"
                )
            ],
        ),
        _vs("retinal_exam_proc", "SNOMED", ["722161008"], source=p6),
        _vs("mammogram_proc", "SNOMED", ["71651007"], source=p6),
        _vs("colonoscopy_proc", "SNOMED", ["73761001"], source=p6),
        _vs("bp_loinc", "LOINC", ["85354-9", "8480-6", "8462-4"], source=p6),
        _vs("tobacco_status_loinc", "LOINC", ["72166-2"], source=p6),
        _vs("sdoh_loinc", "LOINC", ["93025-5"]),
        _vs("pregnancy_snomed", "SNOMED", ["72892002"]),
    ]
    return ValueSets(version="test", sets={vs.id: vs for vs in sets})


@pytest.fixture
def small_value_sets() -> ValueSets:
    return build_small_value_sets()
