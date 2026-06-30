"""Agent 0: MacroCheckAgent — 거시환경 체크.

VIX(변동성), 미국 10년물 금리, 원/달러 환율을 조회해 시장 리스크 레벨을
LOW / MID / HIGH로 판정한다. HIGH면 이후 에이전트에 "관망 우선" 플래그를 전달한다.

LLM을 쓰지 않는다 — 데이터 조회 + 임계값 판정.
"""

from typing import Optional

import config
from agents.base import BaseAgent


def _fetch_latest_close(symbol: str) -> Optional[float]:
    """yfinance로 심볼의 최신 종가를 조회한다. 실패 시 None.

    네트워크/심볼 오류가 전체 파이프라인을 멈추지 않도록 예외를 흡수한다.
    """
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="5d")
        if df is None or df.empty or "Close" not in df:
            return None
        series = df["Close"].dropna()
        if series.empty:
            return None
        return round(float(series.iloc[-1]), 2)
    except Exception:
        return None


def classify_risk(vix: Optional[float]) -> str:
    """VIX 값으로 시장 리스크 레벨을 판정한다.

    vix < MID            -> LOW
    MID <= vix < HIGH    -> MID
    vix >= HIGH          -> HIGH
    vix is None(조회 실패) -> MID (정보 부재 시 보수적으로)
    """
    if vix is None:
        return "MID"
    if vix >= config.VIX_HIGH_THRESHOLD:
        return "HIGH"
    if vix >= config.VIX_MID_THRESHOLD:
        return "MID"
    return "LOW"


class MacroCheckAgent(BaseAgent):
    name = "MacroCheckAgent"

    def execute(self, context: dict) -> dict:
        vix = _fetch_latest_close(config.VIX_SYMBOL)
        usdkrw = _fetch_latest_close(config.USDKRW_SYMBOL)
        us10y = _fetch_latest_close(config.US10Y_SYMBOL)

        risk_level = classify_risk(vix)
        caution = risk_level == "HIGH"

        result = {
            "vix": vix,
            "usdkrw": usdkrw,
            "us10y": us10y,
            "market_risk_level": risk_level,
            "caution": caution,
        }

        self.log.info(
            "VIX=%s USDKRW=%s US10Y=%s -> risk=%s%s",
            vix, usdkrw, us10y, risk_level, " (관망 우선)" if caution else "",
        )

        # 다운스트림 에이전트가 참조할 수 있도록 컨텍스트에 누적
        context["macro"] = result
        context["caution"] = caution

        return result
