"""비교 후보 백엔드: 글 좌표 st(KURE-v1) · 자막 rapidocr. 기본 동작은 그대로다."""
import sys
import types
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import numpy as np

from server import config, embedder
from server.pipeline import ocr

BOX = [[0, 900], [200, 900], [200, 940], [0, 940]]


class FakeSentenceTransformer:
    """sentence_transformers.SentenceTransformer 대역. 받은 글을 기록하고 정규화 안 된 좌표를 돌려줍니다."""

    built: ClassVar[list[str]] = []

    def __init__(self, name, device=None):
        self.name = name
        self.device = device
        self.seen: list[str] = []
        FakeSentenceTransformer.built.append(name)

    def get_embedding_dimension(self):
        return 4

    def encode(self, texts, batch_size=32, normalize_embeddings=False):
        self.seen.extend(texts)
        return np.array([[3.0, 4.0, 0.0, 0.0] for _ in texts], dtype=np.float32)


class StBackendCase(unittest.TestCase):
    def setUp(self):
        FakeSentenceTransformer.built = []
        fake_module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
        patches = [
            patch.dict(sys.modules, {"sentence_transformers": fake_module}),
            patch.object(embedder, "_st_models", {}),
            patch.dict(config._BACKEND_DEFAULTS, {"text_embed": "st"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_encodes_with_the_configured_model_and_normalizes(self):
        out = embedder.encode(["간장 두 큰술", "양파"])
        self.assertEqual(out.shape, (2, 4))
        np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, rtol=1e-6)
        self.assertEqual(FakeSentenceTransformer.built, ["nlpai-lab/KURE-v1"])
        self.assertEqual(embedder.dim(), 4)

    def test_query_and_document_prefixes(self):
        with patch.object(config, "ST_QUERY_PREFIX", "query: "), \
             patch.object(config, "ST_DOC_PREFIX", "passage: "):
            embedder.encode(["양파"], is_query=True)
            embedder.encode(["간장"])
        model = embedder._st_models["nlpai-lab/KURE-v1"]
        self.assertEqual(model.seen, ["query: 양파", "passage: 간장"])

    def test_ready_without_gemini_key(self):
        self.assertEqual(embedder.is_ready(), (True, ""))

    def test_local_v2_pins_its_model_even_if_env_model_differs(self):
        with patch.object(config, "ST_MODEL", "someone/other-model"):
            embedder.encode(["양파"])
            with config.use_condition("local_v2"):
                self.assertEqual(config.backend("text_embed"), "st")
                embedder.encode(["양파"])
            embedder.encode(["양파"])
        # 모델은 이름별로 따로 올라가고, 조건을 벗어나면 .env 값으로 돌아옵니다.
        self.assertEqual(FakeSentenceTransformer.built, ["someone/other-model", "nlpai-lab/KURE-v1"])
        self.assertEqual(len(embedder._st_models["someone/other-model"].seen), 2)
        self.assertEqual(len(embedder._st_models["nlpai-lab/KURE-v1"].seen), 1)


class ConditionSettingCase(unittest.TestCase):
    def test_outside_a_condition_the_default_wins(self):
        self.assertEqual(config.condition_setting("ST_MODEL", "x"), "x")

    def test_conditions_without_settings_do_not_leak_values(self):
        with config.use_condition("local_v2"):
            with config.use_condition("local"):
                self.assertEqual(config.condition_setting("ST_MODEL", "x"), "x")
            self.assertEqual(config.condition_setting("ST_MODEL", "x"), "nlpai-lab/KURE-v1")
        self.assertEqual(config.condition_setting("ST_MODEL", "x"), "x")

    def test_local_v2_changes_only_ocr_and_text_embedding(self):
        local = config.CONDITIONS["local"]["backends"]
        v2 = config.CONDITIONS["local_v2"]["backends"]
        changed = {k for k in local if local[k] != v2[k]}
        self.assertEqual(changed, {"ocr", "text_embed"})
        self.assertEqual(config.collection_for("local_v2"), f"{config.QDRANT_COLLECTION}__local_v2")

    def test_code_defaults_are_unchanged(self):
        self.assertEqual(config.DEFAULT_CONDITION, "gemini")
        self.assertEqual(config.backend("ocr"), "gemini")
        self.assertEqual(config.backend("text_embed"), "gemini")


def rapid_result(lines):
    """RapidOCR 결과 대역. lines = [(글자, 신뢰도), ...]. 빈 목록이면 글자를 못 찾은 프레임."""
    if not lines:
        return types.SimpleNamespace(boxes=None, txts=(), scores=())
    boxes = np.array([[[0, 900 + 50 * i], [200, 900 + 50 * i], [200, 940 + 50 * i], [0, 940 + 50 * i]]
                      for i in range(len(lines))], dtype=np.float32)
    return types.SimpleNamespace(boxes=boxes, txts=tuple(t for t, _ in lines), scores=tuple(s for _, s in lines))


def frames(n):
    return [(i * 0.5, Path(f"{i * 500:08d}.jpg")) for i in range(n)]


class RapidOcrCase(unittest.TestCase):
    def run_rapid(self, engine, n=3):
        with patch.dict(config._BACKEND_DEFAULTS, {"ocr": "rapidocr"}), \
             patch.object(ocr, "_rapid", engine):
            self.assertFalse(ocr.uses_grid())
            return ocr.recognize_all_counted(frames(n))

    def test_reads_frames_with_the_same_filters_as_easyocr(self):
        by_frame = {
            "00000000.jpg": [("간장 두 큰술", 0.95), ("C다 'F60", 0.2)],  # 낮은 신뢰도는 버림
            "00000500.jpg": [],                                       # 글자 없는 프레임
            "00001000.jpg": [("jc", 0.99), ("물 500ml", 0.9)],          # 로고 조각은 버림
        }
        results, counts = self.run_rapid(lambda path: rapid_result(by_frame[Path(path).name]))
        self.assertEqual(results, [{"time": 0.0, "text": "간장 두 큰술"}, {"time": 1.0, "text": "물 500ml"}])
        self.assertEqual(counts["failed_frames"], 0)

    def test_all_frames_failing_is_an_error_not_an_empty_video(self):
        def broken(_path):
            raise RuntimeError("onnxruntime 세션 오류")

        with self.assertRaises(ocr.OcrFailed):
            self.run_rapid(broken)

    def test_partial_failure_is_counted(self):
        def flaky(path):
            if Path(path).name == "00000500.jpg":
                raise RuntimeError("깨진 프레임 00000500.jpg")
            return rapid_result([("간장 두 큰술", 0.9)])

        results, counts = self.run_rapid(flaky)
        self.assertEqual(counts["failed_frames"], 1)
        self.assertEqual(counts["first_error"]["error_code"], "FATAL")
        self.assertEqual(len(results), 2)

    def test_easyocr_path_still_works(self):
        reader = types.SimpleNamespace(readtext=lambda _p: [(BOX, "고추장 넣기", 0.9)])
        with patch.dict(config._BACKEND_DEFAULTS, {"ocr": "easyocr"}), patch.object(ocr, "_reader", reader):
            results, _ = ocr.recognize_all_counted(frames(2))
        self.assertEqual([r["text"] for r in results], ["고추장 넣기", "고추장 넣기"])


if __name__ == "__main__":
    unittest.main()
