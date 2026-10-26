# -*- coding: utf-8 -*-
"""영상 1건당 처리 시간·비용 실측 (멘토 자문 4번). 담당 B·D.

    python -m tools.cost_report
    python -m tools.cost_report --condition local   # 다른 조건의 기록
    python -m tools.cost_report --md                # 문서에 붙일 표

**새로 재지 않습니다.** 저장할 때 `analyze.mark()` 가 단계마다 찍어 `condition_runs`
에 남긴 값을 모아 보여줄 뿐입니다. Gemini 호출 수도 `analysis_health.caption` 에
성공·실패까지 들어 있습니다.

---

### 이 파일은 `timings.py` 와 `cost_report.py` 를 합친 것입니다 (2026-10-26)

둘 다 **같은 `timings_json` 을 각자 읽어 각자 평균**을 냈습니다. 핵심 숫자 네 개가
똑같이 나오는데 **비중과 배율이 미묘하게 달랐습니다** — 프레임 단계 비중이 한쪽은
81.2%, 다른 쪽은 82.4% 였습니다.

원인은 **분모**였습니다. `timings.py` 는 DB 의 `total_seconds` 를, `cost_report.py`
는 세 단계를 직접 더한 값을 썼습니다. 둘은 같지 않습니다 — `total_seconds` 에는
단계로 기록되지 않은 자투리(요약 생성, DB 쓰기 등)가 들어 있습니다.

그래서 **감추지 않고 둘 다 보여줍니다.** 단계 합과 전체 시간을 나란히 찍고 그 차이를
"기타(미기록)" 로 명시합니다. 비중은 **전체 시간을 분모로** 통일했습니다 —
"영상 1건을 처리하는 데 걸리는 시간 중 몇 %" 가 자문 4번이 묻는 것이기 때문입니다.

**비용을 돈으로 환산하지 않습니다.** 무료 티어라 지출이 0원이고 단가는 바뀌며
우리가 확인할 수 없습니다. 대신 **호출 수**와, 그 호출 수가 만드는 실질 제약
(**모델당 하루 20회 → 하루 처리 가능 영상 수**)을 적습니다. 서비스화 때 부딪히는
것이 돈이 아니라 처리량이라는 게 이 프로젝트에서 실제로 일어난 일입니다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from typing import Any

from server.config import DB_PATH, MAX_GEMINI_CALLS_PER_VIDEO

FREE_TIER_PER_DAY = 20  # Gemini 무료 티어: 모델당 하루 요청 수
STAGES = [
    ("transcribe", "음성 받아적기"),
    ("frames", "화면 글자 + 장면 설명"),
    ("embedding", "좌표 만들기"),
]


def rows(condition: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    found = conn.execute(
        """
        SELECT v.video_id, v.title, v.duration, v.status, v.summary_json,
               r.timings_json, r.counts_json, r.total_seconds
          FROM videos v
          LEFT JOIN condition_runs r
            ON r.user_id = v.user_id AND r.video_id = v.video_id AND r.condition = ?
         ORDER BY v.created_at
        """,
        (condition,),
    ).fetchall()
    conn.close()

    out = []
    for r in found:
        if not r["timings_json"]:
            continue
        timings = json.loads(r["timings_json"])
        counts = json.loads(r["counts_json"]) if r["counts_json"] else {}
        summary = json.loads(r["summary_json"] or "{}")
        cap = (summary.get("analysis_health") or {}).get("caption") or {}
        staged = sum(float(timings.get(k, 0)) for k, _ in STAGES)
        total = float(r["total_seconds"] or 0) or staged
        out.append({
            "video_id": r["video_id"], "title": r["title"] or "",
            "duration": float(r["duration"] or 0),
            "timings": timings, "staged": round(staged, 2), "total": round(total, 2),
            "frames": counts.get("frames", 0), "segments": counts.get("segments", 0),
            "ok_calls": int(cap.get("successful_calls", 0)),
            "failed_calls": int(cap.get("failed_calls", 0)),
            "capped": int(cap.get("capped_calls", 0)),
        })
    return out


def per_video(data: list[dict], markdown: bool) -> None:
    head = ["영상", "길이", "프레임", "음성", "화면", "좌표", "합계", "배율"]
    if markdown:
        print("| " + " | ".join(head) + " |")
        print("|" + "---|" * len(head))
        for d in data:
            t = d["timings"]
            ratio = d["total"] / d["duration"] if d["duration"] else 0
            print(f"| {d['title'][:24]} | {d['duration']:.0f}s | {d['frames']} | "
                  f"{t.get('transcribe', 0):.1f}s | {t.get('frames', 0):.1f}s | "
                  f"{t.get('embedding', 0):.1f}s | {d['total']:.1f}s | {ratio:.1f}x |")
        return

    print(f"{'영상':<24}{'길이':>7}{'프레임':>7}{'음성':>8}{'화면':>9}{'좌표':>8}"
          f"{'합계':>9}{'배율':>7}")
    print("-" * 88)
    for d in data:
        t = d["timings"]
        ratio = d["total"] / d["duration"] if d["duration"] else 0
        print(f"{d['title'][:22]:<24}{d['duration']:6.1f}s{d['frames']:7d}"
              f"{t.get('transcribe', 0):7.1f}s{t.get('frames', 0):8.1f}s"
              f"{t.get('embedding', 0):7.1f}s{d['total']:8.1f}s{ratio:6.1f}x")


def summary(data: list[dict]) -> None:
    n = len(data)
    dur = [d["duration"] for d in data]
    tot = [d["total"] for d in data]
    sta = [d["staged"] for d in data]
    sum_total = sum(tot)

    print("-" * 88)
    print(f"{'평균 (영상 ' + str(n) + '개)':<24}{statistics.mean(dur):6.1f}s"
          f"{'':7}{statistics.mean([d['timings'].get('transcribe', 0) for d in data]):7.1f}s"
          f"{statistics.mean([d['timings'].get('frames', 0) for d in data]):8.1f}s"
          f"{statistics.mean([d['timings'].get('embedding', 0) for d in data]):7.1f}s"
          f"{statistics.mean(tot):8.1f}s{sum_total / sum(dur):6.1f}x")

    print(f"\n영상 {n}개")
    print(f"  영상 길이     평균 {statistics.mean(dur):6.1f}초"
          f"  (최소 {min(dur):.0f} / 최대 {max(dur):.0f})")
    print(f"  서버 처리     평균 {statistics.mean(tot):6.1f}초"
          f"  (최소 {min(tot):.1f} / 최대 {max(tot):.1f})")

    print("\n단계별 비중  (분모 = 전체 처리 시간)")
    for key, label in STAGES:
        vals = [d["timings"].get(key, 0) for d in data]
        share = sum(vals) / sum_total * 100 if sum_total else 0
        print(f"  {label:<22}{statistics.mean(vals):7.1f}초  {share:5.1f}%"
              f"  {'#' * int(share / 2)}")
    other = sum_total - sum(sta)
    print(f"  {'기타 (미기록)':<22}{other / n:7.1f}초  {other / sum_total * 100:5.1f}%"
          f"  {'#' * int(other / sum_total * 50)}")
    print("    ↳ 단계로 기록되지 않은 자투리(요약 생성·DB 쓰기 등)입니다.")

    print(f"\n  영상 길이의 평균 {sum_total / sum(dur):.1f}배가 걸립니다.")
    cap_total = sum(dur)  # 캡처는 실시간 1배속이라 영상 길이와 같습니다
    print(f"  사용자가 기다리는 시간 = 캡처(≈영상 길이) + 서버 처리")
    print(f"    평균 {statistics.mean(dur):.1f} + {statistics.mean(tot):.1f}"
          f" = {statistics.mean(dur) + statistics.mean(tot):.1f}초")
    print(f"    캡처가 전체의 {cap_total / (cap_total + sum_total) * 100:.0f}% 를 차지합니다")

    ok = sum(d["ok_calls"] for d in data)
    failed = sum(d["failed_calls"] for d in data)
    capped = sum(d["capped"] for d in data)
    per_day = FREE_TIER_PER_DAY // max(1, MAX_GEMINI_CALLS_PER_VIDEO)
    print(f"\n  Gemini 호출(장면 설명)  성공 {ok}회 / 실패 {failed}회"
          f" / 상한에 막힘 {capped}회")
    print(f"    코드가 영상당 {MAX_GEMINI_CALLS_PER_VIDEO}회로 막고 있습니다"
          f" (config.MAX_GEMINI_CALLS_PER_VIDEO)")
    print(f"    무료 티어는 모델당 하루 {FREE_TIER_PER_DAY}회 → 하루에 {per_day}개 영상")
    print(f"\n  실제 지출: 0원 (무료 티어). 제약은 돈이 아니라 **하루 처리량** 입니다.")
    print("\n⚠ 영상 2개를 동시에 처리하므로(worker.CONCURRENCY=2) 각 영상의 시간에는")
    print("  다른 영상과 CPU 를 나눠 쓴 몫이 섞여 있습니다. 단독 처리보다 느리게 나옵니다.")


def main() -> int:
    p = argparse.ArgumentParser(description="영상 1건당 처리 시간·비용 실측 (자문 4)")
    p.add_argument("--condition", default="gemini", help="집계할 조건 (기본 gemini)")
    p.add_argument("--md", action="store_true", help="문서에 붙일 마크다운 표로")
    args = p.parse_args()

    data = rows(args.condition)
    if not data:
        print("아직 기록이 없습니다. 분석이 끝난 영상이 있어야 합니다.")
        return 1

    print(f"조건: {args.condition}")
    print("=" * 88)
    per_video(data, args.md)
    if not args.md:
        summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
