"""Contract: parse() — submit (X-Async:true) + poll until completed.

Covers:
  - Default convenience method submits with X-Async: true and polls under the hood
  - Per-call use_presigned override propagates to the submit POST
  - submit_parse() returns a Job and does NOT poll
  - Error mapping on submit (401 → AuthenticationError, 5xx → ParseError)
"""

import httpx
import pytest

from hyperapi import AuthenticationError, Job, ParseError


def _seed_presigned(mock_api, *, key="doc_parse"):
    mock_api.post("/v1/documents/upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "document_key": key,
                "upload_url": f"https://s3.local/{key}?sig=x",
                "expires_in": 600,
            },
        ),
    )
    mock_api.put(f"https://s3.local/{key}?sig=x").mock(
        return_value=httpx.Response(200)
    )


def _seed_submit(mock_api, *, job_id="job_parse_1"):
    return mock_api.post("/v1/parse").mock(
        return_value=httpx.Response(
            202,
            json={
                "job_id": job_id,
                "status": "pending",
                "poll_url": f"/v1/jobs/{job_id}",
            },
        ),
    )


def _seed_completed(mock_api, *, job_id="job_parse_1", ocr="Hello"):
    return mock_api.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {"ocr": ocr, "pages": 1},
                "request_id": "req-x",
                "duration_ms": 1234,
            },
        ),
    )


def test_parse_submits_with_x_async_then_polls(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    poll_route = _seed_completed(mock_api, ocr="Invoice #12345")

    result = client.parse(tiny_pdf)

    assert result["ocr"] == "Invoice #12345"
    # Submit happened with X-Async: true
    submit_req = submit_route.calls[0].request
    assert submit_req.headers["X-Async"] == "true"
    assert "ocr_engine" not in submit_req.url.params
    assert b"document_key=doc_parse" in submit_req.read()
    # Poll happened
    assert poll_route.called


def _seed_completed_with_boxes(mock_api, *, job_id="job_parse_1"):
    boxes = [
        {"text": "Invoice #4471", "bbox": [120, 340, 410, 372], "confidence": 0.987}
    ]
    return mock_api.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {
                    "ocr": "Invoice #4471",
                    "pages": [
                        {"page_number": 1, "text": "Invoice #4471", "boxes": boxes}
                    ],
                },
                "request_id": "req-x",
                "duration_ms": 1234,
            },
        ),
    )


def _seed_completed_with_image(mock_api, *, job_id="job_parse_1"):
    image_url = "https://s3.example.com/deskewed/org/hash/0.webp?sig=xxx"
    boxes = [
        {"text": "Invoice #4471", "bbox": [120, 340, 410, 372], "confidence": 0.987}
    ]
    return mock_api.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {
                    "ocr": "Invoice #4471",
                    "pages": [
                        {
                            "page_number": 1,
                            "text": "Invoice #4471",
                            "image_url": image_url,
                            "dimensions": {"width": 824, "height": 1066},
                            "boxes": boxes,
                        }
                    ],
                },
                "request_id": "req-x",
                "duration_ms": 1234,
            },
        ),
    )


def test_parse_default_omits_output_flags(mock_api, client, tiny_pdf):
    """The API treats an omitted include_* flag as ON (fast mode) and a literal
    `false` as an opt-out. The SDK must therefore send nothing unless the caller
    set a flag — sending `false` by default silently stripped boxes, word boxes,
    layout and page images from every result."""
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf)

    params = submit_route.calls[0].request.url.params
    for flag in ("include_boxes", "include_image", "include_layout",
                 "parse_figures", "groups_profile", "mode"):
        assert flag not in params, flag


@pytest.mark.parametrize("flag", ["include_boxes", "include_image", "include_layout"])
def test_parse_explicit_false_is_sent_as_opt_out(mock_api, client, tiny_pdf, flag):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf, **{flag: False})

    params = submit_route.calls[0].request.url.params
    assert params[flag] == "false"
    # Only the flag that was set reaches the wire.
    others = {"include_boxes", "include_image", "include_layout"} - {flag}
    for other in others:
        assert other not in params, other


def test_parse_include_layout_true_is_sent(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf, include_layout=True)

    assert submit_route.calls[0].request.url.params["include_layout"] == "true"


def test_parse_figures_and_groups_profile_propagate(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf, parse_figures="table,chart", groups_profile="B")

    params = submit_route.calls[0].request.url.params
    assert params["parse_figures"] == "table,chart"
    assert params["groups_profile"] == "B"


def test_submit_parse_figures_none_opts_out(mock_api, client, tiny_pdf):
    """`"none"` is the API's documented opt-out literal and must pass through verbatim."""
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)

    client.submit_parse(tiny_pdf, parse_figures="none")

    assert submit_route.calls[0].request.url.params["parse_figures"] == "none"


def test_parse_include_image_propagates_and_returns_image_url(
    mock_api, client, tiny_pdf
):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed_with_image(mock_api)

    result = client.parse(tiny_pdf, include_image=True)

    assert submit_route.calls[0].request.url.params["include_image"] == "true"
    page = result["pages"][0]
    assert "s3.example.com" in page["image_url"]
    assert page["dimensions"] == {"width": 824, "height": 1066}


def test_submit_parse_include_image_propagates(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)

    client.submit_parse(tiny_pdf, include_image=True)

    assert submit_route.calls[0].request.url.params["include_image"] == "true"


def test_parse_include_boxes_propagates_and_returns_boxes(
    mock_api, client, tiny_pdf
):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed_with_boxes(mock_api)

    result = client.parse(tiny_pdf, include_boxes=True)

    # Flag reached the submit POST as a query param.
    assert submit_route.calls[0].request.url.params["include_boxes"] == "true"
    # Boxes flow through the dict-passthrough result untouched.
    box = result["pages"][0]["boxes"][0]
    assert box["text"] == "Invoice #4471"
    assert box["bbox"] == [120, 340, 410, 372]
    assert box["confidence"] == 0.987


def test_submit_parse_include_boxes_propagates(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)

    client.submit_parse(tiny_pdf, include_boxes=True)

    assert submit_route.calls[0].request.url.params["include_boxes"] == "true"


def test_parse_image_path_alias_still_works(mock_api, client, tiny_png):
    _seed_presigned(mock_api)
    _seed_submit(mock_api)
    _seed_completed(mock_api, ocr="image text")

    result = client.parse(image_path=tiny_png)

    assert result["ocr"] == "image text"


def test_parse_use_presigned_false_falls_back_to_presigned(
    mock_api, client, tiny_pdf
):
    """use_presigned=False is no longer supported by the API (no direct-upload
    path); the SDK warns and falls back to the presigned document_key flow."""
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    with pytest.warns(DeprecationWarning):
        client.parse(tiny_pdf, use_presigned=False)

    req = submit_route.calls[0].request
    # No multipart upload — the submit carries the presigned document_key instead.
    assert "multipart/form-data" not in req.headers.get("content-type", "")
    assert b"document_key=doc_parse" in req.read()
    assert req.headers["X-Async"] == "true"


def test_parse_submit_401_raises_authentication_error(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    mock_api.post("/v1/parse").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 401


def test_parse_submit_500_raises_parse_error(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    mock_api.post("/v1/parse").mock(
        return_value=httpx.Response(500, json={"message": "service down"}),
    )

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert ei.value.status_code == 500


def test_parse_failed_job_raises_parse_error(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    _seed_submit(mock_api)
    mock_api.get("/v1/jobs/job_parse_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "OCR pipeline failed",
                "status_code": 500,
                "request_id": "req-x",
            },
        ),
    )

    with pytest.raises(ParseError) as ei:
        client.parse(tiny_pdf)
    assert "OCR pipeline failed" in str(ei.value)


def test_submit_parse_returns_job_no_polling(mock_api, client, tiny_pdf):
    """submit_parse should return immediately with a Job; never call /v1/jobs/."""
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api, job_id="my_job_id")
    poll_route = mock_api.get("/v1/jobs/my_job_id")

    job = client.submit_parse(tiny_pdf)

    assert isinstance(job, Job)
    assert job.job_id == "my_job_id"
    assert job.op == "parse"
    assert job.status == "pending"
    assert submit_route.called
    assert not poll_route.called  # NO polling happened


def test_parse_per_call_poll_overrides_propagate(mock_api, client, tiny_pdf):
    """Per-call poll_timeout should override the constructor's value."""
    _seed_presigned(mock_api)
    _seed_submit(mock_api)
    mock_api.get("/v1/jobs/job_parse_1").mock(
        return_value=httpx.Response(200, json={"status": "pending"}),
    )

    # Use a tiny per-call timeout to force fast JobTimeoutError
    from hyperapi import JobTimeoutError

    with pytest.raises(JobTimeoutError):
        client.parse(tiny_pdf, poll_timeout=0.05, poll_interval=0.01)


def test_parse_each_call_has_unique_request_id(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf)
    client.parse(tiny_pdf)

    ids = {c.request.headers["X-Request-ID"] for c in submit_route.calls}
    assert len(ids) == 2  # two distinct UUIDs


# ── mode (fast | advanced) ───────────────────────────────────────────────


def _seed_completed_with_structured(mock_api, *, job_id="job_parse_1"):
    structured = {
        "html": "<table><tr><td>Invoice #4471</td></tr></table>",
        "markdown": "| Invoice #4471 |",
        "regions": [{"type": "table", "bbox": [10, 10, 600, 200]}],
    }
    return mock_api.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {
                    "ocr": "Invoice #4471",
                    "pages": [
                        {
                            "page_number": 1,
                            "text": "Invoice #4471",
                            "structured": structured,
                        }
                    ],
                },
                "request_id": "req-x",
                "duration_ms": 9876,
            },
        ),
    )


def test_parse_default_omits_mode_for_server_default(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf)

    assert "mode" not in submit_route.calls[0].request.url.params


def test_parse_explicit_fast_is_sent(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed(mock_api)

    client.parse(tiny_pdf, mode="fast")

    assert submit_route.calls[0].request.url.params["mode"] == "fast"


def test_parse_advanced_mode_propagates_and_returns_structured(
    mock_api, client, tiny_pdf
):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)
    _seed_completed_with_structured(mock_api)

    result = client.parse(tiny_pdf, mode="advanced")

    # Flag reached the submit POST as a query param.
    assert submit_route.calls[0].request.url.params["mode"] == "advanced"
    # Structured layout flows through the dict-passthrough result untouched.
    structured = result["pages"][0]["structured"]
    assert "<table>" in structured["html"]
    assert structured["regions"][0]["type"] == "table"


def test_submit_parse_mode_advanced_propagates(mock_api, client, tiny_pdf):
    _seed_presigned(mock_api)
    submit_route = _seed_submit(mock_api)

    client.submit_parse(tiny_pdf, mode="advanced")

    assert submit_route.calls[0].request.url.params["mode"] == "advanced"
