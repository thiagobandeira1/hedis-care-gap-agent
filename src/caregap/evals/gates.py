"""Ratchet gates over MEASURED baselines (SPEC section 6): six gated metrics, 0.02 tolerance.

``evals/baseline.json`` is one flat ``{"metrics": {name: value}}`` block shared by the tiers:
the engine tier contributes the ``engine.*`` keys, the pipeline tier the headline keys plus
the two lower-is-better counts. ``compare_to_baseline`` skips gated metrics the baseline has
not measured yet and flags gated metrics that vanished from the current artifact. There is no
vacuous pass: an absent baseline raises so the caller exits 2 with a clear message.
"""

import json
from collections.abc import Mapping
from pathlib import Path

from clinevals import Gate, compare_to_baseline, load_baseline

TOLERANCE = 0.02

GATES: list[Gate] = [
    Gate("micro_precision"),
    Gate("micro_recall"),
    Gate("engine.micro_precision"),
    Gate("engine.micro_recall"),
    Gate("validator_unsafe_close", higher_is_better=False),
    Gate("unresolved_evidence", higher_is_better=False),
]

#: The subset of ``GATES`` each tier's artifact can be held to.
TIER_GATES: dict[str, tuple[str, ...]] = {
    "engine": ("engine.micro_precision", "engine.micro_recall"),
    "pipeline": tuple(gate.metric for gate in GATES),
    "outreach": (),
}

BASELINE_NOTE = (
    "Measured ratchet baseline on the TEST split (never aspirational). Written only by "
    "`caregap eval --tier <tier> --update-baseline`; CI fails when a gated metric moves more "
    "than 0.02 in the wrong direction. engine.* comes from the engine tier, the rest from the "
    "pipeline tier."
)


def _numbers(block: object) -> dict[str, float]:
    if not isinstance(block, Mapping):
        return {}
    return {
        str(key): float(value)
        for key, value in block.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }


def flatten_metrics(artifact: Mapping[str, object]) -> dict[str, float]:
    """The gate-able numbers of one artifact, keyed the way the baseline stores them.

    Engine tier: ``metrics.overall`` IS the engine-only column -> ``engine.*``. Pipeline
    tier: ``metrics.overall`` -> headline keys, ``metrics.engine_only_overall`` ->
    ``engine.*``, plus the two lower-is-better counts. Other tiers are not gated.
    """
    tier = str(artifact.get("tier", ""))
    metrics = artifact.get("metrics")
    metrics_map: Mapping[str, object] = metrics if isinstance(metrics, Mapping) else {}
    flat: dict[str, float] = {}
    if tier == "engine":
        for key, value in _numbers(metrics_map.get("overall")).items():
            flat[f"engine.{key}"] = value
    elif tier == "pipeline":
        flat.update(_numbers(metrics_map.get("overall")))
        for key, value in _numbers(metrics_map.get("engine_only_overall")).items():
            flat[f"engine.{key}"] = value
        for key in ("validator_unsafe_close", "unresolved_evidence"):
            raw = artifact.get(key)
            if isinstance(raw, int | float) and not isinstance(raw, bool):
                flat[key] = float(raw)
    return dict(sorted(flat.items()))


def gates_for(tier: str) -> list[Gate]:
    wanted = set(TIER_GATES.get(tier, ()))
    return [gate for gate in GATES if gate.metric in wanted]


def check_gates(artifact: Mapping[str, object], baseline_path: Path) -> tuple[bool, str]:
    """``(passed, message)``; raises ``FileNotFoundError`` when the baseline is absent."""
    baseline = load_baseline(baseline_path)
    tier = str(artifact.get("tier", ""))
    gates = gates_for(tier)
    if not gates:
        return True, f"ratchet gate: no gated metrics for tier {tier!r}"
    current = flatten_metrics(artifact)
    regressions = compare_to_baseline(current, baseline, gates=gates, tolerance=TOLERANCE)
    if regressions:
        return False, "ratchet gate: FAIL\n" + "\n".join(f"  REGRESSION: {r}" for r in regressions)
    measured = [gate.metric for gate in gates if gate.metric in baseline]
    if not measured:
        return True, (
            f"ratchet gate: PASS (no gated metric of tier {tier!r} is in the baseline yet; "
            "run --update-baseline to start ratcheting)"
        )
    return True, "ratchet gate: PASS (" + ", ".join(measured) + ")"


def update_baseline(
    artifact: Mapping[str, object], baseline_path: Path, *, source: str
) -> dict[str, float]:
    """Merge the artifact's flattened metrics into the baseline (other tiers' keys kept);
    returns the merged flat block. Deterministic bytes: sorted keys, LF, trailing newline."""
    existing = load_baseline(baseline_path) if baseline_path.exists() else {}
    merged = dict(sorted({**existing, **flatten_metrics(artifact)}.items()))
    sources: dict[str, str] = {}
    if baseline_path.exists():
        raw = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
            sources = {str(k): str(v) for k, v in raw["sources"].items()}
    sources[str(artifact.get("tier", "unknown"))] = source
    payload = {"metrics": merged, "note": BASELINE_NOTE, "sources": dict(sorted(sources.items()))}
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    with baseline_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    return merged
