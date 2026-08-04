"""AsyncHyperAPIClient quickstart — two shapes.

Run as a standalone script or import the FastAPI snippet into your own app.

Standalone::

    python examples/async_quickstart.py path/to/invoice.pdf

FastAPI (excerpt, see `make_fastapi_app` below)::

    # in your_app.py:
    from examples.async_quickstart import make_fastapi_app
    app = make_fastapi_app()
    # then: uvicorn your_app:app

Note: this file intentionally avoids ``from __future__ import annotations``
because FastAPI needs runtime access to type annotations (UploadFile,
specifically) to build its dependency-injection graph; deferred annotations
turn those types into forward-ref strings that FastAPI cannot resolve.
"""

import asyncio
import sys
from pathlib import Path

from hyperapi import AsyncHyperAPIClient


# ──────────────────────────────────────────────────────────────────────────
# Standalone script — extract a single file
# ──────────────────────────────────────────────────────────────────────────


async def extract_one(path: Path) -> dict:
    """The minimal async happy path: one extract call, auto-poll under the hood."""
    async with AsyncHyperAPIClient() as client:  # reads HYPERAPI_KEY from env
        return await client.extract(path)


# ──────────────────────────────────────────────────────────────────────────
# Parallel fan-out — process N invoices concurrently in one process
# ──────────────────────────────────────────────────────────────────────────


async def extract_many(paths: list[Path]) -> list[dict]:
    """Async's biggest practical win: 100s of concurrent extracts in one process.

    The sync client can do this only via threads (heavy stacks + GIL). The
    async client uses one event loop — every wait yields control while the
    server works.
    """
    async with AsyncHyperAPIClient() as client:
        results = await asyncio.gather(*(client.extract(p) for p in paths))
    return results


# ──────────────────────────────────────────────────────────────────────────
# FastAPI integration — one client per app, never per request
# ──────────────────────────────────────────────────────────────────────────


def make_fastapi_app():
    """Return a FastAPI app that wraps HyperAPI's extract endpoint.

    Two important patterns to copy:

    1. **One client per application**, stored on `app.state`. Construction is
       cheap but the underlying `httpx.AsyncClient` holds a connection pool
       you want to reuse across requests.
    2. **Close on shutdown** via the lifespan handler — without it, the
       connection pool leaks at process exit (cosmetic warning, but ugly).

    To run::

        uvicorn examples.async_quickstart:app --reload
    """
    from contextlib import asynccontextmanager
    from tempfile import NamedTemporaryFile

    from fastapi import FastAPI, UploadFile

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hyperapi = AsyncHyperAPIClient()  # reads HYPERAPI_KEY
        try:
            yield
        finally:
            await app.state.hyperapi.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/extract")
    async def extract_endpoint(file: UploadFile):
        # Persist the upload to a tmp file so HyperAPIClient.upload_document
        # can stream it (httpx multipart needs a real file-like object on disk
        # for large bodies — for tiny test files the BytesIO path also works).
        with NamedTemporaryFile(
            delete=False,
            suffix=Path(file.filename or "upload").suffix,
        ) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        return await app.state.hyperapi.extract(tmp_path)

    return app


# NOTE: We intentionally do NOT auto-construct `app = make_fastapi_app()` at
# import time. Reasons:
#   (1) Building the app reads HYPERAPI_KEY from the env via the lifespan
#       handler — surprising side effect for callers who only wanted to import
#       `extract_one` or `extract_many` from this module.
#   (2) Auto-constructing requires FastAPI to be installed, even for users who
#       just want the standalone script path.
#   (3) The user almost certainly wants their own `app = FastAPI()` with their
#       own routes; this file is a snippet to copy, not a service to deploy.
#
# To use this with uvicorn, copy `make_fastapi_app` into your own module:
#     # my_app.py
#     from examples.async_quickstart import make_fastapi_app
#     app = make_fastapi_app()
#     # uvicorn my_app:app


# ──────────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ──────────────────────────────────────────────────────────────────────────


async def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python examples/async_quickstart.py <path-to-file>", file=sys.stderr)
        return 1

    path = Path(argv[1])
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    result = await extract_one(path)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv)))
