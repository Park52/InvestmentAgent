"""BaseAgent, AgentResult — 모든 에이전트의 공통 기반.

파이프라인은 각 에이전트를 BaseAgent.run(context)로 일관되게 실행한다.
run()은 시작/종료 로깅, 실행시간 측정, 예외 캡처를 담당하는 템플릿 메서드이며
실제 로직은 서브클래스의 execute(context)에 구현한다.

설계 원칙:
- 한 에이전트의 예외가 전체 파이프라인을 멈추지 않는다. run()이 예외를 잡아
  AgentResult.fail로 변환하고, 상위(scheduler)가 success 여부를 보고 흐름을 결정한다.
- context는 에이전트 간 공유 상태 dict. 각 에이전트는 필요한 입력을 읽고
  결과를 context에 누적할 수 있다.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from config import LOG_PATH
except Exception:  # config 미구성 시에도 동작
    LOG_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "agent.log"
    )


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """파일(LOG_PATH) + 콘솔에 동시 출력하는 로거를 반환한다.

    중복 핸들러가 붙지 않도록 이미 구성된 로거는 그대로 재사용한다.
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # 이미 구성됨
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(_LOG_FORMAT)

    # 콘솔 핸들러
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    # 파일 핸들러 (디렉토리 없으면 생성, 실패해도 콘솔 로깅은 유지)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(LOG_PATH)), exist_ok=True)
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass

    return logger


# ---------------------------------------------------------------------------
# 결과 객체
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    """에이전트 실행 결과.

    success: 성공 여부
    agent:   에이전트 이름
    data:    결과 페이로드 (성공 시 execute 반환 dict)
    error:   실패 시 오류 메시지
    duration_ms: 실행 소요 시간(ms)
    """
    success: bool
    agent: str
    data: dict = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: Optional[float] = None

    def __bool__(self) -> bool:
        return self.success

    @classmethod
    def ok(cls, agent: str, data: Optional[dict] = None, duration_ms: Optional[float] = None) -> "AgentResult":
        return cls(success=True, agent=agent, data=data or {}, duration_ms=duration_ms)

    @classmethod
    def fail(cls, agent: str, error: str, duration_ms: Optional[float] = None) -> "AgentResult":
        return cls(success=False, agent=agent, error=error, duration_ms=duration_ms)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "agent": self.agent,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# 베이스 에이전트
# ---------------------------------------------------------------------------

class BaseAgent(ABC):
    """모든 에이전트의 추상 기반 클래스.

    서브클래스는 클래스 속성 `name`을 지정하고 `execute(context)`를 구현한다.
    파이프라인은 `run(context)`만 호출한다.
    """

    name: str = "BaseAgent"

    def __init__(self) -> None:
        self.log = get_logger(self.name)

    @abstractmethod
    def execute(self, context: dict) -> dict:
        """실제 에이전트 로직. 결과 dict를 반환한다 (AgentResult.data가 됨).

        실패는 예외를 던지면 된다. run()이 잡아서 AgentResult.fail로 변환한다.
        """
        raise NotImplementedError

    def run(self, context: Optional[dict] = None) -> AgentResult:
        """템플릿 메서드: 로깅 + 시간측정 + 예외처리로 execute를 감싼다."""
        if context is None:
            context = {}

        self.log.info("시작")
        start = time.perf_counter()
        try:
            data = self.execute(context)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise TypeError(
                    f"{self.name}.execute는 dict를 반환해야 한다 (받은 타입: {type(data).__name__})"
                )
            duration_ms = (time.perf_counter() - start) * 1000
            self.log.info("완료 (%.0fms)", duration_ms)
            return AgentResult.ok(self.name, data=data, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001 - 파이프라인 보호를 위해 광범위 캡처
            duration_ms = (time.perf_counter() - start) * 1000
            self.log.exception("실패: %s", exc)
            return AgentResult.fail(self.name, error=str(exc), duration_ms=duration_ms)
