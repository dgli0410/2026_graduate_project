"""저장된 영상 전부를 특정 조건으로 재분석합니다 (배치).

확정본 9주차 실험용입니다. 영상 50개를 로컬 모델로 돌리면 CPU 기준 몇 시간이
걸리므로, 밤에 걸어두고 자면 됩니다.

서버가 떠 있어야 합니다(로컬 Qdrant 는 프로세스 하나만 붙을 수 있어서,
이 도구는 직접 붙지 않고 서버 API 를 씁니다).

    python -m tools.run_condition --condition local
    python -m tools.run_condition --condition local --video _pbcDuN2G2I
    python -m tools.run_condition --list
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from server.config import DB_PATH

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, user_id: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-User-Id": user_id}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def ready_videos() -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT user_id, video_id, title FROM videos WHERE status='ready' ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [(r["user_id"], r["video_id"], r["title"]) for r in rows]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="조건별 배치 재분석")
    parser.add_argument("--condition", default="local", help="돌릴 조건 이름 (기본 local)")
    parser.add_argument("--video", default="", help="영상 하나만 돌릴 때 video_id")
    parser.add_argument("--list", action="store_true", help="조건과 영상 목록만 보기")
    parser.add_argument("--poll", type=float, default=10.0, help="상태 확인 간격(초)")
    args = parser.parse_args(argv)

    videos = ready_videos()
    if not videos:
        print("분석 완료된 영상이 없습니다. 먼저 확장프로그램으로 저장하세요.")
        return 1
    if args.video:
        videos = [v for v in videos if v[1] == args.video]
        if not videos:
            print(f"{args.video} 를 찾지 못했습니다.")
            return 1

    user_id = videos[0][0]
    status, info = call("GET", "/api/conditions", user_id)
    if status != 200:
        print("서버에 연결하지 못했습니다. 먼저 실행하세요:")
        print("  python -m uvicorn server.main:app --port 8000")
        return 1

    if args.list:
        print("조건:")
        for c in info["conditions"]:
            mark = "✅" if c["ready"] else "❌"
            print(f"  {mark} {c['name']:8} {c['label']}  →  {c['collection']}")
            if c["missing"]:
                print(f"       {c['install_hint']}")
        print(f"\n영상 {len(videos)}개:")
        for _, video_id, title in videos:
            print(f"  {video_id:14} {title[:44]}")
        return 0

    target = next((c for c in info["conditions"] if c["name"] == args.condition), None)
    if target is None:
        print(f"모르는 조건입니다: {args.condition}")
        return 1
    if not target["ready"]:
        print(f"필요한 패키지가 없습니다.\n  {target['install_hint']}")
        return 1

    print(f"조건 '{args.condition}' 으로 영상 {len(videos)}개를 재분석합니다.")
    print("원본 프레임/오디오는 그대로 두고, 결과만 따로 저장합니다.\n")

    queued = []
    for user, video_id, title in videos:
        code, result = call("POST", f"/api/videos/{video_id}/conditions/{args.condition}", user)
        if code == 200:
            queued.append((user, video_id, title))
            print(f"  대기열 등록: {title[:44]}")
        else:
            print(f"  ❌ {title[:38]}: {result.get('detail')}")

    if not queued:
        return 1

    print(f"\n{len(queued)}개 처리 중... ({args.poll}초마다 확인, Ctrl+C 로 나가도 서버는 계속 돕니다)")
    started = time.time()
    done: set[str] = set()
    while len(done) < len(queued):
        time.sleep(args.poll)
        for user, video_id, title in queued:
            if video_id in done:
                continue
            _, detail = call("GET", f"/api/videos/{video_id}/conditions", user)
            run = next(
                (r for r in detail.get("runs", []) if r.get("name") == args.condition), {}
            )
            status_name = run.get("status")
            if status_name == "ready":
                done.add(video_id)
                counts = run.get("counts", {})
                print(
                    f"  ✅ {title[:34]:34} 장면 {counts.get('segments','?')}장 "
                    f"/ {run.get('total_seconds','?')}초"
                )
            elif status_name == "failed":
                done.add(video_id)
                print(f"  ❌ {title[:34]:34} {run.get('error','')[:60]}")

    print(f"\n완료. 총 {round(time.time() - started, 1)}초")
    print(f"확장프로그램 '⚗ 비교' 탭에서 결과를 보세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
