"""Qdrant 창고. 마스터 문서의 "의미로 찾아주는 검색 창고".

한 장면 카드에 **두 좌표가 같이 붙습니다**(단계 8).
- text  : BGE-M3 (또는 demo 의 Gemini embedding)
- image : SigLIP2 — IMAGE_EMBED_BACKEND=siglip 일 때만 만들어집니다

demo 는 로컬 파일 모드로 돕니다(도커 불필요).
.env 에 QDRANT_URL 을 넣으면 Qdrant Cloud 로 그대로 붙습니다.

** user_id 필터가 이 파일의 핵심입니다. **
마스터 문서 단계 12-4 "이 사용자가 저장한 영상만" 에 해당합니다.
검색과 삭제 모두 user_id 없이는 호출할 수 없게 만들어 두었습니다.
"""
from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

import numpy as np
from qdrant_client import QdrantClient, models

from server import embedder, image_embedder
from server.config import (
    DEFAULT_CONDITION,
    QDRANT_API_KEY,
    QDRANT_PATH,
    QDRANT_URL,
    collection_for,
)

TEXT_VECTOR = "text"
IMAGE_VECTOR = "image"


def _collection(condition: str | None) -> str:
    return collection_for(condition or DEFAULT_CONDITION)


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    if QDRANT_URL:
        return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(QDRANT_PATH))


def ensure_collection(condition: str | None = None) -> str:
    """조건별 collection 을 만듭니다. 좌표 차원이 달라서 조건마다 분리합니다."""
    name = _collection(condition)
    client = get_client()
    if client.collection_exists(name):
        return name
    config = {
        TEXT_VECTOR: models.VectorParams(size=embedder.dim(), distance=models.Distance.COSINE)
    }
    if image_embedder.is_enabled():
        config[IMAGE_VECTOR] = models.VectorParams(
            size=image_embedder.dim(), distance=models.Distance.COSINE
        )
    client.create_collection(collection_name=name, vectors_config=config)
    if QDRANT_URL:
        # payload index 는 서버 모드에서만 의미가 있습니다(로컬 모드는 경고만 냅니다).
        for field in ("user_id", "video_id"):
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
    return name


def collection_exists(condition: str | None = None) -> bool:
    return get_client().collection_exists(_collection(condition))


def has_image_vector(condition: str | None = None) -> bool:
    """그 조건의 collection 이 그림 좌표를 갖고 있는지."""
    name = _collection(condition)
    if not get_client().collection_exists(name):
        return False
    info = get_client().get_collection(name)
    vectors_config = info.config.params.vectors
    return isinstance(vectors_config, dict) and IMAGE_VECTOR in vectors_config


def _point_id(user_id: str, segment_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"shorts-vault:{user_id}:{segment_id}"))


def _user_filter(
    user_id: str, video_id: str | None = None, video_ids: list[str] | None = None
) -> models.Filter:
    must: list[Any] = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
    ]
    if video_id:
        must.append(models.FieldCondition(key="video_id", match=models.MatchValue(value=video_id)))
    elif video_ids is not None:
        must.append(models.FieldCondition(key="video_id", match=models.MatchAny(any=video_ids)))
    return models.Filter(must=must)


def upsert_segments(
    user_id: str,
    video_id: str,
    title: str,
    segments: list[dict[str, Any]],
    text_vectors: np.ndarray,
    image_vectors: np.ndarray | None = None,
    condition: str | None = None,
) -> None:
    name = ensure_collection(condition)
    use_image = image_vectors is not None and len(image_vectors) == len(segments)

    points = []
    for idx, segment in enumerate(segments):
        vector: dict[str, list[float]] = {
            TEXT_VECTOR: np.asarray(text_vectors[idx], dtype=np.float32).tolist()
        }
        if use_image:
            vector[IMAGE_VECTOR] = np.asarray(image_vectors[idx], dtype=np.float32).tolist()
        points.append(
            models.PointStruct(
                id=_point_id(user_id, segment["segment_id"]),
                vector=vector,
                payload={
                    "user_id": user_id,
                    "video_id": video_id,
                    "segment_id": segment["segment_id"],
                    "title": title,
                    "start_time": float(segment["start_time"]),
                    "end_time": float(segment["end_time"]),
                    "asr_text": segment.get("asr_text", ""),
                    "ocr_text": segment.get("ocr_text", ""),
                    "caption": segment.get("caption", ""),
                    "frame_path": segment.get("frame_path", ""),
                },
            )
        )
    if points:
        get_client().upsert(collection_name=name, points=points)


def search(
    user_id: str,
    query_vector: np.ndarray,
    using: str = TEXT_VECTOR,
    top_n: int = 40,
    video_id: str | None = None,
    video_ids: list[str] | None = None,
    condition: str | None = None,
) -> list[dict[str, Any]]:
    name = ensure_collection(condition)
    if video_ids is not None and not video_ids:
        return []  # 빈 재생목록: 찾을 대상이 없습니다.
    hits = get_client().query_points(
        collection_name=name,
        query=np.asarray(query_vector, dtype=np.float32).tolist(),
        using=using,
        query_filter=_user_filter(user_id, video_id, video_ids),
        limit=top_n,
        with_payload=True,
    ).points
    return [{"score": float(h.score), **dict(h.payload or {})} for h in hits]


def delete_video(user_id: str, video_id: str, condition: str | None = None) -> None:
    """마스터 문서 단계 11: 표·창고·이미지 3곳 중 '창고' 담당."""
    name = _collection(condition)
    client = get_client()
    if not client.collection_exists(name):
        return
    client.delete(
        collection_name=name,
        points_selector=models.FilterSelector(filter=_user_filter(user_id, video_id)),
    )


def delete_video_all_conditions(user_id: str, video_id: str) -> None:
    """영상을 지울 때는 모든 조건의 창고에서 지워야 합니다."""
    from server.config import CONDITIONS

    for condition in CONDITIONS:
        delete_video(user_id, video_id, condition)
