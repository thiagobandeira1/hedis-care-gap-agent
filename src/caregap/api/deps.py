"""Route dependencies: the process-wide ``Runtime`` from ``app.state`` and the shared
404 lookups for runs and patient runs."""

from typing import Annotated

from fastapi import Depends, Request

from caregap.api.errors import NotFoundError, ServiceUnavailableError
from caregap.graph.runstore import PatientRunRecord, RunRecord
from caregap.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, Runtime):
        raise ServiceUnavailableError("runtime is not started", code="runtime-not-ready")
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def require_run(runtime: Runtime, run_id: str) -> RunRecord:
    run = runtime.run_store.get_run(run_id)
    if run is None:
        raise NotFoundError(f"run {run_id!r} not found", code="run-not-found")
    return run


def require_patient_run(runtime: Runtime, run_id: str, patient_id: str) -> PatientRunRecord:
    run = require_run(runtime, run_id)
    row = runtime.run_store.get_patient_run(run_id, patient_id)
    if row is not None:
        return row
    if patient_id in run.patient_ids:
        raise NotFoundError(
            f"patient {patient_id!r} of run {run_id!r} has not started yet",
            code="patient-run-not-started",
        )
    raise NotFoundError(
        f"patient {patient_id!r} is not part of run {run_id!r}", code="patient-run-not-found"
    )
