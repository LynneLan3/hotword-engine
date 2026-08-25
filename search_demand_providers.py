#!/usr/bin/env python3
"""R3B-0 Search provider feasibility helpers.

Providers only return raw search signals. They do not decide SEARCH_CONFIRMED.
Network is injected via fetch_fn so unit tests stay offline.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

STATUS_SUPPORTED = "SUPPORTED"
STATUS_BEST_EFFORT = "BEST_EFFORT"
STATUS_UNAVAILABLE = "UNAVAILABLE"

SOURCE_GOOGLE_AUTOCOMPLETE = "GOOGLE_AUTOCOMPLETE"
SOURCE_GOOGLE_PAA = "GOOGLE_PAA"
SOURCE_GOOGLE_RELATED = "GOOGLE_RELATED"
SOURCE_BING_AUTOCOMPLETE = "BING_AUTOCOMPLETE"
SOURCE_SEARCHAPI_GOOGLE_ORGANIC = "SEARCHAPI_GOOGLE_ORGANIC"

SOURCES = (
    SOURCE_GOOGLE_AUTOCOMPLETE,
    SOURCE_GOOGLE_PAA,
    SOURCE_GOOGLE_RELATED,
    SOURCE_BING_AUTOCOMPLETE,
)

KIND_SUGGESTION = "suggestion"
KIND_QUESTION = "question"
KIND_RELATED_QUERY = "related_query"
KIND_ORGANIC_RESULT = "organic_result"

SEARCHAPI_SEARCH_ENDPOINT = "https://www.searchapi.io/api/v1/search"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SSL_CTX = ssl.create_default_context()

ANCHOR_CLASS_RE = re.compile(r"\bclasses?\b", re.I)

BING_MAPS_MARKERS = (
    "virtualearth.net",
    "dev.virtualearth.net",
    "atlas.microsoft.com",
    "azure maps",
    "bing maps",
)


FetchFn = Callable[[str, dict[str, str] | None], dict[str, Any]]


def provider_result(
    source: str,
    status: str,
    query: str,
    items: list[dict[str, Any]] | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "query": query,
        "items": items or [],
        "error": error,
        "metadata": metadata or {},
    }


def default_http_get(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            raw = resp.read()
            body = raw.decode("utf-8", "replace")
            return {
                "ok": 200 <= int(resp.status) < 300,
                "status": int(resp.status),
                "body": body,
                "url": resp.geturl(),
                "error": None,
            }
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        body = raw.decode("utf-8", "replace") if raw else ""
        return {
            "ok": False,
            "status": int(e.code),
            "body": body,
            "url": url,
            "error": f"HTTPError {e.code}",
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "body": "",
            "url": url,
            "error": f"{type(e).__name__}: {e}",
        }


def looks_like_html(body: str) -> bool:
    s = str(body or "").lstrip().lower()
    return s.startswith("<!doctype") or s.startswith("<html") or s.startswith("<?xml")


def looks_like_captcha(body: str) -> bool:
    s = str(body or "").lower()
    return (
        "/sorry/" in s
        or "unusual traffic" in s
        or "our systems have detected unusual" in s
        or ("captcha" in s and "google" in s)
    )


def looks_like_consent(body: str) -> bool:
    s = str(body or "").lower()
    return (
        "consent.google.com" in s
        or "before you continue to google" in s
        or "action=\"https://consent.google.com" in s
    )


def is_bing_maps_payload(body: str) -> bool:
    raw = str(body or "")
    lower = raw.lower()
    if any(m in lower for m in BING_MAPS_MARKERS):
        return True
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if "resourceSets" in data:
        return True
    brand = str(data.get("brandLogoUri") or "").lower()
    if "virtualearth" in brand or "bingmaps" in brand:
        return True
    auth = str(data.get("authenticationResultCode") or "")
    if auth and "resourceSets" in data:
        return True
    resources = data.get("results") or data.get("resourceSets")
    if isinstance(data.get("value"), list):
        first = data["value"][0] if data["value"] else {}
        if isinstance(first, dict) and ("address" in first or first.get("__type") == "Place"):
            return True
    if isinstance(resources, list) and resources:
        blob = json.dumps(resources[0]).lower()
        if '"address"' in blob and ("locality" in blob or "countryregion" in blob):
            return True
    return False


def parse_suggest_osjson(body: str) -> list[str] | None:
    """Firefox/OSJSON autocomplete: [query, [suggestions...], ...]."""
    raw = str(body or "").strip()
    if not raw or looks_like_html(raw):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 2:
        return None
    suggestions = data[1]
    if not isinstance(suggestions, list):
        return None
    out: list[str] = []
    for item in suggestions:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, list) and item and isinstance(item[0], str):
            text = item[0].strip()
        else:
            continue
        if text:
            out.append(text)
    return out


def parse_google_paa_html(html: str) -> list[str] | None:
    """Return questions only from a recognizable PAA structure.

    None = no stable PAA structure (do not treat random questions as PAA).
    [] = structure present but empty.
    """
    raw = str(html or "")
    if not raw:
        return None
    if looks_like_captcha(raw) or looks_like_consent(raw):
        return None
    questions: list[str] = []
    for rx in (
        re.compile(
            r'class="[^"]*related-question-pair[^"]*"[^>]{0,240}data-q="([^"]+)"',
            re.I,
        ),
        re.compile(
            r'data-q="([^"]+)"[^>]{0,240}class="[^"]*related-question-pair',
            re.I,
        ),
    ):
        for m in rx.finditer(raw):
            q = html_lib.unescape(m.group(1)).strip()
            if q:
                questions.append(q)
    if questions:
        return questions
    # Structured JS blob used by some SERP payloads.
    if re.search(r"people[_ ]also[_ ]ask", raw, re.I) and '"question"' in raw:
        blob_qs = re.findall(
            r'"question"\s*:\s*"((?:\\.|[^"\\])+)"',
            raw,
        )
        cleaned = []
        for q in blob_qs:
            try:
                text = html_lib.unescape(bytes(q, "utf-8").decode("unicode_escape")).strip()
            except Exception:
                text = html_lib.unescape(q).strip()
            if text.endswith("?") or len(text.split()) >= 4:
                cleaned.append(text)
        if cleaned:
            return cleaned
    return None


def _related_section(html: str) -> str:
    raw = str(html or "")
    markers = []
    m = re.search(r'id=["\']bres["\']', raw, re.I)
    if m:
        markers.append(m.start())
    for label in (
        "Related searches",
        "People also search for",
        "Related to this search",
    ):
        idx = raw.find(label)
        if idx >= 0:
            markers.append(idx)
    if not markers:
        return ""
    start = min(markers)
    return raw[start : start + 8000]


def parse_google_related_html(html: str) -> list[str] | None:
    """Related searches only from a related-search section, not organic links."""
    raw = str(html or "")
    if not raw:
        return None
    if looks_like_captcha(raw) or looks_like_consent(raw):
        return None
    section = _related_section(raw)
    if not section:
        return None
    queries: list[str] = []
    for m in re.finditer(r"/search\?q=([^\"'&]+)", section):
        q = urllib.parse.unquote_plus(m.group(1)).strip()
        q = html_lib.unescape(q)
        if q:
            queries.append(q)
    for m in re.finditer(
        r'class="[^"]*(?:s75CSd|k8XOCe|s75CSd B2QT7b)[^"]*"[^>]*>\s*([^<]{2,80})',
        section,
        re.I,
    ):
        q = html_lib.unescape(m.group(1)).strip()
        if q:
            queries.append(q)
    return queries


def dedupe_texts(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        s = " ".join(str(t or "").split()).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def is_anchor_relevant(text: str, anchor_topic: str = "classes") -> bool:
    """Light diagnostic: class/classes (or the given class-like topic) only."""
    s = str(text or "")
    topic = str(anchor_topic or "classes").strip()
    if not s:
        return False
    if topic and re.search(rf"\b{re.escape(topic)}\b", s, re.I):
        return True
    if ANCHOR_CLASS_RE.search(s):
        return True
    return False


def matched_seed_terms(text: str, seed_terms: list[str]) -> list[str]:
    blob = str(text or "").lower()
    hits: list[str] = []
    for seed in seed_terms:
        s = str(seed or "").strip()
        if s and s.lower() in blob:
            hits.append(s)
    return hits


def annotate_items(
    texts: list[str],
    kind: str,
    seed_terms: list[str],
    anchor_topic: str = "classes",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for text in dedupe_texts(texts):
        items.append(
            {
                "text": text,
                "kind": kind,
                "matched_seed_terms": matched_seed_terms(text, seed_terms),
                "anchor_relevant": is_anchor_relevant(text, anchor_topic),
            }
        )
    return items


def _fetch(
    fetch_fn: FetchFn | None,
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    try:
        if fetch_fn is None:
            resp = default_http_get(url, headers, timeout=timeout)
        else:
            # Keep injected fetchers on the existing two-argument test contract.
            resp = fetch_fn(url, headers)
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "body": "",
            "url": url,
            "error": f"{type(e).__name__}: {e}",
        }
    if not isinstance(resp, dict):
        return {
            "ok": False,
            "status": 0,
            "body": "",
            "url": url,
            "error": "invalid_fetch_response",
        }
    return {
        "ok": bool(resp.get("ok")),
        "status": int(resp.get("status") or 0),
        "body": str(resp.get("body") or ""),
        "url": str(resp.get("url") or url),
        "error": resp.get("error"),
    }


def _redact_searchapi_transport_error(error: Any, api_key: str) -> str:
    """Keep transport diagnostics useful without persisting request credentials."""
    text = str(error or "transport failure")
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
        text = text.replace(f"Bearer {api_key}", "Bearer [REDACTED]")
    text = re.sub(r"(?i)authorization\s*:\s*bearer\s+[^\s,;]+", "[REDACTED_AUTHORIZATION]", text)
    text = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;&]+", r"\1[REDACTED]", text)
    return text[:300]


def google_autocomplete_url(query: str, hl: str = "en", gl: str = "us") -> str:
    return "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "hl": hl, "gl": gl, "q": query}
    )


def google_search_url(query: str, hl: str = "en", gl: str = "us") -> str:
    return "https://www.google.com/search?" + urllib.parse.urlencode(
        {"hl": hl, "gl": gl, "pws": "0", "q": query}
    )


def bing_autocomplete_url(query: str) -> str:
    return "https://api.bing.com/osjson.aspx?" + urllib.parse.urlencode({"query": query})


def searchapi_google_organic_url(
    query: str,
    hl: str = "en",
    gl: str = "us",
    device: str = "desktop",
) -> str:
    return SEARCHAPI_SEARCH_ENDPOINT + "?" + urllib.parse.urlencode(
        {
            "engine": "google",
            "q": query,
            "gl": gl,
            "hl": hl,
            "device": device,
        }
    )


def probe_google_autocomplete(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
    hl: str = "en",
    gl: str = "us",
) -> dict[str, Any]:
    url = google_autocomplete_url(query, hl=hl, gl=gl)
    resp = _fetch(fetch_fn, url)
    meta = {
        "http_status": resp["status"],
        "endpoint": url,
        "locale": {"hl": hl, "gl": gl},
        "final_url": resp["url"],
    }
    if not resp["ok"]:
        return provider_result(
            SOURCE_GOOGLE_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error=str(resp["error"] or f"http_{resp['status']}"),
            metadata=meta,
        )
    if looks_like_captcha(resp["body"]) or looks_like_consent(resp["body"]):
        return provider_result(
            SOURCE_GOOGLE_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error="captcha_or_consent",
            metadata=meta,
        )
    suggestions = parse_suggest_osjson(resp["body"])
    if suggestions is None:
        return provider_result(
            SOURCE_GOOGLE_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error="unparseable_autocomplete_body",
            metadata=meta,
        )
    items = annotate_items(suggestions, KIND_SUGGESTION, seed_terms or [query], anchor_topic)
    meta["suggestion_count"] = len(items)
    meta["raw_suggestions"] = [it["text"] for it in items]
    return provider_result(
        SOURCE_GOOGLE_AUTOCOMPLETE,
        STATUS_BEST_EFFORT,
        query,
        items=items,
        metadata=meta,
    )


def probe_google_paa(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    url = google_search_url(query)
    resp = _fetch(fetch_fn, url)
    meta = {"http_status": resp["status"], "endpoint": url, "final_url": resp["url"]}
    if not resp["ok"]:
        return provider_result(
            SOURCE_GOOGLE_PAA,
            STATUS_UNAVAILABLE,
            query,
            error=str(resp["error"] or f"http_{resp['status']}"),
            metadata=meta,
        )
    if looks_like_captcha(resp["body"]) or looks_like_consent(resp["body"]):
        return provider_result(
            SOURCE_GOOGLE_PAA,
            STATUS_UNAVAILABLE,
            query,
            error="captcha_or_consent",
            metadata=meta,
        )
    questions = parse_google_paa_html(resp["body"])
    if questions is None:
        return provider_result(
            SOURCE_GOOGLE_PAA,
            STATUS_UNAVAILABLE,
            query,
            error="no_stable_paa_structure",
            metadata=meta,
        )
    items = annotate_items(questions, KIND_QUESTION, seed_terms or [query], anchor_topic)
    meta["question_count"] = len(items)
    return provider_result(SOURCE_GOOGLE_PAA, STATUS_BEST_EFFORT, query, items=items, metadata=meta)


def probe_google_related(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    url = google_search_url(query)
    resp = _fetch(fetch_fn, url)
    meta = {"http_status": resp["status"], "endpoint": url, "final_url": resp["url"]}
    if not resp["ok"]:
        return provider_result(
            SOURCE_GOOGLE_RELATED,
            STATUS_UNAVAILABLE,
            query,
            error=str(resp["error"] or f"http_{resp['status']}"),
            metadata=meta,
        )
    if looks_like_captcha(resp["body"]) or looks_like_consent(resp["body"]):
        return provider_result(
            SOURCE_GOOGLE_RELATED,
            STATUS_UNAVAILABLE,
            query,
            error="captcha_or_consent",
            metadata=meta,
        )
    related = parse_google_related_html(resp["body"])
    if related is None:
        return provider_result(
            SOURCE_GOOGLE_RELATED,
            STATUS_UNAVAILABLE,
            query,
            error="no_stable_related_section",
            metadata=meta,
        )
    items = annotate_items(related, KIND_RELATED_QUERY, seed_terms or [query], anchor_topic)
    meta["related_count"] = len(items)
    return provider_result(
        SOURCE_GOOGLE_RELATED,
        STATUS_BEST_EFFORT,
        query,
        items=items,
        metadata=meta,
    )


def probe_bing_autocomplete(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
) -> dict[str, Any]:
    url = bing_autocomplete_url(query)
    resp = _fetch(fetch_fn, url)
    meta = {"http_status": resp["status"], "endpoint": url, "final_url": resp["url"]}
    if not resp["ok"]:
        return provider_result(
            SOURCE_BING_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error=str(resp["error"] or f"http_{resp['status']}"),
            metadata=meta,
        )
    if is_bing_maps_payload(resp["body"]):
        return provider_result(
            SOURCE_BING_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error="bing_maps_payload_rejected",
            metadata=meta,
        )
    if looks_like_html(resp["body"]) or looks_like_captcha(resp["body"]):
        return provider_result(
            SOURCE_BING_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error="html_or_captcha_body",
            metadata=meta,
        )
    suggestions = parse_suggest_osjson(resp["body"])
    if suggestions is None:
        return provider_result(
            SOURCE_BING_AUTOCOMPLETE,
            STATUS_UNAVAILABLE,
            query,
            error="unparseable_autocomplete_body",
            metadata=meta,
        )
    items = annotate_items(suggestions, KIND_SUGGESTION, seed_terms or [query], anchor_topic)
    meta["suggestion_count"] = len(items)
    meta["raw_suggestions"] = [it["text"] for it in items]
    return provider_result(
        SOURCE_BING_AUTOCOMPLETE,
        STATUS_BEST_EFFORT,
        query,
        items=items,
        metadata=meta,
    )


def probe_searchapi_google_organic(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
    api_key: str | None = None,
    hl: str = "en",
    gl: str = "us",
    device: str = "desktop",
) -> dict[str, Any]:
    """Probe SearchApi's paid Google organic SERP provider.

    The provider is intentionally not registered in SOURCES/PROBES. Callers
    must opt in explicitly so ordinary provider probes cannot spend credits.
    """
    key = api_key if api_key is not None else os.environ.get("SEARCHAPI_API_KEY")
    url = searchapi_google_organic_url(query, hl=hl, gl=gl, device=device)
    metadata = {
        "http_status": 0,
        "locale": {"hl": hl, "gl": gl},
        "device": device,
        "organic_count": 0,
        "endpoint": url,
    }
    if not str(key or "").strip():
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="missing_searchapi_api_key",
            metadata=metadata,
        )

    # Paid provider requests are single-attempt. Never retry a paid request:
    # a transport failure may still have been billed by SearchApi.
    resp = _fetch(
        fetch_fn,
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
        timeout=45,
    )
    metadata["http_status"] = resp["status"]
    metadata["response_received"] = resp["status"] > 0
    metadata["billing_status"] = "UNKNOWN"
    if resp["status"] == 0:
        metadata["transport_error"] = _redact_searchapi_transport_error(resp.get("error"), str(key))
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="searchapi_transport_error",
            metadata=metadata,
        )
    if not resp["ok"] or not 200 <= resp["status"] < 300:
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="searchapi_http_error",
            metadata=metadata,
        )

    try:
        payload = json.loads(resp["body"])
    except (TypeError, ValueError):
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="invalid_searchapi_json",
            metadata=metadata,
        )

    if not isinstance(payload, dict):
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="invalid_searchapi_payload",
            metadata=metadata,
        )
    search_metadata = payload.get("search_metadata")
    search_status = search_metadata.get("status") if isinstance(search_metadata, dict) else None
    if payload.get("error") or str(payload.get("status") or search_status or "").lower() == "error":
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="searchapi_error_response",
            metadata=metadata,
        )
    organic_results = payload.get("organic_results")
    if not isinstance(organic_results, list):
        return provider_result(
            SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
            STATUS_UNAVAILABLE,
            query,
            error="missing_searchapi_organic_results",
            metadata=metadata,
        )

    terms = seed_terms or [query]
    items: list[dict[str, Any]] = []
    for index, result in enumerate(organic_results, start=1):
        if not isinstance(result, dict):
            continue
        title = " ".join(str(result.get("title") or "").split()).strip()
        link = str(result.get("link") or result.get("url") or "").strip()
        domain = str(result.get("domain") or "").strip()
        if not domain and link:
            domain = urllib.parse.urlparse(link).netloc
        snippet = " ".join(str(result.get("snippet") or "").split()).strip()
        position = result.get("position", index)
        items.append(
            {
                "kind": KIND_ORGANIC_RESULT,
                "text": title,
                "position": position,
                "title": title,
                "domain": domain,
                "url": link,
                "snippet": snippet,
                "matched_seed_terms": matched_seed_terms(f"{title} {snippet}", terms),
                "anchor_relevant": is_anchor_relevant(f"{title} {snippet}", anchor_topic),
            }
        )

    metadata["organic_count"] = len(items)
    return provider_result(
        SOURCE_SEARCHAPI_GOOGLE_ORGANIC,
        STATUS_SUPPORTED,
        query,
        items=items,
        metadata=metadata,
    )


PROBES = {
    SOURCE_GOOGLE_AUTOCOMPLETE: probe_google_autocomplete,
    SOURCE_GOOGLE_PAA: probe_google_paa,
    SOURCE_GOOGLE_RELATED: probe_google_related,
    SOURCE_BING_AUTOCOMPLETE: probe_bing_autocomplete,
}


def probe_all_sources(
    query: str,
    seed_terms: list[str] | None = None,
    anchor_topic: str = "classes",
    fetch_fn: FetchFn | None = None,
    sources: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for source in sources or SOURCES:
        fn = PROBES[source]
        try:
            out.append(
                fn(
                    query,
                    seed_terms=seed_terms,
                    anchor_topic=anchor_topic,
                    fetch_fn=fetch_fn,
                )
            )
        except Exception as e:
            out.append(
                provider_result(
                    source,
                    STATUS_UNAVAILABLE,
                    query,
                    error=f"{type(e).__name__}: {e}",
                    metadata={"isolated_failure": True},
                )
            )
    return out


def summarize_provider_runs(source: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [r.get("status") for r in runs]
    items: list[dict[str, Any]] = []
    for r in runs:
        items.extend(r.get("items") or [])
    texts = dedupe_texts([it.get("text", "") for it in items])
    unique_items = []
    seen: set[str] = set()
    for it in items:
        key = str(it.get("text") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique_items.append(it)
    http_ok = any(int((r.get("metadata") or {}).get("http_status") or 0) == 200 for r in runs)
    best_effort = any(s == STATUS_BEST_EFFORT for s in statuses)
    status = STATUS_BEST_EFFORT if best_effort else STATUS_UNAVAILABLE
    errors = [r.get("error") for r in runs if r.get("error")]
    notes = []
    if best_effort:
        notes.append("unpublished/undocumented HTTP surface returned parseable query/question signals")
    if errors:
        notes.append("errors: " + "; ".join(str(e) for e in errors[:4]))
    if not http_ok:
        notes.append("no successful HTTP 200")
    return {
        "status": status,
        "http_ok": http_ok,
        "evidence_count": len(unique_items),
        "anchor_evidence_count": sum(1 for it in unique_items if it.get("anchor_relevant")),
        "items": unique_items,
        "errors": errors,
        "notes": " | ".join(notes) if notes else "",
        "runs": len(runs),
    }


def source_qualifies_for_r3b(summary: dict[str, Any]) -> bool:
    if summary.get("status") != STATUS_BEST_EFFORT:
        return False
    if not summary.get("http_ok"):
        return False
    if int(summary.get("evidence_count") or 0) <= 0:
        return False
    # Must be able to tell anchor vs game-wide: at least one annotated item exists.
    items = summary.get("items") or []
    if not items:
        return False
    has_true = any(bool(it.get("anchor_relevant")) for it in items)
    has_false = any(not bool(it.get("anchor_relevant")) for it in items)
    if not (has_true or has_false):
        return False
    # Distinction is possible even if all items share one label, because annotation ran.
    # Require mixed labels when there are 2+ items; a single class-specific hit still qualifies.
    if len(items) >= 2 and not (has_true and has_false) and not has_true:
        # all game-wide: still qualifies as distinguishable (all false is a valid diagnostic)
        return True
    return True


def recommend_r3b_sources(summaries: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    recommended: list[str] = []
    excluded: list[str] = []
    for source in SOURCES:
        summary = summaries.get(source) or {}
        if source_qualifies_for_r3b(summary):
            recommended.append(source)
        else:
            excluded.append(source)
    return recommended, excluded
