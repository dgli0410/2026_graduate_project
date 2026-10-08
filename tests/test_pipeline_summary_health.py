"""pipeline-reliability-2 · 요약 단계(summary_llm)의 할당량 소진이 '일시 속도 제한'으로 둔갑하지 않는다.

실제 Google 429 본문은 수백 자이고 free_tier · PerDay 표식이 200자 뒤에 있습니다. 예전에는 화면용으로
200자에서 자른 이유 글을 다시 분류해서, 보관함 카드에 '(Gemini 할당량 소진)'이 빠졌습니다.

S2-4 · 카드가 있는데 요약 응답이 차단 · 깨지면 ok_empty 가 아니라 EMPTY_RESPONSE 오류입니다(음성 · 장면 설명과 같은 규칙).
"""
import types
import unittest
from unittest.mock import patch

import numpy as np

from server import db, gemini
from server.pipeline import health, summarizer, worker
from tests.test_gemini_errors import PER_DAY_FREE, google_429

USER = "c-pipeline-summary"
SEGMENTS = [
    {"segment_id": "sv_seg_000", "start_time": 0.0, "end_time": 4.0, "asr_text": "김치를 볶아요",
     "ocr_text": "", "caption": "냄비에 김치", "frame_path": ""},
]


class RaisingClient:
    def __init__(self, error):
        self.error = error
        self.calls = 0
        self.models = self

    def generate_content(self, **_):
        self.calls += 1
        raise self.error


def summarize_with(client):
    with patch.object(summarizer, "get_client", return_value=client), patch.object(gemini.time, "sleep"):
        return summarizer.summarize("김치볶음", SEGMENTS)


class SummaryStageCase(unittest.TestCase):
    def test_long_quota_error_is_still_a_quota_error(self):
        error = google_429(PER_DAY_FREE)
        self.assertGreater(len(str(error)), 300)
        client = RaisingClient(error)
        summary = summarize_with(client)
        self.assertEqual(client.calls, 1, "일일 할당량 소진은 재시도하지 않습니다")
        self.assertNotIn("PerDay", summary["degraded_reason"], "화면용 이유는 짧게 잘립니다")
        state = worker._summary_stage(summary)
        self.assertEqual(state["status"], health.ERROR)
        self.assertEqual(state["error_code"], gemini.NON_RECOVERABLE_QUOTA, "예전: TRANSIENT_RATE_LIMIT")
        self.assertEqual(state["error_type"], "QuotaExhausted")
        self.assertEqual(health.summarize_health({"summary_llm": state})["quota_stages"], ["summary_llm"])

    def test_transient_error_keeps_its_type_and_retry_count(self):
        summary = summarize_with(RaisingClient(RuntimeError("503 UNAVAILABLE")))
        state = worker._summary_stage(summary)
        # 요약은 2번 시도합니다(재시도 1번). 예전에는 RuntimeError(이유 글)로 다시 만들어 0 이 됐습니다.
        self.assertEqual((state["error_code"], state["error_type"], state["retry_count"]),
                         (gemini.TRANSIENT_SERVER, "RuntimeError", 1))

    def test_blocked_summary_of_real_cards_is_an_empty_response_error(self):
        # S2-4: 카드가 있는데 요약이 비면(차단 · JSON 깨짐) 음성 · 장면 설명처럼 EMPTY_RESPONSE 오류입니다.
        # 예전에는 ok_empty 로 적어 '분석 완전'으로 남았습니다.
        summary = summarize_with(BlockedClient("SAFETY"))
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["headline"], "김치볶음", "대체 요약은 그대로 만듭니다")
        state = worker._summary_stage(summary)
        self.assertEqual((state["status"], state["error_code"], state["error_type"]),
                         (health.ERROR, "EMPTY_RESPONSE", "EmptyResponse"))
        self.assertIn("SAFETY", state["message"])
        verdict = health.summarize_health({"summary_llm": state})
        self.assertEqual((verdict["complete"], verdict["failed_stages"], verdict["quota_stages"]),
                         (False, ["summary_llm"], []))

    def test_unparseable_summary_without_a_reason_is_also_an_error(self):
        client = types.SimpleNamespace(models=types.SimpleNamespace(
            generate_content=lambda **_: types.SimpleNamespace(parsed=None)))
        state = worker._summary_stage(summarize_with(client))
        self.assertEqual((state["status"], state["error_code"]), (health.ERROR, "EMPTY_RESPONSE"))

    def test_no_cards_to_summarize_stays_ok_empty(self):
        # 부르지 않은 요약(카드 0장)은 정상 빈 결과 그대로입니다.
        summary = summarizer.summarize("김치볶음", [])
        self.assertNotIn("degraded_error", summary)
        self.assertEqual(worker._summary_stage(summary)["status"], health.OK_EMPTY)



class BlockedClient:
    """200 응답인데 parsed 가 없음 — 안전 필터 차단(finish_reason)."""

    def __init__(self, finish_reason):
        self.models = self
        self.finish_reason = finish_reason

    def generate_content(self, **_):
        return types.SimpleNamespace(parsed=None, prompt_feedback=None,
                                     candidates=[types.SimpleNamespace(finish_reason=self.finish_reason)])


class SaveFlowCase(unittest.TestCase):
    """저장 경로(_process_save)가 DB 에 남기는 analysis_health — 보관함 카드가 읽는 값."""

    def save_with_summary_client(self, video_id, client):
        db.init_db()
        db.upsert_video(USER, video_id, "김치볶음", "ch", 4.0)
        segments = [{**SEGMENTS[0], "segment_id": f"{video_id}_seg_000"}]  # 카드 id 는 사용자 안에서 유일
        result = {
            "segments": segments, "steps": [], "step_hints": [], "duration": 4.0,
            "text_vectors": np.ones((1, 4), np.float32), "image_vectors": None,
            "timings": {"transcribe": 0.0, "frames": 0.0, "embedding": 0.0},
            "analysis_health": {"asr": health.stage(health.OK), "summary": {}},
            "counts": {"segments": 1},
        }
        with patch.object(worker, "analyze_media", return_value=result), \
             patch.object(worker.vectors, "upsert_segments"), \
             patch.object(summarizer, "get_client", return_value=client), \
             patch.object(gemini.time, "sleep"):
            worker._process_save(USER, video_id)
        return db.get_video(USER, video_id)

    def test_stored_health_names_the_summary_quota(self):
        video = self.save_with_summary_client("sv", RaisingClient(google_429(PER_DAY_FREE)))
        self.assertEqual(video["status"], "ready")
        stored = video["summary"]["analysis_health"]
        self.assertEqual(stored["summary_llm"]["error_code"], gemini.NON_RECOVERABLE_QUOTA)
        self.assertEqual(stored["summary"]["failed_stages"], ["summary_llm"])
        self.assertEqual(stored["summary"]["quota_stages"], ["summary_llm"])
        self.assertFalse(stored["summary"]["complete"])

    def test_blocked_summary_is_saved_degraded_and_marked_failed(self):
        video = self.save_with_summary_client("sv_blocked", BlockedClient("RECITATION"))
        self.assertEqual(video["status"], "ready", "요약이 막혀도 저장은 끝납니다")
        summary = video["summary"]
        self.assertEqual((summary["degraded"], summary["headline"]), (True, "김치볶음"))
        stored = summary["analysis_health"]
        self.assertEqual((stored["summary_llm"]["status"], stored["summary_llm"]["error_code"]),
                         (health.ERROR, "EMPTY_RESPONSE"))
        self.assertEqual(stored["summary"]["failed_stages"], ["summary_llm"], "보관함: '⚠ 일부 분석 실패: 요약'")
        self.assertEqual(stored["summary"]["quota_stages"], [])
        self.assertFalse(stored["summary"]["complete"])


if __name__ == "__main__":
    unittest.main()
