"""Small bounded retry helper for upstream HTTP requests."""

from __future__ import annotations

import socket
import time
import urllib.error
from collections.abc import Callable
from typing import Any

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)


class UpstreamRequestError(RuntimeError):
    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        attempts: int,
        status_code: int | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.attempts = attempts
        self.status_code = status_code
        super().__init__(detail)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc.reason).lower()
    return False


def _retryable_http_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def request_with_retry(
    request: Any,
    *,
    timeout: float,
    opener: Any,
    log: Callable[[str], None] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Run one bounded request; retry only timeouts and transient HTTP status codes."""
    open_fn = opener.open if not callable(opener) and hasattr(opener, "open") else opener
    last_reason = "UPSTREAM_TIMEOUT"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        status_code: int | None = None
        if log:
            log(f"attempt={attempt}/{MAX_ATTEMPTS}")
        try:
            with open_fn(request, timeout=timeout) as response:
                status_code = response.getcode() or 200
                headers = dict(response.headers.items())
                body = response.read()
            if status_code < 400:
                return status_code, headers, body
            last_reason = "UPSTREAM_HTTP_ERROR"
            if not _retryable_http_status(status_code):
                raise UpstreamRequestError(
                    last_reason,
                    f"HTTP {status_code}",
                    attempts=attempt,
                    status_code=status_code,
                )
            detail = f"HTTP {status_code}"
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            last_reason = "UPSTREAM_HTTP_ERROR"
            if not _retryable_http_status(status_code):
                raise UpstreamRequestError(
                    last_reason,
                    f"HTTP {status_code}",
                    attempts=attempt,
                    status_code=status_code,
                ) from exc
            detail = f"HTTP {status_code}"
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            last_reason = "UPSTREAM_TIMEOUT" if _is_timeout(exc) else "UPSTREAM_TIMEOUT"
            detail = exc.__class__.__name__

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS[attempt - 1])
        else:
            raise UpstreamRequestError(
                last_reason,
                detail,
                attempts=attempt,
                status_code=status_code,
            )

    raise AssertionError("unreachable")
