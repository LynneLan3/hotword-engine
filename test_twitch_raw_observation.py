"""Deterministic fixture tests for G011 Twitch raw observation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from opportunity_discovery.sources.twitch import (
    TwitchHttpResponse,
    TwitchTopGamesAdapter,
    append_twitch_run,
)


FIXTURE = json.loads((Path(__file__).parent / "tests/fixtures/twitch_top_games.json").read_text(encoding="utf-8"))


class TwitchRawObservationTests(unittest.TestCase):
    def _adapter(self, *, fail_page: int | None = None, limit: int = 5, token_body: str = '{"access_token":"fixture-token"}'):
        calls: list[tuple[str, str | None]] = []

        def token_fetcher(client_id: str, client_secret: str, _timeout: float):
            self.assertEqual((client_id, client_secret), ("client", "secret"))
            return TwitchHttpResponse(200, token_body)

        def api_fetcher(url, headers, _timeout):
            query = parse_qs(urlparse(url).query)
            page = len(calls) + 1
            calls.append((url, headers.get("Authorization")))
            self.assertEqual(headers["Client-Id"], "client")
            if fail_page == page:
                return TwitchHttpResponse(503, "secret-or-token-must-not-appear")
            return TwitchHttpResponse(200, json.dumps(FIXTURE["pages"][page - 1]))

        adapter = TwitchTopGamesAdapter(
            limit, run_id="run-fixture", observed_at="2026-08-28T00:00:00+00:00",
            client_id="client", client_secret="secret",
            token_fetcher=token_fetcher, api_fetcher=api_fetcher,
        )
        return adapter, calls

    def test_mapping_pagination_rank_missingness_duplicates_and_no_downstream_path(self):
        adapter, calls = self._adapter(limit=300)
        artifact = adapter.collect()
        rows = artifact["observations"]
        self.assertEqual([row["global_rank"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual([row["api_page"] for row in rows], [1, 1, 2, 2, 3])
        self.assertEqual(rows[1]["igdb_id"], None)
        self.assertEqual(artifact["run_metadata"]["duplicates"], 1)
        self.assertEqual(artifact["run_metadata"]["unique_twitch_ids"], 4)
        self.assertEqual(artifact["run_metadata"]["missing_igdb_count"], 1)
        self.assertEqual(artifact["run_metadata"]["endpoint"], "https://api.twitch.tv/helix/games/top")
        self.assertEqual(len(artifact["run_metadata"]["page_requests"]), 3)
        self.assertNotIn("secret", json.dumps(artifact))
        self.assertEqual(parse_qs(urlparse(calls[1][0]).query)["after"], ["cursor-1"])
        self.assertEqual(artifact["run_metadata"]["run_status"], "COMPLETE")
        self.assertNotIn("secret", json.dumps(artifact))
        self.assertNotIn("token", json.dumps(artifact).lower())
        self.assertEqual(calls[0][1], "Bearer fixture-token")

    def test_pagination_is_bounded_to_300_rows_and_three_api_pages(self):
        calls = []

        def api_fetcher(url, _headers, _timeout):
            page = len(calls) + 1
            calls.append(url)
            rows = [
                {"id": f"{page}-{index}", "name": f"Game {page}-{index}", "box_art_url": "https://cdn.example/art.jpg"}
                for index in range(100)
            ]
            return TwitchHttpResponse(200, json.dumps({
                "data": rows,
                "pagination": {"cursor": f"cursor-{page}"} if page < 3 else {"cursor": "cursor-too-many"},
            }))

        adapter = TwitchTopGamesAdapter(
            300, run_id="bounded", observed_at="2026-08-28T00:00:00+00:00",
            client_id="client", client_secret="secret",
            token_fetcher=lambda *_: TwitchHttpResponse(200, '{"access_token":"fixture-token"}'),
            api_fetcher=api_fetcher,
        )
        artifact = adapter.collect()
        self.assertEqual(len(artifact["observations"]), 300)
        self.assertEqual(artifact["run_metadata"]["pages_completed"], 3)
        self.assertEqual(len(calls), 3)

    def test_server_over_return_cannot_exceed_requested_limit(self):
        def api_fetcher(_url, _headers, _timeout):
            return TwitchHttpResponse(200, json.dumps({
                "data": [
                    {"id": str(index), "name": f"Game {index}", "box_art_url": "https://cdn.example/art.jpg"}
                    for index in range(100)
                ],
                "pagination": {"cursor": "cursor-ignored"},
            }))

        adapter = TwitchTopGamesAdapter(
            3, run_id="strict-bound", observed_at="2026-08-28T00:00:00+00:00",
            client_id="client", client_secret="secret",
            token_fetcher=lambda *_: TwitchHttpResponse(200, '{"access_token":"fixture-token"}'),
            api_fetcher=api_fetcher,
        )
        artifact = adapter.collect()
        self.assertEqual(len(artifact["observations"]), 3)
        self.assertEqual([row["global_rank"] for row in artifact["observations"]], [1, 2, 3])
        self.assertEqual(artifact["run_metadata"]["requested_limit"], 3)

    def test_partial_failure_retains_previous_pages(self):
        artifact, _ = self._adapter(fail_page=2, limit=5)
        result = artifact.collect()
        self.assertEqual(result["run_metadata"]["run_status"], "PARTIAL")
        self.assertEqual(result["run_metadata"]["pages_completed"], 1)
        self.assertEqual(result["run_metadata"]["rows_returned"], 2)
        self.assertEqual(result["observations"][0]["twitch_game_id"], "10")
        self.assertEqual(result["run_metadata"]["source_errors"][0]["code"], "HTTP_ERROR")

    def test_auth_missing_and_auth_failed_are_not_zero_data_success(self):
        missing = TwitchTopGamesAdapter(
            client_id="", client_secret="", run_id="missing",
            observed_at="2026-08-28T00:00:00+00:00",
        ).collect()
        self.assertEqual(missing["run_metadata"]["run_status"], "AUTH_MISSING")
        self.assertEqual(missing["observations"], [])
        self.assertEqual(missing["run_id"], "missing")
        self.assertEqual(missing["observed_at"], "2026-08-28T00:00:00+00:00")
        self.assertEqual(missing["source"], "TWITCH_HELIX_TOP_GAMES")
        failed, _ = self._adapter(token_body="{}")
        self.assertEqual(failed.collect()["run_metadata"]["run_status"], "AUTH_FAILED")

    def test_append_run_history_does_not_overwrite_previous_run(self):
        artifact, _ = self._adapter(limit=1)
        run = artifact.collect()
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "twitch-history.json"
            append_twitch_run(path, run)
            append_twitch_run(path, {**run, "run_id": "run-two"})
            history = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([item["run_id"] for item in history["runs"]], ["run-fixture", "run-two"])

    def test_environment_credentials_are_read_without_persisting_them(self):
        with mock.patch.dict(os.environ, {"TWITCH_CLIENT_ID": "env-client", "TWITCH_CLIENT_SECRET": "env-secret"}):
            adapter = TwitchTopGamesAdapter(run_id="env", requested_limit=1, token_fetcher=lambda *_: TwitchHttpResponse(200, '{"access_token":"env-token"}'), api_fetcher=lambda *_: TwitchHttpResponse(200, json.dumps(FIXTURE["pages"][0])))
            artifact = adapter.collect()
        self.assertNotIn("env-secret", json.dumps(artifact))
        self.assertNotIn("env-token", json.dumps(artifact))


if __name__ == "__main__":
    unittest.main()
