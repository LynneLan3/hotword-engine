"""Bounded raw observations from Twitch Helix's Top Games endpoint.

This source is intentionally independent from candidate qualification,
research, scoring, and production runners.  A run keeps every returned row,
including duplicate game IDs and rows without an IGDB ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TWITCH_SOURCE = "TWITCH_HELIX_TOP_GAMES"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_TOP_GAMES_URL = "https://api.twitch.tv/helix/games/top"
TWITCH_PAGE_SIZE = 100
TWITCH_MAX_PAGES = 3
TWITCH_MAX_LIMIT = TWITCH_PAGE_SIZE * TWITCH_MAX_PAGES


class TwitchConfigurationError(RuntimeError):
    """A required credential is absent or Twitch rejected authentication."""


@dataclass(frozen=True)
class TwitchRawObservation:
    run_id: str
    observed_at: str
    source: str
    global_rank: int
    api_page: int
    page_rank: int
    twitch_game_id: str
    name: str
    igdb_id: str | None
    box_art_url: str
    raw_status: str = "OBSERVED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "global_rank": self.global_rank,
            "api_page": self.api_page,
            "page_rank": self.page_rank,
            "twitch_game_id": self.twitch_game_id,
            "name": self.name,
            "igdb_id": self.igdb_id,
            "box_art_url": self.box_art_url,
            "raw_status": self.raw_status,
        }


@dataclass(frozen=True)
class TwitchHttpResponse:
    status: int
    body: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get(url: str, headers: Mapping[str, str], timeout_seconds: float) -> TwitchHttpResponse:
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        return TwitchHttpResponse(response.status, response.read().decode("utf-8"))


def _token_request(client_id: str, client_secret: str, timeout_seconds: float) -> TwitchHttpResponse:
    body = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("ascii")
    request = Request(
        TWITCH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return TwitchHttpResponse(response.status, response.read().decode("utf-8"))
    except Exception as exc:
        status = getattr(exc, "code", None)
        return TwitchHttpResponse(int(status) if isinstance(status, int) else 0, "")


def _safe_error(page: int, status: int | None, code: str) -> dict[str, Any]:
    result: dict[str, Any] = {"api_page": page, "code": code}
    if status is not None:
        result["http_status"] = status
    return result


class TwitchTopGamesAdapter:
    """Collect at most 300 Twitch Top Games rows over at most three pages."""

    source_name = TWITCH_SOURCE

    def __init__(
        self,
        requested_limit: int = TWITCH_MAX_LIMIT,
        *,
        run_id: str | None = None,
        observed_at: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout_seconds: float = 20.0,
        token_fetcher: Callable[[str, str, float], TwitchHttpResponse] = _token_request,
        api_fetcher: Callable[[str, Mapping[str, str], float], TwitchHttpResponse] = _http_get,
    ) -> None:
        if not isinstance(requested_limit, int) or not 1 <= requested_limit <= TWITCH_MAX_LIMIT:
            raise ValueError(f"requested_limit must be between 1 and {TWITCH_MAX_LIMIT}")
        self.requested_limit = requested_limit
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.observed_at = observed_at or _now()
        self.client_id = client_id if client_id is not None else os.environ.get("TWITCH_CLIENT_ID", "").strip()
        self.client_secret = client_secret if client_secret is not None else os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        self.timeout_seconds = timeout_seconds
        self.token_fetcher = token_fetcher
        self.api_fetcher = api_fetcher

    def _get_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise TwitchConfigurationError("AUTH_MISSING")
        response = self.token_fetcher(self.client_id, self.client_secret, self.timeout_seconds)
        if response.status < 200 or response.status >= 300:
            raise TwitchConfigurationError("AUTH_FAILED")
        try:
            token = json.loads(response.body).get("access_token")
        except (TypeError, json.JSONDecodeError):
            token = None
        if not isinstance(token, str) or not token.strip():
            raise TwitchConfigurationError("AUTH_FAILED")
        return token.strip()

    def collect(self) -> dict[str, Any]:
        observations: list[TwitchRawObservation] = []
        source_errors: list[dict[str, Any]] = []
        pages_completed = 0
        cursor: str | None = None
        rank_offset = 0
        try:
            token = self._get_token()
        except TwitchConfigurationError as exc:
            return self._artifact(observations, pages_completed, source_errors, str(exc))

        for api_page in range(1, TWITCH_MAX_PAGES + 1):
            remaining = self.requested_limit - len(observations)
            if remaining <= 0:
                break
            query: dict[str, str] = {"first": str(min(TWITCH_PAGE_SIZE, remaining))}
            if cursor:
                query["after"] = cursor
            url = f"{TWITCH_TOP_GAMES_URL}?{urlencode(query)}"
            try:
                response = self.api_fetcher(
                    url,
                    {"Authorization": f"Bearer {token}", "Client-Id": self.client_id},
                    self.timeout_seconds,
                )
                if response.status < 200 or response.status >= 300:
                    source_errors.append(_safe_error(api_page, response.status, "HTTP_ERROR"))
                    break
                payload = json.loads(response.body)
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise ValueError("data is not an array")
                for page_rank, row in enumerate(rows, start=1):
                    if not isinstance(row, Mapping):
                        raise ValueError("data row is not an object")
                    twitch_game_id = str(row.get("id") or "").strip()
                    name = str(row.get("name") or "").strip()
                    box_art_url = str(row.get("box_art_url") or "").strip()
                    if not twitch_game_id or not name or not box_art_url:
                        raise ValueError("required Twitch game field is missing")
                    igdb = row.get("igdb_id")
                    igdb_id = str(igdb).strip() if igdb not in (None, "") else None
                    global_rank = rank_offset + page_rank
                    observations.append(TwitchRawObservation(
                        self.run_id, self.observed_at, self.source_name, global_rank,
                        api_page, page_rank, twitch_game_id, name, igdb_id, box_art_url,
                    ))
                pages_completed += 1
                rank_offset += min(TWITCH_PAGE_SIZE, remaining)
                pagination = payload.get("pagination") or {}
                cursor = pagination.get("cursor") if isinstance(pagination, Mapping) else None
                if not cursor or len(observations) >= self.requested_limit or not rows:
                    break
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                source_errors.append(_safe_error(api_page, response.status if "response" in locals() else None, "HTTP_ERROR"))
                break
            except Exception:
                source_errors.append(_safe_error(api_page, None, "HTTP_ERROR"))
                break

        status = "COMPLETE" if not source_errors else ("PARTIAL" if observations else "HTTP_ERROR")
        return self._artifact(observations, pages_completed, source_errors, status)

    def _artifact(
        self,
        observations: list[TwitchRawObservation],
        pages_completed: int,
        source_errors: list[dict[str, Any]],
        run_status: str,
    ) -> dict[str, Any]:
        ids = [item.twitch_game_id for item in observations]
        unique_ids = set(ids)
        return {
            "run_id": self.run_id,
            "observations": [item.to_dict() for item in observations],
            "run_metadata": {
                "requested_limit": self.requested_limit,
                "pages_completed": pages_completed,
                "rows_returned": len(observations),
                "unique_twitch_ids": len(unique_ids),
                "duplicates": len(ids) - len(unique_ids),
                "missing_igdb_count": sum(item.igdb_id is None for item in observations),
                "source_errors": source_errors,
                "run_status": run_status,
            },
        }


def append_twitch_run(path: str | Path, artifact: Mapping[str, Any]) -> None:
    """Append one immutable run to a JSON history ledger."""

    target = Path(path)
    if target.exists():
        history = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
            raise ValueError("Twitch history artifact must contain a runs array")
    else:
        history = {"runs": []}
    history["runs"].append(dict(artifact))
    target.write_text(json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_twitch_artifact(artifact: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(dict(artifact), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
