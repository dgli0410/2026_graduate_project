"""쓸 수 있는 Gemini 모델과 응답 속도를 재봅니다.

최신 모델일수록 붐벼서 503(일시 과부하)이 잦습니다. 시연 전날 이걸 돌려서
그날 잘 도는 모델을 .env 의 CAPTION_MODEL / ANSWER_MODEL 에 넣으세요.

    python -m tools.check_models
"""
from __future__ import annotations

import sys
import time

from server.gemini import get_client

CANDIDATES = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]


def main() -> int:
    client = get_client()

    print("이 키로 쓸 수 있는 모델:")
    available = {m.name.replace("models/", "") for m in client.models.list()}
    for name in sorted(n for n in available if "embed" in n):
        print(f"  (임베딩) {name}")

    print("\n응답 속도 측정 (503 = 지금 붐빔, 키 문제 아님):")
    healthy = []
    for name in CANDIDATES:
        if name not in available:
            print(f"  {name:26} 목록에 없음")
            continue
        started = time.time()
        try:
            client.models.generate_content(model=name, contents="1+1은? 숫자만.")
            elapsed = time.time() - started
            print(f"  {name:26} OK   {elapsed:5.1f}초")
            healthy.append((elapsed, name))
        except Exception as exc:  # noqa: BLE001
            code = "503 (붐빔)" if "503" in str(exc) else "429 (한도)" if "429" in str(exc) else str(exc)[:50]
            print(f"  {name:26} 실패 {code}")

    if healthy:
        healthy.sort()
        print(f"\n추천: CAPTION_MODEL={healthy[0][1]}")
        lite = [n for _, n in healthy if "lite" in n]
        print(f"      ANSWER_MODEL={lite[0] if lite else healthy[0][1]}")
    else:
        print("\n지금은 전부 붐빕니다. 몇 분 뒤 다시 시도하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
