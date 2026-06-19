# Customer Simulator (L2)

The simulator drives the SDK against a real backend like a real customer would: real authentication, real network, real documents of varied types and sizes. It writes per-call records and aggregated metrics so regressions become visible *before* a paying customer files a ticket.

This sits on top of the L1 mocked-contract suite (under `tests/test_contract_*.py`) — L1 catches code-shape bugs in <1 minute on every PR; L2 catches real-world drift nightly.

## What it covers

- **Every SDK operation:** `parse`, `extract`, `classify`, `split`, `process`, `upload_document`
- **Both upload paths:** presigned-S3 (default) and `multipart/form-data` legacy fallback
- **Format coverage:** PDF (1–30 pages), PNG, JPEG, TIFF, WEBP
- **Size buckets:** tiny (<100 KB) → small → medium → large → xl (~25 MB)
- **Realistic shapes:** invoices, contracts, receipts (incl. rotated and blurred scans)
- **Edge cases:** zero-byte file, malformed PDF, unsupported MIME
- **Rate-limiting:** explicit burst mode (`--mode rate-limit`) that validates Kong's per-tier 429 envelope and `Retry-After` header
- **Concurrency:** single customer, or N parallel customers (`--workers`)
- **Soak:** continuous loops over a duration window (`--mode soak --duration 30`)

## Metrics captured per call

`tests/customer_sim/reports/{run_id}/raw.jsonl` contains one record per SDK call with:

```
run_id, started_at, ended_at, op, target, base_url,
use_presigned,
doc_id, doc_shape, doc_mime, doc_size_bytes, doc_size_bucket, doc_page_count,
latency_ms, success, http_status, error_type, error_message,
request_id, response_size_bytes,
keywords_expected, keywords_found, keyword_recall, worker_id
```

`summary.json` rolls these up into per-(op, size_bucket) percentiles (p50/p95/p99/max), error rates by type/status, and overall OCR keyword recall (a coarse but cheap accuracy proxy: every fixture carries a list of canonical strings that the OCR output must contain).

## Running locally

You need a real backend reachable at `HYPERAPI_URL` and a real test API key.

```bash
pip install -e ".[dev,sim]"

# Smoke (~1 min) against a local docker-compose backend
HYPERAPI_KEY=hk_test_xxx HYPERAPI_URL=http://localhost:8000 \
  python -m tests.customer_sim --target local --mode smoke

# Full sweep, 4 concurrent customers, against preproduction
HYPERAPI_KEY=$HYPERAPI_KEY_PREPROD \
  python -m tests.customer_sim --target preprod --mode full --workers 4

# 30-minute soak (catches slow degradation that single runs miss)
HYPERAPI_KEY=hk_test_xxx HYPERAPI_URL=http://localhost:8000 \
  python -m tests.customer_sim --target local --mode soak --duration 30 --workers 2

# Error-path scenarios (zero-byte file, malformed PDF, etc.)
python -m tests.customer_sim --target local --mode error-paths

# Rate-limit burst — fires 6 rapid uploads with no sleep between them; on a
# free-tier key (limit:1/60s) you should see 1 OK + 5 × 429. Use this to verify
# Kong's hyperapi-auth rate-limit plugin is enforcing per-tier windows
# correctly. Bump --burst-size for paid tiers (pro=60/min, enterprise=1000/min).
HYPERAPI_KEY=hk_test_xxx HYPERAPI_URL=http://localhost:8000 \
  python -m tests.customer_sim --target local --mode rate-limit --burst-size 6
```

The first run builds the deterministic fixture corpus (~80 MB on disk) under `tests/customer_sim/_corpus_cache/` and caches it. Subsequent runs reuse the cache; pass `--rebuild-corpus` or set `HYPERAPI_SIM_REBUILD=1` to regenerate.

## Safety

- The runner refuses to hit `apis.hyperbots.com` (production). To override (you should never need to), set `ALLOW_PROD_SIM=1`.
- The QA agent's design decision (`qa-agent-design-decisions.md`) is explicit: automated QA runs against preproduction only.

## Regression detection

After each non-soak run, the simulator compares the run's `summary.json` against `baseline.json`. Three checks:

1. **Hard p95 ceiling** per operation (e.g., `parse` p95 must stay under 30 s, `extract` under 180 s). These are absolute SLO ceilings.
2. **Soft p95 vs baseline** — fail if any (op, bucket) p95 is more than 1.5× the baseline.
3. **OCR keyword recall** — fail if recall drops more than 10 percentage points vs baseline.

Findings land in `findings.json` and `findings.md`. The runner exits non-zero if any `severity=fail` finding is present, which in turn drives the GitHub Actions `sdk-drift` issue (idempotent: comments on the existing open issue rather than spamming new ones).

To accept a new performance regime as the baseline (e.g., after a deliberate backend speed-up):

```bash
python -m tests.customer_sim --target preprod --mode full --update-baseline
```

…and commit the resulting `tests/customer_sim/baseline.json`. (Recommended only after eyeballing the run.)

## CI

`.github/workflows/customer-sim.yml`:

- Schedule: nightly at 02:00 UTC against preproduction
- Manual: workflow_dispatch with mode/workers/duration/update_baseline inputs
- Secrets: `HYPERAPI_KEY_PREPROD` (set in repo settings)
- On failure: opens or comments on a GitHub issue tagged `sdk-drift`
- Always: uploads the report directory as a 30-day artifact

## Adding a new fixture

Edit `tests/customer_sim/corpus.py` — append a spec to `pdf_specs` or `image_specs`, run `python -m tests.customer_sim.corpus` to rebuild. Each fixture must include `expected_keywords` so the simulator can score OCR recall against it.
