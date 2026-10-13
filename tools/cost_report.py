"""영상 1건당 처리 시간·비용 실측 (멘토 자문 4번). 담당 D.

새로 재지 않습니다. **저장할 때 이미 쌓인 값**을 모아 표로 만들 뿐입니다 —
`analyze.py` 가 단계마다 `timings` 를 찍고 `condition_runs` 에 저장하고 있었는데
집계하는 도구가 없었습니다. Gemini 호출 수도 `analysis_health.caption` 에
성공·실패·상한에 막힌 수까지 들어 있습니다.

    python -m tools.cost_report
    python -m tools.cost_report --md      # 문서에 붙일 표 형식

**비용을 돈으로 환산하지 않습니다.** 무료 티어라 실제 지출이 0원이고, 단가는 바뀌며
우리가 확인할 수 없습니다. 대신 **호출 수**와, 그 호출 수가 만드는 실질 제약
(**모델당 하루 20회 → 하루에 처리 가능한 영상 수**)을 적습니다. 서비스화 때 부딪히는
것이 돈이 아니라 처리량이라는 게 이 프로젝트에서 실제로 일어난 일입니다.

사용자가 체감하는 시간은 **확장이 영상을 재생하며 캡처하는 시간(≈ 영상 길이)** 과
**서버 처리 시간**의 합입니다. 캡처가 실시간 1배속이라 보통 캡처 쪽이 더 깁니다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from typing import Any

from server.config import DB_PATH, MAX_GEMINI_CALLS_PER_VIDEO

FREE_TIER_PER_DAY = 20  # Gemini 무료 티어: 모델당 하루 요청 수
STAGES = (("transcribe", "음성"), ("frames", "프레임(자막·설명)"), ("embedding", "좌표"))


def rows() -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = []
    for r in conn.execute(
        "SELECT video_id, title, duration, summary_json FROM videos"
        " WHERE status='ready' ORDER BY created_at"
    ):
        summary = json.loads(r["summary_json"] or "{}")
        timings = summary.get("timings") or {}
        if not timings:
            continue
        cap = (summary.get("analysis_health") or {}).get("caption") or {}
        out.append({
            "video_id": r["video_id"],
            "title": r["title"] or "",
            "duration": float(r["duration"] or 0),
            "timings": timings,
            "server_total": round(sum(timings.values()), 2),
            "segments": (summary.get("counts") or {}).get("segments", 0),
            "ok_calls": int(cap.get("successful_calls", 0)),
            "failed_calls": int(cap.get("failed_calls", 0)),
            "capped": int(cap.get("capped_calls", 0)),
        })
    return out


def show(data: list[dict], markdown: bool) -> None:
    sep = " | " if markdown else "  "
    head = ["video_id", "영상(초)", "음성", "프레임", "좌표", "서버합", "배속", "장면", "호출"]
    if markdown:
        print("| " + " | ".join(head) + " |")
        print("|" + "---|" * len(head))
    else:
        print(sep.join(f"{h:>9}" for h in head))

    for d in data:
        t = d["timings"]
        ratio = d["server_total"] / d["duration"] if d["duration"] else 0
        cells = [
            d["video_id"], f"{d['duration']:.0f}",
            f"{t.get('transcribe', 0):.1f}", f"{t.get('frames', 0):.1f}",
            f"{t.get('embedding', 0):.1f}", f"{d['server_total']:.1f}",
            f"{ratio:.2f}x", str(d["segments"]), str(d["ok_calls"]),
        ]
        print(("| " + " | ".join(cells) + " |") if markdown else sep.join(f"{c:>9}" for c in cells))

    print()
    dur = [d["duration"] for d in data]
    tot = [d["server_total"] for d in data]
    print(f"영상 {len(data)}개")
    print(f"  영상 길이      평균 {statistics.mean(dur):.1f}초  "
          f"(최소 {min(dur):.0f} / 최대 {max(dur):.0f})")
    print(f"  서버 처리      평균 {statistics.mean(tot):.1f}초  "
          f"(최소 {min(tot):.1f} / 최대 {max(tot):.1f})")
    for key, label in STAGES:
        vals = [d["timings"].get(key, 0) for d in data]
        share = sum(vals) / sum(tot) * 100 if sum(tot) else 0
        print(f"    {label:<14} 평균 {statistics.mean(vals):>6.1f}초   전체의 {share:>4.1f}%")

    cap_total = sum(dur)  # 캡처는 실시간 1배속이라 영상 길이와 같습니다
    print(f"\n  사용자가 기다리는 시간 = 캡처(≈영상 길이) + 서버 처리")
    print(f"    평균 {statistics.mean(dur):.1f} + {statistics.mean(tot):.1f}"
          f" = {statistics.mean(dur) + statistics.mean(tot):.1f}초")
    print(f"    캡처가 전체의 {cap_total / (cap_total + sum(tot)) * 100:.0f}% 를 차지합니다")

    ok = sum(d["ok_calls"] for d in data)
    failed = sum(d["failed_calls"] for d in data)
    capped = sum(d["capped"] for d in data)
    print(f"\n  Gemini 호출(장면 설명)  성공 {ok}회 / 실패 {failed}회"
          f" / 상한에 막힘 {capped}회")
    print(f"    코드가 영상당 {MAX_GEMINI_CALLS_PER_VIDEO}회로 막고 있습니다"
          f" (config.MAX_GEMINI_CALLS_PER_VIDEO)")
    print(f"    무료 티어는 모델당 하루 {FREE_TIER_PER_DAY}회"
          f" → 하루에 {FREE_TIER_PER_DAY // max(1, MAX_GEMINI_CALLS_PER_VIDEO)}개 영상")
    print(f"    상한을 풀면(격자 전부 설명) 영상당 호출이 늘어 하루 처리량이 그만큼 줄어듭니다")
    print(f"\n  실제 지출: 0원 (무료 티어). 제약은 돈이 아니라 **하루 처리량** 입니다.")


def main() -> int:
    p = argparse.ArgumentParser(description="영상 1건당 처리 시간·비용 실측 (자문 4)")
    p.add_argument("--md", action="store_true", help="문서에 붙일 마크다운 표로")
    args = p.parse_args()

    data = rows()
    if not data:
        print("측정값이 있는 영상이 없습니다.")
        return 1
    show(data, args.md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
