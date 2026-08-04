# Contributing

Thank you for contributing to the HyperAPI Python SDK.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
ruff check hyperapi/
pytest tests/ -v
```

## Pull requests

- Base changes on `main` and keep each pull request focused.
- Add or update hermetic contract tests for behavior changes.
- Use public, user-facing commit and pull-request descriptions.
- Do not include credentials, customer data, private repository references,
  private infrastructure addresses, or internal issue identifiers.
- Update the README and changelog when the public API changes.

By submitting a contribution, you agree that it is licensed under the
repository's MIT License.
