"""단계 5 · 화면 글자 읽기. 담당 C.

쇼츠는 정보의 상당 부분이 화면 자막에 있습니다. 말로 안 하고 자막으로만
알려주는 경우가 많아서 이 단계의 기여도가 롱폼보다 큽니다(9장 실험 항목).

백엔드:
- easyocr : 진짜. 모든 프레임을 읽습니다. (C 담당 목표)
- gemini  : demo 기본값. 캡션용 그리드 호출에서 함께 받아옵니다.
            → 별도 호출이 없으므로 Gemini 호출 상한을 넘지 않습니다.
"""
from __future__ import annotations

from pathlib import Path

from server.config import backend

_reader = None


def uses_grid() -> bool:
    """True 면 caption.describe() 가 반환한 ocr_text 를 그대로 씁니다."""
    return backend("ocr") != "easyocr"


def recognize_all(frames: list[tuple[float, Path]]) -> list[dict]:
    """모든 프레임에서 화면 글자를 읽습니다 -> [{time, text}, ...]

    gemini 백엔드일 때는 빈 리스트를 돌려주고, 그리드 결과를 대신 씁니다.
    """
    if uses_grid():
        return []
    return _recognize_easyocr(frames)


def _recognize_easyocr(frames: list[tuple[float, Path]]) -> list[dict]:
    global _reader
    import easyocr

    from server.config import DEVICE

    if _reader is None:
        # verbose=False: 첫 실행 때 뜨는 다운로드 진행바(█)가 한글 윈도우 콘솔(cp949)에서
        # UnicodeEncodeError 로 죽습니다. 서버 로그에 진행바는 필요 없습니다.
        _reader = easyocr.Reader(["ko", "en"], gpu=DEVICE == "cuda", verbose=False)

    results: list[dict] = []
    for time_sec, path in frames:
        try:
            lines = _reader.readtext(str(path), detail=0)
        except Exception:  # noqa: BLE001 - 프레임 하나가 실패해도 계속합니다
            continue
        text = " ".join(str(line).strip() for line in lines if str(line).strip())
        if text:
            results.append({"time": time_sec, "text": text})
    return results
