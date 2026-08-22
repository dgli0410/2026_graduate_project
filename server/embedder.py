"""단계 8 · 글 좌표. 담당 D.

- bge    : BAAI/bge-m3, 1024차원. 기존 졸업작품과 동일. (D 담당 목표)
- gemini : demo 기본값. 로컬 모델을 받지 않아 아무 노트북에서나 바로 됩니다.

.env 의 TEXT_EMBED_BACKEND 한 줄로 바꿉니다.
"""
from __future__ import annotations

import numpy as np

from server.config import BGE_MODEL, GEMINI_API_KEY, GEMINI_EMBED_DIM, GEMINI_EMBED_MODEL

_bge_model = None


def l2_normalize(arr: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(arr, axis=-1, keepdims=True)
    denom[denom == 0] = 1.0
    return arr / denom


def _backend() -> str:
    from server.config import TEXT_EMBED_BACKEND

    return TEXT_EMBED_BACKEND


def dim() -> int:
    return 1024 if _backend() == "bge" else GEMINI_EMBED_DIM


def _encode_gemini(texts: list[str], task_type: str) -> np.ndarray:
    from google.genai import types

    from server.gemini import get_client

    client = get_client()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), 32):  # 배치 상한이 있어 나눠 호출합니다
        response = client.models.embed_content(
            model=GEMINI_EMBED_MODEL,
            contents=texts[i : i + 32],
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=GEMINI_EMBED_DIM
            ),
        )
        vectors.extend(list(e.values) for e in response.embeddings)
    # 기본 차원이 아닐 때는 정규화가 보장되지 않으므로 직접 합니다.
    return l2_normalize(np.asarray(vectors, dtype=np.float32))


def _encode_bge(texts: list[str]) -> np.ndarray:
    global _bge_model
    if _bge_model is None:
        from FlagEmbedding import BGEM3FlagModel

        from server.config import DEVICE

        _bge_model = BGEM3FlagModel(BGE_MODEL, use_fp16=DEVICE == "cuda", devices=DEVICE)
    out = _bge_model.encode(texts, batch_size=32, max_length=256)["dense_vecs"]
    return l2_normalize(np.asarray(out, dtype=np.float32))


def encode(texts: list[str], is_query: bool = False) -> np.ndarray:
    if not texts:
        return np.zeros((0, dim()), dtype=np.float32)
    if _backend() == "bge":
        return _encode_bge(texts)
    return _encode_gemini(texts, "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT")


def is_ready() -> tuple[bool, str]:
    if _backend() == "bge":
        return True, ""
    if not GEMINI_API_KEY:
        return False, "GEMINI_API_KEY 가 설정되지 않았습니다."
    return True, ""
