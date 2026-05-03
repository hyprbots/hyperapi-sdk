"""
Unit tests for the customer-sim helpers that don't need a live backend.

Keeps metrics/regression logic honest under refactors.
"""

from tests.customer_sim.metrics import (
    CallRecord,
    percentile,
    render_markdown as render_summary_md,
    summarize,
)
from tests.customer_sim.regression import (
    HARD_P95_CEILINGS_MS,
    RegressionFinding,
    detect_regressions,
    render_markdown as render_findings_md,
)


def _rec(op="parse", bucket="small", latency=100.0, success=True, status=200,
         expected=None, found=None):
    expected = expected or []
    found = found or []
    return CallRecord(
        run_id="r", started_at="t0", ended_at="t1", op=op, target="local",
        base_url="http://test", ocr_engine="paddle", use_presigned=True,
        doc_id=f"d_{op}_{bucket}", doc_shape="invoice", doc_mime="application/pdf",
        doc_size_bytes=10_000, doc_size_bucket=bucket, doc_page_count=1,
        latency_ms=latency, success=success, http_status=status,
        error_type=(None if success else "ParseError"),
        error_message=(None if success else "boom"), request_id="x",
        response_size_bytes=1024,
        keywords_expected=expected, keywords_found=found,
        keyword_recall=(len(found) / len(expected)) if expected else None,
    )


def test_percentile_basic():
    assert percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.5) == 55.0
    assert percentile([5], 0.95) == 5.0
    assert percentile([], 0.5) is None


def test_summarize_groups_by_op_and_bucket():
    records = [
        _rec(op="parse", bucket="small", latency=100),
        _rec(op="parse", bucket="small", latency=300),
        _rec(op="parse", bucket="large", latency=2000),
        _rec(op="extract", bucket="small", latency=4000),
        _rec(op="parse", bucket="small", latency=500, success=False, status=500),
    ]
    s = summarize(records, run_id="r", target="local", base_url="http://test")
    assert s["total_calls"] == 5
    assert s["error_calls"] == 1
    assert s["error_rate"] == 0.2

    parse_small = next(g for g in s["groups"] if g["op"] == "parse" and g["size_bucket"] == "small")
    assert parse_small["count"] == 3
    assert parse_small["error_count"] == 1
    # Latency p95 only over successful calls
    assert parse_small["latency_ms"]["p50"] == 200.0


def test_summarize_renders_markdown_without_crashing():
    records = [_rec(latency=120), _rec(latency=480), _rec(latency=900)]
    s = summarize(records, run_id="abc", target="local", base_url="http://test")
    md = render_summary_md(s)
    assert "# Customer Simulator Run" in md
    assert "p50=" in md
    assert "| parse | small |" in md


def test_regression_flags_hard_p95_ceiling():
    summary = {
        "total_calls": 10, "success_calls": 10, "error_calls": 0, "error_rate": 0.0,
        "groups": [{
            "op": "parse", "size_bucket": "small", "count": 10, "success_count": 10,
            "error_count": 0, "error_rate": 0.0,
            "latency_ms": {"p50": 1000, "p95": HARD_P95_CEILINGS_MS["parse"] + 5000,
                            "p99": 50_000, "max": 60_000, "mean": 5_000},
            "ocr_keyword_recall_mean": None,
        }],
    }
    findings = detect_regressions(summary, baseline=None)
    assert any(f.metric == "p95_hard_ceiling" and f.severity == "fail" for f in findings)


def test_regression_flags_soft_p95_vs_baseline():
    baseline = {
        "groups": [{
            "op": "parse", "size_bucket": "small",
            "latency_ms": {"p50": 100, "p95": 200, "p99": 300, "max": 400, "mean": 150},
            "ocr_keyword_recall_mean": 0.95, "count": 10, "error_rate": 0.0,
        }],
    }
    summary = {
        "total_calls": 10, "success_calls": 10, "error_calls": 0, "error_rate": 0.0,
        "groups": [{
            "op": "parse", "size_bucket": "small", "count": 10,
            "success_count": 10, "error_count": 0, "error_rate": 0.0,
            "latency_ms": {"p50": 200, "p95": 500, "p99": 800, "max": 900, "mean": 300},
            "ocr_keyword_recall_mean": 0.95,
        }],
    }
    findings = detect_regressions(summary, baseline=baseline)
    assert any(f.metric == "p95_vs_baseline" and f.severity == "fail" for f in findings)


def test_regression_flags_recall_drop():
    baseline = {"groups": [{
        "op": "parse", "size_bucket": "small", "count": 10, "error_rate": 0.0,
        "latency_ms": {"p50": 100, "p95": 200, "p99": 300, "max": 400, "mean": 150},
        "ocr_keyword_recall_mean": 0.95,
    }]}
    summary = {
        "total_calls": 10, "success_calls": 10, "error_calls": 0, "error_rate": 0.0,
        "groups": [{
            "op": "parse", "size_bucket": "small", "count": 10, "success_count": 10,
            "error_count": 0, "error_rate": 0.0,
            "latency_ms": {"p50": 100, "p95": 200, "p99": 300, "max": 400, "mean": 150},
            "ocr_keyword_recall_mean": 0.70,  # 25pp drop
        }],
    }
    findings = detect_regressions(summary, baseline=baseline)
    assert any(f.metric == "ocr_recall" and f.severity == "fail" for f in findings)


def test_regression_renders_findings_markdown():
    findings = [
        RegressionFinding(severity="fail", op="parse", bucket="small",
                          metric="p95_hard_ceiling", observed=40_000, baseline=30_000,
                          message="parse/small p95 too high"),
        RegressionFinding(severity="warn", op="extract", bucket="medium",
                          metric="group_error_rate", observed=0.05, baseline=0.01,
                          message="extract/medium errors elevated"),
    ]
    md = render_findings_md(findings)
    assert "Failures" in md
    assert "Warnings" in md
    assert "parse/small p95 too high" in md


def test_no_regressions_returns_clean_markdown():
    md = render_findings_md([])
    assert "No regressions" in md


# ── Rate-limit burst scenario ───────────────────────────────────────────────

def test_rate_limit_burst_picks_smallest_pdf_and_repeats():
    from tests.customer_sim.corpus import Fixture
    from tests.customer_sim.scenarios import rate_limit_burst_scenarios, upload_one

    corpus = [
        Fixture(doc_id="a", path="/tmp/a.pdf", mime="application/pdf",
                size_bytes=100_000, size_bucket="small", page_count=1, shape="invoice"),
        Fixture(doc_id="b", path="/tmp/b.pdf", mime="application/pdf",
                size_bytes=10_000, size_bucket="tiny", page_count=1, shape="invoice"),
        Fixture(doc_id="c", path="/tmp/c.png", mime="image/png",
                size_bytes=5_000, size_bucket="tiny", page_count=1, shape="receipt"),
        Fixture(doc_id="edge", path="/tmp/e.pdf", mime="application/pdf",
                size_bytes=0, size_bucket="tiny", page_count=0, shape="edge_case",
                is_edge_case=True, edge_case_kind="zero_byte"),
    ]
    scenarios = rate_limit_burst_scenarios(corpus, burst_size=5)

    assert len(scenarios) == 5, "burst_size respected"
    fns = {fn for fn, _, _ in scenarios}
    assert fns == {upload_one}, "burst hits upload only"
    fixtures = {fx.doc_id for _, fx, _ in scenarios}
    assert fixtures == {"b"}, "smallest non-edge-case PDF chosen, ignoring image and edge case"
