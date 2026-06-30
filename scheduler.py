"""파이프라인 진입점.

전체 에이전트를 순서대로 실행한다. 공유 context dict가 에이전트 간 결과를 전달한다.

사용법:
    python scheduler.py --now     # 즉시 1회 실행 (테스트)
    python scheduler.py           # 스케줄러 시작 (매일 지정 시각 자동 실행)
"""

import argparse
from datetime import datetime, timezone

import config
from agents.base import get_logger
from db import schema
from utils.llm import log_token_usage

# 에이전트 실행 순서 (Agent 0 → 9)
from agents.macro_check import MacroCheckAgent
from agents.data_quality import DataQualityAgent
from agents.data_collector import DataCollectorAgent
from agents.sector_classifier import SectorClassifierAgent
from agents.portfolio_context import PortfolioContextAgent
from agents.stock_screener import StockScreenerAgent
from agents.chart_analyzer import ChartAnalyzerAgent
from agents.thesis_validator import ThesisValidatorAgent
from agents.report_generator import ReportGeneratorAgent
from agents.feedback_tracker import FeedbackTrackerAgent

log = get_logger("scheduler")

PIPELINE = [
    MacroCheckAgent,
    DataQualityAgent,
    DataCollectorAgent,
    SectorClassifierAgent,
    PortfolioContextAgent,
    StockScreenerAgent,
    ChartAnalyzerAgent,
    ThesisValidatorAgent,
    ReportGeneratorAgent,
    FeedbackTrackerAgent,
]


def run_pipeline(context: dict = None) -> dict:
    """파이프라인을 1회 실행하고 최종 context를 반환한다.

    한 에이전트가 실패해도(AgentResult.success=False) 다음 에이전트로 진행한다.
    """
    config.ensure_dirs()
    schema.init_db()

    if context is None:
        context = {}
    context.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    log.info("=== 파이프라인 시작 (%s) ===", context["date"])
    for agent_cls in PIPELINE:
        agent = agent_cls()
        result = agent.run(context)
        if not result.success:
            log.warning("%s 실패(계속 진행): %s", agent.name, result.error)

    log_token_usage()
    log.info("=== 파이프라인 종료 ===")
    return context


def main() -> None:
    """기본 실행: 백그라운드 스케줄러(매일 cron) + 텔레그램 봇 폴링(/run 대기).

    --now: 즉시 1회 실행만 하고 종료.
    텔레그램 미설정 시: 봇 폴링 없이 스케줄러만 블로킹 실행.
    """
    parser = argparse.ArgumentParser(description="Investment Agent 파이프라인")
    parser.add_argument("--now", action="store_true", help="즉시 1회 실행 후 종료")
    args = parser.parse_args()

    if args.now:
        run_pipeline()
        return

    # 백그라운드 스케줄러: 매일 지정 시각 자동 실행
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.error(
            "apscheduler가 설치돼 있지 않다. `pip install apscheduler` 후 사용하거나 "
            "`python scheduler.py --now`로 즉시 실행하라."
        )
        return

    bg = BackgroundScheduler(timezone=config.TIMEZONE)
    bg.add_job(
        run_pipeline, "cron",
        hour=config.SCHEDULE_HOUR, minute=config.SCHEDULE_MINUTE,
    )
    bg.start()
    log.info(
        "스케줄러 시작: 매일 %02d:%02d (%s).",
        config.SCHEDULE_HOUR, config.SCHEDULE_MINUTE, config.TIMEZONE,
    )

    # 텔레그램 봇 폴링: /run 명령 대기 (포그라운드 블로킹)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        from utils.notifier import NotificationService
        try:
            NotificationService().start_bot(run_pipeline)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            bg.shutdown(wait=False)
            log.info("종료")
    else:
        # 텔레그램 미설정: 스케줄러만 유지 (Ctrl+C까지 블로킹)
        log.info("텔레그램 미설정 — 스케줄러만 실행한다. 중단하려면 Ctrl+C.")
        import time
        try:
            while True:
                time.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            bg.shutdown(wait=False)
            log.info("종료")


if __name__ == "__main__":
    main()
