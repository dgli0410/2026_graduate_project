"""Demo 설정. 모든 값은 .env 로 덮어쓸 수 있습니다.

⚠ 마스터 문서 8장: device 설정은 반드시 이 파일 한 곳에만 둡니다.
코드 곳곳에 "cuda" 를 박아넣으면 GPU 도착 후 교체에 2주가 걸립니다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _flag(name: str, default: str) -> str:
    return os.getenv(name, default).strip().lower()


# --- 장치 (여기 한 곳만 고칩니다) -------------------------------------------
DEVICE = _flag("DEVICE", "cpu")  # cpu | cuda
COMPUTE_TYPE = _flag("COMPUTE_TYPE", "int8" if DEVICE == "cpu" else "float16")

# --- Gemini ---------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# 모델을 고를 때는 tools/check_models.py 로 응답 속도를 먼저 재보세요.
# 최신 모델일수록 붐벼서 503(일시 과부하)이 잦습니다.
CAPTION_MODEL = os.getenv("CAPTION_MODEL", "gemini-3.5-flash")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gemini-3.5-flash-lite")
# 마스터 문서 단계 6 / 12장: 영상 1개당 Gemini 호출 상한을 코드로 강제합니다.
MAX_GEMINI_CALLS_PER_VIDEO = int(os.getenv("MAX_GEMINI_CALLS_PER_VIDEO", "3"))
GRID_FRAMES = int(os.getenv("GRID_FRAMES", "6"))  # 그리드 한 장에 넣을 프레임 수

# --- 파이프라인 백엔드 -----------------------------------------------------
# 각 단계는 "진짜 모델"과 "demo 대체품"을 같은 인터페이스로 갖습니다.
# 담당자가 진짜 모델을 붙이면 .env 한 줄만 바꾸면 됩니다.
ASR_BACKEND = _flag("ASR_BACKEND", "gemini")  # whisperx | gemini   (C 담당)
OCR_BACKEND = _flag("OCR_BACKEND", "gemini")  # easyocr  | gemini   (C 담당)
TEXT_EMBED_BACKEND = _flag("TEXT_EMBED_BACKEND", "gemini")  # bge | gemini  (D 담당)
IMAGE_EMBED_BACKEND = _flag("IMAGE_EMBED_BACKEND", "none")  # siglip | none (D 담당)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
BGE_MODEL = os.getenv("BGE_MODEL", "BAAI/bge-m3")
SIGLIP_MODEL = os.getenv("SIGLIP_MODEL", "google/siglip2-base-patch16-224")
SIGLIP_ADAPTER = os.getenv("SIGLIP_ADAPTER", "").strip()  # 요리 도메인 LoRA 경로
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_EMBED_DIM = int(os.getenv("GEMINI_EMBED_DIM", "768"))

# --- 추출 파라미터 (확장프로그램이 서버에서 받아갑니다) ---------------------
FRAME_INTERVAL = float(os.getenv("FRAME_INTERVAL", "0.5"))  # 초당 2장
FRAME_WIDTH = int(os.getenv("FRAME_WIDTH", "540"))
FRAME_HEIGHT = int(os.getenv("FRAME_HEIGHT", "960"))
FRAME_QUALITY = float(os.getenv("FRAME_QUALITY", "0.75"))
FRAME_DEDUP_THRESHOLD = float(os.getenv("FRAME_DEDUP_THRESHOLD", "0.02"))

# --- 장면 카드 / 검색 ------------------------------------------------------
SEGMENT_SECONDS = float(os.getenv("SEGMENT_SECONDS", "4.0"))
SEEK_LEAD_SECONDS = float(os.getenv("SEEK_LEAD_SECONDS", "1.5"))
SEARCH_TOP_K = int(os.getenv("SEARCH_TOP_K", "5"))
DEDUP_OVERLAP_SECONDS = float(os.getenv("DEDUP_OVERLAP_SECONDS", "2.0"))

# --- 저장소 ---------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "vault.db"
MEDIA_DIR = DATA_DIR / "media"
QDRANT_PATH = DATA_DIR / "qdrant"
QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "shorts_segments")

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================================
# 조건(condition) — 같은 프레임·오디오에 백엔드만 갈아끼워 비교하기 위한 장치
# =========================================================================
# 실험의 기본 원칙: 입력이 같아야 차이가 모델 차이입니다.
# 캡처된 프레임/오디오는 디스크에 그대로 있으므로, 아래 조건만 바꿔 다시 돌립니다.

CONDITIONS: dict[str, dict] = {
    "gemini": {
        "label": "Gemini API",
        "description": "음성·자막·장면설명·좌표를 모두 Gemini 로 처리합니다.",
        "backends": {
            "asr": "gemini",
            "ocr": "gemini",
            "text_embed": "gemini",
            "image_embed": "none",
        },
        "requires": [],
    },
    "local": {
        "label": "로컬 모델",
        "description": "WhisperX + EasyOCR + BGE-M3 + SigLIP2. 장면 설명만 Gemini 를 씁니다.",
        "backends": {
            "asr": "whisperx",
            "ocr": "easyocr",
            "text_embed": "bge",
            "image_embed": "siglip",
        },
        "requires": ["whisperx", "easyocr", "FlagEmbedding", "torch", "transformers", "peft"],
    },
}
DEFAULT_CONDITION = "gemini"

# 조건별 Qdrant collection. 좌표 차원이 달라서 반드시 분리해야 합니다.
# 기본 조건은 기존 collection 을 그대로 써서 데모 데이터가 유지됩니다.
_BACKEND_DEFAULTS = {
    "asr": ASR_BACKEND,
    "ocr": OCR_BACKEND,
    "text_embed": TEXT_EMBED_BACKEND,
    "image_embed": IMAGE_EMBED_BACKEND,
}

_active_backends: ContextVar[dict[str, str] | None] = ContextVar("active_backends", default=None)


def backend(kind: str) -> str:
    """지금 이 작업이 써야 할 백엔드 이름. 조건 실행 중이면 그 조건 값을 씁니다."""
    override = _active_backends.get()
    if override and kind in override:
        return override[kind]
    return _BACKEND_DEFAULTS[kind]


@contextmanager
def use_condition(name: str):
    """이 블록 안에서만 백엔드를 조건에 맞게 바꿉니다.

    ContextVar 이라 스레드/태스크마다 독립적입니다. 두 조건을 동시에 돌려도
    서로 섞이지 않습니다.
    """
    if name not in CONDITIONS:
        raise ValueError(f"모르는 조건입니다: {name}. {list(CONDITIONS)} 중에서 고르세요.")
    token = _active_backends.set(dict(CONDITIONS[name]["backends"]))
    try:
        yield
    finally:
        _active_backends.reset(token)


def collection_for(condition: str) -> str:
    """조건별 Qdrant collection 이름."""
    if condition == DEFAULT_CONDITION:
        return QDRANT_COLLECTION  # 데모가 쓰던 것 그대로
    return f"{QDRANT_COLLECTION}__{condition}"


def condition_info(name: str) -> dict:
    """조건 하나의 설명 + 필요한 패키지가 깔려 있는지."""
    import importlib.util

    spec = CONDITIONS[name]
    missing = [
        package
        for package in spec["requires"]
        if importlib.util.find_spec(package.replace("-", "_")) is None
    ]
    return {
        "name": name,
        "label": spec["label"],
        "description": spec["description"],
        "backends": spec["backends"],
        "ready": not missing,
        "missing": missing,
        "install_hint": f"pip install {' '.join(missing)}" if missing else "",
        "collection": collection_for(name),
    }


def capture_settings() -> dict:
    """확장프로그램이 /api/health 로 받아가는 추출 설정."""
    return {
        "frame_interval": FRAME_INTERVAL,
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
        "frame_quality": FRAME_QUALITY,
        "dedup_threshold": FRAME_DEDUP_THRESHOLD,
    }
