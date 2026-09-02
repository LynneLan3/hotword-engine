"""Run one bounded Twitch Top Games raw observation."""

from __future__ import annotations

import argparse
from pathlib import Path

from opportunity_discovery.sources.twitch import TwitchTopGamesAdapter, append_twitch_run, write_twitch_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Twitch Helix Top Games observation")
    parser.add_argument("--requested-limit", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history-output", type=Path)
    args = parser.parse_args()
    artifact = TwitchTopGamesAdapter(requested_limit=args.requested_limit).collect()
    write_twitch_artifact(artifact, args.output)
    if args.history_output:
        append_twitch_run(args.history_output, artifact)
    print(f"artifact={args.output}")
    print(f"run_status={artifact['run_metadata']['run_status']}")
    print(f"rows_returned={artifact['run_metadata']['rows_returned']}")
    return 0 if artifact["run_metadata"]["run_status"] in {"COMPLETE", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
