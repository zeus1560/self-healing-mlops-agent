"""
TelegramChatOps — Telegram Bot을 통한 알림 및 승인 요청 처리.

- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수가 모두 있으면 활성화됩니다.
- callback_query 버튼 클릭 시 approval_store.set_decision()을 호출하여
  기존 SQLite 기반 승인 상태를 갱신합니다.
- TELEGRAM_BOT_TOKEN이 없으면 Slack fallback을 사용할 수 있습니다.
"""
import html
import logging
import os
import threading
from dotenv import load_dotenv

from src import approval_store

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("ALLOWED_USER_ID")

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes
    DEFAULT_CONTEXT_TYPE = ContextTypes.DEFAULT_TYPE
except ImportError:  # pragma: no cover
    Bot = InlineKeyboardButton = InlineKeyboardMarkup = ApplicationBuilder = CallbackQueryHandler = Update = None
    DEFAULT_CONTEXT_TYPE = object


class TelegramChatOps:
    """Telegram Bot을 통해 알림 및 승인 요청을 발송합니다."""

    def __init__(self):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id and Bot is not None)
        self.bot = None
        self.app = None

        if not self.enabled:
            if Bot is None:
                logging.warning(
                    "[Telegram] python-telegram-bot 패키지가 설치되지 않았습니다. Telegram 알림을 사용할 수 없습니다."
                )
            else:
                logging.info(
                    "[Telegram] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다. Telegram 알림을 비활성화합니다."
                )
            return

        approval_store.init_table()
        try:
            self.bot = Bot(token=self.token)
            self.app = ApplicationBuilder().token(self.token).build()
            self.app.add_handler(CallbackQueryHandler(self._handle_callback))
            self._start_polling()
            logging.info("[Telegram] 백그라운드 polling 스레드가 시작되었습니다.")
        except Exception as e:
            self.enabled = False
            logging.error(f"[Telegram] 초기화 실패: {e}")

    def _start_polling(self) -> None:
        def _run():
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # store the loop so other threads can schedule coroutines into the polling loop
            self._loop = loop
            try:
                loop.run_until_complete(
                    self.app.run_polling(
                        allowed_updates=["callback_query"],
                        drop_pending_updates=True,
                        stop_signals=None,
                        close_loop=False,
                    )
                )
            except Exception as e:
                logging.error(f"[Telegram] polling 중 오류 발생: {e}")
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, daemon=True, name="telegram-polling")
        thread.start()

    async def _handle_callback(self, update: "Update", context: DEFAULT_CONTEXT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return

        await query.answer()
        payload = query.data.split("|", 1)
        if len(payload) != 2:
            await query.edit_message_text("잘못된 승인 요청입니다.")
            return

        action, token = payload
        if action == "approve":
            decided = approval_store.set_decision(token, "approved")
            text = "✅ 승인되었습니다. 명령을 실행합니다." if decided else "⚠️ 이 요청은 이미 처리되었거나 만료되었습니다."
        elif action == "reject":
            decided = approval_store.set_decision(token, "rejected")
            text = "🚫 거절되었습니다. 명령 실행이 취소되었습니다." if decided else "⚠️ 이 요청은 이미 처리되었거나 만료되었습니다."
        else:
            text = "잘못된 승인 액션입니다."

        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.edit_message_text(text)
        except Exception:
            logging.debug("[Telegram] 콜백 메시지 편집 중 오류 발생", exc_info=True)

    def send_approval_request(self, error_log: str, command: str, reason: str) -> bool:
        if not self.enabled:
            logging.warning("[Telegram] TELEGRAM_BOT_TOKEN/CHAT_ID 미설정으로 승인 요청을 건너뜁니다.")
            return False

        safe_error_log = html.escape((error_log or "").strip())[:300]
        safe_command = html.escape((command or "").strip())
        safe_reason = html.escape((reason or "").strip())

        text = (
            "<b>🚨 [Self-Healing Agent] 명령어 실행 승인 요청</b>\n\n"
            f"<b>감지된 에러</b>\n<pre>{safe_error_log}</pre>\n\n"
            f"<b>실행 예정 명령어</b>\n<pre>{safe_command}</pre>\n\n"
            f"<b>설명</b>\n{safe_reason}"
        )
        approve_url = f"approve|{self._extract_token(reason)}"
        reject_url = f"reject|{self._extract_token(reason)}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 승인 (실행)", callback_data=approve_url),
                InlineKeyboardButton("🚫 거절 (무시)", callback_data=reject_url),
            ]
        ])

        try:
            coro = self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            # Prefer scheduling into the polling loop if available (thread-safe)
            if getattr(self, '_loop', None):
                import asyncio
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            else:
                # Fallback: run in a temporary event loop
                import asyncio
                asyncio.run(coro)
            logging.info("[Telegram] 관리자에게 승인 요청을 발송했습니다.")
            return True
        except Exception as e:
            logging.error(f"[Telegram] 승인 요청 발송 실패: {e}")
            return False

    def send_notification(self, title: str, message: str) -> bool:
        if not self.enabled:
            logging.warning("[Telegram] TELEGRAM_BOT_TOKEN/CHAT_ID 미설정으로 알림 발송을 건너뜁니다.")
            return False

        text = (
            f"<b>{html.escape(title)}</b>\n\n"
            f"{html.escape(message)}"
        )
        try:
            coro = self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            )
            if getattr(self, '_loop', None):
                import asyncio
                asyncio.run_coroutine_threadsafe(coro, self._loop)
            else:
                import asyncio
                asyncio.run(coro)
            logging.info(f"[Telegram] 알림 발송 완료: {title}")
            return True
        except Exception as e:
            logging.error(f"[Telegram] 알림 발송 실패: {e}")
            return False

    @staticmethod
    def _extract_token(reason: str) -> str:
        if not reason:
            return ""
        parts = reason.split("/pending/")
        if len(parts) < 2:
            return ""
        token = parts[1].split()[0].strip()
        return token


tg_chatops = TelegramChatOps()


def get_chatops_client() -> TelegramChatOps | None:
    return tg_chatops if tg_chatops.enabled else None
