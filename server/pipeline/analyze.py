"""단계 4~8 의 알맹이. Worker 와 실험 러너가 **똑같이** 이 함수를 씁니다.

여기를 공유하는 이유: 실험 결과가 실제 제품과 다른 코드로 나오면 그 숫자는
의미가 없습니다. 조건 비교는 반드시 같은 코드 경로로 해야 합니다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from server import embedder, image_embedder, media
from server.config import FRAME_INTERVAL
from server.pipeline import caption, ocr, transcribe
from server.pipeline.segmenter import build_segments, resolve_duration, search_text

StatusFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def analyze_media(
    user_id: str,
    video_id: str,
    title: str,
    client_duration: float = 0.0,
    on_status: StatusFn = _noop,
) -> dict[str, Any]:
    """저장된 프레임/오디오 -> 장면 카드 + 좌표.

    반환: {segments, duration, text_vectors, image_vectors, timings, counts}
    Qdrant 저장과 DB 기록은 호출한 쪽에서 합니다.
    """
    timings: dict[str, float] = {}
    started = time.time()

    def mark(step: str) -> None:
        timings[step] = round(time.time() - started - sum(timings.values()), 2)

    frames = media.list_frames(user_id, video_id)
    if not frames:
        raise RuntimeError("업로드된 프레임이 없습니다. 확장프로그램의 추출이 실패했습니다.")

    # 단계 4 · 음성 받아적기
    # 말이 없는 쇼츠도 있고 ASR 이 일시적으로 죽기도 합니다. 여기서 실패해도
    # 자막(OCR)과 장면 설명만으로 검색은 됩니다. 저장 전체를 실패시키지 않습니다.
    on_status("transcribing")
    try:
        utterances = transcribe.transcribe(media.audio_path(user_id, video_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[analyze] 음성 분석 실패, 자막·장면만으로 계속합니다: {type(exc).__name__}: {exc}")
        utterances = []
    mark("transcribe")

    # 단계 5 · 화면 글자 읽기 / 단계 6 · 장면 설명
    on_status("analyzing_frames")
    ocr_items = ocr.recognize_all(frames)
    caption_items = caption.describe(frames)
    mark("frames")

    # 단계 7 · 장면 카드 만들기
    duration = resolve_duration(client_duration, frames, utterances, FRAME_INTERVAL)
    segments = build_segments(
        video_id,
        duration,
        frames,
        utterances,
        ocr_items,
        caption_items,
        media_root=media.video_dir(user_id, video_id),
    )
    if not segments:
        raise RuntimeError("장면 카드를 하나도 만들지 못했습니다. 음성·자막·장면이 모두 비었습니다.")

    # 단계 8 · 좌표로 바꾸기
    on_status("embedding")
    texts = [search_text(seg, title=title) for seg in segments]
    text_vectors = embedder.encode(texts)

    image_vectors = None
    if image_embedder.is_enabled():
        image_vectors = _encode_segment_frames(user_id, video_id, segments)
    mark("embedding")

    return {
        "segments": segments,
        "duration": duration,
        "text_vectors": text_vectors,
        "image_vectors": image_vectors,
        "timings": timings,
        "counts": {
            "frames": len(frames),
            "utterances": len(utterances),
            "ocr_items": len(ocr_items),
            "caption_items": len(caption_items),
            "segments": len(segments),
            # 정보원별 기여도 실험(9장)에서 쓸 값들
            "segments_with_asr": sum(1 for s in segments if s["asr_text"]),
            "segments_with_ocr": sum(1 for s in segments if s["ocr_text"]),
            "segments_with_caption": sum(1 for s in segments if s["caption"]),
        },
    }


def _encode_segment_frames(user_id: str, video_id: str, segments: list[dict[str, Any]]):
    frame_dir = media.frames_dir(user_id, video_id)
    paths: list[Path] = [
        frame_dir / str(seg["frame_path"]).rsplit("/", 1)[-1] for seg in segments
    ]
    if not all(p.is_file() for p in paths):
        print("[analyze] 대표 프레임 파일을 못 찾아 그림 좌표를 건너뜁니다.")
        return None
    return image_embedder.encode_images(paths)
