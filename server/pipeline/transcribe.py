"""단계 4 · 음성 받아적기. 담당 C.

세 가지 백엔드를 같은 인터페이스로 갖습니다.

- gemini         : demo 기본값. 설치 없이 바로 됩니다.
- faster-whisper : 로컬. WhisperX 안에서 실제로 도는 바로 그 엔진(CTranslate2 Whisper)이고
                   word_timestamps 로 단어 단위 시각도 나옵니다. **Python 3.14 에서도 됩니다.**
- whisperx       : 로컬. faster-whisper + wav2vec2 강제정렬으로 시각이 더 정확합니다.
                   ⚠ Python 3.13 이하에서만 설치됩니다(3.14 미지원).

.env 의 ASR_BACKEND 한 줄로 바꿉니다. 나머지 코드는 transcribe() 만 봅니다.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from server.config import CAPTION_MODEL, COMPUTE_TYPE, DEVICE, WHISPER_MODEL, backend

_whisper_model = None
_faster_model = None


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


def _decode_audio(audio: Path, sample_rate: int = 16000):
    """오디오 파일 -> 16kHz 모노 float32 배열.

    ⚠ faster-whisper 에 **파일 경로를 그대로 넘기면 안 됩니다.** 내부의 decode_audio 가
      `av.open(..., metadata_errors=...)` 를 부르는데 PyAV 15 부터 그 인자가 없어져
      TypeError 로 죽습니다. 파이프라인이 예외를 삼켜서 **음성이 조용히 전부 빈 값**이
      됩니다(증상: 모든 장면 카드의 asr_text 가 빈 문자열).
      faster-whisper 1.2.1 이 최신이라 올려서 피할 수 없고, PyAV 를 내리면
      Python 3.13 용 wheel 이 없어 빌드에 실패합니다. 그래서 직접 디코딩합니다.
    """
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    chunks = []
    with av.open(str(audio), mode="r") as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32)
        resampler = AudioResampler(format="s16", layout="mono", rate=sample_rate)
        for frame in container.decode(container.streams.audio[0]):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # 남은 버퍼를 비웁니다
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _transcribe_faster_whisper(audio: Path) -> list[dict]:
    """WhisperX 안에서 도는 바로 그 엔진. 단어 단위 시각까지 받아옵니다."""
    global _faster_model
    from faster_whisper import WhisperModel

    if _faster_model is None:
        _faster_model = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)

    signal = _decode_audio(audio)
    if len(signal) == 0:
        return []

    # vad_filter 는 필수입니다. 쇼츠는 내레이션 없이 배경음악만 있는 경우가 많은데,
    # 그때 Whisper 가 2초 간격으로 그럴듯한 단어를 지어냅니다(환각).
    # 실측: 된장찌개 영상에서 VAD 없으면 "다진마늘/갈비/양파/깻잎" 15개, 켜면 0개.
    segments, _ = _faster_model.transcribe(
        signal, language="ko", word_timestamps=True, vad_filter=True
    )
    results: list[dict] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        results.append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": text,
                # 단어 단위 시각 — "22초로 이동" 정확도의 기준이 됩니다.
                "words": [
                    {"start": float(w.start), "end": float(w.end), "word": w.word.strip()}
                    for w in (seg.words or [])
                    if w.start is not None and w.end is not None
                ],
            }
        )
    return results


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
    name = backend("asr")
    if name == "whisperx":
        try:
            return _transcribe_whisperx(audio)
        except ImportError:
            # WhisperX 는 Python 3.14 를 지원하지 않습니다. 같은 엔진으로 대체합니다.
            print("[transcribe] whisperx 를 못 불러와 faster-whisper 로 대체합니다.")
            return _transcribe_faster_whisper(audio)
    if name == "faster-whisper":
        return _transcribe_faster_whisper(audio)
    return _transcribe_gemini(audio)
