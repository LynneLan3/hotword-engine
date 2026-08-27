"""Bounded, environment-keyed access to the Games Popularity Steam API.

The production Apps Script uses one latest request and one follower-history
request per Steam App ID.  This client keeps that contract while making the
network boundary injectable for offline tests.  It never includes the API key
in returned data or error messages.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GAMES_POPULARITY_BASE_URL = "https://games-popularity.com/swagger/api"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_MAX_WORKERS = 4
DEFAULT_MIN_INTERVAL_SECONDS = 0.25
RETRYABLE_HTTP_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class FollowerPoint:
    followers: float
    added: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "followers": self.followers,
            "added": self.added.isoformat(),
        }


@dataclass(frozen=True)
class GamesPopularityEnrichment:
    app_id: str
    current_followers: float | None
    history: tuple[FollowerPoint, ...]
    latest_status: str
    history_status: str
    latest_http_status: int | None = None
    history_http_status: int | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        if self.latest_status == "FAILED" or self.history_status == "FAILED":
            return "FAILED"
        if self.latest_status != "AVAILABLE" or self.history_status != "AVAILABLE":
            return "MISSING"
        return "AVAILABLE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "status": self.status,
            "latest_status": self.latest_status,
            "history_status": self.history_status,
            "latest_http_status": self.latest_http_status,
            "history_http_status": self.history_http_status,
            "current_followers": self.current_followers,
            "history": [point.to_dict() for point in self.history],
            "error": self.error,
        }


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    body: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_latest_response(payload: Mapping[str, Any]) -> float | None:
    """Normalize the production ``/game/latest/{id}`` followers value."""

    followers = payload.get("followers")
    if isinstance(followers, Mapping):
        followers = followers.get("followers")
    return _number(followers)


def normalize_history_response(payload: Mapping[str, Any]) -> tuple[FollowerPoint, ...]:
    """Normalize the production ``/game/followers/{id}`` history points."""

    raw_history = payload.get("history")
    if isinstance(raw_history, Mapping):
        raw_history = raw_history.get("history")
    if not isinstance(raw_history, list):
        return ()

    points: list[FollowerPoint] = []
    for raw in raw_history:
        if not isinstance(raw, Mapping):
            continue
        followers = _number(raw.get("followers"))
        added = _parse_datetime(raw.get("added"))
        if followers is not None and added is not None:
            points.append(FollowerPoint(followers, added))
    return tuple(points)


def calculate_7d_growth(
    history: Iterable[FollowerPoint],
    current_followers: float | None,
    observed_at: datetime,
    min_history_days: float = 5,
) -> dict[str, Any]:
    """Match production's nearest-not-later-than-7-days baseline semantics."""

    if current_followers is None:
        return {"ok": False, "reason": "current followers missing"}
    points = sorted(history, key=lambda point: point.added, reverse=True)
    if not points:
        return {"ok": False, "reason": "no follower history"}

    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    target = observed_at.timestamp() - 7 * 86400
    baseline = next((point for point in points if point.added.timestamp() <= target), points[-1])
    coverage_days = (observed_at - baseline.added).total_seconds() / 86400
    if coverage_days < float(min_history_days):
        return {
            "ok": False,
            "reason": f"follower history covers about {coverage_days:.1f} days, below {min_history_days}",
            "coverage_days": coverage_days,
        }

    gain = float(current_followers) - baseline.followers
    growth_rate = gain / float(current_followers) if float(current_followers) > 0 else None
    if growth_rate is None:
        return {"ok": False, "reason": "growth rate cannot be calculated", "coverage_days": coverage_days}
    return {
        "ok": True,
        "baseline_followers": baseline.followers,
        "gain": gain,
        "growth_rate": growth_rate,
        "coverage_days": coverage_days,
    }


def _default_fetch(url: str, timeout_seconds: float) -> ProviderResponse:
    request = Request(url, headers={"User-Agent": "hotword-engine-steam-qualification/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return ProviderResponse(int(response.status), response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        return ProviderResponse(int(exc.code), exc.read().decode("utf-8", errors="replace"))
    except URLError as exc:
        raise TimeoutError(str(exc.reason)) from exc


class GamesPopularityClient:
    """Bounded client for the two production-compatible Steam endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = GAMES_POPULARITY_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        fetcher: Callable[[str, float], ProviderResponse] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(api_key or "").strip():
            raise ValueError("GAMES_POPULARITY_API_KEY is required")
        if timeout_seconds <= 0 or max_attempts < 1 or max_workers < 1 or min_interval_seconds < 0:
            raise ValueError("invalid Games Popularity request bounds")
        self._api_key = str(api_key).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_workers = max_workers
        self.min_interval_seconds = min_interval_seconds
        self._fetcher = fetcher or _default_fetch
        self._sleep = sleep_fn
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            if self._last_request_at is not None:
                remaining = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at = time.monotonic()

    def _request(self, endpoint: str, app_id: str) -> tuple[ProviderResponse | None, str | None]:
        url = f"{self.base_url}/{endpoint}/{app_id}?apiKey={self._api_key}"
        last_error = "request failed"
        for attempt in range(self.max_attempts):
            self._wait_for_rate_limit()
            try:
                response = self._fetcher(url, self.timeout_seconds)
                if response.status < 400:
                    return response, None
                last_error = f"HTTP {response.status}"
                if response.status not in RETRYABLE_HTTP_STATUSES:
                    return response, None
            except Exception as exc:  # bounded retry; message is never sent to output
                last_error = type(exc).__name__
            if attempt + 1 < self.max_attempts:
                self._sleep(min(4.0, 0.75 * (2**attempt)))
        return None, last_error

    def _fetch_one(self, app_id: str) -> GamesPopularityEnrichment:
        latest, latest_error = self._request("game/latest", app_id)
        history, history_error = self._request("game/followers", app_id)
        latest_status = "FAILED" if latest is None and latest_error else "MISSING"
        history_status = "FAILED" if history is None and history_error else "MISSING"
        latest_payload: Mapping[str, Any] | None = None
        history_payload: Mapping[str, Any] | None = None
        error_parts: list[str] = []

        if latest is not None and latest.status == 200:
            try:
                decoded = json.loads(latest.body)
                if isinstance(decoded, Mapping):
                    latest_payload = decoded
                    latest_status = "AVAILABLE" if normalize_latest_response(decoded) is not None else "MISSING"
                else:
                    latest_status = "FAILED"
                    error_parts.append("latest JSON shape")
            except (TypeError, ValueError):
                latest_status = "FAILED"
                error_parts.append("latest JSON")
        elif latest is not None and latest.status not in (404,):
            latest_status = "FAILED"
        if latest_error:
            error_parts.append(f"latest {latest_error}")

        if history is not None and history.status == 200:
            try:
                decoded = json.loads(history.body)
                if isinstance(decoded, Mapping):
                    history_payload = decoded
                    raw_history = decoded.get("history")
                    if isinstance(raw_history, Mapping):
                        raw_history = raw_history.get("history")
                    points = normalize_history_response(decoded)
                    if not isinstance(raw_history, list) or not raw_history:
                        history_status = "MISSING"
                    elif not points:
                        history_status = "FAILED"
                        error_parts.append("history points")
                    elif len(points) != len(raw_history):
                        history_status = "FAILED"
                        error_parts.append("history point shape")
                    else:
                        history_status = "AVAILABLE"
                else:
                    history_status = "FAILED"
                    error_parts.append("history JSON shape")
            except (TypeError, ValueError):
                history_status = "FAILED"
                error_parts.append("history JSON")
        elif history is not None and history.status not in (404,):
            history_status = "FAILED"
        if history_error:
            error_parts.append(f"history {history_error}")

        current = normalize_latest_response(latest_payload) if latest_payload else None
        points = normalize_history_response(history_payload) if history_payload else ()
        return GamesPopularityEnrichment(
            app_id=app_id,
            current_followers=current,
            history=points,
            latest_status=latest_status,
            history_status=history_status,
            latest_http_status=latest.status if latest else None,
            history_http_status=history.status if history else None,
            error="; ".join(error_parts) or None,
        )

    def fetch_many(self, app_ids: Iterable[str]) -> dict[str, GamesPopularityEnrichment]:
        ids = sorted({str(app_id) for app_id in app_ids if str(app_id).strip()})
        if not ids:
            return {}
        results: dict[str, GamesPopularityEnrichment] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._fetch_one, app_id): app_id for app_id in ids}
            for future in as_completed(futures):
                app_id = futures[future]
                try:
                    results[app_id] = future.result()
                except Exception as exc:  # preserve one-candidate isolation
                    results[app_id] = GamesPopularityEnrichment(
                        app_id=app_id,
                        current_followers=None,
                        history=(),
                        latest_status="FAILED",
                        history_status="FAILED",
                        error=type(exc).__name__,
                    )
        return results


def provider_counts(results: Mapping[str, GamesPopularityEnrichment]) -> dict[str, int]:
    counts = {"requested": len(results), "available": 0, "missing": 0, "failed": 0}
    for result in results.values():
        counts[result.status.lower()] += 1
    return counts
