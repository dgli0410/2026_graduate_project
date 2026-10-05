"""단계 5 · 화면 글자 읽기. 담당 C.

쇼츠는 정보의 상당 부분이 화면 자막에 있습니다. 말로 안 하고 자막으로만
알려주는 경우가 많아서 이 단계의 기여도가 롱폼보다 큽니다(9장 실험 항목).

백엔드:
- easyocr : 진짜. 모든 프레임을 읽습니다. (C 담당 목표)
- gemini  : demo 기본값. 캡션용 그리드 호출에서 함께 받아옵니다.
            → 별도 호출이 없으므로 Gemini 호출 상한을 넘지 않습니다.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from server.config import backend

_reader = None

# 이 아래는 배경 무늬·로고 조각이 대부분입니다. 실측 쓰레기 예: "C다 'F60\" 1' ;;} S다a"
# 0.5 로 잡으면 작은 한글 자막까지 날아갑니다(실측: 된장찌개 영상의 "양파 반 개",
# "파 1/2대", "청고추 2개" 가 전부 사라졌습니다). 낮게 잡고 내용으로 거릅니다.
MIN_CONFIDENCE = 0.35


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


def _normalize(text: str) -> str:
    """비교용 열쇠. 오인식으로 끼어든 기호·공백 차이를 무시합니다."""
    return re.sub(r"[^0-9a-z가-힣]", "", str(text).lower())


def _is_meaningful(key: str) -> bool:
    """글자처럼 생겼는지. 신뢰도만으로는 로고 파편이 안 걸러집니다.

    실측: 채널 로고가 잘려 "jc", "jic", "ral", "e3" 로 읽히며 카드마다 남았습니다.
    한글이 하나도 없고 숫자도 없는 짧은 라틴 문자열은 자막일 수 없습니다.
    "600ml", "350ml" 같은 분량 표기는 숫자가 있어 통과합니다.
    """
    if len(key) < 2:
        return False
    if re.search(r"[가-힣]", key):
        return True
    return bool(re.search(r"\d", key)) and len(key) >= 3


def _cell(box, size: int = 48) -> tuple[int, int]:
    """글자 상자의 화면상 위치를 칸으로 뭉갭니다. 워터마크 판정에 씁니다."""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return (int((min(xs) + max(xs)) / 2 // size), int((min(ys) + max(ys)) / 2 // size))


def _drop_static_overlays(per_frame: list[tuple[float, list[tuple]]]) -> list[tuple[float, list[str]]]:
    """영상 내내 같은 자리에 같은 글자가 떠 있는 것(채널 로고 등)을 지웁니다.

    쇼츠는 채널 워터마크가 모든 프레임에 찍힙니다. 그대로 두면 **모든 장면 카드가
    같은 글자를 공유**해서, 글 좌표와 단어 매칭이 전부 그 글자에 끌려갑니다.
    실측: 파스타 영상에서 채널 로고가 "J희오늘딪먹지8 / J의오늘되업지 / J희오늘딪목지8"
    처럼 매번 다르게 읽히며 카드마다 5회씩 들어갔습니다.

    위치만 보면 안 됩니다 — 쇼츠 자막도 보통 같은 높이에 뜹니다. 그래서
    **같은 칸에 계속 나오면서 글자까지 비슷한** 경우만 지웁니다. 로고는 오인식이
    섞여도 서로 닮았고(비슷도 0.5 이상), 자막은 내용이 바뀌어 닮지 않습니다.
    """
    total = len(per_frame)
    if total < 5:
        return [(t, [b[1] for b in boxes]) for t, boxes in per_frame]

    seen_in: dict[tuple[int, int], list[str]] = {}
    frames_with: dict[tuple[int, int], set[int]] = {}
    for i, (_, boxes) in enumerate(per_frame):
        for box, text, _conf in boxes:
            key = _cell(box)
            seen_in.setdefault(key, []).append(_normalize(text))
            frames_with.setdefault(key, set()).add(i)

    static: set[tuple[int, int]] = set()
    for key, texts in seen_in.items():
        if len(frames_with[key]) < total * 0.8:
            continue
        modal = Counter(texts).most_common(1)[0][0]
        if not modal:
            continue
        similarity = sum(SequenceMatcher(None, modal, t).ratio() for t in texts) / len(texts)
        if similarity >= 0.5:
            static.add(key)

    return [
        (t, [text for box, text, _c in boxes if _cell(box) not in static])
        for t, boxes in per_frame
    ]


def _recognize_easyocr(frames: list[tuple[float, Path]]) -> list[dict]:
    global _reader
    import easyocr

    from server.config import DEVICE

    if _reader is None:
        # verbose=False: 첫 실행 때 뜨는 다운로드 진행바(█)가 한글 윈도우 콘솔(cp949)에서
        # UnicodeEncodeError 로 죽습니다. 서버 로그에 진행바는 필요 없습니다.
        _reader = easyocr.Reader(["ko", "en"], gpu=DEVICE == "cuda", verbose=False)

    per_frame: list[tuple[float, list[tuple]]] = []
    for time_sec, path in frames:
        try:
            # detail=1 로 신뢰도와 위치를 함께 받습니다. detail=0 으로 글자만 받으면
            # 배경 무늬에서 나온 쓰레기("C다 'F60\" 1' ;;}")를 걸러낼 방법이 없습니다.
            boxes = _reader.readtext(str(path))
        except Exception:  # noqa: BLE001 - 프레임 하나가 실패해도 계속합니다
            continue
        picked: list[tuple] = []
        seen: set[str] = set()
        for box, text, confidence in boxes:
            text = str(text).strip()
            key = _normalize(text)
            if float(confidence) < MIN_CONFIDENCE or not _is_meaningful(key) or key in seen:
                continue
            seen.add(key)
            picked.append((box, text, float(confidence)))
        per_frame.append((time_sec, picked))

    results: list[dict] = []
    for time_sec, lines in _drop_static_overlays(per_frame):
        text = " ".join(lines)
        if text:
            results.append({"time": time_sec, "text": text})
    return results
