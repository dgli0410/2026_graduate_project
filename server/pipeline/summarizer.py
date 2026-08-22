"""단계 6 · 장면 카드 전부를 다시 Gemini 에게 주고 전체 요약을 만듭니다."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from server.config import ANSWER_MODEL
from server.gemini import get_client


class SummaryStep(BaseModel):
    time: float = Field(description="이 단계가 시작되는 시각(초)")
    label: str = Field(description="이 단계에서 하는 일, 짧게")


class VideoSummary(BaseModel):
    headline: str = Field(description="영상 내용을 한 문장으로")
    keywords: list[str] = Field(description="핵심 키워드 3~6개")
    steps: list[SummaryStep] = Field(description="시간 순 주요 단계 3~7개")


PROMPT = """
아래는 한 쇼츠 영상을 장면 단위로 정리한 내용이야.
이것만 근거로 요약해줘. 여기 없는 내용은 지어내지 마.

- headline: 영상 내용을 한 문장으로
- keywords: 핵심 키워드 3~6개
- steps: 시간 순으로 주요 단계 3~7개 (time 은 장면 카드에 적힌 초를 그대로 써)

한국어로 적어줘.

장면 카드:
{cards}
""".strip()


def _cards_text(segments: list[dict[str, Any]]) -> str:
    lines = []
    for seg in segments:
        lines.append(
            f"[{seg['start_time']:.1f}s~{seg['end_time']:.1f}s] "
            f"말: {seg.get('asr_text', '') or '-'} / "
            f"자막: {seg.get('ocr_text', '') or '-'} / "
            f"장면: {seg.get('caption', '') or '-'}"
        )
    return "\n".join(lines)


def _fallback_summary(title: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    steps = [
        {"time": seg["start_time"], "label": (seg.get("caption") or seg.get("asr_text") or "")[:40]}
        for seg in segments[:: max(1, len(segments) // 5)][:5]
    ]
    return {
        "headline": title or "요약을 생성하지 못했습니다.",
        "keywords": [],
        "steps": [s for s in steps if s["label"]],
        "degraded": True,
    }


def summarize(title: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    if not segments:
        return _fallback_summary(title, segments)
    from google.genai import types

    try:
        response = get_client().models.generate_content(
            model=ANSWER_MODEL,
            contents=PROMPT.format(cards=_cards_text(segments)),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=VideoSummary
            ),
        )
        parsed = response.parsed
        if parsed is None:
            return _fallback_summary(title, segments)
        summary = parsed if isinstance(parsed, VideoSummary) else VideoSummary.model_validate(parsed)
        return summary.model_dump()
    except Exception:  # noqa: BLE001 - 요약 실패로 저장 전체를 실패시키지는 않습니다
        return _fallback_summary(title, segments)
