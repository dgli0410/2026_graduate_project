"""의미 단위 병합 전후 측정 (멘토 자문 1번). 담당 D.

> 4초 고정 분할은 의미 단위와 경계가 어긋나 하나의 행위가 여러 조각으로 흩어진다.
> **고정 분할을 기준선으로 두고, 인접 세그먼트를 의미 단위로 병합하는 후처리를 추가**할 것.

`segmenter.merge_segments()` 의 임계값을 쓸어가며 같은 평가셋으로 전후를 비교합니다.
임계값 0 이 고정 분할 기준선입니다.

**재분석하지 않습니다.** 병합이 "이미 만들어진 카드"에 대한 후처리라, SQLite 에 저장된
장면 카드를 읽어 합치고 다시 색인하면 끝입니다. **Gemini 호출 0회.**

평가셋 정답을 세그먼트 번호가 아니라 **시간 구간(초)** 으로 적어둔 것이 바로 이 실험을
위해서였습니다. 병합으로 카드 경계가 바뀌어도 같은 평가셋을 그대로 씁니다.

    python -m tools.merge_experiment            # 계획만
    python -m tools.merge_experiment --yes      # 색인 + 측정
    python -m tools.merge_experiment --cleanup  # 실험 창고 삭제

⚠ 로컬 Qdrant 는 프로세스 하나만 붙습니다. **서버를 끄고 실행하세요.**
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from server import db, embedder, image_embedder, media, vectors
from server.config import DB_PATH, QDRANT_COLLECTION, ROOT
from server.pipeline.segmenter import merge_segments, search_text
from tools import eval_search as ev
from tools.reembed import targets

# 쓸어볼 임계값. 0 = 고정 분할(기준선).
THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9)
MAX_SECONDS = 12.0  # 한 카드가 4초 창 3개를 넘지 않게
METRICS = ("ndcg", "recall", "ap", "hit1")


def key_of(t: float) -> str:
    return f"merge_{int(round(t * 100)):03d}"


def collection_of(t: float) -> str:
    return f"{QDRANT_COLLECTION}__{key_of(t)}"


def frame_vectors(user_id: str, video_id: str, segments: list[dict], cache: dict) -> Any:
    """대표 프레임의 그림 좌표. 파일 단위로 캐시해 임계값마다 다시 인코딩하지 않습니다."""
    if not image_embedder.is_enabled():
        return None
    frame_dir = media.frames_dir(user_id, video_id)
    paths = [frame_dir / str(s["frame_path"]).rsplit("/", 1)[-1] for s in segments]
    if not all(p.is_file() for p in paths):
        return None
    todo = [p for p in paths if str(p) not in cache]
    if todo:
        for p, vec in zip(todo, image_embedder.encode_images(todo)):
            cache[str(p)] = vec
    return [cache[str(p)] for p in paths]


def build(t: float, plan: list[tuple], cache: dict) -> int:
    client = vectors.get_client()
    name = collection_of(t)
    if client.collection_exists(name):
        client.delete_collection(name)
    vectors.ensure_collection(key_of(t))

    cards = 0
    for user_id, video_id, title, segments in plan:
        merged = merge_segments(
            segments, video_id, similarity=t, max_seconds=MAX_SECONDS
        )
        texts = [search_text(s, title=title) for s in merged]
        vectors.upsert_segments(
            user_id, video_id, title, merged, embedder.encode(texts),
            frame_vectors(user_id, video_id, merged, cache), key_of(t),
        )
        cards += len(merged)
    return cards


def ask(user_id: str, q: ev.Question, t: float, localize: bool = True) -> list[dict[str, Any]]:
    """제품과 같은 검색 함수. `localize` 가 꺼지면 이동 지점을 카드 맨 앞으로 둡니다.

    자문 1번 결론을 검증하려면 **합치기만 한 것**과 **합치고 조각으로 보내는 것**을
    갈라 봐야 합니다. `search()` 는 config 를 보므로, 여기서만 잠깐 바꿔 끕니다.
    """
    from server import search as search_mod

    saved = search_mod.SEEK_LOCALIZE
    search_mod.SEEK_LOCALIZE = localize
    try:
        return search_mod.search(
            user_id, q.text, video_id=q.video_id, top_k=ev.TOP_K,
            with_answer=False, condition=key_of(t),
        )["scenes"][: ev.TOP_K]
    finally:
        search_mod.SEEK_LOCALIZE = saved


def measure(user_id: str, questions: list[ev.Question], t: float, cards: int,
            localize: bool = True) -> dict:
    answered = [q for q in questions if q.spans]
    per_type: dict[str, list[dict]] = defaultdict(list)
    rows = []
    for q in answered:
        s = ev.score(q, ask(user_id, q, t, localize))
        per_type[q.q_type].append(s)
        rows.append({"qid": q.qid, "q_type": q.q_type, **s})
    overall = [s for lst in per_type.values() for s in lst]
    return {
        "threshold": t,
        "localize": localize,
        "cards": cards,
        "questions": len(answered),
        "overall": {m: ev.mean([s[m] for s in overall]) for m in METRICS},
        "by_type": {
            k: {"n": len(v), **{m: ev.mean([s[m] for s in v]) for m in METRICS}}
            for k, v in per_type.items()
        },
        "rows": rows,
    }


def show(results: list[dict]) -> None:
    base = results[0]
    print("\n" + "=" * 74)
    print("의미 단위 병합 — 임계값을 올릴수록 덜 합칩니다 (0 = 고정 분할 기준선)")
    print("=" * 74)
    print(f"{'임계값':>12}{'장면 카드':>10}{'nDCG@5':>9}{'기준대비':>10}"
          f"{'Recall@5':>10}{'1위적중':>9}")
    print("-" * 74)
    for r in results:
        o = r["overall"]
        gap = "" if r is base else f"{o['ndcg'] - base['overall']['ndcg']:+.3f}"
        label = "0 (기준선)" if r["threshold"] == 0 else f"{r['threshold']:.2f}"
        if r["threshold"]:
            label += " 조각" if r["localize"] else " 앞"
        print(f"{label:>12}{r['cards']:>10}{o['ndcg']:>9.3f}{gap:>10}"
              f"{o['recall']:>10.3f}{o['hit1']:>9.3f}")

    print(f"\n{'유형별 nDCG@5':<16}" + "".join(f"{t:>12}" for t in ev.TYPES))
    print("-" * 74)
    for r in results:
        label = "0 (기준선)" if r["threshold"] == 0 else (
            f"{r['threshold']:.2f} " + ("조각" if r["localize"] else "앞"))
        cells = "".join(
            f"{r['by_type'][t]['ndcg']:>12.3f}" if t in r["by_type"] else f"{'-':>12}"
            for t in ev.TYPES
        )
        print(f"{label:<16}{cells}")


def cleanup() -> int:
    client = vectors.get_client()
    n = 0
    for t in THRESHOLDS:
        name = collection_of(t)
        if client.collection_exists(name):
            client.delete_collection(name)
            print(f"  삭제 {name}")
            n += 1
    print(f"\n실험 창고 {n}개를 지웠습니다. 제품 창고({QDRANT_COLLECTION})는 그대로입니다.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="의미 단위 병합 전후 측정 (자문 1)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--cleanup", action="store_true")
    p.add_argument("--labels", default=str(ROOT / "eval" / "labels_merged.csv"))
    p.add_argument("--out", default=str(ROOT / "eval" / "merge_experiment.json"))
    args = p.parse_args()

    try:
        vectors.get_client()
    except RuntimeError as exc:
        if "already accessed" in str(exc):
            print("서버가 Qdrant 를 잡고 있습니다. 서버를 끄고 다시 실행하세요.", file=sys.stderr)
            return 2
        raise
    if args.cleanup:
        return cleanup()

    conn = sqlite3.connect(DB_PATH)
    known = {r[0] for r in conn.execute("SELECT video_id FROM videos WHERE status='ready'")}
    row = conn.execute("SELECT user_id FROM videos LIMIT 1").fetchone()
    if not row:
        print("저장된 영상이 없습니다.")
        return 1
    user_id = row[0]

    questions, skipped = ev.load(Path(args.labels), known)
    answered = [q for q in questions if q.spans]

    plan = []
    for uid, video_id, title in targets(None):
        segs = db.list_segments(uid, video_id)
        if segs:
            plan.append((uid, video_id, title, segs))
    base_cards = sum(len(r[3]) for r in plan)

    print(f"영상 {len(plan)}개 / 고정 분할 장면 {base_cards}개")
    print(f"평가셋 — 채점 질문 {len(answered)}개 (건너뜀 {len(skipped)}개)")
    print(f"임계값 {len(THRESHOLDS)}개 {THRESHOLDS} · 한 카드 최대 {MAX_SECONDS:.0f}초")
    print("Gemini 호출 0회 (저장된 카드를 합쳐 다시 색인만 합니다)")

    # 병합이 카드를 얼마나 줄이는지는 색인 없이도 셀 수 있습니다.
    print("\n임계값별 장면 카드 수(미리보기):")
    for t in THRESHOLDS:
        n = sum(len(merge_segments(s, v, similarity=t, max_seconds=MAX_SECONDS))
                for _, v, _, s in plan)
        print(f"   {t:.2f} → {n:>4}개  ({n / base_cards * 100:.0f}%)")

    if not args.yes:
        print("\n계획만 보여줬습니다. 실제로 하려면 --yes 를 붙이세요.")
        return 0

    cache: dict[str, Any] = {}
    results = []
    for t in THRESHOLDS:
        started = time.time()
        cards = build(t, plan, cache)
        # 합치기만 한 것 / 합치고 조각으로 보내는 것을 둘 다 잽니다.
        # t=0 은 조각이 없어 둘이 같으므로 한 번만 잽니다.
        modes = [False] if t == 0 else [False, True]
        for loc in modes:
            r = measure(user_id, questions, t, cards, loc)
            results.append(r)
            tag = "조각이동" if loc else "카드앞"
            print(f"  임계값 {t:.2f} [{tag}]  카드 {cards:>4}개"
                  f"  nDCG@5 {r['overall']['ndcg']:.3f}  {time.time() - started:.0f}초")

    show(results)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {args.out}")
    print("실험 창고를 지우려면: python -m tools.merge_experiment --cleanup")
    return 0


def _close() -> None:
    try:
        vectors.get_client().close()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _close()
    raise SystemExit(code)
