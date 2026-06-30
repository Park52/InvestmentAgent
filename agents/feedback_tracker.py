"""Agent 9: FeedbackTrackerAgent — 이력 기록 + 후속 가격 업데이트.

오늘의 예측을 predictions 테이블에 저장하고, 5일/20일 전 예측의 후속 가격을
조회해 수익률을 계산·반영한다. 데이터 축적의 마지막 단계.
"""

from datetime import datetime, timedelta, timezone

import config
from agents.base import BaseAgent
from db import repository


def _latest_price(ticker: str):
    """yfinance로 최신 종가를 조회한다. 실패 시 None."""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period="5d")
        if df is None or df.empty or "Close" not in df:
            return None
        series = df["Close"].dropna()
        if series.empty:
            return None
        return round(float(series.iloc[-1]), 2)
    except Exception:
        return None


_LEADING_KEYS = (
    "eps_revision_trend", "eps_revision_pct",
    "insider_buy_count", "insider_sell_count",
    "short_interest_ratio", "short_interest_change",
    "google_trend_score", "google_trend_change",
)


def _base_row(item: dict, date: str, macro: dict, sector_sentiment: dict,
              total_news_count: int) -> dict:
    """후보/보유 공통 행(거시·차트·선행지표·감성)을 만든다."""
    chart = item.get("chart", {}) or {}
    leading = item.get("leading", {}) or {}

    sent = sector_sentiment.get(item.get("sector"))
    if sent:
        news_sentiment_score = sent.get("score")
        news_count = sent.get("count")
    else:
        news_sentiment_score = None
        news_count = total_news_count

    row = {
        "date": date,
        "ticker": item["ticker"],
        "name": item.get("name"),
        "sector": item.get("sector"),
        "rsi_14": chart.get("rsi_14"),
        "macd_signal": chart.get("macd_signal"),
        "ma_20": chart.get("ma_20"),
        "ma_60": chart.get("ma_60"),
        "ma_cross": chart.get("ma_cross"),
        "volume_trend": chart.get("volume_trend"),
        "vix": macro.get("vix"),
        "usdkrw": macro.get("usdkrw"),
        "market_risk_level": macro.get("market_risk_level"),
        "news_sentiment_score": news_sentiment_score,
        "news_count": news_count,
        "price_at_prediction": chart.get("price"),
    }
    # 선행지표 8컬럼 (없으면 None)
    for key in _LEADING_KEYS:
        row[key] = leading.get(key)
    return row


def _build_rows(context: dict) -> list[dict]:
    """thesis(후보) + holdings(보유)를 predictions 스키마 행으로 변환한다.

    - 후보: 선행지표 + 차트 + verdict/bull/bear (full)
    - 보유: 선행지표 + 차트만 (verdict 없음) → 보유 종목 데이터도 축적
    """
    macro = context.get("macro", {}) or {}
    theses = context.get("theses", []) or []
    holdings_data = context.get("holdings_data", []) or []
    date = context.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_news_count = len(context.get("news", []) or [])
    sector_sentiment = context.get("sector_sentiment", {}) or {}

    rows = []

    # 후보 종목 (full)
    for t in theses:
        if not t.get("ticker"):
            continue
        row = _base_row(t, date, macro, sector_sentiment, total_news_count)
        row["verdict"] = t.get("verdict")
        row["confidence"] = t.get("confidence")
        row["bull_case"] = t.get("bull_case")
        row["bear_case"] = t.get("bear_case")
        rows.append(row)

    # 보유 종목 (선행지표/차트만, verdict 없음)
    for h in holdings_data:
        if not h.get("ticker"):
            continue
        rows.append(_base_row(h, date, macro, sector_sentiment, total_news_count))

    return rows


def _update_followups(today: datetime) -> int:
    """N일 전 예측의 후속 가격/수익률을 갱신하고 갱신 건수를 반환한다."""
    updated = 0
    plan = [
        (config.FOLLOWUP_DAYS_SHORT, "price_5d_later"),
        (config.FOLLOWUP_DAYS_LONG, "price_20d_later"),
    ]
    for days, field in plan:
        target_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        pending = repository.get_predictions_for_followup(target_date, field)
        for p in pending:
            price = _latest_price(p["ticker"])
            if price is None:
                continue
            base = p.get("price_at_prediction")
            ret = round((price - base) / base * 100, 2) if base else None
            repository.update_followup_price(p["id"], field, price, ret)
            updated += 1
    return updated


class FeedbackTrackerAgent(BaseAgent):
    name = "FeedbackTrackerAgent"

    def execute(self, context: dict) -> dict:
        rows = _build_rows(context)
        ids = repository.save_predictions(rows) if rows else []

        today_str = context.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        updated = _update_followups(today)

        self.log.info("예측 %d건 저장, 후속 가격 %d건 갱신", len(ids), updated)
        context["saved_ids"] = ids
        return {"saved": len(ids), "followups_updated": updated}
