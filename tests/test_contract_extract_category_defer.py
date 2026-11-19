"""Basic extract defers `category` to the organization unless the caller declares one.

`category` used to default to "financial" and was sent on every request, so the
server-side default could never apply: an organization with a configured default
category was unreachable from the SDK entirely, and every call used the built-in
invoice and receipt fields.

Omission is the mechanism — the same one `parse_mode` already uses two lines down.
`process()` has always omitted it, so before this change the SDK's two extract
paths disagreed with each other for the same document.

A template with no declared category is sent as "non_financial" (filled
directly). An explicit category is always sent as given, template or not: the
API applies a template on "financial" too, by remapping the invoice extractor's
output into it.
"""

import json

# The seed helpers live in the sibling modes suite rather than conftest; reused
# here so both files exercise the same request-shaping fixtures.
from tests.test_contract_extract_modes import _seed_completed, _seed_presigned


TEMPLATE = {"listings": [{"street_name": None}]}


class TestOmissionIsTheMechanism:
    def test_no_category_means_no_query_param(self, mock_api, client, tiny_pdf):
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_omit", path="/v1/extract")
        client.extract(tiny_pdf)
        assert "category" not in submit.calls[0].request.url.params

    def test_explicit_financial_is_still_sent(self, mock_api, client, tiny_pdf):
        """Deferring must not erase an explicit choice — that is the contract."""
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_fin", path="/v1/extract")
        client.extract(tiny_pdf, category="financial")
        assert submit.calls[0].request.url.params["category"] == "financial"

    def test_explicit_non_financial_is_still_sent(self, mock_api, client, tiny_pdf):
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_non", path="/v1/extract")
        client.extract(tiny_pdf, category="non_financial")
        assert submit.calls[0].request.url.params["category"] == "non_financial"


class TestSchemaWithoutACategory:
    def test_it_does_not_raise(self, mock_api, client, tiny_pdf):
        """The call this change exists to enable. Under the old guard,
        `None != "non_financial"` was True and this raised client-side."""
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_sch", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)  # must not raise
        assert submit.calls[0].request.url.params["category"] == "non_financial"

    def test_the_template_still_rides(self, mock_api, client, tiny_pdf):
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_sch2", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)
        body = submit.calls[0].request.content.decode()
        assert json.dumps(TEMPLATE) in body or "listings" in body

    def test_it_is_inferred_rather_than_deferred(self, mock_api, client, tiny_pdf):
        """non_financial is sent EXPLICITLY, not omitted. The two categories treat
        a template differently (filled directly vs. invoice output remapped into
        it), and the SDK cannot read the organization default, so deferring would
        make the same call behave differently per account."""
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_sch3", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)
        assert "category" in submit.calls[0].request.url.params


class TestAnExplicitCategoryIsRespected:
    def test_schema_with_explicit_financial_is_sent_as_financial(
        self, mock_api, client, tiny_pdf
    ):
        """The caller wrote category="financial" AND passed a template. The API
        runs the invoice extractor and remaps its output into the template, so
        the SDK sends both as given — no inference, no refusal."""
        _seed_presigned(mock_api)
        submit, _ = _seed_completed(mock_api, job_id="j_fin_sch", path="/v1/extract")
        client.extract(tiny_pdf, category="financial", schema=TEMPLATE)
        request = submit.calls[0].request
        assert request.url.params["category"] == "financial"
        assert "schema=" in request.content.decode()
