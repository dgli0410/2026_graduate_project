"""1-2 · 단계 상태: 실패가 빈 결과로 둔갑하지 않는다."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from server import config, gemini
from server.pipeline import analyze, caption, health, ocr, transcribe
from server.pipeline import frames as frames_mod
from tests.test_gemini_errors import FREE_TIER_429, PER_DAY_FREE, google_429

# 프레임마다 다른 자막. 모든 프레임에 같은 글자가 같은 자리에 뜨면 OCR 이 채널 워터마크로 보고 지웁니다.
SUBTITLES = ["간장 두 큰술", "양파를 썰어요", "고추장 넣기", "물 500ml", "마늘을 볶아요", "두부 반 모",
             "센 불로 끓여요", "참기름 한 바퀴", "대파 송송", "설탕 조금", "후추 톡톡", "통깨 마무리"]


def subtitle_for(path) -> str:
    return SUBTITLES[int(Path(path).stem) // 500 % len(SUBTITLES)]


def make_frames(root: Path, n: int = 8) -> list[tuple[float, Path]]:
    frames = []
    for i in range(n):
        t = i * 0.5
        path = root / f"{int(t * 1000):08d}.jpg"
        Image.new("RGB", (54, 96), (i * 20, 40, 40)).save(path)
        frames.append((t, path))
    return frames


class FakeClient:
    """generate_content 가 정해진 예외를 던지거나 정해진 응답을 돌려줍니다."""

    def __init__(self, error=None, parsed=None):
        self.calls = 0
        self.error = error
        self.parsed = parsed
        self.models = self

    def generate_content(self, **_):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return type("R", (), {"parsed": self.parsed})()


class SequenceClient:
    """generate_content 가 호출 순서대로 outcomes 를 돌려줍니다(예외면 던집니다)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.models = self

    def generate_content(self, **_):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return type("R", (), {"parsed": outcome})()


def tiles(n: int):
    return caption.GridReading(tiles=[
        caption.TileReading(index=i, on_screen_text="", description=f"장면 {i}") for i in range(n)
    ])


class SummarizeHealthCase(unittest.TestCase):
    """pipeline-reliability-6: 일부만 실패한 단계도 '분석 완전'이 아니다."""

    def test_partial_quota_stop_is_incomplete_and_counts_as_quota(self):
        state = health.stage(health.OK, quota_stopped=True,
                             partial_error={"error_code": gemini.NON_RECOVERABLE_QUOTA})
        summary = health.summarize_health({"asr": health.stage(health.OK), "caption": state})
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["failed_stages"], [])
        self.assertEqual(summary["partial_stages"], ["caption"])
        self.assertEqual(summary["quota_stages"], ["caption"])

    def test_partial_server_error_is_incomplete_but_not_quota(self):
        state = health.stage(health.OK, quota_stopped=False,
                             partial_error={"error_code": gemini.TRANSIENT_SERVER})
        summary = health.summarize_health({"caption": state})
        self.assertEqual((summary["complete"], summary["partial_stages"], summary["quota_stages"]),
                         (False, ["caption"], []))

    def test_everything_ok_is_complete(self):
        summary = health.summarize_health({"asr": health.stage(health.OK_EMPTY),
                                           "caption": health.stage(health.OK, quota_stopped=False)})
        self.assertEqual((summary["complete"], summary["failed_stages"], summary["partial_stages"]),
                         (True, [], []))


class CaptionHealthCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frames = make_frames(Path(self.tmp.name))

    def run_caption(self, client):
        with patch.object(caption, "get_client", return_value=client), \
             patch.object(gemini.time, "sleep"):
            return caption.describe_with_health(self.frames)

    def test_quota_exhaustion_is_an_error_not_an_empty_result(self):
        client = FakeClient(error=RuntimeError(FREE_TIER_429))
        items, state = self.run_caption(client)
        self.assertEqual(state["status"], health.ERROR)
        self.assertEqual(state["error_code"], gemini.NON_RECOVERABLE_QUOTA)
        self.assertTrue(state["quota_stopped"])
        self.assertEqual(client.calls, 1, "할당량 소진 뒤 재호출 금지")
        # 시간 정보는 남아 장면 카드가 만들어질 수 있습니다.
        self.assertEqual(len(items), len(self.frames))

    def test_success_is_ok_with_call_counts(self):
        parsed = caption.GridReading(tiles=[
            caption.TileReading(index=i, on_screen_text="", description=f"장면 {i}")
            for i in range(len(self.frames))
        ])
        items, state = self.run_caption(FakeClient(parsed=parsed))
        self.assertEqual(state["status"], health.OK)
        self.assertEqual(state["successful_calls"], 1)
        self.assertEqual(state["expected_calls"], 1)
        self.assertEqual(items[3]["caption"], "장면 3")

    def test_missing_key_is_recorded(self):
        with patch.object(caption, "get_client", side_effect=gemini.GeminiNotConfigured("no key")):
            items, state = caption.describe_with_health(self.frames)
        self.assertEqual(items, [])
        self.assertEqual(state["status"], health.ERROR)
        self.assertEqual(state["error_type"], "GeminiNotConfigured")

    def test_quota_stop_after_the_first_grid_is_partial_and_quota(self):
        # pipeline-reliability-6: 그리드 3장(MAX=3) 중 1장째 성공, 2장째에서 일일 할당량 소진.
        root = Path(self.tmp.name) / "multi"
        root.mkdir()
        frames = make_frames(root, n=12)
        client = SequenceClient([tiles(4), google_429(PER_DAY_FREE)])
        with patch.object(caption, "select_key_frames", lambda f: list(f)), \
             patch.object(caption, "chunk", lambda f: frames_mod.chunk(f, 4)), \
             patch.object(caption, "MAX_GEMINI_CALLS_PER_VIDEO", 3), \
             patch.object(caption, "get_client", return_value=client), \
             patch.object(gemini.time, "sleep"):
            items, state = caption.describe_with_health(frames)
        self.assertEqual(client.calls, 2, "할당량이 끊긴 뒤 남은 그리드는 부르지 않습니다")
        self.assertEqual((state["status"], state["quota_stopped"]), (health.OK, True))
        self.assertEqual(sum(1 for item in items if item["caption"]), 4)
        summary = health.summarize_health({"caption": state})
        self.assertFalse(summary["complete"], "캡션 2/3 이 빠진 영상이 '분석 완전'이면 안 됩니다")
        self.assertEqual(summary["partial_stages"], ["caption"])
        self.assertEqual(summary["quota_stages"], ["caption"])


class AnalyzeHealthCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.frames = make_frames(self.root)
        self.audio = self.root / "audio.wav"
        self.audio.write_bytes(b"RIFF....")

    def run_analyze(self, transcribe_effect, caption_result, backends=None):
        """transcribe_effect=None 이면 진짜 transcribe() 를 탑니다.

        backends 를 주면 그 백엔드만 가진 시험용 조건 안에서 돌립니다(.env 와 무관하게 고정).
        """
        patches = [
            patch.object(analyze.media, "list_frames", return_value=self.frames),
            patch.object(analyze.media, "audio_path", return_value=self.audio),
            patch.object(analyze.media, "video_dir", return_value=self.root),
            patch.object(analyze.caption, "describe_with_health", return_value=caption_result),
            patch.object(analyze.embedder, "encode",
                         side_effect=lambda texts, **_: np.ones((len(texts), 4), np.float32)),
            patch.object(analyze.image_embedder, "is_enabled", return_value=False),
        ]
        if transcribe_effect is not None:
            patches.append(patch.object(analyze.transcribe, "transcribe", side_effect=transcribe_effect))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        if backends is None:
            return analyze.analyze_media("u", "v", "제목", client_duration=4.0)
        test_condition = {"t_backends": {"label": "시험", "description": "", "backends": backends,
                                         "requires": []}}
        with patch.dict(config.CONDITIONS, test_condition), config.use_condition("t_backends"):
            return analyze.analyze_media("u", "v", "제목", client_duration=4.0)

    def captioned(self, text="냄비"):
        return [{"time": t, "frame_path": str(p), "caption": text, "ocr_text": ""}
                for t, p in self.frames]

    def test_asr_failure_is_recorded_and_analysis_continues(self):
        result = self.run_analyze(RuntimeError("model load failed"),
                                  (self.captioned(), health.stage(health.OK, backend="gemini")))
        h = result["analysis_health"]
        self.assertEqual(h["asr"]["status"], health.ERROR)
        self.assertEqual(h["asr"]["error_type"], "RuntimeError")
        self.assertEqual(h["summary"]["failed_stages"], ["asr"])
        self.assertFalse(h["summary"]["complete"])
        self.assertTrue(result["segments"])

    def test_silent_video_is_ok_empty(self):
        result = self.run_analyze(lambda _a: [], (self.captioned(), health.stage(health.OK)))
        self.assertEqual(result["analysis_health"]["asr"]["status"], health.OK_EMPTY)
        self.assertTrue(result["analysis_health"]["summary"]["complete"])

    def test_no_segments_error_names_the_failed_stages(self):
        quota = health.stage(health.ERROR, error_code=gemini.NON_RECOVERABLE_QUOTA)
        with self.assertRaises(RuntimeError) as ctx:
            self.run_analyze(RuntimeError("asr down"), (self.captioned(""), quota))
        self.assertIn("asr", str(ctx.exception))
        self.assertIn("caption", str(ctx.exception))

    # --- pipeline-reliability-4: Gemini ASR 의 쓸 수 없는 응답 ------------------------------

    def run_gemini_asr(self, response):
        client = types.SimpleNamespace(
            models=types.SimpleNamespace(generate_content=lambda **_: response))
        backends = {"asr": "gemini", "ocr": "gemini", "text_embed": "gemini", "image_embed": "none"}
        with patch.object(gemini, "get_client", return_value=client):
            return self.run_analyze(None, (self.captioned(), health.stage(health.OK)), backends=backends)

    def test_unusable_gemini_asr_response_is_an_error_not_a_silent_video(self):
        blocked = types.SimpleNamespace(
            parsed=None, prompt_feedback=None,
            candidates=[types.SimpleNamespace(finish_reason="RECITATION")])
        result = self.run_gemini_asr(blocked)
        asr = result["analysis_health"]["asr"]
        self.assertEqual(asr["status"], health.ERROR, "차단된 응답을 '말이 없는 영상'으로 적으면 안 됩니다")
        self.assertEqual(asr["error_code"], "EMPTY_RESPONSE")
        self.assertIn("RECITATION", asr["message"])
        self.assertFalse(result["analysis_health"]["summary"]["complete"])
        self.assertTrue(result["segments"], "음성 없이도 장면 설명으로 카드는 만들어집니다")

    def test_gemini_asr_with_no_speech_stays_ok_empty(self):
        silent = types.SimpleNamespace(parsed=transcribe.Transcript(utterances=[]))
        asr = self.run_gemini_asr(silent)["analysis_health"]["asr"]
        self.assertEqual((asr["status"], asr["note"]), (health.OK_EMPTY, "말이 없는 영상"))

    # --- pipeline-reliability-3: 로컬 OCR 이 프레임마다 실패 ----------------------------------

    def run_local_ocr(self, read):
        # EasyOCR 대역: read(경로) -> 읽은 글자들. 상자 · 신뢰도는 화면 아래쪽 한 줄로 고정합니다.
        box = [[0, 900], [200, 900], [200, 940], [0, 940]]
        reader = types.SimpleNamespace(
            readtext=lambda path: [(box, text, 0.9) for text in read(path)])
        backends = {"asr": "faster-whisper", "ocr": "easyocr", "text_embed": "gemini", "image_embed": "none"}
        with patch.object(ocr, "_reader", reader):
            return self.run_analyze(lambda _a: [], (self.captioned(), health.stage(health.OK)),
                                    backends=backends)

    def test_ocr_failing_on_every_frame_is_an_error_not_an_empty_result(self):
        def broken(_path):
            raise AttributeError("'NoneType' object has no attribute 'shape'")

        h = self.run_local_ocr(broken)["analysis_health"]
        self.assertEqual(h["ocr"]["status"], health.ERROR, "예전: ok_empty(자막 없는 영상)로 둔갑")
        self.assertEqual(h["ocr"]["error_type"], "OcrFailed")
        self.assertIn("NoneType", h["ocr"]["message"])
        self.assertEqual(h["summary"]["failed_stages"], ["ocr"])
        self.assertFalse(h["summary"]["complete"])

    def test_ocr_failing_on_some_frames_is_recorded_as_partial(self):
        def flaky(path):
            if path.endswith("00000500.jpg"):
                raise RuntimeError(f"can't open/read file: {path}")
            return [subtitle_for(path)]

        h = self.run_local_ocr(flaky)["analysis_health"]
        self.assertEqual((h["ocr"]["status"], h["ocr"]["failed_frames"]), (health.OK, 1))
        self.assertEqual(h["ocr"]["partial_error"]["error_type"], "RuntimeError")
        # 파일 이름 00000500.jpg 의 '500' 으로 Gemini 서버 오류라고 추측하지 않습니다.
        self.assertEqual(h["ocr"]["partial_error"]["error_code"], gemini.FATAL)
        self.assertEqual(h["summary"]["partial_stages"], ["ocr"])
        self.assertFalse(h["summary"]["complete"])

    def test_ocr_reading_every_frame_is_complete(self):
        h = self.run_local_ocr(lambda p: [subtitle_for(p)])["analysis_health"]
        self.assertEqual(h["ocr"]["status"], health.OK)
        self.assertNotIn("failed_frames", h["ocr"])
        self.assertTrue(h["summary"]["complete"])


if __name__ == "__main__":
    unittest.main()
