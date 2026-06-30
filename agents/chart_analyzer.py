"""Agent 6: ChartAnalyzerAgent — 기술적 지표 + 선행지표 수집 + 해석.

LLM에게 차트를 '보고 분석'시키지 않는다. utils.indicators로 RSI/MACD/이평선 등
수치를 계산한 뒤, 그 숫자를 Sonnet에 주입해 해석만 요청한다(CLAUDE.md 원칙).

후보 확정(Agent 5) 이후 단계이므로, 여기서 종목 단위 '선행지표'(EPS/내부자/공매도/
구글트렌드)를 utils.market_data로 함께 수집한다.
- 후보 종목: 차트 + 해석(LLM) + 선행지표
- 보유 종목: 차트(코드만, LLM 해석 없음) + 선행지표 → 포트폴리오 모니터링/데이터 축적
"""

import config
from agents.base import BaseAgent
from utils import indicators, llm, market_data

_SYSTEM = "당신은 기술적 분석가다. 주어진 수치만으로 해석하며, 없는 데이터를 지어내지 않는다."


def interpret(ticker: str, ind: dict) -> str:
    """계산된 지표 수치를 Sonnet에 주입해 해석문을 생성한다. 실패 시 빈 문자열."""
    prompt = (
        f"종목: {ticker}\n"
        f"RSI(14): {ind.get('rsi_14')}   # 70 이상 과매수, 30 이하 과매도\n"
        f"MACD: {ind.get('macd_signal')}\n"
        f"20일 이평선: {ind.get('ma_20')}, 60일 이평선: {ind.get('ma_60')}\n"
        f"골든크로스 여부: {ind.get('ma_cross')}\n"
        f"거래량 추세: {ind.get('volume_trend')}\n\n"
        f"위 수치를 바탕으로 기술적 분석 해석을 3~5문장으로 작성하라."
    )
    try:
        return llm.call_claude(prompt, system=_SYSTEM, model=config.MODEL_SMART)
    except Exception:
        return ""


class ChartAnalyzerAgent(BaseAgent):
    name = "ChartAnalyzerAgent"

    def execute(self, context: dict) -> dict:
        candidates = context.get("candidates", []) or []
        analyzed: list[dict] = []

        # ── 후보 종목: 차트 + 해석 + 선행지표 ──────────────
        for c in candidates:
            ticker = c["ticker"]
            ind = indicators.analyze(
                ticker, period=config.HISTORY_PERIOD, interval=config.HISTORY_INTERVAL
            )
            if ind.get("error"):
                self.log.info("지표 계산 실패 스킵: %s (%s)", ticker, ind.get("error"))
                continue

            entry = dict(c)
            entry["chart"] = ind
            entry["chart_interpretation"] = interpret(ticker, ind)
            entry["leading"] = market_data.collect_leading_indicators(ticker, c.get("name"))
            analyzed.append(entry)
            lead = entry["leading"]
            self.log.info(
                "%s RSI=%s MACD=%s cross=%s | EPS=%s insider(b/s)=%s/%s trend=%s",
                ticker, ind.get("rsi_14"), ind.get("macd_signal"), ind.get("ma_cross"),
                lead.get("eps_revision_trend"), lead.get("insider_buy_count"),
                lead.get("insider_sell_count"), lead.get("google_trend_score"),
            )

        # ── 보유 종목: 차트(코드만) + 선행지표 (LLM 호출 없음) ──
        holdings = (context.get("portfolio", {}) or {}).get("holdings", []) or []
        holdings_data: list[dict] = []
        for h in holdings:
            if not isinstance(h, dict) or not h.get("ticker"):
                continue
            ticker = h["ticker"]
            ind = indicators.analyze(
                ticker, period=config.HISTORY_PERIOD, interval=config.HISTORY_INTERVAL
            )
            holdings_data.append({
                "ticker": ticker,
                "name": h.get("name"),
                "sector": h.get("sector"),
                "chart": {} if ind.get("error") else ind,
                "leading": market_data.collect_leading_indicators(ticker, h.get("name")),
            })

        self.log.info("차트 분석: 후보 %d종목, 보유 %d종목", len(analyzed), len(holdings_data))
        context["analyzed"] = analyzed
        context["holdings_data"] = holdings_data
        return {"analyzed": analyzed, "count": len(analyzed), "holdings": len(holdings_data)}
