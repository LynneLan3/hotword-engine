"""Bounded Steam Store discovery for the G002 shadow experiment.

This module only reads public Steam search pages.  It deliberately does not
call follower providers, SearchApi, Sheets, or any production runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import unescape
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..adapters import CanonicalRecords
from ..schema import CandidateSignal, EventType, GameEntity, GameEvent, Platform, PlatformListing


POPULAR_UPCOMING = "Popular Upcoming"
POPULAR_NEW_RELEASES = "Popular New Releases"
STEAM_SOURCE_URLS = {
    POPULAR_UPCOMING: "https://store.steampowered.com/search/?filter=popularcomingsoon&os=win",
    POPULAR_NEW_RELEASES: "https://store.steampowered.com/search/?filter=popularnew&os=win&sort_by=Released_DESC",
}
STEAM_PAGE_SIZE = 50
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_REQUEST_INTERVAL_SECONDS = 1.8
RETRYABLE_HTTP_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class SteamDiscoveryObservation:
    """One raw, page-scoped Steam search observation."""

    source: str
    page: int
    rank_on_page: int
    observed_at: str
    steam_app_id: str
    game_name: str
    store_url: str
    release_date: str | None = None
    release_date_raw: str | None = None
    release_status: str | None = None
    review_count: int | None = None
    review_rating: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "page": self.page,
            "rank_on_page": self.rank_on_page,
            "observed_at": self.observed_at,
            "steam_app_id": self.steam_app_id,
            "game_name": self.game_name,
            "store_url": self.store_url,
            "release_date": self.release_date,
            "release_date_raw": self.release_date_raw,
            "release_status": self.release_status,
            "review_count": self.review_count,
            "review_rating": self.review_rating,
        }


@dataclass(frozen=True)
class SteamPageResult:
    source: str
    page: int
    status: str
    http_status: int | None = None
    item_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "page": self.page,
            "status": self.status,
            "http_status": self.http_status,
            "item_count": self.item_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: str


def build_steam_page_url(source: str, page: int) -> str:
    """Build a Steam search URL using Steam's 50-row ``start`` paging."""

    if source not in STEAM_SOURCE_URLS:
        raise ValueError(f"unsupported Steam source: {source}")
    if not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    return f"{STEAM_SOURCE_URLS[source]}&start={(page - 1) * STEAM_PAGE_SIZE}&count={STEAM_PAGE_SIZE}"


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(_strip_tags(value))).strip()


def _parse_release_date(value: str) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    for fmt in ("%Y-%m-%d", "%d %b, %Y", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _review_summary(row: str) -> tuple[int | None, float | None]:
    match = re.search(
        r"<span\b[^>]*class=[\"'][^\"']*\bsearch_review_summary\b[^\"']*[\"'][^>]*>",
        row,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    tooltip = re.search(r"\bdata-tooltip-html=[\"']([\s\S]*?)[\"']", match.group(0), re.IGNORECASE)
    if not tooltip:
        return None, None
    text = _clean_text(tooltip.group(1))
    summary = re.search(r"(\d{1,3})%\s+of\s+the\s+([\d,]+)\s+user reviews?", text, re.IGNORECASE)
    if not summary:
        return None, None
    return int(summary.group(2).replace(",", "")), int(summary.group(1)) / 100


def parse_steam_search_results(html: str, source: str, page: int, observed_at: str) -> list[SteamDiscoveryObservation]:
    """Parse search result cards while retaining page and rank provenance."""

    if not isinstance(html, str):
        raise TypeError("Steam search response must be text")
    rows = re.findall(
        r"<a\b[^>]*class=[\"'][^\"']*\bsearch_result_row\b[^\"']*[\"'][^>]*>[\s\S]*?</a>",
        html,
        re.IGNORECASE,
    )
    observations: list[SteamDiscoveryObservation] = []
    for row in rows:
        href_match = re.search(r"\bhref=[\"']([^\"']*?/app/(\d+)/?[^\"']*)[\"']", row, re.IGNORECASE)
        app_id_match = re.search(r"\bdata-ds-appid=[\"'](\d+)[\"']", row, re.IGNORECASE)
        title_match = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*\btitle\b[^\"']*[\"'][^>]*>([\s\S]*?)</span>",
            row,
            re.IGNORECASE,
        )
        if not href_match or not title_match:
            continue
        app_id = app_id_match.group(1) if app_id_match else href_match.group(2)
        game_name = _clean_text(title_match.group(1))
        if not app_id or not game_name:
            continue
        release_match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\bsearch_released\b[^\"']*[\"'][^>]*>([\s\S]*?)</div>",
            row,
            re.IGNORECASE,
        )
        release_raw = _clean_text(release_match.group(1)) if release_match else None
        review_count, review_rating = _review_summary(row)
        store_url = unescape(href_match.group(1)).split("?", 1)[0]
        observations.append(
            SteamDiscoveryObservation(
                source=source,
                page=page,
                rank_on_page=len(observations) + 1,
                observed_at=observed_at,
                steam_app_id=app_id,
                game_name=game_name,
                store_url=store_url,
                release_date=_parse_release_date(release_raw or ""),
                release_date_raw=release_raw,
                release_status="UPCOMING" if source == POPULAR_UPCOMING else "RELEASED",
                review_count=review_count,
                review_rating=review_rating,
            )
        )
    return observations


def _default_fetch(url: str, timeout_seconds: float) -> _HttpResponse:
    request = Request(url, headers={"User-Agent": "hotword-engine-steam-shadow/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return _HttpResponse(int(response.status), response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        return _HttpResponse(int(exc.code), exc.read().decode("utf-8", errors="replace"))
    except URLError as exc:
        raise TimeoutError(str(exc.reason)) from exc


class SteamShadowAdapter:
    """Collect two Steam sources over a bounded, explicit page range."""

    source_name = "STEAM_STORE_SHADOW"

    def __init__(
        self,
        page_start: int = 1,
        page_end: int = 5,
        *,
        observed_at: str | None = None,
        fetcher: Callable[[str, float], _HttpResponse] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
        sources: Iterable[str] = (POPULAR_UPCOMING, POPULAR_NEW_RELEASES),
    ) -> None:
        if not isinstance(page_start, int) or not isinstance(page_end, int) or page_start < 1 or page_end < page_start:
            raise ValueError("page range must be positive and page_end >= page_start")
        if max_attempts < 1 or timeout_seconds <= 0 or request_interval_seconds < 0:
            raise ValueError("HTTP bounds must be positive, with a non-negative interval")
        selected_sources = tuple(sources)
        if not selected_sources or any(source not in STEAM_SOURCE_URLS for source in selected_sources):
            raise ValueError("sources must contain supported Steam source names")
        self.page_start = page_start
        self.page_end = page_end
        self.sources = selected_sources
        self.observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        self._fetcher = fetcher or _default_fetch
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.request_interval_seconds = request_interval_seconds
        self.page_results: list[SteamPageResult] = []
        self._entity_name_by_app_id: dict[str, str] = {}
        self._last_request_at: float | None = None

    def collect(self) -> list[SteamDiscoveryObservation]:
        observations: list[SteamDiscoveryObservation] = []
        self.page_results = []
        for source in self.sources:
            for page in range(self.page_start, self.page_end + 1):
                try:
                    response = self._fetch_page(build_steam_page_url(source, page))
                    page_observations = parse_steam_search_results(response.body, source, page, self.observed_at)
                    observations.extend(page_observations)
                    self.page_results.append(SteamPageResult(source, page, "PASS", response.status, len(page_observations)))
                except Exception as exc:  # one page failure must not erase safe pages
                    status = getattr(exc, "status", None)
                    self.page_results.append(SteamPageResult(source, page, "FAIL", status, error=str(exc)))
        return observations

    def _fetch_page(self, url: str) -> _HttpResponse:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            if self._last_request_at is not None:
                wait_seconds = self.request_interval_seconds - (time.monotonic() - self._last_request_at)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()
            try:
                response = self._fetcher(url, self.timeout_seconds)
                if response.status < 400:
                    return response
                error = RuntimeError(f"HTTP {response.status}")
                setattr(error, "status", response.status)
                last_error = error
                if response.status not in RETRYABLE_HTTP_STATUSES:
                    raise error
            except Exception as exc:
                last_error = exc
                status = getattr(exc, "status", None)
                if status not in RETRYABLE_HTTP_STATUSES and not isinstance(exc, TimeoutError):
                    raise
            if attempt + 1 < self.max_attempts:
                time.sleep(min(4.0, 0.75 * (2**attempt)))
        raise last_error or RuntimeError("Steam request failed")

    def normalize(self, raw_record: Mapping[str, Any]) -> CanonicalRecords:
        """Normalize one observation and reuse AppID-based entity/listing IDs."""

        observation = SteamDiscoveryObservation(**dict(raw_record))
        app_id = observation.steam_app_id.strip()
        if not app_id.isdigit():
            raise ValueError("steam_app_id must be numeric")
        canonical_name = self._entity_name_by_app_id.setdefault(app_id, observation.game_name)
        entity = GameEntity.create(canonical_name)
        listing = PlatformListing.create(
            entity.game_entity_id,
            Platform.STEAM,
            app_id,
            store_url=observation.store_url,
            release_date=observation.release_date,
            listing_status=observation.release_status or "ACTIVE",
        )
        event = GameEvent.create(
            entity.game_entity_id,
            EventType.NEW_RELEASE,
            observation.release_date or observation.observed_at,
            self.source_name,
            f"steam-app:{app_id}",
            listing.platform_listing_id,
        )
        metadata = {
            "source": observation.source,
            "page": observation.page,
            "rank_on_page": observation.rank_on_page,
            "observed_at": observation.observed_at,
            "steam_app_id": app_id,
            "store_url": observation.store_url,
            "release_date_raw": observation.release_date_raw,
            "release_status": observation.release_status,
        }
        values: list[tuple[str, Any]] = [("STEAM_DISCOVERY_RANK", observation.rank_on_page)]
        if observation.release_date is not None:
            values.append(("STEAM_RELEASE_DATE", observation.release_date))
        if observation.release_status is not None:
            values.append(("STEAM_RELEASE_STATUS", observation.release_status))
        if observation.review_count is not None:
            values.append(("STEAM_REVIEW_COUNT", observation.review_count))
        if observation.review_rating is not None:
            values.append(("STEAM_REVIEW_RATING", observation.review_rating))
        signals = tuple(
            CandidateSignal.create(
                entity.game_entity_id,
                observation.observed_at,
                self.source_name,
                signal_type,
                raw_value,
                event_id=event.event_id,
                platform_listing_id=listing.platform_listing_id,
                normalized_value=raw_value if isinstance(raw_value, (int, float)) else None,
                metadata=metadata,
            )
            for signal_type, raw_value in values
        )
        return CanonicalRecords((entity,), (listing,), (event,), signals)
