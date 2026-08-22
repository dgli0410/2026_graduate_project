"""Gemini 클라이언트 하나만 만들어서 재사용합니다."""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Callable, TypeVar

from server.config import GEMINI_API_KEY

T = TypeVar("T")

# 마스터 문서가 경고한 상황: 429 RESOURCE_EXHAUSTED / 503 UNAVAILABLE 이 자주 납니다.
RETRYABLE = ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "500")


class GeminiNotConfigured(RuntimeError):
    pass


def call_with_retry(fn: Callable[[], T], attempts: int = 4, base_delay: float = 2.0) -> T:
    """일시적 오류(429/503)면 잠깐 쉬고 다시 시도합니다."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            message = str(exc)
            if not any(code in message for code in RETRYABLE):
                raise
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    raise last  # type: ignore[misc]


@lru_cache(maxsize=1)
def get_client():
    from google import genai

    if not GEMINI_API_KEY:
        raise GeminiNotConfigured(
            "GEMINI_API_KEY 가 없습니다. .env 파일에 GEMINI_API_KEY=... 를 넣어주세요."
        )
    return genai.Client(api_key=GEMINI_API_KEY)
