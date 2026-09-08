# G040 P0 — Machine Signal & Cost Baseline

**Observed:** 2026-09-08
**Goal:** G040 — Human-Ready Today Action Automation V1
**Phase status:** PASS (baseline only; no behavior change)

This is the source-of-truth baseline for the provider/research side of G040.
It records the current chain and cost boundary before P1 changes the order of
research. It does not authorize paid calls, callback writes, Sheet writes,
deployment, or push.

## Current chain

1. `steam_hotword_monitor/SteamCandidateScanner.js` enqueues pending jobs and
   owns callback/writeback and Today Action refresh.
2. `fetch_pending_steam_candidate_research_jobs.py` supplies jobs to
   `steam_candidate_daily_executor.py`.
3. `steam_candidate_daily_executor.py` normalizes/deduplicates jobs, excludes
   existing sites when a live index is available, orders by `first_round_type`,
   and applies the current defaults of five total jobs and three fresh
   attempts per run.
4. `steam_candidate_machine_research_executor.py` reuses a preflight artifact
   when possible; otherwise it runs social collection and preflight. The
   current preflight includes autocomplete and an explicitly opted-in
   SearchApi organic SERP probe.
5. `steam_candidate_research_job_runner.py` builds the callback payload.
   `SteamCandidateScanner.js` writes the machine fields and refreshes Today
   Action.

Today Action is currently evaluated before the selected jobs finish. Pending
rows can therefore remain visible as machine-research work, and Trends is
still a human/manual field. These are G040 P1–P6 target gaps, not P0 changes.

## Machine signal classification

| Signal or field | Current owner/provider | Class | P0 finding |
| --- | --- | --- | --- |
| Steam identity, lifecycle, velocity, candidate state | Steam/API and existing candidate/master data | EXISTING | Reuse as the first ranking input. |
| Historical evidence and prior observations | Existing job artifacts, Raw Ledger, and history | EXISTING / CACHED | Reuse valid artifacts; do not re-fetch or overwrite successful evidence. |
| Games Popularity upstream signal | `opportunity_discovery/games_popularity.py` | CACHED / PAID capability | Existing cache-first contract is separate from G040's SearchApi gate. |
| Existing-site, control, 1A, excluded state | Existing-site index and canonical candidate state | EXISTING | Exclude before paid verification. |
| Alias/entity evidence already stored | Candidate state and alias artifacts | CACHED / EXISTING | Use it before any new alias work. |
| Alias discovery from Reddit, YouTube, Steam Community | Public endpoints in `player_alias_discovery_runner.py` | FREE | Safe fallback for missing aliases in P1. |
| Google Autocomplete | `suggestqueries.google.com` | FREE | Current preflight probes four seeds. |
| Bing Autocomplete | `api.bing.com/osjson.aspx` | FREE | Current preflight probes four seeds. |
| Google PAA / related searches | Direct Google best-effort functions | FREE | Available provider capability; not the current default preflight source list. |
| Reddit, YouTube, Steam Community social evidence | Public endpoints in `research_runner.py` and `game_wide_social_runner.py` | FREE | Current default social providers are Reddit, Steam, and YouTube. |
| Google Organic SERP competition | SearchApi with `SEARCHAPI_API_KEY` | PAID | Current preflight may issue up to three query probes per candidate; paid requests are single-attempt and cache reuse is same-day. |
| Google Trends result / relative strength | `steam_hotword_monitor/ExternalEvidence.gs` manual record/recalc | HUMAN_ONLY | No automatic Trends provider is present in `hotword-engine`; current machine output has no terminal Trends result. |
| Keyword opportunity | Local derivation from autocomplete | DERIVED | Current values can be 有 / 无 / 未检查. |
| Machine recommendation, confidence, reason | Local recommendation and callback payload | DERIVED | Not a final human BUILD/WATCH/REJECT decision. |
| Human decision and note | Today Action user input | HUMAN_ONLY | Remains the final decision boundary. |
| Callback / Sheet writeback | Existing Apps Script endpoint | EXISTING | Platform machinery, not an external research provider. |

## Current cost and ordering baseline

- `steam_candidate_daily_executor.py` currently uses `DEFAULT_MAX_TOTAL_JOBS =
  5` and `DEFAULT_PAID_ATTEMPT_BUDGET = 3`.
- Current ordering is a `first_round_type` priority plus stable queue order;
  it is not yet a preliminary opportunity score.
- Existing-site exclusion and persisted BUILD/REJECT/1A suppression happen
  before selection when the relevant state/index is available.
- `_selection()` counts a non-reused completed artifact as a fresh attempt,
  while same-day preflight cache reuse avoids a new provider request. This is
  an execution bound, not yet the unique candidate-level paid-verification
  boundary required by G040.
- Current preflight can call paid SearchApi SERP before a non-paid preliminary
  score exists. This is the principal ordering gap for P1–P4.
- Missing aliases can reach the conditional SearchApi path in
  `player_alias_discovery_runner.py`; P1 must establish a free/existing/cache
  fallback before any paid alias resolution.
- Trends is not automatically fetched or scored. The current UI can ask a
  human to inspect or enter it.

## G037 production baseline retained

G037 remains the baseline and is not reimplemented by G040: production run
`20260908-171411`, deployment version `74`, `rawUnique=250`,
`ledgerAppended=250`, `ledgerWriteFailures=0`, `eligible=8`, `enriched=8/8`,
with six bounded preflight statuses. The G037 receipt remains in the canonical
continuity repository and its identity, Raw Ledger, 1A/1B, WATCH/BUILD/REJECT,
and opportunity logic are out of scope for rewrite.

## P0 exit and P1 boundary

P0 is complete when this inventory is committed with focused offline
validation. P1 may now implement free-first machine research, but must not call
SearchApi or another paid search provider while establishing the non-paid
signal set. P0 itself made no external calls and changed no runtime behavior.
