"""단계 7 · 장면 카드 만들기. 담당 C.

말(WhisperX) · 자막(OCR) · 설명(Gemini) 세 결과를 시간 기준으로 합쳐
4초짜리 장면 카드로 정리합니다.

카드 모양은 마스터 문서 6장 견본과 같습니다:
    {segment_id, start_time, end_time, asr_text, ocr_text, caption, frame_path}
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from server.config import SEGMENT_SECONDS

# 0.5초마다 한 장이라 4초 카드에는 같은 자막이 8번쯤 들어옵니다. 글자가 정확히 같으면
# 쉽게 걸리지만 OCR 은 프레임마다 조금씩 다르게 읽습니다("올리브유 두르고" / "올리브유 무르고").
# 그대로 두면 한 카드의 자막이 같은 말의 변형으로 채워져, 단어 매칭과 글 좌표가 왜곡됩니다.
NEAR_DUPLICATE = 0.8


def _key(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def _join(values: Iterable[str]) -> str:
    picked: list[str] = []
    keys: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = _key(text)
        if not key or any(
            SequenceMatcher(None, key, seen).ratio() >= NEAR_DUPLICATE for seen in keys
        ):
            continue
        keys.append(key)
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


# --- 의미 단위 병합 (멘토 자문 1번) ---------------------------------------
#
# > 4초 고정 분할은 '재료를 볶는다', '불을 줄인다' 같은 의미 단위와 경계가 어긋나
# > 하나의 행위가 여러 조각으로 흩어진다. **고정 분할을 기준선으로 두고, 인접
# > 세그먼트를 의미 단위로 병합하는 후처리를 추가**할 것. (2026-09-18 서면 자문)
#
# 실제 데이터를 보니 "흩어진다" 가 더 나쁜 형태로 나타나고 있었습니다.
# `_spans_in_window` 는 창에 **걸치기만 해도** 그 발화를 통째로 넣습니다. 그래서 10초짜리
# 발화 하나가 4초 카드 3장에 **글자 그대로 복사**됩니다. 저장된 18개 영상의 인접 쌍 147개
# 중 말 토큰 자카드가 1.00(= 완전히 같은 글)인 쌍이 실제로 있었습니다.
#
# 그래서 병합 기준을 **포함도**로 잡았습니다 — 짧은 쪽 토큰이 긴 쪽에 얼마나 들어 있나.
# 자카드가 아니라 포함도인 이유: 카드 하나가 다음 카드의 앞부분만 공유하는 경우
# (발화가 걸친 전형적인 모양)를 자카드는 길이 차이 때문에 낮게 봅니다.
#
# ⚠ 고정 분할은 **없애지 않습니다.** 멘토가 "기준선으로 두고" 라고 명시했고,
#   `MERGE_SIMILARITY=0` 이면 이 후처리는 아무것도 하지 않아 예전과 같습니다.

def _tokens(segment: dict[str, Any]) -> set[str]:
    text = " ".join(
        str(segment.get(k, "")) for k in ("asr_text", "ocr_text", "caption")
    )
    return set(re.findall(r"[가-힣]{2,}|[A-Za-z]{2,}|\d+[a-zA-Z가-힣]*", text.lower()))


def _containment(a: set[str], b: set[str]) -> float:
    """짧은 쪽이 긴 쪽에 얼마나 들어 있나. 한쪽이 비면 0 (빈 카드는 병합 근거가 못 됩니다)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def merge_segments(
    segments: list[dict[str, Any]],
    video_id: str,
    frames: list[tuple[float, Path]] | None = None,
    media_root: Path | None = None,
    similarity: float = 0.0,
    max_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """인접 카드가 같은 의미 단위면 하나로 합칩니다. 단계 7 후처리.

    `similarity` 가 0 이면 그대로 돌려줍니다(= 고정 분할 기준선).
    `max_seconds` 는 한 카드가 무한정 길어지는 것을 막습니다. 말이 끊김 없이 이어지는
    영상에서는 이 상한이 없으면 영상 하나가 카드 한 장이 됩니다.

    글은 `_join` 으로 합쳐 근접 중복을 한 번 더 걸러냅니다 — 병합의 주 효과가
    **같은 발화의 복사본을 없애는 것**이라 이게 핵심입니다.
    """
    if similarity <= 0 or len(segments) < 2:
        return segments

    merged: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = [segments[0]]

    def flush() -> None:
        if len(group) == 1:
            merged.append(dict(group[0]))
            return
        start = min(float(s["start_time"]) for s in group)
        end = max(float(s["end_time"]) for s in group)
        card = {
            "segment_id": "",  # 아래에서 다시 매깁니다
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "asr_text": _join(s.get("asr_text", "") for s in group),
            "ocr_text": _join(s.get("ocr_text", "") for s in group),
            "caption": _join(s.get("caption", "") for s in group),
            "frame_path": "",
        }
        # 대표 프레임은 합쳐진 구간의 가운데로 다시 고릅니다.
        if frames:
            card["frame_path"] = _nearest_frame(frames, (start + end) / 2, media_root)
        else:
            card["frame_path"] = group[len(group) // 2].get("frame_path", "")
        merged.append(card)

    for nxt in segments[1:]:
        span = float(nxt["end_time"]) - float(group[0]["start_time"])
        too_long = max_seconds > 0 and span > max_seconds
        close = _containment(_tokens(group[-1]), _tokens(nxt)) >= similarity
        if close and not too_long:
            group.append(nxt)
        else:
            flush()
            group = [nxt]
    flush()

    for i, card in enumerate(merged):
        card["segment_id"] = f"{video_id}_seg_{i:03d}"
    return merged


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
