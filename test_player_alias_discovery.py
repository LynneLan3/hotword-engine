#!/usr/bin/env python3
"""Unit tests for player alias discovery ranking and job callback shaping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import player_alias_discovery as core
import player_alias_discovery_job_runner as job_runner
import player_alias_discovery_runner as runner


class PlayerAliasDiscoveryTests(unittest.TestCase):
    def test_patterns_include_main_title_before_colon(self) -> None:
        patterns = core.generate_evidence_patterns("Combolands: Roguelike Citybuilder")
        self.assertIn("Combolands", patterns)
        patterns2 = core.generate_evidence_patterns("ShipShaper: Falconeer Chronicles")
        self.assertIn("ShipShaper", patterns2)

    def test_found_requires_two_source_hits(self) -> None:
        snippets = [
            {
                "source": "reddit",
                "title": "Anyone tried Combolands on Steam?",
                "snippet": "Combolands: Roguelike Citybuilder looks fun",
                "url": "https://www.reddit.com/r/Steam/comments/1",
            },
            {
                "source": "youtube",
                "title": "Combolands Steam Gameplay",
                "snippet": "first look at Combolands",
                "url": "https://www.youtube.com/watch?v=abc",
            },
            {
                "source": "web",
                "title": "Combolands steam game",
                "snippet": "autocomplete",
                "url": "",
            },
        ]
        diags = [
            {"source": "reddit", "ok": True},
            {"source": "youtube", "ok": True},
            {"source": "steam_community", "ok": False},
            {"source": "web", "ok": True},
        ]
        result = core.discover_from_snippets(
            "Combolands: Roguelike Citybuilder",
            snippets,
            diags,
        )
        self.assertEqual(result["alias"], "Combolands")
        self.assertEqual(result["status"], core.STATUS_FOUND)
        self.assertGreaterEqual(result["source_count"], 2)
        self.assertTrue(any("reddit.com" in url for url in result["source_urls"]))

    def test_retrieval_failed_when_all_sources_fail(self) -> None:
        result = core.discover_from_snippets(
            "Tyr",
            [],
            [
                {"source": "reddit", "ok": False},
                {"source": "youtube", "ok": False},
                {"source": "steam_community", "ok": False},
                {"source": "web", "ok": False},
            ],
        )
        self.assertEqual(result["status"], core.STATUS_RETRIEVAL_FAILED)
        self.assertEqual(result["alias"], "")

    def test_no_alias_evidence_when_sources_ok_but_no_hits(self) -> None:
        snippets = [
            {
                "source": "reddit",
                "title": "Unrelated steam news",
                "snippet": "something else",
                "url": "https://www.reddit.com/r/steam/comments/2",
            }
        ]
        result = core.discover_from_snippets(
            "Tyr",
            snippets,
            [{"source": "reddit", "ok": True}, {"source": "youtube", "ok": True}],
        )
        self.assertEqual(result["status"], core.STATUS_NO_EVIDENCE)
        self.assertEqual(result["alias"], "")

    def test_job_runner_callback_includes_urls(self) -> None:
        posted: list[dict] = []

        def collect_fn(game_name: str, app_id: str, steam_url: str) -> dict:
            del steam_url
            self.assertEqual(app_id, "4075620")
            return {
                "snippets": [
                    {
                        "source": "reddit",
                        "title": "Combolands on Steam",
                        "snippet": "Combolands: Roguelike Citybuilder",
                        "url": "https://www.reddit.com/r/games/comments/combolands",
                    },
                    {
                        "source": "youtube",
                        "title": "Combolands Steam Review",
                        "snippet": "game",
                        "url": "https://www.youtube.com/watch?v=combo",
                    },
                ],
                "source_diags": [
                    {"source": "reddit", "ok": True, "httpStatus": 200, "empty": False, "parseCount": 1, "error": ""},
                    {"source": "youtube", "ok": True, "httpStatus": 200, "empty": False, "parseCount": 1, "error": ""},
                    {"source": "steam_community", "ok": False, "httpStatus": 403, "empty": True, "parseCount": 0, "error": "http_403"},
                    {"source": "web", "ok": True, "httpStatus": 200, "empty": False, "parseCount": 1, "error": ""},
                ],
            }

        def post_fn(url: str, body: dict) -> dict:
            posted.append(body)
            return {"ok": True}

        job = {
            "job_id": "alias-discovery-4075620-20260903",
            "job_type": "PLAYER_ALIAS_DISCOVERY",
            "steam_app_id": "4075620",
            "game_name": "Combolands: Roguelike Citybuilder",
            "steam_url": "https://store.steampowered.com/app/4075620/",
            "research_cycle_date": "2026-09-03",
            "created_at": "2026-09-03T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            # Ensure env vars exist for callback path even though post_fn is injected.
            import os

            os.environ["STEAM_CANDIDATE_RESEARCH_API_URL"] = "https://example.test/exec"
            os.environ["STEAM_CANDIDATE_RESEARCH_CALLBACK_TOKEN"] = "token"
            outcome = job_runner.run_job(
                job_path,
                collect_fn=collect_fn,
                post_fn=post_fn,
                root=root,
            )
        self.assertTrue(outcome["callback_ok"])
        self.assertEqual(len(posted), 1)
        body = posted[0]
        self.assertEqual(body["status"], "FOUND")
        self.assertEqual(body["alias"], "Combolands")
        self.assertTrue(body["source_urls"])
        self.assertTrue(any(item.get("url") for item in body["evidence"]))


if __name__ == "__main__":
    unittest.main()
