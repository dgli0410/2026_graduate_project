"""평가셋에 1점(관련) 구간을 규칙으로 채운다. 담당 D.

    python -m tools.add_context_grade1            # 계획만
    python -m tools.add_context_grade1 --yes      # 실제로 추가

**왜 필요한가** — 사람이 단 라벨은 2점(정확)뿐이라 질문당 정답 구간이 1개였다. 그러면
Recall@5 가 전부 1.000 이 되어 변별력이 사라지고, 등급형 라벨을 전제로 하는 nDCG 가
사실상 이진 판정으로 퇴화한다. 멘토가 2/1/0 등급을 요구한 이유가 이것이다.

**규칙** — 2점 구간 바로 앞 4초를 1점(맥락)으로 둔다.

    [s-4, s]  ->  grade 1

근거는 라벨러(임준서)가 시트에 직접 남긴 메모다.

    "순서, 타이밍 관련 쿼리는 앞 과정의 맥락도 어느정도 필요한 것 같음. 여기서는 물을
     넣고 오징어를 넣는 순서라 물 넣는 맥락도 함께 보여줘야 정답으로 해야할듯"

**내용을 보고 판단하지 않는다.** 장면이 실제로 관련 있는지를 기계가 판정하면 그것은
사람이 만든 정답이 아니게 되고, 평가가 시스템을 평가하는 게 아니라 자기 자신을 평가하는
꼴이 된다. 그래서 순수하게 시간 기준으로만 넣고, `source` 칸에 `규칙생성` 으로 표시해
사람 라벨과 구분한다. 보고서에는 이 규칙을 그대로 적으면 된다.

건너뛰는 경우:
- 정답이 없는 질문(grade=없음) — 맥락도 없다
- 앞 구간 길이가 1초 미만 (2점 구간이 영상 맨 앞에 있을 때)
- 그 앞 구간이 같은 질문의 다른 2점 구간과 겹칠 때 — 2점을 1점으로 덮어쓰면 안 된다
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

from server.config import ROOT

CONTEXT = 4.0   # 2점 구간 앞으로 몇 초를 맥락으로 볼지
MIN_LEN = 1.0   # 이보다 짧으면 넣지 않는다
MARK = "규칙생성"


def main() -> int:
    p = argparse.ArgumentParser(description="1점(관련) 구간을 규칙으로 추가")
    p.add_argument("--labels", default=str(ROOT / "eval" / "labels_merged.csv"))
    p.add_argument("--yes", action="store_true", help="실제로 파일에 씁니다")
    args = p.parse_args()

    path = Path(args.labels)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if any(r.get("source") == MARK for r in rows):
        print(f"이미 {MARK} 줄이 있습니다. 중복 추가를 막기 위해 중단합니다.")
        print("다시 만들려면 그 줄들을 먼저 지우세요.")
        return 1

    # 질문별로 2점 구간과 전체 구간을 모은다
    by_q: dict[str, list[dict]] = {}
    for r in rows:
        by_q.setdefault(r["qid"], []).append(r)

    added: list[dict] = []
    skipped: list[str] = []
    for qid, group in by_q.items():
        spans = [(float(r["start"]), float(r["end"]), r["grade"])
                 for r in group if r["start"].strip() and r["end"].strip()]
        if not spans:
            continue  # 정답 없는 질문
        head = group[0]
        for start, _end, grade in spans:
            if grade != "2":
                continue
            c_start, c_end = max(0.0, start - CONTEXT), start
            if c_end - c_start < MIN_LEN:
                skipped.append(f"{qid} {head['question'][:20]} — 앞 구간이 {c_end-c_start:.1f}초")
                continue
            if any(s < c_end and e > c_start for s, e, _ in spans):
                skipped.append(f"{qid} {head['question'][:20]} — 다른 정답 구간과 겹침")
                continue
            added.append({
                "qid": qid, "video_id": head["video_id"], "question": head["question"],
                "q_type": head["q_type"], "start": f"{c_start:.1f}", "end": f"{c_end:.1f}",
                "grade": "1", "labeler": head["labeler"], "source": MARK,
            })

    print(f"2점 구간에서 뽑은 1점(맥락) 구간: {len(added)}개")
    print(f"건너뜀: {len(skipped)}개")
    for s in skipped[:6]:
        print(f"  {s}")
    if len(skipped) > 6:
        print(f"  … 외 {len(skipped)-6}개")

    if not args.yes:
        print("\n계획만 보여줬습니다. 실제로 넣으려면 --yes 를 붙이세요.")
        return 0

    shutil.copy(path, path.with_suffix(".csv.bak"))
    fields = ["qid", "video_id", "question", "q_type", "start", "end", "grade", "labeler", "source"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows + added:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\n{len(rows)} -> {len(rows)+len(added)}줄. 백업: {path.name}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
