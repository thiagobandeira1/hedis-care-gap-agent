"""Streamlit reviewer console: Panel, Patient, Outbox & audit (SPEC section 7).

Started by ``caregap ui`` (``streamlit run src/caregap/ui/app.py``). The console never computes:
each page calls :class:`~caregap.ui.client.ApiClient` and renders what came back, the approval
card's decision is posted as-is, and the API's 404/409/422 answers are shown verbatim.
"""

from datetime import date
from typing import Literal
from urllib.parse import urlsplit

import streamlit as st

from caregap.config import Settings
from caregap.graph.runstore import ApprovalStatus
from caregap.graph.state import ApprovalDecision, RunOptions, RunOutcome
from caregap.ui import components as ui
from caregap.ui.client import ApiClient, ApiError, PatientRunDetail, RunDetail

DEMO_AS_OF = date(2026, 6, 30)
"""SPEC section 10: the quickstart's mid-MY2026 anchor."""
PAGE_PANEL = "Panel"
PAGE_PATIENT = "Patient"
PAGE_OUTBOX = "Outbox & audit"
PAGES: tuple[str, ...] = (PAGE_PANEL, PAGE_PATIENT, PAGE_OUTBOX)
POLL_SECONDS = 2.0
ACTIVE_RUN_STATUSES = frozenset({"created", "queued", "running"})
ValidationMode = Literal["escalated", "off"]
ApprovalMode = Literal["interrupt", "auto"]
VALIDATION_MODES: tuple[ValidationMode, ...] = ("escalated", "off")
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("interrupt", "auto")
APPROVAL_FILTERS: dict[str, ApprovalStatus | None] = {
    "all": None,
    "pending": "pending",
    "superseded": "superseded",
    "resolved": "resolved",
}


def default_base_url() -> str:
    settings = Settings()
    return f"http://{settings.api_host}:{settings.api_port}"


def show_api_error(exc: ApiError) -> None:
    """Problem+json, verbatim: title, detail, then the 422 error list as bullets."""
    lines = [f"{exc.title} (HTTP {exc.status})" if exc.status else exc.title]
    if exc.detail:
        lines.append(exc.detail)
    lines.extend(f"- {error}" for error in exc.errors)
    st.error("\n\n".join(lines))


def _as_date(value: object, fallback: date) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, tuple) and value and isinstance(value[0], date):
        return value[0]
    return fallback


# --- sidebar --------------------------------------------------------------------------------


LOOPBACK_API_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_base_url(text: str, *, api_host: str | None = None) -> str | None:
    """The base URL the console may talk to, or ``None``: ``http``/``https`` only, host in the
    loopback set (plus the configured API host), no userinfo, no path/query/fragment — the
    sidebar field is not a general-purpose HTTP client (V20)."""
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        return None
    allowed = {*LOOPBACK_API_HOSTS, *(() if api_host is None else (api_host,))}
    if host not in allowed:
        return None
    return candidate.rstrip("/")


def sidebar() -> tuple[str, date, str]:
    fallback = default_base_url()
    with st.sidebar:
        st.title("caregap")
        st.caption("HEDIS care-gap reviewer console")
        typed = st.text_input("API base URL", value=fallback, key="base_url") or ""
        base_url = validate_base_url(typed, api_host=Settings().api_host)
        if base_url is None:
            st.error("API base URL must be http(s)://<loopback or configured API host>[:port].")
            base_url = fallback
        as_of = _as_date(st.date_input("as_of", value=DEMO_AS_OF, key="as_of"), DEMO_AS_OF)
        page = st.radio("Page", PAGES, key="page") or PAGE_PANEL
    return base_url, as_of, page


# --- panel ----------------------------------------------------------------------------------


def panel_page(client: ApiClient, as_of: date) -> None:
    st.header(PAGE_PANEL)
    st.caption(f"Patients and their latest run at as_of {as_of.isoformat()} (`GET /v1/panel`).")
    try:
        items = client.panel_all(as_of)
    except ApiError as exc:
        show_api_error(exc)
        return
    ui.render_panel_table(items)
    ids = [item.patient_id for item in items]
    selected = st.multiselect("Patients to run", ids, key="panel_selected")
    cols = st.columns(3)
    # Non-empty options with the default index: Streamlit always returns one of them.
    validation_mode = cols[0].selectbox("validation_mode", VALIDATION_MODES, key="validation_mode")
    approval_mode = cols[1].selectbox("approval_mode", APPROVAL_MODES, key="approval_mode")
    if cols[2].button("Run selected", key="run_selected", disabled=not selected):
        options = RunOptions(validation_mode=validation_mode, approval_mode=approval_mode)
        try:
            created = client.start_run(as_of, selected, options)
        except ApiError as exc:
            show_api_error(exc)
        else:
            st.session_state["follow_run_id"] = created.run_id
            st.success(f"Run {created.run_id} {created.status}.")
    run_id = (st.text_input("Run id to follow", key="follow_run_id") or "").strip()
    if run_id:
        run_status_section(client, run_id)


def run_status_section(client: ApiClient, run_id: str) -> None:
    """Stepper over ``GET /v1/runs/{id}``; while the run is active and auto-refresh is on, a
    fragment re-polls every ``POLL_SECONDS`` and hands back to the page once it settles."""
    try:
        detail = client.get_run(run_id)
    except ApiError as exc:
        show_api_error(exc)
        return
    auto = st.checkbox("Auto-refresh while the run is active", value=True, key="auto_refresh")
    if auto and detail.run.status in ACTIVE_RUN_STATUSES:
        st.fragment(run_every=POLL_SECONDS)(_run_status_fragment)(client.base_url, run_id)
    else:
        render_run_detail(client, detail)


def _run_status_fragment(base_url: str, run_id: str) -> None:
    client = ApiClient(base_url)
    try:
        try:
            detail = client.get_run(run_id)
        except ApiError as exc:
            show_api_error(exc)
            return
        render_run_detail(client, detail)
    finally:
        client.close()
    if detail.run.status not in ACTIVE_RUN_STATUSES:
        st.rerun(scope="app")


def render_run_detail(client: ApiClient, detail: RunDetail) -> None:
    ui.render_run_stepper(detail.run, detail.patients)
    if detail.run.status in ACTIVE_RUN_STATUSES and st.button(
        "Cancel run", key=f"cancel:{detail.run.run_id}"
    ):
        try:
            cancelled = client.cancel_run(detail.run.run_id)
        except ApiError as exc:
            show_api_error(exc)
        else:
            st.info(f"Cancellation requested: run {cancelled.run_id} is {cancelled.status}.")
    awaiting = [record.patient_id for record in detail.patients if record.pending is not None]
    if awaiting:
        st.caption(
            "Requests were emitted for: "
            + ", ".join(awaiting)
            + ". Open the Patient page; the approval card appears only while one is pending."
        )


# --- patient --------------------------------------------------------------------------------


def patient_page(client: ApiClient, as_of: date) -> None:
    st.header(PAGE_PATIENT)
    try:
        items = client.panel_all(as_of)
    except ApiError as exc:
        show_api_error(exc)
        return
    ids = [item.patient_id for item in items]
    if ids:
        patient_id = st.selectbox("Patient", ids, key="patient_id") or ""
    else:
        patient_id = st.text_input("Patient id", key="patient_id_text") or ""
    patient_id = patient_id.strip()
    if not patient_id:
        st.info("Pick a patient.")
        return
    try:
        gaps = client.patient_gaps(patient_id, as_of)
    except ApiError as exc:
        show_api_error(exc)
        return
    ui.render_context(gaps.context)
    st.subheader("Engine verdicts")
    ui.render_verdict_chips(gaps.evaluations)
    with st.expander("Evidence"):
        ui.render_evidence_table(gaps.evaluations)
    with st.expander("Coverage"):
        ui.render_coverage_table(gaps.evaluations)

    st.subheader("Latest run")
    last_run = next((item.last_run for item in items if item.patient_id == patient_id), None)
    run_key = f"run_for:{patient_id}"
    if last_run is not None and not st.session_state.get(run_key):
        st.session_state[run_key] = last_run.run_id
    run_id = (st.text_input("Run id", key=run_key) or "").strip()
    if not run_id:
        st.info("No run for this patient yet; start one from the Panel page.")
        return
    try:
        detail = client.patient_run(run_id, patient_id)
    except ApiError as exc:
        show_api_error(exc)
        return
    render_patient_run(client, detail)


def render_patient_run(client: ApiClient, detail: PatientRunDetail) -> None:
    record = detail.patient_run
    st.markdown(
        f"Run `{record.run_id}` {ui.status_chip(record.status)} · updated {record.updated_at}"
    )
    if record.superseded_by:
        st.info(f"Superseded by run {record.superseded_by}.")
    state = detail.state
    if state is not None:
        st.markdown("**Validator cards**")
        ui.render_validator_cards(state.verdicts)
        ui.render_review_items(state.review_items)
        ui.render_open_gaps(state.open_gaps)
        ui.render_plan(state.plan, state.draft_error, state.revision_count)
        with st.expander("Trace"):
            ui.render_trace(state.trace, state.agent_errors)
    if record.outcome is not None:
        ui.render_outcome(record.outcome)
    if detail.pending is not None:
        key = f"approval:{record.run_id}:{record.patient_id}"
        decision = ui.render_approval_card(detail.pending, key=key)
        if decision is not None:
            submit_decision(client, record.run_id, record.patient_id, decision)


def submit_decision(
    client: ApiClient, run_id: str, patient_id: str, decision: ApprovalDecision
) -> None:
    try:
        result = client.decide(run_id, patient_id, decision)
    except ApiError as exc:
        show_api_error(exc)
        return
    replayed = " (replayed: this decision_id was already recorded)" if result.replayed else ""
    st.success(
        f"Decision '{decision.action}' by {decision.reviewer} recorded as "
        f"{decision.decision_id}{replayed}."
    )
    if isinstance(result.result, RunOutcome):
        ui.render_outcome(result.result)
    else:
        st.info(
            f"Revision {result.result.revision_count} drafted: a new approval request is "
            "pending. Interact again to review it."
        )


# --- outbox & audit -------------------------------------------------------------------------


def outbox_page(client: ApiClient) -> None:
    st.header(PAGE_OUTBOX)
    st.subheader("Outbox")
    run_filter = (st.text_input("Filter by run id (optional)", key="outbox_run_id") or "").strip()
    try:
        entries = client.outbox(run_filter or None)
    except ApiError as exc:
        show_api_error(exc)
    else:
        ui.render_outbox_table(entries)
    st.subheader("Approval requests")
    choice = st.selectbox("Status", tuple(APPROVAL_FILTERS), key="approval_status") or "all"
    try:
        records = client.approvals(APPROVAL_FILTERS[choice])
    except ApiError as exc:
        show_api_error(exc)
    else:
        ui.render_approvals_table(records)


# --- entry point ----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="caregap reviewer console", layout="wide")
    ui.render_banner()
    base_url, as_of, page = sidebar()
    client = ApiClient(base_url)
    try:
        try:
            health = client.healthz()
        except ApiError as exc:
            show_api_error(exc)
            st.info(f"Start the API with `caregap serve` and point the sidebar at it ({base_url}).")
            return
        with st.sidebar:
            st.caption(
                f"API {health.status} · models {health.models_mode} · prompts "
                f"{health.prompt_version} · P6 {health.p6.service_version}"
            )
        if page == PAGE_PANEL:
            panel_page(client, as_of)
        elif page == PAGE_PATIENT:
            patient_page(client, as_of)
        else:
            outbox_page(client)
    finally:
        client.close()


if __name__ == "__main__":
    main()
