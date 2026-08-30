"""24/7 market data recorder: WebSocket feeds in, canonical Parquet out.

Runs as its own process (`liqfade record`), independent of any trading. See
docs/DATA_SOURCES.md for the schemas and docs/ROADMAP.md Phase 1 for what done means.
"""

from liqfade.recorder.main import Recorder

__all__ = ["Recorder"]
