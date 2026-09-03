#!/usr/bin/env python3
"""CLI wrapper: run pending PLAYER_ALIAS_DISCOVERY jobs via executor."""

from __future__ import annotations

import argparse
import os
import sys

import player_alias_discovery_executor as executor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run pending player alias discovery jobs")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_jobs is not None:
        os.environ["PLAYER_ALIAS_DISCOVERY_MAX_JOBS"] = str(args.max_jobs)
    summary = executor.run_batch(max_jobs=args.max_jobs, dry_run=bool(args.dry_run))
    if summary.get("stopped_early"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
