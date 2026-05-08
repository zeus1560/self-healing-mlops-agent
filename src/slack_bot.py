import os
import requests
import json
import logging
from dotenv import load_dotenv

load_dotenv()

class SlackChatOps:
    def __init__(self):
        self.webhook_url = os.getenv("SLACK_WEBHOOK_URL")

    def send_approval_request(self, error_log: str, command: str, reason: str) -> bool:
        """슬랙으로 조치 승인 요청(Interactive Buttons) 메시지를 발송합니다."""
        if not self.webhook_url:
            logging.warning("🔔 SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 알림을 건너뜁니다.")
            return False

        # Slack Block Kit JSON 구조 (예쁜 UI와 버튼 생성)
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🚨 [AI 에이전트] 시스템 장애 조치 승인 요청",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*발견된 에러:*\n{error_log}"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*실행 예정 명령어 (Target Command):*\n`{command}`"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "✅ 조치 승인 (실행)",
                                "emoji": True
                            },
                            "style": "primary",
                            "value": "approve"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "❌ 거절 (무시)",
                                "emoji": True
                            },
                            "style": "danger",
                            "value": "reject"
                        }
                    ]
                }
            ]
        }

        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=5,
            )
            if response.ok:
                logging.info("[Slack] 관리자에게 조치 승인 요청을 발송했습니다.")
                return True
            logging.error(f"Slack 발송 실패 (HTTP {response.status_code}): {response.text}")
            return False
        except Exception as e:
            logging.error(f"Slack 통신 에러: {e}")
            return False