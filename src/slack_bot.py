import os
import re
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

class SlackChatOps:
    def __init__(self):
        self.webhook_url = os.getenv("SLACK_WEBHOOK_URL")

    def send_approval_request(self, error_log: str, command: str, reason: str) -> bool:
        """슬랙으로 조치 승인 요청(URL 버튼) 메시지를 발송합니다.

        Incoming Webhook만으로 동작하는 URL 버튼 방식 사용.
        reason에 포함된 http(s):// URL을 버튼 링크로 추출한다.
        """
        if not self.webhook_url:
            logging.warning("🔔 SLACK_WEBHOOK_URL이 설정되지 않아 슬랙 알림을 건너뜁니다.")
            return False

        url_match = re.search(r'https?://\S+', reason)
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
            # approve/reject 엔드포인트는 동일 토큰으로 /approve/, /reject/ 경로 사용
            approve_url = pending_url.replace("/pending/", "/approve/", 1)
            reject_url  = pending_url.replace("/pending/", "/reject/", 1)
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ 승인 (실행)", "emoji": True},
                        "url": approve_url,
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ 거절 (무시)", "emoji": True},
                        "url": reject_url,
                        "style": "danger",
                    },
                ],
            })
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"⏰ 승인 유효 시간: 5분 | 상세 확인: {pending_url}"}
                ],
            })
        else:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"⚠️ {reason}"},
            })

        try:
            response = requests.post(self.webhook_url, json={"blocks": blocks}, timeout=5)
            if response.ok:
                logging.info("[Slack] 관리자에게 조치 승인 요청을 발송했습니다.")
                return True
            logging.error(f"Slack 발송 실패 (HTTP {response.status_code}): {response.text}")
            return False
        except Exception as e:
            logging.error(f"Slack 통신 에러: {e}")
            return False

    def send_notification(self, title: str, message: str) -> bool:
        """단순 정보성 알림 메시지를 발송합니다."""
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
            logging.error(f"Slack 발송 실패 (HTTP {response.status_code}): {response.text}")
            return False
        except Exception as e:
            logging.error(f"Slack 통신 에러: {e}")
            return False