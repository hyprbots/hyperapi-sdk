"""Contract: edit_detect() / edit_fill() / edit() — the two-call form-filling flow.

Detect is a normal presigned-upload submit (document_key in the form body,
markdown_assist as a query param). Fill is the odd one out across the whole
SDK: a JSON POST that uploads nothing and references the detect job by id, so
the schema and page images never round-trip through the client.
"""

import json

import httpx
import pytest

from hyperapi import EditError, Job


def _seed_presigned(mock_backend, *, key="doc_ed"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_detect_submit(mock_backend, *, job_id="job_ed_detect"):
    return mock_backend.post("/v1/edit/detect").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _seed_detect_completed(mock_backend, *, job_id="job_ed_detect"):
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "status": "success",
                "task": "edit",
                "result": {
                    "phase": "detect",
                    "page_count": 1,
                    "pages": [{
                        "page": 1,
                        "image_url": "https://s3.local/page1.png?sig=y",
                        "size": {"w": 1195, "h": 1536},
                    }],
                    "form_schema": [
                        {
                            "field_name": "Patient:Name",
                            "description": "Full legal name",
                            "type": "text",
                            "bbox": {"left": 0.1, "top": 0.2, "width": 0.3,
                                     "height": 0.02, "page": 1},
                        },
                    ],
                },
                "request_id": "req-ed",
            },
            "duration_ms": 8000,
        }),
    )


def _seed_fill_submit(mock_backend, *, job_id="job_ed_fill"):
    return mock_backend.post("/v1/edit/fill").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _seed_fill_completed(mock_backend, *, job_id="job_ed_fill"):
    return mock_backend.get(f"/v1/jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "result": {
                "status": "success",
                "task": "edit",
                "result": {
                    "phase": "fill",
                    "pages": [{
                        "page": 1,
                        "image_url": "https://s3.local/filled1.png?sig=z",
                        "size": {"w": 1195, "h": 1536},
                    }],
                    "fills": [{"index": 0, "value": "Jane Doe"}],
                },
                "request_id": "req-ed-fill",
            },
            "duration_ms": 2000,
        }),
    )


# ── detect ──────────────────────────────────────────────────────────────────


def test_detect_uses_async_and_markdown_assist_query_param(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_detect_submit(mock_backend)
    poll_route = _seed_detect_completed(mock_backend)

    result = client.edit_detect(tiny_pdf)

    assert result["result"]["form_schema"][0]["field_name"] == "Patient:Name"
    req = submit_route.calls[0].request
    # X-Async is mandatory server-side — a missing header is a hard 400.
    assert req.headers["X-Async"] == "true"
    assert req.url.params["markdown_assist"] == "false"
    # document_key rides in the form body, as with every other upload op.
    assert "document_key" in req.content.decode()
    assert poll_route.called


def test_detect_markdown_assist_true(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)

    client.edit_detect(tiny_pdf, markdown_assist=True)

    assert submit_route.calls[0].request.url.params["markdown_assist"] == "true"


def test_detect_stamps_detect_job_id_onto_envelope(mock_backend, client, tiny_pdf):
    """The fill leg needs the detect job's id, but the server envelope doesn't
    carry it — the SDK adds it so callers needn't hold the Job separately."""
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)

    assert client.edit_detect(tiny_pdf)["detect_job_id"] == "job_ed_detect"


def test_submit_detect_returns_job_with_edit_op(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)

    job = client.submit_edit_detect(tiny_pdf)

    assert isinstance(job, Job)
    assert (job.job_id, job.op, job.status) == ("job_ed_detect", "edit", "pending")


# ── fill ────────────────────────────────────────────────────────────────────


def test_fill_posts_json_with_detect_job_id_and_values(mock_backend, client):
    submit_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    result = client.edit_fill("job_ed_detect", values={"0": "Jane Doe"})

    assert result["result"]["fills"] == [{"index": 0, "value": "Jane Doe"}]
    req = submit_route.calls[0].request
    assert req.headers["X-Async"] == "true"
    # Unlike every other submit, this one is JSON with no multipart body.
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {
        "detect_job_id": "job_ed_detect",
        "natural_language": False,
        "values": {"0": "Jane Doe"},
    }


def test_fill_accepts_indexed_list_values(mock_backend, client):
    submit_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    client.edit_fill("job_ed_detect", values=[{"index": 0, "value": "Jane"}])

    assert json.loads(submit_route.calls[0].request.content)["values"] == [
        {"index": 0, "value": "Jane"}
    ]


def test_fill_natural_language_sends_content(mock_backend, client):
    submit_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    client.edit_fill(
        "job_ed_detect", content="Patient is Jane Doe", natural_language=True
    )

    assert json.loads(submit_route.calls[0].request.content) == {
        "detect_job_id": "job_ed_detect",
        "natural_language": True,
        "content": "Patient is Jane Doe",
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"natural_language": True}, "non-empty content"),
        ({"natural_language": True, "content": "   "}, "non-empty content"),
        ({}, "requires values"),
    ],
)
def test_fill_mode_mismatches_fail_client_side(mock_backend, client, kwargs, match):
    """The server 400s on these; catching them locally saves a round-trip."""
    route = _seed_fill_submit(mock_backend)

    with pytest.raises(ValueError, match=match):
        client.edit_fill("job_ed_detect", **kwargs)

    assert not route.called


# ── one-call convenience ────────────────────────────────────────────────────


def test_edit_chains_detect_then_natural_language_fill(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    detect_route = _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)
    fill_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    result = client.edit(tiny_pdf, content="Patient is Jane Doe")

    assert result["detect_job_id"] == "job_ed_detect"
    assert result["form_schema"][0]["type"] == "text"
    assert result["fills"] == [{"index": 0, "value": "Jane Doe"}]
    # `pages` are the FILLED pages, not the blank ones from detect.
    assert result["pages"][0]["image_url"].endswith("filled1.png?sig=z")
    assert detect_route.called
    # The fill leg references the detect job the SDK just created.
    body = json.loads(fill_route.calls[0].request.content)
    assert body["detect_job_id"] == "job_ed_detect"
    assert body["natural_language"] is True


def test_edit_one_shot_takes_no_values(mock_backend, client, tiny_pdf):
    """One-shot detects the schema for the first time, so the caller cannot know the
    indices `values` are keyed by. Filling by index requires the two-leg flow."""
    _seed_presigned(mock_backend)
    route = _seed_detect_submit(mock_backend)

    with pytest.raises(TypeError, match="values"):
        client.edit(tiny_pdf, values={"0": "Jane Doe"})

    assert not route.called


@pytest.mark.parametrize("content", ["", "   "], ids=["empty", "whitespace"])
def test_edit_requires_non_empty_content(mock_backend, client, tiny_pdf, content):
    _seed_presigned(mock_backend)
    route = _seed_detect_submit(mock_backend)

    with pytest.raises(ValueError, match="content"):
        client.edit(tiny_pdf, content=content)

    # Fails before spending a detect (which is the metered leg).
    assert not route.called


def test_edit_omitting_content_is_an_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = _seed_detect_submit(mock_backend)

    with pytest.raises(TypeError, match="content"):
        client.edit(tiny_pdf)

    assert not route.called


# ── failures ────────────────────────────────────────────────────────────────


def test_failed_detect_job_raises_edit_error(mock_backend, client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ed_detect").mock(
        return_value=httpx.Response(200, json={
            "status": "failed",
            "error": "edit-service /detect failed (502)",
            "error_status_code": 502,
        }),
    )

    with pytest.raises(EditError) as exc:
        client.edit_detect(tiny_pdf)

    assert exc.value.status_code == 502
    assert "502" in str(exc.value)


def test_fill_404_on_unknown_detect_job(mock_backend, client):
    mock_backend.post("/v1/edit/fill").mock(
        return_value=httpx.Response(404, json={"detail": "detect_job_id not found"}),
    )

    with pytest.raises(EditError) as exc:
        client.edit_fill("job_missing", values={"0": "x"})

    assert exc.value.status_code == 404
    assert "not found" in str(exc.value)
