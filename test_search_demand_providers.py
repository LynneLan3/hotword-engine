#!/usr/bin/env python3
"""Offline tests for R3B-0 search provider feasibility helpers."""

from __future__ import annotations

import json
import unittest

import search_demand_providers as sdp


GAME = "Agefield High: Rock the School"
SEEDS = [
    "Agefield High: Rock the School",
    "Agefield High: Rock the School classes",
]


class SchemaTests(unittest.TestCase):
    def test_provider_result_schema(self) -> None:
        row = sdp.provider_result(
            sdp.SOURCE_GOOGLE_AUTOCOMPLETE,
            sdp.STATUS_BEST_EFFORT,
            "q",
            items=[{"text": "a", "kind": "suggestion", "matched_seed_terms": [], "anchor_relevant": False}],
            error=None,
            metadata={"http_status": 200},
        )
        self.assertEqual(
            set(row),
            {"source", "status", "query", "items", "error", "metadata"},
        )
        self.assertIn(row["status"], {sdp.STATUS_SUPPORTED, sdp.STATUS_BEST_EFFORT, sdp.STATUS_UNAVAILABLE})


class NetworkErrorTests(unittest.TestCase):
    def test_network_error_is_unavailable_not_crash(self) -> None:
        def boom(url, headers=None):
            raise ConnectionError("dns fail")

        row = sdp.probe_google_autocomplete("q", fetch_fn=boom)
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)
        self.assertTrue(row["error"])
        self.assertEqual(row["items"], [])

    def test_http_500_unavailable(self) -> None:
        def fetch(url, headers=None):
            return {"ok": False, "status": 500, "body": "nope", "url": url, "error": "HTTPError 500"}

        row = sdp.probe_bing_autocomplete("q", fetch_fn=fetch)
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(row["metadata"]["http_status"], 500)


class ParseGuardTests(unittest.TestCase):
    def test_unknown_html_is_not_paa(self) -> None:
        html = """
        <html><body>
          <p>What is Agefield High?</p>
          <a href="/search?q=how+to+play">How to play Agefield?</a>
          <h3>Is this a question?</h3>
        </body></html>
        """
        self.assertIsNone(sdp.parse_google_paa_html(html))
        row = sdp.probe_google_paa(
            "Agefield High: Rock the School",
            fetch_fn=lambda url, headers=None: {
                "ok": True, "status": 200, "body": html, "url": url, "error": None
            },
        )
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(row["error"], "no_stable_paa_structure")
        self.assertEqual(row["items"], [])

    def test_paa_related_question_pair_is_parsed(self) -> None:
        html = """
        <div class="related-question-pair" data-q="What are the Agefield High classes?">x</div>
        """
        qs = sdp.parse_google_paa_html(html)
        self.assertEqual(qs, ["What are the Agefield High classes?"])

    def test_related_does_not_parse_organic_links(self) -> None:
        html = """
        <html><body>
          <div id="rso">
            <a href="/search?q=agefield+high+walkthrough">Agefield High walkthrough</a>
            <a href="/search?q=agefield+gameplay">gameplay</a>
          </div>
        </body></html>
        """
        self.assertIsNone(sdp.parse_google_related_html(html))
        row = sdp.probe_google_related(
            "Agefield High: Rock the School",
            fetch_fn=lambda url, headers=None: {
                "ok": True, "status": 200, "body": html, "url": url, "error": None
            },
        )
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(row["items"], [])

    def test_related_section_bres_is_parsed(self) -> None:
        html = """
        <div id="bres">
          Related searches
          <a href="/search?q=agefield+high+classes">agefield high classes</a>
          <a href="/search?q=agefield+high+walkthrough">agefield high walkthrough</a>
        </div>
        """
        qs = sdp.parse_google_related_html(html)
        self.assertIn("agefield high classes", qs)
        self.assertIn("agefield high walkthrough", qs)

    def test_bing_maps_response_is_rejected(self) -> None:
        maps = json.dumps(
            {
                "authenticationResultCode": "ValidCredentials",
                "brandLogoUri": "https://dev.virtualearth.net/Branding/logo_powered_by.png",
                "resourceSets": [
                    {
                        "resources": [
                            {
                                "value": [
                                    {
                                        "__type": "Place",
                                        "address": {
                                            "locality": "Seattle",
                                            "countryRegion": "US",
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        )
        self.assertTrue(sdp.is_bing_maps_payload(maps))
        row = sdp.probe_bing_autocomplete(
            "Agefield High",
            fetch_fn=lambda url, headers=None: {
                "ok": True, "status": 200, "body": maps, "url": url, "error": None
            },
        )
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(row["error"], "bing_maps_payload_rejected")
        self.assertEqual(row["items"], [])

    def test_bing_osjson_is_accepted(self) -> None:
        body = json.dumps(["Agefield High", ["Agefield High classes", "Agefield High walkthrough"]])
        row = sdp.probe_bing_autocomplete(
            "Agefield High",
            seed_terms=SEEDS,
            fetch_fn=lambda url, headers=None: {
                "ok": True, "status": 200, "body": body, "url": url, "error": None
            },
        )
        self.assertEqual(row["status"], sdp.STATUS_BEST_EFFORT)
        self.assertEqual(len(row["items"]), 2)


class RelevanceAndDedupeTests(unittest.TestCase):
    def test_anchor_relevance(self) -> None:
        self.assertTrue(sdp.is_anchor_relevant("Agefield High classes"))
        self.assertTrue(sdp.is_anchor_relevant("Agefield High: Rock the School classes"))
        self.assertFalse(sdp.is_anchor_relevant("Agefield High walkthrough"))
        self.assertFalse(sdp.is_anchor_relevant("Agefield High gameplay"))
        self.assertFalse(sdp.is_anchor_relevant("Agefield High release date"))
        self.assertFalse(sdp.is_anchor_relevant("Agefield High trailer"))
        self.assertFalse(sdp.is_anchor_relevant("Agefield High Bully comparison"))

    def test_dedupe_suggestions(self) -> None:
        items = sdp.annotate_items(
            ["Agefield High classes", "agefield high classes", "Agefield High walkthrough"],
            sdp.KIND_SUGGESTION,
            SEEDS,
        )
        texts = [it["text"] for it in items]
        self.assertEqual(len(texts), 2)
        self.assertTrue(items[0]["anchor_relevant"])
        self.assertFalse(items[1]["anchor_relevant"])


class IsolationTests(unittest.TestCase):
    def test_one_provider_failure_does_not_stop_others(self) -> None:
        ac = json.dumps(["q", ["Agefield High classes"]])

        def fetch(url, headers=None):
            if "suggestqueries.google.com" in url:
                return {"ok": True, "status": 200, "body": ac, "url": url, "error": None}
            if "api.bing.com" in url:
                raise TimeoutError("bing down")
            if "google.com/search" in url:
                return {"ok": True, "status": 200, "body": "<html>no structure</html>", "url": url, "error": None}
            raise AssertionError(url)

        rows = sdp.probe_all_sources("Agefield High: Rock the School", seed_terms=SEEDS, fetch_fn=fetch)
        by = {r["source"]: r for r in rows}
        self.assertEqual(len(rows), 4)
        self.assertEqual(by[sdp.SOURCE_GOOGLE_AUTOCOMPLETE]["status"], sdp.STATUS_BEST_EFFORT)
        self.assertEqual(by[sdp.SOURCE_BING_AUTOCOMPLETE]["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(by[sdp.SOURCE_GOOGLE_PAA]["status"], sdp.STATUS_UNAVAILABLE)
        self.assertEqual(by[sdp.SOURCE_GOOGLE_RELATED]["status"], sdp.STATUS_UNAVAILABLE)

    def test_captcha_html_unavailable(self) -> None:
        html = "<html>/sorry/index unusual traffic from your computer</html>"
        row = sdp.probe_google_autocomplete(
            "q",
            fetch_fn=lambda url, headers=None: {
                "ok": True, "status": 200, "body": html, "url": url, "error": None
            },
        )
        self.assertEqual(row["status"], sdp.STATUS_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
