"""``caregap eval`` and ``caregap report`` (SPEC section 6): the three tiers, their stamped
artifacts, the ratchet gate, and the README regions — every published number is read back
from an artifact, never typed.

Tiers (all over the committed gold snapshots in ``evals/gold/snapshots``; the graph runs with
``approval_mode=auto`` so nothing is ever actionable):

``engine``
    keyless, CI-live: ``validation_mode=off`` under fake models; the outcome rows must be
    byte-identical to the committed ``evals/gold/engine-outcomes.jsonl`` (``--regen`` rewrites
    it). ``metrics.overall`` IS the engine-only column.
``pipeline``
    keyless replay of the recorded validator / drafter outputs (``--record`` re-records with
    the real models and needs a key — never in CI). Refuses when the gold set is not frozen or
    changed after freezing. ``fallback_count > 0`` (a case without a recording) stamps the
    column ``unpublishable``.
``outreach``
    ``--judge`` grades every drafted plan with the frozen faithfulness rubric (needs a key)
    and records the responses; CI re-parses ``evals/recorded/judge.jsonl``.

Exit codes: 0 ok · 1 a measured failure (byte diff, freeze mismatch, ratchet regression,
README out of sync) · 2 something required is missing (gold labels, snapshots, recordings,
baseline, key) — never a silent pass.
"""

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from clinevals import (
    EVAL_BEGIN,
    EVAL_END,
    DatasetError,
    ReportError,
    RunStamp,
    build_artifact,
    detect_git_sha,
    readme_in_sync,
    sync_readme,
    write_artifact,
)
from langgraph.checkpoint.memory import MemorySaver

from caregap.agents.prompts import DRAFTER_PROMPT_SHA, PROMPT_VERSION, VALIDATOR_PROMPT_SHA
from caregap.agents.schemas import CareActionPlan
from caregap.config import ConfigError, Settings
from caregap.evals import gates
from caregap.evals.gold import (
    FREEZE_HASH_KEY,
    GoldCase,
    freeze_hash,
    freeze_status,
    load_gold,
    read_frozen_hash,
)
from caregap.evals.outcomes import (
    PatientOutcomeRow,
    RuntimeLike,
    Tier,
    diff_summary,
    gold_patients,
    read_outcomes,
    render_outcomes,
    run_tier,
    state_values,
    tier_options,
    write_outcomes,
)
from caregap.evals.rubrics import (
    RUBRIC,
    OutreachCase,
    evidence_passages,
    judge_cases,
    judge_human_agreement,
    read_judge_records,
    read_spotcheck,
    score_judgements,
    spotcheck_sample,
    write_judge_records,
)
from caregap.evals.scoring import Scorecard, score
from caregap.fakes import ReplayChatModel
from caregap.graph.build import checkpoint_serializer
from caregap.graph.runstore import MEMORY as RUNSTORE_MEMORY
from caregap.graph.state import thread_id
from caregap.llm import ModelBundle, anthropic_bundle, replay_bundle
from caregap.measures.ids import ALL_MEASURES, CORE_MEASURES, SCREENING_MEASURES
from caregap.measures.models import OpenGap, ReviewItem
from caregap.measures.rule_text import load_rule_text
from caregap.p6.snapshot import SnapshotP6Client

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_MISSING = 2

EVALS_DIR = Path("evals")
README_PATH = Path("README.md")
COMPARISON: tuple[str, str] = ("Engine-only", "engine_only_overall")
PRIMARY_LABEL = "Engine+validator"
ENGINE_LABEL = "Engine-only"
MEASURES_BEGIN = "<!-- EVAL-MEASURES:BEGIN -->"
MEASURES_END = "<!-- EVAL-MEASURES:END -->"
NOT_MEASURED = "not yet measured"
EVAL_PLACEHOLDER = (
    f"_Gap-detection precision / recall: {NOT_MEASURED} (no eval artifact yet; run "
    "`caregap eval --tier engine` once `evals/gold/gap_cases.jsonl` exists)._"
)
MEASURES_PLACEHOLDER = f"_Per-measure counts: {NOT_MEASURED}._"
UNTRUSTED_BELOW = 0.80

Artifact = dict[str, object]


class EvalExit(Exception):
    """Internal control flow: a message for stderr and the process exit code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EvalPaths:
    """The DATA layout under ``evals/`` (code lives in ``caregap.evals``)."""

    root: Path

    @property
    def gold_dir(self) -> Path:
        return self.root / "gold"

    @property
    def gold_cases(self) -> Path:
        return self.gold_dir / "gap_cases.jsonl"

    @property
    def freeze(self) -> Path:
        return self.gold_dir / "FREEZE.json"

    @property
    def snapshots(self) -> Path:
        return self.gold_dir / "snapshots"

    @property
    def engine_outcomes(self) -> Path:
        return self.gold_dir / "engine-outcomes.jsonl"

    @property
    def spotcheck(self) -> Path:
        return self.gold_dir / "judge_human_spotcheck.jsonl"

    @property
    def recorded(self) -> Path:
        return self.root / "recorded"

    @property
    def judge_recordings(self) -> Path:
        return self.recorded / "judge.jsonl"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def baseline(self) -> Path:
        return self.root / "baseline.json"

    def artifact(self, tier: str) -> Path:
        return self.artifacts / f"{tier}-latest.json"


# --- plumbing -----------------------------------------------------------------------------------


def _build_runtime(
    settings: Settings, *, models: ModelBundle | None = None, checkpointer: Any = None
) -> RuntimeLike:
    """``caregap.runtime.build_runtime`` (the API layer's composition root), imported lazily so
    the eval modules stay importable on their own."""
    from caregap.runtime import build_runtime

    runtime: RuntimeLike = build_runtime(settings, models=models, checkpointer=checkpointer)
    return runtime


def _memory_checkpointer() -> MemorySaver:
    return MemorySaver(serde=checkpoint_serializer())


def _eval_settings(
    settings: Settings | None, paths: EvalPaths, *, models: Literal["fake", "replay", "anthropic"]
) -> Settings:
    """Eval runs never touch the live ledger / checkpoints (``data/*.sqlite``): the ledger is
    in-memory and every tier injects ``_memory_checkpointer()``, so an eval can neither
    supersede a real pending approval nor leave rows behind for the API and UI."""
    base = settings if settings is not None else Settings()
    return base.model_copy(
        update={
            "p6_mode": "snapshot",
            "snapshot_dir": paths.snapshots,
            "models": models,
            "recordings_dir": paths.recorded,
            "runstore_path": Path(RUNSTORE_MEMORY),
            "checkpoint_path": Path(RUNSTORE_MEMORY),
        }
    )


def _eval_run_id(tier: Tier, paths: EvalPaths) -> str:
    """``eval-<tier>-<gold sha[:8]>``: namespaced so an eval run can never collide with a
    real ``run_*`` id even if the two stores were ever shared."""
    return f"eval-{tier}-{freeze_hash(paths.gold_cases)[:8]}"


def _load_gold(paths: EvalPaths) -> list[GoldCase]:
    try:
        gold = load_gold(paths.gold_cases)
    except FileNotFoundError as exc:
        raise EvalExit(
            EXIT_MISSING,
            f"no gold labels at {paths.gold_cases}: the blind labeling protocol "
            "(docs/SPEC.md section 6, scripts/gold.py) has not produced gap_cases.jsonl yet; "
            "nothing to score",
        ) from exc
    except DatasetError as exc:
        raise EvalExit(EXIT_MISSING, f"gold set invalid ({paths.gold_cases}): {exc}") from exc
    if not gold:
        raise EvalExit(EXIT_MISSING, f"gold set at {paths.gold_cases} is empty; nothing to score")
    return gold


def _snapshot_stamp(paths: EvalPaths) -> dict[str, str]:
    try:
        manifest = SnapshotP6Client(paths.snapshots).manifest
    except FileNotFoundError as exc:
        raise EvalExit(
            EXIT_MISSING,
            f"no gold snapshots at {paths.snapshots} (MANIFEST.json missing); write them with "
            "`python scripts/gold.py snapshot --as-of <date> --out evals/gold/snapshots`",
        ) from exc
    return {
        "service_version": manifest.service_version,
        "feature_version": manifest.feature_version,
        "valuesets_version": manifest.valuesets_version,
        "generated_from_sha": manifest.generated_from_sha,
        "patient_count": str(len(manifest.patient_ids)),
    }


def _git_sha() -> str:
    try:
        return detect_git_sha()
    except RuntimeError:
        return "unknown"


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _config_hash(tier: Tier) -> str:
    options = tier_options(tier)
    payload = {
        "approval_mode": options.approval_mode,
        "validation_mode": options.validation_mode,
        "measures": list(options.measures),
        "prompt_version": PROMPT_VERSION,
        "validator_prompt_sha": VALIDATOR_PROMPT_SHA,
        "drafter_prompt_sha": DRAFTER_PROMPT_SHA,
        "rule_versions": {m: load_rule_text(m).rule_version for m in ALL_MEASURES},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _freeze_block(paths: EvalPaths) -> dict[str, object]:
    pinned = read_frozen_hash(paths.freeze)
    actual = freeze_hash(paths.gold_cases)
    status = "not_frozen" if pinned is None else ("frozen" if pinned == actual else "mismatch")
    return {"status": status, FREEZE_HASH_KEY: actual, "pinned_sha256": pinned}


def _require_frozen(paths: EvalPaths, tier: str) -> None:
    """Every tier scores frozen gold only: exit 2 when ``FREEZE.json`` is missing (something
    required is absent), exit 1 when the gold bytes no longer match the pinned hash."""
    refusal = freeze_status(paths.gold_cases, paths.freeze)
    if refusal is not None:
        code = EXIT_MISSING if read_frozen_hash(paths.freeze) is None else EXIT_FAIL
        raise EvalExit(
            code,
            f"{refusal}; the {tier} tier scores only frozen gold (SPEC section 6: freeze + "
            "hash before engine contact)",
        )


def _unfrozen(artifact: Mapping[str, object] | None) -> str | None:
    """The artifact's freeze status when it is anything but ``frozen`` (``None`` otherwise)."""
    if artifact is None:
        return None
    freeze = artifact.get("freeze")
    status = freeze.get("status") if isinstance(freeze, Mapping) else None
    return None if status == "frozen" else str(status)


def _stamp(
    paths: EvalPaths,
    tier: Tier,
    *,
    models: Mapping[str, str],
    rubric_sha256: str | None = None,
    judge_human_agreement: float | None = None,
) -> RunStamp:
    return RunStamp(
        git_sha=_git_sha(),
        date=_today(),
        config_hash=_config_hash(tier),
        dataset_hash=freeze_hash(paths.gold_cases),
        models=dict(models),
        rubric_sha256=rubric_sha256,
        judge_human_agreement=judge_human_agreement,
    )


def _screening_counts(rows: Sequence[PatientOutcomeRow]) -> dict[str, dict[str, int]]:
    """TSC / SNS final statuses as counts only (SPEC section 6: never a ratio)."""
    counts: dict[str, dict[str, int]] = {m: {} for m in SCREENING_MEASURES}
    for row in rows:
        for measure in SCREENING_MEASURES:
            status = row.final_statuses.get(measure)
            if status is not None:
                counts[measure][status] = counts[measure].get(status, 0) + 1
    return {m: dict(sorted(block.items())) for m, block in counts.items()}


def _compose(
    tier: Tier,
    scorecard: Scorecard,
    stamp: RunStamp,
    *,
    engine_only: Mapping[str, float] | None,
    extra: Mapping[str, object],
) -> Artifact:
    payload = build_artifact(
        scorecard.report,
        stamp,
        tier=tier,
        extra={**scorecard.extras(), **extra},
        include_per_item=True,
    )
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    if engine_only is not None:
        metrics["engine_only_overall"] = dict(engine_only)
    metrics["strict_overall"] = scorecard.strict_headline
    metrics["overall_dev"] = scorecard.report.per_split.get("dev", {})
    return payload


def _sha_drift(bundle: ModelBundle) -> int:
    return sum(
        model.sha_drift_count
        for model in (bundle.validator, bundle.drafter)
        if isinstance(model, ReplayChatModel)
    )


# --- tiers --------------------------------------------------------------------------------------


def _run_engine_tier(
    paths: EvalPaths, settings: Settings | None, gold: Sequence[GoldCase], *, regen: bool
) -> Artifact:
    _require_frozen(paths, "engine")
    snapshot = _snapshot_stamp(paths)
    runtime = _build_runtime(
        _eval_settings(settings, paths, models="fake"), checkpointer=_memory_checkpointer()
    )
    try:
        rows = run_tier("engine", gold, runtime=runtime, run_id=_eval_run_id("engine", paths))
    finally:
        runtime.close()
    fresh = render_outcomes(rows)
    if regen:
        write_outcomes(rows, paths.engine_outcomes)
        print(f"wrote {paths.engine_outcomes} ({len(rows)} patients)")
    elif not paths.engine_outcomes.exists():
        raise EvalExit(
            EXIT_FAIL,
            f"no committed engine outcomes at {paths.engine_outcomes}; run "
            "`caregap eval --tier engine --regen` and commit the file",
        )
    else:
        committed = paths.engine_outcomes.read_bytes().decode("utf-8")
        if committed != fresh:
            raise EvalExit(
                EXIT_FAIL,
                f"engine outcomes drifted from {paths.engine_outcomes}:\n"
                + diff_summary(read_outcomes(paths.engine_outcomes), rows)
                + "\nif intended, regenerate with `caregap eval --tier engine --regen`",
            )
        print(f"engine outcomes match {paths.engine_outcomes} ({len(rows)} patients)")
    scorecard = score(gold, rows, measures=CORE_MEASURES)
    extra: dict[str, object] = {
        "approval_mode": "auto",
        "validation_mode": "off",
        "fallback_count": 0,
        "sha_drift_count": 0,
        "publishable": True,
        "publication_status": "publishable",
        "engine_outcomes_sha256": hashlib.sha256(fresh.encode("utf-8")).hexdigest(),
        "freeze": _freeze_block(paths),
        "snapshot": snapshot,
        "screening_counts": _screening_counts(rows),
    }
    return _compose(
        "engine",
        scorecard,
        _stamp(paths, "engine", models={"validator_model": "none", "drafter_model": "fake"}),
        engine_only=None,
        extra=extra,
    )


def _pipeline_bundle(paths: EvalPaths, settings: Settings, *, record: bool) -> ModelBundle:
    if record:
        try:
            return anthropic_bundle(settings, record_to=paths.recorded)
        except ConfigError as exc:
            raise EvalExit(EXIT_MISSING, f"--record needs the real models: {exc}") from exc
    return replay_bundle(paths.recorded)


def _run_pipeline_tier(
    paths: EvalPaths, settings: Settings | None, gold: Sequence[GoldCase], *, record: bool
) -> Artifact:
    _require_frozen(paths, "pipeline")
    snapshot = _snapshot_stamp(paths)
    if not paths.engine_outcomes.exists():
        raise EvalExit(
            EXIT_MISSING,
            f"no committed engine outcomes at {paths.engine_outcomes} for the engine-only "
            "column; run `caregap eval --tier engine --regen` first",
        )
    engine_rows = read_outcomes(paths.engine_outcomes)
    eval_settings = _eval_settings(settings, paths, models="anthropic" if record else "replay")
    bundle = _pipeline_bundle(paths, eval_settings, record=record)
    runtime = _build_runtime(eval_settings, models=bundle, checkpointer=_memory_checkpointer())
    try:
        rows = run_tier("pipeline", gold, runtime=runtime, run_id=_eval_run_id("pipeline", paths))
    finally:
        runtime.close()
    fallback_count = bundle.fallback_count()
    sha_drift = _sha_drift(bundle)
    scorecard = score(gold, rows, measures=CORE_MEASURES)
    engine_card = score(gold, engine_rows, measures=CORE_MEASURES)
    publishable = fallback_count == 0
    extra: dict[str, object] = {
        "approval_mode": "auto",
        "validation_mode": "escalated",
        "fallback_count": fallback_count,
        "sha_drift_count": sha_drift,
        "publishable": publishable,
        "publication_status": "publishable" if publishable else "unpublishable",
        "unpublishable_reason": (
            None
            if publishable
            else f"{fallback_count} model call(s) had no recording (fallback sentinels)"
        ),
        "recorded_this_run": record,
        "engine_outcomes_sha256": hashlib.sha256(
            render_outcomes(engine_rows).encode("utf-8")
        ).hexdigest(),
        "engine_measures": [row.as_dict() for row in engine_card.measures],
        "freeze": _freeze_block(paths),
        "snapshot": snapshot,
        "screening_counts": _screening_counts(rows),
    }
    return _compose(
        "pipeline",
        scorecard,
        _stamp(paths, "pipeline", models=bundle.model_ids),
        engine_only=engine_card.headline,
        extra=extra,
    )


def _plan_of(state: Mapping[str, Any]) -> CareActionPlan | None:
    plan = state.get("plan")
    if isinstance(plan, CareActionPlan):
        return plan
    if isinstance(plan, Mapping):
        return CareActionPlan.model_validate(plan)
    return None


def _models_of[T: (OpenGap, ReviewItem)](model: type[T], raw: object) -> list[T]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return []
    out: list[T] = []
    for item in raw:
        if isinstance(item, model):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(model.model_validate(item))
    return out


def _outreach_cases(
    gold: Sequence[GoldCase], runtime: RuntimeLike, *, run_id: str
) -> list[OutreachCase]:
    """Run every gold patient through the replayed pipeline and collect the drafted plans."""
    splits = {case.patient_id: case.split for case in gold}
    options = tier_options("outreach")
    cases: list[OutreachCase] = []
    for patient_id, as_of in gold_patients(gold).items():
        runtime.runner.start(run_id, patient_id, as_of, options)
        state = state_values(runtime.graph, thread_id(run_id, patient_id))
        plan = _plan_of(state)
        if plan is None:
            continue
        passages = evidence_passages(
            _models_of(OpenGap, state.get("open_gaps")),
            _models_of(ReviewItem, state.get("review_items")),
            as_of=as_of,
            clinic_name=runtime.deps.clinic_name,
            clinic_phone=runtime.deps.clinic_phone,
        )
        cases.append(
            OutreachCase(
                patient_id=patient_id,
                as_of=as_of,
                split=splits[patient_id],
                plan=plan,
                passages=passages,
            )
        )
    return cases


def _run_outreach_tier(
    paths: EvalPaths, settings: Settings | None, gold: Sequence[GoldCase], *, judge: bool
) -> Artifact:
    _require_frozen(paths, "outreach")
    eval_settings = _eval_settings(settings, paths, models="replay")
    if judge:
        _snapshot_stamp(paths)
        # The judge is built through ``anthropic_bundle`` (the only key reader) with the judge
        # model in the validator slot; nothing here ever touches the key.
        try:
            judge_model = anthropic_bundle(
                eval_settings.model_copy(update={"validator_model": eval_settings.judge_model})
            ).validator
        except ConfigError as exc:
            raise EvalExit(EXIT_MISSING, f"--judge needs the real judge model: {exc}") from exc
        runtime = _build_runtime(
            eval_settings, models=replay_bundle(paths.recorded), checkpointer=_memory_checkpointer()
        )
        try:
            cases = _outreach_cases(gold, runtime, run_id=_eval_run_id("outreach", paths))
        finally:
            runtime.close()
        records = judge_cases(cases, judge_model, judge_model_id=eval_settings.judge_model)
        write_judge_records(records, paths.judge_recordings)
        print(f"wrote {paths.judge_recordings} ({len(records)} judged plans)")
    else:
        try:
            records = read_judge_records(paths.judge_recordings)
        except FileNotFoundError as exc:
            raise EvalExit(
                EXIT_MISSING,
                f"no recorded judge outputs at {paths.judge_recordings}; run "
                "`caregap eval --tier outreach --judge` locally with a key, then commit the file",
            ) from exc
    splits = {case.patient_id: case.split for case in gold}
    records = [
        r.model_copy(update={"split": splits.get(r.patient_id, r.split)})
        for r in sorted(records, key=lambda r: r.case_key)
    ]
    report = score_judgements(records)
    labels = read_spotcheck(paths.spotcheck)
    agreement = judge_human_agreement(records, labels)
    judge_models = sorted({r.judge_model for r in records}) or [eval_settings.judge_model]
    rubric_shas = sorted({r.rubric_sha256 for r in records})
    stamp = _stamp(
        paths,
        "outreach",
        models={"judge_model": ",".join(judge_models)},
        rubric_sha256=RUBRIC.sha256,
        judge_human_agreement=agreement,
    )
    parse_failures = sum(
        1 for item in report.per_item if item.metrics.get("judge_parse_failure") == 1.0
    )
    extra: dict[str, object] = {
        "rubric_name": RUBRIC.name,
        "rubric_drift": bool(rubric_shas and rubric_shas != [RUBRIC.sha256]),
        "judged_count": len(records),
        "judge_parse_failures": parse_failures,
        "judge_human_agreement_status": ("pending" if agreement is None else f"{agreement:.3f}"),
        "untrusted": agreement is not None and agreement < UNTRUSTED_BELOW,
        "spotcheck_labels": len(labels),
        "spotcheck_sample": [r.case_key for r in spotcheck_sample(records)],
        "judged_this_run": judge,
        "freeze": _freeze_block(paths),
    }
    return build_artifact(report, stamp, tier="outreach", extra=extra, include_per_item=True)


# --- entry points -------------------------------------------------------------------------------


def _print_summary(tier: str, artifact: Mapping[str, object], path: Path) -> None:
    metrics = artifact.get("metrics")
    overall = metrics.get("overall") if isinstance(metrics, Mapping) else None
    print(f"{tier} tier (test split):")
    if isinstance(overall, Mapping) and overall:
        for name, value in sorted(overall.items()):
            print(f"  {name:<24} {value:.4f}" if isinstance(value, float) else f"  {name} {value}")
    else:
        print("  (no test-split metric is defined)")
    for key in ("publication_status", "unpublishable_reason", "judge_human_agreement_status"):
        if artifact.get(key) not in (None, ""):
            print(f"  {key:<24} {artifact[key]}")
    publication = artifact.get("publication")
    if isinstance(publication, Mapping):
        print(f"  publication guard        {publication.get('note')}")
    print(f"wrote {path}")


def run_eval(
    tier: Tier,
    *,
    record: bool = False,
    judge: bool = False,
    regen: bool = False,
    update_baseline: bool = False,
    gate: bool = False,
    settings: Settings | None = None,
    out_dir: Path | None = None,
) -> int:
    """Run one tier end to end; returns the process exit code (0 / 1 / 2, see module doc)."""
    paths = EvalPaths(out_dir if out_dir is not None else EVALS_DIR)
    try:
        gold = _load_gold(paths)
        if tier == "engine":
            artifact = _run_engine_tier(paths, settings, gold, regen=regen)
        elif tier == "pipeline":
            artifact = _run_pipeline_tier(paths, settings, gold, record=record)
        elif tier == "outreach":
            artifact = _run_outreach_tier(paths, settings, gold, judge=judge)
        else:
            raise EvalExit(EXIT_MISSING, f"unknown tier {tier!r}")
        artifact_path = paths.artifact(tier)
        write_artifact(artifact_path, artifact)
        _print_summary(tier, artifact, artifact_path)
        if artifact.get("publishable") is False and (gate or update_baseline):
            # V13: fallback-sentinel numbers were never measured; they can neither set nor
            # pass the ratchet. The engine-only column is gated by the engine tier itself.
            raise EvalExit(
                EXIT_FAIL,
                f"unpublishable artifact ({artifact.get('unpublishable_reason')}) cannot "
                f"{'set' if update_baseline else 'pass'} the ratchet; nothing publishable "
                "to gate",
            )
        if gate:
            try:
                passed, message = gates.check_gates(artifact, paths.baseline)
            except FileNotFoundError as exc:
                raise EvalExit(
                    EXIT_MISSING,
                    f"no baseline at {paths.baseline}; run "
                    f"`caregap eval --tier {tier} --update-baseline` after a reviewed run",
                ) from exc
            print(message)
            if not passed:
                raise EvalExit(EXIT_FAIL, "gated metrics regressed against the baseline")
        if update_baseline:
            merged = gates.update_baseline(
                artifact, paths.baseline, source=f"{artifact_path.name}@{artifact.get('git_sha')}"
            )
            print(f"baseline updated at {paths.baseline}: " + ", ".join(sorted(merged)))
    except EvalExit as exc:
        print(f"caregap eval --tier {tier}: {exc.message}", file=sys.stderr)
        return exc.code
    return EXIT_OK


# --- README regions -----------------------------------------------------------------------------


def _read_artifact(path: Path) -> Artifact | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _fmt(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _metrics_line(block: object) -> str:
    if not isinstance(block, Mapping) or not block:
        return NOT_MEASURED
    return " · ".join(f"{k}={_fmt(v)}" for k, v in sorted(block.items()))


def render_measures_region(
    primary: Mapping[str, object],
    *,
    pipeline: Mapping[str, object] | None,
    outreach: Mapping[str, object] | None,
) -> str:
    """The per-measure counts table plus the publication / agent-tier notes, from artifacts."""
    lines = [
        "| measure | n_test | tp | fp | fn | precision | recall | f1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rows = primary.get("measures")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        insufficient = row.get("status") == "insufficient"
        scores = (
            ["insufficient"] * 3
            if insufficient
            else [_fmt(row.get("precision")), _fmt(row.get("recall")), _fmt(row.get("f1"))]
        )
        lines.append(
            f"| {row.get('measure')} | {row.get('n_test')} | {row.get('tp')} | {row.get('fp')} | "
            f"{row.get('fn')} | " + " | ".join(scores) + " |"
        )
    label = PRIMARY_LABEL if primary.get("tier") == "pipeline" else ENGINE_LABEL
    dataset = str(primary.get("dataset_hash", ""))[:12]
    outcomes = str(primary.get("engine_outcomes_sha256", ""))[:12]
    lines.append("")
    # Content hashes only: the git sha changes on every commit and would make `report --check`
    # fail in CI right after `eval` rewrites the artifact (the artifact file keeps the sha).
    lines.append(
        f"_Source: {primary.get('tier')}-latest.json ({label}, test split) · "
        f"outcomes_sha256={outcomes} · gold_sha256={dataset}_"
    )
    publication = primary.get("publication")
    note = publication.get("note") if isinstance(publication, Mapping) else None
    lines.append(f"_Publication guard: {note or NOT_MEASURED}._")
    metrics = primary.get("metrics")
    strict = metrics.get("strict_overall") if isinstance(metrics, Mapping) else None
    lines.append(f"_Strict variant (needs_review counts as no gap): {_metrics_line(strict)}_")
    if pipeline is None:
        lines.append(f"_Pipeline tier (Engine+validator, replayed recordings): {NOT_MEASURED}._")
    elif not pipeline.get("publishable"):
        lines.append(
            "_Pipeline tier (Engine+validator): unpublishable — "
            f"{pipeline.get('unpublishable_reason')}; engine-only numbers are shown._"
        )
    if outreach is None:
        lines.append(f"_Outreach faithfulness (LLM judge): {NOT_MEASURED}._")
    else:
        agreement = outreach.get("judge_human_agreement_status", "pending")
        banner = (
            f"**UNTRUSTED** (judge-human agreement {agreement} < {UNTRUSTED_BELOW:.2f})"
            if outreach.get("untrusted")
            else f"judge-human agreement {agreement}"
        )
        outreach_metrics = outreach.get("metrics")
        overall = outreach_metrics.get("overall") if isinstance(outreach_metrics, Mapping) else None
        lines.append(
            f"_Outreach faithfulness (LLM judge, test split): {_metrics_line(overall)} · "
            f"{banner} · judged={outreach.get('judged_count')}_"
        )
    return "\n".join(lines)


def _replace_region(text: str, begin: str, end: str, body: str, readme: Path) -> str:
    start = text.find(begin)
    stop = text.find(end)
    if start == -1 or stop == -1 or stop < start:
        raise ReportError(f"README markers {begin} / {end} missing or out of order in {readme}")
    return text[: start + len(begin)] + "\n" + body + "\n" + text[stop:]


def _readme_labels(primary: Mapping[str, object]) -> tuple[tuple[str, str], str]:
    return COMPARISON, PRIMARY_LABEL if primary.get("tier") == "pipeline" else ENGINE_LABEL


def _write_readme(readme: Path, text: str) -> None:
    with readme.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def placeholder_regions(text: str, readme: Path) -> str:
    """Both regions carrying the one-line ``not yet measured`` placeholders (idempotent)."""
    updated = _replace_region(text, EVAL_BEGIN, EVAL_END, EVAL_PLACEHOLDER, readme)
    return _replace_region(updated, MEASURES_BEGIN, MEASURES_END, MEASURES_PLACEHOLDER, readme)


def _sync_placeholders(readme: Path, artifacts: Path, *, check: bool) -> int:
    """No artifact exists: the README must say so (never a stale or invented number)."""
    text = readme.read_text(encoding="utf-8")
    updated = placeholder_regions(text, readme)
    hint = f"no eval artifact under {artifacts}; run `caregap eval --tier engine` first"
    if check:
        if updated == text:
            print(f"README eval regions are in sync: '{NOT_MEASURED}' placeholders ({hint})")
            return EXIT_OK
        print(
            f"caregap report --check: README eval regions must carry the '{NOT_MEASURED}' "
            f"placeholders ({hint}); run `caregap report`",
            file=sys.stderr,
        )
        return EXIT_FAIL
    _write_readme(readme, updated)
    print(f"README eval regions set to '{NOT_MEASURED}' ({hint})")
    return EXIT_OK


def _readme_view(artifact: Mapping[str, object]) -> dict[str, object]:
    """The artifact as the README sees it: without ``git_sha``, which changes on every commit
    and would make ``report --check`` fail in CI right after ``eval`` rewrites the artifact.
    The stamp line then carries content hashes only (config, gold); the artifact keeps the sha."""
    return {key: value for key, value in artifact.items() if key != "git_sha"}


def sync_readme_cmd(
    *, check: bool = False, readme: Path = README_PATH, artifacts_dir: Path | None = None
) -> int:
    """Render (or, with ``check``, verify) both README regions from the latest artifacts:
    the pipeline artifact when present and publishable, else the engine artifact; the
    ``not yet measured`` placeholders when there is no artifact at all. Exit 0 when the README
    is (or was made) consistent with the artifacts, 1 when it is stale or lacks the markers."""
    artifacts = artifacts_dir if artifacts_dir is not None else EVALS_DIR / "artifacts"
    engine = _read_artifact(artifacts / "engine-latest.json")
    pipeline = _read_artifact(artifacts / "pipeline-latest.json")
    outreach = _read_artifact(artifacts / "outreach-latest.json")
    primary = pipeline if pipeline is not None and pipeline.get("publishable") else engine
    for name, payload in (("engine", engine), ("pipeline", pipeline), ("outreach", outreach)):
        status = _unfrozen(payload)
        if status is not None:
            print(
                f"caregap report: {artifacts / f'{name}-latest.json'} was scored on gold with "
                f"freeze status {status!r}; only artifacts scored on frozen gold are published "
                f"(freeze the gold set, then re-run `caregap eval --tier {name}`)",
                file=sys.stderr,
            )
            return EXIT_FAIL
    try:
        if primary is None:
            return _sync_placeholders(readme, artifacts, check=check)
        comparison, label = _readme_labels(primary)
        measures_block = render_measures_region(primary, pipeline=pipeline, outreach=outreach)
        primary = _readme_view(primary)
        text = readme.read_text(encoding="utf-8")
        if check:
            eval_ok = readme_in_sync(readme, primary, comparison=comparison, primary_label=label)
            measures_ok = (
                _replace_region(text, MEASURES_BEGIN, MEASURES_END, measures_block, readme) == text
            )
            if eval_ok and measures_ok:
                print(f"README eval regions are in sync with {artifacts}")
                return EXIT_OK
            print(
                "caregap report --check: README eval regions are out of sync; run `caregap report`",
                file=sys.stderr,
            )
            return EXIT_FAIL
        sync_readme(readme, primary, comparison=comparison, primary_label=label)
        updated = _replace_region(
            readme.read_text(encoding="utf-8"), MEASURES_BEGIN, MEASURES_END, measures_block, readme
        )
    except ReportError as exc:
        print(f"caregap report: {exc}", file=sys.stderr)
        return EXIT_FAIL
    _write_readme(readme, updated)
    print(f"README eval regions synced from {primary.get('tier')}-latest.json")
    return EXIT_OK
