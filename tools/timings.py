"""영상별 처리 시간을 단계별로 봅니다.

멘토 자문 4번(영상 1건당 처리 시간·비용 실측)에 쓸 숫자입니다.
분석이 끝난 영상(status=ready)만 집계합니다.

사용:
    python -m tools.timings
    python -m tools.timings --condition local   다른 조건의 기록
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "vault.db"

# timings_json 의 열쇠 -> 사람이 읽는 이름. analyze.mark() 가 적는 값입니다.
STAGES = [
    ("transcribe", "음성 받아적기"),
    ("frames", "화면 글자 + 장면 설명"),
    ("embedding", "좌표 만들기"),
]


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"{DB} 가 없습니다.")
    condition = "gemini"
    if "--condition" in sys.argv:
        condition = sys.argv[sys.argv.index("--condition") + 1]

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT v.video_id, v.title, v.duration, v.status,
               r.timings_json, r.counts_json, r.total_seconds
          FROM videos v
          LEFT JOIN condition_runs r
            ON r.user_id = v.user_id AND r.video_id = v.video_id AND r.condition = ?
         ORDER BY v.rowid
        """,
        (condition,),
    ).fetchall()

    done = [r for r in rows if r["timings_json"]]
    if not done:
        pending = [r["title"][:22] for r in rows if r["status"] != "ready"]
        raise SystemExit(
            "아직 기록이 없습니다. 분석이 끝나야 남습니다."
            + (f"\n진행 중: {', '.join(pending)}" if pending else "")
        )

    print(f"조건: {condition}\n" + "=" * 86)
    print(f"{'영상':<24}{'길이':>7}{'프레임':>6}{'음성':>8}{'화면':>9}{'좌표':>8}{'합계':>9}{'배율':>7}")
    print("-" * 86)

    totals = {k: 0.0 for k, _ in STAGES}
    total_all = total_len = 0.0
    for r in done:
        t = json.loads(r["timings_json"])
        counts = json.loads(r["counts_json"]) if r["counts_json"] else {}
        frames = counts.get("frames", 0)
        length = float(r["duration"] or 0)
        total = float(r["total_seconds"] or 0)
        # 배율 = 처리 시간 / 영상 길이. 1.0 이면 실시간, 10 이면 10배 더 걸린다는 뜻.
        ratio = total / length if length else 0
        for k, _ in STAGES:
            totals[k] += float(t.get(k, 0))
        total_all += total
        total_len += length
        print(
            f"{r['title'][:22]:<24}{length:6.1f}s{frames:6d}"
            f"{t.get('transcribe', 0):7.1f}s{t.get('frames', 0):8.1f}s"
            f"{t.get('embedding', 0):7.1f}s{total:8.1f}s{ratio:6.1f}x"
        )

    n = len(done)
    print("-" * 86)
    print(f"{'평균 (영상 ' + str(n) + '개)':<24}{total_len/n:6.1f}s{'':6}"
          f"{totals['transcribe']/n:7.1f}s{totals['frames']/n:8.1f}s"
          f"{totals['embedding']/n:7.1f}s{total_all/n:8.1f}s"
          f"{(total_all/total_len if total_len else 0):6.1f}x")

    print("\n단계별 비중")
    for k, label in STAGES:
        share = totals[k] / total_all * 100 if total_all else 0
        bar = "#" * int(share / 2)
        print(f"  {label:<22}{totals[k]/n:7.1f}s  {share:5.1f}%  {bar}")

    print(f"\n영상 길이의 평균 {total_all/total_len:.1f}배가 걸립니다."
          if total_len else "")
    print("⚠ 영상 2개를 동시에 처리하므로(CONCURRENCY=2) 각 영상의 시간에는"
          "\n  다른 영상과 CPU 를 나눠 쓴 몫이 섞여 있습니다. 단독 처리보다 느리게 나옵니다.")


if __name__ == "__main__":
    main()
