"""Regenerate `node_fills_by_block_sample.lz4` from the JSONL next to it.

The fixture is committed as both the readable JSONL and the LZ4 frame the parser
actually reads, so a reviewer can see the shape without a decompressor and the test
still exercises the real file format.

    uv run python tests/fixtures/ingest/make_node_fills.py
"""

from __future__ import annotations

from pathlib import Path

import lz4.frame

HERE = Path(__file__).parent
SOURCE = HERE / "node_fills_by_block_sample.jsonl"
TARGET = HERE / "node_fills_by_block_sample.lz4"


def main() -> None:
    TARGET.write_bytes(lz4.frame.compress(SOURCE.read_bytes()))
    print(f"{TARGET} ({TARGET.stat().st_size} bytes) from {SOURCE}")


if __name__ == "__main__":
    main()
