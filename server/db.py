"""SQLite 저장소.

마스터 문서의 PostgreSQL 자리입니다. B 담당이 나중에 갈아끼울 때
이 파일의 함수 시그니처만 유지하면 나머지 코드는 건드릴 필요가 없습니다.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from server.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    user_id      TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    channel      TEXT NOT NULL DEFAULT '',
    duration     REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued',
    progress     INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (user_id, video_id)
);

CREATE TABLE IF NOT EXISTS segments (
    user_id    TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time   REAL NOT NULL,
    asr_text   TEXT NOT NULL DEFAULT '',
    ocr_text   TEXT NOT NULL DEFAULT '',
    caption    TEXT NOT NULL DEFAULT '',
    frame_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, segment_id)
);
CREATE INDEX IF NOT EXISTS idx_segments_video ON segments(user_id, video_id, start_time);

CREATE TABLE IF NOT EXISTS playlists (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id, created_at);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    added_at    REAL NOT NULL,
    PRIMARY KEY (playlist_id, video_id)
);
"""

# 진행 상태 -> (사용자에게 보이는 글, 진행률). 마스터 문서 단계 10 표와 동일합니다.
STATUS_LABELS: dict[str, tuple[str, int]] = {
    "queued": ("분석 대기 중", 0),
    "capturing": ("영상에서 화면·소리 추출 중", 20),
    "transcribing": ("음성 분석 중", 40),
    "analyzing_frames": ("화면 글자·장면 분석 중", 65),
    "embedding": ("검색 데이터 생성 중", 85),
    "summarizing": ("요약 생성 중", 95),
    "ready": ("분석 완료", 100),
    "failed": ("분석 실패", 0),
}
TERMINAL_STATUSES = ("ready", "failed")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """요청마다 연결을 열고 반드시 닫습니다."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)


# --- videos ---------------------------------------------------------------

def upsert_video(user_id: str, video_id: str, title: str, channel: str, duration: float) -> bool:
    """새로 넣었으면 True, 이미 있으면 False. 마스터 문서 단계 3 의 중복 확인입니다."""
    now = time.time()
    capturing_label, capturing_progress = STATUS_LABELS["capturing"]
    with session() as conn:
        cur = conn.execute(
            "SELECT status FROM videos WHERE user_id=? AND video_id=?", (user_id, video_id)
        )
        row = cur.fetchone()
        if row is not None:
            # 실패한 건 다시 시도할 수 있게 허용합니다.
            if row["status"] != "failed":
                return False
            conn.execute(
                "UPDATE videos SET status='capturing', progress=?, error='', updated_at=? "
                "WHERE user_id=? AND video_id=?",
                (capturing_progress, now, user_id, video_id),
            )
            return True
        conn.execute(
            "INSERT INTO videos (user_id, video_id, title, channel, duration, status, progress,"
            " error, summary_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,'capturing',?,'','',?,?)",
            (user_id, video_id, title, channel, duration, capturing_progress, now, now),
        )
    return True


def set_status(user_id: str, video_id: str, status: str, error: str = "") -> None:
    _, progress = STATUS_LABELS.get(status, ("", 0))
    with session() as conn:
        conn.execute(
            "UPDATE videos SET status=?, progress=?, error=?, updated_at=? WHERE user_id=? AND video_id=?",
            (status, progress, error, time.time(), user_id, video_id),
        )


def set_duration(user_id: str, video_id: str, duration: float) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE videos SET duration=?, updated_at=? WHERE user_id=? AND video_id=?",
            (duration, time.time(), user_id, video_id),
        )


def set_summary(user_id: str, video_id: str, summary: dict[str, Any], duration: float) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE videos SET summary_json=?, duration=?, updated_at=? WHERE user_id=? AND video_id=?",
            (json.dumps(summary, ensure_ascii=False), duration, time.time(), user_id, video_id),
        )


def _video_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    label, _ = STATUS_LABELS.get(row["status"], (row["status"], 0))
    return {
        "video_id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "duration": row["duration"],
        "status": row["status"],
        "status_label": label,
        "progress": row["progress"],
        "error": row["error"],
        "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        "thumbnail": f"https://img.youtube.com/vi/{row['video_id']}/hqdefault.jpg",
        "youtube_url": f"https://www.youtube.com/shorts/{row['video_id']}",
        "created_at": row["created_at"],
    }


def get_video(user_id: str, video_id: str) -> dict[str, Any] | None:
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE user_id=? AND video_id=?", (user_id, video_id)
        ).fetchone()
    return _video_row_to_dict(row) if row else None


def list_videos(user_id: str, playlist_id: str | None = None) -> list[dict[str, Any]]:
    with session() as conn:
        if playlist_id:
            rows = conn.execute(
                "SELECT v.* FROM videos v JOIN playlist_items p"
                " ON v.user_id=p.user_id AND v.video_id=p.video_id"
                " WHERE v.user_id=? AND p.playlist_id=? ORDER BY v.created_at DESC",
                (user_id, playlist_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM videos WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
    return [_video_row_to_dict(r) for r in rows]


def delete_video(user_id: str, video_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM videos WHERE user_id=? AND video_id=?", (user_id, video_id))
        conn.execute("DELETE FROM segments WHERE user_id=? AND video_id=?", (user_id, video_id))
        conn.execute("DELETE FROM playlist_items WHERE user_id=? AND video_id=?", (user_id, video_id))


def pending_video_ids(user_id: str) -> list[str]:
    with session() as conn:
        rows = conn.execute(
            "SELECT video_id FROM videos WHERE user_id=? AND status NOT IN ('ready','failed')",
            (user_id,),
        ).fetchall()
    return [r["video_id"] for r in rows]


# --- segments -------------------------------------------------------------

def replace_segments(user_id: str, video_id: str, segments: list[dict[str, Any]]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM segments WHERE user_id=? AND video_id=?", (user_id, video_id))
        conn.executemany(
            "INSERT INTO segments (user_id, video_id, segment_id, start_time, end_time,"
            " asr_text, ocr_text, caption, frame_path) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    user_id,
                    video_id,
                    s["segment_id"],
                    s["start_time"],
                    s["end_time"],
                    s.get("asr_text", ""),
                    s.get("ocr_text", ""),
                    s.get("caption", ""),
                    s.get("frame_path", ""),
                )
                for s in segments
            ],
        )


def list_segments(user_id: str, video_id: str) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM segments WHERE user_id=? AND video_id=? ORDER BY start_time",
            (user_id, video_id),
        ).fetchall()
    return [dict(r) for r in rows]


# --- playlists ------------------------------------------------------------

def create_playlist(user_id: str, name: str) -> dict[str, Any]:
    playlist_id = uuid.uuid4().hex[:12]
    now = time.time()
    with session() as conn:
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at) VALUES (?,?,?,?)",
            (playlist_id, user_id, name, now),
        )
    return {"id": playlist_id, "name": name, "video_count": 0, "created_at": now}


def list_playlists(user_id: str) -> list[dict[str, Any]]:
    with session() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.created_at, COUNT(i.video_id) AS video_count"
            " FROM playlists p LEFT JOIN playlist_items i ON p.id=i.playlist_id"
            " WHERE p.user_id=? GROUP BY p.id ORDER BY p.created_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_playlist(user_id: str, playlist_id: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM playlists WHERE user_id=? AND id=?", (user_id, playlist_id))
        conn.execute(
            "DELETE FROM playlist_items WHERE user_id=? AND playlist_id=?", (user_id, playlist_id)
        )


def add_to_playlist(user_id: str, playlist_id: str, video_id: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO playlist_items (playlist_id, user_id, video_id, added_at)"
            " VALUES (?,?,?,?)",
            (playlist_id, user_id, video_id, time.time()),
        )


def remove_from_playlist(user_id: str, playlist_id: str, video_id: str) -> None:
    with session() as conn:
        conn.execute(
            "DELETE FROM playlist_items WHERE user_id=? AND playlist_id=? AND video_id=?",
            (user_id, playlist_id, video_id),
        )


def playlist_video_ids(user_id: str, playlist_id: str) -> list[str]:
    with session() as conn:
        rows = conn.execute(
            "SELECT video_id FROM playlist_items WHERE user_id=? AND playlist_id=?",
            (user_id, playlist_id),
        ).fetchall()
    return [r["video_id"] for r in rows]
