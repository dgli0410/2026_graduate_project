"""단계 2 · 브라우저 추출이 실제로 됐는지 확인합니다.

확장프로그램에서 저장 버튼을 누른 뒤 이걸 돌리면, 서버에 뭐가 올라왔는지
프레임 수 · 시간 범위 · 오디오 크기까지 보여줍니다.

    python -m tools.check_capture           한 번 보고 끝
    python -m tools.check_capture --watch   3초마다 갱신 (저장 누르기 전에 켜두면 편함)
"""
from __future__ import annotations

import sys
import time
import wave

from server import db, media
from server.config import FRAME_INTERVAL, MEDIA_DIR


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size / 1:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


def _audio_info(path) -> str:
    if not path.exists():
        return "❌ 없음 (오디오 추출 실패 → 음성 분석 없이 자막·장면만으로 진행됩니다)"
    size = path.stat().st_size
    if size == 0:
        return "❌ 0바이트"
    try:
        with wave.open(str(path), "rb") as f:
            seconds = f.getnframes() / f.getframerate()
            return (
                f"✅ {_human(size)} / {seconds:.1f}초 / "
                f"{f.getframerate()}Hz {f.getnchannels()}ch"
            )
    except Exception as exc:  # noqa: BLE001
        return f"⚠ wav 로 못 읽음 ({exc}) — {_human(size)}"


def report() -> None:
    found = 0
    for user_dir in sorted(MEDIA_DIR.iterdir()) if MEDIA_DIR.exists() else []:
        if not user_dir.is_dir():
            continue
        for video_dir in sorted(user_dir.iterdir()):
            if not video_dir.is_dir():
                continue
            found += 1
            user_id, video_id = user_dir.name, video_dir.name
            frames = media.list_frames(user_id, video_id)
            video = db.get_video(user_id, video_id)

            print("=" * 66)
            title = video["title"] if video else "(DB 에 없음)"
            status = f"{video['progress']}% {video['status_label']}" if video else "-"
            print(f"{video_id}  {title}")
            print(f"  상태      : {status}")
            if video and video["error"]:
                print(f"  오류      : {video['error']}")

            if not frames:
                print("  프레임    : ❌ 0장 — 화면 추출이 실패했습니다")
            else:
                first, last = frames[0][0], frames[-1][0]
                size = sum(p.stat().st_size for _, p in frames)
                expected = int((last - first) / FRAME_INTERVAL) + 1
                print(
                    f"  프레임    : ✅ {len(frames)}장 / {first:.1f}초~{last:.1f}초"
                    f" / {_human(size)}"
                )
                if expected > len(frames):
                    skipped = expected - len(frames)
                    print(f"              (중복 제거로 {skipped}장 건너뜀 — 정상입니다)")
                if last < 3:
                    print("              ⚠ 시간 범위가 너무 짧습니다. 추출이 중간에 끊겼을 수 있습니다.")

            print(f"  오디오    : {_audio_info(media.audio_path(user_id, video_id))}")

            if video and video["status"] == "ready":
                segments = db.list_segments(user_id, video_id)
                with_ocr = sum(1 for s in segments if s["ocr_text"])
                with_asr = sum(1 for s in segments if s["asr_text"])
                print(
                    f"  장면 카드 : {len(segments)}장 "
                    f"(자막 있는 카드 {with_ocr}장, 말 있는 카드 {with_asr}장)"
                )
                for seg in segments[:3]:
                    print(
                        f"      {seg['start_time']:5.1f}~{seg['end_time']:5.1f}s"
                        f" | 말:{(seg['asr_text'] or '-')[:20]}"
                        f" | 자막:{(seg['ocr_text'] or '-')[:14]}"
                        f" | 장면:{(seg['caption'] or '-')[:28]}"
                    )

    if found == 0:
        print(f"아직 올라온 게 없습니다.  ({MEDIA_DIR})")
        print("확장프로그램에서 '이 쇼츠 저장' 을 눌러보세요.")


def main(argv: list[str]) -> int:
    db.init_db()
    if "--watch" in argv:
        try:
            while True:
                print("\033[2J\033[H", end="")  # 화면 지우기
                print(f"[{time.strftime('%H:%M:%S')}] 3초마다 갱신 · Ctrl+C 로 종료\n")
                report()
                time.sleep(3)
        except KeyboardInterrupt:
            return 0
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
