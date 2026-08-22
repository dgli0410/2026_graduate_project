"""단계 7 · 장면 카드 만들기. 담당 C.

말(WhisperX) · 자막(OCR) · 설명(Gemini) 세 결과를 시간 기준으로 합쳐
4초짜리 장면 카드로 정리합니다.

카드 모양은 마스터 문서 6장 견본과 같습니다:
    {segment_id, start_time, end_time, asr_text, ocr_text, caption, frame_path}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from server.config import SEGMENT_SECONDS


def _join(values: Iterable[str]) -> str:
    picked: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in picked:
            picked.append(text)
    return " ".join(picked)


def _spans_in_window(items: list[dict], start: float, end: float) -> str:
    """[start, end) 와 겹치는 구간(start/end 를 가진 항목)의 글."""
    return _join(
        item.get("text", "")
        for item in items
        if float(item.get("start", 0.0)) < end and float(item.get("end", 0.0)) > start
    )


def _points_in_window(items: list[dict], start: float, end: float, key: str = "text") -> str:
    """[start, end) 안에 있는 시점(time 하나만 가진 항목)의 글."""
    return _join(
        item.get(key, "") for item in items if start <= float(item.get("time", -1.0)) < end
    )


def _nearest_frame(
    frames: list[tuple[float, Path]], center: float, media_root: Path | None
) -> str:
    if not frames:
        return ""
    _, path = min(frames, key=lambda item: abs(item[0] - center))
    if media_root is not None:
        try:
            return path.relative_to(media_root).as_posix()
        except ValueError:
            pass
    return path.name


def resolve_duration(
    client_duration: float,
    frames: list[tuple[float, Path]],
    utterances: list[dict],
    frame_interval: float,
) -> float:
    candidates = [client_duration or 0.0]
    if frames:
        candidates.append(frames[-1][0] + frame_interval)
    if utterances:
        candidates.append(max(float(u.get("end", 0.0)) for u in utterances))
    return round(max(candidates), 2)


def build_segments(
    video_id: str,
    duration: float,
    frames: list[tuple[float, Path]],
    utterances: list[dict],
    ocr_items: list[dict],
    caption_items: list[dict],
    media_root: Path | None = None,
    window: float = SEGMENT_SECONDS,
) -> list[dict[str, Any]]:
    if duration <= 0:
        return []

    # OCR 이 별도로 안 돌았으면(gemini 백엔드) 그리드 결과의 글자를 씁니다.
    if not ocr_items:
        ocr_items = [
            {"time": item["time"], "text": item.get("ocr_text", "")}
            for item in caption_items
            if item.get("ocr_text")
        ]

    segments: list[dict[str, Any]] = []
    index = 0
    start = 0.0
    while start < duration:
        end = min(start + window, duration)
        # 마지막 조각이 너무 짧으면 앞 카드에 합칩니다.
        if 0 < duration - end < window * 0.4:
            end = duration

        asr = _spans_in_window(utterances, start, end)
        ocr = _points_in_window(ocr_items, start, end)
        caption = _points_in_window(caption_items, start, end, key="caption")

        # 세 정보가 전부 비어 있으면 검색에 쓸모가 없으므로 버립니다.
        if asr or ocr or caption:
            segments.append(
                {
                    "segment_id": f"{video_id}_seg_{index:03d}",
                    "start_time": round(start, 2),
                    "end_time": round(end, 2),
                    "asr_text": asr,
                    "ocr_text": ocr,
                    "caption": caption,
                    "frame_path": _nearest_frame(frames, (start + end) / 2, media_root),
                }
            )
            index += 1
        start = end
    return segments


def search_text(segment: dict[str, Any], title: str = "") -> str:
    """장면 카드를 글 좌표(BGE-M3) 입력용 한 덩어리로 만듭니다. 단계 8."""
    return ". ".join(
        part
        for part in _dedup(
            [
                title,
                segment.get("caption", ""),
                segment.get("asr_text", ""),
                segment.get("ocr_text", ""),
            ]
        )
    )


def _dedup(values: list[str]) -> list[str]:
    picked: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in picked:
            picked.append(text)
    return picked
