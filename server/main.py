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

from server import db, embedder, image_embedder, media, search as search_mod, shopping, vectors
from server.config import (
    ASR_BACKEND,
    CAPTION_MODEL,
    DEVICE,
    IMAGE_EMBED_BACKEND,
    MAX_GEMINI_CALLS_PER_VIDEO,
    OCR_BACKEND,
    SEARCH_TOP_K,
    SEGMENT_SECONDS,
    TEXT_EMBED_BACKEND,
    capture_settings,
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


# --- 상태 -----------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    ready, reason = embedder.is_ready()
    return {
        "ok": ready,
        "reason": reason,
        "device": DEVICE,
        "backends": {
            "asr": ASR_BACKEND,
            "ocr": OCR_BACKEND,
            "text_embed": TEXT_EMBED_BACKEND,
            "image_embed": IMAGE_EMBED_BACKEND,
            "caption_model": CAPTION_MODEL,
        },
        "capture": capture_settings(),
        "segment_seconds": SEGMENT_SECONDS,
        "max_gemini_calls_per_video": MAX_GEMINI_CALLS_PER_VIDEO,
        "image_search_enabled": image_embedder.is_enabled(),
        "shopping_price_enabled": shopping.is_enabled(),
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
    return {"pending": db.pending_video_ids(user_id)}


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
    vectors.delete_video(user_id, video_id)
    media.clear(user_id, video_id)
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


# --- 재료 → 구매 링크 (고급: 네이버쇼핑 API 가격) ---------------------------

@app.get("/api/shopping")
def shopping_search(query: str = "", user_id: str = Depends(current_user)) -> dict[str, Any]:
    """재료 이름으로 최저가를 찾아줍니다. 키가 없으면 503 — 확장은 링크만 보여줍니다."""
    if not shopping.is_enabled():
        raise HTTPException(status_code=503, detail="NAVER_CLIENT_ID/SECRET 이 설정되지 않았습니다.")
    if not query.strip():
        raise HTTPException(status_code=400, detail="query 가 비어 있습니다.")
    try:
        items = shopping.search(query)
    except Exception as exc:  # noqa: BLE001 - 외부 API 실패가 화면을 깨면 안 됩니다
        raise HTTPException(status_code=502, detail=f"네이버쇼핑 API 오류: {exc}") from exc
    return {"query": query, "items": items}


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
