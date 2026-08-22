"""Gemini 연동 4곳이 실제로 도는지 확인합니다 (API 키 필요).

    python -m tools.check_gemini

확인 항목
    ① 장면 설명 + 화면 글자  — 프레임 6장을 한 장으로 붙여 1회 호출 (단계 5·6)
    ② 음성 받아적기          — wav 업로드 (단계 4, ASR_BACKEND=gemini 일 때)
    ③ 글 좌표                — embed_content (단계 8)
    ④ RAG 답변               — 찾은 장면만 근거로 답변 (단계 12)

②는 합성한 무음 오디오를 쓰므로 "빈 결과"가 정상입니다. 호출이 되는지만 봅니다.
실제 한국어 인식 정확도는 진짜 쇼츠로 저장해서 확인하세요.
"""
from __future__ import annotations

import math
import struct
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from server.config import DATA_DIR, GEMINI_API_KEY

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
]
SCENES = [
    ("감자 300g", (200, 160, 90)),
    ("양파 1개", (220, 220, 200)),
    ("간장 2큰술", (90, 60, 40)),
    ("팬에 볶기", (180, 120, 60)),
    ("물 200ml", (120, 160, 200)),
    ("완성!", (200, 90, 70)),
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def make_fake_frames(directory: Path) -> list[tuple[float, Path]]:
    """화면 자막이 박힌 가짜 쇼츠 프레임 6장."""
    directory.mkdir(parents=True, exist_ok=True)
    font = _font(56)
    frames = []
    for idx, (text, color) in enumerate(SCENES):
        image = Image.new("RGB", (540, 960), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle([60, 700, 480, 820], fill=(0, 0, 0))
        draw.text((90, 730), text, fill=(255, 255, 255), font=font)
        draw.ellipse([170, 300, 370, 500], fill=(255, 255, 255))
        path = directory / f"{idx * 4000:08d}.jpg"
        image.save(path, quality=85)
        frames.append((idx * 4.0, path))
    return frames


def make_silent_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        # 완전 무음이면 거부될 수 있어 아주 작은 톤을 넣습니다.
        frames = b"".join(
            struct.pack("<h", int(300 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(int(rate * seconds))
        )
        f.writeframes(frames)
    return path


def main() -> int:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY 가 없습니다. .env 를 확인하세요.")
        return 1

    workdir = DATA_DIR / "gemini_check"
    workdir.mkdir(parents=True, exist_ok=True)
    failures = 0

    # ① 장면 설명 + 화면 글자
    print("=" * 60)
    print("① 장면 설명 + 화면 글자 (프레임 6장 → 그리드 1장 → 호출 1회)")
    print("=" * 60)
    try:
        from server.pipeline.caption import describe
        from server.pipeline.frames import make_grid

        frames = make_fake_frames(workdir / "frames")
        (workdir / "grid.jpg").write_bytes(make_grid(frames))
        print(f"  그리드 저장: {workdir / 'grid.jpg'}  (눈으로 확인해보세요)")

        items = describe(frames)
        for item in items:
            print(f"  {item['time']:5.1f}s  자막:{item['ocr_text'] or '-':<12} 장면:{item['caption'] or '-'}")
        assert len(items) == len(frames), "칸 수와 응답 수가 안 맞습니다"
        read = sum(1 for i in items if i["ocr_text"])
        print(f"  → 화면 글자 인식 {read}/{len(frames)}칸")
        if read == 0:
            print("  ⚠ 자막을 하나도 못 읽었습니다. 실제 쇼츠로 다시 확인하고, 안 되면 OCR_BACKEND=easyocr")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  ❌ 실패: {type(exc).__name__}: {exc}")

    # ② 음성 받아적기
    print("\n" + "=" * 60)
    print("② 음성 받아적기 (합성 오디오 → 빈 결과가 정상)")
    print("=" * 60)
    try:
        from server.pipeline.transcribe import transcribe

        with tempfile.TemporaryDirectory() as tmp:
            wav = make_silent_wav(Path(tmp) / "test.wav")
            utterances = transcribe(wav)
        print(f"  호출 성공. 받아적은 구간 {len(utterances)}개 (합성음이라 0이 정상)")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  ❌ 실패: {type(exc).__name__}: {exc}")

    # ③ 글 좌표
    print("\n" + "=" * 60)
    print("③ 글 좌표 (embed_content)")
    print("=" * 60)
    try:
        import numpy as np

        from server import embedder

        docs = embedder.encode(["감자를 프라이팬에 넣는 장면", "빨래를 개는 장면"])
        query = embedder.encode(["뿌리채소를 팬에 넣는 부분"], is_query=True)[0]
        sims = docs @ query
        print(f"  차원: {docs.shape[1]}")
        print(f"  '뿌리채소를 팬에 넣는 부분' 과의 유사도: 감자 {sims[0]:.3f} / 빨래 {sims[1]:.3f}")
        assert sims[0] > sims[1], "뜻으로 찾는 검색이 동작하지 않습니다"
        print("  → 단어가 달라도 뜻으로 찾힙니다 (의미 검색 확인)")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  ❌ 실패: {type(exc).__name__}: {exc}")

    # ④ RAG 답변
    print("\n" + "=" * 60)
    print("④ RAG 답변 (찾은 장면만 근거로)")
    print("=" * 60)
    try:
        from server.search import generate_answer

        hits = [
            {
                "title": "감자조림",
                "start_time": 22.4,
                "end_time": 26.4,
                "asr_text": "이제 감자를 넣어줍니다",
                "ocr_text": "감자 300g",
                "caption": "손질된 감자를 프라이팬에 넣는 모습",
            }
        ]
        print("  질문: 감자 언제 넣어?")
        print(f"  답변: {generate_answer('감자 언제 넣어?', hits)}")
        print("  질문: 이 영상에 소고기 나와?  (context 에 없는 것)")
        print(f"  답변: {generate_answer('이 영상에 소고기 나와?', hits)}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"  ❌ 실패: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("모두 통과했습니다." if failures == 0 else f"{failures}개 항목이 실패했습니다.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
