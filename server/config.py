"""Demo 설정. 모든 값은 .env 로 덮어쓸 수 있습니다.

⚠ 마스터 문서 8장: device 설정은 반드시 이 파일 한 곳에만 둡니다.
코드 곳곳에 "cuda" 를 박아넣으면 GPU 도착 후 교체에 2주가 걸립니다.
"""
from __future__ import annotations

import os
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

# --- 네이버쇼핑 검색 API (선택) ---------------------------------------------
# 없어도 됩니다. 키가 있으면 재료 구매 링크에 최저가가 붙습니다.
# 발급: https://developers.naver.com/apps → 애플리케이션 등록 → "검색" API (무료)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()

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


def capture_settings() -> dict:
    """확장프로그램이 /api/health 로 받아가는 추출 설정."""
    return {
        "frame_interval": FRAME_INTERVAL,
        "frame_width": FRAME_WIDTH,
        "frame_height": FRAME_HEIGHT,
        "frame_quality": FRAME_QUALITY,
        "dedup_threshold": FRAME_DEDUP_THRESHOLD,
    }
