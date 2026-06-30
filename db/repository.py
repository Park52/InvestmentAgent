"""predictions 테이블 CRUD.

데이터 축적의 입출력 계층. schema.py가 정의한 predictions 테이블에 대해
저장/조회/후속가격 갱신을 담당한다.

설계 원칙:
- 테이블 초기화는 schema.init_db()의 책임. 이 모듈은 테이블이 있다고 가정한다.
- bull_case/bear_case는 list로 받으면 JSON 문자열로 저장하고, 조회 시 list로 복원한다.
- save_prediction은 스키마에 정의된 컬럼만 화이트리스트로 통과시킨다(오타/미정의 키 방어).
"""

import json
import sqlite3
from typing import Any, Optional

from db.schema import get_connection

# 외부에서 INSERT 가능한 컬럼. id/created_at은 DB가 채우므로 제외.
_INSERTABLE_COLUMNS = [
    "date", "ticker", "name", "sector",
    # 선행 지표
    "eps_revision_trend", "eps_revision_pct",
    "insider_buy_count", "insider_sell_count",
    "short_interest_ratio", "short_interest_change",
    "google_trend_score", "google_trend_change",
    # 기술적 지표
    "rsi_14", "macd_signal", "ma_20", "ma_60", "ma_cross", "volume_trend",
    "vix", "usdkrw", "market_risk_level",
    "news_sentiment_score", "news_count",
    "verdict", "confidence", "bull_case", "bear_case",
    "price_at_prediction", "price_5d_later", "price_20d_later",
    "return_5d", "return_20d",
]

# JSON 문자열로 직렬화/역직렬화하는 컬럼.
_JSON_COLUMNS = ("bull_case", "bear_case")

# 후속 가격/수익률 갱신 시 허용되는 컬럼 매핑 (price_col -> return_col).
_FOLLOWUP_FIELDS = {
    "price_5d_later": "return_5d",
    "price_20d_later": "return_20d",
}


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """sqlite3.Row를 dict로 변환하고 JSON 컬럼을 역직렬화한다."""
    if row is None:
        return None
    result = dict(row)
    for col in _JSON_COLUMNS:
        if col in result and result[col] is not None:
            try:
                result[col] = json.loads(result[col])
            except (json.JSONDecodeError, TypeError):
                # 손상된 값은 원문 유지 (조회가 깨지지 않도록)
                pass
    return result


def _rows_to_dicts(rows) -> list[dict]:
    return [_row_to_dict(r) for r in rows]


def _prepare_insert_values(data: dict) -> dict:
    """입력 dict에서 INSERT 가능한 컬럼만 추리고 JSON 컬럼을 직렬화한다."""
    values: dict[str, Any] = {}
    for col in _INSERTABLE_COLUMNS:
        if col not in data:
            continue
        value = data[col]
        if col in _JSON_COLUMNS and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        values[col] = value
    return values


def _insert_one(conn: sqlite3.Connection, data: dict) -> int:
    """단일 INSERT 실행 (커밋은 호출자 책임). 생성된 id 반환."""
    values = _prepare_insert_values(data)
    if "date" not in values or "ticker" not in values:
        raise ValueError("예측 저장에는 'date'와 'ticker'가 반드시 필요하다.")
    columns = list(values.keys())
    placeholders = ", ".join(["?"] * len(columns))
    column_list = ", ".join(columns)
    sql = f"INSERT INTO predictions ({column_list}) VALUES ({placeholders})"
    cursor = conn.execute(sql, [values[c] for c in columns])
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# 생성 (Create)
# ---------------------------------------------------------------------------

def save_prediction(data: dict) -> int:
    """예측 1건을 저장하고 생성된 id를 반환한다.

    data에는 최소한 date, ticker가 포함되어야 한다.
    스키마에 없는 키는 무시된다.
    """
    conn = get_connection()
    try:
        new_id = _insert_one(conn, data)
        conn.commit()
        return new_id
    finally:
        conn.close()


def save_predictions(rows: list[dict]) -> list[int]:
    """여러 예측을 한 트랜잭션에서 저장하고 id 목록을 반환한다."""
    conn = get_connection()
    try:
        ids = [_insert_one(conn, data) for data in rows]
        conn.commit()
        return ids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 조회 (Read)
# ---------------------------------------------------------------------------

def get_prediction(prediction_id: int) -> Optional[dict]:
    """id로 단건 조회."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_predictions_by_date(date: str) -> list[dict]:
    """특정 날짜(YYYY-MM-DD)의 모든 예측 조회."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE date = ? ORDER BY ticker", (date,)
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_recent_predictions(ticker: str, limit: int = 10) -> list[dict]:
    """특정 종목의 최근 예측 이력을 최신순으로 조회."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE ticker = ? ORDER BY date DESC, id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


def get_predictions_for_followup(target_date: str, price_field: str) -> list[dict]:
    """후속 가격 갱신 대상 조회.

    target_date에 예측된 건 중 price_field가 아직 비어있는(NULL) 것을 반환한다.
    FeedbackTracker가 'N일 전 예측'의 후속 가격을 채울 때 사용한다.

    예) 오늘 기준 5거래일 전 날짜를 target_date로, 'price_5d_later'를 price_field로 호출.
    """
    if price_field not in _FOLLOWUP_FIELDS:
        raise ValueError(
            f"price_field는 {list(_FOLLOWUP_FIELDS)} 중 하나여야 한다: {price_field!r}"
        )
    # price_field는 화이트리스트로 검증됐으므로 식별자 직접 삽입 안전.
    sql = (
        f"SELECT * FROM predictions WHERE date = ? AND {price_field} IS NULL "
        "ORDER BY ticker"
    )
    conn = get_connection()
    try:
        rows = conn.execute(sql, (target_date,)).fetchall()
        return _rows_to_dicts(rows)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 갱신 (Update)
# ---------------------------------------------------------------------------

def update_followup_price(
    prediction_id: int,
    price_field: str,
    price: float,
    return_value: Optional[float] = None,
) -> None:
    """후속 가격과(선택) 수익률을 갱신한다.

    price_field는 'price_5d_later' 또는 'price_20d_later'.
    return_value가 주어지면 대응하는 return 컬럼(return_5d/return_20d)도 함께 갱신한다.
    """
    if price_field not in _FOLLOWUP_FIELDS:
        raise ValueError(
            f"price_field는 {list(_FOLLOWUP_FIELDS)} 중 하나여야 한다: {price_field!r}"
        )
    return_field = _FOLLOWUP_FIELDS[price_field]

    if return_value is None:
        sql = f"UPDATE predictions SET {price_field} = ? WHERE id = ?"
        params: tuple = (price, prediction_id)
    else:
        sql = f"UPDATE predictions SET {price_field} = ?, {return_field} = ? WHERE id = ?"
        params = (price, return_value, prediction_id)

    conn = get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
