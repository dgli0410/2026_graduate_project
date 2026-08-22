"""확장프로그램 흉내를 내서 서버 전 구간을 통과시킵니다.

브라우저 없이, 가짜 프레임과 오디오를 만들어 실제 업로드 → 분석 → 검색까지
돌립니다. 크롬에 확장을 올리기 전에 서버가 멀쩡한지 확인하는 용도입니다.

먼저 다른 창에서 서버를 띄우세요:
    python -m uvicorn server.main:app --port 8000

그다음:
    python -m tools.e2e_test
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from tools.check_gemini import make_fake_frames, make_silent_wav

BASE = "http://127.0.0.1:8000"
USER = f"e2e-{uuid.uuid4().hex[:8]}"
VIDEO_ID = "e2eDemo001"
HEADERS = {"X-User-Id": USER}


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS)
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode() or "null")


def post_multipart(path: str, files: list[tuple[str, str, bytes, str]], fields: dict[str, str]):
    boundary = f"----e2e{uuid.uuid4().hex}"
    buffer = io.BytesIO()

    def write(text: str) -> None:
        buffer.write(text.encode("utf-8"))

    for key, value in fields.items():
        write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n")
    for field, filename, content, mime in files:
        write(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\";"
            f' filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        )
        buffer.write(content)
        write("\r\n")
    write(f"--{boundary}--\r\n")

    headers = dict(HEADERS)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(BASE + path, data=buffer.getvalue(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read().decode())


def main() -> int:
    try:
        health = call("GET", "/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"서버에 연결하지 못했습니다: {exc}")
        print("다른 창에서 먼저 실행하세요: python -m uvicorn server.main:app --port 8000")
        return 1

    print(f"서버 연결됨. device={health['device']} backends={health['backends']}")
    if not health["ok"]:
        print(f"서버가 준비되지 않았습니다: {health['reason']}")
        return 1

    workdir = Path("data") / "e2e"
    frames = make_fake_frames(workdir / "frames")
    wav = make_silent_wav(workdir / "audio.wav")
    print(f"가짜 쇼츠 준비: 프레임 {len(frames)}장, 오디오 {wav.stat().st_size}바이트")

    # 재생목록 만들기
    playlist = call("POST", "/api/playlists", {"name": "e2e 테스트"})
    print(f"[재생목록] {playlist['name']} ({playlist['id']})")

    # 단계 3 · 접수
    accepted = call(
        "POST",
        "/api/videos",
        {
            "video_id": VIDEO_ID,
            "title": "감자조림 만들기 (e2e)",
            "channel": "테스트채널",
            "duration": 24.0,
            "playlist_id": playlist["id"],
        },
    )
    print(f"[단계 3] 접수됨. needs_capture={accepted['needs_capture']}")
    assert accepted["needs_capture"], "이미 저장된 상태입니다. data/ 를 지우고 다시 시도하세요."

    # 단계 2 흉내 · 프레임/오디오 업로드
    files = [("frames", p.name, p.read_bytes(), "image/jpeg") for _, p in frames]
    files.append(("audio", "audio.wav", wav.read_bytes(), "audio/wav"))
    uploaded = post_multipart(
        f"/api/videos/{VIDEO_ID}/media", files, {"duration": "24.0"}
    )
    print(f"[단계 2] 업로드 완료. 프레임 {uploaded['frames']}장, 오디오 {uploaded['audio_bytes']}바이트")

    # 단계 4~10 · 진행 상태 확인 (Side Panel 이 3초마다 하는 일)
    print("[단계 4~10] 분석 진행:")
    deadline = time.time() + 300
    last_status = ""
    while time.time() < deadline:
        video = call("GET", f"/api/videos/{VIDEO_ID}")
        if video["status"] != last_status:
            last_status = video["status"]
            print(f"   {video['progress']:>3}%  {video['status_label']}")
        if video["status"] in ("ready", "failed"):
            break
        time.sleep(2)
    else:
        print("   시간 초과")
        return 1

    if video["status"] == "failed":
        print(f"❌ 분석 실패: {video['error']}")
        return 1

    print(f"\n[단계 7] 장면 카드 {len(video['segments'])}장")
    for seg in video["segments"][:6]:
        print(
            f"   {seg['start_time']:5.1f}~{seg['end_time']:5.1f}s"
            f" | 자막:{seg['ocr_text'] or '-':<12} | 장면:{(seg['caption'] or '-')[:34]}"
        )

    summary = video["summary"] or {}
    print(f"\n[단계 9] 요약: {summary.get('headline', '(없음)')}")
    for step in summary.get("steps", [])[:5]:
        print(f"   {step['time']:5.1f}s  {step['label']}")
    print(f"   처리 시간 분해: {summary.get('timings')} / 총 {summary.get('total_seconds')}초")

    # 프레임 이미지가 실제로 내려오는지 (단계 13 장면 사진)
    if video["segments"]:
        name = video["segments"][0]["frame_path"].rsplit("/", 1)[-1]
        req = urllib.request.Request(
            f"{BASE}/api/videos/{VIDEO_ID}/frames/{name}?u={USER}", method="GET"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            size = len(response.read())
        print(f"\n[단계 13] 장면 사진 내려받기 OK ({name}, {size}바이트)")

    # 단계 12 · 검색
    print("\n[단계 12] 검색")
    for query in ["간장 언제 넣어?", "감자 300g", "붉은 화면 나오는 장면"]:
        result = call("POST", "/api/search", {"query": query, "top_k": 3})
        weights = result["weights"]
        print(f"\n  질의: {query!r}  ({result['query_type']}/{result['intent']},"
              f" 글{weights['dense']:.2f}·단어{weights['lexical']:.2f}·그림{weights['image']:.2f})")
        print(f"  답변: {result['answer']}")
        for scene in result["scenes"]:
            print(
                f"    {scene['start_time']:5.1f}s (이동 {scene['seek_time']:.1f}s)"
                f" score={scene['score']:.3f}"
                f" [글{scene['score_dense']:.2f} 단어{scene['score_lexical']:.2f}]"
                f" {scene['ocr_text'] or scene['caption'][:24]}"
            )
        assert result["scenes"], "검색 결과가 비었습니다"

    # 재생목록 범위 검색
    scoped = call(
        "POST", "/api/search", {"query": "감자", "playlist_id": playlist["id"], "top_k": 3}
    )
    print(f"\n[재생목록 범위 검색] 결과 {len(scoped['scenes'])}건")
    assert scoped["scenes"], "재생목록 범위 검색이 비었습니다"

    # 단계 11 · 삭제 (표·창고·이미지 3곳)
    call("DELETE", f"/api/videos/{VIDEO_ID}")
    after = call("POST", "/api/search", {"query": "감자", "top_k": 3})
    assert not after["scenes"], "❌ 삭제했는데 검색에 계속 나옵니다"
    assert not call("GET", "/api/videos")["videos"], "❌ 보관함에 남아 있습니다"
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{BASE}/api/videos/{VIDEO_ID}/frames/{name}?u={USER}")
        )
        print("❌ 프레임 이미지가 안 지워졌습니다")
        return 1
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    print("[단계 11] 삭제 후 표·창고·이미지 3곳 모두 정리 확인")

    call("DELETE", f"/api/playlists/{playlist['id']}")
    print("\n전 구간 통과했습니다. 남은 건 브라우저 추출(단계 2)뿐입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
