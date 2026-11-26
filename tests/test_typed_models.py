"""Typed results (0.8): production-shaped replies become the right models.

The bodies below follow openapi/hyperapi-public.yaml (the shapes the API
sends), not the older flattened fixtures.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import pickle

import httpx
import pytest

from hyperapi import AsyncHyperAPIClient, HyperAPIClient
from hyperapi.types import (
    BatchCreateResponse,
    BatchListResponse,
    BatchStatusResponse,
    ClassifyResponse,
    ExtractEntitiesResult,
    ExtractResponse,
    ExtractTemplateResult,
    GenericResponse,
    HyperAPIModel,
    JobEnvelope,
    ParseResponse,
    ParseResult,
    ProcessResult,
    RecentJob,
    RedactResponse,
    SplitNotRequired,
    SplitResponse,
    SplitResult,
    UploadLimits,
)
from hyperapi.types import _coerce

KEY = "hk_test_unit"
BASE = "http://api.test"

META = {"pages": 1}


def _envelope(task, result, **extra):
    body = {
        "status": "success",
        "request_id": "req-1",
        "task": task,
        "model_used": "standard",
        "cached": False,
        "result": result,
        "duration_ms": 812,
        "metadata": dict(META),
    }
    body.update(extra)
    return body


PARSE = _envelope(
    "parse",
    {
        "ocr": "Invoice 1042\nTotal 128.50",
        "pages": [
            {
                "page_number": 1,
                "text": "Invoice 1042\nTotal 128.50",
                "image_url": "https://s3.local/p1.png",
                "future_page_field": {"x": 1},
            }
        ],
        "blocks": [{"type": "text", "text": "Invoice 1042"}],
    },
)
EXTRACT = _envelope(
    "extract",
    {"entities": {"invoice_number": "1042"}, "line_items": [], "summary": {"total": 128.5}},
    ocr_text="Invoice 1042",
)
EXTRACT_TEMPLATE = _envelope(
    "extract",
    {"data": {"invoice_number": "1042"}, "summary": {}},
    ocr_text="Invoice 1042",
)
CLASSIFY = _envelope(
    "classify",
    {
        "document_type": "invoice",
        "initial_label": "invoice",
        "early_exit": False,
        "early_exit_reason": "",
        "domain": "financial",
        "status": None,
        "raw_label": "invoice",
        "other_hint": None,
        "subject_hint": None,
        "confidence": 0.97,
        "needs_review": False,
        "rejection": None,
        "warnings": [{"code": "W1", "field": None, "message": "note"}],
    },
    ocr_text="Invoice 1042",
)
SPLIT = _envelope(
    "split",
    {
        "segmentation": {
            "splits": [
                {
                    "split_label": "invoice",
                    "start_page": 1,
                    "end_page": 2,
                    "confidence": 0.9,
                    "identifying_features": "header",
                    "sub_documents": [],
                }
            ]
        },
        "metadata": {},
    },
    ocr_text="...",
)
SPLIT_SINGLE = _envelope("split", {"not_required": True, "reason": "single page"}, ocr_text="x")
REDACT = _envelope(
    "redact",
    {
        "mode": "redact",
        "original_text_block": "Call 555-0100",
        "updated_text_block": "Call [PHONE]",
        "updated_dictionary": {"[PHONE]": "555-0100"},
        "summary": {"PHONE": 1},
    },
)


def _job(job_id, result, status="completed"):
    return {
        "job_id": job_id,
        "status": status,
        "request_id": "req-1",
        "created_at": 1700000000.5,
        "completed_at": 1700000002.0,
        "filename": "inv.pdf",
        "endpoint": "/v1/parse",
        "document_key": "doc_1",
        "source": "api",
        "is_test": True,
        "result": result,
        "error": None,
        "status_code": None,
        "retry_after_seconds": None,
    }


async def _call(fn, *args, **kwargs):
    out = fn(*args, **kwargs)
    return await out if inspect.isawaitable(out) else out


def _client(cls):
    return cls(api_key=KEY, base_url=BASE, poll_interval=0.01)


def _seed(router, op, body, job_id="job-1"):
    router.post(f"{BASE}/v1/documents/upload").mock(
        return_value=httpx.Response(
            200, json={"document_key": "doc_1", "upload_url": "http://s3.local/put", "expires_in": 600}
        )
    )
    router.put("http://s3.local/put").mock(return_value=httpx.Response(200))
    router.post(f"{BASE}/v1/{op}").mock(
        return_value=httpx.Response(
            200, json={"job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}"}
        )
    )
    router.get(f"{BASE}/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=_job(job_id, body))
    )


CASES = [
    ("parse", "parse", PARSE, ParseResponse, ParseResult),
    ("extract", "extract", EXTRACT, ExtractResponse, ExtractEntitiesResult),
    ("extract", "extract", EXTRACT_TEMPLATE, ExtractResponse, ExtractTemplateResult),
    ("classify", "classify", CLASSIFY, ClassifyResponse, None),
    ("split", "split", SPLIT, SplitResponse, SplitResult),
    ("split", "split", SPLIT_SINGLE, SplitResponse, SplitNotRequired),
    ("redact", "redact", REDACT, RedactResponse, None),
]


@pytest.fixture(autouse=True)
def _reset_warned():
    _coerce._warned.clear()


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
@pytest.mark.parametrize("method,op,body,model,inner", CASES)
async def test_each_operation_returns_its_model(respx_mock, tiny_pdf, cls, method, op, body, model, inner):
    _seed(respx_mock, op, body)
    c = _client(cls)
    result = await _call(getattr(c, method), tiny_pdf)
    assert type(result) is model
    if inner is not None:
        assert type(result.result) is inner
    # Same data, both ways of reading it, and an exact round trip.
    assert result.request_id == result["request_id"] == "req-1"
    assert result == body
    assert result.to_dict() == body
    assert json.loads(result.to_json()) == body


async def test_typed_attribute_access(respx_mock, tiny_pdf):
    _seed(respx_mock, "parse", PARSE)
    r = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01).parse(tiny_pdf)
    page = r.result.pages[0]
    assert page.page_number == 1 and page.text.startswith("Invoice")
    assert r.metadata.pages == 1
    # Unknown fields survive on the model and in to_dict().
    assert page["future_page_field"] == {"x": 1}
    assert r.to_dict()["result"]["pages"][0]["future_page_field"] == {"x": 1}
    # 0.7-style reads keep working.
    assert r["result"]["ocr"] == PARSE["result"]["ocr"]
    assert r.get("missing", "d") == "d" and "result" in r and "nope" not in r


def test_get_job_picks_the_result_model_by_task(respx_mock):
    for i, (body, model) in enumerate(
        [(PARSE, ParseResponse), (EXTRACT, ExtractResponse), (CLASSIFY, ClassifyResponse),
         (SPLIT, SplitResponse), (REDACT, RedactResponse)]
    ):
        respx_mock.get(f"{BASE}/v1/jobs/j{i}").mock(return_value=httpx.Response(200, json=_job(f"j{i}", body)))
        job = HyperAPIClient(api_key=KEY, base_url=BASE).get_job(f"j{i}")
        assert type(job) is JobEnvelope and type(job.result) is model
        assert job.status == "completed" and job.created_at == 1700000000.5


def test_pending_job_has_no_result(respx_mock):
    respx_mock.get(f"{BASE}/v1/jobs/p").mock(
        return_value=httpx.Response(200, json=_job("p", None, status="pending"))
    )
    job = HyperAPIClient(api_key=KEY, base_url=BASE).get_job("p")
    assert type(job) is JobEnvelope and job.result is None and job.status == "pending"


def test_wait_for_job_by_bare_id_uses_the_reply_task(respx_mock):
    respx_mock.get(f"{BASE}/v1/jobs/b").mock(return_value=httpx.Response(200, json=_job("b", CLASSIFY)))
    r = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01).wait_for_job("b")
    assert type(r) is ClassifyResponse and r.result.document_type == "invoice"


def test_mismatched_reply_falls_back_and_logs_once(respx_mock, tiny_pdf, caplog):
    odd = dict(PARSE, task="parse", duration_ms="not-a-number")
    _seed(respx_mock, "parse", odd)
    c = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01)
    with caplog.at_level(logging.WARNING, logger="hyperapi"):
        r1 = c.parse(tiny_pdf)
        r2 = c.parse(tiny_pdf)
    assert isinstance(r1, GenericResponse) and isinstance(r2, GenericResponse)
    assert r1 == odd and r1["result"]["pages"][0]["page_number"] == 1
    assert r1.result.pages[0].page_number == 1  # attribute access still works
    assert [rec.message for rec in caplog.records].count("response_model_mismatch") == 1


def test_empty_result_is_an_empty_generic(respx_mock):
    respx_mock.get(f"{BASE}/v1/jobs/e").mock(return_value=httpx.Response(200, json=_job("e", None)))
    r = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01).wait_for_job("e")
    assert isinstance(r, GenericResponse) and r == {} and len(r) == 0


def test_limits_recent_and_batch_are_typed(respx_mock):
    tl = {"max_bytes": 52428800, "max_pages": 100, "urls_max_pages": None}
    limits = {"max_upload_bytes": 52428800, "parse_fast": tl, "parse_advanced": tl, "bulk_parse": None,
              "extract_fast": tl, "extract_advanced": None, "classify": tl, "split": tl, "redact": tl}
    respx_mock.get(f"{BASE}/v1/limits").mock(return_value=httpx.Response(200, json=limits))
    recent = {"job_id": "j", "request_id": "r", "status": "cancelled", "task": "parse", "endpoint": "/v1/parse",
              "pages": 1, "filename": "a.pdf", "document_key": "d", "source": "api", "is_test": False,
              "created_at": 1.0, "completed_at": None, "error": None}
    respx_mock.get(f"{BASE}/v1/jobs/recent").mock(return_value=httpx.Response(200, json={"jobs": [recent]}))
    bid = "6f1c2c8e-0d55-4a8a-9d3c-2b7f3b0f9a11"
    counts = {"total": 1, "queued": 0, "processing": 0, "done": 1, "failed": 0}
    item = {"doc_index": 0, "status": "done", "error": None, "result_url": None, "result_expired": False,
            "filename": "a.pdf", "request_id": "r", "pages_processed": 1, "tokens_used": None}
    status = {"batch_id": bid, "status": "completed", "endpoint": "/v1/parse", "counts": counts,
              "items": [item], "created_at": "2026-11-26T00:00:00Z", "metadata": {}}
    respx_mock.post(f"{BASE}/v1/batch").mock(
        return_value=httpx.Response(200, json={"batch_id": bid, "status": "queued", "total_items": 1}))
    respx_mock.get(f"{BASE}/v1/batch/{bid}").mock(return_value=httpx.Response(200, json=status))
    entry = {"batch_id": bid, "status": "completed", "endpoint": "/v1/parse", "total_items": 1,
             "done_items": 1, "failed_items": 0, "created_at": "2026-11-26T00:00:00Z", "metadata": {}}
    respx_mock.get(f"{BASE}/v1/batch").mock(
        return_value=httpx.Response(200, json={"batches": [entry], "limit": 50, "offset": 0}))

    c = HyperAPIClient(api_key=KEY, base_url=BASE)
    lim = c.get_limits()
    assert type(lim) is UploadLimits and lim.parse_fast.max_pages == 100 and lim["bulk_parse"] is None
    [job] = c.list_recent_jobs()
    assert type(job) is RecentJob and job.status == "cancelled"
    created = c.create_batch(endpoint="/v1/parse", document_keys=["d"])
    assert type(created) is BatchCreateResponse
    assert created["batch_id"] == bid and isinstance(created.batch_id, str)  # ids stay str
    got = c.get_batch(bid)
    assert type(got) is BatchStatusResponse and got.counts.done == 1
    # The JSON key `items` reads as before; the attribute is `items_` (`.items()` stays a method).
    assert got["items"][0]["doc_index"] == 0 and got.items_[0].status == "done"
    assert dict(got.items())["endpoint"] == "/v1/parse"
    waited = c.wait_for_batch(bid, poll_interval=0.01)
    assert type(waited) is BatchStatusResponse and waited == status
    listed = c.list_batches()
    assert type(listed) is BatchListResponse and listed.batches[0].done_items == 1


def test_process_returns_process_result(respx_mock, tiny_pdf):
    respx_mock.post(f"{BASE}/v1/documents/upload").mock(return_value=httpx.Response(
        200, json={"document_key": "doc_1", "upload_url": "http://s3.local/put", "expires_in": 600}))
    respx_mock.put("http://s3.local/put").mock(return_value=httpx.Response(200))
    respx_mock.post(f"{BASE}/v1/parse").mock(return_value=httpx.Response(
        200, json={"job_id": "jp", "status": "pending", "poll_url": "/v1/jobs/jp"}))
    respx_mock.post(f"{BASE}/v1/extract").mock(return_value=httpx.Response(
        200, json={"job_id": "je", "status": "pending", "poll_url": "/v1/jobs/je"}))
    respx_mock.get(f"{BASE}/v1/jobs/jp").mock(return_value=httpx.Response(200, json=_job("jp", PARSE)))
    respx_mock.get(f"{BASE}/v1/jobs/je").mock(return_value=httpx.Response(200, json=_job("je", EXTRACT)))
    r = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01).process(tiny_pdf)
    assert type(r) is ProcessResult and type(r.data) is ExtractResponse
    assert r.ocr == PARSE["result"]["ocr"] and r["data"]["result"]["entities"]["invoice_number"] == "1042"


def test_download_pages_accepts_a_model(respx_mock, tiny_pdf, tmp_path):
    _seed(respx_mock, "parse", PARSE)
    respx_mock.get("https://s3.local/p1.png").mock(return_value=httpx.Response(200, content=b"\x89PNGdata"))
    c = HyperAPIClient(api_key=KEY, base_url=BASE, poll_interval=0.01)
    [path] = c.download_pages(c.parse(tiny_pdf), tmp_path)
    assert path.read_bytes() == b"\x89PNGdata"


def test_models_pickle_copy_and_compare():
    r = ParseResponse.model_validate(PARSE)
    assert pickle.loads(pickle.dumps(r)) == PARSE
    assert copy.deepcopy(r) == r
    assert r != {"other": 1}
    assert isinstance(r, HyperAPIModel) and not isinstance(r, dict)
