"""Freeze the blind gold labels BEFORE any engine contact (SPEC section 6).

Input: the JSON returned by the gold-protocol workflow ({prep, labels, second, subset}).
Output (evals/gold/): gap_cases.jsonl (labeler A, split by sha256(patient_id)), second_labeler.jsonl
(labeler B subset), AGREEMENT.md (A vs B), CODE_AUDIT.md (events outside the listed codes), and
FREEZE.json carrying the sha256 of gap_cases.jsonl. Refuses to overwrite an existing freeze.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MEASURES = ("CBP", "EED", "BCS", "COL", "SPC", "SPD", "TSC", "SNS")
STATUSES = ("not_eligible", "closed", "excluded", "open", "escalate")


def split_for(patient_id: str) -> str:
    digest = hashlib.sha256(patient_id.encode()).hexdigest()
    return "dev" if int(digest[:8], 16) % 3 == 0 else "test"


def collapsed(status: str) -> str:
    if status == "open":
        return "gap"
    if status == "escalate":
        return "escalate"
    return "no_gap"


def rows_for(result: dict[str, Any], as_of: str, labeler: str) -> list[dict[str, Any]]:
    pid = str(result["patient_id"])
    by_measure = {str(lb["measure_id"]): lb for lb in result["labels"]}
    rows: list[dict[str, Any]] = []
    for measure in MEASURES:
        lb = by_measure[measure]
        if lb["status"] not in STATUSES:
            raise SystemExit(f"bad status {lb['status']!r} for {pid}/{measure}")
        rows.append(
            {
                "patient_id": pid,
                "measure_id": measure,
                "as_of": as_of,
                "gold": lb["status"],
                "rationale": str(lb.get("rationale", ""))[:600],
                "decisive_dates": sorted(str(d) for d in lb.get("decisive_dates", [])),
                "uncertain": bool(lb.get("uncertain", False)),
                "labeler": labeler,
                "split": split_for(pid),
            }
        )
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows)
    path.write_text(text, encoding="utf-8", newline="\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_sha() -> str:
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        # Fixed argv, resolved executable, no shell: safe.
        return subprocess.check_output([git, "rev-parse", "HEAD"], text=True).strip()  # noqa: S603
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def agreement_md(a_rows: list[dict[str, Any]], b_rows: list[dict[str, Any]]) -> str:
    a = {(r["patient_id"], r["measure_id"]): r["gold"] for r in a_rows}
    b = {(r["patient_id"], r["measure_id"]): r["gold"] for r in b_rows}
    keys = sorted(set(a) & set(b))
    per: dict[str, list[bool]] = defaultdict(list)
    coll: list[bool] = []
    disagreements: list[str] = []
    for key in keys:
        same = a[key] == b[key]
        per[key[1]].append(same)
        coll.append(collapsed(a[key]) == collapsed(b[key]))
        if not same:
            disagreements.append(f"| {key[0][:8]} | {key[1]} | {a[key]} | {b[key]} |")
    n = len(keys)
    overall = sum(sum(v) for v in per.values()) / n if n else 0.0
    coll_rate = sum(coll) / n if n else 0.0
    lines = [
        "# Labeler agreement (independent blind LLM labelers A vs B)",
        "",
        f"Subset: {len({k[0] for k in keys})} patients x {len(MEASURES)} measures = {n} items.",
        "",
        f"- Exact status agreement: **{overall:.3f}**",
        f"- Collapsed (gap / no_gap / escalate) agreement: **{coll_rate:.3f}**",
        "",
        "| measure | n | exact agreement |",
        "|---|---:|---:|",
    ]
    for m in MEASURES:
        v = per.get(m, [])
        if v:
            lines.append(f"| {m} | {len(v)} | {sum(v) / len(v):.3f} |")
        else:
            lines.append(f"| {m} | 0 | - |")
    lines += ["", "## Disagreements", "", "| patient | measure | A | B |", "|---|---|---|---|"]
    lines += disagreements or ["| - | - | - | - |"]
    lines += [
        "",
        "Both labelers saw only LABELING_GUIDE.md and the patient worksheet; neither saw the "
        "engine.",
        "A human spot-check of these items is pending (see evals/README.md).",
        "",
    ]
    return "\n".join(lines)


def code_audit_md(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Code audit: events labelers flagged as matching a concept but outside the listed codes",
        "",
        "| patient | measure | event (code / display / date) |",
        "|---|---|---|",
    ]
    count = 0
    for res in sorted(results, key=lambda r: str(r["patient_id"])):
        for lb in res["labels"]:
            for ev in lb.get("events_outside_listed_codes", []):
                pid = str(res["patient_id"])[:8]
                lines.append(f"| {pid} | {lb['measure_id']} | {str(ev)[:160]} |")
                count += 1
    if count == 0:
        lines.append("| - | - | none reported |")
    lines += ["", f"Total flagged: {count}. Review at the adjudication step (dev split only).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", type=Path, required=True, help="workflow result JSON")
    ap.add_argument("--as-of", default="2025-12-31")
    ap.add_argument("--out", type=Path, default=Path("evals/gold"))
    ap.add_argument("--labeler", default="llm-blind-claude-v1")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    freeze_path = out / "FREEZE.json"
    if freeze_path.exists() and not args.force:
        print(f"refusing to overwrite {freeze_path} (use --force)", file=sys.stderr)
        return 2

    payload = json.loads(args.labels.read_text(encoding="utf-8"))
    a_results: list[dict[str, Any]] = payload["labels"]
    b_results: list[dict[str, Any]] = payload.get("second", [])
    a_sorted = sorted(a_results, key=lambda r: str(r["patient_id"]))
    b_sorted = sorted(b_results, key=lambda r: str(r["patient_id"]))
    a_rows = [r for res in a_sorted for r in rows_for(res, args.as_of, args.labeler + "-A")]
    b_rows = [r for res in b_sorted for r in rows_for(res, args.as_of, args.labeler + "-B")]

    gap_path = out / "gap_cases.jsonl"
    write_jsonl(gap_path, a_rows)
    write_jsonl(out / "second_labeler.jsonl", b_rows)
    (out / "AGREEMENT.md").write_text(agreement_md(a_rows, b_rows), encoding="utf-8", newline="\n")
    (out / "CODE_AUDIT.md").write_text(code_audit_md(a_results), encoding="utf-8", newline="\n")

    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for r in a_rows:
        counts[f"{r['split']}/{r['measure_id']}"][r["gold"]] += 1
    patients = {r["patient_id"] for r in a_rows}
    freeze = {
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_of": args.as_of,
        "gap_cases_sha256": sha256_file(gap_path),
        "n_patients": len(patients),
        "n_rows": len(a_rows),
        "split_patients": dict(Counter(split_for(p) for p in patients)),
        "gold_counts": {k: dict(v) for k, v in sorted(counts.items())},
        "uncertain_rows": sum(1 for r in a_rows if r["uncertain"]),
        "labeler": args.labeler,
        "engine_contact_before_freeze": False,
        "git_sha": git_sha(),
        "protocol": (
            "docs/SPEC.md section 6; labels by a blind LLM labeler over value-set-agnostic "
            "worksheets"
        ),
    }
    freeze_path.write_text(
        json.dumps(freeze, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    summary = {
        k: freeze[k]
        for k in ("n_patients", "n_rows", "split_patients", "uncertain_rows", "gap_cases_sha256")
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
