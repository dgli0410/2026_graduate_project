"""단계 4 · 음성 받아적기. 담당 C.

두 가지 백엔드를 같은 인터페이스로 갖습니다.
- whisperx : 진짜. 단어 단위 시각까지 나옵니다. (C 담당 목표)
- gemini   : demo 대체품. 설치 없이 바로 됩니다.

.env 의 ASR_BACKEND 한 줄로 바꿉니다. 나머지 코드는 transcribe() 만 봅니다.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from server.config import ASR_BACKEND, CAPTION_MODEL, COMPUTE_TYPE, DEVICE, WHISPER_MODEL

_whisper_model = None


class Utterance(BaseModel):
    start: float = Field(description="시작 시각(초)")
    end: float = Field(description="종료 시각(초)")
    text: str


class Transcript(BaseModel):
    utterances: list[Utterance]


PROMPT = """
이 오디오를 듣고 한국어로 받아적어줘.

- 말한 구간마다 시작/종료 시각을 초 단위 실수로 적어줘 (오디오 시작이 0초).
- 실제로 들린 말만 적어. 추측해서 채우지 마.
- 말이 없는 구간은 건너뛰어.
""".strip()


def _transcribe_whisperx(audio: Path) -> list[dict]:
    global _whisper_model
    import whisperx

    if _whisper_model is None:
        _whisper_model = whisperx.load_model(
            WHISPER_MODEL, DEVICE, compute_type=COMPUTE_TYPE, language="ko"
        )
    signal = whisperx.load_audio(str(audio))
    result = _whisper_model.transcribe(signal, batch_size=8)

    # 단어 단위 시각 정렬 — "22초로 이동" 정확도의 기준이 됩니다.
    try:
        align_model, metadata = whisperx.load_align_model(language_code="ko", device=DEVICE)
        result = whisperx.align(result["segments"], align_model, metadata, signal, DEVICE)
    except Exception:  # noqa: BLE001 - 정렬 모델이 없어도 문장 단위로는 씁니다
        pass

    return [
        {
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": str(seg.get("text", "")).strip(),
        }
        for seg in result.get("segments", [])
        if str(seg.get("text", "")).strip()
    ]


def _transcribe_gemini(audio: Path) -> list[dict]:
    from google.genai import types

    from server.gemini import call_with_retry, get_client

    client = get_client()
    data = audio.read_bytes()
    response = call_with_retry(
        lambda: client.models.generate_content(
            model=CAPTION_MODEL,
            contents=types.Content(
                parts=[
                    types.Part(inline_data=types.Blob(data=data, mime_type="audio/wav")),
                    types.Part(text=PROMPT),
                ]
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=Transcript
            ),
        )
    )
    parsed = response.parsed
    if parsed is None:
        return []
    transcript = parsed if isinstance(parsed, Transcript) else Transcript.model_validate(parsed)
    return [
        {"start": float(u.start), "end": float(u.end), "text": u.text.strip()}
        for u in transcript.utterances
        if u.text.strip()
    ]


def transcribe(audio: Path) -> list[dict]:
    """오디오 파일 -> [{start, end, text}, ...]"""
    if not audio.is_file() or audio.stat().st_size == 0:
        return []
    if ASR_BACKEND == "whisperx":
        return _transcribe_whisperx(audio)
    return _transcribe_gemini(audio)
