"""DB 스키마 정의 (SQLite).

데이터 축적의 핵심 모듈. predictions 테이블에 예측 결과와 함께
기술적 지표·거시환경·뉴스 감성을 '수치 피처'로 저장한다.
나중에 패턴 분석/ML에 활용하기 위함.

CRUD 메서드는 db/repository.py에 구현한다. 이 파일은 테이블 정의와
초기화(init_db)만 담당한다.
"""

import os
import sqlite3

# config.py가 아직 비어 있을 수 있으므로 import 실패 시 기본값으로 fallback.
try:
    from config import DB_PATH  # type: ignore
except Exception:  # pragma: no cover - config 미구현 단계 대비
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(_BASE_DIR, "data", "investment.db")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_PREDICTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS predictions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT NOT NULL,          -- YYYY-MM-DD
    ticker               TEXT NOT NULL,
    name                 TEXT,
    sector               TEXT,

    -- 선행 지표 (DataCollector/ChartAnalyzer 수집) ★ 핵심
    eps_revision_trend   TEXT,                   -- up/down/flat
    eps_revision_pct     REAL,                   -- 변화율 %
    insider_buy_count    INTEGER,                -- 최근 30일 내부자 매수
    insider_sell_count   INTEGER,                -- 최근 30일 내부자 매도
    short_interest_ratio REAL,                   -- 공매도 비율
    short_interest_change REAL,                  -- 전주 대비 변화
    google_trend_score   INTEGER,                -- 0~100
    google_trend_change  INTEGER,                -- 전주 대비 변화

    -- 기술적 지표 (후행, 타이밍 보조용)
    rsi_14               REAL,
    macd_signal          TEXT,                   -- bullish/bearish/neutral
    ma_20                REAL,
    ma_60                REAL,
    ma_cross             TEXT,                   -- golden/dead/none
    volume_trend         TEXT,                   -- increasing/decreasing

    -- 거시 환경
    vix                  REAL,
    usdkrw               REAL,
    market_risk_level    TEXT,                   -- LOW/MID/HIGH

    -- 뉴스 감성
    news_sentiment_score REAL,                   -- -1.0 ~ 1.0
    news_count           INTEGER,

    -- 예측 결과
    verdict              TEXT,                   -- bullish/neutral/bearish
    confidence           INTEGER,                -- 0~100
    bull_case            TEXT,                   -- JSON array
    bear_case            TEXT,                   -- JSON array

    -- 가격 추적 (FeedbackTracker가 채움)
    price_at_prediction  REAL,
    price_5d_later       REAL,
    price_20d_later      REAL,
    return_5d            REAL,
    return_20d           REAL,

    created_at           TEXT DEFAULT (datetime('now'))
);
"""

# 조회 패턴: 날짜별 리포트, 종목별 이력 추적, FeedbackTracker의 과거 예측 갱신
CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(date);",
    "CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);",
    "CREATE INDEX IF NOT EXISTS idx_predictions_ticker_date ON predictions(ticker, date);",
]

# 기존 DB(구 스키마)에 누락된 컬럼을 채우는 멱등 마이그레이션 목록.
# (컬럼명 -> 타입). ALTER TABLE ADD COLUMN으로 추가하며 기존 데이터는 보존된다.
_MIGRATION_COLUMNS = {
    "eps_revision_trend": "TEXT",
    "eps_revision_pct": "REAL",
    "insider_buy_count": "INTEGER",
    "insider_sell_count": "INTEGER",
    "short_interest_ratio": "REAL",
    "short_interest_change": "REAL",
    "google_trend_score": "INTEGER",
    "google_trend_change": "INTEGER",
}


def _migrate_predictions(conn: sqlite3.Connection) -> None:
    """predictions 테이블에 누락된 선행지표 컬럼을 추가한다(멱등).

    SQLite는 ALTER TABLE ADD COLUMN만 지원하므로, 기존 컬럼은 PRAGMA로 확인 후
    없는 것만 추가한다. 기존 행은 새 컬럼에 NULL이 채워진다.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    for col, col_type in _MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# 연결 / 초기화
# ---------------------------------------------------------------------------

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """SQLite 연결을 반환한다.

    - DB 파일이 위치할 디렉토리가 없으면 생성한다.
    - row_factory를 sqlite3.Row로 설정해 컬럼명으로 접근 가능하게 한다.
    - 외래키 제약을 활성화한다.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """테이블과 인덱스를 생성한다. 이미 존재하면 무시한다(idempotent)."""
    conn = get_connection(db_path)
    try:
        conn.execute(CREATE_PREDICTIONS_TABLE)
        _migrate_predictions(conn)  # 기존 DB에 선행지표 컬럼 보강
        for index_sql in CREATE_INDEXES:
            conn.execute(index_sql)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"[schema] DB 초기화 완료: {DB_PATH}")
