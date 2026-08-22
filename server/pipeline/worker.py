"""작업 큐 + Worker. 마스터 문서의 "주문표"와 "주방장".

demo 는 서버 프로세스 안의 asyncio 큐로 돕니다. B 담당이 Redis + Celery/RQ 로
갈아끼울 때 enqueue() 만 바꾸면 되도록 만들어 두었습니다.

단계 4(음성) → 5(자막) → 6(장면설명) → 7(장면카드) → 8(좌표) → 9(요약) → 10(완료)
"""
from __future__ import annotations

import asyncio
import time
import traceback

from server import db, embedder, image_embedder, media, vectors
from server.config import FRAME_INTERVAL
from server.pipeline import caption, ocr, transcribe
from server.pipeline.segmenter import build_segments, resolve_duration, search_text
from server.pipeline.summarizer import summarize

_queue: asyncio.Queue[tuple[str, str]] | None = None
_workers: list[asyncio.Task] = []
CONCURRENCY = 2


def _process(user_id: str, video_id: str) -> None:
    """실제 파이프라인. 블로킹 호출이라 별도 스레드에서 돕니다."""
    video = db.get_video(user_id, video_id)
    if video is None:
        return

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
    db.set_status(user_id, video_id, "transcribing")
    try:
        utterances = transcribe.transcribe(media.audio_path(user_id, video_id))
    except Exception as exc:  # noqa: BLE001
        print(f"[worker] 음성 분석 실패, 자막·장면만으로 계속합니다: {type(exc).__name__}: {exc}")
        utterances = []
    mark("transcribe")

    # 단계 5 · 화면 글자 읽기 / 단계 6 · 장면 설명
    db.set_status(user_id, video_id, "analyzing_frames")
    ocr_items = ocr.recognize_all(frames)
    caption_items = caption.describe(frames)
    mark("frames")

    # 단계 7 · 장면 카드 만들기
    duration = resolve_duration(video["duration"], frames, utterances, FRAME_INTERVAL)
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
    db.replace_segments(user_id, video_id, segments)

    # 단계 8 · 좌표로 바꿔 저장
    db.set_status(user_id, video_id, "embedding")
    texts = [search_text(seg, title=video["title"]) for seg in segments]
    text_vectors = embedder.encode(texts)

    image_vectors = None
    if image_embedder.is_enabled():
        frame_dir = media.frames_dir(user_id, video_id)
        paths = [frame_dir / str(seg["frame_path"]).rsplit("/", 1)[-1] for seg in segments]
        if all(p.is_file() for p in paths):
            image_vectors = image_embedder.encode_images(paths)
    vectors.upsert_segments(
        user_id, video_id, video["title"], segments, text_vectors, image_vectors
    )
    mark("embedding")

    # 단계 9 · 전체 요약
    db.set_status(user_id, video_id, "summarizing")
    summary = summarize(video["title"], segments)
    summary["segment_count"] = len(segments)
    summary["frame_count"] = len(frames)
    summary["timings"] = timings  # 9장 실험 "처리 시간 분해" 용
    summary["total_seconds"] = round(time.time() - started, 2)
    db.set_summary(user_id, video_id, summary, duration)

    # 단계 10 · 완료
    db.set_status(user_id, video_id, "ready")


async def _run_one(user_id: str, video_id: str) -> None:
    try:
        await asyncio.to_thread(_process, user_id, video_id)
    except Exception as exc:  # noqa: BLE001 - 실패도 상태로 남겨야 화면이 멈추지 않습니다
        traceback.print_exc()
        db.set_status(user_id, video_id, "failed", f"{type(exc).__name__}: {exc}")


async def _worker_loop() -> None:
    assert _queue is not None
    while True:
        user_id, video_id = await _queue.get()
        try:
            await _run_one(user_id, video_id)
        finally:
            _queue.task_done()


async def start() -> None:
    global _queue
    _queue = asyncio.Queue()
    for _ in range(CONCURRENCY):
        _workers.append(asyncio.create_task(_worker_loop()))


async def stop() -> None:
    for task in _workers:
        task.cancel()
    _workers.clear()


def enqueue(user_id: str, video_id: str) -> None:
    if _queue is None:
        raise RuntimeError("작업 큐가 아직 시작되지 않았습니다.")
    _queue.put_nowait((user_id, video_id))


def queue_size() -> int:
    return _queue.qsize() if _queue is not None else 0
