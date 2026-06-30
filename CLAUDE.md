# CLAUDE.md — Investment Agent 프로젝트

## 프로젝트 개요

멀티 에이전트 기반 투자 정보 수집·분석 시스템.
다중 데이터 수집 → 섹터 분류 → 종목 선별 → 차트 분석 → 상승여력 검증 → 리포트 생성까지
자동화된 파이프라인을 구성한다.

**핵심 목표: 투자 결정을 대신하는 게 아니라, 선행 지표 데이터 축적을 통해
장기적으로 적중률 높은 패턴을 발견하는 것.**

### 데이터 철학

- 뉴스는 단독으로 쓰면 노이즈. "시그널 감지 트리거" 역할로만 사용하고 감성 점수(수치)로 변환해 저장
- 수치 데이터가 핵심이지만 기술적 지표(RSI 등)는 후행. 선행성 있는 수치 데이터를 우선 수집
- 선행성 높은 순서: EPS 추정치 변화 > 내부자 거래 > 공매도 잔고 > 구글 트렌드 > 기술적 지표
- 데이터가 쌓일수록 "어떤 피처 조합이 실제 수익률과 연관됐는지" 패턴이 보임

---

## 절대 규칙 (반드시 준수)

1. **코드 작성 전에 반드시 프로젝트 구조를 분석하고 보고한다.**
   - 어떤 파일이 있는지, 어떤 에이전트가 구현됐는지 먼저 파악
   - 분석 결과를 사용자에게 보고한 후 구현 계획을 제시
   - 승인 없이 구현 시작 금지

2. **구현 전 계획을 먼저 제시하고 승인을 받는다.**
   - 무엇을 만들지, 어떤 파일을 수정/생성할지 목록화
   - 사용자 승인 후 구현 시작

3. **한 번에 하나의 에이전트 또는 모듈만 구현한다.**
   - 여러 파일을 한꺼번에 수정하지 않는다
   - 각 단계마다 테스트 후 다음 단계 진행

4. **TODO 주석을 절대 그냥 두지 않는다.**
   - 구현 범위 내 TODO는 반드시 실제 코드로 채운다
   - 구현 범위 밖 TODO는 명시적으로 "다음 단계" 항목으로 기록

5. **리포트에는 반드시 면책 문구를 포함한다.**
   - "이 분석은 정보 제공 목적이며 투자 결정의 근거로 단독 사용 불가"
   - ReportGeneratorAgent가 이를 강제 삽입

---

## 프로젝트 구조

```
investment-agent/
├── CLAUDE.md                  ← 이 파일
├── config.py                  ← 전역 설정 (API 키, 모델, 스케줄)
├── scheduler.py               ← 파이프라인 진입점
├── requirements.txt
├── .env.example               ← .env 템플릿
├── agents/
│   ├── base.py                ← BaseAgent, AgentResult (모든 에이전트 공통)
│   ├── macro_check.py         ← Agent 0: 거시환경 체크 (VIX, 금리, 환율)
│   ├── data_quality.py        ← Agent 1: 수집 데이터 유효성 필터
│   ├── data_collector.py      ← Agent 2: 다중 데이터 수집 (뉴스+선행지표)
│   ├── sector_classifier.py   ← Agent 3: 섹터 분류 + 핫섹터 선정
│   ├── portfolio_context.py   ← Agent 4: 보유 종목/비중 반영
│   ├── stock_screener.py      ← Agent 5: 후보 종목 선별
│   ├── chart_analyzer.py      ← Agent 6: 기술적 지표 계산 + 해석
│   ├── thesis_validator.py    ← Agent 7: Bull/Bear case 생성 (분리 호출)
│   ├── report_generator.py    ← Agent 8: 최종 리포트 생성
│   └── feedback_tracker.py    ← Agent 9: 이력 기록 + 후속 가격 업데이트
├── rag/
│   ├── embedder.py            ← 뉴스 → 벡터 변환 → ChromaDB 저장
│   └── retriever.py           ← 유사 뉴스 검색
├── db/
│   ├── schema.py              ← DB 테이블 정의 (SQLite)
│   └── repository.py          ← DB CRUD 메서드
├── data/
│   ├── chroma_db/             ← ChromaDB 벡터 저장소
│   ├── investment.db          ← SQLite 예측 이력 DB
│   └── agent.log              ← 실행 로그
├── reports/
│   └── report_YYYYMMDD.md     ← 날짜별 리포트
└── utils/
    ├── llm.py                 ← Claude API 래퍼 + 토큰 추적
    ├── indicators.py          ← yfinance + pandas-ta 기술적 지표 계산
    └── notifier.py            ← 텔레그램 봇 (발송 + /run 명령 수신)
```

---

## 에이전트 파이프라인

```
[Agent 0] MacroCheckAgent
    VIX, 금리, 환율 조회 → 시장 리스크 레벨 판단 (LOW/MID/HIGH)
    HIGH이면 이후 에이전트에 "관망 우선" 플래그 전달
    ↓
[Agent 1] DataQualityAgent
    수집된 데이터 유효성 검증
    뉴스: 중복 URL, 오래된 기사(48시간 초과) 필터
    수치 데이터: 결측값, 이상치 체크
    품질 통과 데이터만 다음 단계로 전달
    ↓
[Agent 2] DataCollectorAgent  ← 핵심 변경 (NewsCollector 대체)
    ├── 뉴스 수집 (feedparser/NewsAPI)
    │     Haiku로 감성 점수(-1.0~1.0) 수치화 → RAG 저장
    │     뉴스 자체보다 "감성 점수"가 DB에 저장되는 값
    ├── EPS 추정치 변화 (yfinance)
    │     analyst_price_targets, earnings_estimate
    │     상향/하향/유지 → up/down/flat + 변화율 %
    ├── 내부자 거래 (yfinance / SEC EDGAR)
    │     최근 30일 내부자 매수/매도 건수
    │     CEO/CFO 매수는 가중치 2배
    ├── 공매도 잔고 (yfinance)
    │     short_interest_ratio, 전주 대비 변화율
    └── 구글 트렌드 (pytrends)
          종목명/티커 검색량 0~100, 전주 대비 변화
    ↓
[Agent 3] SectorClassifierAgent
    뉴스 감성 점수 + EPS 변화 + 내부자 거래를 종합해 핫섹터 선정
    (기존 뉴스 언급량 단독 → 다중 지표 가중 합산으로 변경)
    ↓
[Agent 4] PortfolioContextAgent
    portfolio.json에서 현재 보유 종목/비중 로드
    이미 과대비중인 섹터 플래그 → 스크리너에 전달
    ↓
[Agent 5] StockScreenerAgent
    핫섹터에서 후보 종목 추출 (Haiku)
    yfinance로 시총/거래량 필터
    포트폴리오 중복 섹터 제외
    ↓
[Agent 6] ChartAnalyzerAgent
    yfinance + pandas-ta로 RSI/MACD/이평선 계산 (코드, LLM 아님)
    수치를 Sonnet에 주입해서 해석 요청
    ↓
[Agent 7] ThesisValidatorAgent
    RAG로 종목별 관련 뉴스 검색
    Bull case: 1차 Sonnet 호출 (차트 분석 결과 포함)
    Bear case: 2차 Sonnet 호출 (Bull case 컨텍스트 숨김, 비관론자 역할 강제)
    종합 verdict + confidence 산출
    ↓
[Agent 8] ReportGeneratorAgent
    전체 결과 취합 → Sonnet으로 리포트 작성
    면책 문구 강제 삽입
    reports/report_YYYYMMDD.md 저장
    NotificationService.send() 호출 → 텔레그램 리포트 발송
    ↓
[Agent 9] FeedbackTrackerAgent
    predictions 테이블에 오늘 예측 저장
    5일/20일 전 예측의 후속 가격 자동 업데이트
    수익률 계산 후 DB 반영
```

---

## DB 스키마 (predictions 테이블)

데이터 축적의 핵심. 나중에 패턴 분석/ML에 쓰기 위해
**수치 피처를 반드시 저장**한다.

```sql
CREATE TABLE predictions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT NOT NULL,          -- YYYY-MM-DD
    ticker               TEXT NOT NULL,
    name                 TEXT,
    sector               TEXT,

    -- 선행 지표 (DataCollector 수집) ★ 핵심
    eps_revision_trend   TEXT,                   -- up/down/flat
    eps_revision_pct     REAL,                   -- 변화율 %
    insider_buy_count    INTEGER,                -- 최근 30일 내부자 매수
    insider_sell_count   INTEGER,                -- 최근 30일 내부자 매도
    short_interest_ratio REAL,                   -- 공매도 비율
    short_interest_change REAL,                  -- 전주 대비 변화
    google_trend_score   INTEGER,                -- 0~100
    google_trend_change  INTEGER,                -- 전주 대비 변화

    -- 뉴스 감성 (수치화된 값만 저장)
    news_sentiment_score REAL,                   -- -1.0 ~ 1.0
    news_count           INTEGER,

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
```

---

## 모델 사용 기준

| 작업 | 모델 | 이유 |
|------|------|------|
| 뉴스 감성 점수 추출, 섹터 분류, 종목 추출 | claude-haiku-4-5 | 단순 분류, 비용 절감 |
| 차트 해석, Bull/Bear case, 리포트 | claude-sonnet-4-6 | 복잡한 추론 |
| Bear case 생성 | claude-sonnet-4-6 별도 호출 | Bull 컨텍스트 격리 필수 |
| EPS/내부자/공매도 데이터 수집 | LLM 사용 안 함 | yfinance 코드로 직접 수집 |

---

## RAG 구현 원칙

- 임베딩: ChromaDB 내장 임베딩 사용 (외부 API 비용 없음)
- 청크 크기: 500자 이하
- 중복 방지: URL MD5 해시를 document ID로 사용
- 검색 시: top_k=5, 거리 0.7 이상이면 "관련 뉴스 없음" 처리

---

## 텔레그램 봇 (NotificationService)

별도 에이전트가 아닌 `utils/notifier.py` 유틸로 구현.
**단방향 발송 + 양방향 명령 수신** 모두 담당한다.

### 동작 방식: Polling (권장)

```
재현님 텔레그램              로컬 PC (scheduler.py 실행 중)
      │                                │
      │  /run                          │
      │ ─────────────────────────────► │
      │                       run_pipeline() 실행
      │  ⏳ "파이프라인 시작됨..."      │
      │ ◄───────────────────────────── │
      │                                │  (분석 진행 중)
      │  ✅ 리포트 + 요약 전송          │
      │ ◄───────────────────────────── │
```

- Webhook 방식 사용 안 함 (외부 IP/포트 불필요)
- 봇이 주기적으로 텔레그램 서버에 폴링 → 로컬 PC에서 그냥 실행
- `scheduler.py` 실행 시 스케줄러 + 봇 폴링이 함께 시작됨

### 지원 명령어

| 명령어 | 동작 |
|--------|------|
| `/run` | 파이프라인 즉시 실행 → 완료 후 리포트 전송 |

### config.py 설정

```python
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
```

### NotificationService 구조

```python
# utils/notifier.py
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

class NotificationService:

    # ── 발송 (ReportGeneratorAgent에서 호출) ──────────
    async def send(self, summary: str, report_path: str):
        """파이프라인 완료 후 리포트 요약 발송"""
        await self._bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=summary,
            parse_mode="Markdown"
        )

    # ── 명령 수신 ──────────────────────────────────────
    async def _handle_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/run 명령 처리"""
        await update.message.reply_text("⏳ 파이프라인 시작... 완료되면 리포트를 전송할게요.")
        from scheduler import run_pipeline
        run_pipeline()   # 동기 실행 (파이프라인 내부에서 send() 호출됨)

    # ── 봇 시작 (polling) ──────────────────────────────
    def start_bot(self):
        """scheduler.py에서 호출. 블로킹 루프 시작."""
        app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("run", self._handle_run))
        app.run_polling()
```

### scheduler.py 통합 구조

```python
# scheduler.py
def main():
    notifier = NotificationService()

    # 스케줄러: 매일 07:00 자동 실행 (백그라운드)
    bg_scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    bg_scheduler.add_job(run_pipeline, "cron", hour=7, minute=0)
    bg_scheduler.start()

    # 텔레그램 봇: /run 명령 대기 (포그라운드 블로킹)
    notifier.start_bot()
```

### 텔레그램 봇 설정 방법

```
1. 텔레그램에서 @BotFather 검색
2. /newbot 명령어로 봇 생성 → BOT_TOKEN 발급
3. 봇과 대화 시작 후 아래 URL로 CHAT_ID 확인:
   https://api.telegram.org/bot{TOKEN}/getUpdates
4. .env에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 입력
```

### 리포트 발송 메시지 형식

```
[투자 리포트] 2024-01-15

📊 오늘의 핫섹터: AI, 반도체
🔍 분석 종목: NVDA, TSM, ASML
⚠️ 시장 리스크: LOW

상위 추천:
• NVDA — bullish (confidence 78)
• TSM  — bullish (confidence 65)

전체 리포트: reports/report_20240115.md

⚠️ 이 분석은 정보 제공 목적이며 투자 결정의 근거로 단독 사용 불가
```

---

## DataCollector 수집 데이터 상세

### 데이터별 선행성과 수집 방법

| 데이터 | 선행성 | 라이브러리 | 비용 |
|--------|--------|-----------|------|
| EPS 추정치 변화 | ★★★★ | yfinance | 무료 |
| 내부자 거래 | ★★★★ | yfinance / SEC EDGAR | 무료 |
| 공매도 잔고 변화 | ★★★ | yfinance | 무료 |
| 구글 트렌드 | ★★★ | pytrends | 무료 |
| 뉴스 감성 점수 | ★★ | feedparser + Haiku | 소량 토큰 |
| 기술적 지표 | ★ (후행) | pandas-ta | 무료 |

### 수집 코드 패턴

```python
# EPS 추정치 변화
ticker = yf.Ticker("NVDA")
estimates = ticker.earnings_estimate        # 분기/연간 EPS 추정
price_targets = ticker.analyst_price_targets  # 목표주가 변화

# 내부자 거래
insider = ticker.insider_transactions       # 최근 내부자 거래 내역

# 공매도
short_info = ticker.fast_info               # shortRatio 포함

# 구글 트렌드
from pytrends.request import TrendReq
pytrends = TrendReq()
pytrends.build_payload(["NVDA", "Nvidia"], timeframe="now 7-d")
trend_data = pytrends.interest_over_time()
```

### 뉴스는 이렇게 처리

뉴스 원문은 RAG에 저장. DB에는 수치만 저장.

```python
# 뉴스 → 감성 점수 변환 (Haiku)
sentiment = call_claude(
    prompt=f"다음 뉴스의 {ticker} 주가에 대한 감성을 -1.0(매우부정)~1.0(매우긍정) 숫자 하나로만 답하라:\n{news_text}",
    model=MODEL_CHEAP
)
score = float(sentiment.strip())   # DB에 이 숫자만 저장
```

---

## Bear case 생성 규칙 (중요)

ThesisValidatorAgent에서 Bull/Bear를 같은 컨텍스트로 생성하면
진짜 반론이 안 나온다. 반드시 아래 방식 준수:

```python
# 1차 호출: Bull case
bull_result = call_claude(
    prompt=f"차트 지표: {chart_data}\n관련뉴스: {rag_docs}\n매수 근거 3가지를 작성하라",
    system="당신은 강세론자 애널리스트다.",
    model=MODEL_SMART
)

# 2차 호출: Bear case (bull_result를 프롬프트에 넣지 않음)
bear_result = call_claude(
    prompt=f"종목: {ticker}\n섹터: {sector}\n이 종목의 위험 요인과 하락 근거 3가지를 작성하라",
    system="당신은 비관론자 애널리스트다. 어떤 종목이든 반드시 위험 요인을 찾아낸다.",
    model=MODEL_SMART
)
```

---

## 기술적 지표 계산 원칙

**LLM에게 차트를 "보고 분석"하라고 하지 않는다.**
반드시 yfinance + pandas-ta로 수치를 계산한 뒤 그 숫자를 LLM에 전달한다.

```python
# utils/indicators.py 에서 계산
# agents/chart_analyzer.py 에서 수치를 문자열로 만들어 Sonnet에 주입
prompt = f"""
종목: {ticker}
RSI(14): {rsi:.1f}   # 70 이상 과매수, 30 이하 과매도
MACD: {macd_signal}
20일 이평선: {ma_20:.2f}, 60일 이평선: {ma_60:.2f}
골든크로스 여부: {ma_cross}
거래량 추세: {volume_trend}

위 수치를 바탕으로 기술적 분석 해석을 작성하라.
"""
```

---

## 환경 설정

```bash
# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일에 ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 입력

# 즉시 실행 (테스트)
python scheduler.py --now

# 스케줄러 + 텔레그램 봇 시작 (매일 07:00 자동 실행 + /run 명령 대기)
python scheduler.py
```

---

## 의존성

```
anthropic>=0.40.0           LLM 호출
yfinance>=0.2.40            주가/재무/내부자거래/공매도 데이터
pandas>=2.0.0
pandas-ta>=0.3.14b          기술적 지표 계산
chromadb>=0.5.0             벡터 DB
feedparser>=6.0.0           RSS 파싱
requests>=2.31.0            HTTP 요청
pytrends>=4.9.0             구글 트렌드 데이터
apscheduler>=3.10.0         스케줄러
python-dotenv>=1.0.0        .env 로드
python-telegram-bot>=20.0   텔레그램 발송 (선택)
```

---

## 현재 구현 상태

| 파일 | 상태 |
|------|------|
| config.py | ✅ 완료 |
| scheduler.py | ✅ 완료 (BackgroundScheduler + 봇 폴링) |
| utils/llm.py | ✅ 완료 |
| utils/indicators.py | ✅ 완료 (pandas/numpy 직접 계산) |
| utils/market_data.py | ✅ 완료 (EPS/내부자/공매도/구글트렌드) |
| utils/notifier.py | ✅ 완료 (발송 + /run 롱폴링, requests) |
| agents/base.py | ✅ 완료 |
| agents/macro_check.py | ✅ 완료 |
| agents/data_quality.py | ✅ 완료 (피드 게이트) |
| agents/data_collector.py | ✅ 완료 (뉴스+감성, news_collector 대체) |
| agents/sector_classifier.py | ✅ 완료 (감성 가중 핫섹터) |
| agents/portfolio_context.py | ✅ 완료 |
| agents/stock_screener.py | ✅ 완료 (과대비중 섹터 제외) |
| agents/chart_analyzer.py | ✅ 완료 (차트 + 후보·보유 선행지표) |
| agents/thesis_validator.py | ✅ 완료 (Bull/Bear 격리) |
| agents/report_generator.py | ✅ 완료 (면책 강제 + 선행지표/보유 반영) |
| agents/feedback_tracker.py | ✅ 완료 (후보+보유 저장, 후속가격 추적) |
| rag/embedder.py | ✅ 완료 |
| rag/retriever.py | ✅ 완료 |
| db/schema.py | ✅ 완료 (34컬럼 + 멱등 마이그레이션) |
| db/repository.py | ✅ 완료 |

범례: ✅ 완료 / 🔧 뼈대만 / ⬜ 미구현

> **구현 노트 (결정 사항)**
> - 선행지표(EPS/내부자/공매도/구글트렌드)는 종목 단위라 **후보 확정(Agent 5) 이후**
>   ChartAnalyzer(Agent 6)에서 `utils/market_data.py`로 수집한다. 대상은 **후보 + 보유 종목**.
>   따라서 Agent 1(품질)은 소스 게이트, Agent 3(핫섹터)은 **뉴스 감성 가중**까지만 사용한다
>   (EPS/내부자 가중은 스크리닝 전이라 적용 불가).
> - 보유 종목은 verdict 없이 선행지표/차트만 predictions에 저장해 데이터를 축적한다.
> - 텔레그램은 `requests` 롱폴링으로 `/run`을 수신한다(python-telegram-bot 불필요).
> - pandas-ta는 numpy 2.0 비호환 + PyPI 미제공으로 제외, 지표는 직접 계산한다.
> - **관심 섹터(`config.PREFERRED_SECTORS`, `.env`로 오버라이드)**: 뉴스 핫섹터와 무관하게
>   항상 분석 대상에 포함한다(SectorClassifier가 핫섹터에 병합, 뉴스 0건이어도 유지).
>   과대비중 제외 로직은 그대로 유지 → 보유 섹터는 모니터링만, 발굴은 비보유 관심 섹터에서.
>   기본값 `Energy,Industrials`. 보유 섹터는 portfolio.json에서 자동 모니터링되므로 중복 불필요.

---

## 다음 구현 순서 (권장)

1. `db/schema.py` + `db/repository.py` — 데이터 저장 구조 먼저 확정
2. `rag/embedder.py` + `rag/retriever.py` — RAG 코어 구현
3. `utils/indicators.py` — 기술적 지표 계산 유틸
4. `utils/notifier.py` — 텔레그램 봇 (Polling + /run 명령)
5. `agents/macro_check.py` — Agent 0
6. `agents/data_collector.py` — Agent 2 (뉴스+EPS+내부자거래+공매도+구글트렌드)
7. 나머지 에이전트 순서대로
8. `agents/feedback_tracker.py` — 마지막 (DB 스키마 확정 후)
