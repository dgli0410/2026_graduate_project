"""단계 8 · 그림 좌표 (SigLIP2). 담당 D.

demo 기본값은 none 입니다. torch + transformers + peft 를 설치하지 않아도
글 좌표만으로 전 구간이 돕니다.

D 담당이 붙일 때:
    pip install torch transformers peft accelerate
    .env 에 IMAGE_EMBED_BACKEND=siglip
    (요리 도메인 LoRA 가 있으면 SIGLIP_ADAPTER=/path/to/adapter)

⚠ 백엔드를 바꾸면 Qdrant collection 을 다시 만들어야 합니다.
   벡터 구성이 달라지기 때문입니다.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from server.config import (
    SIGLIP_ADAPTER,
    SIGLIP_MODEL,
    SIGLIP_PROJECTION,
    SIGLIP_QUERY_TRANSLATE,
)
from server.embedder import l2_normalize
from server.model_lock import MODEL_LOCK

_model = None
_processor = None
_dim: int | None = None

_proj: tuple | None = None

# SigLIP 텍스트 탑은 마지막 토큰 자리를 문장 대표 벡터로 씁니다(위치 고정 pooling).
# 길이가 64 가 아니면 엉뚱한 자리를 집습니다. 반드시 명시합니다.
TEXT_MAX_LENGTH = 64

# SigLIP2 는 소문자로 토큰화해 학습됐고, 짧은 alt-text 형태에 맞춰져 있습니다.
# 소문자 + 템플릿으로 단계 구분 R@1 이 0.603 -> 0.664 올랐습니다(RecipeGen val).
# 이득의 대부분은 소문자화(0.661)이고 템플릿 문구 차이는 노이즈입니다
# ("a photo of" 0.664 / "a photo showing" 0.666). CLIP 문헌의 표준 형태를 씁니다.
TEXT_TEMPLATE = "a photo of {}"

# ⚠ 번역 캐시는 **디스크에 둡니다.** 프로세스 메모리에만 두면 서버를 껐다 켤 때마다
#   같은 질의가 다시 번역되고, 영어 문장이 조금만 달라져도 그림 좌표 점수가 통째로
#   바뀝니다. 실측: 서버 재시작 전후로 같은 질의의 "맞은 프레임"이 14.5초 -> 15.5초로
#   이동했습니다. 그 상태로 A/B 를 하면 개선 때문인지 번역 때문인지 구분할 수 없습니다.
_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "query_translations.json"
_translated: dict[str, str] | None = None


def _cache() -> dict[str, str]:
    global _translated
    if _translated is None:
        try:
            _translated = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 캐시가 없거나 깨졌으면 새로 시작합니다
            _translated = {}
    return _translated


def _remember(text: str, english: str) -> None:
    cache = _cache()
    cache[text] = english
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - 저장 실패가 검색을 막지 않습니다
        print(f"[image_embedder] 번역 캐시 저장 실패: {type(exc).__name__}")


def _to_english(text: str) -> str:
    """그림 좌표 전용. 실패하면 원문을 그대로 돌려줍니다.

    글 좌표(BGE/Gemini)는 한국어를 잘 하므로 거기서는 쓰지 않습니다.
    같은 질의는 캐시하므로 호출이 반복되지 않습니다.
    """
    if not SIGLIP_QUERY_TRANSLATE or not text.strip():
        return text
    cached = _cache().get(text)
    if cached:
        return cached
    try:
        from server.config import ANSWER_MODEL
        from server.gemini import get_client

        r = get_client().models.generate_content(
            model=ANSWER_MODEL,
            contents=(f"다음 한국어 요리 검색어를 영어로만 번역해. "
                      f"설명 없이 번역문만 출력해: {text}"),
        )
        out = (getattr(r, "text", "") or "").strip().splitlines()
        out = out[0].strip() if out else ""
        if out:
            _remember(text, out)
            return out
    except Exception as exc:  # noqa: BLE001 - 번역 실패가 검색을 막지 않습니다
        print(f"[image_embedder] 질의 번역 실패, 한국어 그대로 씁니다: {type(exc).__name__}")
    return text


def _projection():
    """proj_best.pt 가 있으면 (Pt, Pi) 를, 없으면 (None, None) 을 돌려줍니다."""
    global _proj
    if _proj is None:
        if not SIGLIP_PROJECTION or not Path(SIGLIP_PROJECTION).exists():
            _proj = (None, None)
        else:
            import torch

            w = torch.load(SIGLIP_PROJECTION, map_location="cpu")
            _proj = (w["Pt"].numpy().astype(np.float32), w["Pi"].numpy().astype(np.float32))
    return _proj


def _pooled(out):
    """transformers 버전에 따라 텐서 또는 출력 객체가 옵니다. 둘 다 받습니다."""
    if hasattr(out, "shape"):
        return out
    for attr in ("pooler_output", "image_embeds", "text_embeds"):
        value = getattr(out, attr, None)
        if value is not None:
            return value
    return out.last_hidden_state[:, -1]


def _backend() -> str:
    from server.config import backend

    return backend("image_embed")


def is_enabled() -> bool:
    return _backend() == "siglip"


def _load():
    if _model is not None:
        return _model, _processor
    with MODEL_LOCK:  # 동시에 두 영상을 저장해도 모델은 한 번만 올립니다
        if _model is None:
            _build()
    return _model, _processor


def _build() -> None:
    global _model, _processor
    import torch
    from transformers import AutoModel, AutoProcessor

    from server.config import DEVICE

    dtype = torch.float16 if DEVICE == "cuda" else torch.float32
    _processor = AutoProcessor.from_pretrained(SIGLIP_MODEL)
    base = AutoModel.from_pretrained(SIGLIP_MODEL, torch_dtype=dtype).to(DEVICE)
    if SIGLIP_ADAPTER:
        from peft import PeftModel

        base = PeftModel.from_pretrained(base, SIGLIP_ADAPTER)
    _model = base.eval()


def dim() -> int:
    global _dim
    if not is_enabled():
        return 0
    if _dim is None:
        _dim = int(encode_texts(["차원 확인"]).shape[-1])
    return _dim


def encode_images(paths: list[Path | str], batch_size: int = 16) -> np.ndarray:
    if not is_enabled() or not paths:
        return np.zeros((0, 0), dtype=np.float32)
    import torch
    from PIL import Image

    from server.config import DEVICE

    model, processor = _load()
    chunks: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(paths), batch_size):
            images = [Image.open(p).convert("RGB") for p in paths[i : i + batch_size]]
            batch = processor(images=images, return_tensors="pt").to(DEVICE)
            # naflex 는 pixel_attention_mask · spatial_shapes 를 함께 받아야
            # 위치 임베딩을 원본 비율에 맞게 보간합니다. pixel_values 만 넘기면 깨집니다.
            out = model.get_image_features(**batch)
            chunks.append(_pooled(out).float().cpu().numpy())
    vectors = np.vstack(chunks).astype(np.float32)
    _, p_image = _projection()
    if p_image is not None:
        vectors = vectors @ p_image.T
    return l2_normalize(vectors)


def encode_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    if not is_enabled() or not texts:
        return np.zeros((0, 0), dtype=np.float32)
    import torch

    from server.config import DEVICE

    model, processor = _load()
    texts = [TEXT_TEMPLATE.format(_to_english(t).strip().lower()) for t in texts]
    chunks: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = processor(
                text=texts[i : i + batch_size],
                padding="max_length",
                max_length=TEXT_MAX_LENGTH,
                truncation=True,
                return_tensors="pt",
            ).to(DEVICE)
            inputs = {
                k: v for k, v in batch.items()
                if "pixel" not in k and k != "spatial_shapes"
            }
            out = model.get_text_features(**inputs)
            chunks.append(_pooled(out).float().cpu().numpy())
    vectors = np.vstack(chunks).astype(np.float32)
    p_text, _ = _projection()
    if p_text is not None:
        vectors = vectors @ p_text.T
    return l2_normalize(vectors)
