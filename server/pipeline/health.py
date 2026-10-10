"""AI 단계별 상태 기록. **작게** 유지합니다 — dict 하나가 전부입니다.

왜 필요한가: "정상적으로 결과가 없음(무음 영상)"과 "외부 API 가 실패함"이 둘 다
빈 리스트로 표현되면, 할당량 소진으로 장면 설명을 통째로 잃은 영상이 `ready` 로
저장됩니다(실제로 일어남). 그런 영상이 평가셋에 섞이면
신호별 기여도(멘토 자문 2)를 잴 때 원인을 구분할 수 없습니다.

결과는 요약(summary_json)의 `analysis_health` 에 실립니다. DB 스키마는 그대로입니다.
"""
from __future__ import annotations

from typing import Any

# 1.1: 일부만 실패한 단계(partial_stages)도 complete=False 로 셉니다.
HEALTH_SCHEMA_VERSION = "1.1"

OK = "ok"  # 결과가 있음
OK_EMPTY = "ok_empty"  # 호출은 성공했고, 결과가 정상적으로 비어 있음(무음 영상 등)
SKIPPED = "skipped"  # 백엔드 구조상 이 단계를 따로 돌리지 않음(예: OCR 을 캡션 그리드가 대신함)
ERROR = "error"  # 외부 호출/모델 실행 실패


def stage(
    status: str,
    *,
    backend: str = "",
    model_id: str = "",
    error_code: str = "",
    error_type: str = "",
    message: str = "",
    retry_count: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    """단계 상태 한 칸. 오류일 때만 오류 항목을 채웁니다."""
    out: dict[str, Any] = {
        "status": status,
        "backend": backend,
        "model_id": model_id,
        "retry_count": retry_count,
    }
    if status == ERROR:
        out.update(
            {
                "error_code": error_code,
                "error_type": error_type,
                "message": message[:500],
            }
        )
    out.update(extra)
    return out


def describe_exception(exc: BaseException) -> dict[str, Any]:
    """예외를 error_code / error_type / message / retry_count 로 풉니다."""
    from server.gemini import classify_error

    info = classify_error(exc)
    code = getattr(exc, "error_code", "") or info.kind
    return {
        "error_code": code,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "retry_count": int(getattr(exc, "retry_count", 0) or 0),
    }


def error_stage(exc: BaseException, *, backend: str = "", model_id: str = "", **extra: Any) -> dict:
    """예외 하나를 오류 상태 한 칸으로."""
    return stage(ERROR, backend=backend, model_id=model_id, **describe_exception(exc), **extra)


def is_error(stage_health: dict[str, Any] | None) -> bool:
    return bool(stage_health) and stage_health.get("status") == ERROR


def is_partial(stage_health: dict[str, Any] | None) -> bool:
    """결과는 있지만 일부를 잃은 단계. 예: 그리드 3장 중 2장째에서 할당량이 끊겨 캡션 2/3 이 빔.

    상태는 ok 라도 '분석 완전'이 아닙니다. 이걸 놓치면 캡션 대부분이 빠진 영상을 완전한 분석으로
    셉니다.
    """
    if not stage_health or is_error(stage_health):
        return False
    return bool(stage_health.get("partial_error") or stage_health.get("quota_stopped"))


def _hit_quota(stage_health: dict[str, Any] | None) -> bool:
    if not stage_health:
        return False
    if stage_health.get("quota_stopped"):
        return True
    if is_error(stage_health) and stage_health.get("error_code") == "NON_RECOVERABLE_QUOTA":
        return True
    partial = stage_health.get("partial_error")
    return isinstance(partial, dict) and partial.get("error_code") == "NON_RECOVERABLE_QUOTA"


def summarize_health(health: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """단계별 상태를 한 줄로: 어떤 단계가 실패했고(전부/일부), 할당량 때문인지."""
    failed = sorted(name for name, item in health.items() if is_error(item))
    partial = sorted(name for name, item in health.items() if is_partial(item))
    quota = sorted(name for name, item in health.items() if _hit_quota(item))
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "complete": not failed and not partial,
        "failed_stages": failed,
        "partial_stages": partial,
        "quota_stages": quota,
    }
