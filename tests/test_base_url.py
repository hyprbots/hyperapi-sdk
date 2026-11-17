"""base_url validation: the API key header must only go to a well-formed http(s) URL."""

import warnings

import pytest

from hyperapi import AsyncHyperAPIClient, HyperAPIClient

KEY = "hk_test_" + "x" * 32


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
def test_default_is_https(cls, monkeypatch):
    monkeypatch.delenv("HYPERAPI_URL", raising=False)
    assert cls(api_key=KEY).base_url == "https://apis.hyperbots.com"


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
def test_https_trailing_slash_stripped(cls):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert cls(api_key=KEY, base_url="https://example.com/").base_url == "https://example.com"


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
@pytest.mark.parametrize("bad", ["ftp://example.com", "example.com", "file:///etc/passwd", "https://"])
def test_rejects_non_http_or_hostless(cls, bad):
    with pytest.raises(ValueError, match="Invalid base_url"):
        cls(api_key=KEY, base_url=bad)


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
def test_plain_http_remote_warns(cls):
    with pytest.warns(UserWarning, match="plain http"):
        cls(api_key=KEY, base_url="http://example.com")


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
@pytest.mark.parametrize("url", ["http://localhost:8001", "http://127.0.0.1:9000"])
def test_plain_http_loopback_is_quiet(cls, url):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cls(api_key=KEY, base_url=url)


def test_env_var_is_validated(monkeypatch):
    monkeypatch.setenv("HYPERAPI_URL", "ftp://example.com")
    with pytest.raises(ValueError):
        HyperAPIClient(api_key=KEY)


@pytest.mark.parametrize("cls", [HyperAPIClient, AsyncHyperAPIClient])
@pytest.mark.parametrize(
    "bad",
    [
        "https://apis.hyperbots.com@other.example",
        "https://user:pw@example.com",
        "https://example.com/?x=1",
        "https://example.com/#frag",
        "https://exa mple.com",
    ],
)
def test_rejects_userinfo_query_fragment_whitespace(cls, bad):
    with pytest.raises(ValueError, match="Invalid base_url"):
        cls(api_key=KEY, base_url=bad)


def test_userinfo_error_does_not_echo_password():
    with pytest.raises(ValueError) as exc:
        HyperAPIClient(api_key=KEY, base_url="https://user:s3cretpw@example.com")
    assert "s3cretpw" not in str(exc.value)


def test_lookalike_loopback_host_still_warns():
    with pytest.warns(UserWarning, match="plain http"):
        HyperAPIClient(api_key=KEY, base_url="http://localhost.other.example")
