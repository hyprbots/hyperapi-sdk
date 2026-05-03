"""
Customer simulator entrypoint.

Examples
--------
Smoke against a local docker-compose backend::

    HYPERAPI_KEY=hk_test_xxx HYPERAPI_URL=http://localhost:8000 \
        python -m tests.customer_sim --target local --mode smoke

Full sweep, 4 concurrent customers, against preprod (CI-only)::

    python -m tests.customer_sim --target preprod --mode full --workers 4

Soak loop for 30 minutes (skips regression compare; just exercises the API)::

    python -m tests.customer_sim --target local --mode soak --duration 30
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from hyperapi import HyperAPIClient

from .corpus import Fixture, load_corpus
from .metrics import (
    MetricsWriter,
    new_run_id,
    render_markdown as render_summary_md,
)
from .regression import (
    detect_regressions,
    load_baseline,
    render_markdown as render_findings_md,
    write_baseline,
)
from .scenarios import (
    ScenarioContext,
    error_path_scenarios,
    full_scenarios,
    rate_limit_burst_scenarios,
    smoke_scenarios,
    soak_iteration,
)


REPORTS_ROOT = Path(__file__).resolve().parent / "reports"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"


# ── URL resolution ──────────────────────────────────────────────────────────

PREPROD_URL = "https://hyperapi-production-12097051.us-east-1.elb.amazonaws.com"


def _resolve_base_url(target: str, override: Optional[str]) -> str:
    if override:
        return override
    if target == "local":
        return os.environ.get("HYPERAPI_URL") or "http://localhost:8000"
    if target == "preprod":
        return PREPROD_URL
    if target == "custom":
        env = os.environ.get("HYPERAPI_URL")
        if not env:
            raise SystemExit("--target custom requires HYPERAPI_URL to be set")
        return env
    raise SystemExit(f"Unknown target: {target}")


def _require_api_key() -> str:
    key = os.environ.get("HYPERAPI_KEY")
    if not key:
        raise SystemExit(
            "HYPERAPI_KEY is not set. Customer simulator requires a real API key.\n"
            "  - For local runs: use any test key seeded into your docker-compose backend.\n"
            "  - For preprod runs: pass via GitHub Secrets (HYPERAPI_KEY_PREPROD).\n"
            "  - NEVER point this at production. The QA agent forbids it.",
        )
    return key


# ── Run orchestration ───────────────────────────────────────────────────────


def _run_single_threaded(ctx_factory: Callable[[int], ScenarioContext],
                         scenarios: list, on_progress, sleep_ms: int = 0) -> None:
    ctx = ctx_factory(0)
    try:
        for idx, (fn, fixture, kwargs) in enumerate(scenarios, start=1):
            rec = fn(ctx, fixture, **kwargs)
            on_progress(idx, len(scenarios), rec)
            if sleep_ms and idx < len(scenarios):
                time.sleep(sleep_ms / 1000)
    finally:
        ctx.client.close()


def _run_concurrent(ctx_factory: Callable[[int], ScenarioContext],
                    scenarios: list, workers: int, on_progress) -> None:
    """Each worker holds its own HyperAPIClient (httpx Client is not safe to share)."""
    chunks: list[list] = [[] for _ in range(workers)]
    for i, item in enumerate(scenarios):
        chunks[i % workers].append(item)

    counter = {"done": 0}
    lock = threading.Lock()
    total = len(scenarios)

    def _worker(worker_id: int, items: list):
        ctx = ctx_factory(worker_id)
        try:
            for fn, fixture, kwargs in items:
                rec = fn(ctx, fixture, **kwargs)
                with lock:
                    counter["done"] += 1
                    on_progress(counter["done"], total, rec)
        finally:
            ctx.client.close()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_worker, wid, items) for wid, items in enumerate(chunks) if items]
        for f in as_completed(futs):
            f.result()  # surface exceptions


# ── Progress reporting ──────────────────────────────────────────────────────


def _print_progress(idx: int, total: int, rec) -> None:
    status = "OK " if rec.success else f"ERR ({rec.error_type or '?'})"
    sys.stdout.write(
        f"[{idx:>4}/{total}] {rec.op:<8} {rec.doc_id:<28} "
        f"{rec.doc_size_bucket:<6} {rec.latency_ms:>7.0f}ms  {status}\n"
    )
    sys.stdout.flush()


# ── Main ────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.customer_sim")
    parser.add_argument("--target", choices=["local", "preprod", "custom"], required=True,
                        help="Backend target. preprod uses the cluster ALB; never run against prod.")
    parser.add_argument("--base-url", default=None, help="Override the resolved base URL.")
    parser.add_argument("--mode", choices=["smoke", "full", "soak", "error-paths", "rate-limit"], default="smoke")
    parser.add_argument("--burst-size", type=int, default=6,
                        help="rate-limit mode only: number of rapid uploads in the burst.")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent customer count.")
    parser.add_argument("--duration", type=int, default=10,
                        help="Soak mode only: minutes to run.")
    parser.add_argument("--sleep-ms", type=int, default=0,
                        help="Sleep between calls in single-threaded modes (kindness to backend).")
    parser.add_argument("--rebuild-corpus", action="store_true",
                        help="Force regeneration of fixture corpus.")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Overwrite baseline.json with this run's summary.")
    parser.add_argument("--no-regression-check", action="store_true",
                        help="Skip baseline comparison (still emits raw + summary).")
    parser.add_argument("--max-error-rate", type=float, default=0.01,
                        help="Overall error-rate threshold (default 1%%).")
    args = parser.parse_args(argv)

    # Safety: make it really hard to fat-finger production.
    base_url = _resolve_base_url(args.target, args.base_url)
    if "apis.hyperbots.com" in base_url and not os.environ.get("ALLOW_PROD_SIM"):
        raise SystemExit(
            "Refusing to run customer simulator against production "
            "(apis.hyperbots.com). Set ALLOW_PROD_SIM=1 to override — but you "
            "almost certainly should not.",
        )
    api_key = _require_api_key()

    if args.rebuild_corpus:
        os.environ["HYPERAPI_SIM_REBUILD"] = "1"

    print(f"Loading corpus from {Path('tests/customer_sim/_corpus_cache')}…")
    corpus = load_corpus()
    print(f"  loaded {len(corpus)} fixtures")

    run_id = new_run_id()
    run_dir = REPORTS_ROOT / run_id
    print(f"Run id: {run_id}")
    print(f"Target: {args.target}  base_url={base_url}")
    print(f"Mode:   {args.mode}  workers={args.workers}")
    print(f"Reports: {run_dir}")

    writer = MetricsWriter(run_dir, run_id, args.target, base_url)

    def _ctx(worker_id: int) -> ScenarioContext:
        return ScenarioContext(
            client=HyperAPIClient(api_key=api_key, base_url=base_url),
            writer=writer, run_id=run_id, target=args.target,
            base_url=base_url, worker_id=worker_id,
        )

    scenarios = _build_scenarios(args.mode, corpus, args.duration, args.burst_size)

    try:
        if args.workers > 1 and args.mode not in ("soak", "rate-limit"):
            _run_concurrent(_ctx, scenarios, args.workers, _print_progress)
        elif args.mode == "soak":
            _run_soak(_ctx, corpus, args.duration, args.workers, args.sleep_ms, _print_progress)
        elif args.mode == "rate-limit":
            # Burst MUST be contiguous to actually trigger Kong's per-minute window;
            # ignore --sleep-ms and --workers in this mode.
            _run_single_threaded(_ctx, scenarios, _print_progress, sleep_ms=0)
        else:
            _run_single_threaded(_ctx, scenarios, _print_progress, sleep_ms=args.sleep_ms)
    finally:
        writer.close()

    summary = writer.summarize()
    (run_dir / "summary.json").write_text(__import__("json").dumps(summary, indent=2))
    (run_dir / "summary.md").write_text(render_summary_md(summary))

    print("\n" + render_summary_md(summary))

    # Regression check
    findings: list = []
    if not args.no_regression_check and args.mode not in ("soak", "rate-limit"):
        baseline = load_baseline(BASELINE_PATH)
        findings = detect_regressions(summary, baseline,
                                      max_error_rate=args.max_error_rate)
        (run_dir / "findings.json").write_text(
            __import__("json").dumps([f.to_dict() for f in findings], indent=2),
        )
        (run_dir / "findings.md").write_text(render_findings_md(findings))
        print(render_findings_md(findings))

    if args.update_baseline:
        write_baseline(BASELINE_PATH, summary)
        print(f"Updated baseline at {BASELINE_PATH}")

    fail_count = sum(1 for f in findings if f.severity == "fail")
    return 1 if fail_count > 0 else 0


def _build_scenarios(mode: str, corpus: list[Fixture], duration_min: int, burst_size: int) -> list:
    if mode == "smoke":
        return smoke_scenarios(corpus)
    if mode == "full":
        return full_scenarios(corpus)
    if mode == "error-paths":
        return error_path_scenarios(corpus)
    if mode == "rate-limit":
        return rate_limit_burst_scenarios(corpus, burst_size=burst_size)
    if mode == "soak":
        return []  # soak builds iterations on-the-fly
    raise SystemExit(f"Unknown mode: {mode}")


def _run_soak(ctx_factory, corpus, duration_min: int, workers: int,
              sleep_ms: int, on_progress) -> None:
    deadline = time.time() + duration_min * 60
    iteration = 0
    while time.time() < deadline:
        iteration += 1
        scenarios = soak_iteration(corpus)
        sys.stdout.write(f"\n--- soak iteration {iteration} "
                         f"(remaining {(deadline - time.time()) / 60:.1f} min) ---\n")
        if workers > 1:
            _run_concurrent(ctx_factory, scenarios, workers, on_progress)
        else:
            _run_single_threaded(ctx_factory, scenarios, on_progress, sleep_ms=sleep_ms)
        if sleep_ms:
            time.sleep(sleep_ms / 1000)


if __name__ == "__main__":
    raise SystemExit(main())
