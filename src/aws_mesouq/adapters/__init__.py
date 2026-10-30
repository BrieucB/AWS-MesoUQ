"""Immutable bundled benchmark adapters for current and legacy execution contexts."""
from __future__ import annotations

import hashlib
from pathlib import Path

LEGACY_BENCHMARK_ADAPTER_SHA256 = "4740f1d4af27e754ba3d4fe4f80d5309e9e2ce1bc9bd70945ef10ae06bcf75e9"


def adapter_snapshot(sha256):
    """Return exact bundled bytes; a registry never supplies executable source."""
    root = Path(__file__).parent
    for name in ("benchmark.py", "benchmark_legacy.py"):
        blob = (root / name).read_bytes()
        if hashlib.sha256(blob).hexdigest() == sha256:
            if name == "benchmark_legacy.py" and sha256 != LEGACY_BENCHMARK_ADAPTER_SHA256:
                raise ValueError("Bundled legacy benchmark adapter differs from its immutable pin")
            return blob
    raise ValueError("Execution context requires an unsupported bundled benchmark adapter")
