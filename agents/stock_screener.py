"""Agent 5: StockScreenerAgent — 후보 종목 선별.

핫섹터에서 후보 종목을 추출하고(Haiku), yfinance로 시총/거래량을 필터링하며,
포트폴리오 과대비중 섹터와 보유 종목은 제외한다.
"""

import config
from agents.base import BaseAgent
from utils import llm

_SYSTEM = "당신은 주식 스크리너다. 각 섹터의 대표 상장 종목 티커를 제안한다."


def propose_candidates(sectors: list[str], per_sector: int) -> list[dict]:
    """핫섹터별 대표 종목 후보를 Haiku로 제안받는다.

    반환: [{ticker, name, sector}, ...] / 실패 시 []
    """
    if not sectors:
        return []
    # 필터로 일부 탈락할 것을 감안해 넉넉히 요청
    ask = max(per_sector * 2, per_sector)
    prompt = (
        f"다음 섹터들에서 미국 증시 상장 대표 종목을 각 섹터당 최대 {ask}개씩 제안하라.\n"
        f"섹터: {', '.join(sectors)}\n\n"
        '결과는 JSON 배열로만 출력하라. '
        '예: [{"ticker":"AAPL","name":"Apple","sector":"Technology"}, ...]'
    )
    try:
        result = llm.call_claude_json(prompt, system=_SYSTEM, model=config.MODEL_FAST)
    except Exception:
        return []
    if not isinstance(result, list):
        return []
    out = []
    for item in result:
        if isinstance(item, dict) and item.get("ticker") and item.get("sector"):
            out.append({
                "ticker": str(item["ticker"]).upper().strip(),
                "name": item.get("name", ""),
                "sector": item["sector"],
            })
    return out


def _fetch_fundamentals(ticker: str) -> dict:
    """yfinance로 시총/평균거래량을 조회한다. 실패 시 None 값."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = getattr(t, "info", {}) or {}
        market_cap = info.get("marketCap")
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day")
        if avg_volume is None:
            hist = t.history(period="1mo")
            if hist is not None and not hist.empty and "Volume" in hist:
                vol = hist["Volume"].dropna()
                if not vol.empty:
                    avg_volume = float(vol.mean())
        return {"market_cap": market_cap, "avg_volume": avg_volume}
    except Exception:
        return {"market_cap": None, "avg_volume": None}


class StockScreenerAgent(BaseAgent):
    name = "StockScreenerAgent"

    def execute(self, context: dict) -> dict:
        hot = context.get("hot_sectors", []) or []
        overweight = set(context.get("overweight_sectors", []) or [])
        held = set(context.get("held_tickers", []) or [])

        # 과대비중 섹터는 후보 추출 대상에서 제외
        target_sectors = [s for s in hot if s not in overweight]
        if not target_sectors:
            self.log.info("대상 섹터 없음 (핫섹터=%s, 과대비중=%s)", hot, overweight)
            context["candidates"] = []
            return {"candidates": [], "count": 0}

        proposed = propose_candidates(target_sectors, config.MAX_CANDIDATES_PER_SECTOR)

        candidates: list[dict] = []
        seen: set[str] = set()
        per_sector_count: dict[str, int] = {}

        for c in proposed:
            ticker = c["ticker"]
            sector = c["sector"]
            if ticker in held or ticker in seen or sector in overweight:
                continue
            if per_sector_count.get(sector, 0) >= config.MAX_CANDIDATES_PER_SECTOR:
                continue
            seen.add(ticker)

            f = _fetch_fundamentals(ticker)
            mc, av = f["market_cap"], f["avg_volume"]
            if mc is None or av is None:
                self.log.info("스킵(데이터 없음): %s", ticker)
                continue
            if mc < config.MIN_MARKET_CAP or av < config.MIN_AVG_VOLUME:
                self.log.info("스킵(필터): %s mc=%s av=%s", ticker, mc, av)
                continue

            per_sector_count[sector] = per_sector_count.get(sector, 0) + 1
            c["market_cap"] = mc
            c["avg_volume"] = av
            candidates.append(c)

        self.log.info("후보 %d종목 선별 (제안 %d)", len(candidates), len(proposed))
        context["candidates"] = candidates
        return {"candidates": candidates, "count": len(candidates)}
