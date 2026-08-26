"""Focused tests for the universal Game x Event schema contract."""

from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from opportunity_discovery import (
    CandidateSignal,
    CanonicalRecords,
    EventType,
    GameEntity,
    GameEvent,
    Platform,
    PlatformListing,
    SchemaValidationError,
    SourceAdapter,
    make_event_id,
    make_game_entity_id,
    make_platform_listing_id,
    make_signal_id,
    normalize_aliases,
    steam_candidate_to_records,
)


def _steam_record() -> dict[str, Any]:
    return {
        "job_id": "steam-research-example-20260820",
        "steam_app_id": "1000001",
        "game_name": "Example Survival Game",
        "steam_url": "https://store.steampowered.com/app/1000001/",
        "created_at": "2026-08-21T09:00:00+00:00",
        "steam_signals": {
            "followers": 1200,
            "followers_gain_7d": 300,
            "release_date": "2026-08-20",
        },
    }


class UniversalSchemaTests(unittest.TestCase):
    def test_deterministic_game_entity_id_and_alias_normalization(self):
        self.assertEqual(make_game_entity_id("Soul's Remnant"), make_game_entity_id("Souls Remnant"))
        self.assertEqual(normalize_aliases(["Soul's Remnant", "Souls Remnant", "  SOULS REMNANT  "]), ("souls remnant",))

    def test_deterministic_platform_listing_id(self):
        entity_id = make_game_entity_id("Example Survival Game")
        first = make_platform_listing_id(entity_id, Platform.STEAM, "1000001")
        second = make_platform_listing_id(entity_id, "STEAM", "1000001")
        self.assertEqual(first, second)

    def test_same_game_different_platform_shares_entity(self):
        entity = GameEntity.create("Example Survival Game")
        steam = PlatformListing.create(entity.game_entity_id, Platform.STEAM, "1000001")
        switch_2 = PlatformListing.create(entity.game_entity_id, Platform.NINTENDO_SWITCH_2, "switch-2-100")
        self.assertEqual(steam.game_entity_id, switch_2.game_entity_id)
        self.assertNotEqual(steam.platform_listing_id, switch_2.platform_listing_id)

    def test_same_game_different_events_have_different_ids(self):
        entity = GameEntity.create("Example Survival Game")
        listing = PlatformListing.create(entity.game_entity_id, Platform.STEAM, "1000001")
        release = GameEvent.create(entity.game_entity_id, EventType.NEW_RELEASE, date(2026, 8, 20), "STEAM", "1000001", listing.platform_listing_id)
        dlc = GameEvent.create(entity.game_entity_id, EventType.DLC, date(2026, 9, 20), "STEAM", "dlc-1", listing.platform_listing_id)
        self.assertEqual(release.game_entity_id, dlc.game_entity_id)
        self.assertNotEqual(release.event_id, dlc.event_id)
        self.assertEqual(release.event_id, make_event_id(entity.game_entity_id, "NEW_RELEASE", "2026-08-20", "STEAM", "1000001", listing.platform_listing_id))

    def test_json_round_trip_for_all_objects(self):
        entity = GameEntity.create("Example Survival Game", aliases=["Example's Survival Game"], genres=["SURVIVAL"])
        listing = PlatformListing.create(entity.game_entity_id, Platform.STEAM, "1000001", release_date=date(2026, 8, 20))
        event = GameEvent.create(entity.game_entity_id, EventType.NEW_RELEASE, date(2026, 8, 20), "STEAM", "1000001", listing.platform_listing_id)
        signal = CandidateSignal.create(entity.game_entity_id, "2026-08-21T09:00:00+00:00", "STEAM", "STEAM_FOLLOWERS", 1200, event.event_id, listing.platform_listing_id, metadata={"region": "US"})
        for item, loader in ((entity, GameEntity.from_dict), (listing, PlatformListing.from_dict), (event, GameEvent.from_dict), (signal, CandidateSignal.from_dict)):
            encoded = item.to_json()
            self.assertEqual(json.loads(encoded), item.to_dict())
            self.assertEqual(loader(json.loads(encoded)).to_dict(), item.to_dict())

    def test_invalid_platform_rejected(self):
        with self.assertRaises(SchemaValidationError):
            PlatformListing("pl", "ge", "NOKIA", "1")

    def test_invalid_event_type_rejected(self):
        with self.assertRaises(SchemaValidationError):
            GameEvent("ev", "ge", "NOT_AN_EVENT", "2026-08-20", "source", "ref")

    def test_required_fields_rejected(self):
        with self.assertRaises(SchemaValidationError):
            GameEntity("ge", "")
        with self.assertRaises(SchemaValidationError):
            PlatformListing("pl", "", Platform.STEAM, "1")
        with self.assertRaises(SchemaValidationError):
            GameEvent("ev", "ge", "", "2026-08-20", "source", "ref")
        with self.assertRaises(SchemaValidationError):
            CandidateSignal("sig", "ge", None, "source", "SIGNAL", 1)

    def test_steam_compatibility_fixture(self):
        records = steam_candidate_to_records(_steam_record())
        self.assertEqual(len(records.game_entities), 1)
        self.assertEqual(records.platform_listings[0].platform, Platform.STEAM)
        self.assertEqual(records.events[0].event_type, EventType.NEW_RELEASE)
        self.assertEqual({signal.signal_type for signal in records.signals}, {"STEAM_FOLLOWERS", "STEAM_7D_GAIN"})
        self.assertTrue(all(signal.game_entity_id == records.game_entities[0].game_entity_id for signal in records.signals))
        self.assertTrue(all(signal.event_id == records.events[0].event_id for signal in records.signals))

    def test_example_fixture_is_a_json_contract(self):
        path = Path(__file__).parent / "tests" / "fixtures" / "example_universal_candidate.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(fixture), {"game_entities", "platform_listings", "events", "signals"})
        entity = GameEntity.from_dict(fixture["game_entities"][0])
        listing = PlatformListing.from_dict(fixture["platform_listings"][0])
        event = GameEvent.from_dict(fixture["events"][0])
        self.assertEqual(entity.to_dict(), fixture["game_entities"][0])
        self.assertEqual(listing.to_dict(), fixture["platform_listings"][0])
        self.assertEqual(event.to_dict(), fixture["events"][0])
        self.assertEqual(entity.game_entity_id, make_game_entity_id(entity.canonical_name))
        self.assertEqual(listing.platform_listing_id, make_platform_listing_id(listing.game_entity_id, listing.platform, listing.platform_game_id))
        self.assertEqual(event.game_entity_id, entity.game_entity_id)
        for signal in fixture["signals"]:
            self.assertEqual(CandidateSignal.from_dict(signal).to_dict(), signal)

    def test_dummy_source_adapter_satisfies_contract(self):
        class DummyAdapter:
            source_name = "DUMMY"

            def collect(self):
                return [{"game_name": "Dummy Game"}]

            def normalize(self, raw_record):
                entity = GameEntity.create(raw_record["game_name"])
                return CanonicalRecords(game_entities=(entity,))

        adapter: SourceAdapter = DummyAdapter()
        self.assertEqual(adapter.source_name, "DUMMY")
        self.assertEqual(adapter.normalize(next(iter(adapter.collect()))).game_entities[0].canonical_name, "Dummy Game")

    def test_signal_id_is_deterministic(self):
        args = ("ge_1", "2026-08-21T09:00:00+00:00", "STEAM", "STEAM_FOLLOWERS", 1200, "ev_1", "pl_1")
        self.assertEqual(make_signal_id(*args), make_signal_id(*args))


if __name__ == "__main__":
    unittest.main()
