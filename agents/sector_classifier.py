"""Agent 3: SectorClassifierAgent — 섹터 분류 + 핫섹터 선정.

뉴스를 섹터별로 태깅하고(Haiku), 언급량 기준으로 핫섹터 상위 N개를 고른다.

비용 절감을 위해 기사를 청크 단위로 묶어 한 번의 LLM 호출로 일괄 분류한다.
"""

from typing import Optional

import config
from agents.base import BaseAgent
from utils import llm

# 고정 섹터 분류 체계 (GICS 기반). 모델은 반드시 이 중에서 고른다.
SECTORS = [
    "Technology",
    "Semiconductors",
    "Communication Services",
    "Healthcare",
    "Financials",
    "Energy",
    "Consumer Discretionary",
    "Consumer Staples",
    "Industrials",
    "Materials",
    "Utilities",
    "Real Estate",
    "Other",
]
_SECTOR_SET = set(SECTORS)
_FALLBACK = "Other"

# 한 번의 LLM 호출로 처리할 기사 수
_BATCH_SIZE = 20

_SYSTEM = (
    "당신은 금융 뉴스 섹터 분류기다. 각 기사를 주어진 섹터 목록 중 하나로만 분류한다."
)


def _classify_prompt(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles):
        title = a.get("title", "")
        summary = a.get("summary") or a.get("content") or ""
        lines.append(f"[{i}] 제목: {title}\n    요약: {summary[:200]}")
    body = "\n".join(lines)
    sector_list = ", ".join(SECTORS)
    return (
        f"다음 기사들을 각각 아래 섹터 중 하나로 분류하라.\n"
        f"섹터 목록: {sector_list}\n\n"
        f"{body}\n\n"
        f"결과는 입력 순서와 동일한 길이의 JSON 배열로만 출력하라. "
        f'예: ["Technology", "Energy", ...]'
    )


def _normalize(sector) -> str:
    """모델이 반환한 섹터 문자열을 정규화한다. 목록에 없으면 Other."""
    if not isinstance(sector, str):
        return _FALLBACK
    s = sector.strip()
    if s in _SECTOR_SET:
        return s
    # 대소문자/부분 매칭 보정
    for known in SECTORS:
        if s.lower() == known.lower():
            return known
    return _FALLBACK


def classify_batch(articles: list[dict]) -> list[str]:
    """기사 청크를 한 번에 분류해 섹터 리스트를 반환한다.

    길이는 입력과 동일하게 보장한다. 실패/불일치 시 전체 Other로 fallback.
    """
    if not articles:
        return []
    try:
        result = llm.call_claude_json(
            _classify_prompt(articles),
            system=_SYSTEM,
            model=config.MODEL_FAST,
        )
        if not isinstance(result, list) or len(result) != len(articles):
            return [_FALLBACK] * len(articles)
        return [_normalize(s) for s in result]
    except Exception:
        return [_FALLBACK] * len(articles)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SectorClassifierAgent(BaseAgent):
    name = "SectorClassifierAgent"

    def execute(self, context: dict) -> dict:
        news = context.get("news", []) or []
        if not news:
            # 뉴스가 없어도 관심 섹터(PREFERRED_SECTORS)는 발굴 대상에 유지한다.
            preferred = [
                s for s in config.PREFERRED_SECTORS
                if s in _SECTOR_SET and s != _FALLBACK
            ]
            hot_sectors = list(dict.fromkeys(preferred))
            self.log.info("분류할 뉴스가 없다 -> 관심섹터만 사용: %s", ", ".join(hot_sectors) or "(없음)")
            context["hot_sectors"] = hot_sectors
            context["sector_counts"] = {}
            context["sector_sentiment"] = {}
            return {"tagged": 0, "sector_counts": {}, "hot_sectors": hot_sectors, "sector_sentiment": {}}

        # 청크 단위 일괄 분류
        sector_counts: dict[str, int] = {}
        sector_sent_sum: dict[str, float] = {}  # 섹터별 감성 합계 (평균 계산용)
        tagged = 0
        for chunk in _chunks(news, _BATCH_SIZE):
            sectors = classify_batch(chunk)
            for article, sector in zip(chunk, sectors):
                article["sector"] = sector
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                sector_sent_sum[sector] = sector_sent_sum.get(sector, 0.0) + float(
                    article.get("sentiment") or 0.0
                )
                tagged += 1

        # 섹터별 평균 감성 + 기사수 집계 (feedback_tracker가 종목 단위로 매핑)
        sector_sentiment = {
            s: {"score": round(sector_sent_sum[s] / sector_counts[s], 4), "count": sector_counts[s]}
            for s in sector_counts
        }

        # 핫섹터 선정: Other 제외, '언급량 × 감성 가중'으로 상위 N
        #   hotness = count × (1 + avg_sentiment)
        #   avg_sentiment ∈ [-1,1] → 가중치 ∈ [0,2]. 긍정 섹터를 끌어올리고
        #   부정 섹터는 낮춘다. 감성이 0이면 언급량 단독 랭킹과 동일(하위호환).
        def _hotness(sector: str) -> float:
            cnt = sector_counts[sector]
            score = sector_sentiment[sector]["score"]
            return cnt * (1.0 + score)

        ranked = sorted(
            (s for s in sector_counts if s != _FALLBACK),
            key=_hotness,
            reverse=True,
        )
        news_hot = ranked[: config.HOT_SECTOR_COUNT]

        # 관심 섹터(PREFERRED_SECTORS)는 뉴스량과 무관하게 항상 포함한다.
        #   - 알 수 없는 섹터명/Other는 무시 (오탈자 방어)
        #   - 뉴스가 없어도 발굴 대상에 넣어 평소 안 보던 분야를 챙긴다
        #   - 과대비중 제외 로직(스크리너)은 그대로 → 보유 섹터는 모니터링만((가) 모드)
        preferred = [
            s for s in config.PREFERRED_SECTORS
            if s in _SECTOR_SET and s != _FALLBACK
        ]
        unknown = [s for s in config.PREFERRED_SECTORS if s not in _SECTOR_SET]
        if unknown:
            self.log.info("알 수 없는 관심 섹터 무시: %s", ", ".join(unknown))

        # 뉴스 핫섹터 먼저, 그다음 관심 섹터(중복 제거, 순서 보존)
        hot_sectors = list(dict.fromkeys(news_hot + preferred))
        added = [s for s in preferred if s not in news_hot]

        self.log.info(
            "태깅 %d건, 섹터 %d종 -> 뉴스핫섹터: %s | 관심섹터 추가: %s",
            tagged, len(sector_counts),
            ", ".join(f"{s}({_hotness(s):.1f})" for s in news_hot) or "(없음)",
            ", ".join(added) or "(없음)",
        )

        context["hot_sectors"] = hot_sectors
        context["sector_counts"] = sector_counts
        context["sector_sentiment"] = sector_sentiment
        return {
            "tagged": tagged,
            "sector_counts": sector_counts,
            "hot_sectors": hot_sectors,
            "sector_sentiment": sector_sentiment,
        }
