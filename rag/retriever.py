"""유사 뉴스 검색.

RAG 코어의 읽기 계층. embedder가 ChromaDB에 저장한 뉴스 청크를
쿼리 텍스트와의 의미 유사도로 검색한다.

RAG 검색 원칙 (CLAUDE.md):
- top_k=5
- 거리(distance) 0.7 이상이면 "관련 뉴스 없음"으로 간주하고 제외
  (embedder가 코사인 거리(cosine)로 컬렉션을 구성하므로 임계값 일관)
"""

from typing import Optional

from rag.embedder import get_collection

DEFAULT_TOP_K = 5
# 이 값 이상의 거리는 무관한 것으로 보고 버린다 (코사인 거리: 0=동일, 클수록 무관).
DEFAULT_MAX_DISTANCE = 0.7


def search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    where: Optional[dict] = None,
) -> list[dict]:
    """쿼리와 유사한 뉴스 청크를 거리 오름차순으로 반환한다.

    각 결과: {document, distance, url, title, source, published, date, chunk_index}
    max_distance 이상으로 먼 결과는 제외한다(관련 뉴스 없음).
    빈 쿼리이거나 컬렉션이 비어 있으면 []를 반환한다.
    """
    query = (query or "").strip()
    if not query:
        return []

    collection = get_collection()

    # 빈 컬렉션에 query하면 예외가 날 수 있으므로 선제 방어.
    if collection.count() == 0:
        return []

    # 임계값 필터로 일부가 잘릴 것에 대비해 약간 여유 있게 가져온다.
    n_results = max(top_k * 2, top_k)

    kwargs = {"query_texts": [query], "n_results": n_results}
    if where:
        kwargs["where"] = where

    raw = collection.query(**kwargs)

    # ChromaDB는 각 키를 [[...]] (쿼리별 리스트)로 반환한다. 단일 쿼리이므로 [0].
    documents = (raw.get("documents") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    results: list[dict] = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        if dist is not None and dist >= max_distance:
            continue
        meta = meta or {}
        results.append({
            "document": doc,
            "distance": dist,
            "url": meta.get("url", ""),
            "title": meta.get("title", ""),
            "source": meta.get("source", ""),
            "published": meta.get("published", ""),
            "date": meta.get("date", ""),
            "chunk_index": meta.get("chunk_index"),
        })

    results.sort(key=lambda r: (r["distance"] is None, r["distance"]))
    return results[:top_k]


def search_for_ticker(
    ticker: str,
    name: Optional[str] = None,
    sector: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> list[dict]:
    """종목 관련 뉴스를 검색한다 (ThesisValidatorAgent용).

    ticker/회사명/섹터를 합쳐 쿼리를 구성해 종목 맥락에 맞는 뉴스를 찾는다.
    """
    query = " ".join(part for part in (ticker, name, sector) if part)
    return search(query, top_k=top_k, max_distance=max_distance)
