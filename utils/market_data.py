"""선행지표 수집 유틸.

종목(티커) 단위의 '선행성 높은' 수치 데이터를 yfinance / pytrends로 수집한다.
LLM을 쓰지 않고 코드로 직접 수집하며(데이터 철학), 어떤 호출이 실패해도
None으로 안전하게 폴백한다(파이프라인 회복탄력성).

수집 항목:
- EPS 추정치 변화 (eps_revision_trend up/down/flat, eps_revision_pct)
- 내부자 거래 (insider_buy_count, insider_sell_count; CEO/CFO 매수 가중치 2배)
- 공매도 (short_interest_ratio, short_interest_change; 전월 대비 % - yfinance 가용 범위)
- 구글 트렌드 (google_trend_score 0~100, google_trend_change 윈도우 내 변화)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


def _num(value) -> Optional[float]:
    """값을 float로 안전 변환. None/NaN/변환불가 → None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


# ---------------------------------------------------------------------------
# EPS 추정치 변화
# ---------------------------------------------------------------------------

def eps_revision(ticker: str) -> dict:
    """애널리스트 EPS 추정치 상향/하향 추세와 변화율을 반환한다.

    trend: 최근 30일 상향 분석가 수 vs 하향 수 비교 → up/down/flat (불명 시 None)
    pct:   earnings_estimate의 growth(%) (불명 시 None)
    """
    result = {"trend": None, "pct": None}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        rev = getattr(t, "eps_revisions", None)
        if rev is not None and getattr(rev, "empty", True) is False:
            row = rev.iloc[0]  # 현재 분기(0q) 기준
            up = _num(row.get("upLast30days"))
            down = _num(row.get("downLast30days"))
            if up is not None and down is not None:
                if up > down:
                    result["trend"] = "up"
                elif down > up:
                    result["trend"] = "down"
                else:
                    result["trend"] = "flat"

        est = getattr(t, "earnings_estimate", None)
        if est is not None and getattr(est, "empty", True) is False and "growth" in est:
            growth = _num(est.iloc[0].get("growth"))
            if growth is not None:
                result["pct"] = round(growth * 100, 2)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# 내부자 거래
# ---------------------------------------------------------------------------

def insider_activity(ticker: str, days: int = 30, now: Optional[datetime] = None) -> dict:
    """최근 N일 내부자 매수/매도 건수. CEO/CFO 거래는 가중치 2배.

    날짜 파싱 불가한 행은 포함(보수적). 데이터 없음 → 0/0. 조회 실패 → None/None.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).insider_transactions
        if df is None or getattr(df, "empty", True):
            return {"buy_count": 0, "sell_count": 0}

        buy = sell = 0
        for _, r in df.iterrows():
            # 거래일 파싱 (불명이면 포함)
            raw_date = r.get("Start Date") if "Start Date" in r else r.get("Date")
            include = True
            try:
                if raw_date is not None:
                    dt = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(str(raw_date))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    include = dt >= cutoff
            except (ValueError, TypeError):
                include = True
            if not include:
                continue

            txt = str(r.get("Transaction") or r.get("Text") or "").lower()
            position = str(r.get("Position") or "").lower()
            weight = 2 if ("ceo" in position or "cfo" in position) else 1
            if "buy" in txt or "purchase" in txt:
                buy += weight
            elif "sale" in txt or "sell" in txt:
                sell += weight
        return {"buy_count": buy, "sell_count": sell}
    except Exception:
        return {"buy_count": None, "sell_count": None}


# ---------------------------------------------------------------------------
# 공매도
# ---------------------------------------------------------------------------

def short_interest(ticker: str) -> dict:
    """공매도 비율과 전월 대비 변화율(%)을 반환한다.

    yfinance는 주 단위 공매도 변화를 제공하지 않아 전월(priorMonth) 대비를 사용한다.
    """
    result = {"ratio": None, "change": None}
    try:
        import yfinance as yf
        info = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            info = {}
        result["ratio"] = _num(info.get("shortRatio"))
        cur = _num(info.get("sharesShort"))
        prev = _num(info.get("sharesShortPriorMonth"))
        if cur is not None and prev not in (None, 0):
            result["change"] = round((cur - prev) / prev * 100, 2)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# 구글 트렌드
# ---------------------------------------------------------------------------

def google_trend(ticker: str, name: Optional[str] = None) -> dict:
    """검색 관심도(0~100)와 7일 윈도우 내 변화를 반환한다.

    pytrends는 rate limit/구조 변경에 취약하므로 실패는 모두 None으로 흡수한다.
    """
    result = {"score": None, "change": None}
    try:
        from pytrends.request import TrendReq
        keyword = name or ticker
        pytrends = TrendReq(hl="en-US", tz=0)
        pytrends.build_payload([keyword], timeframe="now 7-d")
        df = pytrends.interest_over_time()
        if df is None or getattr(df, "empty", True) or keyword not in df:
            return result
        series = df[keyword].dropna()
        if series.empty:
            return result
        score = int(series.iloc[-1])
        first = int(series.iloc[0])
        result["score"] = score
        result["change"] = score - first
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# 통합
# ---------------------------------------------------------------------------

def collect_leading_indicators(ticker: str, name: Optional[str] = None) -> dict:
    """선행지표 전부를 수집해 predictions 컬럼명에 맞는 평탄 dict로 반환한다."""
    eps = eps_revision(ticker)
    insider = insider_activity(ticker)
    short = short_interest(ticker)
    trend = google_trend(ticker, name)
    return {
        "eps_revision_trend": eps["trend"],
        "eps_revision_pct": eps["pct"],
        "insider_buy_count": insider["buy_count"],
        "insider_sell_count": insider["sell_count"],
        "short_interest_ratio": short["ratio"],
        "short_interest_change": short["change"],
        "google_trend_score": trend["score"],
        "google_trend_change": trend["change"],
    }
