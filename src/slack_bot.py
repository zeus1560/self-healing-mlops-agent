"""
SlackChatOps — Slack Incoming Webhook 기반 알림 및 승인 요청 발송.

두 가지 메시지 유형:
  1. send_approval_request(): 명령어 실행 승인 요청 (승인/거절 URL 버튼 포함)
  2. send_notification():     단순 정보성 알림

load_dotenv()는 모듈 임포트 시 한 번만 실행된다.
SLACK_WEBHOOK_URL 미설정 시 경고 로그만 남기고 조용히 실패한다.
"""
import logging
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()


class SlackChatOps:
    """Slack Webhook을 통해 메시지를 발송하는 ChatOps 클라이언트."""

    def __init__(self):
        self.webhook_url = os.getenv("SLACK_WEBHOOK_URL")

    def send_approval_request(
        self, error_log: str, command: str, reason: str
    ) -> bool:
        """
        관리자에게 명령어 실행 승인 요청 메시지를 발송한다.

        reason에 포함된 https?:// URL을 승인/거절 버튼 링크로 추출한다.
        URL이 없으면 텍스트 섹션으로 대체한다.

        Returns:
            발송 성공 여부.
        """
        if not self.webhook_url:
            logging.warning("🔔 SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 알림을 건너뜁니다.")
            return False

        url_match   = re.search(r'https?://\S+', reason)
        pending_url = url_match.group(0) if url_match else None

        short_log = (error_log or "")[:300].replace("\n", " ")
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 [Self-Healing Agent] 명령어 실행 승인 요청",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*감지된 에러*\n```{short_log}```"},
                    {"type": "mrkdwn", "text": f"*실행 예정 명령어*\n`{command}`"},
                ],
            },
            {"type": "divider"},
        ]

        if pending_url:
            # pending_url 형식: {base}/pending/{token}
            approve_url = pending_url.replace("/pending/", "/approve/", 1)
            reject_url  = pending_url.replace("/pending/", "/reject/",  1)
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ 승인 (실행)", "emoji": True},
                        "url":   approve_url,
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ 거절 (무시)", "emoji": True},
                        "url":   reject_url,
                        "style": "danger",
                    },
                ],
            })
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"⏰ 승인 유효 시간: 5분 | 상세 확인: {pending_url}",
                    }
                ],
            })
        else:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"⚠️ {reason}"},
            })

        try:
            response = requests.post(
                self.webhook_url, json={"blocks": blocks}, timeout=5
            )
            if response.ok:
                logging.info("[Slack] 관리자에게 조치 승인 요청을 발송했습니다.")
                return True
            logging.error(
                f"Slack 발송 실패 (HTTP {response.status_code}): {response.text}"
            )
            return False
        except Exception as e:
            logging.error(f"Slack 통신 에러: {e}")
            return False

    def send_notification(self, title: str, message: str) -> bool:
        """
        단순 정보성 알림 메시지를 발송한다.

        Returns:
            발송 성공 여부.
        """
        if not self.webhook_url:
            logging.warning("🔔 SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 알림을 건너뜁니다.")
            return False

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title, "emoji": True},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message},
                },
            ]
        }

        try:
            response = requests.post(self.webhook_url, json=payload, timeout=5)
            if response.ok:
                logging.info(f"[Slack] 알림 발송 완료: {title}")
                return True
            logging.error(
                f"Slack 발송 실패 (HTTP {response.status_code}): {response.text}"
            )
            return False
        except Exception as e:
            logging.error(f"Slack 통신 에러: {e}")
            return False
