from __future__ import annotations

import json
import unittest
from unittest import mock

import steam_candidate_research_job_runner as runner


class _Response:
    def __init__(self, status: int, body: str, content_type: str = "application/json") -> None:
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self._body


class _Opener:
    def __init__(self, response):
        self.response = response

    def open(self, *_args, **_kwargs):
        return self.response


class SteamCandidateResearchCallbackTests(unittest.TestCase):
    def test_apps_script_ok_response_is_success(self) -> None:
        with mock.patch.object(
            runner.urllib.request,
            "build_opener",
            return_value=_Opener(_Response(200, json.dumps({"ok": True, "job_id": "j1"}))),
        ):
            result = runner._http_post("https://sheet.example/exec", {"job_id": "j1"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["final_status"], 200)
        self.assertEqual(result["parsed_ok"], True)
        self.assertEqual(result["content_type"], "application/json")

    def test_redirect_records_both_statuses_and_accepts_final_ok(self) -> None:
        redirect = runner.urllib.error.HTTPError(
            "https://sheet.example/exec", 302, "redirect", {"Location": "https://sheet.example/final"}, None
        )
        redirect.read = lambda: b""
        opener = mock.Mock()
        opener.open.side_effect = [redirect, _Response(200, '{"ok": true}')]
        with mock.patch.object(runner.urllib.request, "build_opener", return_value=opener):
            result = runner._http_post("https://sheet.example/exec", {"job_id": "j1"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["redirect_status"], 302)
        self.assertEqual(result["final_status"], 200)

    def test_json_rejection_and_arbitrary_2xx_are_failures(self) -> None:
        for response in (_Response(200, '{"ok": false}'), _Response(204, "")):
            with self.subTest(response=response.status), mock.patch.object(
                runner.urllib.request,
                "build_opener",
                return_value=_Opener(response),
            ):
                result = runner._http_post("https://sheet.example/exec", {"job_id": "j1"})
            self.assertFalse(result["ok"])

    def test_http_failure_keeps_status_and_safe_body_without_headers(self) -> None:
        response = _Response(500, '{"ok": false, "error": "server"}', "application/json")
        with mock.patch.object(
            runner.urllib.request,
            "build_opener",
            return_value=_Opener(response),
        ):
            result = runner._http_post("https://sheet.example/exec", {"token": "secret"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["parsed_ok"], False)
        self.assertNotIn("token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
