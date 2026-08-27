"""Shadow-only aggregation and cheap classification for discovery recall."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping

from .sources.steam import (
    POPULAR_NEW_RELEASES,
    POPULAR_UPCOMING,
    SteamDiscoveryObservation,
    SteamPageResult,
    SteamShadowAdapter,
)


class ShadowClassification(str, Enum):
    ELIGIBLE_FOR_HISTORY = "ELIGIBLE_FOR_HISTORY"
    NEEDS_HISTORY = "NEEDS_HISTORY"
    CHEAP_REJECT = "CHEAP_REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass
class _Candidate:
    app_id: str
    name: str
    observations: list[SteamDiscoveryObservation]
    history: Mapping[str, Any] | None = None
    classification: ShadowClassification = ShadowClassification.UNKNOWN
    reason: str = ""

    @property
    def release_date(self) -> str | None:
        return next((item.release_date for item in self.observations if item.release_date), None)

    @property
    def release_status(self) -> str | None:
        return next((item.release_status for item in self.observations if item.release_status), None)

    @property
    def review_count(self) -> int | None:
        return next((item.review_count for item in self.observations if item.review_count is not None), None)

    @property
    def review_rating(self) -> float | None:
        return next((item.review_rating for item in self.observations if item.review_rating is not None), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steam_app_id": self.app_id,
            "game_name": self.name,
            "classification": self.classification.value,
            "reason": self.reason,
            "sources": sorted({item.source for item in self.observations}),
            "pages": sorted({item.page for item in self.observations}),
            "observations": [item.to_dict() for item in self.observations],
        }


def _date_from(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _observed_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def classify_candidate(candidate: _Candidate, *, history_min_days: int = 5) -> _Candidate:
    """Apply only cheap Steam facts; missing history never becomes rejection."""

    observed = _observed_date(candidate.observations[0].observed_at)
    release_date = _date_from(candidate.release_date)
    status = candidate.release_status
    reviews = candidate.review_count
    rating = candidate.review_rating

    if release_date and status == "UPCOMING":
        days_until = (release_date - observed).days
        if days_until < 0 or days_until > 30:
            candidate.classification = ShadowClassification.CHEAP_REJECT
            candidate.reason = f"upcoming release is {days_until} days from observation, outside 0-30"
            return candidate
    if release_date and status == "RELEASED":
        days_since = (observed - release_date).days
        if days_since > 14:
            candidate.classification = ShadowClassification.CHEAP_REJECT
            candidate.reason = f"released {days_since} days ago, beyond 14-day window"
            return candidate
    if status == "RELEASED" and reviews is not None:
        if reviews < 20 or reviews > 2000:
            candidate.classification = ShadowClassification.CHEAP_REJECT
            candidate.reason = f"review count {reviews} outside 20-2000"
            return candidate
        if rating is not None and rating < 0.70:
            candidate.classification = ShadowClassification.CHEAP_REJECT
            candidate.reason = f"review rating {rating:.2f} below 0.70"
            return candidate

    history_days = None if not candidate.history else candidate.history.get("days")
    try:
        has_history = history_days is not None and float(history_days) >= history_min_days
    except (TypeError, ValueError):
        has_history = False
    if has_history:
        candidate.classification = ShadowClassification.ELIGIBLE_FOR_HISTORY
        candidate.reason = f"follower history covers at least {history_min_days} days"
    elif candidate.history is not None and history_days is not None:
        candidate.classification = ShadowClassification.NEEDS_HISTORY
        candidate.reason = f"follower history covers less than {history_min_days} days"
    elif status in {"UPCOMING", "RELEASED"}:
        candidate.classification = ShadowClassification.NEEDS_HISTORY
        candidate.reason = f"no follower history covering {history_min_days} days"
    else:
        candidate.classification = ShadowClassification.UNKNOWN
        candidate.reason = "release status is unavailable"
    return candidate


def _page_key(source: str, page: int) -> str:
    return f"{source} page {page}"


def run_steam_shadow(
    adapter: SteamShadowAdapter,
    *,
    history_by_app_id: Mapping[str, Mapping[str, Any]] | None = None,
    sample_limit: int = 20,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Collect, normalize, deduplicate, classify, and return JSON-safe evidence."""

    observations = adapter.collect()
    return build_steam_shadow_artifact(
        adapter,
        observations,
        history_by_app_id=history_by_app_id,
        sample_limit=sample_limit,
        run_id=run_id,
    )


def build_steam_shadow_artifact(
    adapter: SteamShadowAdapter,
    observations: list[SteamDiscoveryObservation],
    *,
    history_by_app_id: Mapping[str, Mapping[str, Any]] | None = None,
    sample_limit: int = 20,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build the G002 artifact from an already-collected page set."""

    by_app: dict[str, _Candidate] = {}
    observations_by_page: dict[tuple[str, int], list[SteamDiscoveryObservation]] = defaultdict(list)
    for observation in observations:
        observations_by_page[(observation.source, observation.page)].append(observation)
        candidate = by_app.setdefault(observation.steam_app_id, _Candidate(observation.steam_app_id, observation.game_name, []))
        candidate.observations.append(observation)
    history = history_by_app_id or {}
    for candidate in by_app.values():
        candidate.history = history.get(candidate.app_id)
        classify_candidate(candidate)

    baseline_ids = {
        app_id
        for (source, page), page_observations in observations_by_page.items()
        if page == 1
        for app_id in (item.steam_app_id for item in page_observations)
    }
    deep_ids = {
        app_id
        for (source, page), page_observations in observations_by_page.items()
        if page >= 2
        for app_id in (item.steam_app_id for item in page_observations)
    }
    incremental_ids = deep_ids - baseline_ids
    incremental_by_page: dict[str, int] = {}
    incremental_by_source_page: dict[str, int] = {}
    first_incremental_location: dict[str, str] = {}
    seen = set(baseline_ids)
    for page in range(max(2, adapter.page_start), adapter.page_end + 1):
        new_ids: set[str] = set()
        for source in adapter.sources:
            page_ids = {item.steam_app_id for item in observations_by_page.get((source, page), [])}
            source_new_ids = page_ids - seen
            new_ids.update(source_new_ids)
            incremental_by_source_page[_page_key(source, page)] = len(source_new_ids)
            for app_id in sorted(source_new_ids):
                first_incremental_location.setdefault(app_id, _page_key(source, page))
            seen.update(page_ids)
        incremental_by_page[f"p{page}"] = len(new_ids)

    incremental_candidates = [by_app[app_id] for app_id in sorted(incremental_ids) if app_id in by_app]
    classification_counts = {classification.value: 0 for classification in ShadowClassification}
    for candidate in incremental_candidates:
        classification_counts[candidate.classification.value] += 1
    potential = [candidate for candidate in incremental_candidates if candidate.classification != ShadowClassification.CHEAP_REJECT]
    classification_priority = {
        ShadowClassification.ELIGIBLE_FOR_HISTORY: 0,
        ShadowClassification.NEEDS_HISTORY: 1,
        ShadowClassification.UNKNOWN: 2,
        ShadowClassification.CHEAP_REJECT: 3,
    }
    sample_candidates = sorted(
        incremental_candidates,
        key=lambda candidate: (
            classification_priority[candidate.classification],
            first_incremental_location.get(candidate.app_id, ""),
            candidate.app_id,
        ),
    )

    page_results = [result.to_dict() for result in adapter.page_results]
    requested_pages = len(adapter.sources) * (adapter.page_end - adapter.page_start + 1)
    successful_pages = sum(result["status"] == "PASS" for result in page_results)
    if successful_pages == 0:
        http_result = "FAILED"
    elif successful_pages < requested_pages:
        http_result = "PARTIAL"
    else:
        http_result = "COMPLETE"
    source_status = {
        source: "PASS"
        if len([result for result in page_results if result["source"] == source]) == (adapter.page_end - adapter.page_start + 1)
        and all(result["status"] == "PASS" for result in page_results if result["source"] == source)
        else "FAIL"
        for source in adapter.sources
    }
    normalized_records = [adapter.normalize(observation.__dict__) for observation in observations]
    entity_ids = {entity.game_entity_id for records in normalized_records for entity in records.game_entities}
    listing_ids = {listing.platform_listing_id for records in normalized_records for listing in records.platform_listings}
    event_ids = {event.event_id for records in normalized_records for event in records.events}
    signal_ids = {signal.signal_id for records in normalized_records for signal in records.signals}
    return {
        "run_id": run_id or f"steam-shadow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "observed_at": adapter.observed_at,
        "sources": list(adapter.sources),
        "source_status": source_status,
        "page_range": {"start": adapter.page_start, "end": adapter.page_end},
        "http_result": http_result,
        "http": {"requested_pages": requested_pages, "successful_pages": successful_pages, "pages": page_results},
        "baseline": {"unique_games": len(baseline_ids)},
        "deep": {
            "unique_games": len(deep_ids),
            "incremental_unique_games": len(incremental_ids),
            "incremental_by_page": incremental_by_page,
            "unique_by_source": {
                source: len({item.steam_app_id for (item_source, page), items in observations_by_page.items() if item_source == source and page >= 2 for item in items})
                for source in adapter.sources
            },
            "incremental_by_source": {
                source: len({item.steam_app_id for (item_source, page), items in observations_by_page.items() if item_source == source and page >= 2 for item in items} - baseline_ids)
                for source in adapter.sources
            },
            "incremental_by_source_page": incremental_by_source_page,
        },
        "classification": {
            "existing_history": classification_counts[ShadowClassification.ELIGIBLE_FOR_HISTORY.value],
            "needs_history": classification_counts[ShadowClassification.NEEDS_HISTORY.value],
            "cheap_reject": classification_counts[ShadowClassification.CHEAP_REJECT.value],
            "unknown": classification_counts[ShadowClassification.UNKNOWN.value],
            "potential": len(potential),
            "deep_incremental_total": len(incremental_candidates),
        },
        "per_source": {
            source: {
                "status": source_status[source],
                "pages": [result for result in page_results if result["source"] == source],
                "unique_games": len({item.steam_app_id for (item_source, _), items in observations_by_page.items() if item_source == source for item in items}),
                "deep_unique_games": len({item.steam_app_id for (item_source, page), items in observations_by_page.items() if item_source == source and page >= 2 for item in items}),
                "incremental_unique_games": len(({item.steam_app_id for (item_source, page), items in observations_by_page.items() if item_source == source and page >= 2 for item in items}) - baseline_ids),
            }
            for source in adapter.sources
        },
        "canonical_record_counts": {
            "game_entities": len(entity_ids),
            "platform_listings": len(listing_ids),
            "events": len(event_ids),
            "signals": len(signal_ids),
        },
        "incremental_candidate_sample": [candidate.to_dict() for candidate in sample_candidates[:sample_limit]],
    }


def write_shadow_artifact(artifact: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
