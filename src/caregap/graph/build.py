"""Wiring: ``GraphDeps`` + ``build_graph``. One thread per (run, patient); the compiled graph
is the ONLY place the node set lives (``NODE_NAMES`` is asserted against the SPEC diagram).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from caregap.agents.schemas import CareActionPlan, ValidationVerdict
from caregap.graph import edges, nodes
from caregap.graph.outbox import Outbox
from caregap.graph.runstore import RunStore
from caregap.graph.state import (
    AgentError,
    ApprovalDecision,
    GapState,
    LoadError,
    MeasurementSummary,
    NodeTrace,
    ReviewResolution,
    RunOptions,
    RunOutcome,
)
from caregap.llm import ModelBundle
from caregap.measures.engine import MeasureEngine
from caregap.measures.ids import MeasureId
from caregap.measures.models import MeasureEvaluation, OpenGap, ReviewItem
from caregap.measures.rule_text import RuleText, load_rule_text
from caregap.measures.value_sets import ValueSets
from caregap.p6.client import P6Client
from caregap.p6.models import FeatureRow, PatientRecord
from caregap.structured import StructuredCaller

NODE_NAMES: tuple[str, ...] = (
    "load_record",
    "evaluate_measures",
    "validate_gaps",
    "draft_actions",
    "await_approval",
    "record_decision",
    "finalize",
)

CHECKPOINT_TYPES: tuple[type, ...] = (
    RunOptions,
    LoadError,
    MeasurementSummary,
    ApprovalDecision,
    ReviewResolution,
    AgentError,
    NodeTrace,
    RunOutcome,
    ValidationVerdict,
    CareActionPlan,
    PatientRecord,
    FeatureRow,
    MeasureEvaluation,
    OpenGap,
    ReviewItem,
)
"""Every pydantic payload a checkpoint may carry (nested models travel as plain dicts)."""


def checkpoint_serializer() -> JsonPlusSerializer:
    """The serde for ``MemorySaver`` / ``SqliteSaver``: the state's own frozen payloads are
    allow-listed explicitly, so a checkpoint never revives an arbitrary class."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


@dataclass(frozen=True)
class GraphDeps:
    p6: P6Client
    models: ModelBundle
    engine: MeasureEngine
    run_store: RunStore
    outbox: Outbox
    structured: StructuredCaller
    value_sets: ValueSets
    clinic_name: str
    clinic_phone: str
    rule_text_loader: Callable[[MeasureId], RuleText] = load_rule_text


def build_graph(
    deps: GraphDeps, checkpointer: Checkpointer
) -> CompiledStateGraph[GapState, Any, GapState, GapState]:
    builder: StateGraph[GapState, Any, GapState, GapState] = StateGraph(GapState)
    builder.add_node("load_record", nodes.load_record(deps))
    builder.add_node("evaluate_measures", nodes.evaluate_measures(deps))
    builder.add_node("validate_gaps", nodes.validate_gaps(deps))
    builder.add_node("draft_actions", nodes.draft_actions(deps))
    builder.add_node("await_approval", nodes.await_approval(deps))
    builder.add_node("record_decision", nodes.record_decision(deps))
    builder.add_node("finalize", nodes.finalize(deps))

    builder.add_edge(START, "load_record")
    builder.add_conditional_edges(
        "load_record", edges.after_load, ["finalize", "evaluate_measures"]
    )
    builder.add_conditional_edges(
        "evaluate_measures", edges.after_evaluate, ["finalize", "validate_gaps", "draft_actions"]
    )
    builder.add_conditional_edges(
        "validate_gaps", edges.after_validate, ["await_approval", "draft_actions", "finalize"]
    )
    builder.add_edge("draft_actions", "await_approval")
    builder.add_edge("await_approval", "record_decision")
    builder.add_conditional_edges(
        "record_decision", edges.after_decision, ["draft_actions", "finalize"]
    )
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)
