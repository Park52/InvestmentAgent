"""Agent 2: DataCollectorAgent — 뉴스 수집 + 감성 수치화 + RAG 저장.

(구 NewsCollectorAgent를 개명·확장한 모듈)

RSS/NewsAPI에서 기사를 수집하고, 중복/오래된 기사를 거른 뒤, Haiku로 3줄 요약하고
감성 점수(-1.0~1.0)를 수치화해 ChromaDB(rag.embedder)에 저장한다.

데이터 철학: 뉴스 원문은 RAG에, DB에는 '감성 점수' 수치만 저장한다.
EPS/내부자/공매도/구글트렌드 등 종목 단위 선행지표는 후보 확정(Agent 5) 이후
ChartAnalyzer(Agent 6)에서 utils.market_data로 수집한다.

설계:
- 외부 의존(feedparser / requests / LLM / embedder)을 함수 경계로 분리해 테스트 가능하게 한다.
- 한 피드/기사의 실패가 전체를 막지 않도록 예외를 기사 단위로 흡수한다.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from agents.base import BaseAgent
from rag import embedder
from utils import llm

_SUMMARY_SYSTEM = "당신은 금융 뉴스 요약가다. 핵심만 간결하게 정리한다."
_SENTIMENT_SYSTEM = (
    "당신은 금융 뉴스 감성 분석기다. 각 기사가 해당 종목/시장에 미치는 영향을 "
    "-1.0(매우 부정)에서 1.0(매우 긍정) 사이 실수로 평가한다. 중립은 0.0."
)

# 한 번의 LLM 호출로 감성 점수를 매길 기사 수
_SENTIMENT_BATCH_SIZE = 20


# ---------------------------------------------------------------------------
# 수집 / 파싱
# ---------------------------------------------------------------------------

def _parse_date(entry) -> tuple[Optional[str], Optional[datetime]]:
    """feedparser 엔트리에서 발행일을 (YYYY-MM-DD, datetime)로 파싱한다."""
    struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not struct:
        return None, None
    try:
        import time
        dt = datetime.fromtimestamp(time.mktime(struct), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt
    except Exception:
        return None, None


def _parse_iso(value: str) -> tuple[Optional[str], Optional[datetime]]:
    """ISO8601 문자열(예: 2024-01-15T10:30:00Z)을 (YYYY-MM-DD, datetime)로 파싱한다."""
    if not value:
        return None, None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt
    except (ValueError, AttributeError):
        return None, None


def _collect_newsapi(
    api_key: str,
    max_articles: int,
    category: str = "business",
    language: str = "en",
    query: Optional[str] = None,
) -> list[dict]:
    """NewsAPI.org에서 기사를 수집한다 (보조 소스).

    query가 있으면 /v2/everything(키워드 검색), 없으면 /v2/top-headlines(카테고리).
    키 미설정/네트워크/응답오류는 모두 흡수하고 []를 반환한다.
    """
    if not api_key:
        return []
    try:
        import requests  # 지연 import
    except ImportError:
        return []

    page_size = max(1, min(max_articles, 100))  # NewsAPI 페이지 상한 100
    if query:
        url = "https://newsapi.org/v2/everything"
        params = {"q": query, "language": language, "sortBy": "publishedAt", "pageSize": page_size}
    else:
        url = "https://newsapi.org/v2/top-headlines"
        params = {"category": category, "language": language, "pageSize": page_size}

    try:
        resp = requests.get(url, params=params, headers={"X-Api-Key": api_key}, timeout=15)
        data = resp.json()
    except Exception:
        return []

    if not isinstance(data, dict) or data.get("status") != "ok":
        return []

    articles: list[dict] = []
    for a in data.get("articles", []) or []:
        url_ = a.get("url") or ""
        if not url_:
            continue
        published, dt = _parse_iso(a.get("publishedAt", ""))
        source = ((a.get("source") or {}).get("name")) or ""
        content = a.get("description") or a.get("content") or ""
        articles.append({
            "url": url_,
            "title": a.get("title") or "",
            "source": source,
            "published": published or "",
            "_dt": dt,
            "content": content,
        })
    return articles


def _collect_raw(feeds: list[str]) -> list[dict]:
    """RSS 피드들을 파싱해 원시 기사 리스트를 반환한다.

    각 기사: {url, title, source, published, _dt, content}
    피드 단위 실패는 흡수하고 나머지 피드는 계속 처리한다.
    """
    import feedparser  # 지연 import

    articles: list[dict] = []
    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception:
            continue
        source = getattr(getattr(parsed, "feed", None), "title", "") or ""
        for entry in getattr(parsed, "entries", []) or []:
            url = getattr(entry, "link", "") or ""
            if not url:
                continue
            published, dt = _parse_date(entry)
            content = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
            articles.append({
                "url": url,
                "title": getattr(entry, "title", "") or "",
                "source": source,
                "published": published or "",
                "_dt": dt,
                "content": content,
            })
    return articles


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------

def _dedupe_by_url(articles: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for a in articles:
        if a["url"] in seen:
            continue
        seen.add(a["url"])
        result.append(a)
    return result


def _filter_recent(articles: list[dict], max_age_days: int, now: Optional[datetime] = None) -> list[dict]:
    """max_age_days 이내 기사만 남긴다. 날짜 불명 기사는 유지한다(버리지 않음)."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    result = []
    for a in articles:
        dt = a.get("_dt")
        if dt is None or dt >= cutoff:
            result.append(a)
    return result


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------

def summarize(article: dict) -> str:
    """기사를 Haiku로 3줄 요약한다. 실패 시 원문(content/title)으로 fallback."""
    body = article.get("content") or article.get("title") or ""
    if not body.strip():
        return ""
    prompt = (
        f"다음 기사를 한국어 3줄로 요약하라.\n\n"
        f"제목: {article.get('title', '')}\n"
        f"본문: {body}"
    )
    try:
        return llm.call_claude(prompt, system=_SUMMARY_SYSTEM, model=config.MODEL_FAST)
    except Exception:
        # 요약 실패해도 원문으로 저장은 진행 (기사 단위 회복탄력성)
        return body.strip()


# ---------------------------------------------------------------------------
# 감성 점수
# ---------------------------------------------------------------------------

def _clamp_sentiment(value) -> float:
    """감성 점수를 -1.0~1.0 실수로 정규화한다. 변환 불가 시 0.0(중립)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(-1.0, min(1.0, f))


def _sentiment_prompt(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles):
        title = a.get("title", "")
        summary = a.get("summary") or a.get("content") or ""
        lines.append(f"[{i}] 제목: {title}\n    요약: {summary[:200]}")
    body = "\n".join(lines)
    return (
        "다음 기사들의 시장 감성을 각각 -1.0(매우 부정)~1.0(매우 긍정) 실수로 평가하라.\n\n"
        f"{body}\n\n"
        "결과는 입력 순서와 동일한 길이의 JSON 숫자 배열로만 출력하라. "
        "예: [0.5, -0.3, 0.0]"
    )


def score_sentiment_batch(articles: list[dict]) -> list[float]:
    """기사 청크의 감성 점수를 한 번에 매겨 -1.0~1.0 리스트를 반환한다.

    길이는 입력과 동일하게 보장한다. 실패/불일치 시 전체 0.0(중립)으로 fallback.
    """
    if not articles:
        return []
    try:
        result = llm.call_claude_json(
            _sentiment_prompt(articles),
            system=_SENTIMENT_SYSTEM,
            model=config.MODEL_FAST,
        )
        if not isinstance(result, list) or len(result) != len(articles):
            return [0.0] * len(articles)
        return [_clamp_sentiment(s) for s in result]
    except Exception:
        return [0.0] * len(articles)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------

class DataCollectorAgent(BaseAgent):
    name = "DataCollectorAgent"

    def execute(self, context: dict) -> dict:
        # DataQualityAgent가 헬스체크해 둔 피드가 있으면 우선 사용, 없으면 config fallback
        feeds = context.get("feeds") or config.NEWS_RSS_FEEDS
        raw = _collect_raw(feeds)

        # NewsAPI는 보조 소스 — 키가 있을 때만 추가 수집 (없으면 RSS만)
        api_raw = _collect_newsapi(
            config.NEWSAPI_KEY,
            config.NEWS_MAX_ARTICLES,
            category=config.NEWSAPI_CATEGORY,
            language=config.NEWSAPI_LANGUAGE,
            query=config.NEWSAPI_QUERY,
        )
        if api_raw:
            self.log.info("NewsAPI 보조 수집 %d건", len(api_raw))
            raw = raw + api_raw

        deduped = _dedupe_by_url(raw)
        recent = _filter_recent(deduped, config.NEWS_MAX_AGE_DAYS)
        selected = recent[: config.NEWS_MAX_ARTICLES]

        self.log.info(
            "수집 %d건 -> 중복제거 %d -> 최근 %d -> 처리 %d",
            len(raw), len(deduped), len(recent), len(selected),
        )

        # 요약 (기사 단위)
        for article in selected:
            article["summary"] = summarize(article)

        # 감성 점수 (청크 단위 배치 — 요약 후라 summary를 근거로 평가)
        for chunk in _chunks(selected, _SENTIMENT_BATCH_SIZE):
            scores = score_sentiment_batch(chunk)
            for article, score in zip(chunk, scores):
                article["sentiment"] = score

        for article in selected:
            article.pop("_dt", None)  # 직렬화 불가 객체 제거

        # ChromaDB 저장
        try:
            embed_stats = embedder.embed_news_batch(selected)
        except Exception as exc:  # 임베딩 실패해도 수집 결과는 반환
            self.log.warning("임베딩 실패: %s", exc)
            embed_stats = {"articles": 0, "chunks": 0, "skipped": len(selected)}

        result = {
            "collected": len(selected),
            "embedded": embed_stats.get("articles", 0),
            "chunks": embed_stats.get("chunks", 0),
            "articles": selected,
        }
        context["news"] = selected
        return result
