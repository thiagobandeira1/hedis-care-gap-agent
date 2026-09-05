"""``POST /v1/runs/{id}/patients/{pid}/decision`` end to end through the real ASGI app:
Tony is run, polled, decided, and the outbox / approvals ledgers are read back — the SPEC
section 7 semantics (404 / 409 / 422, duplicate ``decision_id`` -> the stored result) and the
section 3 guarantee that only a stored human decision reaches ``/v1/outbox``."""

from typing import Any

from tests.unit.api.conftest import (
    KAYCE,
    MEREDITH,
    TONY,
    TONY_PLAN,
    Api,
    ApiFactory,
    assert_problem,
    plan_json,
)


def approvals(api: Api, status: str | None = None) -> list[dict[str, Any]]:
    params = None if status is None else {"status": status}
    response = api.client.get("/v1/approvals", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def outbox(api: Api, run_id: str | None = None) -> list[dict[str, Any]]:
    params = None if run_id is None else {"run_id": run_id}
    response = api.client.get("/v1/outbox", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def test_approve_completes_the_run_and_writes_the_outbox(api: Api) -> None:
    run_id = api.run([TONY])
    before = api.patient(run_id, TONY)
    assert before["patient_run"]["status"] == "awaiting_approval"
    assert [a["patient_id"] for a in approvals(api, "pending")] == [TONY]
    assert outbox(api) == []

    response = api.decide(run_id, TONY, "d1", "approve", note="looks right")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["kind"], body["replayed"]) == ("run_outcome", False)
    outcome = body["result"]
    assert (outcome["status"], outcome["actionable"], outcome["decision_action"]) == (
        "completed",
        True,
        "approve",
    )
    assert outcome["approved_actions"] == ["a1", "a2"]
    assert outcome["final_statuses"]["EED"] == "gap_open"
    assert outcome["final_statuses"]["COL"] == "gap_open"

    after = api.patient(run_id, TONY)
    assert after["patient_run"]["status"] == "completed"
    assert after["patient_run"]["outcome"] == outcome
    assert after["pending"] is None, "pending is derived from the graph, not the stored request"
    assert after["patient_run"]["pending"] == before["pending"], "the ledger keeps the request"
    assert [d["decision_id"] for d in after["state"]["decisions"]] == ["d1"]
    assert [t["node"] for t in after["state"]["trace"]][-3:] == [
        "await_approval",
        "record_decision",
        "finalize",
    ]

    entries = outbox(api)
    assert [e["action_id"] for e in entries] == ["a1", "a2"]
    for entry in entries:
        assert (entry["run_id"], entry["patient_id"]) == (run_id, TONY)
        assert entry["thread_id"] == f"{run_id}:{TONY}"
        assert entry["approval_ref"] == "d1"
        assert entry["measure_id"] in {"COL", "EED"}
        assert entry["created_at"]
    assert outbox(api, run_id) == entries
    assert outbox(api, "run_000000000000") == []

    resolved = approvals(api, "resolved")
    assert [(a["run_id"], a["patient_id"]) for a in resolved] == [(run_id, TONY)]
    assert resolved[0]["request"] == before["pending"]
    assert approvals(api, "pending") == []
    assert [a["status"] for a in approvals(api)] == ["resolved"]


def test_duplicate_decision_id_replays_the_stored_result(api: Api) -> None:
    run_id = api.run([TONY])
    first = api.decide(run_id, TONY, "d1", "approve").json()
    assert first["replayed"] is False
    # Same id, different action: the stored result comes back, nothing is re-run.
    second = api.decide(run_id, TONY, "d1", "reject")
    assert second.status_code == 200, second.text
    assert second.json() == {**first, "replayed": True}
    assert [e["action_id"] for e in outbox(api)] == ["a1", "a2"], "no double append"
    assert api.patient(run_id, TONY)["patient_run"]["status"] == "completed"


def test_decision_id_reused_for_another_patient_run_is_409(api: Api) -> None:
    run_id = api.run([TONY, MEREDITH])
    assert api.decide(run_id, TONY, "d1", "approve").status_code == 200
    body = assert_problem(api.decide(run_id, MEREDITH, "d1", "approve"), 409, "decision-id-reused")
    assert "d1" in body["detail"]
    assert api.patient(run_id, MEREDITH)["patient_run"]["status"] == "awaiting_approval"


def test_not_awaiting_approval_is_409(api: Api) -> None:
    run_id = api.run([KAYCE, TONY])
    body = assert_problem(api.decide(run_id, KAYCE, "d1", "approve"), 409, "not-awaiting-approval")
    assert KAYCE in body["detail"]
    assert api.decide(run_id, TONY, "d2", "approve").status_code == 200
    # Once resolved, a fresh decision id has nothing to resume.
    assert_problem(api.decide(run_id, TONY, "d3", "reject"), 409, "not-awaiting-approval")
    assert api.runtime.run_store.get_decision("d3") is None


def test_invalid_decisions_are_422_with_the_runner_error_list(api: Api) -> None:
    run_id = api.run([TONY])
    body = assert_problem(api.decide(run_id, TONY, "d1", "revise"), 422, "decision-invalid")
    assert body["errors"] == ["revise requires feedback"]
    body = assert_problem(api.decide(run_id, TONY, "d1", "edit"), 422, "decision-invalid")
    assert body["errors"] == ["edit requires edited_plan"]
    body = assert_problem(
        api.decide(
            run_id,
            TONY,
            "d1",
            "approve",
            review_resolutions=[{"measure_id": "CBP", "status": "open", "reason": "look again"}],
        ),
        422,
        "decision-invalid",
    )
    assert any("does not reference a review item" in e for e in body["errors"])
    assert any("requires action 'revise'" in e for e in body["errors"])
    # A body pydantic rejects never reaches the runner.
    assert_problem(api.decide(run_id, TONY, "d1", "yolo"), 422, "request-invalid")
    assert_problem(api.decide(run_id, TONY, "", "approve"), 422, "request-invalid")
    # Nothing was recorded or resumed.
    assert api.runtime.run_store.get_decision("d1") is None
    view = api.patient(run_id, TONY)
    assert view["patient_run"]["status"] == "awaiting_approval"
    assert view["pending"] is not None
    assert [a["patient_id"] for a in approvals(api, "pending")] == [TONY]
    assert outbox(api) == []


def test_reject_records_a_rejected_outcome_and_never_touches_the_outbox(api: Api) -> None:
    run_id = api.run([TONY])
    body = api.decide(run_id, TONY, "d1", "reject", note="not this quarter").json()
    assert body["kind"] == "run_outcome"
    assert (body["result"]["status"], body["result"]["actionable"]) == ("rejected", False)
    assert body["result"]["approved_actions"] == []
    assert api.patient(run_id, TONY)["patient_run"]["status"] == "rejected"
    assert outbox(api) == []
    assert [a["status"] for a in approvals(api)] == ["resolved"]


def test_edit_and_approve_re_lints_and_ships_the_edited_plan(api: Api) -> None:
    run_id = api.run([TONY])
    plan = api.patient(run_id, TONY)["pending"]["plan"]
    edited = {**plan, "provider_note": plan["provider_note"] + " Reviewed by the care team."}
    response = api.decide(run_id, TONY, "d1", "edit", edited_plan=edited)
    assert response.status_code == 200, response.text
    outcome = response.json()["result"]
    assert (outcome["status"], outcome["actionable"], outcome["decision_action"]) == (
        "completed",
        True,
        "edit",
    )
    assert [e["action_id"] for e in outbox(api, run_id)] == ["a1", "a2"]
    stored = api.patient(run_id, TONY)["state"]["plan"]
    assert stored["provider_note"] == edited["provider_note"]
    # An edit that breaks the style guide is refused before anything resumes.
    other = api.run([TONY])
    broken = {**plan, "patient_message": plan["patient_message"] + " Your SNOMED code is 73761001."}
    body = assert_problem(
        api.decide(other, TONY, "d2", "edit", edited_plan=broken), 422, "decision-invalid"
    )
    assert body["errors"] and all(e.startswith("edited_plan:") for e in body["errors"])
    assert api.patient(other, TONY)["patient_run"]["status"] == "awaiting_approval"


def test_revise_returns_a_new_request_then_approve_completes(make_api: ApiFactory) -> None:
    api = make_api(drafter=[TONY_PLAN, TONY_PLAN])
    run_id = api.run([TONY])
    response = api.decide(run_id, TONY, "d1", "revise", feedback="Mention the eye exam first.")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["kind"], body["replayed"]) == ("approval_request", False)
    assert body["result"]["revision_count"] == 1
    assert body["result"]["run_id"] == run_id

    view = api.patient(run_id, TONY)
    assert view["patient_run"]["status"] == "awaiting_approval"
    assert view["pending"]["revision_count"] == 1
    assert view["state"]["revision_count"] == 1
    assert [a["patient_id"] for a in approvals(api, "pending")] == [TONY]
    assert outbox(api) == []

    # The revision budget (max_revisions=1) is enforced by the runner -> 422.
    body = assert_problem(
        api.decide(run_id, TONY, "d2", "revise", feedback="again"), 422, "decision-invalid"
    )
    assert any("revision budget exhausted" in e for e in body["errors"])

    final = api.decide(run_id, TONY, "d3", "approve").json()
    assert final["result"]["status"] == "completed"
    assert [e["approval_ref"] for e in outbox(api)] == ["d3", "d3"]
    assert [d["decision_id"] for d in api.patient(run_id, TONY)["state"]["decisions"]] == [
        "d1",
        "d3",
    ]


def test_superseded_request_is_409_and_no_longer_pending(make_api: ApiFactory) -> None:
    api = make_api(drafter=[TONY_PLAN, TONY_PLAN, plan_json(["BCS"])])
    older = api.run([TONY])
    newer = api.run([TONY], options={"approval_mode": "auto"})
    assert api.patient(newer, TONY)["patient_run"]["status"] == "completed"

    view = api.patient(older, TONY)
    assert view["patient_run"]["superseded_by"] == newer
    assert view["pending"] is None
    assert view["patient_run"]["outcome"] is None
    body = assert_problem(api.decide(older, TONY, "d1", "approve"), 409, "superseded")
    assert newer in body["detail"]
    assert api.runtime.run_store.get_decision("d1") is None

    superseded = approvals(api, "superseded")
    assert [(a["run_id"], a["patient_id"], a["superseded_by"]) for a in superseded] == [
        (older, TONY, newer)
    ]
    assert approvals(api, "pending") == []
    assert outbox(api) == [], "auto mode never writes the outbox"
    # Another patient's pending request is untouched by Tony's supersession.
    third = api.run([MEREDITH])
    assert [a["patient_id"] for a in approvals(api, "pending")] == [MEREDITH]
    assert api.decide(third, MEREDITH, "d2", "approve").status_code == 200
