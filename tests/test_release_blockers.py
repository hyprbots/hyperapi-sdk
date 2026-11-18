"""0.7.1 release blockers: ``schema`` resolution and API-key hygiene.

Both clients are covered: the sync client owns the shared helpers and the async
client imports them, so a test on one client alone would not catch the other
drifting back to its own copy.

* ``schema``: a ``str`` is always JSON text and never touches the filesystem;
  only a :class:`pathlib.Path` is read as a file. Before this, a str that
  looked like a path was read and uploaded — an app forwarding user-supplied
  schema text could be made to send a local file — and a long JSON string
  raised ``OSError: File name too long`` from ``Path(s).exists()``.
* API key: surrounding whitespace is stripped, inner whitespace/control
  characters are refused without echoing the key, and transport errors are
  raised ``from None`` so the httpx exception (whose request carries the
  ``X-API-Key`` header) is not chained into tracebacks.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import httpx
import pytest

from hyperapi import AsyncHyperAPIClient, HyperAPIClient
from hyperapi.exceptions import AuthenticationError, HyperAPIError
from tests.conftest import TEST_BASE_URL
from tests.test_contract_extract_schema import _sent_schema


SECRET_MARKER = "TOP-SECRET-FILE-CONTENT"


def _seed(mock_api, *, key="doc_rb"):
    mock_api.post("/v1/documents/upload").mock(
        return_value=httpx.Response(200, json={
            "document_key": key,
            "upload_url": f"https://s3.local/{key}?sig=x",
            "expires_in": 600,
        }),
    )
    mock_api.put(f"https://s3.local/{key}?sig=x").mock(return_value=httpx.Response(200))
    return mock_api.post("/v1/extract").mock(
        return_value=httpx.Response(202, json={
            "job_id": "job_rb", "status": "pending", "poll_url": "/v1/jobs/job_rb",
        }),
    )


def _big_schema(min_len: int) -> dict:
    schema: dict = {}
    i = 0
    while len(json.dumps(schema)) <= min_len:
        schema[f"field_{i:05d}"] = None
        i += 1
    return schema


def _secret_json_file(tmp_path: Path) -> Path:
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"leak": SECRET_MARKER}))
    return f


def _all_request_bodies(mock_api) -> str:
    return "".join(
        call.request.content.decode("utf-8", "replace") for call in mock_api.calls
    )


# ── schema: sync ────────────────────────────────────────────────────────────


class TestSchemaSync:
    @pytest.mark.parametrize("min_len", [300, 5000])
    def test_long_json_string_is_parsed(self, mock_api, client, tiny_pdf, min_len):
        schema = _big_schema(min_len)
        text = json.dumps(schema)
        assert len(text) > min_len and not text.endswith(".json")
        route = _seed(mock_api)

        client.submit_extract(tiny_pdf, category="non_financial", schema=text)

        assert _sent_schema(route) == schema

    def test_dict_is_unchanged(self, mock_api, client, tiny_pdf):
        route = _seed(mock_api)
        schema = {"a": None, "b": {"c": "unselected"}}

        client.submit_extract(tiny_pdf, category="non_financial", schema=schema)

        assert _sent_schema(route) == schema

    def test_json_file_named_as_str_is_not_read(self, mock_api, client, tiny_pdf, tmp_path):
        f = _secret_json_file(tmp_path)
        _seed(mock_api)

        with pytest.raises(ValueError, match=r"pathlib\.Path") as exc:
            client.submit_extract(tiny_pdf, category="non_financial", schema=str(f))

        assert SECRET_MARKER not in str(exc.value)
        assert SECRET_MARKER not in _all_request_bodies(mock_api)

    def test_system_path_as_str_is_not_read(self, mock_api, client, tiny_pdf):
        _seed(mock_api)

        with pytest.raises(ValueError, match=r"pathlib\.Path"):
            client.submit_extract(tiny_pdf, category="non_financial", schema="/etc/hosts")

        assert not any(c.request.url.path == "/v1/extract" for c in mock_api.calls)

    def test_same_file_as_path_is_read(self, mock_api, client, tiny_pdf, tmp_path):
        f = _secret_json_file(tmp_path)
        route = _seed(mock_api)

        client.submit_extract(tiny_pdf, category="non_financial", schema=f)

        assert _sent_schema(route) == {"leak": SECRET_MARKER}


# ── schema: async ───────────────────────────────────────────────────────────


class TestSchemaAsync:
    @pytest.mark.parametrize("min_len", [300, 5000])
    async def test_long_json_string_is_parsed(self, mock_api, async_client, tiny_pdf, min_len):
        schema = _big_schema(min_len)
        route = _seed(mock_api)

        await async_client.submit_extract(
            tiny_pdf, category="non_financial", schema=json.dumps(schema)
        )

        assert _sent_schema(route) == schema

    async def test_dict_is_unchanged(self, mock_api, async_client, tiny_pdf):
        route = _seed(mock_api)
        schema = {"a": None}

        await async_client.submit_extract(tiny_pdf, category="non_financial", schema=schema)

        assert _sent_schema(route) == schema

    async def test_json_file_named_as_str_is_not_read(
        self, mock_api, async_client, tiny_pdf, tmp_path
    ):
        f = _secret_json_file(tmp_path)
        _seed(mock_api)

        with pytest.raises(ValueError, match=r"pathlib\.Path"):
            await async_client.submit_extract(
                tiny_pdf, category="non_financial", schema=str(f)
            )

        assert SECRET_MARKER not in _all_request_bodies(mock_api)

    async def test_system_path_as_str_is_not_read(self, mock_api, async_client, tiny_pdf):
        _seed(mock_api)

        with pytest.raises(ValueError, match=r"pathlib\.Path"):
            await async_client.submit_extract(
                tiny_pdf, category="non_financial", schema="/etc/hosts"
            )

    async def test_same_file_as_path_is_read(self, mock_api, async_client, tiny_pdf, tmp_path):
        f = _secret_json_file(tmp_path)
        route = _seed(mock_api)

        await async_client.submit_extract(tiny_pdf, category="non_financial", schema=f)

        assert _sent_schema(route) == {"leak": SECRET_MARKER}


# ── API key ─────────────────────────────────────────────────────────────────


CLIENTS = [HyperAPIClient, AsyncHyperAPIClient]
CLEAN_KEY = "hk_live_abcdef1234567890"


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("raw", [CLEAN_KEY + "\n", CLEAN_KEY + " ", "  " + CLEAN_KEY + "\r\n"])
def test_surrounding_whitespace_is_stripped(cls, raw):
    c = cls(api_key=raw, base_url=TEST_BASE_URL)
    assert c.api_key == CLEAN_KEY
    assert c._get_headers()["X-API-Key"] == CLEAN_KEY


@pytest.mark.parametrize("cls", CLIENTS)
def test_env_key_is_stripped(cls, monkeypatch):
    monkeypatch.setenv("HYPERAPI_KEY", CLEAN_KEY + "\n")
    assert cls(base_url=TEST_BASE_URL).api_key == CLEAN_KEY


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_empty_key_is_required(cls, raw):
    with pytest.raises(AuthenticationError, match="API key required"):
        cls(api_key=raw, base_url=TEST_BASE_URL)


@pytest.mark.parametrize("cls", CLIENTS)
@pytest.mark.parametrize(
    "raw",
    [
        "hk_live_abc def123",
        "hk_live_abc\ndef123",
        "hk_live_abc\x00def123",
        "hk_live_abc\x7fdef123",
        "hk_live_abcédef123",
    ],
)
def test_bad_key_is_refused_without_echo(cls, raw):
    with pytest.raises(AuthenticationError) as exc:
        cls(api_key=raw, base_url=TEST_BASE_URL)
    text = "".join(traceback.format_exception(exc.type, exc.value, exc.tb))
    assert "def123" not in text
    assert "hk_live_abc" not in text


@pytest.mark.parametrize("cls", CLIENTS)
def test_api_key_is_read_only_and_repr_masks(cls):
    c = cls(api_key=CLEAN_KEY, base_url=TEST_BASE_URL)
    assert c.api_key == CLEAN_KEY
    with pytest.raises(AttributeError):
        c.api_key = "hk_live_other"  # type: ignore[misc]
    r = repr(c)
    assert CLEAN_KEY not in r
    assert r.startswith(f"{cls.__name__}(api_key='hk_***7890'")


def _connect_error(request: httpx.Request):
    # Mirror the real failure: httpx/httpcore exceptions carry the request,
    # whose headers include X-API-Key in clear.
    raise httpx.ConnectError(f"boom {request.headers['X-API-Key']}", request=request)


def _assert_not_chained(exc_info, key: str):
    err = exc_info.value
    assert isinstance(err, HyperAPIError)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    text = "".join(traceback.format_exception(exc_info.type, err, exc_info.tb))
    assert key not in text


def test_transport_error_is_not_chained_sync(mock_api):
    mock_api.get("/v1/limits").mock(side_effect=_connect_error)
    mock_api.get("/v1/jobs/j1").mock(side_effect=_connect_error)
    c = HyperAPIClient(api_key=CLEAN_KEY, base_url=TEST_BASE_URL, poll_max_transient_retries=0)
    with pytest.raises(HyperAPIError) as exc:
        c.get_limits()
    _assert_not_chained(exc, CLEAN_KEY)
    with pytest.raises(HyperAPIError) as exc:
        c.get_job("j1")
    _assert_not_chained(exc, CLEAN_KEY)
    c.close()


def test_transport_error_on_submit_is_not_chained_sync(mock_api, tiny_pdf):
    mock_api.post("/v1/documents/upload").mock(side_effect=_connect_error)
    c = HyperAPIClient(api_key=CLEAN_KEY, base_url=TEST_BASE_URL)
    with pytest.raises(HyperAPIError) as exc:
        c.submit_parse(tiny_pdf)
    _assert_not_chained(exc, CLEAN_KEY)
    c.close()


async def test_transport_error_is_not_chained_async(mock_api, tiny_pdf):
    mock_api.get("/v1/limits").mock(side_effect=_connect_error)
    mock_api.get("/v1/jobs/j1").mock(side_effect=_connect_error)
    mock_api.post("/v1/documents/upload").mock(side_effect=_connect_error)
    c = AsyncHyperAPIClient(api_key=CLEAN_KEY, base_url=TEST_BASE_URL)
    for call in (c.get_limits, lambda: c.get_job("j1"), lambda: c.submit_parse(tiny_pdf)):
        with pytest.raises(HyperAPIError) as exc:
            await call()
        _assert_not_chained(exc, CLEAN_KEY)
    await c.aclose()


# ── docstrings ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", CLIENTS)
def test_schema_docstrings_say_str_is_json_path_is_file(cls):
    for name in ("submit_extract", "extract"):
        doc = getattr(cls, name).__doc__ or ""
        assert "pathlib.Path" in doc, f"{cls.__name__}.{name}"


@pytest.mark.parametrize("cls", CLIENTS)
def test_process_docstring_points_at_inference_envelope(cls):
    doc = cls.process.__doc__ or ""
    assert '["data"]["result"]["entities"]' in doc
