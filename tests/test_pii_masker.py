"""pii_masker 보강 패턴(2026-10-07) — 넘기기 경보 사유에 에러 로그 첫 줄이 들어가면서 추가."""
import unittest

from src.utils.pii_masker import mask

# 테스트용 가짜 값 — 형식만 실제와 같다.
_TG = "1234567890:AAFakeFakeFakeFakeFakeFakeFakeFake_-x"
_GROQ = "gsk_" + "A1b2C3d4" * 6
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLWZha2U"


class TestPiiMaskerAdditions(unittest.TestCase):
    def test_telegram_bot_token_bare_and_in_url(self):
        out = mask(f"token {_TG} and https://api.telegram.org/bot{_TG}/sendMessage")
        self.assertNotIn("AAFake", out)
        self.assertEqual(out.count("<TG_TOKEN>"), 2)
        self.assertIn("api.telegram.org/bot<TG_TOKEN>/sendMessage", out)

    def test_groq_key(self):
        out = mask(f"Groq call failed with key {_GROQ}")
        self.assertEqual(out, "Groq call failed with key <GROQ_KEY>")

    def test_bearer_value(self):
        out = mask("Authorization: Bearer abc.DEF-123_xyz~+/= rejected")
        self.assertIn("Bearer <REDACTED>", out)
        self.assertNotIn("abc.DEF", out)

    def test_jwt(self):
        out = mask(f"session {_JWT} expired")
        self.assertEqual(out, "session <JWT> expired")

    def test_url_credentials(self):
        out = mask("could not connect postgres://admin:s3cr3t@db.internal:5432/app")
        self.assertEqual(out, "could not connect postgres://<CREDS>@db.internal:5432/app")
        out = mask("redis://default:p@ss:word@10.0.0.5:6379/0")
        self.assertNotIn("word", out)
        self.assertNotIn("10.0.0.5", out)

    def test_normal_sentences_unchanged(self):
        for s in (
            "redis.exceptions.ConnectionError — Could not connect to Redis: Connection refused",
            "2026-10-07T05:12:33.123456 CRITICAL chaos-injector: process exited 137",
            "see https://example.com/docs/page?id=12345678 for details",
            "Bearer token missing",
            "time=12:34:56 pid=128130",
        ):
            self.assertEqual(mask(s), s)


if __name__ == "__main__":
    unittest.main()
