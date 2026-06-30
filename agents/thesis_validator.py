"""Agent 7: ThesisValidatorAgent — Bull/Bear case 생성 + 종합 판정.

RAG로 종목별 관련 뉴스를 검색하고:
  1차 호출: Bull case (강세론자, 차트+뉴스 컨텍스트 포함)
  2차 호출: Bear case (비관론자, Bull 컨텍스트를 절대 넣지 않음 — 진짜 반론 유도)
  3차 호출: 종합 verdict + confidence

CLAUDE.md의 Bear case 격리 규칙을 엄격히 준수한다.
"""

import json

import config
from agents.base import BaseAgent
from rag import retriever
from utils import llm

_BULL_SYSTEM = "당신은 강세론자 애널리스트다."
_BEAR_SYSTEM = (
    "당신은 비관론자 애널리스트다. 어떤 종목이든 반드시 위험 요인을 찾아낸다."
)
_SYNTH_SYSTEM = "당신은 중립적 투자 심사역이다. 강세/약세 논거를 균형 있게 종합해 판정한다."

_VALID_VERDICTS = ("bullish", "neutral", "bearish")


def _docs_text(docs: list[dict]) -> str:
    if not docs:
        return "(관련 뉴스 없음)"
    return "\n".join(
        f"- {d.get('title', '')}: {(d.get('document') or '')[:200]}" for d in docs
    )


def _as_str_list(value, limit: int = 3) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value][:limit]
    return []


def generate_bull(ticker: str, sector: str, chart_data: dict, rag_docs: list[dict]) -> list[str]:
    """1차 호출: 매수 근거 3가지 (차트+뉴스 컨텍스트 포함)."""
    prompt = (
        f"차트 지표: {json.dumps(chart_data, ensure_ascii=False)}\n"
        f"관련 뉴스:\n{_docs_text(rag_docs)}\n\n"
        f"종목 {ticker}({sector})의 매수 근거 3가지를 JSON 배열로만 작성하라. "
        '예: ["근거1", "근거2", "근거3"]'
    )
    try:
        return _as_str_list(
            llm.call_claude_json(prompt, system=_BULL_SYSTEM, model=config.MODEL_SMART)
        )
    except Exception:
        return []


def generate_bear(ticker: str, sector: str) -> list[str]:
    """2차 호출: 하락 근거 3가지. Bull 컨텍스트를 절대 포함하지 않는다."""
    prompt = (
        f"종목: {ticker}\n섹터: {sector}\n"
        f"이 종목의 위험 요인과 하락 근거 3가지를 JSON 배열로만 작성하라. "
        '예: ["위험1", "위험2", "위험3"]'
    )
    try:
        return _as_str_list(
            llm.call_claude_json(prompt, system=_BEAR_SYSTEM, model=config.MODEL_SMART)
        )
    except Exception:
        return []


def synthesize_verdict(ticker: str, bull: list[str], bear: list[str], chart_data: dict) -> dict:
    """3차 호출: Bull/Bear를 종합해 verdict + confidence(0~100) 산출."""
    prompt = (
        f"종목: {ticker}\n"
        f"매수 논거: {json.dumps(bull, ensure_ascii=False)}\n"
        f"매도 논거: {json.dumps(bear, ensure_ascii=False)}\n"
        f"차트 지표: {json.dumps(chart_data, ensure_ascii=False)}\n\n"
        '강세/약세를 균형 있게 종합해 JSON으로만 출력하라. '
        '{"verdict": "bullish|neutral|bearish", "confidence": 0-100}'
    )
    try:
        r = llm.call_claude_json(prompt, system=_SYNTH_SYSTEM, model=config.MODEL_SMART)
    except Exception:
        return {"verdict": "neutral", "confidence": 50}

    verdict = r.get("verdict") if isinstance(r, dict) else None
    if verdict not in _VALID_VERDICTS:
        verdict = "neutral"
    try:
        confidence = int(r.get("confidence")) if isinstance(r, dict) else 50
    except (TypeError, ValueError):
        confidence = 50
    confidence = max(0, min(100, confidence))
    return {"verdict": verdict, "confidence": confidence}


class ThesisValidatorAgent(BaseAgent):
    name = "ThesisValidatorAgent"

    def execute(self, context: dict) -> dict:
        analyzed = context.get("analyzed", []) or []
        theses: list[dict] = []

        for c in analyzed:
            ticker = c["ticker"]
            sector = c.get("sector", "")
            chart = c.get("chart", {}) or {}

            try:
                docs = retriever.search_for_ticker(
                    ticker, name=c.get("name"), sector=sector
                )
            except Exception:
                docs = []

            bull = generate_bull(ticker, sector, chart, docs)   # 1차
            bear = generate_bear(ticker, sector)                # 2차 (Bull 격리)
            verdict = synthesize_verdict(ticker, bull, bear, chart)  # 3차

            entry = dict(c)
            entry["bull_case"] = bull
            entry["bear_case"] = bear
            entry["verdict"] = verdict["verdict"]
            entry["confidence"] = verdict["confidence"]
            entry["related_news"] = docs
            theses.append(entry)

            self.log.info(
                "%s -> %s (conf=%d, bull=%d bear=%d)",
                ticker, verdict["verdict"], verdict["confidence"], len(bull), len(bear),
            )

        context["theses"] = theses
        return {"theses": theses, "count": len(theses)}
