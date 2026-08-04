"""Contract: extract-fast's ``schema`` template (#30 follow-up).

``schema`` is a blank JSON object the result is filled into. It reaches the
router as a ``schema`` form field on POST /v1/extract and only means anything
on ``category="non_financial"`` — the router ignores it everywhere else.

The gating tests matter more than the happy path: a template that is silently
ignored produces plausible-looking output that answers a different question,
with nothing to tell the caller their template did not apply.
"""

import json

import httpx
import pytest


def _seed_presigned(mock_backend, *, key="doc_sch"):
    mock_backend.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_backend.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))


def _seed_submit(mock_backend, *, job_id="job_sch_1"):
    return mock_backend.post("/v1/extract").mock(
        return_value=httpx.Response(202, json={
            "job_id": job_id, "status": "pending", "poll_url": f"/v1/jobs/{job_id}",
        }),
    )


def _sent_schema(route):
    """The `schema` form field on the submit request, or None if absent."""
    body = route.calls.last.request.content.decode()
    fields = dict(
        part.split("=", 1) for part in body.split("&") if "=" in part
    )
    raw = fields.get("schema")
    if raw is None:
        return None
    from urllib.parse import unquote_plus
    return json.loads(unquote_plus(raw))


TEMPLATE = {"patient_name": None, "insurance": {"Medicare": "unselected"}}


class TestSchemaResolution:
    def test_dict_is_sent_as_json(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)

        client.submit_extract(tiny_pdf, category="non_financial", schema=TEMPLATE)

        assert _sent_schema(route) == TEMPLATE

    def test_raw_json_string_is_accepted(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)

        client.submit_extract(
            tiny_pdf, category="non_financial", schema=json.dumps(TEMPLATE)
        )

        assert _sent_schema(route) == TEMPLATE

    def test_path_to_json_file_is_read(self, mock_backend, client, tiny_pdf, tmp_path):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)
        f = tmp_path / "template.json"
        f.write_text(json.dumps(TEMPLATE))

        client.submit_extract(tiny_pdf, category="non_financial", schema=f)

        assert _sent_schema(route) == TEMPLATE

    def test_omitted_schema_sends_no_field(self, mock_backend, client, tiny_pdf):
        # Absent is meaningful upstream: it selects the org's stored default
        # template. An empty string would NOT mean the same thing.
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)

        client.submit_extract(tiny_pdf, category="non_financial")

        assert _sent_schema(route) is None

    def test_non_object_json_is_rejected(self, client, tiny_pdf):
        with pytest.raises(ValueError, match="JSON object"):
            client.submit_extract(tiny_pdf, category="non_financial", schema="[1, 2]")

    def test_malformed_json_is_rejected(self, client, tiny_pdf):
        with pytest.raises(ValueError):
            client.submit_extract(tiny_pdf, category="non_financial", schema="{oops")

    def test_missing_file_path_says_so(self, client, tiny_pdf, tmp_path):
        # Previously fell through to json.loads("/…/nope.json") and reported
        # "must be a dict, a path to a JSON file, or a JSON string" — which
        # points the caller at the wrong problem. _resolve_path (the sibling
        # helper for documents) already raises FileNotFoundError here.
        missing = tmp_path / "nope.json"

        with pytest.raises(FileNotFoundError):
            client.submit_extract(tiny_pdf, category="non_financial", schema=missing)


class TestSchemaGating:
    """The router honours `schema` on non_financial only."""

    def test_schema_with_financial_category_is_rejected(self, client, tiny_pdf):
        with pytest.raises(ValueError, match="non_financial"):
            client.submit_extract(tiny_pdf, category="financial", schema=TEMPLATE)

    def test_financial_without_schema_still_works(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)

        client.submit_extract(tiny_pdf, category="financial")

        assert route.called
        assert _sent_schema(route) is None

    def test_extract_forwards_schema_to_submit(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)
        mock_backend.get("/v1/jobs/job_sch_1").mock(
            return_value=httpx.Response(200, json={
                "status": "completed", "result": {"data": {"patient_name": "A"}},
            }),
        )

        client.extract(tiny_pdf, category="non_financial", schema=TEMPLATE)

        assert _sent_schema(route) == TEMPLATE


class TestDocstrings:
    def test_no_dangling_extract_fast_reference(self):
        """#30 documented a `extract_fast` method that does not exist and was
        deliberately not added — it told users to prefer a name they cannot
        call, and made the sync/async docstrings diverge."""
        from hyperapi.client import HyperAPIClient
        from hyperapi.async_client import AsyncHyperAPIClient

        assert not hasattr(HyperAPIClient, "extract_fast")
        for cls in (HyperAPIClient, AsyncHyperAPIClient):
            for name in ("submit_extract", "extract"):
                doc = getattr(cls, name).__doc__ or ""
                assert "extract_fast" not in doc, f"{cls.__name__}.{name}"


class TestPathVsJsonHeuristic:
    """Free-form text must be reported as invalid JSON, not as a missing file.

    The first cut treated any string not starting with "{" or "[" as a
    filesystem path, so markdown produced
    `FileNotFoundError: Schema file not found: # Patient\n- name` — which
    names the wrong problem and hides that the real rule is "templates are
    JSON objects".
    """

    def test_markdown_is_reported_as_invalid_json(self, client, tiny_pdf):
        with pytest.raises(ValueError, match="JSON"):
            client.submit_extract(
                tiny_pdf,
                category="non_financial",
                schema="# Patient\n- name\n- dob",
            )

    def test_prose_is_reported_as_invalid_json(self, client, tiny_pdf):
        with pytest.raises(ValueError, match="JSON"):
            client.submit_extract(
                tiny_pdf, category="non_financial", schema="just some words"
            )

    def test_json_filename_that_is_missing_still_says_file_not_found(
        self, client, tiny_pdf, tmp_path
    ):
        # A .json path is unambiguously a file reference, so this must stay a
        # FileNotFoundError — the fix must not swallow the case #31 added.
        with pytest.raises(FileNotFoundError):
            client.submit_extract(
                tiny_pdf, category="non_financial", schema=str(tmp_path / "nope.json")
            )

    def test_existing_file_without_json_suffix_is_still_read(
        self, mock_backend, client, tiny_pdf, tmp_path
    ):
        _seed_presigned(mock_backend)
        route = _seed_submit(mock_backend)
        f = tmp_path / "template.txt"
        f.write_text(json.dumps(TEMPLATE))

        client.submit_extract(tiny_pdf, category="non_financial", schema=str(f))

        assert _sent_schema(route) == TEMPLATE
