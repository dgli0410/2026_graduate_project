"""재료 필드 백필 — ingredients 기능 추가 전에 분석된 영상용.

다시 저장(실시간 재생 60초)할 필요 없이, 이미 저장된 장면 카드로
요약을 다시 만들어 ingredients 만 기존 요약에 채워 넣습니다.
영상 1개당 Gemini 1회 호출. 이미 재료가 있는 영상은 건너뜁니다.

사용법:  python -m tools.backfill_ingredients
"""
from __future__ import annotations

import json

from server import db
from server.pipeline.summarizer import summarize


def main() -> None:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT user_id, video_id, title, duration, summary_json FROM videos WHERE status='ready'"
        ).fetchall()

    if not rows:
        print("분석 완료(ready)된 영상이 없습니다.")
        return

    for row in rows:
        user_id, video_id, title = row["user_id"], row["video_id"], row["title"] or ""
        summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
        if summary.get("ingredients"):
            print(f"[건너뜀] {video_id} — 재료 {len(summary['ingredients'])}개 이미 있음")
            continue

        segments = db.list_segments(user_id, video_id)
        if not segments:
            print(f"[건너뜀] {video_id} — 장면 카드가 없습니다")
            continue

        fresh = summarize(title, segments)
        if fresh.get("degraded"):
            print(f"[실패]   {video_id} — Gemini 요약 실패. 나중에 다시 실행해 보세요.")
            continue

        # 화면이 이미 쓰고 있는 headline/steps 는 그대로 두고 재료만 채웁니다.
        summary["ingredients"] = fresh.get("ingredients", [])
        db.set_summary(user_id, video_id, summary, row["duration"])
        print(f"[완료]   {video_id} — 재료: {', '.join(summary['ingredients']) or '(없음)'}")


if __name__ == "__main__":
    main()
