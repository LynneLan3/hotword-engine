"""Post Twitch Historical Raw Ledger appends to the Apps Script web app.

Reuses STEAM_CANDIDATE_RESEARCH_API_URL / CALLBACK_TOKEN. The receiver must
route TWITCH_HISTORICAL_RAW_LEDGER_APPEND to the Twitch ledger writer and must
not write production 候选主表 rows.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from opportunity_discovery.twitch_historical_raw_ledger import TWITCH_LEDGER_JOB_TYPE

API_URL_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
CALLBACK_TOKEN_ENV = "STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"
CALLBACK_TIMEOUT_SEC = 90
PostFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPHandler())
    try:
        with opener.open(request, timeout=CALLBACK_TIMEOUT_SEC) as response:
            code = response.getcode() or 200
            raw = response.read()
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read() or b""
        headers = dict(exc.headers.items() if exc.headers else {})
    location = next(
        (value.strip() for key, value in headers.items() if key.lower() == "location" and value),
        "",
    )
    if location and code in {301, 302, 303, 307, 308}:
        try:
            redirect = urllib.request.Request(
                location,
                headers={"Accept": "application/json,text/plain,*/*"},
                method="GET",
            )
            with opener.open(redirect, timeout=CALLBACK_TIMEOUT_SEC) as response:
                code = response.getcode() or 200
                raw = response.read()
        except Exception as exc:  # noqa: BLE001 - surface redirect failure
            return {"ok": False, "error": f"callback_redirect_failed: {exc}"}
    if code < 200 or code >= 300:
        return {"ok": False, "error": f"callback_http_{code}"}
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid_callback_response"}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_callback_response"}


def post_twitch_historical_ledger(
    payload: Mapping[str, Any],
    *,
    post_fn: PostFn | None = None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    body = dict(payload)
    if _text(body.get("job_type")) != TWITCH_LEDGER_JOB_TYPE:
        return False, "invalid_job_type", None
    if post_fn is None:
        url = _text(os.environ.get(API_URL_ENV))
        token = _text(os.environ.get(CALLBACK_TOKEN_ENV))
        if not url:
            return False, f"{API_URL_ENV} is not set; refusing ledger write", None
        if not token:
            return False, f"{CALLBACK_TOKEN_ENV} is not set; refusing ledger write", None
        body["token"] = token
        try:
            response = _post(url, body)
        except (OSError, urllib.error.URLError) as exc:
            return False, str(exc)[:300], None
    else:
        url = _text(os.environ.get(API_URL_ENV)) or "https://example.invalid/twitch-ledger"
        body.setdefault("token", "test-token")
        try:
            response = post_fn(url, body)
        except (OSError, urllib.error.URLError) as exc:
            return False, str(exc)[:300], None
    if not isinstance(response, dict):
        return False, "invalid_callback_response", None
    if response.get("ok") is False:
        return False, _text(response.get("error")) or "callback_rejected", response
    return True, None, response
