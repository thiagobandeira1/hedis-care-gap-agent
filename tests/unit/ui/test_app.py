"""``caregap.ui.app`` under ``streamlit.testing.v1.AppTest`` with ``ApiClient`` swapped for a
fake returning canned models: the banner, the panel table, the patient page's verdict chips
and validator card, the approval card's buttons, and the decision an approve click posts."""

import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("streamlit.testing")
from streamlit.testing.v1 import AppTest

import caregap.ui.app as app_module
from caregap.graph.runstore import ApprovalRecord, OutboxEntry
from caregap.graph.state import ApprovalDecision, RunOptions
from caregap.measures.ids import ALL_MEASURES
from caregap.ui.client import (
    ApiError,
    DecisionResult,
    HealthInfo,
    MeasureInfo,
    PanelItem,
    PanelPage,
    PatientGaps,
    PatientRunDetail,
    RunCreated,
    RunDetail,
)
from caregap.ui.components import (
    APPROVE_LABEL,
    DEMO_BANNER,
    EDIT_LABEL,
    REJECT_LABEL,
    REVISE_LABEL,
)
from tests.unit.ui import canned

APP_PATH = Path(app_module.__file__)
CARD = f"approval:{canned.RUN_ID}:{canned.PATIENT}"


class FakeApiClient:
    """Canned ``ApiClient`` stand-in. The script re-executes on every ``AppTest.run`` and
    constructs a fresh client each time, so the recorders live on the class."""

    decisions: ClassVar[list[tuple[str, str, ApprovalDecision]]] = []
    started: ClassVar[list[tuple[date, list[str], RunOptions | None]]] = []
    cancelled: ClassVar[list[str]] = []
    fail_decide: ClassVar[ApiError | None] = None
    health_error: ClassVar[ApiError | None] = None
    run_status: ClassVar[str] = "running"

    @classmethod
    def reset(cls) -> None:
        cls.decisions = []
        cls.started = []
        cls.cancelled = []
        cls.fail_decide = None
        cls.health_error = None
        cls.run_status = "running"

    def __init__(self, base_url: str = "", *, timeout: float = 0.0, client: Any = None) -> None:
        self.base_url = base_url

    def close(self) -> None:
        return None

    def healthz(self) -> HealthInfo:
        if self.health_error is not None:
            raise self.health_error
        return canned.HEALTH

    def list_measures(self) -> list[MeasureInfo]:
        return canned.MEASURES

    def panel(self, as_of: date, *, limit: int = 50, offset: int = 0) -> PanelPage:
        return canned.PANEL

    def panel_all(self, as_of: date, **_: Any) -> list[PanelItem]:
        return list(canned.PANEL.items)

    def patient_gaps(self, patient_id: str, as_of: date) -> PatientGaps:
        if patient_id != canned.PATIENT:
            raise ApiError(404, "Not Found", f"unknown patient {patient_id}")
        return canned.GAPS

    def start_run(
        self, as_of: date, patient_ids: list[str], options: RunOptions | None = None
    ) -> RunCreated:
        self.started.append((as_of, list(patient_ids), options))
        return canned.RUN_CREATED

    def get_run(self, run_id: str) -> RunDetail:
        run = canned.RUN.model_copy(update={"status": self.run_status})
        return RunDetail(run=run, patients=[canned.PATIENT_RUN])

    def cancel_run(self, run_id: str) -> RunCreated:
        self.cancelled.append(run_id)
        return RunCreated(run_id=run_id, status="cancelling")

    def patient_run(self, run_id: str, patient_id: str) -> PatientRunDetail:
        return canned.PATIENT_RUN_DETAIL

    def decide(self, run_id: str, patient_id: str, decision: ApprovalDecision) -> DecisionResult:
        if self.fail_decide is not None:
            raise self.fail_decide
        self.decisions.append((run_id, patient_id, decision))
        return canned.DECISION_RESULT

    def approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        return canned.APPROVALS

    def outbox(self, run_id: str | None = None) -> list[OutboxEntry]:
        return canned.OUTBOX


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[AppTest]:
    FakeApiClient.reset()
    monkeypatch.setattr("caregap.ui.client.ApiClient", FakeApiClient)
    monkeypatch.setattr("caregap.ui.app.ApiClient", FakeApiClient)
    yield AppTest.from_file(str(APP_PATH), default_timeout=30)
    FakeApiClient.reset()


def markdown_values(at: AppTest) -> list[str]:
    return [str(element.value) for element in at.markdown]


def open_patient_page(at: AppTest) -> AppTest:
    at.run()
    at.radio(key="page").set_value(app_module.PAGE_PATIENT).run()
    assert not at.exception
    return at


def chips(at: AppTest) -> list[str]:
    return [value for value in markdown_values(at) if "-background[**" in value]


# --- banner and panel -----------------------------------------------------------------------


def test_banner_is_persistent_on_every_page(app: AppTest) -> None:
    for page in app_module.PAGES:
        app.run()
        app.radio(key="page").set_value(page).run()
        assert not app.exception
        assert DEMO_BANNER in [w.value for w in app.warning]
        assert app.warning[0].value == DEMO_BANNER


def test_panel_table_rows(app: AppTest) -> None:
    app.run()
    assert not app.exception
    frame = app.dataframe[0].value
    assert len(frame) == 2
    assert list(frame["patient_id"]) == [canned.PATIENT, canned.OTHER_PATIENT]
    assert list(frame["last_run"]) == [canned.RUN_ID, ""]
    assert "Run selected" in [button.label for button in app.button]


def test_run_selected_posts_run_and_shows_stepper(app: AppTest) -> None:
    app.run()
    app.multiselect(key="panel_selected").select(canned.PATIENT).run()
    app.selectbox(key="approval_mode").set_value("auto").run()
    app.button(key="run_selected").click().run()
    assert not app.exception
    assert FakeApiClient.started == [
        (canned.AS_OF, [canned.PATIENT], RunOptions(approval_mode="auto"))
    ]
    assert any(canned.RUN_ID in value for value in [s.value for s in app.success])
    assert app.text_input(key="follow_run_id").value == canned.RUN_ID
    assert any(f"**Run {canned.RUN_ID}**" in value for value in markdown_values(app))
    assert "Cancel run" in [button.label for button in app.button]


def test_cancel_run_button(app: AppTest) -> None:
    app.run()
    app.text_input(key="follow_run_id").input(canned.RUN_ID).run()
    app.button(key=f"cancel:{canned.RUN_ID}").click().run()
    assert not app.exception
    assert FakeApiClient.cancelled == [canned.RUN_ID]
    assert any("Cancellation requested" in info.value for info in app.info)


def test_finished_run_has_no_cancel_button(app: AppTest) -> None:
    FakeApiClient.run_status = "completed"
    app.run()
    app.text_input(key="follow_run_id").input(canned.RUN_ID).run()
    assert not app.exception
    assert "Cancel run" not in [button.label for button in app.button]
    assert any("completed" in value for value in markdown_values(app))


# --- patient --------------------------------------------------------------------------------


def test_patient_page_renders_a_chip_per_measure(app: AppTest) -> None:
    open_patient_page(app)
    rendered = chips(app)
    for measure_id in ALL_MEASURES:
        matches = [value for value in rendered if f"**{measure_id}**" in value]
        assert len(matches) == 1, measure_id
    cbp = next(value for value in rendered if "**CBP**" in value)
    assert cbp.startswith(":red-background[") and "gap open" in cbp
    bcs = next(value for value in rendered if "**BCS**" in value)
    assert "not eligible" in bcs
    assert any("no_bp_in_my" in caption.value for caption in app.caption)


def test_patient_page_shows_validator_card_plan_and_approval_buttons(app: AppTest) -> None:
    open_patient_page(app)
    values = markdown_values(app)
    assert any("**SPC**" in value and "needs_human" in value for value in values)
    assert any("verified" in value for value in values)
    assert any("**Care-action plan**" in value for value in values)
    labels = {button.label for button in app.button}
    assert {APPROVE_LABEL, EDIT_LABEL, REVISE_LABEL, REJECT_LABEL} <= labels
    assert not FakeApiClient.decisions


def test_approve_click_posts_decision(app: AppTest) -> None:
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.button(key=f"{CARD}:approve").click().run()
    assert not app.exception
    assert len(FakeApiClient.decisions) == 1
    run_id, patient_id, decision = FakeApiClient.decisions[0]
    assert (run_id, patient_id) == (canned.RUN_ID, canned.PATIENT)
    assert decision.action == "approve"
    assert decision.reviewer == "Dr Demo"
    assert decision.edited_plan is None
    assert decision.feedback is None
    assert decision.review_resolutions == []
    assert uuid.UUID(decision.decision_id).version == 4
    assert any(decision.decision_id in success.value for success in app.success)
    assert any("**Outcome**" in value for value in markdown_values(app))


def test_approve_without_reviewer_is_blocked(app: AppTest) -> None:
    open_patient_page(app)
    app.button(key=f"{CARD}:approve").click().run()
    assert FakeApiClient.decisions == []
    assert any("Reviewer name is required" in error.value for error in app.error)


def test_reject_click_posts_reject(app: AppTest) -> None:
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.text_input(key=f"{CARD}:note").input("not this quarter").run()
    app.button(key=f"{CARD}:reject").click().run()
    assert [d.action for _, _, d in FakeApiClient.decisions] == ["reject"]
    assert FakeApiClient.decisions[0][2].note == "not this quarter"


def test_revise_requires_feedback_then_posts_it(app: AppTest) -> None:
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.button(key=f"{CARD}:revise").click().run()
    assert FakeApiClient.decisions == []
    assert any("Feedback is required" in error.value for error in app.error)
    app.text_area(key=f"{CARD}:feedback").input("Mention the retinal exam.").run()
    app.button(key=f"{CARD}:revise").click().run()
    assert [d.action for _, _, d in FakeApiClient.decisions] == ["revise"]
    assert FakeApiClient.decisions[0][2].feedback == "Mention the retinal exam."


def test_edit_and_approve_posts_edited_plan(app: AppTest) -> None:
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.text_area(key=f"{CARD}:edit:patient_message").input("Edited message.").run()
    app.button(key=f"{CARD}:edit").click().run()
    assert not app.exception
    assert [d.action for _, _, d in FakeApiClient.decisions] == ["edit"]
    edited = FakeApiClient.decisions[0][2].edited_plan
    assert edited is not None
    assert edited.patient_message == "Edited message."
    assert edited.provider_note == canned.PLAN.provider_note
    assert edited.actions == canned.PLAN.actions


def test_review_resolution_is_sent_with_the_decision(app: AppTest) -> None:
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.selectbox(key=f"{CARD}:resolution:0:status").set_value("excluded").run()
    app.button(key=f"{CARD}:approve").click().run()
    assert FakeApiClient.decisions == []
    assert any("A reason is required" in error.value for error in app.error)
    app.text_input(key=f"{CARD}:resolution:0:reason").input("hospice noted").run()
    app.button(key=f"{CARD}:approve").click().run()
    decision = FakeApiClient.decisions[0][2]
    assert [(r.measure_id, r.status, r.reason) for r in decision.review_resolutions] == [
        ("SPC", "excluded", "hospice noted")
    ]


def test_decision_api_error_is_shown_verbatim(app: AppTest) -> None:
    FakeApiClient.fail_decide = ApiError(
        422,
        "Unprocessable Entity",
        "decision rejected",
        errors=["resolution of SPC to 'open' requires action 'revise'"],
    )
    open_patient_page(app)
    app.text_input(key=f"{CARD}:reviewer").input("Dr Demo").run()
    app.button(key=f"{CARD}:approve").click().run()
    assert not app.exception
    assert FakeApiClient.decisions == []
    errors = " ".join(error.value for error in app.error)
    assert "HTTP 422" in errors
    assert "requires action 'revise'" in errors


def test_unknown_patient_shows_problem(app: AppTest) -> None:
    open_patient_page(app)
    app.selectbox(key="patient_id").set_value(canned.OTHER_PATIENT).run()
    assert not app.exception
    assert any("unknown patient p2" in error.value for error in app.error)
    assert chips(app) == []


# --- outbox & audit -------------------------------------------------------------------------


def test_outbox_page_tables(app: AppTest) -> None:
    app.run()
    app.radio(key="page").set_value(app_module.PAGE_OUTBOX).run()
    assert not app.exception
    outbox, approvals = app.dataframe[0].value, app.dataframe[1].value
    assert list(outbox["approval_ref"]) == ["d1"]
    assert list(outbox["action_id"]) == ["a1"]
    assert list(approvals["status"]) == ["pending"]
    assert list(approvals["open_gaps"]) == ["CBP, EED"]


# --- API unavailable ------------------------------------------------------------------------


def test_unreachable_api_shows_error_and_no_data(app: AppTest) -> None:
    FakeApiClient.health_error = ApiError(0, "API unreachable", "GET /healthz: ConnectError")
    app.run()
    assert not app.exception
    assert DEMO_BANNER in [w.value for w in app.warning]
    assert any("API unreachable" in error.value for error in app.error)
    assert any("caregap serve" in info.value for info in app.info)
    assert len(app.dataframe) == 0
