"""단계 4~8 의 알맹이. Worker 와 실험 러너가 **똑같이** 이 함수를 씁니다.

여기를 공유하는 이유: 실험 결과가 실제 제품과 다른 코드로 나오면 그 숫자는
의미가 없습니다. 조건 비교는 반드시 같은 코드 경로로 해야 합니다.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from server import embedder, image_embedder, media
from server.config import FRAME_INTERVAL, WHISPER_MODEL, backend
from server.pipeline import caption, ocr, transcribe
from server.pipeline.health import (
    OK,
    OK_EMPTY,
    SKIPPED,
    error_stage,
    stage,
    summarize_health,
)
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

    반환: {segments, duration, text_vectors, image_vectors, timings, counts, analysis_health}
    Qdrant 저장과 DB 기록은 호출한 쪽에서 합니다.

    analysis_health 는 단계(asr · ocr · caption)마다 결과가 있었는지 / 정상적으로 비었는지 / 실패했는지를
    적습니다. 실패해도 나머지로 저장은 계속하지만, "말이 없는 영상"과 "음성 인식이 죽은 영상"을
    구분할 수 있어야 평가에서 그 영상을 걸러낼 수 있습니다.
    """
    timings: dict[str, float] = {}
    started = time.time()
    health: dict[str, dict[str, Any]] = {}

    def mark(step: str) -> None:
        timings[step] = round(time.time() - started - sum(timings.values()), 2)

    frames = media.list_frames(user_id, video_id)
    if not frames:
        raise RuntimeError("업로드된 프레임이 없습니다. 확장프로그램의 추출이 실패했습니다.")

    # 단계 4 · 음성 받아적기
    # 말이 없는 쇼츠도 있고 ASR 이 일시적으로 죽기도 합니다. 여기서 실패해도
    # 자막(OCR)과 장면 설명만으로 검색은 됩니다. 저장 전체를 실패시키지는 않지만,
    # **실패했다는 사실과 원인은 반드시 남깁니다**(빈 결과로 둔갑시키지 않음).
    on_status("transcribing")
    asr_backend = backend("asr")
    asr_model = WHISPER_MODEL if asr_backend != "gemini" else "gemini"
    audio = media.audio_path(user_id, video_id)
    try:
        utterances = transcribe.transcribe(audio)
        if utterances:
            health["asr"] = stage(OK, backend=asr_backend, model_id=asr_model)
        else:
            has_audio = audio.is_file() and audio.stat().st_size > 0
            health["asr"] = stage(
                OK_EMPTY,
                backend=asr_backend,
                model_id=asr_model,
                note="말이 없는 영상" if has_audio else "오디오 파일이 없음(프레임만 캡처됨)",
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[analyze] 음성 분석 실패, 자막·장면만으로 계속합니다: {type(exc).__name__}: {exc}")
        utterances = []
        health["asr"] = error_stage(exc, backend=asr_backend, model_id=asr_model)
    mark("transcribe")

    # 단계 5 · 화면 글자 읽기 / 단계 6 · 장면 설명
    on_status("analyzing_frames")
    if ocr.uses_grid():
        ocr_items = []
        health["ocr"] = stage(SKIPPED, backend="gemini-grid", note="캡션 그리드 호출이 함께 읽습니다")
    else:
        try:
            # 모든 프레임이 실패하면 예외(아래 except → 오류). 일부만 실패하면 결과는 쓰되 그 사실을 남깁니다.
            ocr_items, ocr_counts = ocr.recognize_all_counted(frames)
            partial = {}
            if ocr_counts["failed_frames"]:
                partial = {
                    "failed_frames": ocr_counts["failed_frames"],
                    "partial_error": ocr_counts["first_error"],
                }
            health["ocr"] = stage(OK if ocr_items else OK_EMPTY, backend=backend("ocr"), **partial)
        except Exception as exc:  # noqa: BLE001
            print(f"[analyze] 화면 글자 읽기 실패: {type(exc).__name__}: {exc}")
            ocr_items = []
            health["ocr"] = error_stage(exc, backend=backend("ocr"))
    caption_items, health["caption"] = caption.describe_with_health(frames)
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
        failed = summarize_health(health)["failed_stages"]
        reason = f" 실패한 단계: {', '.join(failed)}" if failed else ""
        raise RuntimeError(
            "장면 카드를 하나도 만들지 못했습니다. 음성·자막·장면이 모두 비었습니다." + reason
        )

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
        "analysis_health": {**health, "summary": summarize_health(health)},
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
