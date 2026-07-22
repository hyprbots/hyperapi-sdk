"""Contract: download_pages() — write a result's page images to a folder.

One helper serves every op that returns presigned page images, but their page shapes differ:

  edit_detect/edit_fill : {"page": 1,        "image_url": ".../1.png",  "size": {...}}
  edit (composite)      : same, but flattened to result["pages"] instead of result["result"]["pages"]
  parse(include_image)  : {"page_number": 1, "image_url": ".../0.webp", "dimensions": {...}}

So the tests below pin page numbering (`page` vs `page_number`) and the file extension
(.png vs .webp) rather than assuming edit's shape everywhere.
"""

import httpx
import pytest

from hyperapi import HyperAPIError

PNG = b"\x89PNG\r\n\x1a\nfake"
WEBP = b"RIFF\x00\x00\x00\x00WEBPfake"


def _edit_result(nested=True):
    pages = [
        {"page": 1, "image_url": "https://s3.local/edit/1.png?sig=a", "size": {"w": 10, "h": 20}},
        {"page": 2, "image_url": "https://s3.local/edit/2.png?sig=b", "size": {"w": 10, "h": 20}},
    ]
    return {"status": "success", "result": {"phase": "fill", "pages": pages}} if nested \
        else {"detect_job_id": "j1", "pages": pages}


def _parse_result():
    return {
        "ocr": "Invoice #4471",
        "pages": [{
            "page_number": 1,
            "image_url": "https://s3.local/deskewed/org/hash/0.webp?sig=x",
            "dimensions": {"width": 824, "height": 1066},
        }],
    }


def _seed_images(mock_backend):
    mock_backend.get("https://s3.local/edit/1.png?sig=a").mock(
        return_value=httpx.Response(200, content=PNG))
    mock_backend.get("https://s3.local/edit/2.png?sig=b").mock(
        return_value=httpx.Response(200, content=PNG))
    mock_backend.get("https://s3.local/deskewed/org/hash/0.webp?sig=x").mock(
        return_value=httpx.Response(200, content=WEBP))


def test_downloads_edit_pages(mock_backend, client, tmp_path):
    _seed_images(mock_backend)

    paths = client.download_pages(_edit_result(), tmp_path)

    assert [p.name for p in paths] == ["page-1.png", "page-2.png"]
    assert paths[0].read_bytes() == PNG
    assert all(p.parent == tmp_path for p in paths)


def test_downloads_flattened_edit_result(mock_backend, client, tmp_path):
    """edit() flattens pages to the top level; edit_detect/edit_fill nest them under `result`."""
    _seed_images(mock_backend)

    paths = client.download_pages(_edit_result(nested=False), tmp_path)

    assert [p.name for p in paths] == ["page-1.png", "page-2.png"]


def test_parse_pages_keep_their_own_numbering_and_extension(mock_backend, client, tmp_path):
    """parse numbers pages with `page_number` and serves .webp, not .png."""
    _seed_images(mock_backend)

    paths = client.download_pages(_parse_result(), tmp_path)

    assert [p.name for p in paths] == ["page-1.webp"]
    assert paths[0].read_bytes() == WEBP


def test_prefix_is_configurable(mock_backend, client, tmp_path):
    _seed_images(mock_backend)

    paths = client.download_pages(_edit_result(), tmp_path, prefix="filled")

    assert [p.name for p in paths] == ["filled-1.png", "filled-2.png"]


def test_creates_the_destination_folder(mock_backend, client, tmp_path):
    _seed_images(mock_backend)
    dest = tmp_path / "nested" / "out"

    client.download_pages(_edit_result(), dest)

    assert dest.is_dir()


def test_expired_url_reports_as_expired(mock_backend, client, tmp_path):
    mock_backend.get("https://s3.local/edit/1.png?sig=a").mock(
        return_value=httpx.Response(403, content=b"<Error>Request has expired</Error>"))

    with pytest.raises(HyperAPIError, match="expired"):
        client.download_pages(_edit_result(), tmp_path)


def test_result_without_pages_is_rejected(client, tmp_path):
    # A basic parse() carries `pages` as an INT page count, not a list of pages.
    with pytest.raises(ValueError, match="No 'pages'"):
        client.download_pages({"ocr": "text", "pages": 1}, tmp_path)


def test_page_without_image_url_is_rejected(client, tmp_path):
    result = {"result": {"pages": [{"page": 1, "text": "no image"}]}}

    with pytest.raises(ValueError, match="include_image"):
        client.download_pages(result, tmp_path)


def test_redact_images_are_not_downloadable(client, tmp_path):
    """redact returns `images` as inline base64 strings — a different shape with nothing to
    fetch. It must fail loudly rather than silently write nothing."""
    with pytest.raises(ValueError, match="No 'pages'"):
        client.download_pages({"result": {"images": ["UkVEQUNURUQ="]}}, tmp_path)


async def test_async_downloads_pages(mock_backend, async_client, tmp_path):
    _seed_images(mock_backend)

    paths = await async_client.download_pages(_edit_result(), tmp_path)

    assert [p.name for p in paths] == ["page-1.png", "page-2.png"]
    assert paths[0].read_bytes() == PNG


async def test_async_expired_url_reports_as_expired(mock_backend, async_client, tmp_path):
    mock_backend.get("https://s3.local/edit/1.png?sig=a").mock(
        return_value=httpx.Response(403, content=b"<Error>Request has expired</Error>"))

    with pytest.raises(HyperAPIError, match="expired"):
        await async_client.download_pages(_edit_result(), tmp_path)
