"""Shadow-only Steam 1A/1B qualification for G002A."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from .games_popularity import GamesPopularityEnrichment, calculate_7d_growth, provider_counts
from .shadow import ShadowClassification, _Candidate, classify_candidate, build_steam_shadow_artifact
from .sources.steam import POPULAR_NEW_RELEASES, POPULAR_UPCOMING, SteamDiscoveryObservation, SteamShadowAdapter


ONE_A_PASS = "1A_PASS"
REJECT = "REJECT"
# Backward-compatible symbol for callers that used the implementation name.
ONE_A_REJECT = REJECT
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
DATA_ERROR = "DATA_ERROR"

TREND = "TREND"
EARLY = "EARLY"
CONTROL = "CONTROL"
LOW_PRIORITY = "LOW_PRIORITY"

RULES: dict[str, float] = {
    "UPCOMING_DAYS_MIN": 0,
    "UPCOMING_DAYS_MAX": 30,
    "UPCOMING_FOLLOWERS_MIN": 200,
    "UPCOMING_FOLLOWERS_MAX": 30000,
    "RELEASED_DAYS_MAX": 14,
    "RELEASED_FOLLOWERS_MIN": 200,
    "RELEASED_FOLLOWERS_MAX": 50000,
    "RELEASED_REVIEWS_MIN": 20,
    "RELEASED_REVIEWS_MAX": 2000,
    "RELEASED_RATING_MIN": 0.70,
    "TREND_GAIN_MIN": 1000,
    "TREND_GROWTH_MIN": 0.10,
    "EARLY_FOLLOWERS_MAX": 5000,
    "EARLY_GAIN_MIN": 600,
    "EARLY_GROWTH_MIN": 0.175,
    "CONTROL_FOLLOWERS_MIN": 30000,
    "CONTROL_FOLLOWERS_MAX": 60000,
    "CONTROL_GAIN_MIN": 1500,
    "CONTROL_GROWTH_MAX": 0.10,
    "CONTROL_MAX_PER_RUN": 3,
    "FOLLOWER_HISTORY_MIN_DAYS": 5,
}


@dataclass
class QualificationCandidate:
    app_id: str
    name: str
    observations: list[SteamDiscoveryObservation]
    first_page: int
    first_source: str
    cheap_reject: bool = False
    cheap_reject_reason: str = ""
    one_a: str = DATA_ERROR
    one_a_reason: str = ""
    one_b: str | None = None
    one_b_reason: str = ""
    current_followers: float | None = None
    baseline_followers: float | None = None
    gain_7d: float | None = None
    growth_rate: float | None = None
    coverage_days: float | None = None
    control_only: bool = False
    enrichment_status: str = "NOT_REQUESTED"
    enrichment: GamesPopularityEnrichment | None = None

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
            "sources": sorted({item.source for item in self.observations}),
            "pages": sorted({item.page for item in self.observations}),
            "first_source": self.first_source,
            "first_page": self.first_page,
            "release_date": self.release_date,
            "release_status": self.release_status,
            "review_count": self.review_count,
            "review_rating": self.review_rating,
            "enrichment_status": self.enrichment_status,
            "enrichment": self.enrichment.to_dict() if self.enrichment is not None else None,
            "current_followers": self.current_followers,
            "baseline_followers": self.baseline_followers,
            "gain_7d": self.gain_7d,
            "growth_rate": self.growth_rate,
            "coverage_days": self.coverage_days,
            "1A": self.one_a,
            "1A_reason": self.one_a_reason,
            "1B": self.one_b,
            "1B_reason": self.one_b_reason,
        }


def _observed_date(observed_at: str) -> date:
    try:
        return datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.now(timezone.utc).date()


def _date_from(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except (TypeError, ValueError):
        return None


def _first_incremental_locations(
    adapter: SteamShadowAdapter,
    observations: Sequence[SteamDiscoveryObservation],
) -> tuple[set[str], dict[str, tuple[int, str]]]:
    by_page: dict[tuple[str, int], set[str]] = defaultdict(set)
    for item in observations:
        by_page[(item.source, item.page)].add(item.steam_app_id)
    baseline_ids = {app_id for (source, page), ids in by_page.items() if page == 1 for app_id in ids}
    deep_ids = {app_id for (source, page), ids in by_page.items() if page >= 2 for app_id in ids}
    seen = set(baseline_ids)
    locations: dict[str, tuple[int, str]] = {}
    for page in range(max(2, adapter.page_start), adapter.page_end + 1):
        for source in adapter.sources:
            for app_id in sorted(by_page.get((source, page), set()) - seen):
                locations.setdefault(app_id, (page, source))
            seen.update(by_page.get((source, page), set()))
    return deep_ids - baseline_ids, locations


def _candidate_map(
    adapter: SteamShadowAdapter,
    observations: Sequence[SteamDiscoveryObservation],
) -> tuple[dict[str, QualificationCandidate], set[str], dict[str, tuple[int, str]]]:
    by_app: dict[str, QualificationCandidate] = {}
    for item in observations:
        candidate = by_app.setdefault(
            item.steam_app_id,
            QualificationCandidate(item.steam_app_id, item.game_name, [], item.page, item.source),
        )
        candidate.observations.append(item)
    incremental_ids, locations = _first_incremental_locations(adapter, observations)
    for app_id, (page, source) in locations.items():
        if app_id in by_app:
            by_app[app_id].first_page = page
            by_app[app_id].first_source = source
    return by_app, incremental_ids, locations


def _cheap_classification(candidate: QualificationCandidate) -> tuple[bool, str]:
    shadow_candidate = _Candidate(candidate.app_id, candidate.name, candidate.observations)
    classify_candidate(shadow_candidate)
    if shadow_candidate.classification == ShadowClassification.CHEAP_REJECT:
        return True, shadow_candidate.reason
    return False, ""


def _release_window(candidate: QualificationCandidate) -> tuple[str | None, float | None]:
    observed = _observed_date(candidate.observations[0].observed_at)
    release = _date_from(candidate.release_date)
    status = candidate.release_status
    if release is None or status not in {"UPCOMING", "RELEASED"}:
        return status, None
    if status == "UPCOMING":
        return status, float((release - observed).days)
    return status, float((release - observed).days)


def classify_one_a(candidate: QualificationCandidate) -> QualificationCandidate:
    """Apply the production 1A semantics without writing a production row."""

    if candidate.cheap_reject:
        candidate.one_a = ONE_A_REJECT
        candidate.one_a_reason = candidate.cheap_reject_reason
        return candidate
    if candidate.current_followers is None:
        candidate.one_a = DATA_ERROR
        candidate.one_a_reason = "Followers current value missing"
        return candidate
    status, days_to_release = _release_window(candidate)
    if days_to_release is None:
        candidate.one_a = DATA_ERROR
        candidate.one_a_reason = "exact release date or release stage missing"
        return candidate
    followers = candidate.current_followers
    if status == "UPCOMING":
        if not RULES["UPCOMING_DAYS_MIN"] <= days_to_release <= RULES["UPCOMING_DAYS_MAX"]:
            candidate.one_a = ONE_A_REJECT
            candidate.one_a_reason = f"upcoming release is {days_to_release:g} days from observation, outside 0-30"
            return candidate
        if RULES["UPCOMING_FOLLOWERS_MIN"] <= followers <= RULES["UPCOMING_FOLLOWERS_MAX"]:
            candidate.one_a = ONE_A_PASS
            candidate.one_a_reason = "upcoming primary follower window"
            return candidate
        if RULES["CONTROL_FOLLOWERS_MIN"] <= followers <= RULES["CONTROL_FOLLOWERS_MAX"]:
            candidate.one_a = ONE_A_PASS
            candidate.control_only = True
            candidate.one_a_reason = "upcoming control reserve"
            return candidate
        candidate.one_a = ONE_A_REJECT
        candidate.one_a_reason = "upcoming followers outside primary/control range"
        return candidate

    days_since = abs(days_to_release)
    reviews = candidate.review_count
    rating = candidate.review_rating
    if reviews is None:
        candidate.one_a = DATA_ERROR
        candidate.one_a_reason = "released review count missing"
        return candidate
    if days_since > RULES["RELEASED_DAYS_MAX"] or not RULES["RELEASED_REVIEWS_MIN"] <= reviews <= RULES["RELEASED_REVIEWS_MAX"]:
        candidate.one_a = ONE_A_REJECT
        candidate.one_a_reason = "released time/review window failed"
        return candidate
    if rating is None:
        candidate.one_a = DATA_ERROR
        candidate.one_a_reason = "released rating missing"
        return candidate
    if rating < RULES["RELEASED_RATING_MIN"]:
        candidate.one_a = ONE_A_REJECT
        candidate.one_a_reason = "released rating below 0.70"
        return candidate
    if RULES["RELEASED_FOLLOWERS_MIN"] <= followers <= RULES["RELEASED_FOLLOWERS_MAX"]:
        candidate.one_a = ONE_A_PASS
        candidate.one_a_reason = "released primary follower window"
        return candidate
    if RULES["CONTROL_FOLLOWERS_MIN"] <= followers <= RULES["CONTROL_FOLLOWERS_MAX"]:
        candidate.one_a = ONE_A_PASS
        candidate.control_only = True
        candidate.one_a_reason = "released control reserve"
        return candidate
    candidate.one_a = ONE_A_REJECT
    candidate.one_a_reason = "released followers outside primary/control range"
    return candidate


def classify_one_b(candidate: QualificationCandidate) -> QualificationCandidate:
    if candidate.one_a != ONE_A_PASS or candidate.gain_7d is None or candidate.growth_rate is None:
        return candidate
    followers = candidate.current_followers or 0
    gain = candidate.gain_7d
    growth = candidate.growth_rate
    if candidate.control_only:
        if (
            RULES["CONTROL_FOLLOWERS_MIN"] <= followers <= RULES["CONTROL_FOLLOWERS_MAX"]
            and gain >= RULES["CONTROL_GAIN_MIN"]
            and growth < RULES["CONTROL_GROWTH_MAX"]
        ):
            candidate.one_b = CONTROL
            candidate.one_b_reason = "control reserve trend shape"
        else:
            candidate.one_b = LOW_PRIORITY
            candidate.one_b_reason = "control reserve did not meet control shape"
        return candidate
    if gain >= RULES["TREND_GAIN_MIN"] and growth >= RULES["TREND_GROWTH_MIN"]:
        candidate.one_b = TREND
        candidate.one_b_reason = "7-day gain and growth rate met Trend thresholds"
    elif (
        followers <= RULES["EARLY_FOLLOWERS_MAX"]
        and gain >= RULES["EARLY_GAIN_MIN"]
        and growth >= RULES["EARLY_GROWTH_MIN"]
    ):
        candidate.one_b = EARLY
        candidate.one_b_reason = "follower count, 7-day gain, and growth rate met Early thresholds"
    elif (
        followers >= RULES["CONTROL_FOLLOWERS_MIN"]
        and gain >= RULES["CONTROL_GAIN_MIN"]
        and growth < RULES["CONTROL_GROWTH_MAX"]
    ):
        candidate.one_b = CONTROL
        candidate.one_b_reason = "follower count, 7-day gain, and growth rate met control thresholds"
    else:
        candidate.one_b = LOW_PRIORITY
        candidate.one_b_reason = "did not meet Trend, Early, or control thresholds"
    return candidate


def experiment_verdict(artifact: Mapping[str, Any]) -> tuple[str, str]:
    qualification = artifact["qualification"]
    if artifact.get("http_result") != "COMPLETE":
        return "INCONCLUSIVE", "Steam discovery HTTP coverage was not complete"
    attempted = int(qualification["enrichment_attempted"])
    missing_results = int(qualification.get("enrichment_missing_results", 0))
    failed = int(qualification["enrichment_failed"])
    missing = int(qualification["enrichment_missing"])
    high = int(qualification["qualified_high_priority"])
    if attempted == 0 or missing_results > 0 or failed > 0:
        return "INCONCLUSIVE", "Games Popularity qualification data was incomplete"
    if high == 0:
        if missing > 0:
            return "INCONCLUSIVE", "some incremental candidates lacked usable follower data"
        return "NO_MEANINGFUL_GAIN", "all enriched deep candidates failed to produce Trend or Early"
    if missing > 0:
        return "KEEP_SHADOW", "high-priority recall exists, but follower coverage is incomplete"
    pages = artifact["by_page"]
    later_pages = sum(
        int(pages.get(f"p{page}", {}).get("qualified_high_priority", 0))
        for page in (3, 4, 5)
    )
    if later_pages > 0:
        return "PROMOTE_DEEP_DISCOVERY", "qualified high-priority recall exists beyond page 2"
    return "LIMIT_TO_PAGE_2", "qualified high-priority recall was confined to page 2"


def qualify_steam_deep_candidates(
    adapter: SteamShadowAdapter,
    observations: Sequence[SteamDiscoveryObservation],
    popularity: Mapping[str, GamesPopularityEnrichment],
    *,
    provider_requested: int | None = None,
    run_id: str | None = None,
    provider_error: str | None = None,
) -> dict[str, Any]:
    """Qualify only pages 2+ incremental games and return JSON-safe evidence."""

    by_app, incremental_ids, locations = _candidate_map(adapter, observations)
    candidates = [by_app[app_id] for app_id in sorted(incremental_ids) if app_id in by_app]
    for candidate in candidates:
        candidate.cheap_reject, candidate.cheap_reject_reason = _cheap_classification(candidate)
        if candidate.cheap_reject:
            classify_one_a(candidate)

    enrichment_targets = [candidate for candidate in candidates if not candidate.cheap_reject]
    for candidate in enrichment_targets:
        result = popularity.get(candidate.app_id)
        if result is None:
            candidate.enrichment_status = "FAILED"
            candidate.one_a = DATA_ERROR
            candidate.one_a_reason = "provider result missing"
            continue
        candidate.enrichment_status = result.status
        candidate.enrichment = result
        candidate.current_followers = result.current_followers
        if result.status == "FAILED":
            candidate.one_a = DATA_ERROR
            candidate.one_a_reason = result.error or "Games Popularity request failed"
            continue
        classify_one_a(candidate)
        if candidate.one_a != ONE_A_PASS:
            continue
        observed_at = datetime.fromisoformat(candidate.observations[0].observed_at.replace("Z", "+00:00"))
        growth = calculate_7d_growth(
            result.history,
            candidate.current_followers,
            observed_at,
            RULES["FOLLOWER_HISTORY_MIN_DAYS"],
        )
        candidate.coverage_days = growth.get("coverage_days")
        if not growth.get("ok"):
            candidate.one_a = (
                INSUFFICIENT_HISTORY
                if "history" in str(growth.get("reason", "")).lower()
                else DATA_ERROR
            )
            candidate.one_a_reason = str(growth.get("reason", "follower history insufficient"))
            continue
        candidate.baseline_followers = growth["baseline_followers"]
        candidate.gain_7d = growth["gain"]
        candidate.growth_rate = growth["growth_rate"]
        classify_one_b(candidate)

    # Production keeps only the highest-Gain three control rows.
    controls = sorted(
        (c for c in candidates if c.one_b == CONTROL),
        key=lambda c: (-(c.gain_7d or 0), c.app_id),
    )
    for candidate in controls[int(RULES["CONTROL_MAX_PER_RUN"]):]:
        candidate.one_b = LOW_PRIORITY
        candidate.one_b_reason = "control reserve per-run cap"

    counts_1a = {ONE_A_PASS: 0, ONE_A_REJECT: 0, INSUFFICIENT_HISTORY: 0, DATA_ERROR: 0}
    counts_1b = {TREND: 0, EARLY: 0, CONTROL: 0, LOW_PRIORITY: 0}
    for candidate in candidates:
        counts_1a[candidate.one_a] = counts_1a.get(candidate.one_a, 0) + 1
        if candidate.one_b:
            counts_1b[candidate.one_b] = counts_1b.get(candidate.one_b, 0) + 1

    target_ids = {candidate.app_id for candidate in enrichment_targets}
    returned_ids = target_ids.intersection(popularity)

    def aggregate(items: list[QualificationCandidate]) -> dict[str, int]:
        return {
            "incremental": len(items),
            "1A_pass": sum(item.one_a == ONE_A_PASS for item in items),
            "1A_reject": sum(item.one_a == REJECT for item in items),
            "insufficient_history": sum(item.one_a == INSUFFICIENT_HISTORY for item in items),
            "data_error": sum(item.one_a == DATA_ERROR for item in items),
            "trend": sum(item.one_b == TREND for item in items),
            "early": sum(item.one_b == EARLY for item in items),
            "control": sum(item.one_b == CONTROL for item in items),
            "low_priority": sum(item.one_b == LOW_PRIORITY for item in items),
            "qualified_high_priority": sum(item.one_b in {TREND, EARLY} for item in items),
            "enriched": sum(item.enrichment_status == "AVAILABLE" for item in items),
            "enrichment_requested": sum(item.app_id in target_ids for item in items),
            "enrichment_returned": sum(item.app_id in returned_ids for item in items),
        }

    by_page = {f"p{page}": aggregate([c for c in candidates if c.first_page == page]) for page in range(2, adapter.page_end + 1)}
    by_source = {
        source: aggregate([c for c in candidates if c.first_source == source])
        for source in (POPULAR_UPCOMING, POPULAR_NEW_RELEASES)
    }
    counts = provider_counts(popularity)
    attempted = provider_requested if provider_requested is not None else len(enrichment_targets)
    counts["requested"] = attempted
    base_artifact = build_steam_shadow_artifact(adapter, observations, sample_limit=0, run_id=run_id)
    artifact: dict[str, Any] = {
        **base_artifact,
        "qualification": {
            "incremental_total": len(candidates),
            "cheap_reject_before_enrichment": sum(c.cheap_reject for c in candidates),
            "enrichment_attempted": attempted,
            "enrichment_requested": len(enrichment_targets),
            "enrichment_returned": len(returned_ids),
            "enrichment_missing_results": len(target_ids - returned_ids),
            "enrichment_available": counts["available"],
            "enrichment_missing": counts["missing"],
            "enrichment_failed": counts["failed"],
            "one_a": {"pass": counts_1a[ONE_A_PASS], "reject": counts_1a[ONE_A_REJECT], "insufficient_history": counts_1a[INSUFFICIENT_HISTORY], "data_error": counts_1a[DATA_ERROR]},
            "one_b": {"trend": counts_1b[TREND], "early": counts_1b[EARLY], "control": counts_1b[CONTROL], "low_priority": counts_1b[LOW_PRIORITY]},
            "qualified_high_priority": counts_1b[TREND] + counts_1b[EARLY],
            "games_popularity": counts,
            "provider_error": provider_error,
        },
        "by_page": by_page,
        "by_source": by_source,
        "candidate_results": [candidate.to_dict() for candidate in candidates],
    }
    verdict, reason = experiment_verdict(artifact)
    artifact["experiment_verdict"] = {"verdict": verdict, "reason": reason}
    return artifact
