"""Gemini 없이 돌려보는 자체 점검.

장면 카드 합치기(단계 7), 검색 신호 계산(단계 12), 중복 제거를 가짜 데이터로
확인합니다. API 키가 없어도 실행됩니다.

    python -m tools.smoke_test
"""
from __future__ import annotations

import sys
from pathlib import Path

from server.pipeline.segmenter import build_segments, resolve_duration, search_text
from server.search import classify_query, dedup_adjacent, detect_intent, group_by_video, weights_for

# 확장프로그램이 0.5초 간격으로 올린 프레임이라고 가정합니다.
FRAMES = [(round(i * 0.5, 1), Path(f"frames/{int(i * 500):08d}.jpg")) for i in range(60)]

# 단계 4 · WhisperX 결과
UTTERANCES = [
    {"start": 0.0, "end": 6.0, "text": "오늘은 감자조림을 만들어 볼게요"},
    {"start": 10.0, "end": 14.0, "text": "양파를 먼저 볶아줍니다"},
    {"start": 21.0, "end": 25.0, "text": "이제 감자를 넣어줍니다"},
    {"start": 26.0, "end": 29.5, "text": "간장을 넣고 조려주세요"},
]

# 단계 5 · OCR 결과
OCR = [
    {"time": 9.5, "text": "양파 1개"},
    {"time": 21.0, "text": "감자 300g"},
    {"time": 27.0, "text": "간장 2큰술"},
]

# 단계 6 · Gemini 캡션 결과 (대표 프레임에만 붙습니다)
CAPTIONS = [
    {"time": 0.0, "frame_path": "frames/00000000.jpg", "caption": "재료를 도마 위에 늘어놓는 모습", "ocr_text": ""},
    {"time": 10.0, "frame_path": "frames/00010000.jpg", "caption": "달군 팬에 양파를 볶는 모습", "ocr_text": "양파 1개"},
    {"time": 22.0, "frame_path": "frames/00022000.jpg", "caption": "손질된 감자를 프라이팬에 넣는 모습", "ocr_text": "감자 300g"},
    {"time": 27.0, "frame_path": "frames/00027000.jpg", "caption": "간장을 붓고 국물을 졸이는 모습", "ocr_text": "간장 2큰술"},
]


def main() -> int:
    duration = resolve_duration(0.0, FRAMES, UTTERANCES, 0.5)
    segments = build_segments("abc123", duration, FRAMES, UTTERANCES, OCR, CAPTIONS)
    print(f"[단계 7] duration={duration}s → 장면 카드 {len(segments)}장")
    for seg in segments:
        print(
            f"  {seg['segment_id']}  {seg['start_time']:5.1f}~{seg['end_time']:5.1f}s"
            f" | 말:{seg['asr_text'] or '-'} | 자막:{seg['ocr_text'] or '-'}"
            f" | 장면:{seg['caption'] or '-'} | 프레임:{seg['frame_path']}"
        )
    assert segments, "장면 카드가 하나도 안 만들어졌습니다"
    assert all(s["end_time"] > s["start_time"] for s in segments)
    assert all(s["frame_path"] for s in segments), "대표 프레임이 안 붙은 카드가 있습니다"

    potato = [s for s in segments if "감자를 넣" in s["asr_text"]]
    assert potato, "감자 투입 대사가 어느 카드에도 안 들어갔습니다"
    print(f"[확인] '감자 넣는' 장면 → {potato[0]['start_time']}s 카드, 자막 '{potato[0]['ocr_text']}'")

    print("\n[단계 8 입력]")
    print(" ", search_text(segments[0], title="감자조림 레시피"))

    print("\n[단계 12] 검색어 성격 → 비중 (글 좌표, 정확한 단어, 그림 좌표)")
    for query in ["감자 언제 넣어?", "빨간 냄비 나오는 장면", "간장 2큰술", "간장으로 조리는 영상 찾아줘"]:
        dense, lexical, image = weights_for(query)
        print(
            f"  {query!r:28} → {classify_query(query):7} / {detect_intent(query):7}"
            f" / ({dense:.2f}, {lexical:.2f}, {image:.2f})"
        )
    assert classify_query("간장 2큰술") == "lexical", "수량 표현이 lexical 로 안 잡혔습니다"
    assert classify_query("빨간 냄비 나오는 장면") == "visual", "시각 표현이 visual 로 안 잡혔습니다"
    assert detect_intent("간장으로 조리는 영상 찾아줘") == "video"

    fake_hits = [
        {"score": 0.9, "video_id": "a", "start_time": 20.0, "end_time": 24.0, "title": "감자조림"},
        {"score": 0.8, "video_id": "a", "start_time": 22.0, "end_time": 26.0, "title": "감자조림"},
        {"score": 0.7, "video_id": "b", "start_time": 4.0, "end_time": 8.0, "title": "볶음밥"},
    ]
    deduped = dedup_adjacent(fake_hits, top_k=5)
    print(f"\n[단계 12] 중복 제거: {len(fake_hits)}건 → {len(deduped)}건")
    assert len(deduped) == 2, "겹치는 장면이 제거되지 않았습니다"
    print(f"[단계 12] 영상 묶기: {[v['video_id'] for v in group_by_video(fake_hits)]}")

    print("\n모두 통과했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
