"""거절 임계값 곡선 (멘토 자문 3번). 담당 D.

> 근거가 부족하면 "관련 장면 없음" 이라고 답할 것. (2026-09-18 서면 자문)

**이건 순위 문제가 아니라 분류 문제입니다.** "어떤 장면이 제일 맞나"(순위)와 "답할
자격이 있나"(거절)는 별개인데, `tools.eval_search` 는 앞의 것만 잽니다. 거절은
nDCG 가 아니라 **정밀도·재현율**로 봐야 합니다.

판단 근거는 `search()` 가 새로 내려주는 `top_raw` — **정규화 전** 코사인 유사도입니다.
정규화된 점수로는 못 합니다. minmax 가 후보 중 최고를 항상 1.0 으로 만들어서, 아무것도
안 맞는 질의에서도 1위가 만점이 되기 때문입니다.

거절해야 할 질문은 두 종류이고 난이도가 다릅니다. 섞어서 하나의 숫자로 보고하면
"어느 쪽을 해결한 것이냐" 는 질문에 답할 수 없어, 나눠서 보고합니다.

    (나) 맞는 영상인데 그 장면만 없음   "맛술은 얼마나 넣어?"      ← 평가셋의 13개
    (가) 분야가 아예 다름              "자동차 엔진 오일 교환"    ← eval/offtopic.txt

⚠ (가) 질문은 **팀이 쓴 것이고 외부 지인에게 받은 것이 아닙니다.** 정답 라벨이 필요 없는
  (요리 영상에 자동차 질문은 당연히 답이 없는) 점검용 질의라 성격이 다릅니다. 평가셋의
  (나) 와 섞지 않고 따로 보고하는 이유이기도 합니다.

서버를 띄우고 실행합니다(Gemini 호출 0회 — with_answer=false 로 보냅니다).

    python -m tools.threshold_sweep
    python -m tools.threshold_sweep --out eval/threshold.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import urllib.error
import urllib.request
from pathlib import Path

from server.config import DB_PATH, ROOT
from tools import eval_search as ev

API = "http://127.0.0.1:8000/api/search"
STEPS = [x / 100 for x in range(40, 68, 2)]


def ask_raw(user_id: str, query: str, video_id: str | None) -> float:
    body = {"query": query, "top_k": ev.TOP_K, "with_answer": False, "video_limit": 1}
    if video_id:
        body["video_id"] = video_id
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(), method="POST",
        headers={"X-User-Id": user_id, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            data = json.loads(res.read().decode())
    except urllib.error.URLError as exc:
        raise SystemExit(f"서버에 못 붙었습니다({exc}). uvicorn 을 먼저 띄우세요.") from None
    if "top_raw" not in data:
        raise SystemExit("서버가 top_raw 를 안 내려줍니다. 최신 코드로 서버를 다시 띄우세요.")
    return float(data["top_raw"])


def spread(name: str, values: list[float]) -> None:
    if not values:
        return
    print(f"  {name:<22} n={len(values):<3} 평균 {statistics.mean(values):.3f}"
          f"  최소 {min(values):.3f}  최대 {max(values):.3f}")


def auc(pos: list[float], neg: list[float]) -> float:
    """정답 없는 질문이 정답 있는 질문보다 낮게 나올 확률. 0.5 면 신호 없음."""
    if not pos or not neg:
        return 0.0
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def main() -> int:
    p = argparse.ArgumentParser(description="거절 임계값 곡선 (자문 3)")
    p.add_argument("--labels", default=str(ROOT / "eval" / "labels_merged.csv"))
    p.add_argument("--offtopic", default=str(ROOT / "eval" / "offtopic.txt"))
    p.add_argument("--out", default=str(ROOT / "eval" / "threshold.json"))
    args = p.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    known = {r[0] for r in conn.execute("SELECT video_id FROM videos WHERE status='ready'")}
    row = conn.execute("SELECT user_id FROM videos LIMIT 1").fetchone()
    if not row:
        print("저장된 영상이 없습니다.")
        return 1
    user_id = row[0]

    questions, _ = ev.load(Path(args.labels), known)
    answered = [q for q in questions if q.spans]
    missing = [q for q in questions if q.no_answer and not q.spans]

    print("1위 원유사도를 모으는 중...")
    yes = [ask_raw(user_id, q.text, q.video_id) for q in answered]
    no_b = [ask_raw(user_id, q.text, q.video_id) for q in missing]

    no_a: list[float] = []
    off_path = Path(args.offtopic)
    if off_path.is_file():
        lines = [l.strip() for l in off_path.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
        # 분야가 다른 질의는 보관함 전체에서 검색합니다(영상을 지정할 수 없으므로).
        no_a = [ask_raw(user_id, q, None) for q in lines]

    print("\n[1위 원유사도 분포]")
    spread("정답 있음", yes)
    spread("(나) 장면만 없음", no_b)
    spread("(가) 분야가 다름", no_a)

    print(f"\n  판별력 AUC  (나) {auc(yes, no_b):.3f}   (가) {auc(yes, no_a):.3f}"
          if no_a else f"\n  판별력 AUC  (나) {auc(yes, no_b):.3f}")
    print("    0.5 = 신호 없음, 1.0 = 완벽히 갈림")

    print("\n[임계값 곡선]  이 값보다 낮으면 '관련 장면 없음' 으로 처리")
    head = f"{'임계값':>7}{'(나) 올바른 거절':>16}{'(가) 올바른 거절':>16}{'잘못된 거절':>13}{'거절 정밀도':>12}"
    print(head)
    print("-" * len(head))
    rows = []
    for t in STEPS:
        tn_b = sum(1 for v in no_b if v < t)
        tn_a = sum(1 for v in no_a if v < t)
        fn = sum(1 for v in yes if v < t)
        prec = (tn_b + tn_a) / (tn_b + tn_a + fn) if (tn_b + tn_a + fn) else 0.0
        rows.append({"threshold": t, "reject_b": tn_b, "reject_a": tn_a,
                     "wrong_reject": fn, "precision": round(prec, 3)})
        print(f"{t:>7.2f}{f'{tn_b}/{len(no_b)}':>16}"
              f"{(f'{tn_a}/{len(no_a)}' if no_a else '-'):>16}"
              f"{f'{fn}/{len(yes)}':>13}{prec:>12.2f}")

    print("\n  잘못된 거절 = 답이 있는데 없다고 하는 것. 심사 데모에서 더 치명적입니다.")
    print("  운영점을 고를 때 어느 실수가 더 나쁜지를 먼저 정하세요.")

    Path(args.out).write_text(json.dumps(
        {"answered": yes, "missing_scene": no_b, "offtopic": no_a, "curve": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {args.out}")
    print("고른 값은 .env 의 NO_ANSWER_THRESHOLD 에 넣으면 적용됩니다(0 이면 꺼짐).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
