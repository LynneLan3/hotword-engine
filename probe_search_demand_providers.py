#!/usr/bin/env python3
"""R3B-0 live feasibility probe for Agefield Search sources.

Does not create SEARCH_DEMAND jobs, does not send callbacks, does not
touch Apps Script. Writes artifacts/search_provider_feasibility_agefield.json.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import search_demand_providers as sdp

ROOT = Path(__file__).resolve().parent
OUT_DEFAULT = ROOT / "artifacts" / "search_provider_feasibility_agefield.json"

GAME = "Agefield High: Rock the School"
ANCHOR_TOPIC = "classes"
SEEDS = [
    "Agefield High: Rock the School",
    "Agefield High: Rock the School classes",
]
AUTOCOMPLETE_PREFIX = "Agefield High: Rock the School clas"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(msg, flush=True)


def run_autocomplete_google() -> list[dict]:
    runs = []
    queries = [SEEDS[0], SEEDS[1], AUTOCOMPLETE_PREFIX]
    for q in queries:
        log(f"GOOGLE_AUTOCOMPLETE probe: {q}")
        runs.append(
            sdp.probe_google_autocomplete(q, seed_terms=SEEDS, anchor_topic=ANCHOR_TOPIC)
        )
        time.sleep(1.0)
    # Stability: repeat the class seed once.
    log("GOOGLE_AUTOCOMPLETE repeat for stability")
    first = sdp.probe_google_autocomplete(SEEDS[1], seed_terms=SEEDS, anchor_topic=ANCHOR_TOPIC)
    time.sleep(1.0)
    second = sdp.probe_google_autocomplete(SEEDS[1], seed_terms=SEEDS, anchor_topic=ANCHOR_TOPIC)
    a = [it["text"] for it in first.get("items") or []]
    b = [it["text"] for it in second.get("items") or []]
    stable = first.get("status") == sdp.STATUS_BEST_EFFORT and a == b
    second.setdefault("metadata", {})["repeat_stable"] = stable
    second["metadata"]["repeat_first_count"] = len(a)
    second["metadata"]["repeat_second_count"] = len(b)
    runs.append(second)
    return runs


def run_serp_source(source: str) -> list[dict]:
    fn = sdp.PROBES[source]
    runs = []
    # 1–2 real queries only.
    for q in SEEDS[:2]:
        log(f"{source} probe: {q}")
        runs.append(fn(q, seed_terms=SEEDS, anchor_topic=ANCHOR_TOPIC))
        time.sleep(1.2)
    return runs


def run_bing() -> list[dict]:
    runs = []
    for q in SEEDS[:2]:
        log(f"BING_AUTOCOMPLETE probe: {q}")
        runs.append(sdp.probe_bing_autocomplete(q, seed_terms=SEEDS, anchor_topic=ANCHOR_TOPIC))
        time.sleep(1.0)
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description="Agefield search provider feasibility probe")
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    args = parser.parse_args()

    all_runs: dict[str, list[dict]] = {
        sdp.SOURCE_GOOGLE_AUTOCOMPLETE: run_autocomplete_google(),
        sdp.SOURCE_GOOGLE_PAA: run_serp_source(sdp.SOURCE_GOOGLE_PAA),
        sdp.SOURCE_GOOGLE_RELATED: run_serp_source(sdp.SOURCE_GOOGLE_RELATED),
        sdp.SOURCE_BING_AUTOCOMPLETE: run_bing(),
    }

    summaries = {
        source: sdp.summarize_provider_runs(source, runs) for source, runs in all_runs.items()
    }
    recommended, excluded = sdp.recommend_r3b_sources(summaries)

    providers = {}
    for source, runs in all_runs.items():
        summary = summaries[source]
        providers[source] = {
            "status": summary["status"],
            "http_ok": summary["http_ok"],
            "evidence_count": summary["evidence_count"],
            "anchor_evidence_count": summary["anchor_evidence_count"],
            "notes": summary["notes"],
            "errors": summary["errors"],
            "repeat_stable": (runs[-1].get("metadata") or {}).get("repeat_stable")
            if source == sdp.SOURCE_GOOGLE_AUTOCOMPLETE
            else None,
            "sample_items": [
                {
                    "text": it.get("text"),
                    "kind": it.get("kind"),
                    "anchor_relevant": it.get("anchor_relevant"),
                }
                for it in summary["items"][:12]
            ],
        }

    paid_needed = [
        s
        for s in (sdp.SOURCE_GOOGLE_PAA, sdp.SOURCE_GOOGLE_RELATED)
        if s in excluded
    ]

    expressions = []
    seen = set()
    for source, summary in providers.items():
        for it in summary.get("sample_items") or []:
            text = str(it.get("text") or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            expressions.append(
                {
                    "text": text,
                    "source": source,
                    "anchor_relevant": bool(it.get("anchor_relevant")),
                }
            )

    report = {
        "game": GAME,
        "anchor_topic": ANCHOR_TOPIC,
        "seed_terms": SEEDS,
        "autocomplete_prefix": AUTOCOMPLETE_PREFIX,
        "providers": providers,
        "recommended_r3b_sources": recommended,
        "excluded_sources": excluded,
        "paid_serp_needed_for": paid_needed,
        "observed_search_expressions": expressions,
        "observed_at": now_iso(),
        "live_probe": True,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {out}")
    log("recommended_r3b_sources=" + ",".join(recommended) if recommended else "recommended_r3b_sources=")
    log("excluded_sources=" + ",".join(excluded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
