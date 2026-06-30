"""Agent 4: PortfolioContextAgent — 보유 종목/비중 반영.

portfolio.json에서 현재 보유 종목과 비중을 읽어 섹터별 비중을 집계하고,
과대비중 섹터를 플래그로 만들어 스크리너(Agent 5)에 전달한다.

portfolio.json 스키마:
    {
      "holdings": [
        {"ticker": "AAPL", "name": "Apple", "sector": "Technology", "weight": 25},
        ...
      ]
    }
  weight는 0~1(비율) 또는 0~100(퍼센트) 둘 다 허용한다(자동 감지).
"""

import json
import os
from typing import Optional

import config
from agents.base import BaseAgent


def _load_portfolio(path: str) -> dict:
    """portfolio.json을 읽는다. 파일 없음/손상 시 빈 포트폴리오를 반환한다."""
    if not os.path.exists(path):
        return {"holdings": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        holdings = data.get("holdings") if isinstance(data, dict) else None
        if not isinstance(holdings, list):
            return {"holdings": []}
        return {"holdings": holdings}
    except (json.JSONDecodeError, OSError):
        return {"holdings": []}


def _sector_weights(holdings: list[dict]) -> dict[str, float]:
    """보유 종목의 비중을 섹터별로 합산한다.

    비중 단위 자동 감지: 어떤 weight라도 1을 초과하면 전체를 퍼센트로 보고 /100.
    """
    weights = [float(h.get("weight", 0) or 0) for h in holdings]
    is_percent = any(w > 1 for w in weights)
    divisor = 100.0 if is_percent else 1.0

    sector_weights: dict[str, float] = {}
    for h, w in zip(holdings, weights):
        sector = h.get("sector") or "Unknown"
        sector_weights[sector] = sector_weights.get(sector, 0.0) + w / divisor
    # 부동소수 정리
    return {s: round(w, 4) for s, w in sector_weights.items()}


class PortfolioContextAgent(BaseAgent):
    name = "PortfolioContextAgent"

    def execute(self, context: dict) -> dict:
        portfolio = _load_portfolio(config.PORTFOLIO_PATH)
        holdings = portfolio["holdings"]

        held_tickers = [
            h["ticker"] for h in holdings if isinstance(h, dict) and h.get("ticker")
        ]
        sector_weights = _sector_weights([h for h in holdings if isinstance(h, dict)])

        threshold = config.PORTFOLIO_OVERWEIGHT_THRESHOLD
        overweight_sectors = [
            s for s, w in sector_weights.items()
            if s != "Unknown" and w >= threshold
        ]

        self.log.info(
            "보유 %d종목, 섹터비중 %s -> 과대비중: %s",
            len(held_tickers), sector_weights, overweight_sectors or "(없음)",
        )

        context["portfolio"] = {"holdings": holdings, "sector_weights": sector_weights}
        context["held_tickers"] = held_tickers
        context["overweight_sectors"] = overweight_sectors

        return {
            "holdings": len(held_tickers),
            "held_tickers": held_tickers,
            "sector_weights": sector_weights,
            "overweight_sectors": overweight_sectors,
        }
