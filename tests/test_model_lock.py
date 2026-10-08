"""1-3 · 로컬 모델은 동시에 두 개를 올리지 않는다."""
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from server import embedder, image_embedder, model_lock
from server.pipeline import ocr, transcribe


class Tracker:
    """모델 생성자 대역. 생성이 겹쳤는지, 몇 번 만들어졌는지 셉니다."""

    def __init__(self):
        self.active = 0
        self.overlaps = 0
        self.built = 0
        self.guard = threading.Lock()

    def slow_build(self, *_a, **_k):
        with self.guard:
            self.active += 1
            self.built += 1
            if self.active > 1:
                self.overlaps += 1
        time.sleep(0.05)
        with self.guard:
            self.active -= 1
        return types.SimpleNamespace(encode=None, transcribe=None)


class ModelLockCase(unittest.TestCase):
    def test_every_loader_uses_the_shared_lock(self):
        for module in (embedder, image_embedder, ocr, transcribe):
            self.assertIs(module.MODEL_LOCK, model_lock.MODEL_LOCK, module.__name__)

    def test_two_different_models_never_build_at_the_same_time(self):
        tracker = Tracker()
        fake_flag = types.ModuleType("FlagEmbedding")
        fake_flag.BGEM3FlagModel = tracker.slow_build
        fake_fw = types.ModuleType("faster_whisper")
        fake_fw.WhisperModel = tracker.slow_build
        saved = (embedder._bge_model, transcribe._faster_model)
        embedder._bge_model = transcribe._faster_model = None
        start = threading.Barrier(4)

        def bge():
            start.wait()
            try:  # 실제 지연 로딩 블록을 그대로 탑니다. 가짜 모델엔 encode 가 없어 그 뒤에 실패.
                embedder._encode_bge(["x"])
            except Exception:  # noqa: BLE001
                pass

        def whisper():
            start.wait()
            try:
                transcribe._transcribe_faster_whisper(Path("x.wav"))
            except Exception:  # noqa: BLE001
                pass

        try:
            with patch.dict(sys.modules, {"FlagEmbedding": fake_flag, "faster_whisper": fake_fw}):
                threads = [threading.Thread(target=f) for f in (bge, bge, whisper, whisper)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            embedder._bge_model, transcribe._faster_model = saved
        self.assertEqual(tracker.overlaps, 0, "모델 생성이 겹쳤습니다")
        self.assertEqual(tracker.built, 2, "같은 모델이 두 벌 올라갔습니다")


if __name__ == "__main__":
    unittest.main()
