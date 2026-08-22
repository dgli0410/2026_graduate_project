"""마스터 문서 단계 9-3 "보안의 핵심" 검증.

남이 저장한 영상이 내 검색 결과에 나오면 안 됩니다.
Gemini 없이 가짜 벡터로 Qdrant 필터만 확인합니다.

    python -m tools.test_user_isolation
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

# config 를 import 하기 전에 임시 폴더로 돌려서 실제 데이터를 건드리지 않습니다.
_TMP = tempfile.mkdtemp(prefix="vault-isolation-")
os.environ["DATA_DIR"] = _TMP

import numpy as np  # noqa: E402

from server import vectors  # noqa: E402
from server.embedder import dim as embedding_dim  # noqa: E402

DIM = embedding_dim()


def fake_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


def segment(idx: int) -> dict:
    return {
        "segment_id": f"seg_{idx}",
        "start_time": idx * 4.0,
        "end_time": idx * 4.0 + 4.0,
        "asr_text": f"대사 {idx}",
        "ocr_text": "",
        "caption": f"장면 {idx}",
    }


def main() -> int:
    try:
        shared = fake_vector(0)  # 두 사용자가 똑같은 내용을 저장한 상황
        vectors.upsert_segments(
            "alice", "vidA", "앨리스 영상", [segment(0)], np.stack([shared])
        )
        vectors.upsert_segments(
            "bob", "vidB", "밥 영상", [segment(0)], np.stack([shared])
        )

        alice_hits = vectors.search("alice", shared, top_n=10)
        bob_hits = vectors.search("bob", shared, top_n=10)
        print(f"alice 검색 결과 {len(alice_hits)}건: {[h['video_id'] for h in alice_hits]}")
        print(f"bob   검색 결과 {len(bob_hits)}건: {[h['video_id'] for h in bob_hits]}")

        assert len(alice_hits) == 1 and alice_hits[0]["video_id"] == "vidA", (
            "❌ alice 검색에 남의 영상이 섞였습니다"
        )
        assert len(bob_hits) == 1 and bob_hits[0]["video_id"] == "vidB", (
            "❌ bob 검색에 남의 영상이 섞였습니다"
        )
        print("✔ 사용자 격리 통과")

        # 영상 안 검색 / 재생목록 범위 검색
        vectors.upsert_segments(
            "alice", "vidC", "앨리스 영상2", [segment(1)], np.stack([fake_vector(1)])
        )
        in_video = vectors.search("alice", shared, top_n=10, video_id="vidA")
        assert [h["video_id"] for h in in_video] == ["vidA"], "❌ video_id 필터 실패"
        print("✔ 영상 내 검색 필터 통과")

        in_playlist = vectors.search("alice", shared, top_n=10, video_ids=["vidC"])
        assert [h["video_id"] for h in in_playlist] == ["vidC"], "❌ 재생목록 필터 실패"
        empty = vectors.search("alice", shared, top_n=10, video_ids=[])
        assert empty == [], "❌ 빈 재생목록인데 결과가 나왔습니다"
        print("✔ 재생목록 범위 검색 통과")

        # 삭제하면 검색에서도 사라져야 합니다
        vectors.delete_video("alice", "vidA")
        after = vectors.search("alice", shared, top_n=10)
        assert all(h["video_id"] != "vidA" for h in after), "❌ 삭제한 영상이 검색에 나옵니다"
        assert len(vectors.search("bob", shared, top_n=10)) == 1, "❌ 남의 데이터까지 지웠습니다"
        print("✔ 삭제 시 창고 정리 통과")

        print("\n모두 통과했습니다.")
        return 0
    finally:
        try:
            vectors.get_client().close()
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
