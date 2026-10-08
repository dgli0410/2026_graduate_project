"""1-1 · Gemini 오류 등급: 일일 할당량 소진은 재시도하지 않는다."""
import math
import random
import types
import unittest
from unittest.mock import patch

from google.genai import errors as genai_errors

from server import config, embedder, gemini, vectors

FREE_TIER_429 = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20"
)

# 실제 무료 등급 429 의 quotaId. 분당/일일 한도는 quotaMetric 이 같고 이것만 다릅니다.
PER_MINUTE_FREE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
PER_MINUTE_TPM_FREE = "GenerateContentInputTokensPerModelPerMinute-FreeTier"
EMBED_PER_MINUTE_FREE = "EmbedContentRequestsPerMinutePerProjectPerModel-FreeTier"
PER_DAY_FREE = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
EMBED_METRIC = "generativelanguage.googleapis.com/embed_content_free_tier_requests"


def google_429(quota_id: str, delay: str = "35s",
               metric: str = "generativelanguage.googleapis.com/generate_content_free_tier_requests",
               limit: str = "20", exact: str | None = None) -> Exception:
    """google-genai 가 실제로 던지는 429(ClientError). 본문 모양은 Google 응답 그대로입니다.

    str() 이 수백 자이고 free_tier · quotaId 표식은 200자 뒤쪽에 있습니다. 기다릴 시간은 두 군데에 있습니다:
    본문 메시지에 정확한 값(exact, "Please retry in 35.79s."), RetryInfo.retryDelay 에 초 단위로 자른 값
    (delay, "35s"). exact 를 주지 않으면 delay 에 .79 를 붙인 값(자른 값과 맞는 정확한 값)입니다.
    """
    if exact is None:
        exact = f"{delay[:-1]}.79"
    body = {"error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing details. For more "
            "information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. "
            f"\n* Quota exceeded for metric: {metric}, limit: {limit}, model: gemini-2.5-flash"
            f"\nPlease retry in {exact}s."
        ),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
                "quotaMetric": metric, "quotaId": quota_id,
                "quotaDimensions": {"location": "global", "model": "gemini-2.5-flash"},
                "quotaValue": limit}]},
            {"@type": "type.googleapis.com/google.rpc.Help", "links": [{
                "description": "Learn more about Gemini API quotas",
                "url": "https://ai.google.dev/gemini-api/docs/rate-limits"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ],
    }}
    return genai_errors.ClientError(429, body)


class Clock:
    """가짜 시계. 쉬는 시간(time.sleep)만큼 흐르고, 쉰 초를 sleeps 에 적습니다."""

    def __init__(self):
        self.clock = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds


def fake_clock(clock: Clock):
    """time.sleep 과 time.perf_counter 를 이 시계로 바꿉니다. 대화형 재시도는 호출 시간을 time.perf_counter 로
    재므로, 그것까지 바꿔야 가짜 시계의 호출 시간(latency)이 보이고 실제 μs 가 끼지 않습니다."""
    return (patch.object(gemini.time, "sleep", side_effect=clock.sleep),
            patch.object(gemini.time, "perf_counter", side_effect=lambda: clock.clock))


def run_retry(errors: list, **kwargs):
    """errors 를 차례로 던진 뒤 'ok'. -> (결과 또는 예외, 호출 수, 실제로 쉰 초 목록)

    가짜 시계에서 돌고 호출은 시간이 걸리지 않습니다(실제 시계로 재면 μs 단위 호출 시간이 끼어 쉬는 시간이
    12.000001초처럼 됩니다)."""
    calls = []
    clock = Clock()

    def fn():
        calls.append(1)
        if errors:
            raise errors.pop(0)
        return "ok"

    sleep, perf_counter = fake_clock(clock)
    with sleep, perf_counter:
        try:
            result = gemini.call_with_retry(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001
            result = exc
    return result, len(calls), clock.sleeps


def old_call_with_retry(fn):
    """팀원 원본(server/gemini.py)의 재시도 — 비교 기준. 4번, 2·4·8초, 오류 등급 구분 없음."""
    last = None
    for attempt in range(4):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if not any(code in str(exc) for code in gemini.RETRYABLE):
                raise
            if attempt < 3:
                gemini.time.sleep(2.0 * (2**attempt))
    raise last


def interactive(fn):
    return gemini.call_with_retry(fn, **gemini.INTERACTIVE_RETRY)


class GoogleLimit(Clock):
    """clear_at 초에 풀리는 분당 한도(가짜 시계 — 쉬는 시간과 호출 시간이 흐름). 남은 시간을 알리는 방식(report):

      google    : Google 응답 그대로 — 본문에 정확한 값("Please retry in 12.500s."), retryDelay 는 초 단위로
                  자른 값("12s", 1초 미만이면 "0s")
      truncated : retryDelay 만(자른 값)    ceil : retryDelay 만(올린 값)    none : 알리지 않음
      503       : 429 대신 503 UNAVAILABLE(기다릴 시간 없음)

    then 은 그 뒤에 이어지는 오류 [(풀리는 초, 방식)]입니다(예: 429 가 풀린 뒤 503 이 더 이어짐).
    latency 는 호출 한 번이 걸리는 초 — 요청은 보낸 뒤 반만큼 지나 서버에 닿고 응답은 다 지나서 옵니다.
    scale 은 남은 시간을 그 배수로 짧게 알리는 서버(1 = 정확). calls 는 호출이 서버에 닿은 시각입니다.
    """

    def __init__(self, clear_at: float, report: str = "google", *, then=(), latency: float = 0.0,
                 scale: float = 1.0):
        super().__init__()
        self.phases = [(clear_at, report), *then]
        self.latency, self.scale = latency, scale
        self.calls: list[float] = []

    def error(self, report: str, left: float) -> Exception:
        left *= self.scale
        if report == "google":
            return google_429(EMBED_PER_MINUTE_FREE, delay=f"{math.floor(left)}s", exact=f"{left:.3f}",
                              metric=EMBED_METRIC)
        if report == "503":
            return RuntimeError("503 UNAVAILABLE. The model is overloaded. Please try again later.")
        head = f"429 RESOURCE_EXHAUSTED quotaId {EMBED_PER_MINUTE_FREE}"
        if report == "truncated":
            return RuntimeError(f"{head} {{'retryDelay': '{math.floor(left)}s'}}")
        if report == "ceil":
            return RuntimeError(f"{head} {{'retryDelay': '{max(1, math.ceil(left))}s'}}")
        return RuntimeError(head)

    def call(self):
        at_server = self.clock + self.latency / 2
        self.clock += self.latency
        self.calls.append(at_server)
        for until, report in self.phases:
            if at_server < until:
                raise self.error(report, until - at_server)
        return "ok"

    def run(self, retry):
        """retry(self.call) 를 이 시계로 부릅니다 -> (결과 'ok' 또는 None, 흐른 시간)."""
        sleep, perf_counter = fake_clock(self)
        with sleep, perf_counter:
            try:
                result = retry(self.call)
            except Exception:  # noqa: BLE001
                result = None
        return result, self.clock

    def worst_original_time(self) -> float:
        """원본 재시도가 가장 오래 걸리는 경우: 쉬는 시간 14초 + 호출 4번."""
        return 14.0 + 4 * self.latency


class ClassifyCase(unittest.TestCase):
    def test_free_tier_is_non_recoverable(self):
        info = gemini.classify_error(RuntimeError(FREE_TIER_429))
        self.assertEqual(info.kind, gemini.NON_RECOVERABLE_QUOTA)
        self.assertFalse(info.retryable)

    def test_plain_429_is_transient(self):
        info = gemini.classify_error(RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '7s'"))
        self.assertEqual(info.kind, gemini.TRANSIENT_RATE_LIMIT)
        self.assertEqual(info.retry_after_sec, 7.0)
        self.assertIsNone(info.retry_in_sec, "본문에 정확한 값이 없음")

    def test_503_is_server(self):
        self.assertEqual(
            gemini.classify_error(RuntimeError("503 UNAVAILABLE")).kind, gemini.TRANSIENT_SERVER
        )

    def test_auth_error_is_fatal(self):
        self.assertEqual(
            gemini.classify_error(RuntimeError("400 API key not valid")).kind, gemini.FATAL
        )

    def test_googles_exact_wait_is_read_from_the_message(self):
        # retryDelay 는 초 단위로 잘린 값(12s), 본문이 정확한 값(12.5s)입니다.
        info = gemini.classify_error(google_429(EMBED_PER_MINUTE_FREE, delay="12s", exact="12.5",
                                                metric=EMBED_METRIC))
        self.assertEqual((info.retry_after_sec, info.retry_in_sec), (12.0, 12.5))
        under_a_second = gemini.classify_error(google_429(EMBED_PER_MINUTE_FREE, delay="0s", exact="0.500"))
        self.assertEqual((under_a_second.retry_after_sec, under_a_second.retry_in_sec), (0.0, 0.5))

    def test_go_style_durations_in_the_message(self):
        for text, seconds in (("Please retry in 41.893423088s.", 41.893423088), ("Please retry in 750ms.", 0.75),
                              ("Please retry in 1m2.5s.", 62.5), ("retry in 250µs", 0.00025),
                              ("Please retry in 0s.", 0.0), ("retry in 2h", 7200.0)):
            with self.subTest(text=text):
                self.assertAlmostEqual(gemini._retry_in(f"429 RESOURCE_EXHAUSTED. {text}"), seconds)
        self.assertIsNone(gemini._retry_in("429 RESOURCE_EXHAUSTED retryDelay: '7s'"))
        self.assertIsNone(gemini._retry_in("Please retry in a moment."))


class RetryCase(unittest.TestCase):
    def run_with(self, errors):
        calls = []

        def fn():
            calls.append(1)
            if errors:
                raise errors.pop(0)
            return "ok"

        with patch.object(gemini.time, "sleep") as sleep:
            try:
                result = gemini.call_with_retry(fn)
            except Exception as exc:  # noqa: BLE001
                result = exc
        return result, calls, sleep

    def test_quota_exhausted_stops_after_one_call(self):
        result, calls, sleep = self.run_with([RuntimeError(FREE_TIER_429)] * 4)
        self.assertIsInstance(result, gemini.QuotaExhausted)
        self.assertEqual(len(calls), 1, "일일 할당량 소진은 재시도하면 안 됩니다")
        sleep.assert_not_called()

    def test_transient_is_retried_then_succeeds(self):
        result, calls, _ = self.run_with([RuntimeError("503 UNAVAILABLE")] * 2)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)

    def test_fatal_is_not_retried(self):
        result, calls, _ = self.run_with([RuntimeError("400 bad request")])
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), 1)

    def test_retry_sleep_is_capped(self):
        _, _, sleep = self.run_with([RuntimeError("429 retryDelay: '120s'")])
        self.assertLessEqual(sleep.call_args[0][0], 30.0)


class QuotaWindowCase(unittest.TestCase):
    """pipeline-reliability-1: 무료 등급의 **분당** 한도를 일일 소진으로 오판하지 않는다."""

    def test_free_tier_per_minute_is_transient_with_retry_delay(self):
        for quota_id in (PER_MINUTE_FREE, PER_MINUTE_TPM_FREE, EMBED_PER_MINUTE_FREE):
            with self.subTest(quota_id=quota_id):
                info = gemini.classify_error(google_429(quota_id, delay="41s"))
                self.assertEqual(info.kind, gemini.TRANSIENT_RATE_LIMIT)
                self.assertTrue(info.retryable)
                self.assertEqual(info.retry_after_sec, 41.0)
                self.assertAlmostEqual(info.retry_in_sec, 41.79)

    def test_free_tier_per_day_is_non_recoverable(self):
        error = google_429(PER_DAY_FREE)
        self.assertGreater(str(error).lower().find("perday"), 200, "표식이 본문 뒤쪽에 있는 실제 모양")
        self.assertEqual(gemini.classify_error(error).kind, gemini.NON_RECOVERABLE_QUOTA)

    def test_per_day_wins_when_both_windows_are_listed(self):
        both = RuntimeError(f"{google_429(PER_MINUTE_FREE)} {google_429(PER_DAY_FREE)}")
        self.assertEqual(gemini.classify_error(both).kind, gemini.NON_RECOVERABLE_QUOTA)

    def test_per_minute_429_is_retried_after_its_retry_delay(self):
        # 저장 작업(백그라운드)은 예전 그대로 retryDelay 만큼 쉽니다.
        result, calls, sleeps = run_retry([google_429(PER_MINUTE_FREE, delay="12s")])
        self.assertEqual(result, "ok", "분당 한도는 retryDelay 뒤에 풀립니다")
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [12.0])

    def test_per_day_429_still_stops_at_once(self):
        result, calls, sleeps = run_retry([google_429(PER_DAY_FREE)] * 4)
        self.assertIsInstance(result, gemini.QuotaExhausted)
        self.assertEqual((calls, sleeps), (1, []))


class RetryBudgetCase(unittest.TestCase):
    """pipeline-reliability-8 · search-11: 사람이 기다리는 호출은 오래 붙잡지 않는다."""

    def test_interactive_fails_fast_when_the_server_asks_to_wait_longer(self):
        error = RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '41s'}")
        result, calls, sleeps = run_retry([error] * 4, **gemini.INTERACTIVE_RETRY)
        self.assertIs(result, error)
        self.assertEqual((calls, sleeps), (1, []), "덜 쉬고 다시 불러봐야 또 429 입니다")
        self.assertEqual(result.retry_count, 0)

    def test_interactive_fails_fast_when_googles_exact_wait_exceeds_the_budget(self):
        # retryDelay 는 14s(남은 14초 안)이지만 본문의 정확한 값은 14.5초 — 14초 안에 풀리지 않습니다(예전도 실패).
        error = google_429(EMBED_PER_MINUTE_FREE, delay="14s", exact="14.5", metric=EMBED_METRIC)
        result, calls, sleeps = run_retry([error] * 4, **gemini.INTERACTIVE_RETRY)
        self.assertIs(result, error)
        self.assertEqual((calls, sleeps), (1, []))

    def test_interactive_waits_googles_exact_value(self):
        error = google_429(EMBED_PER_MINUTE_FREE, delay="3s", exact="3.25", metric=EMBED_METRIC)
        self.assertEqual(run_retry([error], **gemini.INTERACTIVE_RETRY), ("ok", 2, [3.25]))

    def test_interactive_treats_a_bare_retry_delay_as_truncated(self):
        # 본문에 정확한 값이 없으면 retryDelay 는 초 단위로 잘린 값으로 봅니다("3s" = 3~4초 뒤) — 1초 더.
        result, calls, sleeps = run_retry([RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '3s'")],
                                          **gemini.INTERACTIVE_RETRY)
        self.assertEqual((result, calls, sleeps), ("ok", 2, [4.0]))

    def test_interactive_zero_retry_delay_means_within_a_second_not_missing(self):
        # 예전 코드는 "0s" 를 '없음'으로 보고 2·4·8초 규칙으로 넘어갔습니다.
        result, calls, sleeps = run_retry([RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '0s'")],
                                          **gemini.INTERACTIVE_RETRY)
        self.assertEqual((result, calls, sleeps), ("ok", 2, [1.0]))

    def test_interactive_truncated_wait_is_capped_at_the_remaining_budget(self):
        # 13.5초 뒤 풀림을 "13s" 로만 알림: 14초(남은 예산)까지만 쉽니다 — 13+1 = 14.
        result, calls, sleeps = run_retry([RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '13s'")],
                                          **gemini.INTERACTIVE_RETRY)
        self.assertEqual((result, calls, sleeps), ("ok", 2, [14.0]))
        # 2초를 쉰 뒤 "12s": 12 는 남은 12초 안이므로 남은 예산(12초)까지 쉬고 한 번 더 — 합 14초.
        result, calls, sleeps = run_retry([RuntimeError("503 UNAVAILABLE"),
                                           RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '12s'")],
                                          **gemini.INTERACTIVE_RETRY)
        self.assertEqual((result, calls, sleeps), ("ok", 3, [2.0, 12.0]))

    def test_interactive_server_errors_are_bounded(self):
        # 503 은 예전 재시도(4번 · 2·4·8초)와 똑같습니다. 합 14초를 넘지 않습니다.
        result, calls, sleeps = run_retry([RuntimeError("503 UNAVAILABLE")] * 9,
                                          **gemini.INTERACTIVE_RETRY)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual((calls, sleeps), (4, [2.0, 4.0, 8.0]))
        self.assertLessEqual(sum(sleeps), gemini.INTERACTIVE_RETRY["max_total_wait"])
        self.assertEqual(result.retry_count, 3)

    def test_interactive_recovers_googles_limit_that_clears_within_14_seconds(self):
        # 예전 재시도는 14초 안에 풀리는 분당 한도를 2·4·8초 쉬며 넘겼습니다(t=2·6·14초). 같은 경우에 실패하면
        # 검색이 500 이 되고 eval_run 은 그 질문을 실패로 셉니다. 12.5초: 고치기 전에는 12초(retryDelay)만 쉬고
        # 두 번째 429 의 "0s" 를 '없음'으로 봐 4초를 요구하다 예산을 넘겨 실패했습니다.
        for clear_at in (0.5, 1.5, 7.0, 11.5, 12.5, 13.99, 14.0):
            with self.subTest(clear_at=clear_at):
                limit = GoogleLimit(clear_at)
                self.assertEqual(limit.run(interactive), ("ok", clear_at))
                self.assertEqual((limit.calls, limit.sleeps), ([0.0, clear_at], [clear_at]))

    def test_interactive_never_fails_where_the_old_retry_succeeded(self):
        # 가짜 시계(호출 시간 0): 0~20초에 0.01초 간격으로 풀리는 분당 한도를 예전 재시도(4번 · t=0·2·6·14초)와
        # 비교합니다. Google 모양(본문의 정확한 값)과 알리지 않는 응답은 예전보다 늦지도 않습니다. retryDelay 만
        # 알리는 서버(자른 값 · 올린 값)는 +1초 규칙 때문에 최대 1초 늦을 수 있습니다(실패는 없음). 호출 시간이 0 이면
        # 쉬는 시간의 합도 14초를 넘지 않습니다. 호출 시간 · 섞인 오류는 InteractiveAgainstOriginalCase 가 봅니다.
        budget = gemini.INTERACTIVE_RETRY["max_total_wait"]
        clears = [hundredth / 100 for hundredth in range(2001)]
        # 예전 재시도는 알려 준 시간을 보지 않으므로(2·4·8초 고정) 보고 방식과 무관합니다 — 한 번만 잽니다.
        before = {clear_at: GoogleLimit(clear_at, "none").run(old_call_with_retry) for clear_at in clears}
        for report, slack in (("google", 0.0), ("none", 0.0), ("truncated", 1.0), ("ceil", 1.0)):
            failed, slower, overspent = [], [], []
            for clear_at in clears:
                old, old_time = before[clear_at]
                limit = GoogleLimit(clear_at, report)
                new, new_time = limit.run(interactive)
                if old == "ok" and new != "ok":
                    failed.append(clear_at)
                if old == "ok" and new == "ok" and new_time > old_time + slack + 1e-9:
                    slower.append((clear_at, old_time, new_time))
                if sum(limit.sleeps) > budget + 1e-9 or len(limit.calls) > 4:
                    overspent.append((clear_at, limit.sleeps))
            with self.subTest(report=report):
                self.assertEqual(failed, [], "예전 재시도가 성공하던 한도에서 실패")
                self.assertEqual(slower[:5], [], "예전 재시도보다 오래 기다림")
                self.assertEqual(overspent[:5], [])

    def test_background_calls_keep_waiting_for_the_server(self):
        # 저장 작업은 사람이 기다리지 않습니다. 41초를 요구하면 30초(상한) 쉬고 다시 — 대개 풀립니다.
        result, calls, sleeps = run_retry([RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '41s'")])
        self.assertEqual((result, calls, sleeps), ("ok", 2, [30.0]))

    def test_background_retry_is_unchanged_for_googles_shape(self):
        # 저장 작업(백그라운드)의 규칙은 이번 수정과 무관합니다(기본 경로 그대로): retryDelay 12초, 다음 "0s" 는
        # 예전처럼 2·4·8초 규칙(4초). 더 오래 쉬어도 시도 횟수는 같고 기다리는 사람이 없습니다.
        limit = GoogleLimit(12.5)
        self.assertEqual(limit.run(gemini.call_with_retry), ("ok", 16.0))
        self.assertEqual(limit.sleeps, [12.0, 4.0])
        # 호출 시간을 셈에 넣는 것(R4-S2)도 대화형 재시도만입니다 — 호출이 0.25초씩 걸려도 저장 작업은 그대로.
        slow = GoogleLimit(12.5, latency=0.25)
        self.assertEqual(slow.run(gemini.call_with_retry)[0], "ok")
        self.assertEqual(slow.sleeps, [12.0, 4.0])


def compare_with_original(make_limit, slack: float | None) -> list:
    """make_limit() 로 같은 한도를 두 번 만들어 팀원 원본 재시도와 대화형 재시도를 돌립니다. -> 문제 목록:
    원본은 성공했는데 실패 · 4번 넘게 부름 · 원본의 가장 긴 경우(14초 + 호출 4번)보다 호출 한 번 넘게 늦음 ·
    (slack 을 주면) 둘 다 성공했는데 원본보다 slack 초 넘게 늦음."""
    old_limit, new_limit = make_limit(), make_limit()
    old, old_time = old_limit.run(old_call_with_retry)
    new, new_time = new_limit.run(interactive)
    problems = []
    if old == "ok" and new != "ok":
        problems.append(("원본은 성공", new_limit.phases, old_limit.calls, new_limit.calls))
    if len(new_limit.calls) > 4:
        problems.append(("4번 넘게 부름", new_limit.phases, new_limit.calls))
    if new_time > new_limit.worst_original_time() + new_limit.latency + 1e-9:
        problems.append(("원본의 가장 긴 경우보다 늦음", new_limit.phases, new_time))
    if slack is not None and old == new == "ok" and new_time > old_time + slack + 1e-9:
        problems.append(("원본보다 늦음", new_limit.phases, old_time, new_time))
    return problems


class InteractiveAgainstOriginalCase(unittest.TestCase):
    """R4-S2 · 대화형 재시도를 팀원 원본(4번 · 2·4·8초)과 가짜 시계로 비교합니다 — 호출에 시간이 걸리고, 429 와
    503 이 섞이고, 남은 시간을 짧게 알리는 서버에서도. 원본의 마지막(4번째) 호출은 쉬는 시간 14초 + 앞 호출 3번
    뒤에 나가 반 호출 뒤 서버에 닿습니다(호출 0.25초면 t=14.875초). 고치기 전에는 쉬는 시간만 셌습니다.

    clear_at 격자는 원본의 마지막 호출이 서버에 닿는 때(14 + 3.5 × 호출 시간)와 겹치지 않게 골랐습니다. 딱 그
    때에 풀리는 한도는 두 쪽 모두 그 순간에 부르므로 결과가 부동소수점 반올림(1e-15초)에 달립니다."""

    def test_a_503_after_a_short_exact_wait_still_gets_its_last_call_at_14_seconds(self):
        # 429 "retry in 0.5s" 로 시도 하나를 쓴 뒤 12.75초까지 503. 고치기 전: 0 · 0.5 · 4.5 · 12.5초에 부르고 포기
        # (검색 500). 원본: 0 · 2 · 6 · 14초 → 성공. 마지막 시도는 원본처럼 t=14초에 합니다.
        limit = GoogleLimit(0.5, then=[(12.75, "503")])
        self.assertEqual(limit.run(interactive), ("ok", 14.0))
        self.assertEqual((limit.calls, limit.sleeps), ([0.0, 0.5, 4.5, 14.0], [0.5, 4.0, 9.5]))
        self.assertEqual(GoogleLimit(0.5, then=[(12.75, "503")]).run(old_call_with_retry), ("ok", 14.0))

    def test_a_503_after_a_long_exact_wait_gets_one_more_call_at_14_seconds(self):
        # 12초를 쉰 뒤 13초까지 503: 고치기 전에는 4초가 남은 2초를 넘는다며 바로 포기했습니다(원본은 t=14초 성공).
        limit = GoogleLimit(12.0, then=[(13.0, "503")])
        self.assertEqual(limit.run(interactive), ("ok", 14.0))
        self.assertEqual((limit.calls, limit.sleeps), ([0.0, 12.0, 14.0], [12.0, 2.0]))

    def test_the_call_time_is_credited_before_failing_fast(self):
        # 호출 0.25초. 14.5초에 풀리는 한도: 원본은 성공하는데 고치기 전에는 "retry in 14.375s" 가 남은 14초를
        # 넘는다며 바로 실패(검색 500)했습니다. 원본의 마지막 호출이 닿는 14.875초 정각도 같이 성공합니다.
        for clear_at, sleeps in ((14.5, [14.375]), (14.875, [14.5])):
            with self.subTest(clear_at=clear_at):
                limit = GoogleLimit(clear_at, latency=0.25)
                self.assertEqual(limit.run(interactive)[0], "ok")
                self.assertEqual((len(limit.calls), limit.sleeps), (2, sleeps))
                self.assertLessEqual(limit.calls[-1], 14.875, "원본의 마지막 호출보다 늦게 부르지 않습니다")
                self.assertEqual(GoogleLimit(clear_at, latency=0.25).run(old_call_with_retry)[0], "ok")
        # 원본의 마지막 호출보다 늦게 풀리면 예전처럼 바로 실패를 알립니다(원본도 4번 다 실패).
        limit = GoogleLimit(14.9, latency=0.25)
        self.assertEqual((limit.run(interactive)[0], len(limit.calls), limit.sleeps), (None, 1, []))
        self.assertIsNone(GoogleLimit(14.9, latency=0.25).run(old_call_with_retry)[0])

    def test_server_errors_with_call_time_keep_the_original_schedule(self):
        # 503 만 이어지면 호출 시간이 있어도 원본과 똑같이 2·4·8초 쉬고 4번 부릅니다.
        limit = GoogleLimit(30.0, "503", latency=0.25)
        self.assertIsNone(limit.run(interactive)[0])
        self.assertEqual((limit.sleeps, limit.calls), ([2.0, 4.0, 8.0], [0.125, 2.375, 6.625, 14.875]))

    def test_latency_sweep_against_the_original(self):
        # 호출 0.05 · 0.25 · 0.5초, 0~20초에 0.04초 간격으로 풀리는 한도 하나. 고치기 전: Google 모양에서 원본이
        # 성공하던 (14 + 반 호출, 14 + 3.5 호출] 구간이 모두 실패. 늦는 정도: Google 모양은 왕복 한 번(3자리로 자른
        # 본문 몫 1ms 포함), retryDelay 만 알리면 +1초(올린 값은 +2초)와 왕복 한 번, 알리지 않으면 원본과 같음.
        for latency in (0.05, 0.25, 0.5):
            for report, slack in (("google", latency + 0.001), ("none", 0.0), ("truncated", 1.0 + latency),
                                  ("ceil", 2.0 + latency)):
                problems = []
                for step in range(501):
                    problems += compare_with_original(
                        lambda: GoogleLimit(step / 25, report, latency=latency), slack)  # noqa: B023 - 바로 부름
                with self.subTest(latency=latency, report=report):
                    self.assertEqual(problems[:3], [])

    def test_mixed_errors_against_the_original(self):
        # 429(Google 모양)가 A초까지, 이어서 503 이 B초까지 — 그 반대 순서도. 고치기 전(호출 시간 0): 429 다음 503 에서
        # 1537쌍 중 308쌍이 원본은 성공하는데 실패했습니다.
        for latency in (0.0, 0.25):
            for first, second in (("google", "503"), ("503", "google")):
                problems = []
                for a_step in range(29):
                    for b_step in range(81):
                        a, b = a_step / 2, a_step / 2 + b_step / 4
                        if b <= 20:
                            problems += compare_with_original(
                                lambda: GoogleLimit(a, first, then=[(b, second)], latency=latency),  # noqa: B023
                                None)
                with self.subTest(latency=latency, order=f"{first} -> {second}"):
                    self.assertEqual(problems[:3], [])

    def test_servers_that_report_less_than_the_real_wait(self):
        # 남은 시간을 90% · 50% 로 짧게 알리는 서버: 알린 만큼만 쉬면 매번 조금 일찍 닿아 시도를 다 쓰고 실패했습니다
        # (고치기 전, 호출 시간 0: 0.01~14초 전부). 마지막 시도를 원본의 마지막 호출 때에 하므로 원본이 성공하면 성공.
        for latency in (0.0, 0.25):
            for scale in (0.9, 0.5):
                problems = []
                for step in range(401):
                    problems += compare_with_original(
                        lambda: GoogleLimit(step / 20, latency=latency, scale=scale), None)  # noqa: B023
                with self.subTest(latency=latency, scale=scale):
                    self.assertEqual(problems[:3], [])

    def test_random_error_sequences_against_the_original(self):
        # 오류 구간 1~3개(보고 방식 넷 · 503 섞임), 호출 시간 0 · 0.05 · 0.25초를 무작위로(시드 고정) 2000번.
        rng = random.Random(20261006)
        problems = []
        for _ in range(2000):
            latency = rng.choice((0.0, 0.05, 0.25))
            ends = sorted(round(rng.uniform(0, 20), 2) for _ in range(rng.randint(1, 3)))
            phases = [(end, rng.choice(("google", "truncated", "ceil", "none", "503"))) for end in ends]
            problems += compare_with_original(
                lambda: GoogleLimit(*phases[0], then=phases[1:], latency=latency), None)  # noqa: B023
        self.assertEqual(problems[:3], [])


class FakeEmbedClient:
    def __init__(self, errors=(), limit: GoogleLimit | None = None, dim: int = 2):
        self.errors = list(errors)
        self.limit = limit
        self.dim = dim
        self.calls = 0
        self.models = self

    def embed_content(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.limit is not None:
            self.limit.call()
        return types.SimpleNamespace(
            embeddings=[types.SimpleNamespace(values=[1.0] + [0.0] * (self.dim - 1)) for _ in kwargs["contents"]]
        )


class QueryEmbeddingRetryCase(unittest.TestCase):
    """검색창 질문 좌표(embed_query)는 실제 embedder 경로에서 대화형 재시도를 씁니다."""

    def encode(self, errors, task_type, limit=None):
        client = FakeEmbedClient(errors, limit)
        clock = limit if limit is not None else Clock()
        sleep, perf_counter = fake_clock(clock)
        with patch.object(gemini, "get_client", return_value=client), sleep, perf_counter:
            try:
                result = embedder._encode_gemini(["김치찌개 간장"], task_type)
            except Exception as exc:  # noqa: BLE001
                result = exc
        return result, client.calls, clock.sleeps

    def test_query_embedding_does_not_hang_on_a_long_retry_delay(self):
        long_429 = RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '41s'}")
        result, calls, sleeps = self.encode([long_429] * 4, "RETRIEVAL_QUERY")
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual((calls, sum(sleeps)), (1, 0), "예전: 30+30+30 = 90초 동안 검색창이 멈춤")

    def test_query_embedding_recovers_from_a_per_minute_free_tier_limit(self):
        short = google_429(EMBED_PER_MINUTE_FREE, delay="1s", exact="1.5", metric=EMBED_METRIC)
        result, calls, sleeps = self.encode([short], "RETRIEVAL_QUERY")
        self.assertEqual(result.shape, (1, 2))
        self.assertEqual((calls, sleeps), (2, [1.5]), "고치기 전: 1초 + 4초 = 5초(예전 재시도는 2초)")

    def test_query_embedding_waits_out_a_12_5_second_limit_like_before(self):
        # Google 모양 그대로(본문 12.500s · retryDelay 12s). 예전(4번 · 2·4·8초)은 t=14초에 성공.
        # 고치기 전: 12초만 쉬고, 두 번째 429 의 "0s" 를 '없음'으로 봐 실패(검색 500).
        result, calls, sleeps = self.encode([], "RETRIEVAL_QUERY", limit=GoogleLimit(12.5))
        self.assertEqual(getattr(result, "shape", result), (1, 2))
        self.assertEqual((calls, sleeps), (2, [12.5]))

    def test_document_embedding_keeps_the_background_retry(self):
        long_429 = RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '41s'}")
        result, calls, sleeps = self.encode([long_429], "RETRIEVAL_DOCUMENT")
        self.assertEqual(result.shape, (1, 2))
        self.assertEqual((calls, sleeps), (2, [30.0]))


class SearchRetryHttpCase(unittest.TestCase):
    """POST /api/search(실제 경로) — Google 모양의 분당 한도가 12.5초 뒤 풀릴 때. 고치기 전: 500, 원본: 200."""

    COLLECTION = "t_search_retry"

    def setUp(self):
        for p in (patch.object(config, "QDRANT_COLLECTION", self.COLLECTION),
                  patch.dict(config._BACKEND_DEFAULTS, config.CONDITIONS[config.DEFAULT_CONDITION]["backends"]),
                  patch.object(embedder, "GEMINI_API_KEY", "test-key")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._drop_collection)

    @staticmethod
    def _drop_collection():
        name = config.collection_for(config.DEFAULT_CONDITION)
        if vectors.get_client().collection_exists(name):
            vectors.get_client().delete_collection(name)

    def search(self, clear_at, **limit_kwargs):
        from fastapi.testclient import TestClient

        from server.main import app

        limit = GoogleLimit(clear_at, **limit_kwargs)
        client = FakeEmbedClient(limit=limit, dim=config.GEMINI_EMBED_DIM)
        sleep, perf_counter = fake_clock(limit)
        with patch.object(gemini, "get_client", return_value=client), sleep, perf_counter, \
             TestClient(app, raise_server_exceptions=False) as http:
            r = http.post("/api/search", headers={"X-User-Id": "retry-user"},
                          json={"query": "김치찌개 간장 언제 넣어", "top_k": 5, "with_answer": False})
        return r, limit

    def test_search_succeeds_when_the_limit_clears_at_12_5_seconds(self):
        r, limit = self.search(12.5)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((limit.calls, limit.sleeps), ([0.0, 12.5], [12.5]))

    def test_search_is_not_slower_than_before_for_a_short_limit(self):
        r, limit = self.search(1.5)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(limit.sleeps, [1.5], "고치기 전: [1, 4] = 5초, 원본: 2초")

    def test_search_counts_the_call_time_like_the_original(self):
        # R4-S2: 질문 좌표 호출이 0.25초씩 걸리면 원본의 마지막(4번째) 호출은 14.875초에 서버에 닿습니다. 14.5초에
        # 풀리는 한도를 원본은 200 으로 넘기는데, 고치기 전에는 "retry in 14.375s" 가 남은 14초를 넘는다며 바로 500.
        r, limit = self.search(14.5, latency=0.25)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((limit.calls, limit.sleeps), ([0.125, 14.75], [14.375]))

    def test_search_after_a_short_429_and_then_503_until_12_75_seconds(self):
        # R4-S2: 429 "retry in 0.5s" 다음 12.75초까지 503. 고치기 전: 0 · 0.5 · 4.5 · 12.5초에 부르고 500.
        # 원본: t=14초 200.
        r, limit = self.search(0.5, then=[(12.75, "503")])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(limit.calls, [0.0, 0.5, 4.5, 14.0])


if __name__ == "__main__":
    unittest.main()
