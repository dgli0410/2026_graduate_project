"""평가셋으로 검색 성능을 잰다 (멘토 자문 「평가 방법」 구현). 담당 D.

    python -m tools.eval_search                      # 현행 시스템
    python -m tools.eval_search --baseline title     # 기준선 A: 제목·설명만
    python -m tools.eval_search --baseline asr       # 기준선 B: 음성 신호만
    python -m tools.eval_search --all                # 셋 다 돌려 한 표로
    python -m tools.eval_search --out eval/result_20261009.json

서버가 떠 있어야 한다(로컬 Qdrant 는 프로세스 하나만 붙을 수 있어 API 로 간다):
    python -m uvicorn server.main:app --port 8000

입력은 eval/labels_merged.csv. 컬럼:
    qid, video_id, question, q_type, start, end, grade, labeler, source

측정 설계는 멘토 자문을 그대로 따른다.
- 주지표 **nDCG@5**. 등급형 정답(정확 2 / 관련 1 / 무관 0)과 세트다
- **비대칭 허용 오차** — 결과 시작점 s 가 정답 구간 [g, e] 에 대해
      g - 5.0  <=  s  <=  g + 2.0   이면 그 구간의 등급을 인정한다.
  사용자는 목표보다 앞서 시작하는 것은 감수해도 지나쳐 시작하는 것은 감수하지 못한다
- **질문 유형별로 분리**해서 보고한다. 전체 평균 하나보다 유형 간 차이가 유용하다
- 정답이 없는 질문(grade=없음)은 nDCG 가 정의되지 않는다(IDCG=0). 따로 뺀다 —
  멘토 자문 3번(관련 장면 없음)의 성능은 이 질문들에서만 의미가 있다
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from server.config import DB_PATH, ROOT

API = "http://127.0.0.1:8000/api/search"
TOP_K = 5
LEAD, LAG = 5.0, 2.0  # 허용 오차: 이전 5초 / 이후 2초
TYPES = ("재료분량", "순서타이밍", "시각적상태")


# --- 입력 ------------------------------------------------------------------

class Question:
    def __init__(self, qid: str, video_id: str, text: str, q_type: str):
        self.qid, self.video_id, self.text, self.q_type = qid, video_id, text, q_type
        self.spans: list[tuple[float, float, int]] = []  # (start, end, grade)
        self.no_answer = False
        self.labelers: set[str] = set()

    @property
    def gains(self) -> list[int]:
        return sorted((g for _, _, g in self.spans), reverse=True)


def load(path: Path, known: set[str]) -> tuple[list[Question], list[str]]:
    """csv -> 질문 목록. 색인에 없는 영상은 건너뛰고 사유를 돌려준다."""
    by_qid: dict[str, Question] = {}
    skipped: list[str] = []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vid = row["video_id"].strip()
            if vid not in known:
                skipped.append(f"{row['qid']} {vid} — 색인에 없는 영상")
                continue
            q = by_qid.get(row["qid"])
            if q is None:
                q = by_qid[row["qid"]] = Question(row["qid"], vid, row["question"], row["q_type"])
            q.labelers.add(row.get("labeler", ""))
            grade = row["grade"].strip()
            if grade == "없음":
                q.no_answer = True
                continue
            if not row["start"].strip():
                continue
            start, end = float(row["start"]), float(row["end"])
            if start > end:  # 뒤바뀐 입력은 바로잡아 쓴다
                start, end = end, start
            q.spans.append((start, end, int(grade)))
    return list(by_qid.values()), skipped


# --- 검색 ------------------------------------------------------------------

def ask(user_id: str, question: Question, baseline: str | None) -> list[dict[str, Any]]:
    """그 영상 안에서만 검색한다. 평가셋이 질문마다 정답 영상을 정해두기 때문이다."""
    body = {"query": question.text, "video_id": question.video_id, "top_k": TOP_K}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        API, data=data, method="POST",
        headers={"X-User-Id": user_id, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            scenes = json.loads(res.read().decode())["scenes"]
    except urllib.error.URLError as exc:
        raise SystemExit(f"서버에 못 붙었습니다({exc}). uvicorn 을 먼저 띄우세요.") from None

    if baseline:
        scenes = rerank_baseline(scenes, question, baseline)
    return scenes[:TOP_K]


def rerank_baseline(scenes: list[dict], question: Question, baseline: str) -> list[dict]:
    """기준선 — 신호 하나만 쓰면 어디까지 가는지.

    같은 후보 집합을 쓰고 **정렬 기준만** 바꾼다. 후보를 만드는 방법까지 바꾸면
    무엇 때문에 차이가 났는지 말할 수 없다.
    """
    tokens = [t for t in question.text.replace("?", " ").split() if len(t) > 1]

    def overlap(text: str) -> float:
        if not tokens:
            return 0.0
        return sum(1 for t in tokens if t in text) / len(tokens)

    if baseline == "title":
        key = lambda s: overlap(str(s.get("title", "")))
    elif baseline == "asr":
        key = lambda s: overlap(str(s.get("asr_text", "")))
    else:
        raise SystemExit(f"모르는 기준선입니다: {baseline}")
    return sorted(scenes, key=key, reverse=True)


# --- 채점 ------------------------------------------------------------------

def grade_results(scenes: list[dict], question: Question) -> tuple[list[int], set[int]]:
    """결과 목록을 등급 목록으로. 비대칭 허용 오차 안에 들어온 정답 구간의 등급을 준다.

    ⚠ **정답 구간 하나는 한 번만 득점한다.** 허용 오차가 이전 5초로 넓어서, 맞닿은
      4초 카드 여러 장이 같은 정답에 동시에 걸린다. 매번 점수를 주면 DCG 가 IDCG 를
      넘어 nDCG 가 1 을 초과한다(실제로 1.15 가 나왔다). 이미 쓴 구간은 소비 처리한다.
    """
    used: set[int] = set()
    gains: list[int] = []
    for scene in scenes:
        start = float(scene.get("seek_time", scene.get("start_time", 0)))
        best, best_i = 0, None
        for i, (g_start, _g_end, grade) in enumerate(question.spans):
            if i in used:
                continue
            if g_start - LEAD <= start <= g_start + LAG and grade > best:
                best, best_i = grade, i
        if best_i is not None:
            used.add(best_i)
        gains.append(best)
    return gains, used


def dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def score(question: Question, scenes: list[dict]) -> dict[str, float]:
    gains, used = grade_results(scenes, question)
    ideal = question.gains[:TOP_K]
    idcg = dcg(ideal)
    ndcg = dcg(gains) / idcg if idcg > 0 else 0.0

    relevant = sum(1 for _, _, g in question.spans if g >= 1)
    found = len(used)          # 맞힌 **정답 구간** 수 (결과 수가 아니다)
    recall = found / relevant if relevant else 0.0

    hits, precision_sum = 0, 0.0
    for i, g in enumerate(gains, start=1):
        if g >= 1:
            hits += 1
            precision_sum += hits / i
    ap = precision_sum / min(relevant, TOP_K) if relevant else 0.0
    return {"ndcg": ndcg, "recall": recall, "ap": ap, "hit1": 1.0 if gains and gains[0] >= 1 else 0.0}


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# --- 실행 ------------------------------------------------------------------

def run(user_id: str, questions: list[Question], baseline: str | None) -> dict[str, Any]:
    answered = [q for q in questions if q.spans]
    empty = [q for q in questions if q.no_answer and not q.spans]

    per_type: dict[str, list[dict]] = defaultdict(list)
    rows = []
    for q in answered:
        scenes = ask(user_id, q, baseline)
        s = score(q, scenes)
        per_type[q.q_type].append(s)
        rows.append({"qid": q.qid, "q_type": q.q_type, "question": q.text, **s})

    # 정답이 없는 질문: 시스템이 "관련 장면 없음" 을 말할 수 있는가 (자문 3)
    empty_rows = []
    for q in empty:
        scenes = ask(user_id, q, baseline)
        top = float(scenes[0]["score"]) if scenes else 0.0
        empty_rows.append({"qid": q.qid, "question": q.text, "returned": len(scenes), "top_score": top})

    overall = [s for lst in per_type.values() for s in lst]
    return {
        "baseline": baseline or "현행",
        "questions": len(answered),
        "overall": {k: mean([s[k] for s in overall]) for k in ("ndcg", "recall", "ap", "hit1")},
        "by_type": {
            t: {"n": len(per_type[t]), **{k: mean([s[k] for s in per_type[t]]) for k in ("ndcg", "recall", "ap", "hit1")}}
            for t in TYPES if per_type[t]
        },
        "no_answer": {
            "n": len(empty_rows),
            "returned_anyway": sum(1 for r in empty_rows if r["returned"] > 0),
            "mean_top_score": mean([r["top_score"] for r in empty_rows]),
        },
        "rows": rows,
        "no_answer_rows": empty_rows,
    }


def show(result: dict[str, Any]) -> None:
    o = result["overall"]
    print(f"\n[{result['baseline']}]  질문 {result['questions']}개")
    print(f"  {'':14} {'nDCG@5':>8} {'Recall@5':>9} {'mAP':>7} {'1위적중':>8}   n")
    print(f"  {'전체':14} {o['ndcg']:>8.3f} {o['recall']:>9.3f} {o['ap']:>7.3f} {o['hit1']:>8.3f}   {result['questions']}")
    for t, v in result["by_type"].items():
        print(f"  {'  ' + t:14} {v['ndcg']:>8.3f} {v['recall']:>9.3f} {v['ap']:>7.3f} {v['hit1']:>8.3f}   {v['n']}")
    na = result["no_answer"]
    if na["n"]:
        print(f"\n  정답 없는 질문 {na['n']}개 — 그래도 결과를 내놓은 질문 {na['returned_anyway']}개"
              f" (평균 1위 점수 {na['mean_top_score']:.3f})")
        print("  → 이 값이 멘토 자문 3번(관련 장면 없음)의 현재 성적입니다. 임계값 도입 전후를 여기서 비교하세요.")


def agreement(path: Path) -> None:
    """같은 질문을 두 사람 이상이 라벨링했을 때만 계산한다."""
    by_q: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row["video_id"], row["question"])
            by_q[key][row.get("labeler", "")].append(row)
    shared = {k: v for k, v in by_q.items() if len(v) >= 2}
    if not shared:
        print("\n[라벨러 일치도] 두 사람 이상이 라벨링한 질문이 없어 계산하지 않았습니다.")
        print("  같은 질문에 서로 다른 라벨러의 줄이 있어야 합니다(영상이 같은 것만으로는 안 됩니다).")
        return
    agree = 0
    for key, per in shared.items():
        starts = []
        for rows in per.values():
            s = [float(r["start"]) for r in rows if r["start"].strip()]
            starts.append(min(s) if s else None)
        a, b = starts[0], starts[1]
        if a is None and b is None:
            agree += 1          # 둘 다 "정답 없음" 이면 일치
        elif a is not None and b is not None and abs(a - b) <= LEAD:
            agree += 1          # 허용 오차 안이면 같은 곳을 가리킨 것으로 본다
    print(f"\n[라벨러 일치도] 공통 질문 {len(shared)}개 중 {agree}개 일치"
          f" = {agree / len(shared):.1%}  (기준: 시작점 차이 {LEAD:.0f}초 이내)")


def main() -> int:
    p = argparse.ArgumentParser(description="평가셋으로 검색 성능 측정")
    p.add_argument("--labels", default=str(ROOT / "eval" / "labels_merged.csv"))
    p.add_argument("--baseline", choices=["title", "asr"], default=None)
    p.add_argument("--all", action="store_true", help="현행 + 기준선 2종을 모두")
    p.add_argument("--out", default=None, help="결과를 JSON 으로 저장")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    known = {r[0] for r in conn.execute("SELECT video_id FROM videos WHERE status='ready'")}
    user_id = conn.execute("SELECT user_id FROM videos LIMIT 1").fetchone()
    if not user_id:
        return print("저장된 영상이 없습니다.") or 1
    user_id = user_id[0]

    path = Path(args.labels)
    if not path.is_file():
        return print(f"평가셋이 없습니다: {path}") or 1
    questions, skipped = load(path, known)
    print(f"평가셋: {path.name} — 질문 {len(questions)}개 사용")
    if skipped:
        print(f"  건너뜀 {len(skipped)}개:")
        for s in skipped[:8]:
            print(f"    {s}")
        if len(skipped) > 8:
            print(f"    … 외 {len(skipped) - 8}개")

    runs = [None, "title", "asr"] if args.all else [args.baseline]
    results = [run(user_id, questions, b) for b in runs]
    for r in results:
        show(r)

    if len(results) > 1:
        print("\n=== 기준선 대비 개선폭 (nDCG@5) ===")
        base = {r["baseline"]: r["overall"]["ndcg"] for r in results}
        cur = base.get("현행", 0.0)
        for name in ("title", "asr"):
            if name in base:
                print(f"  현행 - {name:5} = {cur - base[name]:+.3f}   ({cur:.3f} vs {base[name]:.3f})")

    agreement(path)

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
