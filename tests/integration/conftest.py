"""Everything under tests/integration/ needs testnet keys and network access.

These tests are marked `integration` automatically and are deselected by the default
addopts in pyproject.toml. Run them with `make test-int`.
"""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)
