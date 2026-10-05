"""
에이전트 로깅 설정 회귀 테스트 (2026-10-05).

배경: import 단계(src.telegram_bot 모듈 레벨 싱글톤 생성 시 logging 호출)에서 파이썬이 루트
로거를 WARNING 기본값으로 자동 설정해, log_watcher의 basicConfig(level=INFO)가 무시됐다 —
VM journal에 INFO가 9/21 이후 0줄. configure_logging()이 force=True로 그 상태를 덮어쓰는지,
그리고 INFO를 켜도 httpx가 텔레그램 봇 토큰 URL을 남기지 않는지 실제 stderr 출력으로 확인한다.
"""
import asyncio
import io
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import llm_engine, llm_mode  # noqa: E402
from src.log_watcher import configure_logging  # noqa: E402

FAKE_TOKEN = "123456:FAKE-bot-token-for-test"


class _LoggingCase(unittest.TestCase):
    """VM 상태 재현(설정 전 logging 호출 → WARNING 기본값) + stderr 캡처 공통 준비."""

    def setUp(self):
        root = logging.getLogger()
        self._saved = (root.level, root.handlers[:],
                       {n: logging.getLogger(n).level for n in ("httpx", "httpcore")})
        # VM에서 일어나던 상태 재현: 설정 전 logging 호출 → WARNING 기본값으로 자동 설정
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        self.stderr = io.StringIO()
        with patch.object(sys, "stderr", self.stderr):
            logging.warning("import-time warning (implicit basicConfig 재현)")
        self.assertTrue(root.handlers, "암묵적 기본 설정이 재현되지 않음")

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self._saved[1]
        root.setLevel(self._saved[0])
        for n, lvl in self._saved[2].items():
            logging.getLogger(n).setLevel(lvl)

    def _configure(self):
        with patch.dict(os.environ, {"USE_JSON_LOG": "0"}), patch.object(sys, "stderr", self.stderr):
            configure_logging()


class TestConfigureLogging(_LoggingCase):

    def test_info_reaches_output_after_implicit_warning_config(self):
        self._configure()
        logging.info("INFO-PROBE")
        self.assertIn("INFO-PROBE", self.stderr.getvalue())

    def test_without_force_info_would_be_dropped(self):
        """버그 재현 확인: force 없이 basicConfig를 다시 불러도 INFO는 버려진다."""
        with patch.object(sys, "stderr", self.stderr):
            logging.basicConfig(level=logging.INFO)
        logging.info("DROPPED-PROBE")
        self.assertNotIn("DROPPED-PROBE", self.stderr.getvalue())

    def test_startup_mode_line_is_logged(self):
        self._configure()
        with patch.object(llm_mode, "LLM_PROVIDER", "groq"), \
             patch.object(llm_mode, "LLM_PROVIDER_RAW", "groq"), \
             patch.object(llm_engine, "GROQ_API_KEY", "gsk_secret_value"):
            llm_engine.log_llm_mode()
        out = self.stderr.getvalue()
        self.assertIn("L2 mode=cloud", out)
        self.assertNotIn("gsk_secret_value", out)

    def test_warmup_skip_is_logged_in_cloud_mode(self):
        self._configure()

        class _Sync:
            def __init__(self, target, **kw): self._t = target
            def start(self): self._t()

        with patch.object(llm_mode, "LLM_PROVIDER", "groq"), \
             patch.object(llm_engine, "_is_ollama_available", return_value=False), \
             patch.object(llm_engine.threading, "Thread", _Sync):
            llm_engine._ollama_warmup()
        self.assertIn("사전 로딩 건너뜀", self.stderr.getvalue())

    def test_telegram_send_success_is_logged(self):
        from src.telegram_bot import TelegramChatOps
        self._configure()
        client = TelegramChatOps.__new__(TelegramChatOps)
        client.enabled = True
        client.chat_id = "1"
        client._loop = None
        client.bot = type("B", (), {"send_message": AsyncMock(return_value=None)})()
        self.assertTrue(client.send_approval_request("ERR", "systemctl restart nginx",
                                                     "http://x/pending/tok", "설명"))
        self.assertTrue(client.send_notification("제목", "본문"))
        out = self.stderr.getvalue()
        self.assertIn("관리자에게 승인 요청을 발송했습니다", out)
        self.assertIn("알림 발송 완료", out)

    def test_httpx_does_not_log_bot_token_url(self):
        """INFO를 켜도 httpx 요청 로그(봇 토큰이 든 URL)는 남지 않아야 한다."""
        import httpx
        self._configure()

        def handler(request):
            return httpx.Response(200, json={"ok": True, "result": []})

        async def call():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                await c.post(f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates")

        asyncio.run(call())
        self.assertNotIn(FAKE_TOKEN, self.stderr.getvalue())


class TestChatOpsStatusLine(_LoggingCase):
    """configure_logging() 뒤에 텔레그램 상태를 한 줄 남긴다 — 값(토큰·chat ID)은 출력하지 않는다."""

    def _status(self, token, chat_id, enabled, app):
        from src import telegram_bot
        from src.log_watcher import log_chatops_status
        self._configure()
        fake = type("T", (), {"token": token, "chat_id": chat_id, "enabled": enabled, "app": app})()
        with patch.object(telegram_bot, "tg_chatops", fake):
            log_chatops_status()
        return self.stderr.getvalue()

    def test_enabled_reports_settings_without_values(self):
        out = self._status(FAKE_TOKEN, "987654321", True, object())
        self.assertIn("[Telegram] 상태: 활성", out)
        self.assertIn("승인 알림 대상(chat) 설정: 예", out)
        self.assertIn("polling: 예", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("987654321", out)

    def test_disabled_reports_missing_chat(self):
        out = self._status(FAKE_TOKEN, "", False, None)
        self.assertIn("[Telegram] 상태: 비활성", out)
        self.assertIn("승인 알림 대상(chat) 설정: 아니오", out)
        self.assertIn("polling: 아니오", out)


if __name__ == "__main__":
    unittest.main()
