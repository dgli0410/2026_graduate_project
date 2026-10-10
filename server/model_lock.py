"""로컬 모델을 처음 올릴 때 쓰는 **하나의** 잠금.

worker 는 영상 두 개를 동시에 처리하고(CONCURRENCY=2), 검색 요청은 또 다른 스레드에서
옵니다. 잠금이 없으면 같은 모델이 두 벌 올라가 16GB 노트북에서 메모리가 터집니다.

모델마다 따로 잠그지 않고 **모든 모델이 이 잠금 하나를 같이 씁니다.** 서로 다른 스레드가
같은 순간에 transformers 를 처음 import 하다가 반쯤 초기화된 모듈에서 ImportError 가 나,
그 영상의 음성 근거가 통째로 사라진 일이 실측됐습니다.

이미 올라간 뒤에는 아무도 이 잠금을 잡지 않습니다(`is None` 확인을 잠금 밖에서 먼저 함).
"""
from __future__ import annotations

import threading

MODEL_LOCK = threading.RLock()
