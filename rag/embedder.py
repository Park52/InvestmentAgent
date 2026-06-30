"""뉴스 → 벡터 변환 → ChromaDB 저장.

RAG 코어의 쓰기 계층. 수집된 뉴스를 청크로 나눠 ChromaDB에 저장한다.
검색은 rag/retriever.py가 담당한다.

RAG 원칙 (CLAUDE.md):
- 임베딩: ChromaDB 내장 임베딩 사용 (외부 API 비용 없음)
- 청크 크기: 500자 이하
- 중복 방지: URL MD5 해시를 document ID로 사용
"""

import hashlib
import os
import re
from typing import Optional

import chromadb

# config가 아직 비어 있을 수 있으므로 import 실패 시 기본값으로 fallback.
try:
    from config import CHROMA_PATH  # type: ignore
except Exception:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CHROMA_PATH = os.path.join(_BASE_DIR, "data", "chroma_db")

try:
    from config import CHROMA_COLLECTION  # type: ignore
except Exception:
    CHROMA_COLLECTION = "news"

MAX_CHUNK_CHARS = 500

# 모듈 레벨 캐시 (PersistentClient 재생성 비용 회피)
_client: Optional["chromadb.api.client.Client"] = None


# ---------------------------------------------------------------------------
# 클라이언트 / 컬렉션
# ---------------------------------------------------------------------------

def _get_client():
    global _client
    if _client is None:
        os.makedirs(CHROMA_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def get_collection():
    """뉴스 컬렉션 핸들을 반환한다 (없으면 생성).

    내장 임베딩 함수(기본 all-MiniLM-L6-v2)를 사용하므로
    별도 임베딩 함수 지정 없이 get_or_create_collection을 호출한다.
    코사인 거리를 쓰도록 metadata로 명시한다.
    """
    return _get_client().get_or_create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _url_to_id(url: str) -> str:
    """URL을 MD5 해시 문자열로 변환 (document ID 베이스)."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """텍스트를 max_chars 이하 청크로 분할한다.

    문장 경계(. ! ? 줄바꿈)를 우선 기준으로 쪼개되, 한 문장이 max_chars를
    넘으면 강제로 잘라 청크 크기 상한을 보장한다.
    """
    text = (text or "").strip()
    if not text:
        return []

    # 문장 단위 분리 (종결부호/줄바꿈 뒤에서 분할, 구분자는 유지)
    sentences = re.split(r"(?<=[.!?。!?])\s+|\n+", text)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # 단일 문장이 상한을 초과하면 글자 단위로 강제 분할
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(sentence), max_chars):
                chunks.append(sentence[i:i + max_chars])
            continue

        # 현재 청크에 붙였을 때 상한 초과 → 청크 확정 후 새로 시작
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence

    if current:
        chunks.append(current)
    return chunks


def _build_metadata(article: dict, chunk_index: int) -> dict:
    """청크 메타데이터 구성. None 값은 ChromaDB가 허용하지 않으므로 제외."""
    raw = {
        "url": article.get("url", ""),
        "title": article.get("title", ""),
        "source": article.get("source", ""),
        "published": article.get("published", ""),
        "date": article.get("date", ""),
        "chunk_index": chunk_index,
    }
    return {k: v for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------------------
# 저장 (Create / Upsert)
# ---------------------------------------------------------------------------

def embed_news(article: dict, collection=None) -> int:
    """기사 1건을 청크로 나눠 ChromaDB에 upsert하고 저장된 청크 수를 반환한다.

    article 예상 키:
      - url       (필수, 중복 방지 ID 베이스)
      - content   (본문; 없으면 summary, 둘 다 없으면 title 사용)
      - title, source, published, date (선택, 메타데이터)

    동일 URL을 재수집하면 같은 ID로 upsert되어 중복이 생기지 않는다(멱등).
    """
    url = (article.get("url") or "").strip()
    if not url:
        return 0

    body = article.get("content") or article.get("summary") or article.get("title") or ""
    chunks = _chunk_text(body)
    if not chunks:
        return 0

    if collection is None:
        collection = get_collection()

    base_id = _url_to_id(url)
    ids = [f"{base_id}_{i}" for i in range(len(chunks))]
    metadatas = [_build_metadata(article, i) for i in range(len(chunks))]

    collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
    return len(chunks)


def embed_news_batch(articles: list[dict]) -> dict:
    """여러 기사를 일괄 저장하고 통계를 반환한다.

    반환: {"articles": 처리된 기사 수, "chunks": 저장된 총 청크 수, "skipped": 건너뛴 기사 수}
    """
    collection = get_collection()
    total_chunks = 0
    processed = 0
    skipped = 0
    for article in articles:
        n = embed_news(article, collection=collection)
        if n == 0:
            skipped += 1
        else:
            processed += 1
            total_chunks += n
    return {"articles": processed, "chunks": total_chunks, "skipped": skipped}
