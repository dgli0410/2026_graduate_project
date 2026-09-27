"""저장된 영상 목록을 노션에 붙여넣을 표로 만듭니다 (평가셋 준비용).

    python -m tools.make_video_list

링크를 두 가지로 냅니다. 용도가 달라서 하나로는 안 됩니다.
- 쇼츠 링크 : 영상을 보거나 확장으로 저장할 때. 클릭하면 바로 쇼츠 플레이어로 들어갑니다.
- watch 링크: 정답 라벨링할 때. 쇼츠 페이지는 타임라인에 초가 안 보여 구간을 못 적습니다.

캡처가 끊긴 영상(커버율이 낮은 것)은 질문 대상에서 자동으로 빼고 사유를 적습니다.
그 구간에 답이 있는 질문은 시스템이 찾을 수 없어 점수가 부당하게 깎이기 때문입니다.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from server.config import DB_PATH, MEDIA_DIR, ROOT

MIN_COVERAGE = 85.0  # 캡처 커버율(%)이 이보다 낮으면 질문 대상에서 제외


def coverage(user_id: str, video_id: str, duration: float) -> float:
    """마지막 프레임 시각 / 영상 길이. 저장 도중 끊겼는지 봅니다."""
    frames = sorted((MEDIA_DIR / user_id / video_id / "frames").glob("*.jpg"))
    if not frames or not duration:
        return 0.0
    return int(frames[-1].stem) / 1000 / duration * 100


def main() -> int:
    parser = argparse.ArgumentParser(description="영상 목록 표 만들기")
    parser.add_argument("--out", default=str(ROOT / "eval" / "video_list.md"))
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    videos = list(
        conn.execute(
            "SELECT user_id, video_id, title, duration FROM videos"
            " WHERE status='ready' ORDER BY created_at"
        )
    )
    if not videos:
        print("분석이 끝난 영상이 없습니다.")
        return 1

    lines: list[str] = [
        "# 저장 완료 영상 목록 (평가셋 대상)",
        "",
        f"> 노션에 그대로 붙여넣으면 표로 들어갑니다. {date.today()} 기준 · 총 {len(videos)}개",
        ">",
        "> **링크가 두 개인 이유** — 용도가 다릅니다.",
        "> - **쇼츠**: 영상을 보거나 확장으로 저장할 때. 클릭하면 바로 쇼츠 플레이어입니다.",
        "> - **라벨링**: 정답 구간을 적을 때. 쇼츠 페이지는 타임라인에 초가 안 보입니다.",
        ">   (`,` `.` 한 프레임씩 · `j` `l` 10초씩 · `k` 일시정지)",
        ">",
        "> **질문 대상** O 인 영상에만 질문을 만듭니다. X 는 검색 난이도를 유지하는 오답 후보로 남깁니다.",
        "> **질문 작성자 / 라벨러** 칸은 회의에서 정해 채웁니다.",
        "",
        "| # | 제목 | 쇼츠 | 라벨링 | 길이 | 장면 | 질문 대상 | 질문 작성자 | 라벨러 A | 라벨러 B |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    excluded: list[tuple[str, str]] = []
    total_segments = 0
    for index, video in enumerate(videos, start=1):
        vid = video["video_id"]
        segments = conn.execute(
            "SELECT COUNT(*) FROM segments WHERE user_id=? AND video_id=?",
            (video["user_id"], vid),
        ).fetchone()[0]
        total_segments += segments

        cov = coverage(video["user_id"], vid, video["duration"])
        target = "O"
        if cov < MIN_COVERAGE:
            target = "X"
            excluded.append(
                (
                    vid,
                    f"{video['duration']:.0f}초 영상인데 캡처가 {video['duration'] * cov / 100:.1f}초에서"
                    f" 끊김({cov:.0f}%). 빈 구간의 질문은 시스템이 찾을 수 없어 질문 대상에서 제외."
                    " 색인에는 남겨 오답 후보로 씁니다.",
                )
            )

        title = video["title"].replace("|", "/")[:46]
        lines.append(
            f"| {index} | {title} "
            f"| [쇼츠](https://www.youtube.com/shorts/{vid}) "
            f"| [{vid}](https://www.youtube.com/watch?v={vid}) "
            f"| {video['duration']:.0f}초 | {segments} | {target} |  |  |  |"
        )

    lines += [
        "",
        f"**합계: 영상 {len(videos)}개 · 장면 카드 {total_segments}개"
        f" · 질문 대상 {len(videos) - len(excluded)}개**",
    ]

    if excluded:
        lines += ["", "## 질문 대상에서 제외한 영상", "", "| video_id | 사유 |", "| --- | --- |"]
        lines += [f"| {vid} | {why} |" for vid, why in excluded]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"만들었습니다: {out}")
    print(f"영상 {len(videos)}개 (질문 대상 {len(videos) - len(excluded)}개) · 장면 {total_segments}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
