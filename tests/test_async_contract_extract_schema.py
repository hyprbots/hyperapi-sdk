"""Async twin of test_contract_extract_schema.py.

The sync client owns the exhaustive cases; this pins that the async client
resolves and gates `schema` identically, since the two carry duplicated
`_resolve_schema` implementations that can silently drift apart.
"""

import json

import httpx
import pytest

from tests.test_contract_extract_schema import TEMPLATE, _sent_schema


def _seed_presigned(mock_backend, *, key="doc_sch_a"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_submit(mock_backend, *, job_id="job_sch_a1"):
    return mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


@pytest.mark.asyncio
async def test_async_dict_schema_is_sent(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = _seed_submit(mock_backend)

    await async_client.submit_extract(
        tiny_pdf, category="non_financial", schema=TEMPLATE
    )

    assert _sent_schema(route) == TEMPLATE


@pytest.mark.asyncio
async def test_async_raw_json_string_is_accepted(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = _seed_submit(mock_backend)

    await async_client.submit_extract(
        tiny_pdf, category="non_financial", schema=json.dumps(TEMPLATE)
    )

    assert _sent_schema(route) == TEMPLATE


@pytest.mark.asyncio
async def test_async_omitted_schema_sends_no_field(mock_backend, async_client, tiny_pdf):
    _seed_presigned(mock_backend)
    route = _seed_submit(mock_backend)

    await async_client.submit_extract(tiny_pdf, category="non_financial")

    assert _sent_schema(route) is None


@pytest.mark.asyncio
async def test_async_schema_with_financial_is_rejected(async_client, tiny_pdf):
    with pytest.raises(ValueError, match="non_financial"):
        await async_client.submit_extract(
            tiny_pdf, category="financial", schema=TEMPLATE
        )


@pytest.mark.asyncio
async def test_async_missing_file_path_says_so(async_client, tiny_pdf, tmp_path):
    with pytest.raises(FileNotFoundError):
        await async_client.submit_extract(
            tiny_pdf, category="non_financial", schema=tmp_path / "nope.json"
        )


@pytest.mark.asyncio
async def test_async_non_object_json_is_rejected(async_client, tiny_pdf):
    with pytest.raises(ValueError, match="JSON object"):
        await async_client.submit_extract(
            tiny_pdf, category="non_financial", schema="[1, 2]"
        )
