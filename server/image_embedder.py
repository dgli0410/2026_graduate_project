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

from pathlib import Path

import numpy as np

from server.config import SIGLIP_ADAPTER, SIGLIP_MODEL
from server.embedder import l2_normalize

_model = None
_processor = None
_dim: int | None = None


def _backend() -> str:
    from server.config import IMAGE_EMBED_BACKEND

    return IMAGE_EMBED_BACKEND


def is_enabled() -> bool:
    return _backend() == "siglip"


def _load():
    global _model, _processor
    if _model is not None:
        return _model, _processor
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
    return _model, _processor


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
            out = model.vision_model(pixel_values=batch["pixel_values"])
            chunks.append(out.pooler_output.float().cpu().numpy())
    return l2_normalize(np.vstack(chunks).astype(np.float32))


def encode_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    if not is_enabled() or not texts:
        return np.zeros((0, 0), dtype=np.float32)
    import torch

    from server.config import DEVICE

    model, processor = _load()
    chunks: list[np.ndarray] = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = processor(
                text=texts[i : i + batch_size], padding="max_length", return_tensors="pt"
            ).to(DEVICE)
            inputs = {k: v for k, v in batch.items() if "pixel" not in k}
            out = model.text_model(**inputs)
            chunks.append(out.pooler_output.float().cpu().numpy())
    return l2_normalize(np.vstack(chunks).astype(np.float32))
