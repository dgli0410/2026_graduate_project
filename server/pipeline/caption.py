"""단계 6 · 장면 설명 만들기 (+ 그리드 방식일 때는 단계 5 화면 글자도 함께). 담당 D.

프레임 6장을 한 장으로 붙여 Gemini 에 한 번만 물어봅니다.
호출 수는 MAX_GEMINI_CALLS_PER_VIDEO 로 코드에서 막습니다.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from server.config import CAPTION_MODEL, MAX_GEMINI_CALLS_PER_VIDEO
from server.gemini import NON_RECOVERABLE_QUOTA, get_client
from server.pipeline.frames import chunk, make_grid, select_key_frames
from server.pipeline.health import ERROR, OK, OK_EMPTY, describe_exception, stage


class TileReading(BaseModel):
    index: int = Field(description="격자 이미지에 노란 숫자로 표시된 번호")
    on_screen_text: str = Field(description="그 칸 화면에 보이는 글자. 없으면 빈 문자열")
    description: str = Field(description="그 칸에서 무슨 일이 벌어지는지 한 문장")


class GridReading(BaseModel):
    tiles: list[TileReading]


PROMPT = """
이 이미지는 짧은 세로 영상에서 뽑은 장면 {count}칸을 격자로 붙인 것이야.
각 칸 왼쪽 위에 노란 숫자로 번호가 적혀 있어.

칸마다 두 가지를 적어줘.
- on_screen_text: 그 칸 화면에 실제로 보이는 글자(자막, 텍스트 오버레이)를 그대로. 없으면 빈 문자열.
- description: 그 칸에서 무슨 일이 벌어지는지 한 문장으로.

규칙:
- 번호를 빠뜨리지 말고 0번부터 {last}번까지 전부 답해줘.
- 실제로 보이는 것만 적어. 추측해서 채우지 마.
- 한국어로 적어줘.
""".strip()


def describe(frames: list[tuple[float, Path]]) -> list[dict]:
    """대표 프레임들 -> [{time, frame_path, caption, ocr_text}, ...]

    frames 는 (시각, 파일경로) 목록입니다. 호출 상태까지 필요하면 describe_with_health() 를 쓰세요.
    """
    items, _health = describe_with_health(frames)
    return items


def describe_with_health(frames: list[tuple[float, Path]]) -> tuple[list[dict], dict]:
    """캡션 결과와 **호출 상태**를 함께 돌려줍니다.

    그리드 호출이 실패했는데 빈 캡션으로 조용히 넘어가면, 그 영상은 장면 설명 없이 `ready` 로
    저장됩니다. 실패를 세어서 호출한 쪽(analyze)이 기록할 수 있게 합니다.
    키가 없으면 예외를 던지지 않고 오류 상태로 돌려줍니다 — 음성 · 자막만으로도 검색은 됩니다.
    """
    key_frames = select_key_frames(frames)
    groups = list(chunk(key_frames))
    expected = min(len(groups), MAX_GEMINI_CALLS_PER_VIDEO)
    extra = {
        "expected_calls": expected,
        "successful_calls": 0,
        "failed_calls": 0,
        "capped_calls": max(0, len(groups) - expected),
        "key_frames": len(key_frames),
    }
    if not key_frames:
        return [], stage(OK_EMPTY, backend="gemini", model_id=CAPTION_MODEL, **extra)

    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 - 키가 없으면 캡션 없이 계속합니다
        print(f"[caption] Gemini 클라이언트를 만들 수 없습니다: {type(exc).__name__}: {exc}")
        return [], stage(ERROR, backend="gemini", model_id=CAPTION_MODEL, **describe_exception(exc), **extra)

    results: list[dict] = []
    calls = 0
    first_error: dict | None = None
    quota_stop = False

    for group in groups:
        if calls >= MAX_GEMINI_CALLS_PER_VIDEO or quota_stop:
            # 상한을 넘었거나 일일 할당량이 끊겼으면 남은 프레임은 설명 없이 시간만 남깁니다.
            results.extend(
                {"time": t, "frame_path": str(p), "caption": "", "ocr_text": ""}
                for t, p in group
            )
            continue

        readings, error = _read_grid(client, group)
        calls += 1
        if error is None:
            extra["successful_calls"] += 1
        else:
            extra["failed_calls"] += 1
            first_error = first_error or error
            if error.get("error_code") == NON_RECOVERABLE_QUOTA:
                # 남은 그리드를 더 불러봐야 같은 오류입니다. 할당량만 태웁니다.
                quota_stop = True
        for idx, (time_sec, path) in enumerate(group):
            tile = readings.get(idx, {})
            results.append(
                {
                    "time": time_sec,
                    "frame_path": str(path),
                    "caption": tile.get("description", ""),
                    "ocr_text": tile.get("on_screen_text", ""),
                }
            )

    results.sort(key=lambda item: item["time"])
    extra["quota_stopped"] = quota_stop
    if first_error is not None and extra["successful_calls"] == 0:
        return results, stage(ERROR, backend="gemini", model_id=CAPTION_MODEL, **first_error, **extra)
    if first_error is not None:
        # 일부만 실패. 결과는 쓰되, 부분 실패라는 사실은 남깁니다.
        extra["partial_error"] = first_error
    status = OK if any(item["caption"] or item["ocr_text"] for item in results) else OK_EMPTY
    return results, stage(status, backend="gemini", model_id=CAPTION_MODEL, **extra)


def _read_grid(client, group: list[tuple[float, Path]]) -> tuple[dict[int, dict], dict | None]:
    """그리드 한 장 -> ({index: {description, on_screen_text}}, 오류 정보 또는 None)."""
    from google.genai import types

    from server.gemini import call_with_retry

    image = make_grid(group)
    prompt = PROMPT.format(count=len(group), last=len(group) - 1)
    try:
        response = call_with_retry(
            lambda: client.models.generate_content(
                model=CAPTION_MODEL,
                contents=types.Content(
                    parts=[
                        types.Part(inline_data=types.Blob(data=image, mime_type="image/jpeg")),
                        types.Part(text=prompt),
                    ]
                ),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=GridReading
                ),
            )
        )
        parsed = response.parsed
        if parsed is None:
            # 호출은 성공했는데 쓸 내용이 없음(차단 · JSON 깨짐). '정상적으로 빈 결과'가 아닙니다.
            from server.gemini import empty_reason

            return {}, {
                "error_code": "EMPTY_RESPONSE",
                "error_type": "ParseError",
                "message": f"응답을 읽지 못했습니다({empty_reason(response)}).",
                "retry_count": 0,
            }
        reading = parsed if isinstance(parsed, GridReading) else GridReading.model_validate(parsed)
        return {
            tile.index: {
                "description": tile.description.strip(),
                "on_screen_text": tile.on_screen_text.strip(),
            }
            for tile in reading.tiles
        }, None
    except Exception as exc:  # noqa: BLE001 - 한 그리드가 실패해도 나머지는 계속합니다
        # 조용히 삼키면 "왜 캡션이 다 비었지?" 를 못 찾습니다. 반드시 남깁니다.
        print(f"[caption] 그리드 분석 실패: {type(exc).__name__}: {exc}")
        return {}, describe_exception(exc)
