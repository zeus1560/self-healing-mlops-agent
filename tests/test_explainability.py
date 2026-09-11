"""
tests/test_explainability.py

L1 캐시 히트 시 앙상블 투표 근거(어떤 과거 사건들과 얼마나 비슷해서 이 조치를
골랐는지)를 사람이 검증 가능하게 보여주는 Explainability 기능(2026-09-11 추가,
docs/SRE_PRACTICES.md/project-capstone-pivot 참고) 회귀 테스트.

커버 범위:
  - _format_evidence()가 투표 요약·거리순 정렬·승자 표시를 올바르게 만드는지
  - _ensemble_vote()가 evidence를 3번째 값으로 반환하는지
  - RAGEngine.analyze_error()가 L1 히트 시 AgentResponse.l1_evidence를 채우는지,
    L1 미스(L2 폴백) 시에는 None으로 남는지
  - executor._compose_explanation()이 reasoning+l1_evidence를 올바르게 합치는지
  - SlackChatOps.send_approval_request가 explanation을 실제 메시지 블록에 포함하는지
    (2026-09-10까지는 "설명" 섹션에 승인 링크 URL만 보이고 판단 근거 자체는 승인
    화면 어디에도 안 보이고 있었음 — 이번에 고친 실제 버그의 회귀 테스트)
"""
import unittest
from unittest.mock import MagicMock, patch


class TestFormatEvidence(unittest.TestCase):
    def test_marks_winning_candidate(self):
        from src.llm_engine import _format_evidence

        candidates = [
            ({"action_type": "clear_memory"}, 0.30, "id_a", "second closest match text"),
            ({"action_type": "clear_memory"}, 0.10, "id_b", "closest match text"),
            ({"action_type": "kill_process"}, 0.05, "id_c", "minority vote but closest"),
        ]
        text = _format_evidence(candidates, top_action="clear_memory", best_id="id_b")

        self.assertIn("2개가 'clear_memory' 선택", text)
        lines = text.splitlines()
        winner_line = next(ln for ln in lines if "closest match text" in ln and "second" not in ln)
        self.assertTrue(winner_line.strip().startswith("✓"))

    def test_sorted_by_distance_ascending(self):
        from src.llm_engine import _format_evidence

        candidates = [
            ({"action_type": "restart_service"}, 0.50, "id_far", "far doc"),
            ({"action_type": "restart_service"}, 0.05, "id_near", "near doc"),
        ]
        text = _format_evidence(candidates, top_action="restart_service", best_id="id_near")
        near_idx = text.index("near doc")
        far_idx = text.index("far doc")
        self.assertLess(near_idx, far_idx)

    def test_truncates_long_document_text(self):
        from src.llm_engine import _format_evidence, _EVIDENCE_SNIPPET_LEN

        long_text = "x" * 500
        candidates = [({"action_type": "alert_only"}, 0.1, "id_1", long_text)]
        text = _format_evidence(candidates, top_action="alert_only", best_id="id_1")
        self.assertNotIn("x" * (_EVIDENCE_SNIPPET_LEN + 1), text)


class TestTrackRecordAndSourceLabel(unittest.TestCase):
    """
    신뢰도 판단 보강(2026-09-12 추가) — 온라인학습 문서는 실제 실행 트랙 레코드
    (success_count/failure_count)가 있고, 큐레이션/크롤링 문서는 출처 라벨만 있다.
    이미 존재하던 데이터(success_count/failure_count, source)를 근거 텍스트에
    노출하기만 하는 확장이라 저장 스키마는 안 건드린다.
    """

    def test_online_learning_shows_track_record(self):
        from src.llm_engine import _format_track_record

        meta = {"source": "online_learning", "success_count": 11, "failure_count": 1}
        text = _format_track_record(meta)
        self.assertIn("12회 실행", text)
        self.assertIn("11회 성공", text)

    def test_online_learning_with_no_history_yet(self):
        from src.llm_engine import _format_track_record

        meta = {"source": "online_learning", "success_count": 0, "failure_count": 0}
        self.assertEqual(_format_track_record(meta), " [온라인학습, 실행 이력 없음]")

    def test_curated_source_has_no_track_record(self):
        from src.llm_engine import _format_track_record

        meta = {"source": "chaos_injector_signature", "success_count": 99}
        self.assertEqual(_format_track_record(meta), "")

    def test_missing_source_has_no_track_record(self):
        from src.llm_engine import _format_track_record

        self.assertEqual(_format_track_record({}), "")

    def test_evidence_includes_source_label_for_curated_doc(self):
        from src.llm_engine import _format_evidence

        candidates = [
            ({"action_type": "clear_memory", "source": "chaos_injector_signature"},
             0.05, "id_1", "OOM killed process"),
        ]
        text = _format_evidence(candidates, top_action="clear_memory", best_id="id_1")
        self.assertIn("사람이 직접 큐레이션", text)

    def test_evidence_includes_track_record_for_online_learning_doc(self):
        from src.llm_engine import _format_evidence

        candidates = [
            ({"action_type": "restart_service", "source": "online_learning",
              "success_count": 3, "failure_count": 0},
             0.05, "id_1", "learned solution text"),
        ]
        text = _format_evidence(candidates, top_action="restart_service", best_id="id_1")
        self.assertIn("3회 실행 중 3회 성공", text)

    def test_evidence_omits_suffix_for_untagged_doc(self):
        """train_set.json 초기 시딩 문서처럼 source 태그가 전혀 없는 경우 — 빈 접미사."""
        from src.llm_engine import _format_evidence

        candidates = [({"action_type": "alert_only"}, 0.05, "id_1", "seed doc")]
        text = _format_evidence(candidates, top_action="alert_only", best_id="id_1")
        line = next(ln for ln in text.splitlines() if "seed doc" in ln)
        self.assertTrue(line.strip().endswith("seed doc"))


class TestEnsembleVoteEvidenceIntegration(unittest.TestCase):
    def test_returns_three_tuple(self):
        from src.llm_engine import _ensemble_vote

        candidates = [({"action_type": "alert_only"}, 0.1, "id_1", "doc text")]
        result = _ensemble_vote(candidates)
        self.assertEqual(len(result), 3)
        _, _, evidence = result
        self.assertIsInstance(evidence, str)
        self.assertTrue(evidence)


class TestAnalyzeErrorPopulatesEvidence(unittest.TestCase):
    """RAGEngine.analyze_error()가 실제로 l1_evidence를 끝까지 배관하는지 end-to-end 확인."""

    def _make_engine_with_query_result(self, query_return: dict):
        patcher_client = patch("src.llm_engine._get_chroma_client")
        patcher_warmup = patch("src.llm_engine._ollama_warmup")
        mock_client = patcher_client.start()
        self.addCleanup(patcher_client.stop)
        patcher_warmup.start()
        self.addCleanup(patcher_warmup.stop)

        mock_col = MagicMock()
        mock_col.query.return_value = query_return
        mock_client.return_value.get_collection.return_value = mock_col

        from src.llm_engine import RAGEngine
        return RAGEngine()

    def test_l1_hit_populates_l1_evidence(self):
        engine = self._make_engine_with_query_result({
            "documents": [["CRITICAL chaos-injector: OOM killed process"]],
            "metadatas": [[{"action_type": "clear_memory", "error_category": "Out_Of_Memory"}]],
            "distances": [[0.05]],
            "ids": [["chaos_sig_v1_abc"]],
        })
        resp = engine.analyze_error("CRITICAL chaos-injector: OOM killed process")
        self.assertEqual(resp.resolution_source, "L1_CACHE")
        self.assertIsNotNone(resp.l1_evidence)
        self.assertIn("clear_memory", resp.l1_evidence)
        self.assertIn("OOM killed process", resp.l1_evidence)

    def test_l1_miss_leaves_l1_evidence_none(self):
        # 거리가 임계값을 훨씬 초과 — L1 미스로 L2 슬로우 트랙(에스컬레이션 등)으로 감.
        engine = self._make_engine_with_query_result({
            "documents": [["completely unrelated document"]],
            "metadatas": [[{"action_type": "alert_only"}]],
            "distances": [[5.0]],
            "ids": [["irrelevant_id"]],
        })
        with patch.object(engine, "_l2_slow_track") as mock_slow_track:
            from src.schemas import ActionType, AgentResponse
            mock_slow_track.return_value = AgentResponse(
                error_category="Unknown", severity="HIGH",
                action_type=ActionType.ESCALATE_TO_HUMAN,
                resolution_source="L2_LLM",
            )
            resp = engine.analyze_error("some novel error text")
        self.assertIsNone(resp.l1_evidence)


class TestComposeExplanation(unittest.TestCase):
    def test_combines_reasoning_and_l1_evidence(self):
        from src.executor import _compose_explanation
        from src.schemas import ActionType, AgentResponse

        decision = AgentResponse(
            error_category="Out_Of_Memory", severity="HIGH",
            action_type=ActionType.CLEAR_MEMORY,
            reasoning="L1 캐시 히트",
            l1_evidence="L1 앙상블: 후보 3개 중 2개가 'clear_memory' 선택(다수결)",
        )
        text = _compose_explanation(decision)
        self.assertIn("L1 캐시 히트", text)
        self.assertIn("clear_memory", text)

    def test_handles_missing_l1_evidence(self):
        from src.executor import _compose_explanation
        from src.schemas import ActionType, AgentResponse

        decision = AgentResponse(
            error_category="LLM_Inferred", severity="CRITICAL",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="Groq 추론 성공",
        )
        self.assertEqual(_compose_explanation(decision), "Groq 추론 성공")

    def test_handles_empty_reasoning_and_evidence(self):
        from src.executor import _compose_explanation
        from src.schemas import ActionType, AgentResponse

        decision = AgentResponse(
            error_category="Unknown", severity="LOW", action_type=ActionType.ALERT_ONLY,
        )
        self.assertEqual(_compose_explanation(decision), "")


class TestSlackApprovalMessageIncludesExplanation(unittest.TestCase):
    """
    2026-09-10까지 실제로 있던 버그의 회귀 테스트: send_approval_request의 reason
    파라미터는 승인/거절 링크 추출 전용이라, 판단 근거(reasoning/l1_evidence)가
    승인 메시지 어디에도 보이지 않고 있었음 — explanation 파라미터로 분리해 고쳤다.
    """

    def test_explanation_appears_in_message_blocks(self):
        from src.slack_bot import SlackChatOps

        client = SlackChatOps()
        client.webhook_url = "https://hooks.slack.com/services/test"

        with patch("src.slack_bot.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            client.send_approval_request(
                error_log="CRITICAL: oom",
                command="systemctl restart nginx",
                reason="🔐 명령어 확인 및 승인: http://localhost:8080/pending/tok123",
                explanation="L1 앙상블: 후보 3개 중 2개가 'restart_service' 선택",
            )
        payload = mock_post.call_args.kwargs["json"]
        block_texts = " ".join(
            b.get("text", {}).get("text", "")
            for b in payload["blocks"]
            if "text" in b
        )
        self.assertIn("restart_service", block_texts)

    def test_no_explanation_omits_section(self):
        from src.slack_bot import SlackChatOps

        client = SlackChatOps()
        client.webhook_url = "https://hooks.slack.com/services/test"

        with patch("src.slack_bot.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            client.send_approval_request(
                error_log="CRITICAL: oom",
                command="systemctl restart nginx",
                reason="🔐 명령어 확인 및 승인: http://localhost:8080/pending/tok123",
            )
        payload = mock_post.call_args.kwargs["json"]
        block_texts = [b for b in payload["blocks"] if "설명" in str(b)]
        self.assertEqual(block_texts, [])


if __name__ == "__main__":
    unittest.main()
