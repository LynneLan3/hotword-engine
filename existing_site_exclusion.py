#!/usr/bin/env python3
"""Existing-site exclusion and stale BUILD reconciliation for daily candidates.

Reads three authority sources without creating a second registry:

1. Control Center ``registry/sites.yaml`` (+ optional ``games.yaml``)
2. GSC Sheet「站点配置」rows (injected snapshot)
3. Steam「站点项目池」rows (injected snapshot)

Identity match priority:

    Steam App ID > site_id / canonical game_id > normalized exact game name

When a Steam App ID is present, name-only matches are never used.
Fixtures / artificial sites never exclude a real candidate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is expected in local tooling
    yaml = None  # type: ignore[assignment]

BLOCKING_LIFECYCLE_PHASES = frozenset({
    "GENERATED",
    "PREVIEW_READY",
    "LIVE",
    "PRODUCTION",
})

# Site-pool / sheet status labels that prove a site already exists.
TERMINAL_SITE_STATUSES = frozenset({
    "GENERATED",
    "PREVIEW_READY",
    "LIVE",
    "PRODUCTION",
    "已建站",
    "已上线",
    "已完成",
    "BUILD_COMPLETE",
    "COMPLETED",
    "COMPLETE",
    "DONE",
    "PUBLISHED",
    "READY_FOR_PRODUCTION_REVIEW",
})

ALREADY_BUILT = "ALREADY_BUILT"
STATE_SYNC_GAP = "STATE_SYNC_GAP"
SOURCE_REGISTRY = "CONTROL_CENTER_REGISTRY"
SOURCE_GSC = "GSC_SITE_CONFIG"
SOURCE_SITE_POOL = "STEAM_SITE_POOL"

_FIXTURE_RE = re.compile(r"(?:^|[-_])(?:fixture|artificial)(?:[-_]|$)", re.IGNORECASE)
_PUNCTUATION_RE = re.compile(r"[^\w\s']+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_game_name(value: Any) -> str:
    """Normalize trademark / punctuation differences for exact-name matching."""
    text = _text(value)
    for mark in ("™", "®", "©", "℠"):
        text = text.replace(mark, " ")
    text = unicodedata.normalize("NFKC", text)
    # NFKC may expand leftover trademark codepoints into ascii letters.
    text = re.sub(r"\b(?:tm|sm)\b", " ", text, flags=re.IGNORECASE)
    text = text.replace("(r)", " ").replace("(c)", " ")
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    text = text.casefold()
    text = _PUNCTUATION_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def compact_identity(value: Any) -> str:
    """Lowercase alphanumeric-only form for site_id / game_id style keys."""
    return _NON_ALNUM_RE.sub("", normalize_game_name(value))


def is_fixture_or_artificial(row: dict[str, Any]) -> bool:
    environment = _text(row.get("environment")).casefold()
    if environment in {"artificial", "fixture", "test"}:
        return True
    for key in ("site_id", "game_id", "siteId", "gameId", "name", "游戏名称", "站点名称"):
        value = _text(row.get(key))
        if value and _FIXTURE_RE.search(value):
            return True
    local_path = _text(row.get("local_path") or row.get("localPath"))
    if local_path and _FIXTURE_RE.search(Path(local_path).name):
        return True
    return False


def normalize_lifecycle_phase(value: Any) -> str:
    raw = _text(value).upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "READY_FOR_PRODUCTION_REVIEW": "PREVIEW_READY",
        "PREVIEW": "PREVIEW_READY",
        "PROD": "PRODUCTION",
        "PRODUCTION_LIVE": "PRODUCTION",
    }
    return aliases.get(raw, raw)


def is_blocking_lifecycle(value: Any) -> bool:
    phase = normalize_lifecycle_phase(value)
    if phase in BLOCKING_LIFECYCLE_PHASES:
        return True
    # Sheet Chinese / mixed labels
    upper = _text(value).upper()
    return upper in {item.upper() for item in TERMINAL_SITE_STATUSES}


def next_action_for_lifecycle(status: str) -> str:
    phase = normalize_lifecycle_phase(status)
    if phase == "PREVIEW_READY" or phase == "GENERATED":
        return "audit/publish"
    if phase in {"LIVE", "PRODUCTION"}:
        return "GSC monitoring / create-guide"
    return "reconcile existing site lifecycle"


@dataclass
class ExistingSiteRecord:
    site_id: str = ""
    game_id: str = ""
    steam_app_id: str = ""
    game_name: str = ""
    status: str = ""
    source: str = ""
    enabled: bool | None = None
    fixture: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "game_id": self.game_id,
            "steam_app_id": self.steam_app_id,
            "game_name": self.game_name,
            "status": self.status,
            "source": self.source,
            "enabled": self.enabled,
            "fixture": self.fixture,
            "evidence": self.evidence,
        }


@dataclass
class ExistingSiteIndex:
    records: list[ExistingSiteRecord] = field(default_factory=list)
    by_app_id: dict[str, list[ExistingSiteRecord]] = field(default_factory=dict)
    by_site_id: dict[str, list[ExistingSiteRecord]] = field(default_factory=dict)
    by_game_id: dict[str, list[ExistingSiteRecord]] = field(default_factory=dict)
    by_name: dict[str, list[ExistingSiteRecord]] = field(default_factory=dict)

    def add(self, record: ExistingSiteRecord) -> None:
        if record.fixture:
            return
        if not is_blocking_lifecycle(record.status) and not (
            record.source == SOURCE_GSC and record.enabled is True
        ):
            return
        self.records.append(record)
        if record.steam_app_id:
            self.by_app_id.setdefault(record.steam_app_id, []).append(record)
        if record.site_id:
            self.by_site_id.setdefault(record.site_id, []).append(record)
            compact = compact_identity(record.site_id)
            if compact:
                self.by_site_id.setdefault(compact, []).append(record)
        if record.game_id:
            self.by_game_id.setdefault(record.game_id, []).append(record)
            compact = compact_identity(record.game_id)
            if compact:
                self.by_game_id.setdefault(compact, []).append(record)
        name_key = normalize_game_name(record.game_name)
        if name_key:
            self.by_name.setdefault(name_key, []).append(record)
        compact_name = compact_identity(record.game_name)
        if compact_name:
            self.by_name.setdefault(compact_name, []).append(record)


def _truthy_enabled(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return _text(value).upper() in {"TRUE", "1", "YES", "Y", "ENABLED"}


def _row_get(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return ""


def record_from_registry_site(
    site: dict[str, Any],
    *,
    games_by_id: dict[str, dict[str, Any]] | None = None,
) -> ExistingSiteRecord:
    games_by_id = games_by_id or {}
    game_id = _text(site.get("game_id"))
    game = games_by_id.get(game_id) or {}
    lifecycle = _as_dict(site.get("lifecycle"))
    status = normalize_lifecycle_phase(
        lifecycle.get("phase") or _as_dict(site.get("site")).get("status")
    )
    steam_app_id = _text(
        site.get("steam_app_id")
        or site.get("steamAppId")
        or game.get("steam_app_id")
        or game.get("steamAppId")
    )
    game_name = _text(game.get("name") or site.get("name") or game_id or site.get("site_id"))
    return ExistingSiteRecord(
        site_id=_text(site.get("site_id")),
        game_id=game_id,
        steam_app_id=steam_app_id,
        game_name=game_name,
        status=status,
        source=SOURCE_REGISTRY,
        enabled=None,
        fixture=is_fixture_or_artificial(site),
        evidence={
            "lifecycle_phase": lifecycle.get("phase"),
            "site_status": _as_dict(site.get("site")).get("status"),
            "local_path": site.get("local_path"),
        },
    )


def record_from_gsc_row(row: dict[str, Any]) -> ExistingSiteRecord:
    enabled = _truthy_enabled(_row_get(row, "enabled", "Enabled", "ENABLE"))
    status = "LIVE" if enabled else _text(_row_get(row, "status", "当前状态"))
    return ExistingSiteRecord(
        site_id=_text(_row_get(row, "site_id", "siteId", "Site ID")),
        game_id=_text(_row_get(row, "game_id", "gameId")),
        steam_app_id=_text(_row_get(row, "steam_app_id", "steamAppId", "Steam App ID", "AppID")),
        game_name=_text(_row_get(row, "game_name", "name", "站点名称", "游戏名称")),
        status=status or ("LIVE" if enabled else ""),
        source=SOURCE_GSC,
        enabled=enabled,
        fixture=is_fixture_or_artificial(row),
        evidence={
            "enabled": enabled,
            "property_url": _row_get(row, "property_url", "propertyUrl", "Property URL"),
        },
    )


def record_from_site_pool_row(row: dict[str, Any]) -> ExistingSiteRecord:
    status = _text(
        _row_get(row, "当前状态", "Build状态", "status", "lifecycle_phase", "phase")
    )
    return ExistingSiteRecord(
        site_id=_text(_row_get(row, "site_id", "siteId", "Site ID")),
        game_id=_text(_row_get(row, "game_id", "gameId")),
        steam_app_id=_text(_row_get(row, "steam_app_id", "steamAppId", "Steam App ID", "AppID")),
        game_name=_text(_row_get(row, "game_name", "name", "游戏名称", "站点名称")),
        status=status,
        source=SOURCE_SITE_POOL,
        enabled=None,
        fixture=is_fixture_or_artificial(row),
        evidence={"raw_status": status, "vercel_url": _row_get(row, "Vercel URL", "vercel_url")},
    )


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load Control Center registry files")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def load_registry_records(
    sites_path: Path,
    games_path: Path | None = None,
) -> list[ExistingSiteRecord]:
    sites_payload = load_yaml(sites_path)
    games_by_id: dict[str, dict[str, Any]] = {}
    if games_path and games_path.exists():
        games_payload = load_yaml(games_path)
        for game in games_payload.get("games") or []:
            if isinstance(game, dict) and _text(game.get("game_id")):
                games_by_id[_text(game["game_id"])] = game
    records: list[ExistingSiteRecord] = []
    for site in sites_payload.get("sites") or []:
        if not isinstance(site, dict):
            continue
        records.append(record_from_registry_site(site, games_by_id=games_by_id))
    return records


def load_json_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "sites", "gscSiteRows", "steamSitePoolRows", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def build_existing_site_index(
    *,
    registry_records: list[ExistingSiteRecord] | None = None,
    gsc_rows: list[dict[str, Any]] | None = None,
    site_pool_rows: list[dict[str, Any]] | None = None,
) -> ExistingSiteIndex:
    registry_records = list(registry_records or [])
    by_site: dict[str, ExistingSiteRecord] = {
        record.site_id: record for record in registry_records if record.site_id
    }

    def enrich(row: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(row)
        site_id = _text(_row_get(row, "site_id", "siteId", "Site ID"))
        authority = by_site.get(site_id)
        if not authority:
            return enriched
        if not _text(_row_get(enriched, "steam_app_id", "steamAppId", "Steam App ID", "AppID")):
            if authority.steam_app_id:
                enriched["steam_app_id"] = authority.steam_app_id
        if not _text(_row_get(enriched, "game_id", "gameId")):
            if authority.game_id:
                enriched["game_id"] = authority.game_id
        if not _text(_row_get(enriched, "game_name", "name", "游戏名称", "站点名称")):
            if authority.game_name:
                enriched["game_name"] = authority.game_name
        return enriched

    index = ExistingSiteIndex()
    for record in registry_records:
        index.add(record)
    for row in gsc_rows or []:
        index.add(record_from_gsc_row(enrich(row)))
    for row in site_pool_rows or []:
        index.add(record_from_site_pool_row(enrich(row)))
    return index


def load_existing_site_index(
    *,
    registry_sites_path: Path | None = None,
    registry_games_path: Path | None = None,
    gsc_snapshot_path: Path | None = None,
    site_pool_snapshot_path: Path | None = None,
    gsc_rows: list[dict[str, Any]] | None = None,
    site_pool_rows: list[dict[str, Any]] | None = None,
) -> ExistingSiteIndex:
    registry_records: list[ExistingSiteRecord] = []
    if registry_sites_path and registry_sites_path.exists():
        registry_records = load_registry_records(registry_sites_path, registry_games_path)
    return build_existing_site_index(
        registry_records=registry_records,
        gsc_rows=gsc_rows if gsc_rows is not None else load_json_rows(gsc_snapshot_path),
        site_pool_rows=(
            site_pool_rows
            if site_pool_rows is not None
            else load_json_rows(site_pool_snapshot_path)
        ),
    )


def _candidate_identity(candidate: dict[str, Any]) -> dict[str, str]:
    state = _as_dict(candidate.get("candidate_state"))
    return {
        "steam_app_id": _text(
            candidate.get("steam_app_id")
            or candidate.get("steamAppId")
            or candidate.get("appId")
            or state.get("steam_app_id")
        ),
        "site_id": _text(candidate.get("site_id") or candidate.get("siteId") or state.get("site_id")),
        "game_id": _text(candidate.get("game_id") or candidate.get("gameId") or state.get("game_id")),
        "game_name": _text(
            candidate.get("game_name")
            or candidate.get("name")
            or candidate.get("游戏名称")
        ),
    }


def _unique_records(records: list[ExistingSiteRecord]) -> list[ExistingSiteRecord]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[ExistingSiteRecord] = []
    for record in records:
        key = (record.source, record.site_id, record.steam_app_id or record.game_name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def match_existing_sites(
    candidate: dict[str, Any],
    index: ExistingSiteIndex,
) -> list[ExistingSiteRecord]:
    """Match a candidate against the index using the required identity priority."""
    identity = _candidate_identity(candidate)
    app_id = identity["steam_app_id"]
    if app_id:
        return _unique_records(list(index.by_app_id.get(app_id) or []))

    matches: list[ExistingSiteRecord] = []
    site_id = identity["site_id"]
    game_id = identity["game_id"]
    if site_id:
        matches.extend(index.by_site_id.get(site_id) or [])
        matches.extend(index.by_site_id.get(compact_identity(site_id)) or [])
    if game_id:
        matches.extend(index.by_game_id.get(game_id) or [])
        matches.extend(index.by_game_id.get(compact_identity(game_id)) or [])
    if matches:
        return _unique_records(matches)

    name = normalize_game_name(identity["game_name"])
    compact = compact_identity(identity["game_name"])
    if name:
        matches.extend(index.by_name.get(name) or [])
    if compact:
        matches.extend(index.by_name.get(compact) or [])
    return _unique_records(matches)


def _preferred_status(matches: list[ExistingSiteRecord]) -> str:
    # Registry lifecycle is the canonical phase for next-action routing.
    # GSC Enabled proves existence but must not rewrite PREVIEW_READY → LIVE.
    for record in matches:
        if record.source == SOURCE_REGISTRY and normalize_lifecycle_phase(record.status):
            return normalize_lifecycle_phase(record.status)
    priority = {"PRODUCTION": 4, "LIVE": 3, "PREVIEW_READY": 2, "GENERATED": 1}
    best = ""
    best_score = -1
    for record in matches:
        phase = normalize_lifecycle_phase(record.status)
        score = priority.get(phase, 0)
        if record.source == SOURCE_GSC and record.enabled:
            score = max(score, priority["LIVE"])
            phase = phase or "LIVE"
        if score > best_score:
            best_score = score
            best = phase or record.status
    return best


def evaluate_existing_site(
    candidate: dict[str, Any],
    index: ExistingSiteIndex,
) -> dict[str, Any]:
    """Return exclusion / reconciliation fields for one candidate."""
    matches = match_existing_sites(candidate, index)
    identity = _candidate_identity(candidate)
    decision = _text(
        _as_dict(candidate.get("candidate_state")).get("decision")
        or candidate.get("decision")
        or candidate.get("MachineDecision")
        or candidate.get("machine_recommendation")
    ).upper()
    if decision.startswith("RECOMMEND_"):
        decision = decision.replace("RECOMMEND_", "", 1)

    if not matches:
        return {
            "steam_app_id": identity["steam_app_id"],
            "game_name": identity["game_name"],
            "existingSite": False,
            "eligibleForNewSite": True,
            "matches": [],
            "existingSiteSource": None,
            "existingSiteID": None,
            "existingSiteStatus": None,
            "stateSyncGap": False,
            "state_sync_codes": [],
            "reconciled_decision": decision or None,
            "next_action": None,
            "exclude_from_build": False,
        }

    sources = sorted({record.source for record in matches})
    statuses = sorted({normalize_lifecycle_phase(record.status) or "UNKNOWN" for record in matches})
    site_ids = sorted({record.site_id for record in matches if record.site_id})
    expected_sources = {SOURCE_REGISTRY, SOURCE_GSC, SOURCE_SITE_POOL}
    missing_sources = sorted(expected_sources - set(sources))
    state_sync_gap = len(sources) < 3 or len(statuses) > 1 or bool(missing_sources)
    status = _preferred_status(matches)
    primary = matches[0]
    for preferred_source in (SOURCE_REGISTRY, SOURCE_GSC, SOURCE_SITE_POOL):
        for record in matches:
            if record.source == preferred_source:
                primary = record
                break
        else:
            continue
        break

    # Always reconcile away from NEW/BUILD once an existing site is proven.
    result = {
        "steam_app_id": identity["steam_app_id"],
        "game_name": identity["game_name"],
        "existingSite": True,
        "eligibleForNewSite": False,
        "matches": [record.to_dict() for record in matches],
        "existingSiteSource": ",".join(sources),
        "existingSiteID": primary.site_id or (site_ids[0] if site_ids else None),
        "existingSiteStatus": status or primary.status,
        "stateSyncGap": state_sync_gap,
        "state_sync_codes": [STATE_SYNC_GAP] if state_sync_gap else [],
        "reconciled_decision": ALREADY_BUILT,
        "next_action": next_action_for_lifecycle(status or primary.status),
        "exclude_from_build": True,
        "prior_decision": decision or None,
    }
    return result


def annotate_candidate(
    candidate: dict[str, Any],
    index: ExistingSiteIndex,
) -> dict[str, Any]:
    """Return a shallow-copied candidate with existing-site fields attached."""
    annotated = dict(candidate)
    evaluation = evaluate_existing_site(candidate, index)
    annotated["existing_site"] = evaluation
    annotated["existingSite"] = evaluation["existingSite"]
    annotated["eligibleForNewSite"] = evaluation["eligibleForNewSite"]
    if evaluation["existingSite"]:
        state = dict(_as_dict(annotated.get("candidate_state")))
        if _text(state.get("decision")).upper() == "BUILD" or not _text(state.get("decision")):
            state["decision"] = ALREADY_BUILT
            state["next_action"] = evaluation["next_action"]
            state["existing_site_source"] = evaluation["existingSiteSource"]
            state["existing_site_id"] = evaluation["existingSiteID"]
            state["existing_site_status"] = evaluation["existingSiteStatus"]
            if evaluation["stateSyncGap"]:
                state["state_sync_gap"] = STATE_SYNC_GAP
            annotated["candidate_state"] = state
        annotated["machine_recommendation"] = ALREADY_BUILT
        annotated["recommendation"] = ALREADY_BUILT
    return annotated
