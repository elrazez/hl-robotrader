"""Regenerate the committed `.lz4` fixtures from the JSONL files next to them.

Each fixture is committed as both the readable JSONL and the LZ4 frame the parser
actually reads, so a reviewer can see the shape without a decompressor and the test
still exercises the real file format. A test asserts the two stay in step.

    uv run python tests/fixtures/ingest/make_node_fills.py
"""

from __future__ import annotations

from pathlib import Path

import lz4.frame

HERE = Path(__file__).parent


def main() -> None:
    for source in sorted(HERE.glob("*.jsonl")):
        target = source.with_suffix(".lz4")
        target.write_bytes(lz4.frame.compress(source.read_bytes()))
        print(f"{target.name} ({target.stat().st_size} bytes) from {source.name}")


if __name__ == "__main__":
    main()
