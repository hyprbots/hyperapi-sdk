"""
Compare a run's summary against a committed baseline and decide pass/fail.

Thresholds are intentionally generous — too strict and nightly noise creates
flake spam; too loose and real regressions slip. The defaults below come from
backend SLO discussions: p95 budgets are operation-specific.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Per-op p95 ceilings in ms. Anything above is a regression regardless of baseline.
HARD_P95_CEILINGS_MS = {
    "parse": 30_000,
    "extract": 180_000,
    "classify": 60_000,
    "split": 90_000,
    "process": 200_000,
    "upload": 15_000,
}

# Soft regression: fail if a (op, bucket) p95 is more than this multiple of baseline.
SOFT_P95_MULTIPLIER = 1.5

# Error rate: anything above 1% is a regression (5% for soak, configured separately).
DEFAULT_MAX_ERROR_RATE = 0.01

# OCR keyword recall: drop more than 10 percentage points → regression.
RECALL_DROP_PP = 0.10


@dataclass
class RegressionFinding:
    severity: str   # "fail" | "warn"
    op: Optional[str]
    bucket: Optional[str]
    metric: str
    observed: Optional[float]
    baseline: Optional[float]
    message: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "op": self.op, "bucket": self.bucket,
            "metric": self.metric, "observed": self.observed,
            "baseline": self.baseline, "message": self.message,
        }


def _group(summary: dict, op: str, bucket: str) -> Optional[dict]:
    for g in summary.get("groups", []):
        if g["op"] == op and g["size_bucket"] == bucket:
            return g
    return None


def detect_regressions(
    summary: dict,
    baseline: Optional[dict],
    *,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
) -> list[RegressionFinding]:
    findings: list[RegressionFinding] = []

    # Overall error rate
    err_rate = summary.get("error_rate", 0.0)
    if err_rate > max_error_rate:
        findings.append(RegressionFinding(
            severity="fail", op=None, bucket=None, metric="error_rate",
            observed=err_rate, baseline=max_error_rate,
            message=f"Overall error rate {err_rate:.1%} exceeds threshold {max_error_rate:.1%}",
        ))

    # Per-group checks
    for g in summary.get("groups", []):
        op, bucket = g["op"], g["size_bucket"]
        p95 = (g.get("latency_ms") or {}).get("p95")

        # Hard ceiling
        hard = HARD_P95_CEILINGS_MS.get(op)
        if p95 is not None and hard is not None and p95 > hard:
            findings.append(RegressionFinding(
                severity="fail", op=op, bucket=bucket, metric="p95_hard_ceiling",
                observed=p95, baseline=float(hard),
                message=f"{op}/{bucket} p95={p95:.0f}ms exceeds hard ceiling {hard}ms",
            ))

        # Soft regression vs baseline
        if baseline is not None:
            base_g = _group(baseline, op, bucket)
            if base_g and p95 is not None:
                base_p95 = (base_g.get("latency_ms") or {}).get("p95")
                if base_p95 and p95 > base_p95 * SOFT_P95_MULTIPLIER:
                    findings.append(RegressionFinding(
                        severity="fail", op=op, bucket=bucket, metric="p95_vs_baseline",
                        observed=p95, baseline=base_p95,
                        message=(f"{op}/{bucket} p95 regressed: {p95:.0f}ms vs "
                                 f"baseline {base_p95:.0f}ms (>{SOFT_P95_MULTIPLIER:.1f}x)"),
                    ))

            # OCR recall drop
            cur_recall = g.get("ocr_keyword_recall_mean")
            if base_g and cur_recall is not None:
                base_recall = base_g.get("ocr_keyword_recall_mean")
                if base_recall is not None and base_recall - cur_recall > RECALL_DROP_PP:
                    findings.append(RegressionFinding(
                        severity="fail", op=op, bucket=bucket, metric="ocr_recall",
                        observed=cur_recall, baseline=base_recall,
                        message=(f"{op}/{bucket} OCR keyword recall dropped "
                                 f"{cur_recall:.1%} vs baseline {base_recall:.1%}"),
                    ))

        # Per-group error rate
        if g.get("count", 0) >= 5 and g.get("error_rate", 0.0) > max_error_rate * 3:
            findings.append(RegressionFinding(
                severity="warn", op=op, bucket=bucket, metric="group_error_rate",
                observed=g["error_rate"], baseline=max_error_rate,
                message=f"{op}/{bucket} error rate {g['error_rate']:.1%} unusually high",
            ))

    return findings


def load_baseline(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_baseline(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))


def render_markdown(findings: list[RegressionFinding]) -> str:
    if not findings:
        return "No regressions detected.\n"
    lines = ["## Regression findings", ""]
    fails = [f for f in findings if f.severity == "fail"]
    warns = [f for f in findings if f.severity == "warn"]
    if fails:
        lines.append("### ❌ Failures")
        for f in fails:
            lines.append(f"- **{f.metric}** ({f.op or '-'}/{f.bucket or '-'}): {f.message}")
    if warns:
        lines += ["", "### ⚠️  Warnings"]
        for f in warns:
            lines.append(f"- **{f.metric}** ({f.op or '-'}/{f.bucket or '-'}): {f.message}")
    return "\n".join(lines) + "\n"
