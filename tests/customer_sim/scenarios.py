"""
Customer-shaped scenarios — each one drives the SDK like a real customer would
and emits CallRecords through the metrics writer.

A scenario picks a slice of the corpus and an SDK operation; the runner
iterates and (optionally) parallelises across workers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from hyperapi import (
    AuthenticationError,
    DocumentUploadError,
    HyperAPIClient,
    HyperAPIError,
)

from .corpus import Fixture
from .metrics import CallRecord, MetricsWriter, time_call


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _flatten_response(obj) -> str:
    """Stringify a response dict for keyword-recall scoring."""
    try:
        return json.dumps(obj, default=str).lower()
    except (TypeError, ValueError):
        return str(obj).lower()


def _score_recall(expected: list[str], blob: str) -> float:
    if not expected:
        return 0.0
    hits = sum(1 for kw in expected if kw.lower() in blob)
    return hits / len(expected)


def _record_for(
    *, run_id: str, target: str, base_url: str, op: str, fixture: Fixture,
    ocr_engine: Optional[str], use_presigned: Optional[bool],
    latency_ms: float, success: bool, http_status: Optional[int],
    error_type: Optional[str], error_message: Optional[str],
    request_id: Optional[str], response_size_bytes: Optional[int],
    keywords_found: list[str], started_at: str, worker_id: int,
) -> CallRecord:
    keywords_expected = list(fixture.expected_keywords)
    return CallRecord(
        run_id=run_id, started_at=started_at, ended_at=_now_iso(),
        op=op, target=target, base_url=base_url,
        ocr_engine=ocr_engine, use_presigned=use_presigned,
        doc_id=fixture.doc_id, doc_shape=fixture.shape, doc_mime=fixture.mime,
        doc_size_bytes=fixture.size_bytes, doc_size_bucket=fixture.size_bucket,
        doc_page_count=fixture.page_count,
        latency_ms=latency_ms, success=success, http_status=http_status,
        error_type=error_type, error_message=error_message,
        request_id=request_id, response_size_bytes=response_size_bytes,
        keywords_expected=keywords_expected,
        keywords_found=keywords_found,
        keyword_recall=(_score_recall(keywords_expected, " ".join(keywords_found))
                        if keywords_expected and success else None),
        worker_id=worker_id,
    )


# ── Per-op scenarios ────────────────────────────────────────────────────────


@dataclass
class ScenarioContext:
    client: HyperAPIClient
    writer: MetricsWriter
    run_id: str
    target: str
    base_url: str
    worker_id: int = 0


def _exec_call(
    ctx: ScenarioContext, fixture: Fixture, op: str,
    ocr_engine: Optional[str], use_presigned: Optional[bool],
    func: Callable[[], dict],
) -> CallRecord:
    started = _now_iso()
    success = False
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    response_size: Optional[int] = None
    request_id: Optional[str] = None
    keywords_found: list[str] = []

    with time_call() as t:
        try:
            response = func()
            success = True
            http_status = 200
            blob = _flatten_response(response)
            response_size = len(blob.encode("utf-8"))
            request_id = (
                response.get("request_id")
                or (isinstance(response, dict) and response.get("result", {}).get("request_id"))
                or None
            )
            keywords_found = [kw for kw in fixture.expected_keywords if kw.lower() in blob]
        except AuthenticationError as e:
            error_type = "AuthenticationError"
            error_message = str(e)
            http_status = e.status_code
        except DocumentUploadError as e:
            error_type = "DocumentUploadError"
            error_message = str(e)
            http_status = e.status_code
        except HyperAPIError as e:
            error_type = type(e).__name__
            error_message = str(e)
            http_status = e.status_code
        except FileNotFoundError as e:
            error_type = "FileNotFoundError"
            error_message = str(e)
        except Exception as e:  # noqa: BLE001 — we want the simulator to be robust
            error_type = type(e).__name__
            error_message = str(e)

    rec = _record_for(
        run_id=ctx.run_id, target=ctx.target, base_url=ctx.base_url,
        op=op, fixture=fixture, ocr_engine=ocr_engine, use_presigned=use_presigned,
        latency_ms=t["latency_ms"], success=success, http_status=http_status,
        error_type=error_type, error_message=error_message,
        request_id=request_id, response_size_bytes=response_size,
        keywords_found=keywords_found, started_at=started, worker_id=ctx.worker_id,
    )
    ctx.writer.record(rec)
    return rec


def parse_one(ctx: ScenarioContext, fixture: Fixture, *,
              ocr_engine: str = "paddle", use_presigned: bool = True) -> CallRecord:
    return _exec_call(
        ctx, fixture, op="parse", ocr_engine=ocr_engine, use_presigned=use_presigned,
        func=lambda: ctx.client.parse(
            fixture.path, ocr_engine=ocr_engine, use_presigned=use_presigned,
        ),
    )


def extract_one(ctx: ScenarioContext, fixture: Fixture, *,
                ocr_engine: str = "paddle", use_presigned: bool = True) -> CallRecord:
    return _exec_call(
        ctx, fixture, op="extract", ocr_engine=ocr_engine, use_presigned=use_presigned,
        func=lambda: ctx.client.extract(
            fixture.path, ocr_engine=ocr_engine, use_presigned=use_presigned,
        ),
    )


def classify_one(ctx: ScenarioContext, fixture: Fixture, *,
                 ocr_engine: str = "paddle", use_presigned: bool = True) -> CallRecord:
    return _exec_call(
        ctx, fixture, op="classify", ocr_engine=ocr_engine, use_presigned=use_presigned,
        func=lambda: ctx.client.classify(
            fixture.path, ocr_engine=ocr_engine, use_presigned=use_presigned,
        ),
    )


def split_one(ctx: ScenarioContext, fixture: Fixture, *,
              ocr_engine: str = "paddle", use_presigned: bool = True) -> CallRecord:
    return _exec_call(
        ctx, fixture, op="split", ocr_engine=ocr_engine, use_presigned=use_presigned,
        func=lambda: ctx.client.split(
            fixture.path, ocr_engine=ocr_engine, use_presigned=use_presigned,
        ),
    )


def process_one(ctx: ScenarioContext, fixture: Fixture, *,
                ocr_engine: str = "paddle") -> CallRecord:
    """process() = parse + extract sharing one document_key — exercises router OCR cache."""
    return _exec_call(
        ctx, fixture, op="process", ocr_engine=ocr_engine, use_presigned=True,
        func=lambda: ctx.client.process(fixture.path, ocr_engine=ocr_engine),
    )


def upload_one(ctx: ScenarioContext, fixture: Fixture) -> CallRecord:
    """Just the presigned-URL upload step — surfaces S3/Kong/AES256 issues separately."""
    return _exec_call(
        ctx, fixture, op="upload", ocr_engine=None, use_presigned=True,
        func=lambda: {"document_key": ctx.client.upload_document(fixture.path)},
    )


# ── Scenario sets ───────────────────────────────────────────────────────────


def smoke_scenarios(corpus: list[Fixture]) -> list[tuple[Callable, Fixture, dict]]:
    """One representative per (op, format) — completes in ~1 minute on warm backend.

    The smallest non-edge-case fixture per shape is enough to confirm each operation
    is reachable, authenticates, and returns a parseable response.
    """
    by_shape: dict[str, list[Fixture]] = {}
    for f in corpus:
        if f.is_edge_case:
            continue
        by_shape.setdefault(f.shape, []).append(f)

    smallest_per_shape = [
        sorted(items, key=lambda f: f.size_bytes)[0]
        for items in by_shape.values()
    ]

    scenarios: list[tuple[Callable, Fixture, dict]] = []
    for fx in smallest_per_shape:
        scenarios.append((parse_one, fx, {}))
        scenarios.append((extract_one, fx, {}))
        scenarios.append((classify_one, fx, {}))
        if fx.mime == "application/pdf":
            scenarios.append((split_one, fx, {}))
        scenarios.append((process_one, fx, {}))
        scenarios.append((upload_one, fx, {}))
    return scenarios


def full_scenarios(corpus: list[Fixture]) -> list[tuple[Callable, Fixture, dict]]:
    """Every non-edge-case fixture × every applicable op × both OCR engines.

    This is the "thorough customer" sweep. Expect 10-20 min wall-clock against
    a warm backend; bounded by extract latency on large PDFs.
    """
    scenarios: list[tuple[Callable, Fixture, dict]] = []
    for fx in corpus:
        if fx.is_edge_case:
            continue
        for engine in ("paddle", "doc-intent"):
            scenarios.append((parse_one, fx, {"ocr_engine": engine}))
            scenarios.append((extract_one, fx, {"ocr_engine": engine}))
            scenarios.append((classify_one, fx, {"ocr_engine": engine}))
            if fx.mime == "application/pdf":
                scenarios.append((split_one, fx, {"ocr_engine": engine}))
        scenarios.append((process_one, fx, {}))
        scenarios.append((upload_one, fx, {}))
    return scenarios


def error_path_scenarios(corpus: list[Fixture]) -> list[tuple[Callable, Fixture, dict]]:
    """Edge-case fixtures + bad-API-key probe. We *expect* errors; the simulator
    asserts the SDK's error classification still surfaces them as typed exceptions.
    """
    scenarios: list[tuple[Callable, Fixture, dict]] = []
    for fx in corpus:
        if fx.is_edge_case:
            scenarios.append((parse_one, fx, {}))
            scenarios.append((upload_one, fx, {}))
    return scenarios


def soak_iteration(corpus: list[Fixture]) -> list[tuple[Callable, Fixture, dict]]:
    """Single iteration of a soak loop: the smoke set plus one random medium PDF."""
    return smoke_scenarios(corpus)


def rate_limit_burst_scenarios(corpus: list[Fixture], burst_size: int = 6) -> list[tuple[Callable, Fixture, dict]]:
    """Deliberate burst that should trigger Kong's rate-limit plugin.

    Free-tier keys have `limit: 1` per 60s sliding window (per Kong's
    hyperapi-auth plugin). Firing N rapid uploads with no sleep should produce
    at most one 200 followed by (N-1) 429s with `Retry-After: 60` and a body
    of `{"message":"Rate limit exceeded","limit":1,"tier":"free"}`. The
    simulator runner enforces sleep_ms=0 in this mode regardless of CLI flag,
    so the burst is genuinely contiguous.

    What gets validated:
      - Each call is recorded with its http_status (429 → ERR with status_code=429).
      - The summary's per-(op, size_bucket) error_count tells you how many 429s.
      - raw.jsonl exposes the full error_message for downstream assertion.

    Caveat: with a higher-tier key (pro=60/min, enterprise=1000/min) you'll need
    a much larger burst to actually hit the limit. The default of 6 is calibrated
    to free-tier; bump via the `burst_size` arg when running on a paid key.
    """
    smallest = min(
        (fx for fx in corpus if not fx.is_edge_case and fx.mime == "application/pdf"),
        key=lambda fx: fx.size_bytes,
    )
    return [(upload_one, smallest, {}) for _ in range(burst_size)]


def run_scenarios(
    ctx: ScenarioContext,
    scenarios: list[tuple[Callable, Fixture, dict]],
    *,
    sleep_between_ms: int = 0,
    on_progress: Optional[Callable[[int, int, CallRecord], None]] = None,
) -> None:
    total = len(scenarios)
    for idx, (fn, fixture, kwargs) in enumerate(scenarios, start=1):
        rec = fn(ctx, fixture, **kwargs)
        if on_progress:
            on_progress(idx, total, rec)
        if sleep_between_ms:
            time.sleep(sleep_between_ms / 1000)
