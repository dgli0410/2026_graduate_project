"""대표 프레임 고르기 + 그리드 만들기.

마스터 문서 단계 6 / 12장: 프레임 하나하나 Gemini 에 물어보면 영상당 120번
호출하게 됩니다. **6장을 한 장으로 붙여서** 물어보고, 영상당 호출을
MAX_GEMINI_CALLS_PER_VIDEO 회로 코드에서 강제합니다.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from server.config import GRID_FRAMES, MAX_GEMINI_CALLS_PER_VIDEO

TILE_WIDTH = 270
TILE_HEIGHT = 480
GRID_COLS = 3


def max_key_frames() -> int:
    """Gemini 호출 상한에서 역산한 대표 프레임 개수."""
    return GRID_FRAMES * MAX_GEMINI_CALLS_PER_VIDEO


def select_key_frames(frames: list[tuple[float, Path]]) -> list[tuple[float, Path]]:
    """전체 프레임에서 시간축으로 고르게 대표 프레임을 뽑습니다."""
    limit = max_key_frames()
    if len(frames) <= limit:
        return list(frames)
    step = len(frames) / limit
    return [frames[min(len(frames) - 1, int(i * step))] for i in range(limit)]


def chunk(frames: list[tuple[float, Path]], size: int = GRID_FRAMES):
    for i in range(0, len(frames), size):
        yield frames[i : i + size]


def make_grid(frames: list[tuple[float, Path]]) -> bytes:
    """프레임 여러 장을 한 장의 격자 이미지로 붙이고 번호를 찍습니다.

    번호를 찍어야 Gemini 응답의 index 와 실제 프레임을 짝지을 수 있습니다.
    """
    cols = min(GRID_COLS, len(frames))
    rows = (len(frames) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * TILE_WIDTH, rows * TILE_HEIGHT), (16, 16, 16))
    draw = ImageDraw.Draw(canvas)

    for idx, (_, path) in enumerate(frames):
        with Image.open(path) as img:
            tile = img.convert("RGB").resize((TILE_WIDTH, TILE_HEIGHT))
        x = (idx % cols) * TILE_WIDTH
        y = (idx // cols) * TILE_HEIGHT
        canvas.paste(tile, (x, y))
        label = str(idx)
        draw.rectangle([x + 4, y + 4, x + 44, y + 40], fill=(0, 0, 0))
        draw.text((x + 16, y + 12), label, fill=(255, 255, 0))

    buffer = io.BytesIO()
    canvas.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()
