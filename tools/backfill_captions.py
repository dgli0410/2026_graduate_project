"""장면 설명 백필 — 할당량·503 때문에 캡션이 비어 있는 영상을 채웁니다. 담당 D.

멘토 2차 자문(2026-10-12)이 전제를 걸었습니다:

> 장면 설명(Gemini)을 제외한 조건(0.806)이 기준선보다 높으므로, **장면 설명 생성
> 완료 후 공식 기준선을 확정**하고 비용 대비 효과를 재검토할 것을 권고함.

즉 **설명이 다 채워져야 공식 기준선을 말할 수 있습니다.** 지금은 채움률 55% 라
모든 수치가 잠정값입니다.

**다시 저장할 필요가 없습니다.** 프레임이 `data/media/` 에 그대로 있어서, 저장된
프레임으로 **설명만 다시 뽑아** 기존 장면 카드에 채워 넣으면 됩니다.

하는 일:
  1. 설명이 0개인 영상을 찾습니다
  2. 저장된 프레임으로 `caption.describe_with_health()` 실행 (영상당 Gemini 1회)
  3. 시간 창으로 기존 장면 카드에 설명을 배정 — **카드 경계는 건드리지 않습니다**
  4. 글 좌표를 다시 만들어 Qdrant 에 반영 (설명이 바뀌면 좌표도 바뀝니다)

⚠ 3번이 중요합니다. `build_segments` 를 다시 돌리면 카드 경계가 달라질 수 있는데,
  그러면 평가셋 라벨과의 대응이 흔들립니다. 기존 카드를 **그대로 두고** 설명만
  채웁니다.

⚠ 4번을 빠뜨리면 DB 에는 설명이 있는데 **검색은 예전 좌표를 쓰게 됩니다.** 조용히
  틀린 결과가 나오므로 반드시 같이 합니다.

    python -m tools.backfill_captions            # 계획만 (호출 0회)
    python -m tools.backfill_captions --yes      # 실제로 채우기
    python -m tools.backfill_captions --yes --video OgR00HBMKFs

⚠ 로컬 Qdrant 는 프로세스 하나만 붙습니다. **서버를 끄고 실행하세요.**
⚠ Gemini 무료 티어는 모델당 하루 20회입니다. 영상 1개 = 1회.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

from server import db, embedder, image_embedder, media, vectors
from server.config import CAPTION_MODEL, DB_PATH, GRID_FRAMES, MAX_GEMINI_CALLS_PER_VIDEO
from server.pipeline import caption as caption_mod
from server.pipeline.segmenter import _points_in_window, search_text


def blank_videos(user_id: str, only: str | None) -> list[tuple[str, str]]:
    """(video_id, title) — 설명이 하나도 없는 ready 영상."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT v.video_id, v.title
        FROM videos v
        WHERE v.status = 'ready' AND v.user_id = ?
          AND NOT EXISTS (
            SELECT 1 FROM segments s
            WHERE s.video_id = v.video_id AND s.user_id = v.user_id
              AND COALESCE(s.caption, '') <> ''
          )
        ORDER BY v.created_at
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    out = [(r["video_id"], r["title"] or "") for r in rows]
    return [p for p in out if p[0] == only] if only else out


def fill_one(user_id: str, video_id: str, title: str) -> dict:
    """영상 하나의 설명을 채우고 다시 색인합니다."""
    frames = media.list_frames(user_id, video_id)
    if not frames:
        return {"ok": False, "reason": "프레임이 없습니다"}

    items, health = caption_mod.describe_with_health(frames)

    def why() -> str:
        """왜 안 됐는지 **진짜 이유**를 돌려줍니다.

        ⚠ 처음에는 `items` 가 비었는지만 보고 "장면 카드 시간대에 걸리는 설명이
          없습니다" 라고 적었습니다. 503(구글 과부하)으로 호출이 통째로 실패한
          경우에도 그렇게 찍혀서 원인이 데이터 문제처럼 보였습니다. health 에
          실패 기록이 그대로 있는데 그걸 안 읽은 것이 문제였습니다.
        """
        if health.get("failed_calls"):
            code = health.get("error_code") or health.get("error_type") or "호출 실패"
            msg = str(health.get("error") or health.get("message") or "")[:80]
            return f"Gemini {code}" + (f" — {msg}" if msg else "")
        if not items:
            return str(health.get("status") or "설명이 비었습니다")
        return "장면 카드 시간대에 걸리는 설명이 없습니다"

    if not items or health.get("failed_calls"):
        return {"ok": False, "reason": why(), "health": health}

    segments = db.list_segments(user_id, video_id)
    filled = 0
    for seg in segments:
        # ⚠ 카드 경계는 그대로 둡니다. 시간 창만 보고 설명을 배정합니다.
        text = _points_in_window(
            items, float(seg["start_time"]), float(seg["end_time"]), key="caption"
        )
        if text:
            seg["caption"] = text
            filled += 1
    if not filled:
        return {"ok": False, "reason": why(), "health": health}

    db.replace_segments(user_id, video_id, segments)

    # 설명이 바뀌면 글 좌표도 바뀝니다. 안 하면 검색이 예전 좌표를 씁니다.
    texts = [search_text(s, title=title) for s in segments]
    images = None
    if image_embedder.is_enabled():
        frame_dir = media.frames_dir(user_id, video_id)
        paths = [frame_dir / str(s["frame_path"]).rsplit("/", 1)[-1] for s in segments]
        if all(p.is_file() for p in paths):
            images = image_embedder.encode_images(paths)
    vectors.upsert_segments(
        user_id, video_id, title, segments, embedder.encode(texts), images
    )
    return {"ok": True, "filled": filled, "segments": len(segments), "items": len(items)}


def coverage(user_id: str) -> str:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        """SELECT COUNT(*) n,
             SUM(CASE WHEN COALESCE(caption,'')  <> '' THEN 1 ELSE 0 END) cap,
             SUM(CASE WHEN COALESCE(asr_text,'') <> '' THEN 1 ELSE 0 END) asr,
             SUM(CASE WHEN COALESCE(ocr_text,'') <> '' THEN 1 ELSE 0 END) ocr
           FROM segments WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    conn.close()
    n = r["n"] or 1
    return (f"장면 {r['n']}개 — 말 {r['asr']} ({r['asr']/n:.0%}) "
            f"자막 {r['ocr']} ({r['ocr']/n:.0%}) 설명 {r['cap']} ({r['cap']/n:.0%})")


def main() -> int:
    p = argparse.ArgumentParser(description="설명이 빈 영상의 장면 설명을 채웁니다")
    p.add_argument("--yes", action="store_true", help="실제로 실행 (없으면 계획만)")
    p.add_argument("--video", default=None, help="이 영상 하나만")
    args = p.parse_args()

    try:
        vectors.get_client()
    except RuntimeError as exc:
        if "already accessed" in str(exc):
            print("서버가 Qdrant 를 잡고 있습니다. 서버를 끄고 다시 실행하세요.", file=sys.stderr)
            return 2
        raise

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    row = conn.execute("SELECT user_id FROM videos LIMIT 1").fetchone()
    conn.close()
    if not row:
        print("저장된 영상이 없습니다.")
        return 1
    user_id = row[0]

    targets = blank_videos(user_id, args.video)
    print(f"지금: {coverage(user_id)}")
    if not targets:
        print("\n설명이 빈 영상이 없습니다. 채울 것이 없습니다.")
        return 0

    print(f"\n설명이 0개인 영상 {len(targets)}개:")
    for vid, title in targets:
        print(f"   {vid:<13} {title[:36]}")
    print(f"\n모델 {CAPTION_MODEL} · 영상당 {MAX_GEMINI_CALLS_PER_VIDEO}회"
          f" → Gemini **{len(targets) * MAX_GEMINI_CALLS_PER_VIDEO}회**"
          f" (무료 티어 하루 20회)")
    points = GRID_FRAMES * MAX_GEMINI_CALLS_PER_VIDEO
    print(f"\n[상한] 한 영상이 받을 수 있는 설명 지점 = GRID_FRAMES({GRID_FRAMES})"
          f" x 호출({MAX_GEMINI_CALLS_PER_VIDEO}) = {points}개")
    print("  장면이 그보다 많은 영상은 **설계상 100% 가 될 수 없습니다.**")
    print("  더 채우려면 MAX_GEMINI_CALLS_PER_VIDEO 를 올려야 하고, 하루 처리량이 그만큼 줍니다.")

    if not args.yes:
        print("\n계획만 보여줬습니다. 실제로 하려면 --yes 를 붙이세요.")
        return 0

    print("\n--- 채우는 중 ---")
    ok = 0
    for vid, title in targets:
        started = time.time()
        try:
            r = fill_one(user_id, vid, title)
        except Exception as exc:  # noqa: BLE001 - 하나가 실패해도 나머지는 계속합니다
            r = {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
        if r["ok"]:
            ok += 1
            print(f"  완료 {vid}  장면 {r['filled']}/{r['segments']}개에 설명"
                  f"  ({time.time() - started:.0f}초)")
        else:
            print(f"  실패 {vid}  {r['reason']}")

    vectors.get_client().close()
    print(f"\n{ok}/{len(targets)}개 영상을 채웠습니다.")
    print(f"지금: {coverage(user_id)}")
    if ok < len(targets):
        print("\n실패한 영상은 할당량이 풀린 뒤(또는 503 이 가라앉은 뒤) 다시 돌리면 됩니다.")
    if ok:
        print("\n다음: 서버를 켜고 `python -m tools.eval_search --all` 로 재측정하세요.")
        print("      이 값이 멘토가 말한 **공식 기준선**이 됩니다.")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
