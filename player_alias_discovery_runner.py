#!/usr/bin/env python3
"""Collect public-web evidence for player alias discovery.

Reuses research_runner (YouTube / Reddit RSS / Steam discussions) and
search_demand_providers (autocomplete + optional SearchApi organic).
No model guessing — only retrieved page/snippet text is ranked.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

import player_alias_discovery as alias_core
import research_runner as social
import search_demand_providers as search

JOB_TYPE = "PLAYER_ALIAS_DISCOVERY"
CollectFn = Callable[..., dict[str, Any]]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _diag(
    source: str,
    *,
    ok: bool,
    http_status: int | None = None,
    empty: bool = True,
    parse_count: int = 0,
    error: str = "",
) -> dict[str, Any]:
    return {
        "source": source,
        "httpStatus": http_status,
        "empty": empty,
        "parseCount": parse_count,
        "ok": ok,
        "error": error[:160] if error else "",
    }


def _push_snippet(
    snippets: list[dict[str, Any]],
    *,
    source: str,
    title: str,
    snippet: str = "",
    url: str = "",
) -> None:
    title_text = _text(title)
    body = _text(snippet)
    if not title_text and not body:
        return
    snippets.append(
        {
            "source": source,
            "title": title_text,
            "snippet": body[:400],
            "url": _text(url),
        }
    )


def collect_reddit(game_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = f"{alias_core.clean_name(game_name)} steam"
    snippets: list[dict[str, Any]] = []
    try:
        rows = social.reddit_search_rss(query)
        for row in rows[:25]:
            _push_snippet(
                snippets,
                source="reddit",
                title=row.get("title") or "",
                snippet=row.get("content") or "",
                url=row.get("url") or "",
            )
        return snippets, _diag(
            "reddit",
            ok=True,
            http_status=200,
            empty=not snippets,
            parse_count=len(snippets),
        )
    except Exception as exc:
        return [], _diag("reddit", ok=False, empty=True, error=str(exc))


def collect_youtube(game_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = f"{alias_core.clean_name(game_name)} steam game"
    snippets: list[dict[str, Any]] = []
    try:
        rows = social.youtube_search(query)
        for row in rows[:25]:
            _push_snippet(
                snippets,
                source="youtube",
                title=row.get("title") or "",
                snippet=row.get("desc") or "",
                url=row.get("url") or "",
            )
        return snippets, _diag(
            "youtube",
            ok=True,
            http_status=200,
            empty=not snippets,
            parse_count=len(snippets),
        )
    except Exception as exc:
        return [], _diag("youtube", ok=False, empty=True, error=str(exc))


def _steam_titles_from_html(html: str) -> list[str]:
    titles: list[str] = []
    for match in re.finditer(r'<span class="title">([^<]+)</span>', html, flags=re.I):
        titles.append(social.strip_html(match.group(1)))
    for match in re.finditer(r'data-tooltip-text="([^"]+)"', html, flags=re.I):
        titles.append(social.strip_html(match.group(1)))
    # Topic links from the shared steam parser when available.
    return [t for t in titles if t]


def collect_steam(game_name: str, app_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    app = _text(app_id)
    query = alias_core.clean_name(game_name)
    urls: list[str] = []
    if app:
        urls.append(f"https://steamcommunity.com/app/{app}/discussions/?fp=1")
        urls.append(
            "https://steamcommunity.com/app/"
            + app
            + "/discussions/search/?"
            + urllib.parse.urlencode({"q": query})
        )
    else:
        urls.append(
            "https://steamcommunity.com/discussions/search/?"
            + urllib.parse.urlencode({"q": f"{query} steam"})
        )

    snippets: list[dict[str, Any]] = []
    headers = {"Cookie": getattr(social, "STEAM_COOKIE", ""), "Referer": "https://steamcommunity.com/"}
    last_status: int | None = None
    last_error = ""
    saw_body = False
    last_body = ""

    for url in urls:
        try:
            raw, _final = social.http_get(url, headers)
            body = raw.decode("utf-8", "ignore")
            last_status = 200
            saw_body = True
            last_body = body
            for title in _steam_titles_from_html(body)[:20]:
                _push_snippet(snippets, source="steam_community", title=title, url=url)
            topic_urls = social.steam_topic_urls(body, app) if app else []
            for topic_url in topic_urls[:10]:
                _push_snippet(
                    snippets,
                    source="steam_community",
                    title=query,
                    snippet="steam discussion",
                    url=topic_url,
                )
            time.sleep(0.35)
        except Exception as exc:
            last_error = str(exc)
            if hasattr(exc, "code"):
                try:
                    last_status = int(exc.code)  # type: ignore[arg-type]
                except Exception:
                    pass

    looks_like_steam = bool(re.search(r"forum_|discussion|responsive_tab|commentthread", last_body, re.I))
    ok = bool(snippets) or (saw_body and looks_like_steam)
    return snippets, _diag(
        "steam_community",
        ok=ok,
        http_status=last_status,
        empty=not snippets,
        parse_count=len(snippets),
        error="" if ok else (last_error or "all_requests_failed"),
    )


def collect_web(game_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = alias_core.clean_name(game_name)
    snippets: list[dict[str, Any]] = []
    ok_any = False
    parse_count = 0
    errors: list[str] = []
    http_status: int | None = None

    for probe in (
        search.probe_google_autocomplete,
        search.probe_bing_autocomplete,
    ):
        try:
            result = probe(query)
        except Exception as exc:
            errors.append(str(exc))
            continue
        status = _text(result.get("status")).upper()
        if status in {search.STATUS_SUPPORTED, search.STATUS_BEST_EFFORT}:
            ok_any = True
            http_status = 200
        elif status == search.STATUS_UNAVAILABLE:
            errors.append(_text(result.get("error")) or f"{result.get('source')}_unavailable")
            continue
        for item in result.get("items") or []:
            text = _text(item.get("text"))
            if not text:
                continue
            parse_count += 1
            _push_snippet(
                snippets,
                source="web",
                title=text,
                snippet=f"autocomplete:{result.get('source')}",
                url="",
            )

    api_key = _text(os.environ.get("SEARCHAPI_API_KEY"))
    if api_key:
        try:
            organic = search.probe_searchapi_google_organic(f"{query} steam game", api_key=api_key)
            status = _text(organic.get("status")).upper()
            if status in {search.STATUS_SUPPORTED, search.STATUS_BEST_EFFORT}:
                ok_any = True
                http_status = 200
                for item in organic.get("items") or []:
                    title = _text(item.get("title") or item.get("text"))
                    url = _text(item.get("url"))
                    snippet = _text(item.get("snippet"))
                    if not title and not snippet:
                        continue
                    parse_count += 1
                    _push_snippet(
                        snippets,
                        source="web",
                        title=title,
                        snippet=snippet,
                        url=url,
                    )
            else:
                errors.append(_text(organic.get("error")) or "searchapi_unavailable")
        except Exception as exc:
            errors.append(str(exc))

    return snippets, _diag(
        "web",
        ok=ok_any,
        http_status=http_status,
        empty=not snippets,
        parse_count=parse_count,
        error="" if ok_any else ("; ".join(errors)[:160] or "all_web_probes_failed"),
    )


def collect_all_sources(
    game_name: str,
    app_id: str,
    steam_url: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    del steam_url  # reserved for future store-page evidence
    snippets: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []

    for collector in (
        lambda: collect_reddit(game_name),
        lambda: collect_youtube(game_name),
        lambda: collect_steam(game_name, app_id),
        lambda: collect_web(game_name),
    ):
        part, diag = collector()
        snippets.extend(part)
        diags.append(diag)
        time.sleep(0.4)
    return snippets, diags


def validate_job(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("player alias job must be an object")
    required = ("job_id", "job_type", "steam_app_id", "game_name", "steam_url")
    missing = [key for key in required if not _text(job.get(key))]
    if missing:
        raise ValueError("player alias job missing fields: " + ", ".join(missing))
    if _text(job.get("job_type")).upper() != JOB_TYPE:
        raise ValueError("job_type must be PLAYER_ALIAS_DISCOVERY")
    return {
        "job_id": _text(job.get("job_id")),
        "job_type": JOB_TYPE,
        "steam_app_id": _text(job.get("steam_app_id")),
        "game_name": _text(job.get("game_name")),
        "steam_url": _text(job.get("steam_url")),
        "research_cycle_date": _text(job.get("research_cycle_date")),
        "created_at": _text(job.get("created_at")),
    }


def run_discovery(
    job: dict[str, Any],
    *,
    collect_fn: CollectFn | None = None,
) -> dict[str, Any]:
    validated = validate_job(job)
    if collect_fn is not None:
        collected = collect_fn(
            validated["game_name"],
            validated["steam_app_id"],
            validated["steam_url"],
        )
        snippets = list(collected.get("snippets") or [])
        diags = list(collected.get("source_diags") or [])
    else:
        snippets, diags = collect_all_sources(
            validated["game_name"],
            validated["steam_app_id"],
            validated["steam_url"],
        )

    discovery = alias_core.discover_from_snippets(
        validated["game_name"],
        snippets,
        diags,
    )
    return {
        "job_id": validated["job_id"],
        "job_type": JOB_TYPE,
        "steam_app_id": validated["steam_app_id"],
        "game_name": validated["game_name"],
        "steam_url": validated["steam_url"],
        "research_cycle_date": validated["research_cycle_date"],
        "research_status": "COMPLETED",
        "alias": discovery["alias"],
        "status": discovery["status"],
        "confidence": discovery["confidence"],
        "source_count": discovery["source_count"],
        "source_urls": discovery["source_urls"],
        "evidence": discovery["evidence"],
        "patterns": discovery["patterns"],
        "ranked": [
            {
                "text": row["text"],
                "hits": row["hits"],
                "sources": row["sources"],
            }
            for row in discovery["ranked"]
        ],
        "source_diags": discovery["source_diags"],
        "completed_at": _now_iso(),
    }
