"""Run the bounded G002 Steam discovery shadow experiment."""

from __future__ import annotations

import argparse
from pathlib import Path

from opportunity_discovery.shadow import run_steam_shadow, write_shadow_artifact
from opportunity_discovery.sources.steam import SteamShadowAdapter


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Steam pages 1-5 shadow discovery")
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--request-interval-seconds", type=float, default=1.8)
    args = parser.parse_args()
    adapter = SteamShadowAdapter(
        args.page_start,
        args.page_end,
        timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts,
        request_interval_seconds=args.request_interval_seconds,
    )
    artifact = run_steam_shadow(adapter)
    write_shadow_artifact(artifact, args.output)
    print(f"artifact={args.output}")
    print(f"http_result={artifact['http_result']}")
    print(f"successful_pages={artifact['http']['successful_pages']}/{artifact['http']['requested_pages']}")
    print(f"baseline_unique={artifact['baseline']['unique_games']}")
    print(f"deep_unique={artifact['deep']['unique_games']}")
    print(f"incremental_unique={artifact['deep']['incremental_unique_games']}")
    return 0 if artifact["http_result"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
