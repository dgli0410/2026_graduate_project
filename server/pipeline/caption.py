"""단계 6 · 장면 설명 만들기 (+ 그리드 방식일 때는 단계 5 화면 글자도 함께). 담당 D.

프레임 6장을 한 장으로 붙여 Gemini 에 한 번만 물어봅니다.
호출 수는 MAX_GEMINI_CALLS_PER_VIDEO 로 코드에서 막습니다.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from server.config import CAPTION_MODEL, MAX_GEMINI_CALLS_PER_VIDEO
from server.gemini import get_client
from server.pipeline.frames import chunk, make_grid, select_key_frames


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

    frames 는 (시각, 파일경로) 목록입니다.
    """
    key_frames = select_key_frames(frames)
    if not key_frames:
        return []

    client = get_client()
    results: list[dict] = []
    calls = 0

    for group in chunk(key_frames):
        if calls >= MAX_GEMINI_CALLS_PER_VIDEO:
            # 상한을 넘으면 남은 프레임은 설명 없이 시간만 남깁니다.
            results.extend(
                {"time": t, "frame_path": str(p), "caption": "", "ocr_text": ""}
                for t, p in group
            )
            continue

        readings = _read_grid(client, group)
        calls += 1
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
    return results


def _read_grid(client, group: list[tuple[float, Path]]) -> dict[int, dict]:
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
            return {}
        reading = parsed if isinstance(parsed, GridReading) else GridReading.model_validate(parsed)
        return {
            tile.index: {
                "description": tile.description.strip(),
                "on_screen_text": tile.on_screen_text.strip(),
            }
            for tile in reading.tiles
        }
    except Exception as exc:  # noqa: BLE001 - 한 그리드가 실패해도 나머지는 계속합니다
        # 조용히 삼키면 "왜 캡션이 다 비었지?" 를 못 찾습니다. 반드시 남깁니다.
        print(f"[caption] 그리드 분석 실패: {type(exc).__name__}: {exc}")
        return {}
