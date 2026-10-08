"""단계 8 · 글 좌표. 담당 D.

- bge    : BAAI/bge-m3, 1024차원. 기존 졸업작품과 동일. (D 담당 목표)
- st     : sentence-transformers 로 도는 모델(ST_MODEL, 기본 KURE-v1). 한국어 특화 임베딩 비교용
- gemini : demo 기본값. 로컬 모델을 받지 않아 아무 노트북에서나 바로 됩니다.

.env 의 TEXT_EMBED_BACKEND 한 줄로 바꿉니다.
"""
from __future__ import annotations

import numpy as np

from server.config import BGE_MODEL, GEMINI_API_KEY, GEMINI_EMBED_DIM, GEMINI_EMBED_MODEL
from server.model_lock import MODEL_LOCK

_bge_model = None
# st 모델은 **이름별로** 따로 올립니다. 조건(local_v2 는 KURE-v1 고정)과 .env 의 ST_MODEL 이 다를 수 있어,
# 하나만 캐시하면 먼저 올라간 모델이 다른 조건의 좌표까지 만듭니다(차원이 같으면 오류도 없음).
_st_models: dict[str, object] = {}


def l2_normalize(arr: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(arr, axis=-1, keepdims=True)
    denom[denom == 0] = 1.0
    return arr / denom


def _backend() -> str:
    from server.config import backend

    return backend("text_embed")


def dim() -> int:
    if _backend() == "bge":
        return 1024
    if _backend() == "st":
        model = _st()
        # 새 sentence-transformers 는 get_embedding_dimension 이 정식 이름입니다(옛 이름은 FutureWarning).
        getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
        return int(getter())
    return GEMINI_EMBED_DIM


def _st_setting(key: str) -> str:
    """ST_MODEL · ST_QUERY_PREFIX · ST_DOC_PREFIX. 지금 조건이 정해 둔 값이 먼저, 없으면 .env 의 값."""
    from server import config

    return config.condition_setting(key, getattr(config, key))


def _st():
    name = _st_setting("ST_MODEL")
    model = _st_models.get(name)
    if model is None:
        with MODEL_LOCK:  # 동시에 두 영상을 저장해도 모델은 한 번만 올립니다
            model = _st_models.get(name)
            if model is None:
                from sentence_transformers import SentenceTransformer

                from server.config import DEVICE

                model = SentenceTransformer(name, device=DEVICE)
                # BGE 경로와 같은 길이로 자릅니다(공정한 비교). 장면 카드 글은 대부분 이보다 짧습니다.
                model.max_seq_length = 256
                _st_models[name] = model
    return model


def _encode_st(texts: list[str], is_query: bool) -> np.ndarray:
    prefix = _st_setting("ST_QUERY_PREFIX" if is_query else "ST_DOC_PREFIX")
    out = _st().encode([prefix + t for t in texts], batch_size=32, normalize_embeddings=True)
    return l2_normalize(np.asarray(out, dtype=np.float32))


def _encode_gemini(texts: list[str], task_type: str) -> np.ndarray:
    from google.genai import types

    from server.gemini import INTERACTIVE_RETRY, call_with_retry, get_client

    client = get_client()
    # 검색창 질문은 사람이 기다립니다. 저장 작업의 재시도(한 번에 최대 30초)를 그대로 쓰면 한도에 걸렸을 때
    # 검색이 1분 넘게 멈춥니다. 질문 좌표는 예전 재시도 시간(최대 약 14초) 안에서만 다시 부릅니다.
    retry = INTERACTIVE_RETRY if task_type == "RETRIEVAL_QUERY" else {}
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
            ),
            **retry,
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

        with MODEL_LOCK:  # 동시에 두 영상을 저장해도 모델은 한 번만 올립니다
            if _bge_model is None:
                _bge_model = BGEM3FlagModel(BGE_MODEL, use_fp16=DEVICE == "cuda", devices=DEVICE)
    out = _bge_model.encode(texts, batch_size=32, max_length=256)["dense_vecs"]
    return l2_normalize(np.asarray(out, dtype=np.float32))


def encode(texts: list[str], is_query: bool = False) -> np.ndarray:
    if not texts:
        return np.zeros((0, dim()), dtype=np.float32)
    if _backend() == "bge":
        return _encode_bge(texts)
    if _backend() == "st":
        return _encode_st(texts, is_query)
    return _encode_gemini(texts, "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT")


def is_ready() -> tuple[bool, str]:
    if _backend() in ("bge", "st"):
        return True, ""
    if not GEMINI_API_KEY:
        return False, "GEMINI_API_KEY 가 설정되지 않았습니다."
    return True, ""
