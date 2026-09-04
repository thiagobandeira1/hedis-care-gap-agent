"""End to end over EVERY committed persona (``synthetic/p6_snapshots/MANIFEST.json``) at
2025-12-31 through the real compiled graph and ``PatientRunner``, keyless.

The fakes are scripted to the fail-closed paths: every validator call answers
``needs_human`` (whatever the measure — ``verify_verdict`` keeps a ``needs_human`` as
issued) and every drafter call answers ``{}`` (an empty plan fails lint twice and the
``TemplateDrafter`` takes over). ``approval_mode="auto"`` records ``auto_reviewed`` without
an interrupt, so each persona finishes in one ``start`` and is never actionable.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from caregap.graph.build import GraphDeps, build_graph, checkpoint_serializer
from caregap.graph.hitl import pending_request
from caregap.graph.outbox import InMemoryOutbox
from caregap.graph.runner import PatientRunner
from caregap.graph.runstore import RunStore
from caregap.graph.state import RunOptions, RunOutcome, thread_id
from caregap.llm import fake_bundle
from caregap.measures.engine import default_engine
from caregap.measures.ids import ALL_MEASURES
from caregap.measures.value_sets import load_value_sets
from caregap.p6.snapshot import SnapshotP6Client
from caregap.structured import StructuredCaller

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_DIR = REPO_ROOT / "synthetic" / "p6_snapshots"
MANIFEST = SNAPSHOT_DIR / "MANIFEST.json"
AS_OF = date(2025, 12, 31)
RUN_ID = "e2e"

KAYCE = "009969ab-f1b8-a2c0-9fb7-f0621d7beea8"
"""Deceased in 1954: ``not_eligible`` on every measure at every anchor."""

CLINIC_NAME = "Demo Primary Care"
CLINIC_PHONE = "555-0100"

#: Scripted outputs per role. ``StructuredCaller`` may spend two outputs per call (one
#: correction turn) and ``draft_plan`` may call twice per node (one regeneration); a panel of
#: five personas with at most one draft node each stays far below this.
SCRIPT_DEPTH = 64
NEEDS_HUMAN = json.dumps(
    {
        "measure_id": "CBP",
        "decision": "needs_human",
        "exclusion_category": None,
        "evidence_ids": [],
        "rule_citation": "",
        "confidence": "low",
        "rationale": "scripted: route to a human",
    }
)
EMPTY_PLAN = "{}"

TERMINAL_STATUSES = {"no_action", "completed"}


def read_manifest() -> dict[str, Any]:
    if not MANIFEST.is_file():
        return {}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    return manifest


PATIENT_IDS: list[str] = [str(pid) for pid in read_manifest().get("patient_ids", [])]


@pytest.fixture(scope="module")
def p6() -> SnapshotP6Client:
    assert MANIFEST.is_file(), f"missing P6 snapshot fixture at {SNAPSHOT_DIR}"
    assert AS_OF.isoformat() in read_manifest()["as_of_dates"]
    return SnapshotP6Client(SNAPSHOT_DIR)


class System:
    """One real ``GraphDeps`` + compiled graph + runner over in-memory stores."""

    def __init__(self, p6: SnapshotP6Client) -> None:
        self.store = RunStore(":memory:")
        self.outbox = InMemoryOutbox()
        self.deps = GraphDeps(
            p6=p6,
            models=fake_bundle([NEEDS_HUMAN] * SCRIPT_DEPTH, [EMPTY_PLAN] * SCRIPT_DEPTH),
            engine=default_engine(),
            run_store=self.store,
            outbox=self.outbox,
            structured=StructuredCaller(),
            value_sets=load_value_sets(),
            clinic_name=CLINIC_NAME,
            clinic_phone=CLINIC_PHONE,
        )
        self.graph = build_graph(self.deps, MemorySaver(serde=checkpoint_serializer()))
        self.runner = PatientRunner(self.graph, self.deps, timeout_s=60)

    def start(self, patient_id: str) -> RunOutcome:
        result = self.runner.start(RUN_ID, patient_id, AS_OF, RunOptions(approval_mode="auto"))
        assert isinstance(result, RunOutcome), "auto mode never interrupts"
        return result


def outcome_json(outcome: RunOutcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), sort_keys=True)


def test_manifest_lists_the_committed_personas() -> None:
    assert PATIENT_IDS, f"no patient_ids in {MANIFEST}"
    assert KAYCE in PATIENT_IDS
    assert len(PATIENT_IDS) == len(set(PATIENT_IDS))
    assert all((SNAPSHOT_DIR / patient_id).is_dir() for patient_id in PATIENT_IDS)


@pytest.mark.parametrize("patient_id", PATIENT_IDS, ids=[p[:8] for p in PATIENT_IDS])
def test_every_persona_finishes_in_auto_mode(p6: SnapshotP6Client, patient_id: str) -> None:
    system = System(p6)
    outcome = system.start(patient_id)

    assert outcome.status in TERMINAL_STATUSES
    assert outcome.load_error is None
    assert set(outcome.engine_verdicts) == set(ALL_MEASURES)
    assert set(outcome.final_statuses) == set(ALL_MEASURES)
    # Auto mode is never actionable and never reaches the outbox.
    assert outcome.actionable is False
    assert outcome.approved_actions == []
    assert system.outbox.entries == []
    assert outcome.decision_action == ("auto_reviewed" if outcome.status == "completed" else None)
    assert pending_request(system.graph, thread_id(RUN_ID, patient_id)) is None

    row = system.store.get_patient_run(RUN_ID, patient_id)
    assert row is not None
    assert (row.status, row.outcome, row.pending) == (outcome.status, outcome, None)
    assert system.store.list_approvals(status="pending") == []


def test_kayce_is_no_action_with_every_measure_not_eligible(p6: SnapshotP6Client) -> None:
    outcome = System(p6).start(KAYCE)
    assert outcome.status == "no_action"
    assert outcome.decision_action is None
    assert set(outcome.engine_verdicts) == set(ALL_MEASURES)
    assert set(outcome.engine_verdicts.values()) == {"not_eligible"}
    assert outcome.final_statuses == outcome.engine_verdicts


def test_outcomes_across_two_runs_are_byte_identical(p6: SnapshotP6Client) -> None:
    def run_panel() -> str:
        system = System(p6)
        outcomes = [system.start(patient_id) for patient_id in PATIENT_IDS]
        assert {o.status for o in outcomes} <= TERMINAL_STATUSES
        return json.dumps([outcome_json(o) for o in outcomes], sort_keys=True)

    first = run_panel()
    second = run_panel()
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
