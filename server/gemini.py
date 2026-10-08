"""Gemini 클라이언트 하나만 만들어서 재사용합니다.

여기서 오류를 **등급별로 나눕니다.** 모든 429 를 똑같이 재시도하면 일일 할당량이
소진된 상황에서 남은 할당량을 4배로 더 태우기만 합니다(실제로 관측: 실패한 그리드마다
4회씩 재호출).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, TypeVar

from server.config import GEMINI_API_KEY

T = TypeVar("T")

# 오류 등급
NON_RECOVERABLE_QUOTA = "NON_RECOVERABLE_QUOTA"  # 일일 할당량 소진 — 몇 초 뒤 다시 해도 안 됨
TRANSIENT_RATE_LIMIT = "TRANSIENT_RATE_LIMIT"  # 분당 속도 제한 등 — 잠깐 쉬면 풀림
TRANSIENT_SERVER = "TRANSIENT_SERVER"  # 503/500 — 서버 쪽 일시 장애
FATAL = "FATAL"  # 인증 오류, 잘못된 요청 등 — 재시도 의미 없음

# "일일/비회복" 이라고 **판단할 근거가 있을 때만** 그렇게 부릅니다.
# 무료 등급의 분당 한도와 일일 한도는 quotaMetric(..._free_tier_requests)이 **같고**, 둘을 가르는 것은
# quotaId 뿐입니다. 그래서 free_tier 표식보다 기간 표식을 먼저 봅니다(둘 다 적혀 있으면 일일이 이깁니다).
#   GenerateRequestsPerDayPerProjectPerModel-FreeTier     → 오늘은 끝(비회복)
#   GenerateRequestsPerMinutePerProjectPerModel-FreeTier  → retryDelay 뒤 풀림(일시적)
_PER_DAY_MARKERS = ("perday", "per day", "per_day", "daily")
_PER_MINUTE_MARKERS = ("perminute", "per minute", "per_minute")
# 기간 표식이 없는 짧은 메시지(quotaId 없음)의 free_tier 는 예전에 관측한 대로(하루 20회 소진) 비회복으로 봅니다.
# 관측된 실제 응답: generativelanguage.googleapis.com/generate_content_free_tier_requests
_FREE_TIER_MARKERS = ("free_tier", "freetier")
_RATE_LIMIT_CODES = ("429", "RESOURCE_EXHAUSTED")
_SERVER_CODES = ("503", "UNAVAILABLE", "500", "INTERNAL")

# 예전 이름 유지(다른 곳에서 import 할 수 있음)
RETRYABLE = ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "500")

_RETRY_DELAY_RE = re.compile(r"retry[_-]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s?", re.I)
_RETRY_AFTER_RE = re.compile(r"retry[_-]?after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", re.I)
# Google 의 429 는 기다릴 시간을 두 군데에 적습니다. 본문 메시지에는 정확한 값("Please retry in 12.5s.",
# 1초 미만이면 "750ms" 처럼 Go 의 시간 표기)을, RetryInfo.retryDelay 에는 초 단위로 **잘라서**("12s",
# 1초 미만이면 "0s") 적습니다.
_RETRY_IN_RE = re.compile(r"retry in\s+((?:\d+(?:\.\d+)?(?:ns|us|µs|μs|ms|s|m|h))+)", re.IGNORECASE)
_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|s|m|h)", re.IGNORECASE)
_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ns": 1e-9}


class GeminiNotConfigured(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    """일일 할당량 소진. 오늘은 더 불러봐야 소용이 없습니다."""

    error_code = NON_RECOVERABLE_QUOTA


class EmptyResponse(RuntimeError):
    """호출은 성공(200)했는데 쓸 내용이 없음 — 차단(SAFETY · RECITATION), 빈 후보, JSON 깨짐.

    "정상적으로 비어 있음"(무음 영상 → utterances=[])과 구별하려고 따로 둡니다.
    """

    error_code = "EMPTY_RESPONSE"


def empty_reason(response) -> str:
    """응답이 비었을 때 SDK 가 알려 준 이유(block_reason · finish_reason)를 짧게."""
    parts = []
    feedback = getattr(response, "prompt_feedback", None)
    block = getattr(feedback, "block_reason", None)
    if block:
        parts.append(f"block_reason={getattr(block, 'name', block)}")
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    if finish:
        parts.append(f"finish_reason={getattr(finish, 'name', finish)}")
    return ", ".join(parts) or "내용 없음 또는 JSON 깨짐"


@dataclass(frozen=True)
class ErrorClass:
    kind: str
    http_code: str
    retry_after_sec: float | None  # retryDelay · retry-after 값 그대로(Google 은 초 단위로 자른 값)
    message: str
    retry_in_sec: float | None = None  # 본문의 정확한 대기 시간("Please retry in 12.5s.")

    @property
    def retryable(self) -> bool:
        return self.kind in (TRANSIENT_RATE_LIMIT, TRANSIENT_SERVER)


def _retry_after(message: str) -> float | None:
    for pattern in (_RETRY_DELAY_RE, _RETRY_AFTER_RE):
        found = pattern.search(message)
        if found:
            try:
                return float(found.group(1))
            except ValueError:
                pass
    return None


def _retry_in(message: str) -> float | None:
    """본문의 정확한 대기 시간("retry in 12.5s" · "750ms" · "1m2s")을 초로. 없으면 None."""
    found = _RETRY_IN_RE.search(message)
    if not found:
        return None
    return sum(
        float(number) * _DURATION_UNITS[unit.lower()]
        for number, unit in _DURATION_PART_RE.findall(found.group(1))
    )


def _rate_limit_kind(low: str) -> str:
    """429 하나를 일일(비회복)/일시적으로 나눕니다. 순서가 곧 규칙입니다(위 표식 설명)."""
    if any(marker in low for marker in _PER_DAY_MARKERS):
        return NON_RECOVERABLE_QUOTA
    if any(marker in low for marker in _PER_MINUTE_MARKERS):
        return TRANSIENT_RATE_LIMIT
    if any(marker in low for marker in _FREE_TIER_MARKERS):
        return NON_RECOVERABLE_QUOTA
    return TRANSIENT_RATE_LIMIT


def classify_error(exc: BaseException) -> ErrorClass:
    """예외 하나를 등급으로 나눕니다.

    근거가 없으면 비회복이라고 **추측하지 않습니다.** 그냥 429 면 일시적 속도 제한으로
    보고 정해진 횟수만 재시도합니다. 무료 등급이라도 분당 한도(quotaId ...PerMinute...)면
    일시적입니다 — retryDelay 만큼 쉬면 풀립니다.
    """
    if isinstance(exc, QuotaExhausted):
        return ErrorClass(NON_RECOVERABLE_QUOTA, "429", None, str(exc))
    message = str(exc)
    low = message.lower()
    delay = _retry_after(message)
    exact = _retry_in(message)

    if any(code.lower() in low for code in _RATE_LIMIT_CODES):
        return ErrorClass(_rate_limit_kind(low), "429", delay, message, exact)
    if any(code.lower() in low for code in _SERVER_CODES):
        code = "503" if ("503" in low or "unavailable" in low) else "500"
        return ErrorClass(TRANSIENT_SERVER, code, delay, message, exact)
    return ErrorClass(FATAL, "", delay, message, exact)


# 사람이 화면 앞에서 기다리는 호출(검색창 질문의 글 좌표)용 재시도. 저장 작업(백그라운드)의 기본값
# (4번 · 한 번에 최대 30초)을 그대로 쓰면 retryDelay 41초짜리 429 에 30+30+30 = 90초 동안 검색창이
# 멈춥니다. 기준은 팀원 원본 재시도(4번 · 2·4·8초 쉼)입니다. 원본의 마지막(4번째) 호출은 쉬는 시간 14초
# (max_total_wait)와 앞 호출 3번이 걸린 시간 뒤에 나갑니다. 호출 시간은 실측 평균으로 셉니다.
#   503 · 기다릴 시간이 없는 429 : 예전처럼 2·4·8초
#   본문의 정확한 값(retry in 12.5s) : 그만큼 쉬고 다시
#   retryDelay 만("12s")           : 초 단위 값이라(Google 은 12.5초 → "12s" 로 자르고, HTTP Retry-After 는 보통
#                                    올림) 1초 더 쉽니다. "0s" 는 "없음"이 아니라 "1초 안에 풀림"입니다.
#   어느 경우든 원본의 마지막 호출을 보낼 때를 넘겨 쉬지 않고, **마지막 시도는 그 때에** 부릅니다(남은 시간을 다 씀).
#   앞에서 짧게 쉬었거나(429 "retry in 0.5s" 다음 503) 서버가 남은 시간을 짧게 알려도 원본처럼 t≈14초에 한 번 더
#   부릅니다. 그 때가 응답을 기다리는 사이에 지났는데 방금 호출이 원본의 마지막 호출보다 앞섰으면 쉬지 않고 한 번 더.
#   알린 시간이 원본의 마지막 호출이 서버에 닿는 때보다 늦게 풀린다는 뜻이면 **바로 실패를 알리고**, 방금 호출이 이미
#   그 때만큼 늦었는데 실패했으면 그만둡니다 — 원본도 실패했고, 다시 불러봐야 요청 수만 늡니다.
# tests/test_gemini_errors.py 가 가짜 시계(호출마다 0~0.5초)로 원본과 비교합니다: 호출 시간이 고르고 서버가 남은
# 시간을 실제보다 길게 알리지 않으면 원본이 성공하던 경우에 실패하지 않습니다(429 · 503 이 섞여도). 호출은 4번
# 이하이고, 원본의 가장 긴 경우(14초 + 호출 4번)보다 호출 한 번 넘게 늦지 않습니다. 한도 하나를 Google 모양(본문의
# 정확한 값)으로 알리면 원본보다 왕복 한 번 넘게 늦지 않고, retryDelay 만 알리면 1초(올린 값이면 2초)와 왕복 한 번까지
# 늦을 수 있습니다. 오류가 섞이거나 짧게 알리는 서버에서는 마지막 시도가 원본의 마지막 호출 때까지 늦어질 수 있습니다.
INTERACTIVE_RETRY: dict[str, Any] = {"attempts": 4, "max_total_wait": 14.0}
# 이만큼 안쪽의 차이는 같은 때로 봅니다(부동소수점 잡음으로 "딱 원본의 마지막 호출 때"를 넘었다고 보지 않게).
_SAME_TIME = 1e-6


def _interactive_wait(info: ErrorClass, attempt: int, attempts: int, base_delay: float, left: float,
                      per_call: float) -> float | None:
    """사람이 기다리는 호출의 다음 대기(초). None 이면 더 부르지 않고 실패를 알립니다.

    left 는 남은 쉬는 시간(max_total_wait − 지금까지 쉰 시간, 호출 시간 몫을 당겨 쓰면 음수), per_call 은 호출 한 번이
    걸린 실측 평균입니다. 원본은 이 시도 뒤에 later 번 더 부르므로 그 마지막 호출은 지금부터
    left + (later − 1) × per_call 뒤에 나가고, 방금 실패한 호출보다 left + later × per_call 늦게 서버에 닿습니다
    (서버에는 보낸 뒤 반 호출 만에 닿고, 알린 시간은 응답을 받기 반 호출 전부터 셉니다).
    """
    later = attempts - 1 - attempt  # 원본이 이 시도 뒤에 더 부르는 횟수
    reach = left + later * per_call  # 원본의 마지막 호출이 방금 호출보다 늦게 서버에 닿는 시간
    if reach <= _SAME_TIME:  # 방금 호출이 이미 원본의 마지막 호출만큼 늦었습니다 — 원본도 실패
        return None
    until_last = max(left + (later - 1) * per_call, 0.0)  # 지금부터 원본의 마지막 호출을 보낼 때까지
    if info.retry_in_sec:  # 본문의 정확한 값 — 그만큼만
        at_least = wait = info.retry_in_sec
    else:
        # retryDelay · retry-after 는 초 단위 값입니다. 본문이 0초라고만 적었어도 같은 뜻("1초 안에 풀림").
        delay = info.retry_after_sec if info.retry_after_sec is not None else info.retry_in_sec
        if delay is not None:
            # 잘린 값이면(Google) 1초 늦게, 올린 값이면 1초 일찍 풀릴 수 있습니다. 쉴 때는 1초 더 쉬고, 실패를 알리는
            # 기준은 1초 일찍 풀린다고 봅니다(올린 값을 주는 서버에서 원본이 성공하던 경계를 놓치지 않게).
            at_least, wait = (delay - 1.0 if delay == int(delay) else delay), delay + 1.0
        else:  # 503 · 기다릴 시간이 없는 429 — 예전 그대로 2·4·8초
            at_least, wait = 0.0, base_delay * (2**attempt)
    if at_least > reach + _SAME_TIME:  # 원본의 마지막 호출보다 늦게 풀립니다 — 원본도 실패
        return None
    if later == 1:  # 다음이 마지막 시도: 알린 시간이 더 짧아도 원본의 마지막 호출과 같은 때에
        return until_last
    return min(wait, until_last)


def call_with_retry(
    fn: Callable[[], T],
    attempts: int = 4,
    base_delay: float = 2.0,
    max_sleep: float = 30.0,
    *,
    max_total_wait: float | None = None,
) -> T:
    """일시적 오류면 잠깐 쉬고 다시 시도합니다.

    비회복 할당량 소진이면 **즉시 포기합니다.** 재시도해도 복구되지 않고 남은 할당량만
    더 씁니다. 몇 번째 시도에서 실패했는지는 예외의 `retry_count` 에 실어 보냅니다.

    쉬는 시간은 서버가 준 retryDelay(없으면 2·4·8초…)이고 한 번에 max_sleep 을 넘지 않습니다.
    max_total_wait 를 주면(사람이 기다리는 호출) 그만큼 쉬는 원본 재시도의 마지막 호출 시각을 넘기지 않고(호출에
    걸린 시간도 실측으로 셈), 본문의 정확한 대기 시간을 먼저 봅니다(_interactive_wait). 서버가 요구한 시간이 그
    시각을 넘으면 더 쉬지 않고 바로 마지막 오류를 올립니다.
    """
    last: Exception | None = None
    waited = 0.0
    spent = 0.0  # 실패한 호출들이 걸린 시간의 합(대화형 재시도가 원본의 호출 시간을 셈에 넣을 때 씀)
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            spent += time.perf_counter() - started
            last = exc
            info = classify_error(exc)
            setattr(exc, "error_class", info)
            setattr(exc, "retry_count", attempt)
            if info.kind == NON_RECOVERABLE_QUOTA:
                quota = QuotaExhausted(info.message)
                quota.retry_count = attempt  # type: ignore[attr-defined]
                raise quota from exc
            if not info.retryable:
                raise
            if attempt == attempts - 1:
                break
            if max_total_wait is None:
                # 저장 작업(백그라운드): 예전 그대로 retryDelay(없거나 0이면 2·4·8초…)를 한 번에 max_sleep 까지.
                # 더 오래 쉬어도 실패가 늘지 않고(횟수는 같음) 기다리는 사람이 없습니다.
                wait = min(info.retry_after_sec or base_delay * (2**attempt), max_sleep)
            else:
                planned = _interactive_wait(info, attempt, attempts, base_delay, max_total_wait - waited,
                                            spent / (attempt + 1))
                if planned is None:
                    break
                wait = min(planned, max_sleep)
            time.sleep(wait)
            waited += wait
    # retry_count 는 마지막으로 실패한 시도의 번호(위에서 이미 실었습니다).
    raise last  # type: ignore[misc]


@lru_cache(maxsize=1)
def get_client():
    from google import genai

    if not GEMINI_API_KEY:
        raise GeminiNotConfigured(
            "GEMINI_API_KEY 가 없습니다. .env 파일에 GEMINI_API_KEY=... 를 넣어주세요."
        )
    return genai.Client(api_key=GEMINI_API_KEY)
