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
    from server.config import backend

    return backend("text_embed")


def dim() -> int:
    return 1024 if _backend() == "bge" else GEMINI_EMBED_DIM


def _encode_gemini(texts: list[str], task_type: str) -> np.ndarray:
    from google.genai import types

    from server.gemini import call_with_retry, get_client

    client = get_client()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), 32):  # 배치 상한이 있어 나눠 호출합니다
        # ⚠ 문자열 리스트를 그대로 넘기면 gemini-embedding-2 는 그것을 "한 문서의 여러
        #   조각"으로 보고 좌표를 1개만 돌려줍니다. Content 로 하나씩 감싸야 문서 수만큼
        #   나옵니다. (gemini-embedding-001 은 문자열 리스트도 문서별로 돌려줬습니다)
        batch = [types.Content(parts=[types.Part(text=t)]) for t in texts[i : i + 32]]
        # 캡션·음성과 같은 재시도를 여기에도 겁니다. 이게 없으면 임베딩이 429 한 번에
        # 저장 전체가 실패합니다(단계 8 이 죽으면 장면 카드가 창고에 못 들어감).
        response = call_with_retry(
            lambda b=batch: client.models.embed_content(
                model=GEMINI_EMBED_MODEL,
                contents=b,
                config=types.EmbedContentConfig(
                    task_type=task_type, output_dimensionality=GEMINI_EMBED_DIM
                ),
            )
        )
        vectors.extend(list(e.values) for e in response.embeddings)
    if len(vectors) != len(texts):
        # 조용히 넘어가면 좌표와 장면 카드의 짝이 어긋난 채 창고에 들어갑니다.
        raise RuntimeError(
            f"{GEMINI_EMBED_MODEL}: 글 {len(texts)}개를 보냈는데 좌표가 {len(vectors)}개 왔습니다."
        )
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
