"""작업 큐 + Worker. 마스터 문서의 "주문표"와 "주방장".

두 종류의 일을 처리합니다.
  1) save      — 저장 직후 기본 조건으로 분석 (단계 4~10)
  2) condition — 같은 프레임/오디오를 다른 백엔드로 재분석 (비교 실험)

demo 는 서버 프로세스 안의 asyncio 큐로 돕니다. B 담당이 Redis + Celery/RQ 로
갈아끼울 때 enqueue() 만 바꾸면 되도록 만들어 두었습니다.
"""
from __future__ import annotations

import asyncio
import time
import traceback

from server import db, vectors
from server.config import DEFAULT_CONDITION, use_condition
from server.pipeline.analyze import analyze_media
from server.pipeline.summarizer import summarize

Job = tuple[str, str, str, str]  # (kind, user_id, video_id, condition)

_queue: asyncio.Queue[Job] | None = None
_workers: list[asyncio.Task] = []
CONCURRENCY = 2


def _process_save(user_id: str, video_id: str) -> None:
    """저장 직후 기본 조건 분석. 데모가 쓰는 경로입니다(동작 그대로)."""
    video = db.get_video(user_id, video_id)
    if video is None:
        return
    started = time.time()

    # 단계 4~8 (비교 실험 러너와 똑같은 코드 경로를 씁니다)
    result = analyze_media(
        user_id,
        video_id,
        title=video["title"],
        client_duration=video["duration"],
        on_status=lambda status: db.set_status(user_id, video_id, status),
    )
    segments = result["segments"]
    db.replace_segments(user_id, video_id, segments)
    vectors.upsert_segments(
        user_id,
        video_id,
        video["title"],
        segments,
        result["text_vectors"],
        result["image_vectors"],
        condition=DEFAULT_CONDITION,
    )

    # 단계 9 · 전체 요약
    db.set_status(user_id, video_id, "summarizing")
    summary = summarize(video["title"], segments)
    summary["segment_count"] = len(segments)
    summary["counts"] = result["counts"]
    summary["timings"] = result["timings"]  # 9장 실험 "처리 시간 분해" 용
    summary["total_seconds"] = round(time.time() - started, 2)
    db.set_summary(user_id, video_id, summary, result["duration"])

    # 단계 10 · 완료
    db.set_status(user_id, video_id, "ready")

    # 기본 조건도 비교표에 나오도록 기록해 둡니다.
    db.start_condition_run(user_id, video_id, DEFAULT_CONDITION)
    db.save_condition_result(
        user_id,
        video_id,
        DEFAULT_CONDITION,
        segments,
        result["counts"],
        result["timings"],
        summary["total_seconds"],
    )


def _process_condition(user_id: str, video_id: str, condition: str) -> None:
    """같은 프레임/오디오를 다른 백엔드로 재분석합니다.

    ⚠ 원본 미디어는 건드리지 않고, 결과만 조건별로 따로 저장합니다.
      보관함/검색 탭이 쓰는 기본 조건 데이터는 그대로 유지됩니다.
    """
    video = db.get_video(user_id, video_id)
    if video is None:
        raise RuntimeError("저장되지 않은 영상입니다.")
    started = time.time()

    with use_condition(condition):
        result = analyze_media(
            user_id,
            video_id,
            title=video["title"],
            client_duration=video["duration"],
            on_status=lambda status: db.set_condition_status(
                user_id, video_id, condition, status
            ),
        )
        vectors.upsert_segments(
            user_id,
            video_id,
            video["title"],
            result["segments"],
            result["text_vectors"],
            result["image_vectors"],
            condition=condition,
        )

    db.save_condition_result(
        user_id,
        video_id,
        condition,
        result["segments"],
        result["counts"],
        result["timings"],
        round(time.time() - started, 2),
    )


async def _run_one(job: Job) -> None:
    kind, user_id, video_id, condition = job
    try:
        if kind == "save":
            await asyncio.to_thread(_process_save, user_id, video_id)
        else:
            await asyncio.to_thread(_process_condition, user_id, video_id, condition)
    except Exception as exc:  # noqa: BLE001 - 실패도 상태로 남겨야 화면이 멈추지 않습니다
        traceback.print_exc()
        message = f"{type(exc).__name__}: {exc}"
        if kind == "save":
            db.set_status(user_id, video_id, "failed", message)
        else:
            db.set_condition_status(user_id, video_id, condition, "failed", message)


async def _worker_loop() -> None:
    assert _queue is not None
    while True:
        job = await _queue.get()
        try:
            await _run_one(job)
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
    _queue.put_nowait(("save", user_id, video_id, DEFAULT_CONDITION))


def enqueue_condition(user_id: str, video_id: str, condition: str) -> None:
    if _queue is None:
        raise RuntimeError("작업 큐가 아직 시작되지 않았습니다.")
    _queue.put_nowait(("condition", user_id, video_id, condition))


def queue_size() -> int:
    return _queue.qsize() if _queue is not None else 0
