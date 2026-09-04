"""Build ``src/caregap/measures/rules/json/<id>.json`` from P2's committed public corpus.

Source of truth for the *public* wording is ``hedis-spec-copilot/corpus/committed/
cms-tn-2026.json`` (CMS 2026 Part C & D Star Ratings Technical Notes, US-government public
domain, normalised by P2). For each Technical Notes measure this script

* extracts the Description / Metric / Exclusions / Data Time Frame sections VERBATIM with
  their page numbers (``tn_sections``);
* builds the tagged elements that mirror ``docs/SPEC.md`` section 2 -- every ``quoted``
  sentence is asserted to be a verbatim substring of the cited section, so the build fails
  loudly when the corpus changes;
* records the demo choices (with WHY) and the public criteria Synthea / P6 cannot represent.

TSC and SNS have no Technical Notes entry: their sources are cite-only public URLs and
NOTHING is quoted from them beyond a one-line title.

Usage (from the repo root)::

    uv run python scripts/sync_rule_text.py                # write the 8 JSON files
    uv run python scripts/sync_rule_text.py --render-docs  # ... and docs/MEASURES.md
    uv run python scripts/sync_rule_text.py --check        # exit 1 when files are stale
    uv run python scripts/sync_rule_text.py --corpus PATH  # non-default corpus location

Unicode note: the corpus uses EN DASH (\\u2013), RIGHT SINGLE QUOTATION MARK (\\u2019) and
BULLET (\\u2022); quotes below spell them as escapes so ruff's ambiguous-character rules stay
quiet and reviewers can see exactly which glyph is being matched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from caregap.measures.ids import (  # noqa: E402
    ALL_MEASURES,
    CONFORMANCE_NOTICE,
    MEASURE_NAMES,
    STAR_IDS,
    MeasureId,
)
from caregap.measures.models import Coverage  # noqa: E402
from caregap.measures.rule_text import (  # noqa: E402
    ElementKind,
    NormativeQuote,
    RuleElement,
    RuleSource,
    RuleText,
    TnSection,
)

JSON_DIR = REPO / "src" / "caregap" / "measures" / "rules" / "json"
DOCS_PATH = REPO / "docs" / "MEASURES.md"
DEFAULT_CORPUS = REPO.parent / "hedis-spec-copilot" / "corpus" / "committed" / "cms-tn-2026.json"
CORPUS_ENV = "CAREGAP_P2_CORPUS"

SPEC_DOC_ID = "caregap-spec"
SPEC_CITATION = f"{SPEC_DOC_ID} sec.2"
#: Date the cite-only public URLs were last checked (see the per-source notes).
CITE_ONLY_CHECKED = date(2026, 9, 3)

TN_HEADINGS: tuple[str, ...] = ("Description", "Metric", "Exclusions", "Data Time Frame")


# --- P2 corpus (minimal typed projection; P2 is not a dependency of P1) -------------------


class _CorpusSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str
    heading: str
    text: str
    page: int | None = None


class _CorpusMeasure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    measure_id: str
    measure_name: str
    sections: list[_CorpusSection]


class Corpus(BaseModel):
    """P2 ``NormalizedDoc`` projection (extra fields ignored)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    doc_id: str
    title: str
    source_url: str
    retrieval_date: date
    license_posture: Literal["us_gov_public_domain"]
    measures: list[_CorpusMeasure]

    def measure(self, star_id: str) -> _CorpusMeasure:
        for m in self.measures:
            if m.measure_id == star_id:
                return m
        raise KeyError(f"measure {star_id!r} not in corpus {self.doc_id}")


def corpus_path() -> Path:
    return Path(os.environ.get(CORPUS_ENV, str(DEFAULT_CORPUS)))


def load_corpus(path: Path) -> Corpus:
    return Corpus.model_validate(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class TnMeasure:
    """One Technical Notes measure with verbatim-quote helpers."""

    corpus: Corpus
    star_id: str

    @property
    def block(self) -> _CorpusMeasure:
        return self.corpus.measure(self.star_id)

    def section(self, heading: str) -> _CorpusSection:
        for s in self.block.sections:
            if s.heading == heading:
                return s
        raise KeyError(f"{self.star_id}: no section {heading!r}")

    def page(self, heading: str = "Metric") -> int:
        page = self.section(heading).page
        if page is None:
            raise ValueError(f"{self.star_id}/{heading}: corpus section has no page")
        return page

    def citation(self, heading: str) -> str:
        return f"{self.corpus.doc_id} p.{self.page(heading)}"

    def verify(self, heading: str, quote: str) -> str:
        """Return ``quote`` after asserting it is a verbatim substring of the section."""
        text = self.section(heading).text
        if quote not in text:
            raise SystemExit(
                f"{self.star_id}/{heading}: quote is not verbatim in the corpus:\n  {quote!r}"
            )
        return quote

    def sections(self) -> dict[str, TnSection]:
        out: dict[str, TnSection] = {}
        for heading in TN_HEADINGS:
            s = self.section(heading)
            key = heading.lower().replace(" ", "_")
            out[key] = TnSection(heading=heading, text=s.text, page=self.page(heading))
        return out

    def source(self) -> RuleSource:
        return RuleSource(
            doc_id=self.corpus.doc_id,
            title=self.corpus.title,
            url=self.corpus.source_url,
            page=self.page(),
            retrieval_date=self.corpus.retrieval_date,
            license_posture=self.corpus.license_posture,
        )

    # element constructors ---------------------------------------------------------------

    def quoted(
        self,
        element_id: str,
        kind: ElementKind,
        heading: str,
        quote: str,
        *,
        coverage: Coverage | None = None,
        rationale: str | None = None,
    ) -> RuleElement:
        return RuleElement(
            id=element_id,
            kind=kind,
            text=self.verify(heading, quote),
            source="quoted",
            citation=self.citation(heading),
            quote=quote,
            rationale=rationale,
            coverage=coverage,
        )

    def not_representable(
        self,
        element_id: str,
        kind: ElementKind,
        heading: str,
        quote: str,
        *,
        rationale: str,
        coverage: Coverage = "not_representable",
    ) -> RuleElement:
        return RuleElement(
            id=element_id,
            kind=kind,
            text=self.verify(heading, quote),
            source="not_representable",
            citation=self.citation(heading),
            quote=quote,
            rationale=rationale,
            coverage=coverage,
        )


def demo(
    element_id: str,
    kind: ElementKind,
    text: str,
    *,
    rationale: str,
    citation: str = SPEC_CITATION,
    coverage: Coverage | None = None,
    quote: str | None = None,
) -> RuleElement:
    return RuleElement(
        id=element_id,
        kind=kind,
        text=text,
        source="demo_choice",
        citation=citation,
        quote=quote,
        rationale=rationale,
        coverage=coverage,
    )


def unrepresentable(
    element_id: str,
    kind: ElementKind,
    text: str,
    *,
    rationale: str,
    citation: str,
    coverage: Coverage = "not_representable",
) -> RuleElement:
    """A public criterion known only through a cite-only URL (nothing quoted)."""
    return RuleElement(
        id=element_id,
        kind=kind,
        text=text,
        source="not_representable",
        citation=citation,
        rationale=rationale,
        coverage=coverage,
    )


# --- cite-only public sources (nothing quoted beyond a one-line title) --------------------

SPEC_SOURCE = RuleSource(
    doc_id=SPEC_DOC_ID,
    title="hedis-care-gap-agent SPEC section 2 (demo-grade rules table)",
    url="docs/SPEC.md",
    retrieval_date=CITE_ONLY_CHECKED,
    license_posture="project_spec",
    note="The demo parameters themselves; cited for every demo_choice element.",
)


def _ncqa(doc_id: str, slug: str, measure: str) -> RuleSource:
    return RuleSource(
        doc_id=doc_id,
        title=f"NCQA public measure summary: {measure} (HEDIS Measure Library)",
        url=f"https://www.ncqa.org/hedis/measures/{slug}/",
        retrieval_date=CITE_ONLY_CHECKED,
        license_posture="public_web_cite_only",
        note=(
            "Cite-only (nothing quoted). On the retrieval date the deep link resolved to "
            "NCQA's 'HEDIS Measure Library & Historical Data' page."
        ),
    )


NCQA_CBP = _ncqa("ncqa-cbp", "controlling-high-blood-pressure", "Controlling High Blood Pressure")
NCQA_EED = _ncqa(
    "ncqa-eed", "eye-exam-for-patients-with-diabetes", "Eye Exam for Patients With Diabetes"
)
NCQA_BCS = _ncqa("ncqa-bcs", "breast-cancer-screening", "Breast Cancer Screening")
NCQA_COL = _ncqa("ncqa-col", "colorectal-cancer-screening", "Colorectal Cancer Screening")
NCQA_SPC = _ncqa(
    "ncqa-spc",
    "statin-therapy-for-patients-with-cardiovascular-disease-and-diabetes",
    "Statin Therapy for Patients With Cardiovascular Disease and Diabetes",
)
ACC_AHA = RuleSource(
    doc_id="acc-aha-2018",
    title="2018 AHA/ACC Multisociety Guideline on the Management of Blood Cholesterol "
    "(statin intensity table; value set statin_intensity.json source 'public-acc-aha')",
    url="https://doi.org/10.1161/CIR.0000000000000625",
    retrieval_date=CITE_ONLY_CHECKED,
    license_posture="public_web_cite_only",
    note=(
        "Cite-only (nothing quoted). The DOI resolved to ahajournals.org on the retrieval "
        "date; the article page itself refused the automated fetch (HTTP 403)."
    ),
)
CMS138 = RuleSource(
    doc_id="cms138v13",
    title="CMS138v13 Preventive Care and Screening: Tobacco Use: Screening and Cessation "
    "Intervention (eCQI Resource Center)",
    url="https://ecqi.healthit.gov/ecqm/ep/2025/cms138v13",
    retrieval_date=CITE_ONLY_CHECKED,
    license_posture="public_web_cite_only",
    note=(
        "Cite-only (nothing quoted). URL as assigned; it returned HTTP 404 on the retrieval "
        "date -- verify before publishing."
    ),
)
NCQA_SNS = RuleSource(
    doc_id="ncqa-sns-e",
    title="NCQA public measure summary: Social Need Screening and Intervention (SNS-E)",
    url="https://www.ncqa.org/hedis/measures/social-need-screening-and-intervention/",
    retrieval_date=CITE_ONLY_CHECKED,
    license_posture="public_web_cite_only",
    note=(
        "Cite-only (nothing quoted). URL as assigned; it returned HTTP 404 on the retrieval "
        "date -- verify before publishing."
    ),
)


# --- elements shared by every measure ----------------------------------------------------


def _timeline(m: str, tn: TnMeasure | None) -> list[RuleElement]:
    out: list[RuleElement] = []
    if tn is not None:
        out.append(
            tn.quoted(
                f"{m}/timeline/data_time_frame",
                "timeline",
                "Data Time Frame",
                "01/01/2024 \u2013 12/31/2024",
                rationale=(
                    "The 2026 Star Ratings score measurement year 2024; the demo evaluates "
                    "the calendar year of as_of instead (see measurement_year)."
                ),
            )
        )
    out.append(
        demo(
            f"{m}/timeline/measurement_year",
            "timeline",
            "Measurement year (MY) = the calendar year of as_of. Denominator and exclusion "
            "windows use the MY bounds (Jan 1 - Dec 31); every numerator window ends at as_of; "
            "age = age at Dec 31 of the MY.",
            rationale=(
                "Prospective gap detection: events dated after as_of never count, so a run at "
                "2026-06-30 sees only what a care manager could see that day. Age at Dec 31 "
                "follows the Technical Notes wording 'as of December 31 of the measurement "
                "year'. Eval anchor as_of = 2025-12-31 (complete MY2025); demo anchor "
                "2026-06-30."
            ),
        )
    )
    out.append(
        demo(
            f"{m}/timeline/death_before_my",
            "timeline",
            "death_date before Jan 1 of the MY -> not_eligible (denominator no); death inside "
            "[Jan 1 of the MY, as_of] -> excluded (global rule).",
            rationale=(
                "A member who died before the MY was never enrolled for it; the public "
                "'died during the measurement period' wording only covers deaths inside the "
                "MY. Future death dates are masked by P6 (mask_as_of) and never seen."
            ),
        )
    )
    return out


def _global_escalations(m: str) -> list[RuleElement]:
    return [
        demo(
            f"{m}/coverage/e1_prior_hospice",
            "coverage",
            "E1 (global): a hospice event within 90 days before Jan 1 of the MY, with none "
            "inside the MY, raises a review flag. Hospice BEFORE the MY is deterministically "
            "NOT an exclusion.",
            rationale=(
                "The public wording is 'any time during the measurement period'; 32% of "
                "living Synthea patients carry prior hospice codes, so prior hospice can only "
                "be a hint for a human, never an exclusion."
            ),
            coverage="partial",
        ),
        demo(
            f"{m}/coverage/e4_advanced_illness_hint",
            "coverage",
            "E4 (global): dementia diagnosis or dementia medication plus an inpatient/ED "
            "encounter in the MY at age 66+ raises a review flag; never computed as an "
            "exclusion.",
            rationale=(
                "The public frailty-and-advanced-illness exclusion needs two frailty "
                "indications on different dates plus advanced-illness claims; Synthea carries "
                "no frailty claims, so the engine can only hint (coverage: partial)."
            ),
            coverage="partial",
        ),
    ]


def _e3(m: str) -> RuleElement:
    return demo(
        f"{m}/coverage/e3_medication_status_conflict",
        "coverage",
        "E3 medication_status_conflict: a statin authored inside the MY whose "
        "MedicationRequest.status is stopped or cancelled raises a review flag, even when "
        "another statin closes the numerator.",
        rationale=(
            "ADR-0002: this flag and the on-therapy rule are the only readers of "
            "MedicationRequest.status; a stop inside the MY may mean the therapy ended."
        ),
        coverage="partial",
    )


def _frailty_hint(tn: TnMeasure, element_id: str, quote: str) -> RuleElement:
    return tn.not_representable(
        element_id,
        "exclusion",
        "Exclusions",
        quote,
        rationale=(
            "Needs two frailty indications on different dates plus advanced-illness claims / "
            "dispensed dementia medication; Synthea carries no frailty claims. The global E4 "
            "flag only hints (dementia + inpatient/ED care at 66+)."
        ),
        coverage="partial",
    )


def _isnp_lti(tn: TnMeasure, element_id: str, quote: str) -> RuleElement:
    return tn.not_representable(
        element_id,
        "exclusion",
        "Exclusions",
        quote,
        rationale=(
            "Institutional SNP enrollment and the long-term-institution (LTI) flag are plan "
            "enrollment data; Synthea / P6 carry no enrollment records."
        ),
    )


def _palliative(tn: TnMeasure, element_id: str, quote: str) -> RuleElement:
    return tn.not_representable(
        element_id,
        "exclusion",
        "Exclusions",
        quote,
        rationale="No palliative-care codes appear in the committed Synthea scan (SCAN.md).",
    )


# --- CBP (C14) ----------------------------------------------------------------------------


def build_cbp(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "C14")
    m = "cbp"
    isnp = (
        "Members 66 years of age and older by the end of the measurement period who meet "
        "either of the following:\n- Enrolled in an Institutional SNP (I-SNP) any time during "
        "the measurement year.\n- Living long-term in an institution any time during the "
        "measurement year."
    )
    elements = [
        tn.quoted(
            f"{m}/denominator/age",
            "denominator",
            "Metric",
            "MA members 18\u201385 years of age who had a diagnosis of hypertension (HTN) "
            "(denominator)",
        ),
        demo(
            f"{m}/denominator/hypertension_active",
            "denominator",
            "Hypertension = a hypertension_snomed condition with onset <= Dec 31 of the MY and "
            "abatement null or > Jan 1 of the MY.",
            rationale=(
                "The Technical Notes give no diagnosis window; Synthea records one condition "
                "row per diagnosis (no visit-level claims), so 'active during the MY' is the "
                "closest evidence. clinical_status / verification_status are never read."
            ),
        ),
        tn.quoted(
            f"{m}/numerator/controlled",
            "numerator",
            "Metric",
            "whose blood pressure (BP) was adequately controlled (less than 140/90 mm Hg) "
            "(numerator)",
        ),
        demo(
            f"{m}/numerator/bp_panel",
            "numerator",
            "A BP reading = LOINC 85354-9 panel with BOTH 8480-6 (systolic) and 8462-4 "
            "(diastolic) children sharing its parent_observation_id, unit mm[Hg], taken in an "
            "encounter whose class is not IMP or EMER.",
            rationale=(
                "Synthea / P6 record blood pressure as a LOINC panel with component children; "
                "the unit check keeps unit-less values out; HEDIS-style summaries exclude BPs "
                "taken in acute inpatient / ED settings (NCQA public summary, cite-only)."
            ),
        ),
        demo(
            f"{m}/numerator/representative_bp",
            "numerator",
            "Representative BP = the most-recent-date panel(s) in [Jan 1 of the MY, as_of]; "
            "among same-date panels take the lowest systolic and the lowest diastolic. Met "
            "iff systolic < 140 AND diastolic < 90. No panel in the MY -> gap_open with "
            "subtype no_bp_in_my.",
            rationale=(
                "Mirrors the public 'most recent BP; lowest systolic and lowest diastolic on "
                "the same date' convention (NCQA public summary, cite-only). '< 140/90' is the "
                "quoted threshold. no_bp_in_my is a distinct outreach message (get a reading) "
                "and earns a +1 clinical-weight priority bonus."
            ),
        ),
        tn.quoted(
            f"{m}/exclusion/death",
            "exclusion",
            "Exclusions",
            "Members who die any time during the measurement period.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "Members in hospice or using hospice services any time during the measurement period.",
            coverage="observable",
            rationale=(
                "hospice_snomed procedures / conditions / encounter types in [Jan 1 of the "
                "MY, as_of]; prior hospice is E1 only."
            ),
        ),
        tn.quoted(
            f"{m}/exclusion/esrd",
            "exclusion",
            "Exclusions",
            "Members with a diagnosis that indicates end-stage renal disease (ESRD) any time "
            "during the member\u2019s history on or prior to December 31 of the measurement "
            "year. Do not include laboratory claims.",
            coverage="observable",
            rationale="esrd_snomed condition with onset on or before Dec 31 of the MY.",
        ),
        demo(
            f"{m}/exclusion/dialysis",
            "exclusion",
            "Dialysis: a dialysis_snomed procedure any time through Dec 31 of the MY excludes "
            "the member (treated as ESRD-equivalent).",
            rationale=(
                "The Technical Notes name only the ESRD diagnosis; NCQA's public CBP summary "
                "lists ESRD, dialysis, nephrectomy or kidney transplant (cite-only). Synthea "
                "codes dialysis as procedures, so the engine reads them as ESRD evidence."
            ),
            citation="ncqa-cbp",
            coverage="observable",
        ),
        demo(
            f"{m}/exclusion/kidney_transplant",
            "exclusion",
            "Kidney transplant: a kidney_transplant_snomed procedure or condition any time "
            "through Dec 31 of the MY excludes the member (treated as ESRD-equivalent).",
            rationale=(
                "Same basis as dialysis: NCQA's public CBP summary lists kidney transplant "
                "with ESRD (cite-only); the Technical Notes name only the ESRD diagnosis."
            ),
            citation="ncqa-cbp",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/pregnancy",
            "exclusion",
            "Exclusions",
            "Members with a diagnosis of pregnancy any time during the measurement year. Do "
            "not include laboratory claims",
            coverage="observable",
            rationale=(
                "pregnancy_snomed condition active in the MY (a trap set keeps non-pregnancy "
                "obstetric codes out). The corpus sentence has no closing period."
            ),
        ),
        _palliative(
            tn,
            f"{m}/exclusion/palliative_care",
            "Members receiving palliative care any time during the measurement period.",
        ),
        _isnp_lti(tn, f"{m}/exclusion/isnp_or_long_term_institution_66_plus", isnp),
        _frailty_hint(
            tn,
            f"{m}/exclusion/frailty_and_advanced_illness_66_80",
            "Members 66-80 years of age and older as of December 31 of the measurement year "
            "with frailty and advanced illness.",
        ),
        tn.not_representable(
            f"{m}/exclusion/frailty_81_plus",
            "exclusion",
            "Exclusions",
            "Members 81 years of age and older as of December 31 of the measurement year with "
            "at least two indications of frailty with different dates of service during the "
            "measurement year. Do not include laboratory claims.",
            rationale="Synthea carries no frailty claims.",
        ),
        *_global_escalations(m),
        demo(
            f"{m}/coverage/e5_incomplete_panel_or_unit",
            "coverage",
            "E5: a BP panel in the MY missing its systolic or diastolic child, or carrying a "
            "unit other than mm[Hg], raises a review flag; such a panel never counts toward "
            "the numerator.",
            rationale=(
                "An incomplete or mis-united panel may still be a real reading a human can "
                "confirm; the engine refuses to guess."
            ),
            coverage="partial",
        ),
        demo(
            f"{m}/coverage/e6_hypertension_abated_in_my",
            "coverage",
            "E6: a hypertension condition abated inside the MY raises a review flag; the "
            "member stays in the denominator.",
            rationale=(
                "Synthea abatement dates are unreliable as resolution evidence; a human "
                "decides whether the diagnosis still stands."
            ),
            coverage="partial",
        ),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="CBP",
        star_id="C14",
        name=MEASURE_NAMES["CBP"],
        rule_version="cbp-v1",
        sources=[tn.source(), NCQA_CBP, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "The percentage of MA members 18\u201385 years of age who had a diagnosis of "
                "hypertension (HTN) (denominator) and whose blood pressure (BP) was adequately "
                "controlled (less than 140/90 mm Hg) (numerator).",
            ),
            description=tn.verify(
                "Description",
                "Percent of plan members with high blood pressure who got treatment and were "
                "able to maintain a healthy pressure.",
            ),
            citation=tn.citation("Metric"),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- EED (C11) ----------------------------------------------------------------------------


def build_eed(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "C11")
    m = "eed"
    isnp = (
        "Medicare members 66 years of age and older as of December 31 of the measurement year "
        "who meet either of the following:\n- Enrolled in an Institutional SNP (I-SNP) any "
        "time during the measurement year.\n- Living long-term in an institution any time "
        "during the measurement year as identified by the LTI flag in the Monthly Membership "
        "Detail Data File."
    )
    elements = [
        tn.quoted(
            f"{m}/denominator/age",
            "denominator",
            "Metric",
            "diabetic MA enrollees age 18-75 with diabetes (type 1 and type 2) (denominator)",
        ),
        demo(
            f"{m}/denominator/diabetes_active",
            "denominator",
            "Diabetes = a diabetes_snomed condition active in the MY or the prior year (onset "
            "<= Dec 31 of the MY, abatement null or >= Jan 1 of MY-1). Prediabetes codes "
            "(prediabetes trap set) never qualify.",
            rationale=(
                "HEDIS-style summaries identify diabetes from claims or pharmacy data in the "
                "MY or the year prior (NCQA public summary, cite-only); Synthea offers "
                "condition rows only, so the engine reads diagnosis activity over the same "
                "two years."
            ),
        ),
        tn.quoted(
            f"{m}/numerator/retinal_exam_in_my",
            "numerator",
            "Metric",
            "who had an eye exam (retinal) performed during the measurement year (numerator)",
            rationale="retinal_exam_proc procedure in [Jan 1 of the MY, as_of].",
        ),
        demo(
            f"{m}/numerator/prior_year_negative_exam",
            "numerator",
            "A retinal_exam_proc procedure in MY-1 also closes the gap when it carries a "
            "retinopathy-negative answer (LOINC 71490-7 or 71491-5 with value LA18643-9) and "
            "no diabetic-retinopathy diagnosis has onset on or before the exam date.",
            rationale=(
                "NCQA's public EED summary counts a negative retinal exam from the year prior "
                "(cite-only); the Technical Notes name only the MY. The retinopathy-negative "
                "LOINC answers are how Synthea records the result."
            ),
            citation="ncqa-eed",
        ),
        tn.quoted(
            f"{m}/exclusion/death",
            "exclusion",
            "Exclusions",
            "Members who died any time during the measurement year.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "Members in hospice or using hospice services any time during the measurement year.",
            coverage="observable",
        ),
        _palliative(
            tn,
            f"{m}/exclusion/palliative_care",
            "Members receiving palliative care any time during the measurement year.",
        ),
        tn.not_representable(
            f"{m}/exclusion/no_diabetes_dx_with_pcos_gestational_or_steroid",
            "exclusion",
            "Exclusions",
            "Members who did not have a diagnosis of diabetes, in any setting, during the "
            "measurement year or the year prior to the measurement year and who had a "
            "diagnosis of polycystic ovarian syndrome, gestational diabetes or steroid-induced "
            "diabetes, in any setting, during the measurement year or the year prior to the "
            "measurement year.",
            rationale=(
                "Needs a claims-based diabetes signal that is absent while a PCOS / "
                "gestational / steroid-induced diagnosis is present; the demo denominator is "
                "diagnosis-based (diabetes_snomed), so this criterion is not computed."
            ),
        ),
        _isnp_lti(tn, f"{m}/exclusion/isnp_or_long_term_institution_66_plus", isnp),
        _frailty_hint(
            tn,
            f"{m}/exclusion/frailty_and_advanced_illness_66_plus",
            "Members 66 years of age and older as of December 31 of the measurement year with "
            "both frailty and advanced illness during the measurement year.",
        ),
        *_global_escalations(m),
        demo(
            f"{m}/coverage/e6_diabetes_abated_in_my",
            "coverage",
            "E6: a diabetes condition abated inside the MY raises a review flag; the member "
            "stays in the denominator.",
            rationale=(
                "Synthea abatement dates are unreliable as resolution evidence; a human "
                "decides whether the diagnosis still stands."
            ),
            coverage="partial",
        ),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="EED",
        star_id="C11",
        name=MEASURE_NAMES["EED"],
        rule_version="eed-v1",
        sources=[tn.source(), NCQA_EED, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "The percentage of diabetic MA enrollees age 18-75 with diabetes (type 1 and "
                "type 2) (denominator) who had an eye exam (retinal) performed during the "
                "measurement year (numerator).",
            ),
            description=tn.verify(
                "Description",
                "Percent of plan members with diabetes who had an eye exam to check for damage "
                "from diabetes during the year.",
            ),
            citation=tn.citation("Metric"),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- BCS (C01) ----------------------------------------------------------------------------


def build_bcs(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "C01")
    m = "bcs"
    isnp = (
        "Medicare members 66 years of age and older by the end of the measurement period who "
        "meet either of the following:\n- Enrolled in an Institutional SNP (I-SNP) any time "
        "during the measurement year.\n- Living long-term in an institution any time during "
        "the measurement year."
    )
    elements = [
        tn.quoted(
            f"{m}/denominator/sex",
            "denominator",
            "Description",
            "Percent of female plan members",
            rationale=(
                "P6 sex 'female' -> yes; 'male' / 'other' -> no; anything else -> unknown "
                "(needs_review), never a silent not_eligible."
            ),
        ),
        tn.quoted(
            f"{m}/denominator/age",
            "denominator",
            "Metric",
            "women MA enrollees 52 to 74 years of age (denominator) as of December 31 of the "
            "measurement year",
            rationale=(
                "The Description says 50-74 and the Metric says 52-74; the engine implements "
                "52-74 (the Metric is what the rate is computed from -- see normative_quote)."
            ),
        ),
        tn.quoted(
            f"{m}/numerator/mammogram",
            "numerator",
            "Metric",
            "who had a mammogram to screen for breast cancer in the past two years (numerator)",
            rationale="Mammogram = any procedure in P6's mammogram_proc SNOMED value set.",
        ),
        demo(
            f"{m}/numerator/window_27_months",
            "numerator",
            "Numerator window = Oct 1 of MY-2 through as_of (the 27-month look-back).",
            rationale=(
                "NCQA's public BCS summary states the look-back as October 1 two years prior "
                "through the end of the MY (cite-only); 'the past two years' in the Technical "
                "Notes is the same 27-month window, truncated here at as_of."
            ),
            citation="ncqa-bcs",
        ),
        tn.quoted(
            f"{m}/exclusion/death",
            "exclusion",
            "Exclusions",
            "Members who died any time during the measurement period.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "Members in hospice or using hospice services any time during the measurement period.",
            coverage="observable",
        ),
        tn.not_representable(
            f"{m}/exclusion/bilateral_mastectomy",
            "exclusion",
            "Exclusions",
            "Members who had a bilateral mastectomy or both right and left unilateral "
            "mastectomies any time during the member\u2019s history through December 31 of "
            "the measurement year.",
            rationale=(
                "No mastectomy code appears in the committed Synthea scan (SCAN.md) and there "
                "is no value set; a member with a bilateral mastectomy would show as gap_open."
            ),
        ),
        _palliative(
            tn,
            f"{m}/exclusion/palliative_care",
            "Members receiving palliative care any time during the measurement period.",
        ),
        _isnp_lti(tn, f"{m}/exclusion/isnp_or_long_term_institution_66_plus", isnp),
        _frailty_hint(
            tn,
            f"{m}/exclusion/frailty_and_advanced_illness_66_plus",
            "Members 66 years of age and older by the end of the measurement period with "
            "frailty and advanced illness.",
        ),
        *_global_escalations(m),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="BCS",
        star_id="C01",
        name=MEASURE_NAMES["BCS"],
        rule_version="bcs-v1",
        sources=[tn.source(), NCQA_BCS, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "The percentage of women MA enrollees 52 to 74 years of age (denominator) as of "
                "December 31 of the measurement year who had a mammogram to screen for breast "
                "cancer in the past two years (numerator).",
            ),
            description=tn.verify(
                "Description", "Percent of female plan members aged 50-74 who had a mammogram."
            ),
            citation=tn.citation("Metric"),
            conflict=(
                "The Description states 50-74 while the Metric states 52-74. The engine "
                "implements 52-74 (bcs/denominator/age); the UI surfaces both."
            ),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- COL (C02) ----------------------------------------------------------------------------


def build_col(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "C02")
    m = "col"
    isnp = (
        "Medicare members 66 years of age and older by the end of the measurement period who "
        "meet either of the following:\n- Enrolled in an Institutional SNP (I-SNP) any time "
        "during the measurement period.\n- Living long-term in an institution any time during "
        "the measurement period."
    )
    modality_rationale = (
        "NCQA's public COL summary lists this modality (cite-only); the Technical Notes say "
        "only 'appropriate screenings'. No such code appears in the committed Synthea scan "
        "(SCAN.md)."
    )
    elements = [
        tn.quoted(
            f"{m}/denominator/age",
            "denominator",
            "Metric",
            "MA enrollees aged 50 to 75 (denominator) as of December 31 of the measurement year",
        ),
        tn.quoted(
            f"{m}/numerator/appropriate_screening",
            "numerator",
            "Metric",
            "who had appropriate screenings for colorectal cancer (numerator)",
            rationale=(
                "The Technical Notes name no modality; the demo evidences two of the public "
                "modalities (colonoscopy, FOBT/FIT) and lists the rest as not_representable."
            ),
        ),
        demo(
            f"{m}/numerator/colonoscopy_my_plus_9_prior_years",
            "numerator",
            "Colonoscopy (SNOMED 73761001, colonoscopy_proc) in [Jan 1 of MY-9, as_of].",
            rationale=(
                "NCQA's public COL summary: colonoscopy during the MY or the nine years prior "
                "(cite-only). Anchoring 'nine years prior' to calendar years is the demo's "
                "simplification."
            ),
            citation="ncqa-col",
        ),
        demo(
            f"{m}/numerator/fobt_fit_in_my",
            "numerator",
            "FOBT / FIT in [Jan 1 of the MY, as_of]: a LOINC 57905-2 observation "
            "(fobt_fit_loinc) OR a SNOMED 104435004 'Screening for occult blood in feces' "
            "procedure.",
            rationale=(
                "NCQA's public COL summary: FOBT during the MY (cite-only). Synthea records "
                "FIT both as the LOINC result and as the literal SNOMED procedure, so both "
                "count."
            ),
            citation="ncqa-col",
        ),
        unrepresentable(
            f"{m}/numerator/flexible_sigmoidoscopy_my_plus_4_prior_years",
            "numerator",
            "Flexible sigmoidoscopy during the MY or the four years prior.",
            rationale=modality_rationale,
            citation="ncqa-col",
        ),
        unrepresentable(
            f"{m}/numerator/ct_colonography_my_plus_4_prior_years",
            "numerator",
            "CT colonography during the MY or the four years prior.",
            rationale=modality_rationale,
            citation="ncqa-col",
        ),
        unrepresentable(
            f"{m}/numerator/sdna_fit_my_plus_2_prior_years",
            "numerator",
            "Stool DNA (sDNA) with FIT test during the MY or the two years prior.",
            rationale=modality_rationale,
            citation="ncqa-col",
        ),
        tn.quoted(
            f"{m}/exclusion/death",
            "exclusion",
            "Exclusions",
            "Members who died any time during the measurement period.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "Members who use hospice services or elect to use a hospice benefit any time "
            "during the measurement period.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/colorectal_cancer",
            "exclusion",
            "Exclusions",
            "Members who had colorectal cancer any time during the member\u2019s history "
            "through December 31 of the measurement year. Do not include laboratory claims.",
            coverage="observable",
            rationale="colorectal_cancer_snomed condition with onset any time through as_of.",
        ),
        tn.not_representable(
            f"{m}/exclusion/total_colectomy",
            "exclusion",
            "Exclusions",
            "Members who had a total colectomy any time during the member\u2019s history "
            "through December 31 of the measurement period.",
            rationale=(
                "No total-colectomy code appears in the committed Synthea scan; ambiguous "
                "colon codes raise E7 instead (see coverage)."
            ),
        ),
        _palliative(
            tn,
            f"{m}/exclusion/palliative_care",
            "Members receiving palliative care any time during the measurement period.",
        ),
        _isnp_lti(tn, f"{m}/exclusion/isnp_or_long_term_institution_66_plus", isnp),
        _frailty_hint(
            tn,
            f"{m}/exclusion/frailty_and_advanced_illness_66_plus",
            "Members 66 years of age and older by the end of the measurement period with "
            "frailty and advanced illness.",
        ),
        *_global_escalations(m),
        demo(
            f"{m}/coverage/e7_ambiguous_colon_code",
            "coverage",
            "E7: any colon_ambiguous_snomed condition or procedure through as_of (a possible "
            "total-colectomy or colorectal-cancer signal) promotes the verdict to "
            "needs_review.",
            rationale=(
                "Synthea's colon codes do not separate partial from total colectomy or "
                "history-of from active cancer; a human resolves the exclusion."
            ),
            coverage="partial",
        ),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="COL",
        star_id="C02",
        name=MEASURE_NAMES["COL"],
        rule_version="col-v1",
        sources=[tn.source(), NCQA_COL, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "The percentage of MA enrollees aged 50 to 75 (denominator) as of December 31 "
                "of the measurement year who had appropriate screenings for colorectal cancer "
                "(numerator).",
            ),
            description=tn.verify(
                "Description",
                "Percent of plan members aged 50-75 who had appropriate screening for "
                "colorectal cancer.",
            ),
            citation=tn.citation("Metric"),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- SPC (C19) ----------------------------------------------------------------------------


def _statin_status_rule(m: str) -> RuleElement:
    return demo(
        f"{m}/numerator/on_therapy_status",
        "numerator",
        "ON THERAPY in the MY = a statin_rxnorm MedicationRequest authored in [Jan 1 of the "
        "MY, as_of], OR authored before the MY with status == active. Requests with status "
        "stopped, cancelled or entered-in-error never count.",
        rationale=(
            "'Dispensed' cannot be observed: P6 carries MedicationRequest rows, not pharmacy "
            "claims, and Synthea authors most statins once. ADR-0002 makes this rule (and E3) "
            "the only readers of MedicationRequest.status -- an explicit deviation from P6's "
            "date-only doctrine, with a leakage caveat."
        ),
    )


def build_spc(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "C19")
    m = "spc"
    isnp = (
        "Members 66 years of age and older as of December 31 of the measurement year who meet "
        "either of the following:\n- Enrolled in an Institutional SNP (I-SNP) any time during "
        "the measurement year.\n- Living long-term in an institution any time during the "
        "measurement year as identified by the LTI flag in the Monthly Membership Detail Data "
        "File."
    )
    elements = [
        tn.quoted(
            f"{m}/denominator/age_sex",
            "denominator",
            "Metric",
            "males 21\u201375 years of age and females 40\u201375 years of age during the "
            "measurement year",
            rationale=(
                "Age at Dec 31 of the MY. Unknown sex: yes when the age is inside both bands, "
                "no when outside both, otherwise unknown (needs_review)."
            ),
        ),
        tn.quoted(
            f"{m}/denominator/ascvd",
            "denominator",
            "Metric",
            "who were identified as having clinical atherosclerotic cardiovascular disease "
            "(ASCVD) (denominator)",
        ),
        demo(
            f"{m}/denominator/ascvd_onset",
            "denominator",
            "ASCVD = any ascvd_snomed condition with onset <= Dec 31 of the MY; abatement is "
            "ignored.",
            rationale=(
                "The public event / diagnosis look-back (an MI, CABG or PCI event in the "
                "prior year, or an IVD diagnosis in both years) needs claims; Synthea carries "
                "one condition row per diagnosis, so onset is the only usable signal."
            ),
        ),
        tn.quoted(
            f"{m}/numerator/statin_moderate_or_high",
            "numerator",
            "Metric",
            "were dispensed at least one high or moderate-intensity statin medication during "
            "the measurement year (numerator)",
        ),
        _statin_status_rule(m),
        demo(
            f"{m}/numerator/intensity",
            "numerator",
            "Intensity comes from statin_intensity.json (public ACC/AHA table). Only "
            "low-intensity statins on therapy -> numerator no with subtype "
            "low_intensity_only; a statin with no intensity entry -> numerator unknown "
            "(needs_review).",
            rationale=(
                "The Technical Notes reference the HEDIS intensity tables, which are not "
                "public-domain; the ACC/AHA guideline table is the public equivalent. "
                "low_intensity_only is a distinct outreach message (intensify, not start) and "
                "earns a +1 clinical-weight priority bonus."
            ),
            citation="acc-aha-2018",
        ),
        tn.quoted(
            f"{m}/exclusion/death",
            "exclusion",
            "Exclusions",
            "Members who died any time during the measurement year.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "Members in hospice or using hospice services any time during the measurement year.",
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/esrd_or_dialysis",
            "exclusion",
            "Exclusions",
            "ESRD or dialysis during the measurement year or the year prior to the measurement "
            "year.",
            coverage="observable",
            rationale=(
                "esrd_snomed condition active in [Jan 1 of MY-1, Dec 31 of the MY] or a "
                "dialysis_snomed procedure in that window (two exclusion categories: esrd, "
                "dialysis)."
            ),
        ),
        tn.quoted(
            f"{m}/exclusion/pregnancy",
            "exclusion",
            "Exclusions",
            "Pregnancy during the measurement year or year prior to the measurement year.",
            coverage="observable",
            rationale="pregnancy_snomed condition active in [Jan 1 of MY-1, Dec 31 of the MY].",
        ),
        tn.not_representable(
            f"{m}/exclusion/cirrhosis",
            "exclusion",
            "Exclusions",
            "Cirrhosis during the measurement year or the year prior to the measurement year.",
            rationale="No cirrhosis code appears in the committed Synthea scan (SCAN.md).",
        ),
        tn.not_representable(
            f"{m}/exclusion/myalgia_myositis_myopathy_rhabdomyolysis",
            "exclusion",
            "Exclusions",
            "Myalgia, myositis, myopathy, or rhabdomyolysis during the measurement year.",
            rationale=(
                "No myopathy-family code appears in the committed Synthea scan (SCAN.md). "
                "Fibromyalgia is NOT a public SPC criterion and is deliberately not coded."
            ),
        ),
        tn.not_representable(
            f"{m}/exclusion/in_vitro_fertilization",
            "exclusion",
            "Exclusions",
            "In vitro fertilization in the measurement year or year prior to the measurement year.",
            rationale="No IVF code appears in the committed Synthea scan (SCAN.md).",
        ),
        tn.not_representable(
            f"{m}/exclusion/clomiphene",
            "exclusion",
            "Exclusions",
            "Dispensed at least one prescription for clomiphene (Table SPC-A) during the "
            "measurement year or the year prior to the measurement year.",
            rationale="No clomiphene RxNorm code appears in the committed Synthea scan.",
        ),
        _palliative(
            tn,
            f"{m}/exclusion/palliative_care",
            "Members receiving palliative care any time during the measurement year.",
        ),
        _isnp_lti(tn, f"{m}/exclusion/isnp_or_long_term_institution_66_plus", isnp),
        _frailty_hint(
            tn,
            f"{m}/exclusion/frailty_and_advanced_illness_66_plus",
            "Members 66 years of age and older as of December 31 of the measurement year with "
            "frailty and advanced illness during the measurement year.",
        ),
        *_global_escalations(m),
        _e3(m),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="SPC",
        star_id="C19",
        name=MEASURE_NAMES["SPC"],
        rule_version="spc-v1",
        sources=[tn.source(), NCQA_SPC, ACC_AHA, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "The percentage of males 21\u201375 years of age and females 40\u201375 years "
                "of age during the measurement year, who were identified as having clinical "
                "atherosclerotic cardiovascular disease (ASCVD) (denominator) and were "
                "dispensed at least one high or moderate-intensity statin medication during "
                "the measurement year (numerator).",
            ),
            description=tn.verify(
                "Description",
                "This rating is based on the percent of plan members with heart disease who "
                "get the right type of cholesterol-lowering drugs.",
            ),
            citation=tn.citation("Metric"),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- SPD (D12-style) ----------------------------------------------------------------------


def build_spd(corpus: Corpus) -> RuleText:
    tn = TnMeasure(corpus, "D12")
    m = "spd"
    elements = [
        tn.quoted(
            f"{m}/denominator/age",
            "denominator",
            "Metric",
            "40-75 years old",
            rationale="Age at Dec 31 of the MY (the demo's shared age convention).",
        ),
        demo(
            f"{m}/denominator/diabetes_diagnosis_proxy",
            "denominator",
            "Diabetes is identified by DIAGNOSIS exactly as for EED (diabetes_snomed condition "
            "active in the MY or the prior year; prediabetes never), instead of by diabetes "
            "medication fills.",
            rationale=(
                "The public D12 denominator counts Part D fills ('at least two diabetes "
                "medication fills on unique dates of service'); P6 carries no pharmacy claims "
                "and Synthea MedicationRequests are not fills, so the diagnosis is the only "
                "evidence. This widens the denominator relative to the public measure."
            ),
            quote=(
                "who were dispensed at least two diabetes medication fills on unique dates of "
                "service"
            ),
            citation=tn.citation("Metric"),
        ),
        demo(
            f"{m}/denominator/not_in_spc_denominator",
            "denominator",
            "Members who fall in the SPC denominator (clinical ASCVD) are NOT in the SPD "
            "denominator.",
            rationale=(
                "Product choice: one statin gap per member, no double outreach. The public D12 "
                "measure has no such rule; SPC's stricter intensity rule takes precedence."
            ),
        ),
        tn.not_representable(
            f"{m}/denominator/ipsd_at_least_90_days_before_my_end",
            "denominator",
            "Metric",
            "Beneficiaries are only included in the measure calculation if the IPSD occurs at "
            "least 90 days before the end of the measurement period.",
            rationale="The index prescription start date is a Part D fill concept (no fills).",
        ),
        tn.not_representable(
            f"{m}/denominator/continuous_enrollment",
            "denominator",
            "Metric",
            "Continuous enrollment (CE) is defined as being continuously enrolled in a Medicare "
            "Part D cont ract during the measurement period, with one allowable gap in "
            "enrollment of up to one calendar month.",
            rationale=(
                "No enrollment records in Synthea / P6. (The corpus text carries the PDF "
                "extraction artefact 'cont ract'; quoted verbatim by design.)"
            ),
        ),
        tn.quoted(
            f"{m}/numerator/statin_fill",
            "numerator",
            "Metric",
            "received a statin medication fill during the measurement period (numerator)",
        ),
        _statin_status_rule(m),
        demo(
            f"{m}/numerator/any_intensity",
            "numerator",
            "Any statin intensity counts (low, moderate or high).",
            rationale=(
                "The public D12 numerator is any statin fill; intensity only matters for SPC."
            ),
        ),
        tn.quoted(
            f"{m}/exclusion/hospice",
            "exclusion",
            "Exclusions",
            "The following beneficiaries are excluded from the denominator if at any time "
            "during the measurement period:\n\u2022 Hospice enrollment",
            coverage="observable",
            rationale=(
                "Hospice enrollment is approximated by hospice_snomed events in [Jan 1 of the "
                "MY, as_of] (global rule)."
            ),
        ),
        demo(
            f"{m}/exclusion/death",
            "exclusion",
            "Death inside [Jan 1 of the MY, as_of] excludes the member (global rule applied "
            "to every measure).",
            rationale=(
                "The public D12 text lists no death criterion (Part D denominators drop "
                "disenrolled beneficiaries instead). The engine applies its global death rule "
                "uniformly: a deceased member can never receive outreach."
            ),
            coverage="observable",
        ),
        tn.quoted(
            f"{m}/exclusion/esrd_or_dialysis",
            "exclusion",
            "Exclusions",
            "ESRD diagnosis or dialysis coverage dates",
            coverage="observable",
            rationale=(
                "esrd_snomed condition active in the window or a dialysis_snomed procedure in "
                "it, as implemented by the SPD rule (dialysis coverage dates are enrollment "
                "data; procedures stand in)."
            ),
        ),
        tn.not_representable(
            f"{m}/exclusion/rhabdomyolysis_and_myopathy",
            "exclusion",
            "Exclusions",
            "Rhabdomyolysis and Myopathy",
            rationale="No myopathy-family code appears in the committed Synthea scan (SCAN.md).",
        ),
        tn.not_representable(
            f"{m}/exclusion/pregnancy_lactation_and_fertility",
            "exclusion",
            "Exclusions",
            "Pregnancy, Lactation, and Fertility",
            rationale=(
                "Lactation and fertility treatment have no codes in the committed Synthea "
                "scan; pregnancy alone is observable but the SPEC does not apply it to SPD."
            ),
        ),
        tn.not_representable(
            f"{m}/exclusion/cirrhosis",
            "exclusion",
            "Exclusions",
            "Cirrhosis",
            rationale="No cirrhosis code appears in the committed Synthea scan (SCAN.md).",
        ),
        demo(
            f"{m}/exclusion/prediabetes",
            "exclusion",
            "Handled at the denominator: prediabetes codes (prediabetes trap set) never "
            "qualify as diabetes, so no separate exclusion is computed.",
            rationale=(
                "The public criterion removes fills made for prediabetes; the diagnosis-based "
                "demo denominator never admits prediabetes in the first place."
            ),
            quote="Pre-Diabetes",
            citation=tn.citation("Exclusions"),
            coverage="partial",
        ),
        tn.not_representable(
            f"{m}/exclusion/polycystic_ovary_syndrome",
            "exclusion",
            "Exclusions",
            "Polycystic Ovary Syndrome",
            rationale="No PCOS code appears in the committed Synthea scan (SCAN.md).",
        ),
        *_global_escalations(m),
        _e3(m),
        *_timeline(m, tn),
    ]
    return RuleText(
        measure_id="SPD",
        star_id="D12",
        name=MEASURE_NAMES["SPD"],
        rule_version="spd-v1",
        sources=[tn.source(), NCQA_SPC, ACC_AHA, SPEC_SOURCE],
        elements=elements,
        normative_quote=NormativeQuote(
            metric=tn.verify(
                "Metric",
                "This measure is defined as the percentage of Medicare Part D benef iciaries, "
                "40-75 years old, who were dispensed at least two diabetes medication fills on "
                "unique dates of service and received a statin medication fill during the "
                "measurement period.",
            ),
            description=tn.verify(
                "Description",
                "This rating is based on the percent of plan members with diabetes who take "
                "the most effective cholesterol-lowering drugs.",
            ),
            citation=tn.citation("Metric"),
            conflict=(
                "D12 is a Part D pharmacy-fill measure (PQA SUPD); the demo evaluates a "
                "diagnosis-based proxy over MedicationRequest rows. The Metric text carries "
                "PDF extraction artefacts ('benef iciaries'), quoted verbatim by design."
            ),
        ),
        tn_sections=tn.sections(),
        conformance=CONFORMANCE_NOTICE,
    )


# --- TSC / SNS (MY2026-style screening; no Technical Notes entry) -------------------------


def _screening_denominator(m: str, citation: str) -> list[RuleElement]:
    return [
        demo(
            f"{m}/denominator/age_18_plus",
            "denominator",
            "Age 18 or older at Dec 31 of the MY.",
            rationale=(
                "Adult screening band shared by TSC and SNS (one rule); the public measure's "
                "own age bands are not quoted (cite-only source)."
            ),
        ),
        demo(
            f"{m}/denominator/encounter_in_my",
            "denominator",
            "At least one encounter (any class) in [Jan 1 of the MY, as_of].",
            rationale=(
                "The public measures require qualifying visits; any Synthea encounter stands "
                "in so that members never seen in the MY are not chased for a screening."
            ),
            citation=citation,
        ),
    ]


def _screening_tail(m: str) -> list[RuleElement]:
    return [
        demo(
            f"{m}/exclusion/death",
            "exclusion",
            "Death inside [Jan 1 of the MY, as_of] excludes the member (global rule).",
            rationale="Global rule applied to every measure; a deceased member gets no outreach.",
            coverage="observable",
        ),
        demo(
            f"{m}/exclusion/hospice",
            "exclusion",
            "A hospice_snomed event inside [Jan 1 of the MY, as_of] excludes the member "
            "(global rule).",
            rationale="Global rule applied to every measure; prior hospice is E1 only.",
            coverage="observable",
        ),
        demo(
            f"{m}/coverage/no_measure_escalations",
            "coverage",
            "No measure-scoped escalations; only the global E1 / E4 hints apply.",
            rationale=(
                "Screening gaps are low-priority (clinical weight 1) and counts-only in evals."
            ),
            coverage="partial",
        ),
        *_global_escalations(m),
        *_timeline(m, None),
    ]


def build_tsc() -> RuleText:
    m = "tsc"
    elements = [
        *_screening_denominator(m, "cms138v13"),
        demo(
            f"{m}/numerator/tobacco_status_answer",
            "numerator",
            "A LOINC 72166-2 'Tobacco smoking status' observation with a non-null value_code "
            "in [Jan 1 of the MY, as_of].",
            rationale=(
                "Any coded tobacco-status answer evidences that screening happened; the "
                "answer itself (user / non-user) is not judged. Synthea records the status at "
                "most visits."
            ),
            citation="cms138v13",
        ),
        unrepresentable(
            f"{m}/numerator/cessation_intervention",
            "numerator",
            "Cessation counselling or pharmacotherapy for identified tobacco users (the public "
            "measure's intervention component).",
            rationale=(
                "Synthea carries no cessation-counselling procedure or nicotine-replacement "
                "request in the committed scan; the demo measures screening only."
            ),
            citation="cms138v13",
        ),
        *_screening_tail(m),
    ]
    return RuleText(
        measure_id="TSC",
        star_id=STAR_IDS["TSC"],
        name=MEASURE_NAMES["TSC"],
        rule_version="tsc-v1",
        sources=[CMS138, SPEC_SOURCE],
        elements=elements,
        conformance=CONFORMANCE_NOTICE,
    )


def build_sns() -> RuleText:
    m = "sns"
    elements = [
        *_screening_denominator(m, "ncqa-sns-e"),
        demo(
            f"{m}/numerator/prapare_panel",
            "numerator",
            "A LOINC 93025-5 PRAPARE (social-determinants screening) observation in [Jan 1 of "
            "the MY, as_of].",
            rationale=(
                "Synthea records the PRAPARE panel as one observation; its presence evidences "
                "that a social-need screening happened."
            ),
            citation="ncqa-sns-e",
        ),
        unrepresentable(
            f"{m}/numerator/domain_screens_and_interventions",
            "numerator",
            "Domain-specific screens (food, housing, transportation) and the follow-up "
            "intervention within the public measure's window.",
            rationale=(
                "The public SNS-E is scored per domain with an intervention component; Synthea "
                "offers the PRAPARE panel only, so the demo measures one screening event."
            ),
            citation="ncqa-sns-e",
        ),
        *_screening_tail(m),
    ]
    return RuleText(
        measure_id="SNS",
        star_id=STAR_IDS["SNS"],
        name=MEASURE_NAMES["SNS"],
        rule_version="sns-v1",
        sources=[NCQA_SNS, SPEC_SOURCE],
        elements=elements,
        conformance=CONFORMANCE_NOTICE,
    )


# --- assembly -----------------------------------------------------------------------------


def build_all(corpus: Corpus) -> dict[MeasureId, RuleText]:
    texts: dict[MeasureId, RuleText] = {
        "CBP": build_cbp(corpus),
        "EED": build_eed(corpus),
        "BCS": build_bcs(corpus),
        "COL": build_col(corpus),
        "SPC": build_spc(corpus),
        "SPD": build_spd(corpus),
        "TSC": build_tsc(),
        "SNS": build_sns(),
    }
    for measure_id, text in texts.items():
        if text.star_id != STAR_IDS[measure_id]:
            raise SystemExit(f"{measure_id}: star_id {text.star_id!r} != {STAR_IDS[measure_id]!r}")
    if tuple(texts) != ALL_MEASURES:
        raise SystemExit("build_all must cover ALL_MEASURES in registry order")
    return texts


def dumps(text: RuleText) -> str:
    return json.dumps(text.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def json_path(measure_id: MeasureId) -> Path:
    return JSON_DIR / f"{measure_id.lower()}.json"


# --- docs renderer ------------------------------------------------------------------------


def _cell(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("|", "\\|").replace("\n", "<br>")


def _table(header: Sequence[str], rows: Iterable[Sequence[str | None]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def _tag(e: RuleElement) -> str:
    return f"`{e.source}`"


def render_card(text: RuleText) -> list[str]:
    star = f"Star {text.star_id}" if text.star_id else "no Star Ratings entry"
    lines = [
        f"## {text.measure_id} \u2014 {text.name} ({star})",
        "",
        f"Rule version `{text.rule_version}` \u00b7 conformance: **{text.conformance}**",
        "",
    ]
    nq = text.normative_quote
    if nq is not None:
        lines += [f"> {nq.metric} \u2014 *{nq.citation}*", ""]
        if nq.description:
            lines += [f"> Description: {nq.description}", ""]
        if nq.conflict:
            lines += [f"**Conflict:** {nq.conflict}", ""]
    lines += ["### Sources", ""]
    lines += _table(
        ("doc_id", "title", "page", "retrieved", "license", "url"),
        (
            (
                f"`{s.doc_id}`",
                s.title + (f" \u2014 {s.note}" if s.note else ""),
                str(s.page) if s.page is not None else "",
                s.retrieval_date.isoformat(),
                s.license_posture,
                s.url,
            )
            for s in text.sources
        ),
    )
    lines += ["", "### Elements", ""]
    ordered: list[RuleElement] = []
    for kind in ("denominator", "numerator", "exclusion", "timeline", "coverage"):
        ordered += text.elements_of(kind)
    lines += _table(
        ("id", "kind", "tag", "text", "why / how", "citation"),
        (
            (
                f"`{e.id}`",
                e.kind,
                _tag(e),
                f"\u201c{e.text}\u201d" if e.source == "quoted" else e.text,
                (e.rationale or "")
                + (
                    f" Public wording: \u201c{e.quote}\u201d"
                    if e.quote is not None and e.quote != e.text
                    else ""
                ),
                e.citation,
            )
            for e in ordered
        ),
    )
    lines += ["", "### Coverage (public exclusion criteria)", ""]
    lines += _table(
        ("criterion", "tag", "coverage", "citation"),
        (
            (f"`{e.id.rsplit('/', 1)[1]}`", _tag(e), e.coverage, e.citation)
            for e in text.elements_of("exclusion")
        ),
    )
    hints = text.elements_of("coverage")
    if hints:
        lines += ["", "### Escalation hints", ""]
        lines += _table(
            ("id", "text", "why"),
            ((f"`{e.id.rsplit('/', 1)[1]}`", e.text, e.rationale) for e in hints),
        )
    lines.append("")
    return lines


def render_docs(texts: dict[MeasureId, RuleText]) -> str:
    counts = {
        tag: sum(1 for t in texts.values() for e in t.elements if e.source == tag)
        for tag in ("quoted", "demo_choice", "not_representable")
    }
    lines = [
        "# Measures \u2014 demo-grade rule text",
        "",
        "> GENERATED by `scripts/sync_rule_text.py --render-docs` from "
        "`src/caregap/measures/rules/json/*.json`; do not edit by hand.",
        ">",
        f"> **{CONFORMANCE_NOTICE}.** Public wording comes from the CMS 2026 Part C & D Star "
        "Ratings Technical Notes via P2's committed corpus (US-government public domain); "
        "NCQA / eCQI / ACC-AHA pages are cited by URL only and never quoted.",
        "",
        "Element tags: `quoted` = verbatim public sentence with page citation; `demo_choice` = "
        "a parameter this demo fixed, with the reason; `not_representable` = a public "
        "criterion Synthea / P6 cannot evidence. Coverage: `observable` (computed), `partial` "
        "(only an escalation hint for a human), `not_representable`. Escalation hints E1-E7 "
        "are listed as `coverage` elements. Age = age at Dec 31 of the measurement year; "
        "numerator windows end at `as_of`.",
        "",
        f"Element counts across {len(texts)} measures: "
        + ", ".join(f"{tag} {n}" for tag, n in counts.items())
        + ".",
        "",
    ]
    for text in texts.values():
        lines += render_card(text)
    return "\n".join(lines).rstrip("\n") + "\n"


# --- CLI ----------------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=None, help="P2 cms-tn-2026.json")
    parser.add_argument("--check", action="store_true", help="exit 1 when outputs are stale")
    parser.add_argument("--render-docs", action="store_true", help="also write docs/MEASURES.md")
    args = parser.parse_args(argv)

    path = args.corpus or corpus_path()
    if not path.exists():
        print(f"P2 corpus not found: {path} (set --corpus or ${CORPUS_ENV})", file=sys.stderr)
        return 2
    texts = build_all(load_corpus(path))

    outputs: dict[Path, str] = {json_path(mid): dumps(text) for mid, text in texts.items()}
    if args.render_docs or args.check:
        outputs[DOCS_PATH] = render_docs(texts)

    stale = [p for p, content in outputs.items() if _read(p) != content]
    if args.check:
        for p in stale:
            print(f"stale: {p.relative_to(REPO)}", file=sys.stderr)
        print(f"checked {len(outputs)} files, {len(stale)} stale")
        return 1 if stale else 0
    for p, content in outputs.items():
        _write(p, content)
    quoted = sum(1 for t in texts.values() for e in t.elements if e.source == "quoted")
    total = sum(len(t.elements) for t in texts.values())
    print(
        f"wrote {len(outputs)} files ({total} elements, {quoted} quoted verbatim from {path.name})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
