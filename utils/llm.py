"""Claude API 래퍼 + 토큰 추적.

모든 에이전트는 이 모듈의 call_claude / call_claude_json를 통해서만 LLM을 호출한다.
이렇게 단일 통로로 모으면 토큰 사용량/비용을 한 곳에서 집계할 수 있다.

모델 사용 기준 (CLAUDE.md):
  - 단순 분류/요약/추출 → config.MODEL_FAST (claude-haiku-4-5)
  - 차트 해석/Bull·Bear/리포트 → config.MODEL_SMART (claude-sonnet-4-6)

주의: Haiku 4.5 / Sonnet 4.6는 temperature와 구조화 출력을 지원하지만,
effort/adaptive thinking은 기본 호출에 사용하지 않는다(Haiku 미지원).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import config
from agents.base import get_logger

_log = get_logger("llm")

# 모듈 레벨 클라이언트 캐시 (지연 생성)
_client = None

# 모델별 단가 (USD per 1M tokens) — 비용 추정용. 모델명 부분일치로 매칭.
_PRICING = {
    "haiku": {"input": 1.0, "output": 5.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "opus": {"input": 5.0, "output": 25.0},
}


# ---------------------------------------------------------------------------
# 클라이언트
# ---------------------------------------------------------------------------

def get_client():
    """anthropic 클라이언트를 반환한다 (지연 생성).

    anthropic 미설치 또는 API 키 미설정 시 명확한 에러를 던진다.
    """
    global _client
    if _client is not None:
        return _client

    try:
        import anthropic  # 지연 import: 미설치 환경에서도 모듈 import는 가능
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "anthropic 패키지가 설치돼 있지 않다. `pip install anthropic`로 설치하라."
        ) from exc

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 설정되지 않았다. .env에 키를 추가하라."
        )

    _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# 토큰/비용 추적
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    """모델별 누적 토큰/비용."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        """단가표 기준 추정 비용(USD). 캐시 읽기는 0.1x, 쓰기는 1.25x로 근사."""
        price = _pricing_for(model)
        if price is None:
            return 0.0
        in_rate = price["input"] / 1_000_000
        out_rate = price["output"] / 1_000_000
        return (
            self.input_tokens * in_rate
            + self.output_tokens * out_rate
            + self.cache_read_tokens * in_rate * 0.1
            + self.cache_write_tokens * in_rate * 1.25
        )


@dataclass
class _Tracker:
    by_model: dict = field(default_factory=dict)

    def record(self, model: str, usage) -> None:
        u = self.by_model.setdefault(model, TokenUsage())
        u.calls += 1
        u.input_tokens += getattr(usage, "input_tokens", 0) or 0
        u.output_tokens += getattr(usage, "output_tokens", 0) or 0
        u.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        u.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


_TRACKER = _Tracker()


def _pricing_for(model: str) -> Optional[dict]:
    for key, price in _PRICING.items():
        if key in model:
            return price
    return None


def get_token_usage() -> dict:
    """모델별 누적 사용량과 총 비용을 dict로 반환한다."""
    summary = {"models": {}, "total_cost_usd": 0.0, "total_calls": 0}
    for model, usage in _TRACKER.by_model.items():
        cost = usage.cost_usd(model)
        summary["models"][model] = {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cost_usd": round(cost, 4),
        }
        summary["total_cost_usd"] += cost
        summary["total_calls"] += usage.calls
    summary["total_cost_usd"] = round(summary["total_cost_usd"], 4)
    return summary


def reset_token_usage() -> None:
    _TRACKER.by_model.clear()


def log_token_usage() -> None:
    """누적 토큰 사용량을 로그로 출력한다 (파이프라인 종료 시 호출)."""
    summary = get_token_usage()
    _log.info(
        "토큰 사용: %d회 호출, 추정 비용 $%.4f",
        summary["total_calls"], summary["total_cost_usd"],
    )
    for model, stats in summary["models"].items():
        _log.info(
            "  %s: in=%d out=%d cost=$%.4f",
            model, stats["input_tokens"], stats["output_tokens"], stats["cost_usd"],
        )


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------

def _default_max_tokens(model: str) -> int:
    return config.MAX_TOKENS_FAST if "haiku" in model else config.MAX_TOKENS_SMART


def _extract_text(message) -> str:
    """응답 message에서 text 블록만 합쳐 반환한다. refusal은 예외 처리."""
    if getattr(message, "stop_reason", None) == "refusal":
        raise RuntimeError("모델이 안전상의 이유로 응답을 거부했다 (stop_reason=refusal).")
    parts = [
        block.text for block in (message.content or [])
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def call_claude(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """Claude를 호출하고 텍스트 응답을 반환한다.

    토큰 사용량은 자동으로 누적된다.
    """
    model = model or config.MODEL_SMART
    max_tokens = max_tokens or _default_max_tokens(model)
    if temperature is None:
        temperature = config.DEFAULT_TEMPERATURE

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    client = get_client()
    message = client.messages.create(**kwargs)
    _TRACKER.record(model, getattr(message, "usage", None) or _EMPTY_USAGE)
    return _extract_text(message)


def call_claude_json(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    schema: Optional[dict] = None,
) -> Any:
    """Claude를 호출해 JSON(dict/list)을 반환한다.

    schema가 주어지면 구조화 출력(output_config.format)을 사용하고,
    없으면 'JSON만 출력하라'고 지시한 뒤 응답에서 JSON을 파싱한다.
    """
    model = model or config.MODEL_SMART
    max_tokens = max_tokens or _default_max_tokens(model)
    if temperature is None:
        temperature = config.DEFAULT_TEMPERATURE

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    client = get_client()
    message = client.messages.create(**kwargs)
    _TRACKER.record(model, getattr(message, "usage", None) or _EMPTY_USAGE)
    text = _extract_text(message)
    return _parse_json(text)


def _parse_json(text: str) -> Any:
    """텍스트에서 JSON을 파싱한다. ```json ...``` 펜스나 앞뒤 잡음을 견딘다."""
    text = text.strip()
    # 코드펜스 제거
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 첫 { 또는 [ 부터 마지막 } 또는 ] 까지 추출 재시도
        match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


class _EmptyUsage:
    """usage가 없는 응답(테스트/모의)에 대한 안전 기본값."""
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


_EMPTY_USAGE = _EmptyUsage()
