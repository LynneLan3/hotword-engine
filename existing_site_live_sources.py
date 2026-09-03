#!/usr/bin/env python3
"""Load live Existing Site authorities for production daily selection.

Sources (no second registry / sync system):

1. Control Center ``registry/sites.yaml`` (+ ``games.yaml``)
2. GSC「站点配置」— live sheet snapshot via env/API when available, otherwise
   the live Control Center analytics/monitoring GSC bindings that mirror Enabled
   rows (plus local GSC pending-receipt site_ids when present)
3. Steam「站点项目池」— live sheet snapshot via env/API when available

``tests/fixtures/**`` paths are rejected for production loads.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import existing_site_exclusion as exclusion
import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY_SITES = ROOT.parent / "hotword-control-center" / "registry" / "sites.yaml"
DEFAULT_REGISTRY_GAMES = ROOT.parent / "hotword-control-center" / "registry" / "games.yaml"
DEFAULT_GSC_RECEIPTS = ROOT.parent / "gsc_hotword_monitor" / "pending-receipts"
STEAM_API_ENV = "STEAM_CANDIDATE_RESEARCH_API_URL"
FetchFn = Callable[[str], dict[str, Any]]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def assert_production_source_path(path: Path | None, *, label: str) -> Path | None:
    """Reject test fixtures from production / live loads."""
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    parts = {part.casefold() for part in resolved.parts}
    posix = resolved.as_posix().casefold()
    if "fixtures" in parts or "/tests/fixtures/" in posix or posix.endswith("/tests/fixtures"):
        raise ValueError(
            f"{label} refuses fixture path for production existing-site sources: {resolved}"
        )
    return resolved


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _valid_gsc_property(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.upper() in {"MISSING", "NULL", "NONE", "N/A", "-"}:
        return ""
    return text


def gsc_rows_from_registry_sites(sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror live GSC Enabled rows from Control Center GSC bindings."""
    rows: list[dict[str, Any]] = []
    for site in sites:
        if not isinstance(site, dict) or exclusion.is_fixture_or_artificial(site):
            continue
        analytics = _as_dict(site.get("analytics"))
        monitoring = _as_dict(site.get("monitoring"))
        property_url = _valid_gsc_property(
            analytics.get("gsc_property") or monitoring.get("gsc_property")
        )
        monitoring_registered = _text(monitoring.get("status")).upper() in {
            "MONITORING_REGISTERED",
            "ENABLED",
            "LIVE",
        }
        if not property_url and not monitoring_registered:
            continue
        rows.append(
            {
                "site_id": _text(site.get("site_id")),
                "game_id": _text(site.get("game_id")),
                "steam_app_id": _text(site.get("steam_app_id")),
                "站点名称": _text(site.get("site_id")),
                "Enabled": True,
                "enabled": True,
                "property_url": property_url or _text(monitoring.get("gsc_property")),
                "source_note": "live_registry_gsc_binding",
            }
        )
    return rows


def gsc_rows_from_pending_receipts(receipts_root: Path | None) -> list[dict[str, Any]]:
    if receipts_root is None or not receipts_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(receipts_root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        site_id = _text(payload.get("siteId") or payload.get("site_id"))
        if not site_id or exclusion.is_fixture_or_artificial({"site_id": site_id}):
            continue
        common = _as_dict(_as_dict(payload.get("receipt")).get("common"))
        rows.append(
            {
                "site_id": site_id,
                "站点名称": _text(common.get("game") or common.get("site") or site_id),
                "Enabled": True,
                "enabled": True,
                "property_url": _text(common.get("productionUrl")),
                "source_note": "live_gsc_pending_receipt",
            }
        )
    return rows


def _merge_gsc_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            site_id = _text(row.get("site_id") or row.get("siteId"))
            key = site_id or _text(row.get("property_url") or row.get("steam_app_id"))
            if not key:
                continue
            current = merged.get(key) or {}
            combined = dict(current)
            combined.update({k: v for k, v in row.items() if v not in (None, "")})
            combined["enabled"] = True
            combined["Enabled"] = True
            merged[key] = combined
    return list(merged.values())


def _fetch_json(url: str, fetch_fn: FetchFn | None = None) -> dict[str, Any]:
    if fetch_fn is not None:
        payload = fetch_fn(url)
        return payload if isinstance(payload, dict) else {}
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hotword-engine-existing-site-live/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.URLError:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_sheet_source_rows(
    *,
    action: str,
    keys: tuple[str, ...],
    fetch_fn: FetchFn | None = None,
) -> list[dict[str, Any]]:
    base = _text(os.environ.get(STEAM_API_ENV))
    if not base:
        return []
    separator = "&" if "?" in base else "?"
    payload = _fetch_json(f"{base}{separator}action={action}", fetch_fn=fetch_fn)
    if _text(payload.get("error")).upper() in {"UNKNOWN_ACTION", "UNAUTHORIZED"}:
        return []
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def load_live_gsc_rows(
    *,
    registry_sites: list[dict[str, Any]],
    gsc_snapshot_path: Path | None = None,
    gsc_receipts_root: Path | None = DEFAULT_GSC_RECEIPTS,
    fetch_fn: FetchFn | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    provenance: list[str] = []
    snapshot = assert_production_source_path(
        Path(os.environ["HOTWORD_GSC_SITE_SNAPSHOT"])
        if os.environ.get("HOTWORD_GSC_SITE_SNAPSHOT")
        else gsc_snapshot_path,
        label="GSC snapshot",
    )
    api_rows = fetch_sheet_source_rows(
        action="exportGscSiteConfig",
        keys=("gscSiteRows", "rows", "sites"),
        fetch_fn=fetch_fn,
    )
    if api_rows:
        provenance.append("steam_api:exportGscSiteConfig")
    snapshot_rows = exclusion.load_json_rows(snapshot) if snapshot and snapshot.exists() else []
    if snapshot_rows:
        provenance.append(f"snapshot:{snapshot}")
    binding_rows = gsc_rows_from_registry_sites(registry_sites)
    if binding_rows:
        provenance.append("live_registry_gsc_bindings")
    receipt_rows = gsc_rows_from_pending_receipts(gsc_receipts_root)
    if receipt_rows:
        provenance.append("live_gsc_pending_receipts")
    rows = _merge_gsc_rows(api_rows, snapshot_rows, binding_rows, receipt_rows)
    return rows, provenance


def load_live_site_pool_rows(
    *,
    site_pool_snapshot_path: Path | None = None,
    fetch_fn: FetchFn | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    provenance: list[str] = []
    snapshot = assert_production_source_path(
        Path(os.environ["HOTWORD_STEAM_SITE_POOL_SNAPSHOT"])
        if os.environ.get("HOTWORD_STEAM_SITE_POOL_SNAPSHOT")
        else site_pool_snapshot_path,
        label="Steam site pool snapshot",
    )
    api_rows = fetch_sheet_source_rows(
        action="exportSteamSitePool",
        keys=("steamSitePoolRows", "rows", "sites"),
        fetch_fn=fetch_fn,
    )
    if api_rows:
        provenance.append("steam_api:exportSteamSitePool")
    snapshot_rows = exclusion.load_json_rows(snapshot) if snapshot and snapshot.exists() else []
    if snapshot_rows:
        provenance.append(f"snapshot:{snapshot}")
    # Intentionally do NOT invent site-pool rows from registry. A missing pool
    # row is a STATE_SYNC_GAP signal, not a reason to fabricate coverage.
    return api_rows or snapshot_rows, provenance


def load_production_existing_site_index(
    *,
    registry_sites_path: Path | None = None,
    registry_games_path: Path | None = None,
    gsc_snapshot_path: Path | None = None,
    site_pool_snapshot_path: Path | None = None,
    gsc_receipts_root: Path | None = DEFAULT_GSC_RECEIPTS,
    fetch_fn: FetchFn | None = None,
    allow_missing_registry: bool = False,
) -> tuple[exclusion.ExistingSiteIndex, dict[str, Any]]:
    """Build the production ExistingSiteIndex from live authorities only."""
    sites_path = assert_production_source_path(
        Path(os.environ["HOTWORD_REGISTRY_SITES"])
        if os.environ.get("HOTWORD_REGISTRY_SITES")
        else (registry_sites_path or DEFAULT_REGISTRY_SITES),
        label="Control Center registry sites",
    )
    games_path = assert_production_source_path(
        Path(os.environ["HOTWORD_REGISTRY_GAMES"])
        if os.environ.get("HOTWORD_REGISTRY_GAMES")
        else (registry_games_path or DEFAULT_REGISTRY_GAMES),
        label="Control Center registry games",
    )
    if sites_path is None or not sites_path.exists():
        if allow_missing_registry:
            sites_payload: dict[str, Any] = {"sites": []}
        else:
            raise FileNotFoundError(
                f"Live Control Center registry sites.yaml required for production exclusion: {sites_path}"
            )
    else:
        sites_payload = _load_yaml(sites_path)

    sites = [row for row in (sites_payload.get("sites") or []) if isinstance(row, dict)]
    registry_records = exclusion.load_registry_records(
        sites_path,
        games_path if games_path and games_path.exists() else None,
    ) if sites_path and sites_path.exists() else []

    gsc_rows, gsc_provenance = load_live_gsc_rows(
        registry_sites=sites,
        gsc_snapshot_path=gsc_snapshot_path,
        gsc_receipts_root=gsc_receipts_root,
        fetch_fn=fetch_fn,
    )
    pool_rows, pool_provenance = load_live_site_pool_rows(
        site_pool_snapshot_path=site_pool_snapshot_path,
        fetch_fn=fetch_fn,
    )
    index = exclusion.build_existing_site_index(
        registry_records=registry_records,
        gsc_rows=gsc_rows,
        site_pool_rows=pool_rows,
    )
    meta = {
        "mode": "live_production",
        "registry_sites_path": str(sites_path) if sites_path else None,
        "registry_games_path": str(games_path) if games_path else None,
        "registry_site_count": len(registry_records),
        "gsc_row_count": len(gsc_rows),
        "gsc_provenance": gsc_provenance,
        "site_pool_row_count": len(pool_rows),
        "site_pool_provenance": pool_provenance,
        "fixture_paths_rejected": True,
        "indexed_blocking_records": len(index.records),
    }
    return index, meta
