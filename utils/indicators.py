"""기술적 지표 계산 (yfinance + pandas).

기술적 지표 계산 원칙 (CLAUDE.md):
  LLM에게 차트를 "보고 분석"하라고 하지 않는다.
  반드시 코드로 수치를 계산한 뒤 그 숫자를 LLM(ChartAnalyzerAgent)에 전달한다.

지표는 pandas/numpy로 직접 계산한다. (pandas-ta 0.3.14b는 numpy 2.0에서
`from numpy import NaN` 제거로 import가 깨지므로 사용하지 않는다.)

반환 dict는 predictions 스키마의 지표 컬럼과 1:1 대응한다:
  rsi_14, macd_signal, ma_20, ma_60, ma_cross, volume_trend (+ price)
"""

from typing import Optional

import pandas as pd

# MACD가 macd선과 시그널선을 "교차"로 보지 않고 같다고 볼 임계(노이즈 차단).
_MACD_NEUTRAL_EPS = 1e-9


# ---------------------------------------------------------------------------
# 데이터 수집
# ---------------------------------------------------------------------------

def fetch_history(ticker: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    """yfinance로 OHLCV 히스토리를 가져온다.

    ma_60 계산을 위해 최소 60거래일 이상이 필요하므로 기본 period는 6개월.
    """
    import yfinance as yf  # 지연 import: 테스트/미사용 경로에서 부담 줄임

    df = yf.Ticker(ticker).history(period=period, interval=interval)
    return df


# ---------------------------------------------------------------------------
# 개별 지표 (순수 pandas)
# ---------------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder 평활화 = alpha 1/period 의 지수이동평균
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi_series = 100 - (100 / (1 + rs))
    # avg_loss == 0 (상승만) 인 경우 rs=inf → rsi=100. NaN 방어.
    rsi_series = rsi_series.where(avg_loss != 0, 100.0)
    return rsi_series


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD선과 시그널선을 반환한다 (macd_line, signal_line)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


# ---------------------------------------------------------------------------
# 해석 헬퍼
# ---------------------------------------------------------------------------

def _last_valid(series: pd.Series) -> Optional[float]:
    """마지막 유효(NaN 아님) 값을 float로 반환, 없으면 None."""
    s = series.dropna()
    if s.empty:
        return None
    return float(s.iloc[-1])


def _macd_signal(macd_line: pd.Series, signal_line: pd.Series) -> Optional[str]:
    m = _last_valid(macd_line)
    s = _last_valid(signal_line)
    if m is None or s is None:
        return None
    diff = m - s
    if diff > _MACD_NEUTRAL_EPS:
        return "bullish"
    if diff < -_MACD_NEUTRAL_EPS:
        return "bearish"
    return "neutral"


def _ma_cross(ma_short: pd.Series, ma_long: pd.Series, lookback: int = 5) -> str:
    """최근 lookback봉 내 교차를 감지한다.

    (ma_short - ma_long) 부호가 음→양으로 바뀌면 golden, 양→음이면 dead,
    그 외 none.
    """
    diff = (ma_short - ma_long).dropna()
    if len(diff) < 2:
        return "none"
    window = diff.iloc[-(lookback + 1):] if len(diff) > lookback else diff
    signs = window.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    signs = signs[signs != 0]  # 0(정확히 동일)은 무시
    if len(signs) < 2:
        return "none"
    first, last = signs.iloc[0], signs.iloc[-1]
    if first < 0 and last > 0:
        return "golden"
    if first > 0 and last < 0:
        return "dead"
    return "none"


def _volume_trend(volume: pd.Series, window: int = 5) -> Optional[str]:
    """최근 window일 평균 거래량 vs 직전 window일 평균 → increasing/decreasing."""
    vol = volume.dropna()
    if len(vol) < window * 2:
        return None
    recent = vol.iloc[-window:].mean()
    prior = vol.iloc[-window * 2:-window].mean()
    if prior == 0:
        return None
    return "increasing" if recent >= prior else "decreasing"


# ---------------------------------------------------------------------------
# 종합 계산
# ---------------------------------------------------------------------------

def compute_indicators(df: pd.DataFrame) -> dict:
    """OHLCV DataFrame에서 지표 dict를 계산한다.

    df는 'Close'와 'Volume' 컬럼을 가져야 한다(yfinance 기본 형식).
    데이터가 부족한 항목은 None으로 둔다.
    """
    result = {
        "rsi_14": None,
        "macd_signal": None,
        "ma_20": None,
        "ma_60": None,
        "ma_cross": "none",
        "volume_trend": None,
        "price": None,
    }
    if df is None or df.empty or "Close" not in df:
        return result

    close = df["Close"].astype(float)

    result["price"] = _last_valid(close)
    result["rsi_14"] = _last_valid(rsi(close))

    macd_line, signal_line = macd(close)
    result["macd_signal"] = _macd_signal(macd_line, signal_line)

    ma_20_series = close.rolling(window=20).mean()
    ma_60_series = close.rolling(window=60).mean()
    result["ma_20"] = _last_valid(ma_20_series)
    result["ma_60"] = _last_valid(ma_60_series)
    result["ma_cross"] = _ma_cross(ma_20_series, ma_60_series)

    if "Volume" in df:
        result["volume_trend"] = _volume_trend(df["Volume"].astype(float))

    # 소수 자리 정리 (DB/LLM 가독성)
    for key in ("rsi_14", "ma_20", "ma_60", "price"):
        if result[key] is not None:
            result[key] = round(result[key], 2)

    return result


def analyze(ticker: str, period: str = "6mo", interval: str = "1d") -> dict:
    """종목의 OHLCV를 받아 지표 dict를 반환한다 (ticker 포함).

    데이터를 못 받으면 'error' 키를 채워 반환한다.
    """
    df = fetch_history(ticker, period=period, interval=interval)
    if df is None or df.empty:
        return {"ticker": ticker, "error": "no_data"}
    result = compute_indicators(df)
    result["ticker"] = ticker
    return result
