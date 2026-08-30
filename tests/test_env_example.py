"""Every LIQFADE_ variable the code reads must be documented in .env.example.

Deliverable 11 of the Phase 0 prompt: .env.example is kept in sync with the code. This
test is the thing that keeps it that way, because nobody remembers to.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
# Variables that are documented but read only inside a YAML default or a shell script,
# and variables that exist only to be deliberately absent.
NAME_RE = re.compile(r'"(LIQFADE_[A-Z0-9_]+)"')


def _read_env_vars_from_code() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "liqfade").rglob("*.py"):
        names |= set(NAME_RE.findall(path.read_text()))
    return names


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text()
    # Both `NAME=` and `# NAME=` count as documented.
    return set(re.findall(r"^#?\s*(LIQFADE_[A-Z0-9_]+)=", text, flags=re.MULTILINE))


def test_env_example_documents_every_variable_the_code_reads() -> None:
    missing = _read_env_vars_from_code() - _documented()
    assert not missing, f"add these to .env.example: {sorted(missing)}"


def test_env_example_never_contains_a_filled_in_secret() -> None:
    """Guards against someone editing the example with a real value and committing it."""
    secret_like = ("PRIVATE_KEY", "BOT_TOKEN", "API_KEY", "SECRET_ACCESS_KEY", "PASSWORD_HASH")
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if any(marker in name for marker in secret_like):
            assert value.strip() == "", f"{name} has a value in .env.example"
