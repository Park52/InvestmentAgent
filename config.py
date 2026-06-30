"""전역 설정.

API 키, 모델명, 경로, 임계값, 스케줄을 한 곳에서 관리한다.
다른 모듈은 `from config import X`로 참조한다.

.env 파일에서 비밀값(API 키)을 로드한다. python-dotenv가 없거나 .env가
없어도 import가 깨지지 않도록 방어한다.
"""

import os

# ---------------------------------------------------------------------------
# .env 로드 (선택적)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv 미설치 시에도 동작
    pass


def _env(key: str, default=None):
    """환경변수 조회 헬퍼 (빈 문자열은 미설정으로 취급)."""
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, default))
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: list[str]) -> list[str]:
    """콤마 구분 환경변수를 리스트로 파싱한다. 미설정 시 default."""
    raw = _env(key)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

DB_PATH = os.path.join(DATA_DIR, "investment.db")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma_db")
CHROMA_COLLECTION = "news"
LOG_PATH = os.path.join(DATA_DIR, "agent.log")
PORTFOLIO_PATH = os.path.join(BASE_DIR, "portfolio.json")


def ensure_dirs() -> None:
    """런타임에 필요한 디렉토리를 생성한다 (scheduler 진입 시 호출)."""
    for path in (DATA_DIR, REPORTS_DIR, CHROMA_PATH):
        os.makedirs(path, exist_ok=True)


# ---------------------------------------------------------------------------
# API 키 (.env)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
NEWSAPI_KEY = _env("NEWSAPI_KEY")


# ---------------------------------------------------------------------------
# 모델 (CLAUDE.md 모델 사용 기준)
#   - 단순 분류/요약/추출 → 빠르고 저렴한 Haiku
#   - 차트 해석/Bull·Bear/리포트 → 추론에 강한 Sonnet
# ---------------------------------------------------------------------------
MODEL_FAST = _env("MODEL_FAST", "claude-haiku-4-5")
MODEL_SMART = _env("MODEL_SMART", "claude-sonnet-4-6")

# LLM 호출 기본값
MAX_TOKENS_FAST = _env_int("MAX_TOKENS_FAST", 1024)
MAX_TOKENS_SMART = _env_int("MAX_TOKENS_SMART", 4096)
# 리포트는 종목별 Bull/Bear·표가 길어 별도로 더 큰 한도를 둔다(잘림 방지).
MAX_TOKENS_REPORT = _env_int("MAX_TOKENS_REPORT", 8192)
DEFAULT_TEMPERATURE = _env_float("DEFAULT_TEMPERATURE", 0.7)


# ---------------------------------------------------------------------------
# RAG (rag/embedder.py, rag/retriever.py)
# ---------------------------------------------------------------------------
RAG_TOP_K = _env_int("RAG_TOP_K", 5)
RAG_MAX_DISTANCE = _env_float("RAG_MAX_DISTANCE", 0.7)
RAG_CHUNK_SIZE = _env_int("RAG_CHUNK_SIZE", 500)


# ---------------------------------------------------------------------------
# 기술적 지표 (utils/indicators.py, agents/chart_analyzer.py)
# ---------------------------------------------------------------------------
HISTORY_PERIOD = _env("HISTORY_PERIOD", "6mo")
HISTORY_INTERVAL = _env("HISTORY_INTERVAL", "1d")
RSI_PERIOD = _env_int("RSI_PERIOD", 14)
MA_SHORT = _env_int("MA_SHORT", 20)
MA_LONG = _env_int("MA_LONG", 60)
RSI_OVERBOUGHT = _env_float("RSI_OVERBOUGHT", 70.0)
RSI_OVERSOLD = _env_float("RSI_OVERSOLD", 30.0)


# ---------------------------------------------------------------------------
# 거시 환경 (agents/macro_check.py, Agent 0)
#   VIX 기준으로 시장 리스크 레벨을 판정한다.
#   vix < MID -> LOW, MID <= vix < HIGH -> MID, vix >= HIGH -> HIGH
# ---------------------------------------------------------------------------
VIX_MID_THRESHOLD = _env_float("VIX_MID_THRESHOLD", 20.0)
VIX_HIGH_THRESHOLD = _env_float("VIX_HIGH_THRESHOLD", 30.0)

# yfinance 심볼
VIX_SYMBOL = _env("VIX_SYMBOL", "^VIX")
USDKRW_SYMBOL = _env("USDKRW_SYMBOL", "KRW=X")
US10Y_SYMBOL = _env("US10Y_SYMBOL", "^TNX")  # 미국 10년물 금리


# ---------------------------------------------------------------------------
# 종목 스크리너 (agents/stock_screener.py, Agent 5)
# ---------------------------------------------------------------------------
HOT_SECTOR_COUNT = _env_int("HOT_SECTOR_COUNT", 3)
MIN_MARKET_CAP = _env_float("MIN_MARKET_CAP", 1_000_000_000)   # 시총 하한 (USD)
MIN_AVG_VOLUME = _env_float("MIN_AVG_VOLUME", 500_000)         # 평균 거래량 하한
MAX_CANDIDATES_PER_SECTOR = _env_int("MAX_CANDIDATES_PER_SECTOR", 5)

# 관심 섹터 (PREFERRED_SECTORS) — 뉴스 핫섹터와 무관하게 항상 분석 대상에 포함한다.
#   콤마 구분. 예: "Energy,Industrials". SectorClassifier가 핫섹터에 합친다.
#   보유 섹터는 portfolio.json에서 자동 모니터링되므로 여기엔 보통 미보유 섹터를 넣는다.
#   과대비중 제외 로직은 그대로 유지된다(관심 섹터가 과대비중이면 신규 후보는 안 나옴).
PREFERRED_SECTORS = _env_list("PREFERRED_SECTORS", ["Energy", "Industrials"])


# ---------------------------------------------------------------------------
# 뉴스 수집 (agents/news_collector.py, agents/data_quality.py)
# ---------------------------------------------------------------------------
NEWS_MAX_AGE_DAYS = _env_int("NEWS_MAX_AGE_DAYS", 3)
NEWS_MAX_ARTICLES = _env_int("NEWS_MAX_ARTICLES", 100)

# 기본 RSS 피드 (필요 시 .env나 운영 중 확장)
NEWS_RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://finance.yahoo.com/news/rssindex",
]

# NewsAPI.org (보조 소스 — NEWSAPI_KEY가 있을 때만 사용, 없으면 RSS만)
#   NEWSAPI_QUERY가 있으면 /v2/everything(키워드 검색), 없으면 /v2/top-headlines(business).
NEWSAPI_CATEGORY = _env("NEWSAPI_CATEGORY", "business")
NEWSAPI_LANGUAGE = _env("NEWSAPI_LANGUAGE", "en")
NEWSAPI_QUERY = _env("NEWSAPI_QUERY")  # 예: "stocks OR earnings OR Fed"


# ---------------------------------------------------------------------------
# 포트폴리오 (agents/portfolio_context.py, Agent 4)
#   섹터 비중이 이 값(0~1) 이상이면 '과대비중'으로 보고 스크리너에 제외 신호를 보낸다.
# ---------------------------------------------------------------------------
PORTFOLIO_OVERWEIGHT_THRESHOLD = _env_float("PORTFOLIO_OVERWEIGHT_THRESHOLD", 0.30)


# ---------------------------------------------------------------------------
# 스케줄 (scheduler.py) - 매일 지정 시각 실행
# ---------------------------------------------------------------------------
SCHEDULE_HOUR = _env_int("SCHEDULE_HOUR", 7)
SCHEDULE_MINUTE = _env_int("SCHEDULE_MINUTE", 0)
TIMEZONE = _env("TIMEZONE", "Asia/Seoul")


# ---------------------------------------------------------------------------
# 피드백 추적 (agents/feedback_tracker.py, Agent 9)
# ---------------------------------------------------------------------------
FOLLOWUP_DAYS_SHORT = _env_int("FOLLOWUP_DAYS_SHORT", 5)
FOLLOWUP_DAYS_LONG = _env_int("FOLLOWUP_DAYS_LONG", 20)


# ---------------------------------------------------------------------------
# 알림 발송 (utils/notifier.py, ReportGeneratorAgent 마지막 단계에서 호출)
#   "telegram" | "gmail" | "none"
# ---------------------------------------------------------------------------
NOTIFICATION_CHANNEL = _env("NOTIFICATION_CHANNEL", "none")

# 텔레그램 (requests로 Bot API 직접 호출 — python-telegram-bot 불필요)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

# Gmail (stdlib smtplib 사용 — 앱 비밀번호 필요)
GMAIL_SENDER = _env("GMAIL_SENDER")
GMAIL_PASSWORD = _env("GMAIL_PASSWORD")       # 16자리 앱 비밀번호
GMAIL_RECIPIENT = _env("GMAIL_RECIPIENT")
