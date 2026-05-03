"""
Metrics capture and reporting.

One JSONL row per call (raw.jsonl). One summary.json with aggregated percentiles
per (op, size_bucket). Both are intentionally simple: the goal is to ship the
raw record so any downstream tool (Grafana, BigQuery, jq) can re-aggregate.
"""

from __future__ import annotations

import json
import math
import os
import socket
import statistics
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass
class CallRecord:
    """Single SDK call observation — what a customer would experience."""

    run_id: str
    started_at: str
    ended_at: str
    op: str                      # parse | extract | classify | split | process | upload
    target: str                  # local | preprod | custom
    base_url: str
    ocr_engine: Optional[str]
    use_presigned: Optional[bool]

    doc_id: str
    doc_shape: str
    doc_mime: str
    doc_size_bytes: int
    doc_size_bucket: str
    doc_page_count: int

    latency_ms: float
    success: bool
    http_status: Optional[int]
    error_type: Optional[str]
    error_message: Optional[str]
    request_id: Optional[str]
    response_size_bytes: Optional[int]

    # OCR-accuracy spot check: did the response contain every expected keyword?
    keywords_expected: list[str] = field(default_factory=list)
    keywords_found: list[str] = field(default_factory=list)
    keyword_recall: Optional[float] = None

    worker_id: int = 0


class MetricsWriter:
    """Append-only JSONL writer plus in-memory roll-up."""

    def __init__(self, run_dir: Path, run_id: str, target: str, base_url: str):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.target = target
        self.base_url = base_url
        self.raw_path = run_dir / "raw.jsonl"
        self._records: list[CallRecord] = []
        self._fp = self.raw_path.open("a", encoding="utf-8")

    def record(self, rec: CallRecord) -> None:
        self._records.append(rec)
        self._fp.write(json.dumps(asdict(rec)) + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()

    @property
    def records(self) -> list[CallRecord]:
        return list(self._records)

    def summarize(self) -> dict[str, Any]:
        return summarize(self._records, run_id=self.run_id, target=self.target,
                         base_url=self.base_url)


@contextmanager
def time_call() -> Iterator[dict[str, float]]:
    """Context manager that captures wall-clock latency in ms."""
    holder = {"t0": time.perf_counter(), "latency_ms": 0.0}
    try:
        yield holder
    finally:
        holder["latency_ms"] = (time.perf_counter() - holder["t0"]) * 1000


def percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def summarize(records: list[CallRecord], *, run_id: str, target: str,
              base_url: str) -> dict[str, Any]:
    """Produce the aggregated summary that ships alongside raw.jsonl."""
    by_group: dict[tuple[str, str], list[CallRecord]] = {}
    for r in records:
        by_group.setdefault((r.op, r.doc_size_bucket), []).append(r)

    groups = []
    for (op, bucket), recs in sorted(by_group.items()):
        latencies_ok = [r.latency_ms for r in recs if r.success]
        recalls = [r.keyword_recall for r in recs if r.keyword_recall is not None]
        groups.append({
            "op": op,
            "size_bucket": bucket,
            "count": len(recs),
            "success_count": sum(1 for r in recs if r.success),
            "error_count": sum(1 for r in recs if not r.success),
            "error_rate": (sum(1 for r in recs if not r.success) / len(recs)) if recs else 0.0,
            "latency_ms": {
                "p50": percentile(latencies_ok, 0.50),
                "p95": percentile(latencies_ok, 0.95),
                "p99": percentile(latencies_ok, 0.99),
                "max": max(latencies_ok) if latencies_ok else None,
                "mean": statistics.fmean(latencies_ok) if latencies_ok else None,
            },
            "ocr_keyword_recall_mean": (sum(recalls) / len(recalls)) if recalls else None,
            "errors_by_type": _count_by(recs, lambda r: r.error_type or ""),
            "errors_by_status": _count_by(recs, lambda r: str(r.http_status) if r.http_status else ""),
        })

    overall_latencies = [r.latency_ms for r in records if r.success]
    overall_recalls = [r.keyword_recall for r in records if r.keyword_recall is not None]

    return {
        "run_id": run_id,
        "target": target,
        "base_url": base_url,
        "host": socket.gethostname(),
        "started_at": records[0].started_at if records else _now_iso(),
        "ended_at": records[-1].ended_at if records else _now_iso(),
        "total_calls": len(records),
        "success_calls": sum(1 for r in records if r.success),
        "error_calls": sum(1 for r in records if not r.success),
        "error_rate": (sum(1 for r in records if not r.success) / len(records)) if records else 0.0,
        "overall_latency_ms": {
            "p50": percentile(overall_latencies, 0.50),
            "p95": percentile(overall_latencies, 0.95),
            "p99": percentile(overall_latencies, 0.99),
            "max": max(overall_latencies) if overall_latencies else None,
        },
        "ocr_keyword_recall_mean": (sum(overall_recalls) / len(overall_recalls)) if overall_recalls else None,
        "groups": groups,
    }


def _count_by(records: list[CallRecord], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        k = key(r)
        if not k:
            continue
        out[k] = out.get(k, 0) + 1
    return out


def render_markdown(summary: dict[str, Any]) -> str:
    """Human-readable run summary suitable for GitHub Actions step output."""
    lines = [
        f"# Customer Simulator Run — `{summary['run_id']}`",
        "",
        f"- **target:** `{summary['target']}` (`{summary['base_url']}`)",
        f"- **window:** {summary['started_at']} → {summary['ended_at']}",
        f"- **calls:** {summary['total_calls']} "
        f"({summary['success_calls']} ok, {summary['error_calls']} error, "
        f"{summary['error_rate']:.1%} error rate)",
    ]
    olm = summary["overall_latency_ms"]
    if olm["p50"] is not None:
        lines.append(
            f"- **latency:** p50={olm['p50']:.0f}ms  p95={olm['p95']:.0f}ms  "
            f"p99={olm['p99']:.0f}ms  max={olm['max']:.0f}ms"
        )
    if summary.get("ocr_keyword_recall_mean") is not None:
        lines.append(f"- **OCR keyword recall (mean):** {summary['ocr_keyword_recall_mean']:.1%}")

    lines += ["", "## Per (op, size) breakdown", "",
              "| op | size | n | err% | p50 | p95 | p99 | recall |",
              "|---|---|---|---|---|---|---|---|"]
    for g in summary["groups"]:
        lat = g["latency_ms"]
        recall = g.get("ocr_keyword_recall_mean")
        lines.append(
            f"| {g['op']} | {g['size_bucket']} | {g['count']} | "
            f"{g['error_rate']:.1%} | "
            f"{_fmt(lat['p50'])} | {_fmt(lat['p95'])} | {_fmt(lat['p99'])} | "
            f"{(f'{recall:.1%}' if recall is not None else '—')} |"
        )
    return "\n".join(lines) + "\n"


def _fmt(v: Optional[float]) -> str:
    return f"{v:.0f}ms" if v is not None else "—"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(2).hex()
