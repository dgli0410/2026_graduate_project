"""저장된 영상을 다시 분석시킨다. 확장프로그램에서 다시 저장할 필요가 없다.

프레임과 오디오는 data/media 에 그대로 있으므로, 상태만 되돌리면
서버가 시작하면서 자동으로 큐에 넣는다(main.py 시작 루틴).

백엔드를 바꿨을 때(.env 수정) 기존 영상에 반영하는 용도다.

사용:
    python -m tools.requeue              모든 영상을 다시 분석하도록 표시
    python -m tools.requeue _pbcDuN2G2I  한 영상만
    python -m tools.requeue --status     지금 진행 상황만 본다

실행 후 서버를 껐다 켜면 분석이 다시 돈다.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "vault.db"


STAGE = {
    "queued": "대기 중",
    "transcribing": "1/5 음성 받아적기",
    "analyzing_frames": "2/5 화면 글자 + 장면 설명",
    "embedding": "3/5 좌표 만들기  ← SigLIP 이 여기서 돕니다",
    "summarizing": "4/5 전체 요약",
    "ready": "완료",
    "failed": "실패",
}


def show_status() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT video_id, title, status, error FROM videos").fetchall()
    for r in rows:
        label = STAGE.get(r["status"], r["status"])
        print(f"  {r['status']:18s} {label:32s} [{r['title'][:30]}]")
        if r["error"]:
            print(f"      오류: {r['error']}")
    n = sum(1 for r in rows if r["status"] == "ready")
    print()
    print(f"완료 {n}/{len(rows)}")


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"{DB} 가 없습니다.")

    if "--status" in sys.argv:
        show_status()
        return

    target = sys.argv[1] if len(sys.argv) > 1 else None
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = con.execute("SELECT user_id, video_id, title, status FROM videos").fetchall()
    if not rows:
        raise SystemExit("저장된 영상이 없습니다.")

    changed = 0
    for r in rows:
        if target and r["video_id"] != target:
            continue
        frames = ROOT / "data" / "media" / r["user_id"] / r["video_id"] / "frames"
        n = len(list(frames.glob("*"))) if frames.is_dir() else 0
        if n == 0:
            print(f"  건너뜀  {r['video_id']}  프레임 없음 — 확장에서 다시 저장해야 합니다")
            continue
        con.execute(
            "UPDATE videos SET status='queued' WHERE user_id=? AND video_id=?",
            (r["user_id"], r["video_id"]),
        )
        changed += 1
        print(f"  큐 등록  {r['video_id']}  [{r['title'][:30]}]  프레임 {n}장")

    con.commit()
    print(f"\n{changed}개를 다시 분석하도록 표시했습니다.")
    print("서버를 껐다 켜면 시작하면서 자동으로 돌아갑니다.\n")
    print("    python -m uvicorn server.main:app --port 8000")


if __name__ == "__main__":
    main()
