"""API 서버. 마스터 문서의 "카운터 직원".

중요: 이 서버는 **유튜브에 한 번도 접속하지 않습니다.**
화면과 소리는 확장프로그램이 브라우저에서 뽑아 올려줍니다(단계 2, 3).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server import db, embedder, image_embedder, media, search as search_mod, vectors
from server.config import (
    CAPTION_MODEL,
    CONDITIONS,
    DEFAULT_CONDITION,
    DEVICE,
    MAX_GEMINI_CALLS_PER_VIDEO,
    SEARCH_TOP_K,
    SEGMENT_SECONDS,
    backend,
    capture_settings,
    condition_info,
    use_condition,
)
from server.pipeline import worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await worker.start()
    _requeue_unfinished()
    yield
    await worker.stop()


def _requeue_unfinished() -> None:
    """서버가 중간에 꺼졌다 켜졌을 때, 파일이 이미 올라온 작업만 다시 큐에 넣습니다."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT user_id, video_id FROM videos WHERE status NOT IN ('ready','failed')"
        ).fetchall()
    for row in rows:
        user_id, video_id = row["user_id"], row["video_id"]
        if media.list_frames(user_id, video_id):
            db.set_status(user_id, video_id, "queued")
            worker.enqueue(user_id, video_id)
        else:
            # 추출 도중 끊긴 건 다시 저장해야 합니다.
            db.set_status(user_id, video_id, "failed", "추출이 완료되지 않았습니다. 다시 저장해주세요.")


app = FastAPI(title="쇼츠 AI 보관함 (Demo)", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # youtube.com 이 들어 있는 이유: 프레임·오디오 업로드를 content script 가 직접 합니다.
    # MV3 content script 의 fetch 는 페이지 origin 으로 나가기 때문입니다.
    # 공용 서버에 올릴 때는 Cloudflare Tunnel + 이 목록 정리가 필요합니다(마스터 문서 11장).
    allow_origin_regex=(
        r"chrome-extension://.*|https://(www|m)\.youtube\.com"
        r"|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?"
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_user(x_user_id: str = Header(default="")) -> str:
    """demo 용 사용자 식별. 확장프로그램이 만든 uuid 를 헤더로 보냅니다.

    실제 서비스라면 여기에 로그인/토큰 검증이 들어갑니다. 중요한 건 이 값이
    검색·삭제의 필터로 끝까지 따라간다는 점입니다(마스터 문서 단계 12-4).
    """
    user_id = (x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id 헤더가 필요합니다.")
    return user_id


class SaveRequest(BaseModel):
    video_id: str
    title: str = ""
    channel: str = ""
    duration: float = 0.0
    playlist_id: str | None = None


class SearchRequest(BaseModel):
    query: str
    video_id: str | None = None
    playlist_id: str | None = None
    top_k: int = Field(default=SEARCH_TOP_K, ge=1, le=20)


class PlaylistRequest(BaseModel):
    name: str


class PlaylistItemRequest(BaseModel):
    video_id: str


class CompareRequest(BaseModel):
    video_id: str
    query: str
    conditions: list[str] | None = None
    top_k: int = Field(default=3, ge=1, le=10)


# --- 상태 -----------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    ready, reason = embedder.is_ready()
    return {
        "ok": ready,
        "reason": reason,
        "device": DEVICE,
        "backends": {
            "asr": backend("asr"),
            "ocr": backend("ocr"),
            "text_embed": backend("text_embed"),
            "image_embed": backend("image_embed"),
            "caption_model": CAPTION_MODEL,
        },
        "capture": capture_settings(),
        "segment_seconds": SEGMENT_SECONDS,
        "max_gemini_calls_per_video": MAX_GEMINI_CALLS_PER_VIDEO,
        "image_search_enabled": image_embedder.is_enabled(),
        "queue_size": worker.queue_size(),
    }


# --- 단계 3 · 접수 ---------------------------------------------------------

@app.post("/api/videos")
def save_video(req: SaveRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
    """1) 메타데이터 접수. 확장은 이 응답을 받고 화면·소리 추출을 시작합니다."""
    if not req.video_id.strip():
        raise HTTPException(status_code=400, detail="video_id 가 비어 있습니다.")
    ready, reason = embedder.is_ready()
    if not ready:
        raise HTTPException(status_code=503, detail=reason)

    video_id = req.video_id.strip()
    created = db.upsert_video(user_id, video_id, req.title, req.channel, req.duration)
    if req.playlist_id:
        db.add_to_playlist(user_id, req.playlist_id, video_id)
    if created:
        media.clear(user_id, video_id)  # 이전 시도의 찌꺼기 정리
    return {
        "accepted": True,
        "already_saved": not created,
        "needs_capture": created,
        "capture": capture_settings(),
        "video": db.get_video(user_id, video_id),
    }


@app.post("/api/videos/{video_id}/media")
async def upload_media(
    video_id: str,
    frames: list[UploadFile] = File(default=[]),
    audio: UploadFile | None = File(default=None),
    duration: float = Form(default=0.0),
    user_id: str = Depends(current_user),
) -> dict[str, Any]:
    """2) 추출된 프레임·오디오 수신 → 작업 큐 등록.

    프레임 파일 이름이 곧 시각입니다: 000500.jpg = 0.5초
    """
    video = db.get_video(user_id, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="먼저 /api/videos 로 접수해야 합니다.")
    if not frames:
        raise HTTPException(status_code=400, detail="프레임이 하나도 오지 않았습니다.")

    frames_dir = media.frames_dir(user_id, video_id)
    frames_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for upload in frames:
        name = (upload.filename or "").rsplit("/", 1)[-1]
        if not name.endswith(".jpg") or not name[:-4].isdigit():
            continue
        (frames_dir / name).write_bytes(await upload.read())
        saved += 1
    if saved == 0:
        raise HTTPException(status_code=400, detail="프레임 파일 이름 형식이 잘못됐습니다.")

    if audio is not None:
        data = await audio.read()
        if data:
            media.audio_path(user_id, video_id).write_bytes(data)

    if duration > 0:
        db.set_duration(user_id, video_id, duration)

    db.set_status(user_id, video_id, "queued")
    worker.enqueue(user_id, video_id)
    return {
        "queued": True,
        "frames": saved,
        "audio_bytes": media.audio_path(user_id, video_id).stat().st_size
        if media.audio_path(user_id, video_id).exists()
        else 0,
        "video": db.get_video(user_id, video_id),
    }


# --- 보관함 ---------------------------------------------------------------

@app.get("/api/videos")
def get_videos(
    playlist_id: str | None = None, user_id: str = Depends(current_user)
) -> dict[str, Any]:
    return {"videos": db.list_videos(user_id, playlist_id)}


@app.get("/api/pending")
def get_pending(user_id: str = Depends(current_user)) -> dict[str, Any]:
    """Side Panel 이 3초마다 부르는 가벼운 상태 확인용."""
    return {
        "pending": db.pending_video_ids(user_id),
        "pending_conditions": db.pending_condition_runs(user_id),
    }


@app.get("/api/videos/{video_id}")
def get_video_detail(video_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
    video = db.get_video(user_id, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="저장되지 않은 영상입니다.")
    segments = db.list_segments(user_id, video_id)
    for segment in segments:
        name = str(segment.get("frame_path", "")).rsplit("/", 1)[-1]
        if name:
            segment["frame_url"] = f"/api/videos/{video_id}/frames/{name}"
    video["segments"] = segments
    video["media_bytes"] = media.usage_bytes(user_id, video_id)
    return video


@app.get("/api/videos/{video_id}/frames/{name}")
def get_frame(video_id: str, name: str, u: str = "", x_user_id: str = Header(default="")):
    """검색 결과 카드에 띄울 장면 사진(단계 13).

    <img src> 는 헤더를 붙일 수 없어서 ?u= 쿼리로도 사용자 식별을 받습니다.
    """
    user_id = (x_user_id or u or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="사용자 식별이 필요합니다.")
    path = media.resolve_frame(user_id, video_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="프레임을 찾을 수 없습니다.")
    return FileResponse(path, media_type="image/jpeg")


@app.delete("/api/videos/{video_id}")
def remove_video(video_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
    if db.get_video(user_id, video_id) is None:
        raise HTTPException(status_code=404, detail="저장되지 않은 영상입니다.")
    # 표 · 창고 · 이미지 3곳을 모두 지웁니다 (마스터 문서 단계 11, 12장 함정).
    # 창고는 조건별로 나뉘어 있으므로 전부 돌면서 지웁니다.
    vectors.delete_video_all_conditions(user_id, video_id)
    media.clear(user_id, video_id)
    db.delete_condition_runs(user_id, video_id)
    db.delete_video(user_id, video_id)
    return {"deleted": True, "video_id": video_id}


# --- 재생목록 --------------------------------------------------------------

@app.get("/api/playlists")
def get_playlists(user_id: str = Depends(current_user)) -> dict[str, Any]:
    return {"playlists": db.list_playlists(user_id)}


@app.post("/api/playlists")
def add_playlist(req: PlaylistRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="재생목록 이름이 비어 있습니다.")
    return db.create_playlist(user_id, name)


@app.delete("/api/playlists/{playlist_id}")
def remove_playlist(playlist_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
    db.delete_playlist(user_id, playlist_id)
    return {"deleted": True, "playlist_id": playlist_id}


@app.post("/api/playlists/{playlist_id}/videos")
def playlist_add_video(
    playlist_id: str, req: PlaylistItemRequest, user_id: str = Depends(current_user)
) -> dict[str, Any]:
    if db.get_video(user_id, req.video_id) is None:
        raise HTTPException(status_code=404, detail="먼저 저장된 영상이어야 합니다.")
    db.add_to_playlist(user_id, playlist_id, req.video_id)
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}/videos/{video_id}")
def playlist_remove_video(
    playlist_id: str, video_id: str, user_id: str = Depends(current_user)
) -> dict[str, Any]:
    db.remove_from_playlist(user_id, playlist_id, video_id)
    return {"ok": True}


# --- 비교 실험 -------------------------------------------------------------
# 같은 프레임·오디오를 다른 백엔드로 다시 돌려 나란히 봅니다.
# 보관함/검색 탭이 쓰는 기본 조건 데이터는 건드리지 않습니다.

@app.get("/api/conditions")
def get_conditions() -> dict[str, Any]:
    return {
        "default": DEFAULT_CONDITION,
        "conditions": [condition_info(name) for name in CONDITIONS],
    }


def _backfill_default_run(user_id: str, video: dict[str, Any]) -> None:
    """비교 기능을 만들기 전에 분석된 영상도 비교표에 나오도록 기록을 채웁니다."""
    video_id = video["video_id"]
    if video["status"] != "ready":
        return
    if any(
        run["condition"] == DEFAULT_CONDITION and run["status"] == "ready"
        for run in db.list_condition_runs(user_id, video_id)
    ):
        return

    segments = db.list_segments(user_id, video_id)
    if not segments:
        return
    summary = video.get("summary") or {}
    counts = summary.get("counts") or {
        "segments": len(segments),
        "segments_with_asr": sum(1 for s in segments if s.get("asr_text")),
        "segments_with_ocr": sum(1 for s in segments if s.get("ocr_text")),
        "segments_with_caption": sum(1 for s in segments if s.get("caption")),
        "frames": len(media.list_frames(user_id, video_id)),
    }
    db.start_condition_run(user_id, video_id, DEFAULT_CONDITION)
    db.save_condition_result(
        user_id,
        video_id,
        DEFAULT_CONDITION,
        segments,
        counts,
        summary.get("timings") or {},
        summary.get("total_seconds") or 0.0,
    )


@app.get("/api/videos/{video_id}/conditions")
def get_video_conditions(video_id: str, user_id: str = Depends(current_user)) -> dict[str, Any]:
    video = db.get_video(user_id, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="저장되지 않은 영상입니다.")
    _backfill_default_run(user_id, video)
    runs = {run["condition"]: run for run in db.list_condition_runs(user_id, video_id)}
    return {
        "video_id": video_id,
        "runs": [
            {**condition_info(name), **runs.get(name, {"status": "none", "status_label": "안 돌림"})}
            for name in CONDITIONS
        ],
    }


@app.post("/api/videos/{video_id}/conditions/{condition}")
def run_condition(
    video_id: str, condition: str, user_id: str = Depends(current_user)
) -> dict[str, Any]:
    """같은 프레임·오디오를 그 조건으로 재분석합니다. 원본은 그대로 둡니다."""
    if condition not in CONDITIONS:
        raise HTTPException(status_code=400, detail=f"모르는 조건입니다: {condition}")
    if db.get_video(user_id, video_id) is None:
        raise HTTPException(status_code=404, detail="저장되지 않은 영상입니다.")
    if not media.list_frames(user_id, video_id):
        raise HTTPException(status_code=400, detail="저장된 프레임이 없어 재분석할 수 없습니다.")

    info = condition_info(condition)
    if not info["ready"]:
        raise HTTPException(
            status_code=503,
            detail=f"필요한 패키지가 없습니다. {info['install_hint']}",
        )

    db.start_condition_run(user_id, video_id, condition)
    worker.enqueue_condition(user_id, video_id, condition)
    return {"queued": True, "video_id": video_id, "condition": condition}


@app.get("/api/videos/{video_id}/conditions/{condition}/segments")
def get_condition_segments(
    video_id: str, condition: str, user_id: str = Depends(current_user)
) -> dict[str, Any]:
    segments = db.get_condition_segments(user_id, video_id, condition)
    for segment in segments:
        name = str(segment.get("frame_path", "")).rsplit("/", 1)[-1]
        if name:
            segment["frame_url"] = f"/api/videos/{video_id}/frames/{name}"
    return {"video_id": video_id, "condition": condition, "segments": segments}


@app.post("/api/compare")
def compare(req: CompareRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
    """한 질문을 여러 조건으로 동시에 검색해 나란히 돌려줍니다."""
    if db.get_video(user_id, req.video_id) is None:
        raise HTTPException(status_code=404, detail="저장되지 않은 영상입니다.")
    names = req.conditions or list(CONDITIONS)

    results = []
    for name in names:
        if name not in CONDITIONS:
            continue
        if not vectors.collection_exists(name):
            results.append({"condition": name, "available": False, "scenes": []})
            continue
        with use_condition(name):
            # 답변 생성은 조건 비교와 무관하므로 끕니다(Gemini 호출 절약 + 공정성).
            found = search_mod.search(
                user_id,
                req.query,
                video_id=req.video_id,
                top_k=req.top_k,
                with_answer=False,
                condition=name,
            )
        results.append({**found, "available": True, "label": CONDITIONS[name]["label"]})

    # 두 조건이 같은 지점을 가리키는지 (초 단위)
    tops = [r["scenes"][0]["start_time"] for r in results if r.get("scenes")]
    agreement = None
    if len(tops) >= 2:
        agreement = {"gap_seconds": round(abs(tops[0] - tops[1]), 1), "same": abs(tops[0] - tops[1]) < 4.0}

    return {"query": req.query, "video_id": req.video_id, "results": results, "agreement": agreement}


# --- 단계 12 · 검색 --------------------------------------------------------

@app.post("/api/search")
def do_search(req: SearchRequest, user_id: str = Depends(current_user)) -> dict[str, Any]:
    ready, reason = embedder.is_ready()
    if not ready:
        raise HTTPException(status_code=503, detail=reason)
    return search_mod.search(
        user_id,
        req.query,
        video_id=req.video_id,
        playlist_id=req.playlist_id,
        top_k=req.top_k,
    )
