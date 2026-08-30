"""Everything under tests/integration/ needs testnet keys and network access.

These tests are marked `integration` automatically and are deselected by the default
addopts in pyproject.toml. Run them with `make test-int`.
"""

from pathlib import Path

import pytest

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # This hook is called with the whole collection even though the conftest lives in a
    # subdirectory, so mark only the items that actually live under this directory.
    for item in items:
        if item.path is not None and HERE in item.path.parents:
            item.add_marker(pytest.mark.integration)
