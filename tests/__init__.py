"""자동 테스트. 외부 AI 호출 · 모델 다운로드 없이 돕니다.

    python -m unittest discover -s tests -t .

server.config 가 import 시점에 DATA_DIR 을 만들기 때문에, 테스트가 실제 data/ 를 건드리지 않도록 **가장 먼저**
임시 폴더로 돌려 둡니다. server/config.py 가 읽는 나머지 환경 변수도 코드 기본값으로 못 박습니다(TEST_ENV).
개발자의 .env · 셸에 무엇이 있든 테스트는 같은 설정에서 돕니다. 특히:
- QDRANT_URL · QDRANT_API_KEY: 팀이 같이 쓰는 Qdrant 주소가 .env 에 있으면 테스트가 그 창고에 씁니다.
- 백엔드 · 모델: 로컬 백엔드가 .env 에 있으면 테스트가 진짜 모델을 올리거나 내려받으려 합니다.
load_dotenv 는 이미 있는 환경 변수를 덮어쓰지 않으므로 .env 보다 이 값이 이깁니다.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="shorts-vault-test-")

# server/config.py 가 읽는 환경 변수 → 테스트 값. DATA_DIR · GEMINI_API_KEY 말고는 코드 기본값과 같습니다.
TEST_ENV: dict[str, str] = {
    "DATA_DIR": _TMP,
    "GEMINI_API_KEY": "",
    "DEVICE": "cpu",
    "COMPUTE_TYPE": "int8",
    "CAPTION_MODEL": "gemini-3.5-flash",
    "ANSWER_MODEL": "gemini-3.5-flash-lite",
    "MAX_GEMINI_CALLS_PER_VIDEO": "1",
    "GRID_FRAMES": "12",
    "ASR_BACKEND": "gemini",
    "OCR_BACKEND": "gemini",
    "TEXT_EMBED_BACKEND": "gemini",
    "IMAGE_EMBED_BACKEND": "none",
    "WHISPER_MODEL": "small",
    "BGE_MODEL": "BAAI/bge-m3",
    "SIGLIP_MODEL": "google/siglip2-base-patch16-naflex",
    "SIGLIP_ADAPTER": "",
    "SIGLIP_PROJECTION": "",
    "SIGLIP_QUERY_TRANSLATE": "false",
    "GEMINI_EMBED_MODEL": "gemini-embedding-001",
    "GEMINI_EMBED_DIM": "768",
    "FRAME_INTERVAL": "0.5",
    "FRAME_WIDTH": "540",
    "FRAME_HEIGHT": "960",
    "FRAME_QUALITY": "0.75",
    "FRAME_DEDUP_THRESHOLD": "0.02",
    "SEGMENT_SECONDS": "4.0",
    "SEEK_LEAD_SECONDS": "1.5",
    "SEARCH_TOP_K": "5",
    "LEXICAL_IDF": "true",
    "DEDUP_OVERLAP_SECONDS": "2.0",
    "QDRANT_URL": "",
    "QDRANT_API_KEY": "",
    "QDRANT_COLLECTION": "shorts_segments",
}
os.environ.update(TEST_ENV)


def _close_qdrant() -> None:
    """로컬 Qdrant 를 인터프리터 종료 전에 닫습니다(종료 중 소멸자 경고 방지)."""
    try:
        from server import vectors

        if vectors.get_client.cache_info().currsize:
            vectors.get_client().close()
    except Exception:  # noqa: BLE001
        pass


import atexit  # noqa: E402

atexit.register(_close_qdrant)
