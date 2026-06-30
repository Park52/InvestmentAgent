"""Agent 1: DataQualityAgent — 뉴스 소스 점검.

수집(Agent 2) 전에 실행되어, 설정된 RSS 피드를 헬스체크하고
정상 피드만 추려 다운스트림(NewsCollector)에 전달한다.

중복 URL 필터와 오래된 기사 제거는 수집된 기사 단위로 NewsCollector가
수행한다(책임 중복 방지). 이 에이전트는 '소스 레벨' 품질 게이트를 담당한다.
"""

from typing import Optional

import config
from agents.base import BaseAgent


def _check_feed(url: str) -> dict:
    """피드 하나를 파싱 시도하고 상태를 진단한다.

    반환: {url, status, entries}
      status: "ok"    - 기사 1건 이상
              "empty" - 파싱은 됐으나 기사 0건
              "error" - 파싱 실패/예외
    """
    try:
        import feedparser
        parsed = feedparser.parse(url)
        # bozo가 set이고 entries도 없으면 사실상 실패로 본다
        entries = getattr(parsed, "entries", []) or []
        if entries:
            return {"url": url, "status": "ok", "entries": len(entries)}
        if getattr(parsed, "bozo", 0):
            return {"url": url, "status": "error", "entries": 0}
        return {"url": url, "status": "empty", "entries": 0}
    except Exception:
        return {"url": url, "status": "error", "entries": 0}


class DataQualityAgent(BaseAgent):
    name = "DataQualityAgent"

    def execute(self, context: dict) -> dict:
        diagnostics = [_check_feed(url) for url in config.NEWS_RSS_FEEDS]

        healthy = [d["url"] for d in diagnostics if d["status"] == "ok"]
        unhealthy = [d for d in diagnostics if d["status"] != "ok"]

        self.log.info(
            "피드 점검 %d개 -> 정상 %d, 비정상 %d",
            len(diagnostics), len(healthy), len(unhealthy),
        )
        for d in unhealthy:
            self.log.warning("피드 비정상(%s): %s", d["status"], d["url"])

        # NewsCollector가 우선 사용할 피드 목록
        context["feeds"] = healthy

        return {
            "checked": len(diagnostics),
            "healthy": len(healthy),
            "unhealthy": len(unhealthy),
            "diagnostics": diagnostics,
        }
