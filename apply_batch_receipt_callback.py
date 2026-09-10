#!/usr/bin/env python3
"""Apply one machine-facing cross-repo receipt callback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import batch_receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--base-json", required=True, type=Path)
    parser.add_argument("--patch-json", required=True, type=Path)
    args = parser.parse_args()
    base = json.loads(args.base_json.read_text(encoding="utf-8"))
    patch = json.loads(args.patch_json.read_text(encoding="utf-8"))
    updated = batch_receipt.advance_receipt(base, patch)
    batch_receipt.write_receipt(args.receipt, updated)
    print(updated["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
