"""
온라인학습(learn_from_feedback → L1 Cache upsert → record_learned_outcome) 회귀 테스트.

2026-09-08: 기존에 있었지만 provenance 태깅·품질관리가 없던 이 파이프라인에
source="online_learning" 태깅과 success_count/failure_count 기반 반복실패
자동제거를 추가하면서 신규 작성. 커버 범위:
  - _build_response_from_meta / _ensemble_vote가 L1_CACHE 히트 시 doc_id·source를
    AgentResponse까지 정확히 흘려보내는지 (record_learned_outcome이 되먹일 대상을
    특정하는 데 필수)
  - learn_from_feedback이 source 태깅 + 반복 성공 카운터 누적을 하는지
  - record_learned_outcome이 카운터를 갱신하고, 반복 실패 시에만 엔트리를 삭제하며,
    온라인학습이 아닌(큐레이션된) 엔트리는 절대 건드리지 않는지
  - log_watcher가 L1_CACHE + online_learning 히트에만 되먹임을 호출하는지
"""
import unittest
from unittest.mock import MagicMock, patch

from src.schemas import ActionType, AgentResponse


def _make_engine():
    """RAGEngine을 mock ChromaDB collection과 함께 생성한다 (test_final_comprehensive.py와 동일 패턴)."""
    patcher_client = patch("src.llm_engine._get_chroma_client")
    patcher_warmup = patch("src.llm_engine._ollama_warmup")
    mock_client = patcher_client.start()
    patcher_warmup.start()

    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_client.return_value.get_collection.side_effect = Exception("no col")
    mock_client.return_value.get_or_create_collection.return_value = mock_col

    from src.llm_engine import RAGEngine
    engine = RAGEngine()

    return engine, mock_col, (patcher_client, patcher_warmup)


class TestBuildResponseFromMetaCarriesDocId(unittest.TestCase):
    def tearDown(self):
        pass

    def test_doc_id_and_source_propagated(self):
        from src.llm_engine import _build_response_from_meta
        meta = {
            "action_type":    "execute_llm_command",
            "error_category": "Learned_from_LLM",
            "command":        "systemctl restart nginx",
            "reasoning":      "test",
            "source":         "online_learning",
        }
        resp = _build_response_from_meta(meta, "L1_CACHE", doc_id="learned_abc123")
        self.assertEqual(resp.l1_doc_id, "learned_abc123")
        self.assertEqual(resp.l1_source, "online_learning")

    def test_doc_id_defaults_to_none(self):
        from src.llm_engine import _build_response_from_meta
        meta = {"action_type": "kill_process", "error_category": "OOM", "reasoning": "t"}
        resp = _build_response_from_meta(meta, "L1_CACHE")
        self.assertIsNone(resp.l1_doc_id)
        self.assertIsNone(resp.l1_source)


class TestEnsembleVoteReturnsWinningId(unittest.TestCase):
    def test_majority_action_id_returned(self):
        from src.llm_engine import _ensemble_vote
        candidates = [
            ({"action_type": "restart_service"}, 0.3, "id_a", "doc a text"),
            ({"action_type": "restart_service"}, 0.1, "id_b", "doc b text"),  # 더 가까움 → 이게 선택돼야 함
            ({"action_type": "kill_process"},    0.05, "id_c", "doc c text"),  # 소수 액션, 거리는 가장 가까움
        ]
        best_meta, best_id, evidence = _ensemble_vote(candidates)
        self.assertEqual(best_meta["action_type"], "restart_service")
        self.assertEqual(best_id, "id_b")
        self.assertIn("restart_service", evidence)
        self.assertIn("doc b text", evidence)

    def test_single_candidate(self):
        from src.llm_engine import _ensemble_vote
        candidates = [({"action_type": "alert_only"}, 0.2, "solo_id", "solo doc text")]
        best_meta, best_id, evidence = _ensemble_vote(candidates)
        self.assertEqual(best_id, "solo_id")
        self.assertIn("solo doc text", evidence)


class TestLearnFromFeedbackTagging(unittest.TestCase):
    def setUp(self):
        self.engine, self.mock_col, self._patchers = _make_engine()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_first_learn_tags_source_and_counts(self):
        self.mock_col.get.return_value = {"ids": []}
        self.engine.learn_from_feedback("ERROR: disk full on /data", "rm -rf /tmp/cache")

        _, kwargs = self.mock_col.upsert.call_args
        meta = kwargs["metadatas"][0]
        self.assertEqual(meta["source"], "online_learning")
        self.assertEqual(meta["success_count"], 1)
        self.assertEqual(meta["failure_count"], 0)
        self.assertEqual(meta["error_category"], "Learned_from_LLM")

    def test_repeated_same_command_increments_success_count(self):
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{
                "command": "systemctl restart nginx",
                "source": "online_learning",
                "success_count": 3,
                "failure_count": 1,
            }],
        }
        self.engine.learn_from_feedback("ERROR: nginx down", "systemctl restart nginx")

        _, kwargs = self.mock_col.upsert.call_args
        meta = kwargs["metadatas"][0]
        self.assertEqual(meta["success_count"], 4)
        self.assertEqual(meta["failure_count"], 1)  # 실패 카운터는 안 건드림

    def test_different_command_resets_counters(self):
        """같은 에러에 다른 커맨드가 학습되면(새 해결책으로 대체) 카운터를 리셋한다."""
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{
                "command": "systemctl restart nginx",
                "source": "online_learning",
                "success_count": 5,
                "failure_count": 3,
            }],
        }
        self.engine.learn_from_feedback("ERROR: nginx down", "systemctl restart httpd")

        _, kwargs = self.mock_col.upsert.call_args
        meta = kwargs["metadatas"][0]
        self.assertEqual(meta["success_count"], 1)
        self.assertEqual(meta["failure_count"], 0)


class TestRecordLearnedOutcome(unittest.TestCase):
    def setUp(self):
        self.engine, self.mock_col, self._patchers = _make_engine()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_ignores_missing_entry(self):
        self.mock_col.get.return_value = {"ids": []}
        self.engine.record_learned_outcome("learned_gone", success=False)
        self.mock_col.update.assert_not_called()
        self.mock_col.delete.assert_not_called()

    def test_ignores_non_online_learning_entry(self):
        """큐레이션된 데이터(GitHub 크롤링·카오스 시그니처 등)는 런타임 피드백으로 절대 안 건드린다."""
        self.mock_col.get.return_value = {
            "ids": ["curated_1"],
            "metadatas": [{"source": "chaos_injector_signature", "success_count": 0, "failure_count": 0}],
        }
        self.engine.record_learned_outcome("curated_1", success=False)
        self.mock_col.update.assert_not_called()
        self.mock_col.delete.assert_not_called()

    def test_success_increments_and_updates(self):
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{"source": "online_learning", "success_count": 2, "failure_count": 0}],
        }
        self.engine.record_learned_outcome("learned_x", success=True)

        self.mock_col.update.assert_called_once()
        _, kwargs = self.mock_col.update.call_args
        self.assertEqual(kwargs["metadatas"][0]["success_count"], 3)
        self.mock_col.delete.assert_not_called()

    def test_single_failure_does_not_delete(self):
        """실패 1건만으론 안 지운다 — 우연한 실패 방어(_LEARNED_ENTRY_MAX_FAILURES=2 기본값)."""
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{"source": "online_learning", "success_count": 5, "failure_count": 0}],
        }
        self.engine.record_learned_outcome("learned_x", success=False)

        self.mock_col.delete.assert_not_called()
        self.mock_col.update.assert_called_once()
        self.assertEqual(self.mock_col.update.call_args.kwargs["metadatas"][0]["failure_count"], 1)

    def test_repeated_failures_over_threshold_deletes_entry(self):
        """실패 건수(>=2) AND 실패율(>0.5) 둘 다 넘으면 삭제."""
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{"source": "online_learning", "success_count": 1, "failure_count": 1}],
        }
        self.engine.record_learned_outcome("learned_x", success=False)

        self.mock_col.delete.assert_called_once_with(ids=["learned_x"])
        self.mock_col.update.assert_not_called()

    def test_high_failure_count_but_low_rate_not_deleted(self):
        """실패 건수는 많아도 성공이 훨씬 많아 실패율이 낮으면 안 지운다."""
        self.mock_col.get.return_value = {
            "ids": ["learned_x"],
            "metadatas": [{"source": "online_learning", "success_count": 20, "failure_count": 1}],
        }
        self.engine.record_learned_outcome("learned_x", success=False)

        self.mock_col.delete.assert_not_called()
        self.mock_col.update.assert_called_once()


class TestLogWatcherRecordLearnedOutcomeWiring(unittest.TestCase):
    """log_watcher가 L1_CACHE + online_learning 히트에만 되먹임을 호출하는지 확인."""

    def _make_handler(self):
        from src.log_watcher import LogTailHandler
        from src.utils.debouncer import LogDebouncer

        mock_engine   = MagicMock()
        mock_executor = MagicMock()
        mock_observer = MagicMock()
        mock_breaker  = MagicMock()
        mock_breaker.can_proceed.return_value = True

        handler = LogTailHandler(
            "/tmp/nonexistent_test_log_for_online_learning_tests.log",
            LogDebouncer(cooldown_seconds=0),
            executor=mock_executor,
            observer_agent=mock_observer,
            engine=mock_engine,
            circuit_breaker=mock_breaker,
        )
        return handler, mock_engine, mock_executor

    def test_l1_online_learning_hit_triggers_feedback(self):
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = AgentResponse(
            error_category="Learned_from_LLM", severity="HIGH",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="test", resolution_source="L1_CACHE",
            command="systemctl restart nginx",
            l1_doc_id="learned_abc", l1_source="online_learning",
        )
        mock_executor.execute.return_value = {
            "success": True, "result_category": "SUCCESS",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR nginx down")
        mock_engine.record_learned_outcome.assert_called_once_with("learned_abc", True)

    def test_l1_curated_hit_does_not_trigger_feedback(self):
        """큐레이션 데이터(source가 online_learning이 아님)는 되먹임 대상 아님."""
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = AgentResponse(
            error_category="Out_Of_Memory", severity="HIGH",
            action_type=ActionType.CLEAR_MEMORY,
            reasoning="test", resolution_source="L1_CACHE",
            l1_doc_id="chaos_sig_1", l1_source="chaos_injector_signature",
        )
        mock_executor.execute.return_value = {
            "success": True, "result_category": "SUCCESS",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR oom")
        mock_engine.record_learned_outcome.assert_not_called()

    def test_l2_hit_does_not_trigger_feedback(self):
        """애초에 L1 히트가 아니면(L2_LLM) 되먹임 대상 아님 — learn_from_feedback 쪽 로직."""
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = AgentResponse(
            error_category="LLM_Inferred", severity="HIGH",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="test", resolution_source="L2_LLM",
            command="systemctl restart nginx",
        )
        mock_executor.execute.return_value = {
            "success": True, "result_category": "SUCCESS",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR nginx down")
        mock_engine.record_learned_outcome.assert_not_called()

    def test_observed_only_does_not_trigger_feedback_even_if_online_learning(self):
        handler, mock_engine, mock_executor = self._make_handler()
        mock_engine.analyze_error.return_value = AgentResponse(
            error_category="Learned_from_LLM", severity="HIGH",
            action_type=ActionType.EXECUTE_LLM_COMMAND,
            reasoning="test", resolution_source="L1_CACHE",
            command="systemctl restart nginx",
            l1_doc_id="learned_abc", l1_source="online_learning",
        )
        mock_executor.execute.return_value = {
            "success": True, "result_category": "OBSERVED_ONLY",
            "error_type": None, "error_detail": "",
        }
        handler.trigger_agent_pipeline("ERROR nginx down")
        mock_engine.record_learned_outcome.assert_not_called()


if __name__ == "__main__":
    unittest.main()
