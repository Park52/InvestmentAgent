"""리포트 발송 + 텔레그램 명령 수신 유틸 (NotificationService).

별도 에이전트가 아니라 유틸로 구현한다.
- 발송: ReportGeneratorAgent 마지막 단계에서 send() 호출. config.NOTIFICATION_CHANNEL에
  따라 텔레그램 / Gmail / 미발송.
- 수신: scheduler.py에서 start_bot()으로 getUpdates 롱폴링 → /run 명령 시 파이프라인 실행.

텔레그램은 requests로 Bot API를 동기 호출한다(python-telegram-bot 불필요).
Gmail은 stdlib smtplib(SSL)로 발송하며 리포트 파일을 첨부한다.

발송 실패가 파이프라인을 죽이지 않도록 모든 경로에서 예외를 흡수하고 False를 반환한다.
"""

import os
import smtplib
import time
from email.message import EmailMessage

import config
from agents.base import get_logger

log = get_logger("notifier")

_TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 465  # SSL


def _telegram_url(token: str, method: str) -> str:
    return _TELEGRAM_API_BASE.format(token=token, method=method)


class NotificationService:
    """리포트 요약을 설정된 채널로 발송한다."""

    def send(self, report_path: str, summary: str) -> bool:
        """summary(핵심 3~5줄)를 발송한다. report_path는 첨부/참고용 전체 리포트 경로.

        Returns:
            성공 시 True. 채널이 "none"이면 스킵하고 True. 실패 시 False.
        """
        channel = (config.NOTIFICATION_CHANNEL or "none").lower()
        if channel == "telegram":
            ok = self._send_telegram(summary)
            # 요약 메시지에 더해 전체 리포트 파일도 첨부 발송 (폰에서 열람용)
            if report_path and os.path.exists(report_path):
                self._send_telegram_document(report_path)
            return ok
        if channel == "gmail":
            return self._send_gmail(report_path, summary)
        if channel == "none":
            log.info("알림 채널이 'none'이라 발송을 건너뛴다.")
            return True
        log.warning("알 수 없는 알림 채널: %s (발송 생략)", channel)
        return False

    # ------------------------------------------------------------------
    # 텔레그램
    # ------------------------------------------------------------------
    def _send_telegram(self, summary: str) -> bool:
        token = config.TELEGRAM_BOT_TOKEN
        chat_id = config.TELEGRAM_CHAT_ID
        if not token or not chat_id:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 — 발송 생략")
            return False

        try:
            import requests
        except ImportError:
            log.error("requests 미설치 — `pip install requests` 필요")
            return False

        try:
            resp = requests.post(
                _telegram_url(token, "sendMessage"),
                data={"chat_id": chat_id, "text": summary, "disable_web_page_preview": True},
                timeout=15,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                log.info("텔레그램 발송 성공")
                return True
            log.error("텔레그램 발송 실패: HTTP %s %s", resp.status_code, resp.text[:300])
            return False
        except Exception as exc:  # 네트워크/JSON 등 모든 예외 흡수
            log.error("텔레그램 발송 예외: %s", exc)
            return False

    def _send_telegram_document(self, report_path: str) -> bool:
        """전체 리포트 파일을 텔레그램 문서로 첨부 발송한다 (sendDocument).

        파일 발송 실패는 흡수한다(요약 메시지는 이미 전송됐으므로 치명적이지 않음).
        """
        token = config.TELEGRAM_BOT_TOKEN
        chat_id = config.TELEGRAM_CHAT_ID
        if not token or not chat_id:
            return False
        try:
            import requests
        except ImportError:
            return False

        filename = os.path.basename(report_path)
        # 확장자에 맞는 MIME — .html은 폰에서 브라우저로 렌더링되게 한다.
        mime = "text/html" if filename.lower().endswith(".html") else "text/markdown"
        try:
            with open(report_path, "rb") as f:
                resp = requests.post(
                    _telegram_url(token, "sendDocument"),
                    data={"chat_id": chat_id, "caption": f"📄 전체 리포트 ({filename})"},
                    files={"document": (filename, f, mime)},
                    timeout=30,
                )
            if resp.status_code == 200 and resp.json().get("ok"):
                log.info("텔레그램 리포트 파일 발송 성공: %s", filename)
                return True
            log.error("텔레그램 파일 발송 실패: HTTP %s %s", resp.status_code, resp.text[:300])
            return False
        except Exception as exc:
            log.error("텔레그램 파일 발송 예외: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Gmail
    # ------------------------------------------------------------------
    def _send_gmail(self, report_path: str, summary: str) -> bool:
        sender = config.GMAIL_SENDER
        password = config.GMAIL_PASSWORD
        recipient = config.GMAIL_RECIPIENT
        if not sender or not password or not recipient:
            log.warning("GMAIL_SENDER/PASSWORD/RECIPIENT 미설정 — 발송 생략")
            return False

        # 제목: summary 첫 줄, 본문: summary 전체
        subject = summary.strip().splitlines()[0] if summary.strip() else "투자 정보 리포트"

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(summary)

        # 전체 리포트 파일 첨부 (있을 때만)
        if report_path and os.path.exists(report_path):
            try:
                with open(report_path, "rb") as f:
                    data = f.read()
                msg.add_attachment(
                    data,
                    maintype="text",
                    subtype="markdown",
                    filename=os.path.basename(report_path),
                )
            except Exception as exc:
                log.warning("리포트 첨부 실패(본문만 발송): %s", exc)

        try:
            with smtplib.SMTP_SSL(_GMAIL_SMTP_HOST, _GMAIL_SMTP_PORT, timeout=30) as server:
                server.login(sender, password)
                server.send_message(msg)
            log.info("Gmail 발송 성공 -> %s", recipient)
            return True
        except Exception as exc:
            log.error("Gmail 발송 예외: %s", exc)
            return False

    # ==================================================================
    # 명령 수신 (getUpdates 롱폴링) — scheduler.py에서 start_bot() 호출
    # ==================================================================
    def _get_updates(self, offset=None, timeout: int = 30):
        """텔레그램 getUpdates 롱폴링. 업데이트 리스트(실패 시 [])를 반환한다."""
        import requests
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(
                _telegram_url(config.TELEGRAM_BOT_TOKEN, "getUpdates"),
                params=params,
                timeout=timeout + 10,
            )
            data = resp.json()
        except Exception as exc:
            log.warning("getUpdates 예외: %s", exc)
            return []
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        return data.get("result", []) or []

    def _delete_webhook(self) -> None:
        """getUpdates가 동작하도록 기존 webhook을 제거한다(설정돼 있을 수 있음)."""
        import requests
        try:
            requests.get(
                _telegram_url(config.TELEGRAM_BOT_TOKEN, "deleteWebhook"),
                timeout=10,
            )
        except Exception:
            pass

    def _handle_update(self, update: dict, run_callback) -> bool:
        """단일 업데이트를 처리한다. /run 명령을 인가된 사용자가 보냈으면 실행.

        Returns: 파이프라인을 실행했으면 True, 아니면 False.
        """
        message = update.get("message") or update.get("edited_message") or {}
        text = (message.get("text") or "").strip()
        chat_id = str((message.get("chat") or {}).get("id") or "")

        # 인가: 설정된 CHAT_ID만 명령 허용
        if chat_id != str(config.TELEGRAM_CHAT_ID):
            return False
        if not text.startswith("/run"):
            return False

        self._send_telegram("⏳ 파이프라인 시작... 완료되면 리포트를 전송할게요.")
        try:
            run_callback()
            return True
        except Exception as exc:  # 파이프라인 실패해도 봇은 계속 살아있어야 함
            log.error("/run 실행 중 예외: %s", exc)
            self._send_telegram(f"⚠️ 파이프라인 실행 중 오류: {exc}")
            return False

    def poll_once(self, offset, run_callback):
        """getUpdates 1회 호출 → 업데이트 처리. 다음 offset을 반환한다."""
        updates = self._get_updates(offset=offset)
        for u in updates:
            offset = u.get("update_id", 0) + 1
            self._handle_update(u, run_callback)
        return offset

    def start_bot(self, run_callback, poll_timeout: int = 30) -> None:
        """블로킹 롱폴링 루프. /run 명령을 대기하며 처리한다.

        run_callback: 인자 없는 호출 가능 객체(보통 scheduler.run_pipeline).
        """
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            log.warning("TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 봇 폴링을 시작하지 않는다.")
            return

        self._delete_webhook()
        log.info("텔레그램 봇 폴링 시작 (/run 대기). 중단하려면 Ctrl+C.")
        offset = None
        while True:
            try:
                offset = self.poll_once(offset, run_callback)
            except KeyboardInterrupt:
                log.info("봇 폴링 종료")
                break
            except Exception as exc:  # 루프는 어떤 경우에도 죽지 않는다
                log.warning("폴링 루프 예외(계속): %s", exc)
                time.sleep(5)
