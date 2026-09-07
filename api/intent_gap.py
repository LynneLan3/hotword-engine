"""Minimal production SERP adapter for Launch Intent Coverage."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from search_demand_providers import probe_searchapi_google_organic


_STOP = {"the", "a", "an", "how", "to", "do", "does", "is", "are", "of", "in", "on", "for", "with", "can", "you"}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in _STOP and len(token) > 2}


def _candidate_result(item: Mapping[str, Any], task: str, local_urls: set[str]) -> dict[str, Any] | None:
    url = _text(item.get("url"))
    if not url or url in local_urls:
        return None
    position = item.get("position", 99)
    try:
        position = float(position)
    except (TypeError, ValueError):
        position = 99
    if position > 10:
        return None
    title = _text(item.get("title") or item.get("text"))
    snippet = _text(item.get("snippet"))
    task_tokens = _tokens(task)
    result_tokens = _tokens(f"{title} {snippet}")
    if task_tokens and not (task_tokens & result_tokens):
        return None
    return {"url": url, "title": title, "snippet": snippet, "position": position}


def build_intent_gap_response(payload: Mapping[str, Any], *, fetch_fn: Any = None, now: datetime | None = None) -> dict[str, Any]:
    game = _text(payload.get("game") or payload.get("site") or payload.get("gameName"))
    lifecycle = _text(payload.get("lifecycle") or payload.get("lifecyclePhase"))
    local_urls = {_text(item) for item in _list(payload.get("localOwnedUrls") or payload.get("local_owned_urls")) if _text(item)}
    local_tasks = {_text(item).casefold() for item in _list(payload.get("playerTasks") or payload.get("player_tasks")) if _text(item)}
    intents = [item for item in _list(payload.get("intents")) if isinstance(item, Mapping)]
    if not intents:
        intents = [{"clusterKey": _text(payload.get("intentKey") or game), "playerTask": game, "queries": [game]}]
    if not game:
        raise ValueError("game is required")
    observed_at = (now or datetime.now(timezone.utc)).isoformat()
    signals: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for intent in intents[:20]:
        key = _text(intent.get("clusterKey") or intent.get("intentKey") or intent.get("key"))
        task = _text(intent.get("playerTask") or intent.get("task") or key)
        queries = [_text(item) for item in _list(intent.get("queries")) if _text(item)] or [f"{game} {task}"]
        evidence: list[dict[str, Any]] = []
        for query in queries[:3]:
            result = probe_searchapi_google_organic(query, seed_terms=[game, task], fetch_fn=fetch_fn)
            if result.get("status") != "SUPPORTED":
                unavailable.append(query)
                continue
            for item in result.get("items", []):
                candidate = _candidate_result(item, task, local_urls)
                if candidate:
                    evidence.append({"source": "SEARCHAPI_GOOGLE_ORGANIC", "query": query, **candidate})
        unique: dict[str, dict[str, Any]] = {item["url"]: item for item in evidence}
        if not unique:
            continue
        local_gap = task.casefold() not in local_tasks and not any(url in local_urls for url in unique)
        signals.append({
            "clusterKey": key,
            "playerTask": task,
            "urls": list(unique),
            "localCoverageGap": local_gap,
            "evidence": list(unique.values()),
            "source": "SEARCHAPI_GOOGLE_ORGANIC",
            "observedAt": observed_at,
            "confidence": "HIGH" if len(unique) >= 2 else "MEDIUM",
            "lifecycle": lifecycle,
        })
    return {"status": "ACTIVE", "game": game, "lifecycle": lifecycle, "observedAt": observed_at, "signals": signals, "unavailableQueries": unavailable}


class handler(BaseHTTPRequestHandler):  # noqa: N801 - Vercel Python function contract
    def _write(self, status: int, body: Mapping[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _payload(self) -> dict[str, Any]:
        if self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) or b"{}")
            return value if isinstance(value, dict) else {}
        query = parse_qs(urlparse(self.path).query)
        return {key: values[-1] for key, values in query.items()}

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        if not os.environ.get("SEARCHAPI_API_KEY"):
            self._write(503, {"status": "UNAVAILABLE", "error": "SEARCHAPI_API_KEY is not configured"})
            return
        try:
            self._write(200, build_intent_gap_response(self._payload()))
        except (ValueError, json.JSONDecodeError) as exc:
            self._write(400, {"status": "ERROR", "error": str(exc)})
