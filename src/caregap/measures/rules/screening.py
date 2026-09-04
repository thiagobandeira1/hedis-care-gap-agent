"""TSC / SNS — Tobacco Use Screening and Social Need Screening (MY2026-style, demo-grade).

One shared screening rule with two instances: :class:`TscRule` (tobacco use screening) and
:class:`SnsRule` (social need screening). Both share the denominator (18+ with at least one
encounter in the measurement year) and differ only in the screening instrument that closes
the numerator.

Public-source basis
-------------------
Neither screening measure carries a CMS Star Ratings id in the 2026 cycle
(``ids.STAR_IDS["TSC"] is None`` / ``ids.STAR_IDS["SNS"] is None``); they follow the
MY2026-style HEDIS screening concepts (NCQA "Tobacco Use Screening" and "Social Need
Screening" lineage, public measure summaries) as fixed by ``docs/SPEC.md`` section 2, with the
tagged rule text in ``rules/json/tsc.json`` / ``rules/json/sns.json``. The **CMS 2026 Star
Ratings Technical Notes** (P2's committed corpus) supply the cross-measure conventions this
rule inherits: age evaluated at Dec 31 of the measurement year (MY), and the death / hospice
"during the measurement period" exclusions applied by the GLOBAL rules. Demo-grade,
HEDIS-aligned, NOT NCQA-certified.

Element tags
------------
quoted
    * age is evaluated at Dec 31 of the MY (Technical Notes convention, ``ctx.age_at_my_end``);
    * exclusions: died during the MY; hospice during the MY — both GLOBAL rules
      (``rules/global_rules.py``) and deliberately NOT re-implemented here.
demo_choice
    * denominator: age >= 18 at Dec 31 of the MY AND at least one encounter (any class) dated
      in [my_start, as_of]. The public "visit during the measurement period" concept is
      approximated by any encounter; the window ends at ``as_of`` because the record is
      as_of-masked (an MY-bounded window is identical on the data the engine can see);
    * TSC numerator: an observation whose code is in ``tobacco_status_loinc`` and equals
      LOINC 72166-2 ("Tobacco smoking status"), dated in [my_start, as_of], carrying a
      non-null ``value_code`` (a status without a coded answer is NOT a screening result:
      numerator ``no``, subtype ``status_without_value``);
    * SNS numerator: an observation whose code is in ``sdoh_screening_loinc`` and equals
      LOINC 93025-5 (PRAPARE panel), dated in [my_start, as_of]. The panel itself carries no
      value, so no ``value_code`` is required;
    * SNS ``positive_domains:<codes>`` reason: the PRAPARE component observations (children
      whose ``parent_observation_id`` points at a qualifying panel, dated in the numerator
      window) with a non-null ``value_code`` are reported by code, sorted and de-duplicated.
      Best effort: a non-null answer marks the domain as *answered*; whether the answer
      signals a need is item-specific and NOT interpreted (see not_representable);
    * numerator windows END at ``as_of``; denominator / exclusion windows use MY bounds;
    * death before the MY -> denominator ``no`` (``global_rules.died_before_my``);
    * missing birth_date -> denominator ``unknown`` (needs_review) unless another criterion
      already fails (three-valued AND: ``no`` wins over ``unknown``).
not_representable (listed in the coverage table, never computed)
    * tobacco cessation intervention (counselling / pharmacotherapy) — the intervention rate
      of the public measure;
    * SDOH interventions (food / housing / transportation / utility follow-up) — the
      intervention rates of the public measure;
    * domain-specific SDOH screening instruments other than PRAPARE (e.g. Hunger Vital Sign);
    * per-item positivity semantics of PRAPARE answers;
    * palliative care during the MY; I-SNP / long-term institutional residence (66+);
    * frailty plus advanced illness (66+) is ``partial``: only the global E4 hint.

Doctrine: deterministic and pure; order-independent over shuffled events; every date is
compared against ``ctx`` / ``windows``; ``clinical_status`` / ``verification_status`` /
``MedicationEvent.status`` are never read; features never enter; the record is read only
through :mod:`caregap.measures.evidence` (plus :func:`_children_of`, a local helper that
mirrors those semantics because ``evidence.py`` has no child-observation lookup — contract
gap, reported). No measure-scoped exclusions or escalations (E1/E4 are global).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from caregap.measures.context import MeasurementContext
from caregap.measures.engine import RuleOutput
from caregap.measures.evidence import (
    enc_ref,
    encounters_in,
    obs_ref,
    observations_in,
    patient_ref,
)
from caregap.measures.ids import MeasureId
from caregap.measures.models import Coverage, EvidenceRef, TriResult
from caregap.measures.rules.global_rules import died_before_my
from caregap.measures.tri import Tri
from caregap.measures.value_sets import ValueSets
from caregap.measures.windows import Window, measurement_year
from caregap.p6.models import ObservationEvent, PatientRecord

MIN_AGE = 18

#: Encounter evidence is capped (earliest first) — the criterion is "at least one".
MAX_ENCOUNTER_EVIDENCE = 3

TOBACCO_STATUS_SET = "tobacco_status_loinc"
TOBACCO_STATUS_CODE = "72166-2"
"""LOINC "Tobacco smoking status" (P6's ``tobacco_status_loinc`` holds exactly this code)."""

SDOH_SCREENING_SET = "sdoh_screening_loinc"
PRAPARE_CODE = "93025-5"
"""LOINC PRAPARE panel — the SDOH screening instrument Synthea records."""

STATUS_WITHOUT_VALUE = "status_without_value"
POSITIVE_DOMAINS_PREFIX = "positive_domains:"

#: Every public exclusion criterion (and the numerator rates) for the screening measures and
#: how far this demo rule can observe them. Shared by both instances.
COVERAGE: dict[str, Coverage] = {
    # numerator rates
    "numerator_screening_in_my": "observable",
    "numerator_tobacco_cessation_intervention": "not_representable",
    "numerator_sdoh_intervention_food_housing_transportation_utility": "not_representable",
    "numerator_domain_specific_sdoh_instruments": "not_representable",  # PRAPARE only
    "numerator_prapare_item_positivity_semantics": "not_representable",  # answered != positive
    # public exclusion criteria (death / hospice are the GLOBAL rules, quoted)
    "exclusion_death_during_measurement_period": "observable",
    "exclusion_hospice_during_measurement_period": "observable",
    "exclusion_palliative_care_during_measurement_period": "not_representable",
    "exclusion_isnp_or_long_term_institution_66_plus": "not_representable",
    "exclusion_frailty_and_advanced_illness_66_plus": "partial",  # global E4 hint only
}


@dataclass(frozen=True)
class ScreeningSpec:
    """What distinguishes one screening instance from the other."""

    set_id: str
    """Value set the instrument code must belong to (supplies the code system)."""
    code: str
    """The literal instrument code (SPEC section 2) — pinned even if the set grows."""
    label: str
    """Human wording for reasons."""
    require_value_code: bool
    """Whether a non-null ``value_code`` is needed for the observation to count."""
    report_domains: bool
    """Whether answered child components are reported as ``positive_domains:<codes>``."""


TSC_SPEC = ScreeningSpec(
    set_id=TOBACCO_STATUS_SET,
    code=TOBACCO_STATUS_CODE,
    label="tobacco use screening (72166-2 with a coded status)",
    require_value_code=True,
    report_domains=False,
)
SNS_SPEC = ScreeningSpec(
    set_id=SDOH_SCREENING_SET,
    code=PRAPARE_CODE,
    label="social need screening (PRAPARE 93025-5)",
    require_value_code=False,
    report_domains=True,
)


def _tri_all(values: Sequence[Tri]) -> Tri:
    """Kleene AND: any ``no`` wins, then any ``unknown``, else ``yes``."""
    if "no" in values:
        return "no"
    if "unknown" in values:
        return "unknown"
    return "yes"


def _fmt(window: Window) -> str:
    return f"{window.label} [{window.start:%Y-%m-%d} .. {window.end:%Y-%m-%d}]"


def _children_of(
    record: PatientRecord, parent_ids: frozenset[str], window: Window
) -> list[ObservationEvent]:
    """Component observations of the given parents, dated in the window, stable-sorted.

    ``evidence.py`` has no child-observation lookup (contract gap, reported); this mirrors its
    semantics — exact id match, inclusive window, deterministic ``(date, id)`` order.
    """
    return sorted(
        (
            o
            for o in record.observations
            if o.parent_observation_id is not None
            and o.parent_observation_id in parent_ids
            and window.contains(o.effective_date)
        ),
        key=lambda o: (o.effective_date, o.observation_id),
    )


def _age_tri(age_at_my_end: int | None) -> tuple[Tri, str]:
    if age_at_my_end is None:
        return "unknown", "birth_date unknown: age at Dec 31 of the MY cannot be computed"
    if age_at_my_end >= MIN_AGE:
        return "yes", f"age {age_at_my_end} at Dec 31 of the MY is {MIN_AGE}+"
    return "no", f"age {age_at_my_end} at Dec 31 of the MY is under {MIN_AGE}"


def _denominator(record: PatientRecord, ctx: MeasurementContext) -> TriResult:
    if died_before_my(record, ctx):
        return TriResult(
            value="no",
            reasons=["died before the measurement year"],
            evidence=[patient_ref("eligibility", event_date=record.patient.death_date)],
            window_start=ctx.my_start,
            window_end=ctx.my_end,
        )

    values: list[Tri] = []
    reasons: list[str] = []
    evidence: list[EvidenceRef] = [patient_ref("eligibility", event_date=record.patient.birth_date)]

    age_value, age_reason = _age_tri(ctx.age_at_my_end)
    values.append(age_value)
    reasons.append(age_reason)

    # demo_choice: any encounter class counts; the window ends at as_of because the record is
    # as_of-masked (identical to the MY-bounded window on visible data).
    window = measurement_year(ctx.as_of)
    encounters = encounters_in(record, window)
    if encounters:
        values.append("yes")
        reasons.append(f"{len(encounters)} encounter(s) in {_fmt(window)}")
        evidence.extend(enc_ref(e, "eligibility") for e in encounters[:MAX_ENCOUNTER_EVIDENCE])
    else:
        values.append("no")
        reasons.append(f"no encounter in {_fmt(window)}")

    return TriResult(
        value=_tri_all(values),
        reasons=reasons,
        evidence=evidence,
        window_start=ctx.my_start,
        window_end=ctx.my_end,
    )


def _positive_domains(
    record: PatientRecord, parents: Sequence[ObservationEvent], window: Window
) -> str | None:
    """``positive_domains:<sorted codes>`` for answered components, or None when there are none."""
    parent_ids = frozenset(p.observation_id for p in parents)
    children = _children_of(record, parent_ids, window)
    answered = {o.code for o in children if o.value_code is not None}
    if not answered:
        return None
    return POSITIVE_DOMAINS_PREFIX + ",".join(sorted(answered))


def _numerator(
    record: PatientRecord, ctx: MeasurementContext, vs: ValueSets, spec: ScreeningSpec
) -> TriResult:
    window = measurement_year(ctx.as_of)
    in_set = observations_in(record, vs, spec.set_id, window)
    instrument = [o for o in in_set if o.code == spec.code]
    qualifying = [o for o in instrument if not spec.require_value_code or o.value_code is not None]

    if qualifying:
        most_recent = qualifying[-1]  # observations_in sorts by (date, id)
        reasons = [f"{spec.label} on {most_recent.effective_date.isoformat()} in {_fmt(window)}"]
        if spec.report_domains:
            domains = _positive_domains(record, qualifying, window)
            if domains is not None:
                reasons.append(domains)
        return TriResult(
            value="yes",
            reasons=reasons,
            evidence=[obs_ref(o, "numerator") for o in qualifying],
            window_start=window.start,
            window_end=window.end,
        )
    if instrument:
        # Only reachable when the spec requires a coded value and none of the rows carry one.
        return TriResult(
            value="no",
            reasons=[f"{spec.code} recorded in {_fmt(window)} without a coded value"],
            evidence=[obs_ref(o, "numerator") for o in instrument],
            subtype=STATUS_WITHOUT_VALUE,
            window_start=window.start,
            window_end=window.end,
        )
    return TriResult(
        value="no",
        reasons=[f"no {spec.label} in {_fmt(window)}"],
        window_start=window.start,
        window_end=window.end,
    )


class _ScreeningRule:
    """Shared evaluate(); subclasses pin ``measure_id`` / ``rule_version`` / ``spec``."""

    measure_id: MeasureId
    rule_version: str
    spec: ScreeningSpec

    def evaluate(
        self, record: PatientRecord, ctx: MeasurementContext, value_sets: ValueSets
    ) -> RuleOutput:
        return RuleOutput(
            denominator=_denominator(record, ctx),
            numerator=_numerator(record, ctx, value_sets, self.spec),
            exclusions=[],  # none observable beyond the global death/hospice rules
            coverage=dict(COVERAGE),
            escalations=[],  # no measure-scoped escalations for the screening measures
        )


class TscRule(_ScreeningRule):
    """Tobacco Use Screening: 18+ with an encounter in the MY; 72166-2 with a coded status."""

    measure_id: MeasureId = "TSC"
    rule_version: str = "tsc-v1"
    spec: ScreeningSpec = TSC_SPEC


class SnsRule(_ScreeningRule):
    """Social Need Screening: 18+ with an encounter in the MY; PRAPARE 93025-5 in the MY."""

    measure_id: MeasureId = "SNS"
    rule_version: str = "sns-v1"
    spec: ScreeningSpec = SNS_SPEC
