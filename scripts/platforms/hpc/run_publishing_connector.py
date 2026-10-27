#!/usr/bin/env python3
"""Foreground own-account connector with separate SCRATCH/PROJECT storage."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from aws_mesouq.connector.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
