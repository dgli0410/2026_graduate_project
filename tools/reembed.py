"""단계 8 재실행 · 저장된 장면 카드를 지금 설정의 글 좌표로 다시 색인합니다. 담당 D.

임베딩 모델(또는 TEXT_EMBED_BACKEND)을 바꾸면 **벡터 공간이 달라집니다.** 차원이 같아도
예전 모델로 만든 장면 벡터와 새 모델로 만든 검색어 벡터는 비교가 되지 않아, 섞인 채로 두면
검색이 조용히 망가집니다. 이 도구로 기존 영상을 전부 다시 색인해서 공간을 통일합니다.

**Gemini 생성 호출(캡션·음성·요약)은 한 번도 하지 않습니다.**
장면 카드의 글(asr/ocr/caption)이 이미 SQLite 에 있어서 임베딩만 다시 걸면 됩니다.

좌표 차원이 바뀌는 전환(예: Gemini 768 -> BGE-M3 1024)은 창고를 새로 만들어야 하므로
`--recreate` 가 필요합니다. 글은 SQLite 에 남아 있어 잃는 것은 없습니다.

    python -m tools.reembed              # 계획만 보여줍니다 (호출 0회)
    python -m tools.reembed --yes        # 실제로 다시 색인
    python -m tools.reembed --yes --video PbCq-C9GRzs
    python -m tools.reembed --yes --recreate   # 좌표 차원이 바뀌었을 때

⚠ 로컬 Qdrant 는 프로세스 하나만 붙을 수 있습니다. **서버를 먼저 끄세요.**
"""
from __future__ import annotations

import argparse
import math
import sys

from server import db, embedder, vectors
from server.config import (
    DEFAULT_CONDITION,
    GEMINI_EMBED_MODEL,
    backend,
    collection_for,
)
from server.pipeline.segmenter import search_text

BATCH = 32  # embedder._encode_gemini 의 배치 크기와 같아야 호출 수 예측이 맞습니다


def collection_dim(condition: str) -> int | None:
    """지금 창고가 쓰는 글 좌표 차원. 창고가 없으면 None."""
    client = vectors.get_client()
    name = collection_for(condition)
    if not client.collection_exists(name):
        return None
    config = client.get_collection(name).config.params.vectors
    if isinstance(config, dict) and vectors.TEXT_VECTOR in config:
        return int(config[vectors.TEXT_VECTOR].size)
    return None


def targets(video_id: str | None) -> list[tuple[str, str, str]]:
    """(user_id, video_id, title) — 분석이 끝난 영상만."""
    with db.session() as conn:
        sql = "SELECT user_id, video_id, title FROM videos WHERE status='ready'"
        args: tuple = ()
        if video_id:
            sql += " AND video_id=?"
            args = (video_id,)
        rows = conn.execute(sql + " ORDER BY created_at", args).fetchall()
    return [(r["user_id"], r["video_id"], r["title"]) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="저장된 장면 카드를 다시 색인합니다")
    parser.add_argument("--yes", action="store_true", help="실제로 실행 (없으면 계획만)")
    parser.add_argument("--video", default=None, help="이 영상 하나만")
    parser.add_argument("--condition", default=DEFAULT_CONDITION, help="색인할 조건")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="창고를 비우고 새로 만듭니다 (좌표 차원이 바뀌었을 때 필요)",
    )
    args = parser.parse_args()

    model = GEMINI_EMBED_MODEL if backend("text_embed") == "gemini" else "BGE-M3"
    print(f"글 좌표 백엔드 : {backend('text_embed')}  ({model}, {embedder.dim()}차원)")
    print(f"조건           : {args.condition}")

    rows = targets(args.video)
    if not rows:
        print("대상 영상이 없습니다.")
        return 1

    plan: list[tuple[str, str, str, list[dict]]] = []
    calls = 0
    for user_id, video_id, title in rows:
        segments = db.list_segments(user_id, video_id)
        if not segments:
            print(f"  건너뜀 {video_id} — 장면 카드가 없습니다")
            continue
        calls += math.ceil(len(segments) / BATCH)
        plan.append((user_id, video_id, title, segments))
        print(f"  {video_id:<13} 장면 {len(segments):>3}개  {title[:30]}")

    total = sum(len(p[3]) for p in plan)
    print(f"\n영상 {len(plan)}개 / 장면 {total}개 / 예상 임베딩 호출 {calls}회")
    print("Gemini 생성 호출(캡션·음성·요약): 0회")

    if not args.yes:
        print("\n계획만 보여줬습니다. 실제로 하려면 --yes 를 붙이세요.")
        return 0

    # 여기서부터 Qdrant 를 건드립니다. 서버가 켜져 있으면 잠금 때문에 실패합니다.
    try:
        have, want = collection_dim(args.condition), embedder.dim()
        name = collection_for(args.condition)

        if have is not None and have != want and not args.recreate:
            print(
                f"\n창고({name})는 {have}차원인데 지금 백엔드는 {want}차원입니다."
                "\n차원이 다르면 같은 창고에 넣을 수 없습니다."
                "\n--recreate 를 붙이면 창고를 비우고 새로 만듭니다."
                " 글은 SQLite 에 있으니 잃는 것은 없습니다.",
                file=sys.stderr,
            )
            return 2

        if args.recreate and have is not None:
            print(f"  창고 {name} 를 비웁니다 ({have}차원 -> {want}차원)")
            vectors.get_client().delete_collection(name)

        vectors.ensure_collection(args.condition)
    except RuntimeError as exc:
        if "already accessed" in str(exc):
            print("\n서버가 Qdrant 를 잡고 있습니다. 서버를 끄고 다시 실행하세요.", file=sys.stderr)
            return 2
        raise

    done = 0
    for user_id, video_id, title, segments in plan:
        texts = [search_text(seg, title=title) for seg in segments]
        try:
            text_vectors = embedder.encode(texts)
        except Exception as exc:  # noqa: BLE001 - 한 영상이 실패해도 나머지는 계속합니다
            print(f"  실패 {video_id}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        # point id 가 (user_id, segment_id) 로 정해져 있어 같은 자리를 덮어씁니다.
        vectors.upsert_segments(
            user_id, video_id, title, segments, text_vectors, None, args.condition
        )
        done += 1
        print(f"  완료 {video_id}  장면 {len(segments)}개")

    print(f"\n{done}/{len(plan)}개 영상을 다시 색인했습니다.")
    if done < len(plan):
        print("실패한 영상은 할당량이 풀린 뒤 같은 명령을 다시 돌리면 됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
