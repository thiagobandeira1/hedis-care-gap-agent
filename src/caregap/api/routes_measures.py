"""``GET /v1/measures`` — the tagged, citable rule text behind every shipped measure, in
engine registry order (UI cards, coverage table, docs)."""

from fastapi import APIRouter

from caregap.api.deps import RuntimeDep
from caregap.api.schemas import ElementCounts, MeasureElement, MeasureInfo
from caregap.measures.ids import MEASURE_NAMES
from caregap.measures.rule_text import RuleText

router = APIRouter(tags=["measures"])


def measure_info(text: RuleText) -> MeasureInfo:
    sources = [e.source for e in text.elements]
    return MeasureInfo(
        measure_id=text.measure_id,
        name=MEASURE_NAMES.get(text.measure_id, text.name),
        star_id=text.star_id,
        rule_version=text.rule_version,
        conformance=text.conformance,
        element_counts=ElementCounts(
            quoted=sources.count("quoted"),
            demo_choice=sources.count("demo_choice"),
            not_representable=sources.count("not_representable"),
        ),
        coverage=text.coverage_table,
        elements=[
            MeasureElement(
                id=e.id,
                kind=e.kind,
                text=e.text,
                source=e.source,
                citation=e.citation,
                coverage=e.coverage,
            )
            for e in text.elements
        ],
    )


@router.get("/v1/measures", response_model=list[MeasureInfo])
def list_measures(runtime: RuntimeDep) -> list[MeasureInfo]:
    loader = runtime.deps.rule_text_loader
    return [measure_info(loader(measure_id)) for measure_id in runtime.deps.engine.measure_ids]
