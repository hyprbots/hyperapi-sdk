"""Basic extract defers `category` to the organization unless the caller declares one.

`category` used to default to "financial" and was sent on every request, so the
router's request > org > platform precedence could never reach the org tier: an
organization with `defaults.extract_category` configured was unreachable from the
SDK entirely, and every call landed on the two-leg invoice adapter.

Omission is the mechanism — the same one `parse_mode` already uses two lines down.
`process()` has always omitted it, so before this change the SDK's two extract
paths disagreed with each other for the same document.

The guard is the subtle part. It used to raise whenever `category != "non_financial"`,
which under a None default would reject the exact call this change exists to enable:
a template with no declared category. It now fires only on an explicitly-financial
call — a self-contradiction the SDK can prove without knowing anything about the org.
"""

import json

# The seed helpers live in the sibling modes suite rather than conftest; reused
# here so both files exercise the same request-shaping fixtures.
from tests.test_contract_extract_modes import _seed_completed, _seed_presigned


TEMPLATE = {"listings": [{"street_name": None}]}


class TestOmissionIsTheMechanism:
    def test_no_category_means_no_query_param(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_omit", path="/v1/extract")
        client.extract(tiny_pdf)
        assert "category" not in submit.calls[0].request.url.params

    def test_explicit_financial_is_still_sent(self, mock_backend, client, tiny_pdf):
        """Deferring must not erase an explicit choice — that is the contract."""
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_fin", path="/v1/extract")
        client.extract(tiny_pdf, category="financial")
        assert submit.calls[0].request.url.params["category"] == "financial"

    def test_explicit_non_financial_is_still_sent(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_non", path="/v1/extract")
        client.extract(tiny_pdf, category="non_financial")
        assert submit.calls[0].request.url.params["category"] == "non_financial"


class TestSchemaWithoutACategory:
    def test_it_does_not_raise(self, mock_backend, client, tiny_pdf):
        """The call this change exists to enable. Under the old guard,
        `None != "non_financial"` was True and this raised client-side."""
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_sch", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)  # must not raise
        assert submit.calls[0].request.url.params["category"] == "non_financial"

    def test_the_template_still_rides(self, mock_backend, client, tiny_pdf):
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_sch2", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)
        body = submit.calls[0].request.content.decode()
        assert json.dumps(TEMPLATE) in body or "listings" in body

    def test_it_is_inferred_rather_than_deferred(self, mock_backend, client, tiny_pdf):
        """non_financial is sent EXPLICITLY, not omitted. Deferring would risk an
        org whose default is financial, where the server ignores `schema` silently
        — invoice-shaped output with nothing to tell the caller their template was
        dropped. The SDK cannot detect that (the org default is JWT-only), so it
        infers from the one signal it has: a schema is meaningful on non_financial
        alone."""
        _seed_presigned(mock_backend)
        submit, _ = _seed_completed(mock_backend, job_id="j_sch3", path="/v1/extract")
        client.extract(tiny_pdf, schema=TEMPLATE)
        assert "category" in submit.calls[0].request.url.params


class TestTheGuardStillCatchesTheContradiction:
    def test_schema_with_explicit_financial_raises(self, client, tiny_pdf):
        """The caller wrote category="financial" AND passed a template. That is
        self-contradictory regardless of org state, so it stays a loud failure."""
        try:
            client.extract(tiny_pdf, category="financial", schema=TEMPLATE)
        except ValueError as exc:
            assert "non_financial" in str(exc)
        else:
            raise AssertionError("expected ValueError for schema + category='financial'")
