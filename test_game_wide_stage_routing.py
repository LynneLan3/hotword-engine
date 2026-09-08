import unittest

from game_wide_social_runner import (
    build_content_routing_receipt,
    build_game_wide_callback_body,
    build_research_pack,
    resolve_content_stage,
)


class StageRoutingTest(unittest.TestCase):
    def test_four_stages_are_fact_driven(self):
        self.assertEqual(resolve_content_stage({"is_playable": False})["content_stage"], "PRE_LAUNCH")
        self.assertEqual(resolve_content_stage({"is_playable": True, "launch_signal": True})["content_stage"], "LAUNCH")
        self.assertEqual(
            resolve_content_stage(
                {"is_playable": True, "gsc_context": {"impressions": 12}},
                [{"evidence_count": 2}],
            )["content_stage"],
            "GROWTH",
        )
        self.assertEqual(resolve_content_stage({"is_playable": True})["content_stage"], "STABLE")

    def test_prelaunch_does_not_release_gameplay(self):
        receipt = build_content_routing_receipt(
            {"is_playable": False},
            {"decision": "NEW", "intent_family": "BOSS", "evidence_count": 3, "official_evidence": True},
            resolve_content_stage({"is_playable": False}),
        )
        self.assertEqual(receipt["publish_state"], "RESEARCH_REQUIRED")
        self.assertIn("unverified gameplay", receipt["routing_reason"])
        official = build_content_routing_receipt(
            {"is_playable": False},
            {"decision": "NEW", "intent_family": "FEATURE", "evidence_count": 1, "official_evidence": True},
            resolve_content_stage({"is_playable": False}),
        )
        self.assertEqual(official["publish_state"], "READY_FOR_WRITER")

    def test_launch_gap_and_media_missing(self):
        job = {"is_playable": True, "launch_signal": True}
        stage = resolve_content_stage(job)
        gap = build_content_routing_receipt(
            job,
            {"decision": "NEW", "intent_family": "PROGRESSION", "evidence_count": 1, "source_families": ["COMMUNITY"]},
            stage,
        )
        self.assertEqual(gap["publish_state"], "RESEARCH_REQUIRED")
        ready = build_content_routing_receipt(
            job,
            {"decision": "NEW", "intent_family": "PROGRESSION", "evidence_count": 2, "source_families": ["COMMUNITY", "VIDEO"], "media_state": "MISSING"},
            stage,
        )
        self.assertEqual(ready["publish_state"], "READY_FOR_WRITER")
        self.assertEqual(ready["media_requirement"], "MISSING")

    def test_watch_is_never_writer_ready(self):
        receipt = build_content_routing_receipt(
            {"is_playable": True, "launch_signal": True},
            {"decision": "WATCH", "intent_family": "PROGRESSION", "evidence_count": 3, "source_families": ["COMMUNITY", "VIDEO"]},
            resolve_content_stage({"is_playable": True, "launch_signal": True}),
        )
        self.assertEqual(receipt["publish_state"], "RESEARCH_REQUIRED")

    def test_callback_keeps_stage_and_receipts(self):
        body = build_game_wide_callback_body(
            {
                "job_id": "g036-job",
                "radar_id": "radar-1",
                "discovery_cycle_date": "2026-09-08",
                "content_stage": "LAUNCH",
                "content_stage_reason": "playable first-wave fact",
                "content_stage_evidence": {"playability": True},
                "content_routing_receipts": [{"publish_state": "READY_FOR_WRITER"}],
                "clusters": [{}],
            },
            result_path="jobs/g036-job/game_wide_social_result.json",
        )
        self.assertEqual(body["discovery_scope"], "GAME_WIDE")
        self.assertEqual(body["radar_id"], "radar-1")
        self.assertEqual(body["execution_status"], "COMPLETED")
        self.assertEqual(body["content_stage"], "LAUNCH")
        self.assertEqual(body["content_routing_receipts"][0]["publish_state"], "READY_FOR_WRITER")

    def test_demand_does_not_satisfy_answer_gate(self):
        receipt = build_content_routing_receipt(
            {"is_playable": True, "launch_signal": True},
            {
                "decision": "NEW",
                "intent_family": "PROGRESSION",
                "evidence_count": 4,
                "demand_evidence_count": 4,
                "answer_evidence_count": 0,
                "source_families": ["COMMUNITY", "VIDEO"],
            },
            resolve_content_stage({"is_playable": True, "launch_signal": True}),
        )
        self.assertEqual(receipt["publish_state"], "RESEARCH_REQUIRED")
        self.assertEqual(receipt["article_class"], "PREMIUM_PROBLEM_SOLVING")

    def test_research_pack_is_portable_and_preserves_evidence_split(self):
        demand = {
            "evidence_id": "demand-001",
            "provider": "reddit",
            "source_family": "COMMUNITY",
            "url": "https://example.test/demand",
            "player_question": "Where is the key?",
            "excerpt": "Players are asking where the key is.",
        }
        answer = {
            "evidence_id": "answer-001",
            "source_type": "official",
            "source_ref": "https://example.test/answer",
            "summary": "The official answer identifies the key location.",
            "status": "VERIFIED",
        }
        result = {
            "job_id": "pack-job",
            "game_name": "Example Game",
            "demand_evidence": [demand],
            "answer_evidence": [answer],
            "demand_evidence_count": 1,
            "answer_evidence_count": 1,
            "clusters": [
                {
                    "cluster_id": "cluster-001-key",
                    "topic_key": "key",
                    "topic": "Where is the key?",
                    "representative_questions": ["Where is the key?"],
                    "decision": "NEW",
                    "demand_evidence": [demand],
                    "answer_evidence": [answer],
                    "answer_evidence_count": 1,
                    "content_routing": {"intent_type": "ITEM_LOCATION", "publish_state": "READY_FOR_WRITER"},
                }
            ],
            "content_routing_receipts": [],
        }
        pack = build_research_pack({"job_id": "pack-job", "game_name": "Example Game"}, result)
        self.assertEqual(pack["schemaVersion"], "hotword-research-pack-v1")
        self.assertEqual(pack["schema_version"], "hotword-research-package-v1")
        self.assertEqual(pack["target"]["game_name"], "Example Game")
        self.assertEqual({item["evidence_kind"] for item in pack["search_evidence"]}, {"DEMAND", "ANSWER"})
        self.assertEqual(pack["page_plan"][0]["recommendation"], "INCLUDE")


if __name__ == "__main__":
    unittest.main()
