"""제거법(ablation) · 음성·자막·설명 중 무엇이 검색을 맞히고 있는가. 담당 D.

멘토 자문 2번("신호별 역할을 규명하라")에 답하기 위한 도구입니다.

**왜 필요한가.** 지금 색인은 `segmenter.search_text()` 에서 제목·설명·음성·자막을
**문자열 하나로 붙여** BGE-M3 에 넣습니다. 좌표가 1개뿐이라 검색이 맞혔을 때
어느 신호 덕인지 점수만 봐서는 알 수 없습니다. 그래서 **빼보고 얼마나 떨어지는지**
재는 방법밖에 없습니다.

세 신호의 조합은 2^3-1 = 7개뿐이라 전수로 돕니다.

    1 음성+자막+설명   기준
    2 음성+자막        설명을 뺀다 — Gemini 없이 로컬만으로 되나
    3 음성+설명        자막을 뺀다 — EasyOCR 이 필요한가
    4 자막+설명        음성을 뺀다 — Whisper 가 필요한가
    5 설명만 / 6 자막만 / 7 음성만

**제품 코드는 하나도 바꾸지 않습니다.** 조건마다 Qdrant 창고를 따로 만들고
(`shorts_segments__abl_*`), 검색은 제품과 똑같은 `server.search.search()` 를
`condition=` 만 바꿔 부릅니다(설계 원칙 6).

빼는 방법은 **장면 카드에서 그 필드를 빈 문자열로 만들어 색인**하는 것입니다.
글 좌표(BGE)와 정확한 단어(lexical) 둘 다 그 필드를 못 보게 됩니다 — 단어 점수는
Qdrant payload 를 읽기 때문에 payload 도 같이 비워야 합니다. 벡터만 비우면
단어 채널로 정보가 새어 결과가 과대평가됩니다.

**그림 좌표(SigLIP)는 7개 조건 모두에 똑같이 켜 둡니다.** 글 쪽만 바꾸는 대조군이라
조건 간 차이는 전부 글에서 온 것이 됩니다. 프레임은 한 번만 인코딩해 7벌이 나눠 씁니다.

**Gemini 호출 0회** — 장면 카드의 글은 이미 SQLite 에 있고, 답변 생성은 끕니다.
(참고: `tools.eval_search` 는 서버 API 로 가기 때문에 질문마다 답변 생성이 돌아
한 번 돌릴 때 Gemini 를 60회 가까이 씁니다. 여기서는 그게 없습니다.)

    python -m tools.ablation            # 계획만 (아무것도 안 바꿈)
    python -m tools.ablation --yes      # 7벌 색인 + 측정
    python -m tools.ablation --cleanup  # 실험 창고 삭제 (제품 창고는 안 건드림)

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

from server import db, embedder, image_embedder, vectors
from server.config import DB_PATH, QDRANT_COLLECTION, ROOT
from server.pipeline.segmenter import search_text
from tools import eval_search as ev
from tools.reembed import encode_images, targets

# (키, 쓰는 필드, 보여줄 이름, 답하는 질문)
# 필드 이름은 장면 카드 JSON 의 키 그대로입니다.
PLAN: list[tuple[str, tuple[str, ...], str, str]] = [
    ("all",    ("asr_text", "ocr_text", "caption"), "음성+자막+설명",  "기준"),
    ("no_cap", ("asr_text", "ocr_text"),            "음성+자막 (-설명)", "Gemini 없이 로컬만으로 되나"),
    ("no_ocr", ("asr_text", "caption"),             "음성+설명 (-자막)", "EasyOCR 이 필요한가"),
    ("no_asr", ("ocr_text", "caption"),             "자막+설명 (-음성)", "Whisper 가 필요한가"),
    ("cap",    ("caption",),                        "설명만",           "Gemini 만으로 충분한가"),
    ("ocr",    ("ocr_text",),                       "자막만",           ""),
    ("asr",    ("asr_text",),                       "음성만",           ""),
]

ALL_FIELDS = ("asr_text", "ocr_text", "caption")
METRICS = ("ndcg", "recall", "ap", "hit1")


def condition_of(key: str) -> str:
    """제품 창고와 절대 겹치지 않는 이름. collection 은 shorts_segments__abl_<key>."""
    return f"abl_{key}"


def collection_of(key: str) -> str:
    return f"{QDRANT_COLLECTION}__{condition_of(key)}"


def strip_fields(segment: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any]:
    """쓰지 않는 필드를 빈 문자열로. 벡터와 payload 가 함께 비워집니다."""
    out = dict(segment)
    for field in ALL_FIELDS:
        if field not in keep:
            out[field] = ""
    return out


# --- 색인 ------------------------------------------------------------------

def build(plan_rows: list[tuple], image_cache: dict[str, Any]) -> None:
    """7개 조건의 창고를 만듭니다. 프레임 인코딩은 첫 조건에서만 돌고 뒤는 재사용합니다."""
    client = vectors.get_client()
    for key, keep, label, _ in PLAN:
        name = collection_of(key)
        if client.collection_exists(name):
            client.delete_collection(name)
        vectors.ensure_collection(condition_of(key))

        started, cards = time.time(), 0
        for user_id, video_id, title, segments in plan_rows:
            trimmed = [strip_fields(s, keep) for s in segments]
            text_vectors = embedder.encode([search_text(s, title=title) for s in trimmed])
            if video_id not in image_cache:
                image_cache[video_id] = encode_images(user_id, video_id, segments)
            vectors.upsert_segments(
                user_id, video_id, title, trimmed,
                text_vectors, image_cache[video_id], condition_of(key),
            )
            cards += len(segments)
        print(f"  [{label}] 장면 {cards}개 색인  {time.time() - started:.0f}초")


# --- 측정 ------------------------------------------------------------------

def ask(user_id: str, question: ev.Question, key: str) -> list[dict[str, Any]]:
    """제품과 같은 검색 함수. 답변 생성만 끕니다(Gemini 호출 0회)."""
    from server import search as search_mod

    result = search_mod.search(
        user_id, question.text,
        video_id=question.video_id, top_k=ev.TOP_K,
        with_answer=False, condition=condition_of(key),
    )
    return result["scenes"][: ev.TOP_K]


def measure(user_id: str, questions: list[ev.Question], key: str, label: str) -> dict[str, Any]:
    answered = [q for q in questions if q.spans]
    empty = [q for q in questions if q.no_answer and not q.spans]

    per_type: dict[str, list[dict]] = defaultdict(list)
    rows = []
    for q in answered:
        s = ev.score(q, ask(user_id, q, key))
        per_type[q.q_type].append(s)
        # ⚠ 문항별로 남깁니다. 조건별 평균만 보면 "설명만" 이 낮은 것이 설명이 쓸모없어서인지
        #   그 영상에 설명이 애초에 없어서인지 구분할 수 없습니다. 48문항 중 17개가
        #   설명이 0개인 영상에 붙어 있어, 나중에 잘라 보려면 원자료가 필요합니다.
        rows.append({"qid": q.qid, "video_id": q.video_id, "q_type": q.q_type, **s})

    returned = sum(1 for q in empty if ask(user_id, q, key))
    overall = [s for lst in per_type.values() for s in lst]
    return {
        "key": key,
        "label": label,
        "questions": len(answered),
        "rows": rows,
        "overall": {m: ev.mean([s[m] for s in overall]) for m in METRICS},
        "by_type": {
            t: {"n": len(per_type[t]),
                **{m: ev.mean([s[m] for s in per_type[t]]) for m in METRICS}}
            for t in ev.TYPES if per_type[t]
        },
        "no_answer": {"n": len(empty), "returned_anyway": returned},
    }


def show(results: list[dict[str, Any]]) -> None:
    base = next(r for r in results if r["key"] == "all")
    w = max(len(r["label"]) for r in results) + 2

    print("\n" + "=" * 76)
    print("제거법 결과 — 무엇을 빼면 얼마나 떨어지는가")
    print("=" * 76)
    print(f"{'#':<3}{'조건':<{w}}{'nDCG@5':>9}{'기준대비':>10}{'Recall@5':>10}{'1위적중':>9}")
    print("-" * 76)
    for i, r in enumerate(results, start=1):
        o = r["overall"]
        gap = "" if r["key"] == "all" else f"{o['ndcg'] - base['overall']['ndcg']:+.3f}"
        print(f"{i:<3}{r['label']:<{w}}{o['ndcg']:>9.3f}{gap:>10}"
              f"{o['recall']:>10.3f}{o['hit1']:>9.3f}")

    print(f"\n{'유형별 nDCG@5':<{w + 3}}" + "".join(f"{t:>12}" for t in ev.TYPES))
    print("-" * 76)
    for i, r in enumerate(results, start=1):
        cells = "".join(
            f"{r['by_type'][t]['ndcg']:>12.3f}" if t in r["by_type"] else f"{'-':>12}"
            for t in ev.TYPES
        )
        print(f"{i:<3}{r['label']:<{w}}{cells}")

    print("\n정답 없는 질문에 그래도 답한 비율 (자문 3번 — 낮을수록 좋음)")
    print("-" * 76)
    for i, r in enumerate(results, start=1):
        na = r["no_answer"]
        print(f"{i:<3}{r['label']:<{w}}{na['returned_anyway']}/{na['n']}")


# --- 실행 ------------------------------------------------------------------

def cleanup() -> int:
    client = vectors.get_client()
    removed = 0
    for key, *_ in PLAN:
        name = collection_of(key)
        if client.collection_exists(name):
            client.delete_collection(name)
            print(f"  삭제 {name}")
            removed += 1
    print(f"\n실험 창고 {removed}개를 지웠습니다. 제품 창고({QDRANT_COLLECTION})는 그대로입니다.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="음성·자막·설명 제거법 실험 (7개 조건 전수)")
    p.add_argument("--yes", action="store_true", help="실제로 색인하고 측정 (없으면 계획만)")
    p.add_argument("--cleanup", action="store_true", help="실험 창고만 삭제")
    p.add_argument("--labels", default=str(ROOT / "eval" / "labels_merged.csv"))
    p.add_argument("--out", default=str(ROOT / "eval" / "ablation.json"))
    p.add_argument(
        "--include-incomplete",
        action="store_true",
        help="설명이 0개인 영상도 포함 (기본은 제외 — 수집 실패를 성능으로 오인하게 됩니다)",
    )
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

    # 설명이 0개인 영상은 실험에서 통째로 뺍니다 — 색인에서도, 질문에서도.
    # Gemini 일일 할당량이 떨어져 캡션을 못 뽑은 것이지 "설명이 쓸모없어서" 비어 있는 게
    # 아닙니다. 그대로 두면 "설명만" 조건이 **존재하지도 않는 신호**로 채점돼,
    # 제거법이 재려는 것(신호의 값어치)이 아니라 수집 실패를 재게 됩니다.
    # 빼면 표본이 줄지만, 7개 조건 모두 같은 영상·같은 질문을 보게 되어 비교가 성립합니다.
    blank = {
        r[0] for r in conn.execute(
            "SELECT video_id FROM segments GROUP BY video_id"
            " HAVING SUM(CASE WHEN COALESCE(caption,'') <> '' THEN 1 ELSE 0 END) = 0"
        )
    } & known
    if blank and not args.include_incomplete:
        known -= blank
        print(f"제외: 설명이 0개인 영상 {len(blank)}개 — {', '.join(sorted(blank))}")
        print("  (할당량으로 캡션을 못 뽑은 영상입니다. 색인·질문 양쪽에서 뺍니다)")

    path = Path(args.labels)
    if not path.is_file():
        print(f"평가셋이 없습니다: {path}")
        return 1
    questions, skipped = ev.load(path, known)
    answered = [q for q in questions if q.spans]

    plan_rows = []
    for uid, video_id, title in targets(None):
        if video_id not in known:
            continue
        segments = db.list_segments(uid, video_id)
        if segments:
            plan_rows.append((uid, video_id, title, segments))
    cards = sum(len(r[3]) for r in plan_rows)

    print(f"영상 {len(plan_rows)}개 / 장면 {cards}개")
    print(f"평가셋 {path.name} — 채점 질문 {len(answered)}개 (건너뜀 {len(skipped)}개)")
    print(f"글 좌표 {embedder.dim()}차원 / 그림 좌표 "
          f"{'켬' if image_embedder.is_enabled() else '끔'}")
    print(f"\n조건 {len(PLAN)}개 — 글 임베딩 {cards * len(PLAN)}회, 프레임 {cards}회, Gemini 0회")
    for i, (_, _, label, why) in enumerate(PLAN, start=1):
        print(f"  {i}. {label:<18} {why}")

    if not args.yes:
        print("\n계획만 보여줬습니다. 실제로 하려면 --yes 를 붙이세요.")
        return 0

    print("\n--- 색인 ---")
    image_cache: dict[str, Any] = {}
    build(plan_rows, image_cache)

    print("\n--- 측정 ---")
    results = []
    for key, _, label, _ in PLAN:
        started = time.time()
        results.append(measure(user_id, questions, key, label))
        print(f"  [{label}] nDCG@5 {results[-1]['overall']['ndcg']:.3f}"
              f"  {time.time() - started:.0f}초")

    show(results)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {args.out}")
    print("실험 창고를 지우려면: python -m tools.ablation --cleanup")
    return 0


def _close() -> None:
    """Qdrant 를 명시적으로 닫습니다.

    ⚠ 인터프리터 종료에 맡기면 __del__ 이 sys.meta_path 가 사라진 뒤에 불려
      close() 가 ImportError 로 죽습니다. 그러면 storage.sqlite 가 갱신되지 않은 채
      남아, 메타데이터와 실제 저장물이 어긋납니다(예전에 768차원 잔재가 이렇게 남았습니다).
    """
    try:
        vectors.get_client().close()
    except Exception:  # noqa: BLE001 - 닫기 실패가 결과를 무르게 하면 안 됩니다
        pass


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _close()
    raise SystemExit(code)
