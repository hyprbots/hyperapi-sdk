"""Contract: async edit_detect() / edit_fill() / edit() — the two-call form-filling flow.

Parity mirror of tests/test_contract_edit.py — same wire assertions, async
client. Keeps the JSON-bodied fill leg (the SDK's only submit that uploads
nothing) honest on both clients.
"""

import json

import httpx
import pytest

from hyperapi import EditError, Job

# Fixtures are shared with the sync suite; the seeds are duplicated rather than
# imported so each file reads standalone, matching the other async contract tests.
from tests.test_contract_edit import (  # noqa: F401
    _seed_detect_completed,
    _seed_detect_submit,
    _seed_fill_completed,
    _seed_fill_submit,
    _seed_presigned,
)


# ── detect ──────────────────────────────────────────────────────────────────


async def test_detect_uses_async_and_markdown_assist_query_param(
    mock_backend, async_client, tiny_pdf
):
    _seed_presigned(mock_backend)
    submit_route = _seed_detect_submit(mock_backend)
    poll_route = _seed_detect_completed(mock_backend)

    result = await async_client.edit_detect(tiny_pdf)

    assert result["result"]["form_schema"][0]["field_name"] == "Patient:Name"
    req = submit_route.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.url.params["markdown_assist"] == "false"
    assert "document_key" in req.content.decode()
    assert poll_route.called


async def test_detect_markdown_assist_true(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    submit_route = _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)

    await async_client.edit_detect(tiny_pdf, markdown_assist=True)

    assert submit_route.calls[0].request.url.params["markdown_assist"] == "true"


async def test_detect_stamps_detect_job_id_onto_envelope(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)

    result = await async_client.edit_detect(tiny_pdf)

    assert result["detect_job_id"] == "job_ed_detect"


async def test_submit_detect_returns_job_with_edit_op(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)

    job = await async_client.submit_edit_detect(tiny_pdf)

    assert isinstance(job, Job)
    assert (job.job_id, job.op, job.status) == ("job_ed_detect", "edit", "pending")


# ── fill ────────────────────────────────────────────────────────────────────


async def test_fill_posts_json_with_detect_job_id_and_values(mock_backend, async_client):
    submit_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    result = await async_client.edit_fill("job_ed_detect", values={"0": "Jane Doe"})

    assert result["result"]["fills"] == [{"index": 0, "value": "Jane Doe"}]
    req = submit_route.calls[0].request
    assert req.headers["X-Async"] == "true"
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {
        "detect_job_id": "job_ed_detect",
        "natural_language": False,
        "values": {"0": "Jane Doe"},
    }


async def test_fill_natural_language_sends_content(mock_backend, async_client):
    submit_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    await async_client.edit_fill(
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
        ({}, "requires values"),
    ],
)
async def test_fill_mode_mismatches_fail_client_side(mock_backend, async_client, kwargs, match):
    route = _seed_fill_submit(mock_backend)

    with pytest.raises(ValueError, match=match):
        await async_client.edit_fill("job_ed_detect", **kwargs)

    assert not route.called


# ── one-call convenience ────────────────────────────────────────────────────


async def test_edit_chains_detect_then_natural_language_fill(
    mock_backend, async_client, tiny_pdf
):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)
    _seed_detect_completed(mock_backend)
    fill_route = _seed_fill_submit(mock_backend)
    _seed_fill_completed(mock_backend)

    result = await async_client.edit(tiny_pdf, content="Patient is Jane Doe")

    assert result["detect_job_id"] == "job_ed_detect"
    assert result["fills"] == [{"index": 0, "value": "Jane Doe"}]
    assert result["pages"][0]["image_url"].endswith("filled1.png?sig=z")
    body = json.loads(fill_route.calls[0].request.content)
    assert (body["detect_job_id"], body["natural_language"]) == ("job_ed_detect", True)


async def test_edit_one_shot_takes_no_values(mock_backend, async_client, tiny_pdf):
    """Mirrors the sync client: one-shot is content-only — it detects the schema for the
    first time, so the caller cannot know the indices `values` are keyed by."""
    _seed_presigned(mock_backend)
    route = _seed_detect_submit(mock_backend)

    with pytest.raises(TypeError, match="values"):
        await async_client.edit(tiny_pdf, values={"0": "Jane"})

    assert not route.called


@pytest.mark.parametrize("content", ["", "   "], ids=["empty", "whitespace"])
async def test_edit_requires_non_empty_content(mock_backend, async_client, tiny_pdf, content):
    _seed_presigned(mock_backend)
    route = _seed_detect_submit(mock_backend)

    with pytest.raises(ValueError, match="content"):
        await async_client.edit(tiny_pdf, content=content)

    # Fails before spending a detect (which is the metered leg).
    assert not route.called


# ── failures ────────────────────────────────────────────────────────────────


async def test_failed_detect_job_raises_edit_error(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    _seed_detect_submit(mock_backend)
    mock_backend.get("/v1/jobs/job_ed_detect").mock(
        return_value=httpx.Response(200, json={
            "status": "failed",
            "error": "edit-service /detect failed (502)",
            "status_code": 502,
        }),
    )

    with pytest.raises(EditError) as exc:
        await async_client.edit_detect(tiny_pdf)

    assert exc.value.status_code == 502


async def test_fill_404_on_unknown_detect_job(mock_backend, async_client):
    mock_backend.post("/v1/edit/fill").mock(
        return_value=httpx.Response(404, json={"detail": "detect_job_id not found"}),
    )

    with pytest.raises(EditError) as exc:
        await async_client.edit_fill("job_missing", values={"0": "x"})

    assert exc.value.status_code == 404
