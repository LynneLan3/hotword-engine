#!/usr/bin/env python3
"""Apply an evidence-backed implementation/deployment patch to one receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import batch_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Advance one Batch Receipt lifecycle")
    parser.add_argument("--receipt", required=True, help="Path to batch_receipt.json")
    parser.add_argument("--patch", required=True, help="JSON patch containing the same batch_id")
    args = parser.parse_args()
    receipt_path = Path(args.receipt)
    patch_path = Path(args.patch)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    update: dict[str, Any] = json.loads(patch_path.read_text(encoding="utf-8"))
    updated = batch_receipt.advance_receipt(receipt, update)
    receipt_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(updated["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
