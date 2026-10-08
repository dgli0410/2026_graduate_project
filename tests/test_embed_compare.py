"""글 좌표 모델 비교: 저장된 장면 카드를 후보 모델로 다시 색인하고 그 색인으로 검색한다."""
import sys
import types
import unittest
from typing import ClassVar
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from server import config, db, embed_compare, embedder, image_embedder, main, vectors

DIMS = {"nlpai-lab/KURE-v1": 16, "dragonkue/snowflake-arctic-embed-l-v2.0-ko": 16,
        "dragonkue/multilingual-e5-small-ko-v2": 8}


class FakeSentenceTransformer:
    """글자 해시로 좌표를 만드는 대역. 같은 글자가 많을수록 가깝습니다. 받은 글을 모델별로 기록합니다."""

    seen: ClassVar[dict[str, list[str]]] = {}

    def __init__(self, name, device=None):
        self.name = name
        self.size = DIMS[name]
        FakeSentenceTransformer.seen.setdefault(name, [])

    def get_embedding_dimension(self):
        return self.size

    def encode(self, texts, batch_size=32, normalize_embeddings=False):
        FakeSentenceTransformer.seen[self.name].extend(texts)
        out = np.zeros((len(texts), self.size), dtype=np.float32)
        for row, text in enumerate(texts):
            for ch in text.removeprefix("query: ").removeprefix("passage: "):
                if not ch.isspace():
                    out[row, ord(ch) % self.size] += 1.0
        return out


SEGMENTS = [
    {"segment_id": "seg_000", "start_time": 0.0, "end_time": 4.0, "asr_text": "양파를 썰어요",
     "ocr_text": "양파 반 개", "caption": "도마 위에서 양파를 써는 장면", "frame_path": ""},
    {"segment_id": "seg_001", "start_time": 4.0, "end_time": 8.0, "asr_text": "두부를 넣어요",
     "ocr_text": "두부 한 모", "caption": "냄비에 두부를 넣는 장면", "frame_path": ""},
    {"segment_id": "seg_002", "start_time": 8.0, "end_time": 12.0, "asr_text": "된장 두 큰술",
     "ocr_text": "된장 2큰술", "caption": "된장을 풀어 넣는 장면", "frame_path": ""},
]


class EmbedCompareCase(unittest.TestCase):
    user = "emb-user"

    def setUp(self):
        FakeSentenceTransformer.seen = {}
        fake_module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
        for p in (
            patch.dict(sys.modules, {"sentence_transformers": fake_module}),
            patch.object(embedder, "_st_models", {}),
            patch.object(config, "_installed", lambda _p: True),
        ):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        db.upsert_video(self.user, "vid1", "된장찌개", "ch", 12.0)
        db.replace_segments(self.user, "vid1", [{**s, "segment_id": f"vid1_{s['segment_id']}"} for s in SEGMENTS])
        db.set_status(self.user, "vid1", "ready")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for target in [config.embed_target(m) for m in config.EMBED_CANDIDATES] + [config.DEFAULT_CONDITION]:
            vectors.drop_collection(target)
        db.delete_video(self.user, "vid1")

    def build(self, model, **kwargs):
        return embed_compare.build_index(model, user_id=self.user, log=lambda _m: None, **kwargs)

    def test_builds_a_separate_index_and_finds_the_right_scene(self):
        report = self.build("kure")
        self.assertEqual((report["videos"], report["segments"]), (1, 3))
        self.assertEqual(embed_compare.indexed_segments("kure", self.user), 3)
        # 기본 창고는 만들지도 건드리지도 않습니다.
        self.assertFalse(vectors.get_client().collection_exists(config.collection_for(config.DEFAULT_CONDITION)))

        result = embed_compare.search(self.user, "두부 언제 넣어요", "kure", top_k=3)
        self.assertEqual(result["scenes"][0]["segment_id"], "vid1_seg_001")
        self.assertEqual(result["embed_model"], "kure")
        self.assertEqual(result["answer"], "")  # 답변 생성은 기본으로 끕니다

    def test_each_candidate_gets_its_own_prefixes(self):
        self.build("arctic")
        self.build("e5small")
        embed_compare.search(self.user, "두부", "arctic")
        embed_compare.search(self.user, "두부", "e5small")
        arctic = FakeSentenceTransformer.seen["dragonkue/snowflake-arctic-embed-l-v2.0-ko"]
        e5 = FakeSentenceTransformer.seen["dragonkue/multilingual-e5-small-ko-v2"]
        self.assertFalse(any(t.startswith(("query: ", "passage: ")) for t in arctic[:3]))  # 장면: 접두어 없음
        self.assertEqual(arctic[-1], "query: 두부")
        self.assertTrue(all(t.startswith("passage: ") for t in e5[:3]))
        self.assertEqual(e5[-1], "query: 두부")
        # 후보마다 창고 차원이 모델을 따라갑니다.
        self.assertEqual(embed_compare.collection_dim("arctic"), 16)
        self.assertEqual(embed_compare.collection_dim("e5small"), 8)

    def test_search_without_an_index_says_so_and_creates_nothing(self):
        with self.assertRaises(embed_compare.NotIndexed):
            embed_compare.search(self.user, "두부", "kure")
        self.assertIsNone(embed_compare.collection_dim("kure"))

    def test_unknown_candidate(self):
        with self.assertRaises(ValueError):
            embed_compare.search(self.user, "두부", "nope")

    def test_dimension_change_needs_recreate(self):
        self.build("kure")
        with patch.dict(DIMS, {"nlpai-lab/KURE-v1": 8}), patch.object(embedder, "_st_models", {}):
            with self.assertRaises(RuntimeError):
                self.build("kure")
            self.build("kure", recreate=True)
        self.assertEqual(embed_compare.collection_dim("kure"), 8)

    def test_image_vectors_are_copied_from_the_default_index(self):
        segments = db.list_segments(self.user, "vid1")
        images = np.eye(3, 4, dtype=np.float32)
        with patch.object(image_embedder, "is_enabled", return_value=True), \
             patch.object(image_embedder, "dim", return_value=4):
            vectors.upsert_segments(self.user, "vid1", "된장찌개", segments,
                                    np.ones((3, 768), np.float32), images)
            report = self.build("kure")
        self.assertEqual(report["without_image"], 0)
        name = config.collection_for(config.embed_target("kure"))
        ids = [vectors._point_id(self.user, s["segment_id"]) for s in segments]
        points = vectors.get_client().retrieve(collection_name=name, ids=ids, with_vectors=True)
        got = {str(p.id): p.vector[vectors.IMAGE_VECTOR] for p in points}
        for i, pid in enumerate(ids):
            np.testing.assert_allclose(got[pid], images[i])

    def test_deleting_a_video_removes_it_from_candidate_indexes(self):
        self.build("kure")
        vectors.delete_video_all_conditions(self.user, "vid1")
        self.assertEqual(embed_compare.indexed_segments("kure", self.user), 0)

    def test_status_reports_coverage(self):
        self.build("kure")
        rows = {r["name"]: r for r in embed_compare.status(self.user)}
        self.assertEqual(rows["kure"]["indexed_segments"], 3)
        self.assertEqual(rows["kure"]["expected_segments"], 3)
        self.assertEqual(rows["arctic"]["indexed_segments"], 0)
        self.assertFalse(rows["gemini"]["ready"])  # 키가 없으면 준비 안 됨

    def test_api(self):
        self.build("kure")
        client = TestClient(main.app)
        headers = {"X-User-Id": self.user}
        ok = client.post("/api/embed-compare/search", headers=headers,
                         json={"query": "된장 두 큰술", "model": "kure", "top_k": 2})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["scenes"][0]["segment_id"], "vid1_seg_002")
        missing = client.post("/api/embed-compare/search", headers=headers, json={"query": "두부", "model": "arctic"})
        self.assertEqual(missing.status_code, 409)
        unknown = client.post("/api/embed-compare/search", headers=headers, json={"query": "두부", "model": "nope"})
        self.assertEqual(unknown.status_code, 400)
        listing = client.get("/api/embed-compare", headers=headers).json()["candidates"]
        self.assertEqual([r["name"] for r in listing], list(config.EMBED_CANDIDATES))


class ConfigCase(unittest.TestCase):
    def test_use_embedding_only_changes_the_text_model(self):
        with config.use_embedding("arctic"):
            self.assertEqual(config.backend("text_embed"), "st")
            self.assertEqual(config.backend("image_embed"), config.IMAGE_EMBED_BACKEND)  # .env 그대로
            self.assertEqual(config.condition_setting("ST_QUERY_PREFIX", ""), "query: ")
        self.assertEqual(config.backend("text_embed"), config.TEXT_EMBED_BACKEND)

    def test_candidate_collections_never_clash_with_conditions(self):
        names = {config.collection_for(c) for c in config.CONDITIONS}
        for model in config.EMBED_CANDIDATES:
            self.assertNotIn(config.collection_for(config.embed_target(model)), names)


if __name__ == "__main__":
    unittest.main()
