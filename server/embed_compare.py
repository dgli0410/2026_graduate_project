"""글 좌표 모델 비교. 저장된 장면 카드를 후보 모델로 다시 색인하고, 그 색인으로 검색합니다.

기본 조건에 저장된 장면 카드의 **글은 그대로** 쓰고 좌표만 후보 모델로 다시 만듭니다. 그래서 후보끼리의
차이는 글 좌표 모델에서만 나옵니다. 그림 좌표는 기본 창고의 것을 그대로 옮겨 모든 후보가 같은 값을 씁니다.
생성 호출(캡션 · 음성 · 요약)은 하지 않습니다. gemini 후보만 임베딩 API 를 부릅니다.

후보 목록은 server/config.py 의 EMBED_CANDIDATES.

    from server import embed_compare
    embed_compare.build_index("kure")                      # 색인 (tools/build_embed_index.py 와 같음)
    embed_compare.search(user_id, "두부 언제 넣어?", "kure")  # 검색 (/api/search 와 같은 모양으로 돌려줌)
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from server import db, embedder, vectors
from server.config import (
    DEFAULT_CONDITION,
    EMBED_CANDIDATES,
    collection_for,
    embed_candidate_info,
    embed_target,
    use_embedding,
)
from server.pipeline.segmenter import search_text


class NotIndexed(RuntimeError):
    """그 후보의 색인이 아직 없습니다."""


def ready_videos(user_id: str | None = None, video_id: str | None = None) -> list[tuple[str, str, str]]:
    """(user_id, video_id, title) — 분석이 끝난 영상만."""
    sql = "SELECT user_id, video_id, title FROM videos WHERE status='ready'"
    args: list[str] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    if video_id:
        sql += " AND video_id=?"
        args.append(video_id)
    with db.session() as conn:
        rows = conn.execute(sql + " ORDER BY created_at", args).fetchall()
    return [(r["user_id"], r["video_id"], r["title"]) for r in rows]


def _source_image_vectors(user_id: str, segments: list[dict[str, Any]]) -> np.ndarray | None:
    """기본 창고에 저장된 그림 좌표. 장면마다 다 있을 때만 돌려줍니다(일부만 있으면 None)."""
    client = vectors.get_client()
    name = collection_for(DEFAULT_CONDITION)
    if not vectors.has_image_vector(DEFAULT_CONDITION):
        return None
    ids = [vectors._point_id(user_id, str(seg["segment_id"])) for seg in segments]
    points = client.retrieve(collection_name=name, ids=ids, with_vectors=[vectors.IMAGE_VECTOR])
    found = {str(p.id): (p.vector or {}).get(vectors.IMAGE_VECTOR) for p in points}
    rows = [found.get(i) for i in ids]
    if any(row is None for row in rows):
        return None
    return np.asarray(rows, dtype=np.float32)


def _ensure_target(model: str, dim: int) -> str:
    """후보 창고를 만듭니다. 그림 좌표 칸은 **기본 창고 설정을 그대로** 따릅니다.

    vectors.ensure_collection 은 그림 좌표 크기를 알려고 SigLIP 을 올립니다. 여기서는 그림 좌표를 기본 창고에서
    복사만 하므로 모델이 필요 없고, .env 의 IMAGE_EMBED_BACKEND 와도 무관하게 모든 후보가 같은 구성을 갖습니다.
    """
    from qdrant_client import models

    from server.config import QDRANT_URL

    client = vectors.get_client()
    name = collection_for(embed_target(model))
    if client.collection_exists(name):
        return name
    config = {vectors.TEXT_VECTOR: models.VectorParams(size=dim, distance=models.Distance.COSINE)}
    source = collection_for(DEFAULT_CONDITION)
    if client.collection_exists(source):
        params = client.get_collection(source).config.params.vectors
        if isinstance(params, dict) and vectors.IMAGE_VECTOR in params:
            config[vectors.IMAGE_VECTOR] = params[vectors.IMAGE_VECTOR]
    client.create_collection(collection_name=name, vectors_config=config)
    if QDRANT_URL:
        for field in ("user_id", "video_id"):
            client.create_payload_index(collection_name=name, field_name=field,
                                        field_schema=models.PayloadSchemaType.KEYWORD)
    return name


def collection_dim(model: str) -> int | None:
    """그 후보 창고의 글 좌표 차원. 창고가 없으면 None."""
    client = vectors.get_client()
    name = collection_for(embed_target(model))
    if not client.collection_exists(name):
        return None
    params = client.get_collection(name).config.params.vectors
    if isinstance(params, dict) and vectors.TEXT_VECTOR in params:
        return int(params[vectors.TEXT_VECTOR].size)
    return None


def build_index(
    model: str,
    user_id: str | None = None,
    video_id: str | None = None,
    recreate: bool = False,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """저장된 장면 카드를 그 후보 모델로 다시 색인합니다. 같은 장면은 같은 자리를 덮어씁니다.

    recreate=True 면 그 후보 창고를 비우고 새로 만듭니다(후보 모델을 바꿨을 때).
    """
    target = embed_target(model)
    info = embed_candidate_info(model)
    if not info["ready"]:
        raise RuntimeError(f"{info['label']}: 준비되지 않았습니다 — 없음: {', '.join(info['missing'])}")

    name = collection_for(target)
    report: dict[str, Any] = {"model": model, "collection": name, "videos": 0, "segments": 0,
                              "without_image": 0, "failed": []}
    with use_embedding(model):
        have, want = collection_dim(model), embedder.dim()
        if recreate and have is not None:
            log(f"  창고 {name} 를 비웁니다")
            vectors.drop_collection(target)
        elif have is not None and have != want:
            raise RuntimeError(
                f"창고 {name} 는 {have}차원인데 {info['label']} 은 {want}차원입니다. recreate 로 다시 만드세요."
            )
        _ensure_target(model, want)
        use_image = vectors.has_image_vector(target)

        for uid, vid, title in ready_videos(user_id, video_id):
            segments = db.list_segments(uid, vid)
            if not segments:
                continue
            try:
                text_vectors = embedder.encode([search_text(seg, title=title) for seg in segments])
            except Exception as exc:  # noqa: BLE001 - 한 영상이 실패해도 나머지는 계속합니다
                report["failed"].append({"video_id": vid, "error": f"{type(exc).__name__}: {exc}"[:200]})
                log(f"  실패 {vid}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            image_vectors = _source_image_vectors(uid, segments) if use_image else None
            if use_image and image_vectors is None:
                report["without_image"] += 1
            vectors.upsert_segments(uid, vid, title, segments, text_vectors, image_vectors, target)
            report["videos"] += 1
            report["segments"] += len(segments)
            log(f"  완료 {vid}  장면 {len(segments)}개")
    return report


def indexed_segments(model: str, user_id: str) -> int:
    """그 후보 창고에 이 사용자의 장면이 몇 개 들어 있는지. 창고가 없으면 0."""
    client = vectors.get_client()
    name = collection_for(embed_target(model))
    if not client.collection_exists(name):
        return 0
    from qdrant_client import models

    flt = models.Filter(must=[models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))])
    return int(client.count(collection_name=name, count_filter=flt, exact=True).count)


def status(user_id: str) -> list[dict[str, Any]]:
    """후보마다 설명 · 준비 상태 · 색인된 장면 수. 기본 조건의 장면 수와 같아야 공정한 비교입니다."""
    expected = sum(len(db.list_segments(uid, vid)) for uid, vid, _ in ready_videos(user_id))
    return [
        {**embed_candidate_info(model), "indexed_segments": indexed_segments(model, user_id),
         "expected_segments": expected}
        for model in EMBED_CANDIDATES
    ]


def search(user_id: str, query: str, model: str, **kwargs: Any) -> dict[str, Any]:
    """그 후보 모델의 색인으로 검색합니다. 인자와 돌려주는 모양은 server.search.search 와 같습니다.

    with_answer 의 기본값만 False 입니다(모델 비교에 답변은 필요 없고 Gemini 호출만 씁니다).
    색인이 없으면 NotIndexed — 빈 창고를 만들어 '결과 없음'으로 보이지 않게 합니다.
    """
    from server import search as search_mod

    target = embed_target(model)
    if not indexed_segments(model, user_id):
        raise NotIndexed(
            f"{EMBED_CANDIDATES[model]['label']} 색인이 없습니다. "
            f"python -m tools.build_embed_index --model {model} 로 먼저 만드세요."
        )
    kwargs.setdefault("with_answer", False)
    with use_embedding(model):
        result = search_mod.search(user_id, query, condition=target, **kwargs)
    result["embed_model"] = model
    return result
