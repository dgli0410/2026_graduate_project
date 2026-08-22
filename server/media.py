"""확장프로그램이 올려준 프레임/오디오 파일 보관.

마스터 문서 단계 11 "삭제": 표(DB) · 창고(Qdrant) · **이미지 파일** 3곳을
모두 지워야 합니다. 이 파일이 그 세 번째입니다.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from server.config import MEDIA_DIR

_SAFE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe(value: str) -> str:
    return _SAFE.sub("_", value)[:64]


def video_dir(user_id: str, video_id: str) -> Path:
    return MEDIA_DIR / _safe(user_id) / _safe(video_id)


def frames_dir(user_id: str, video_id: str) -> Path:
    return video_dir(user_id, video_id) / "frames"


def audio_path(user_id: str, video_id: str) -> Path:
    return video_dir(user_id, video_id) / "audio.webm"


def wav_path(user_id: str, video_id: str) -> Path:
    return video_dir(user_id, video_id) / "audio.wav"


def frame_time(path: Path) -> float:
    """파일 이름이 곧 시각입니다. 000500.jpg = 0.5초"""
    try:
        return int(path.stem) / 1000.0
    except ValueError:
        return 0.0


def frame_name(seconds: float) -> str:
    return f"{int(round(seconds * 1000)):08d}.jpg"


def list_frames(user_id: str, video_id: str) -> list[tuple[float, Path]]:
    directory = frames_dir(user_id, video_id)
    if not directory.exists():
        return []
    frames = [(frame_time(p), p) for p in directory.glob("*.jpg")]
    return sorted(frames, key=lambda item: item[0])


def resolve_frame(user_id: str, video_id: str, name: str) -> Path | None:
    """API 로 프레임 이미지를 내줄 때 경로 탈출을 막습니다."""
    if not re.fullmatch(r"\d{1,12}\.jpg", name):
        return None
    path = frames_dir(user_id, video_id) / name
    return path if path.is_file() else None


def clear(user_id: str, video_id: str) -> None:
    shutil.rmtree(video_dir(user_id, video_id), ignore_errors=True)


def usage_bytes(user_id: str, video_id: str) -> int:
    directory = video_dir(user_id, video_id)
    if not directory.exists():
        return 0
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
