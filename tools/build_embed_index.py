"""글 좌표 모델 비교용 색인. 저장된 장면 카드를 후보 모델로 다시 색인합니다. 담당 D.

장면 카드의 글(음성 · 자막 · 장면 설명)은 SQLite 에 있는 그대로 쓰고 좌표만 후보 모델로 만듭니다.
후보마다 따로 창고(shorts_segments__emb_<이름>)를 두므로 기본 창고와 보관함 · 검색 탭은 그대로입니다.
**Gemini 생성 호출(캡션 · 음성 · 요약)은 하지 않습니다.** gemini 후보만 임베딩 API 를 부릅니다.

    python -m tools.build_embed_index                      # 후보 목록과 준비 상태만 (아무것도 안 바꿈)
    python -m tools.build_embed_index --model kure --yes
    python -m tools.build_embed_index --model all --yes    # 준비된 후보 전부
    python -m tools.build_embed_index --model arctic --yes --recreate   # 후보 모델을 바꿨을 때

색인한 뒤 검색: server.embed_compare.search(...) 또는 POST /api/embed-compare/search {"query", "model"}
후보 목록 · 접두어: server/config.py 의 EMBED_CANDIDATES.

⚠ 로컬 Qdrant 는 프로세스 하나만 붙을 수 있습니다. **서버를 먼저 끄세요.**
⚠ 영상을 새로 저장하면 후보 창고에는 자동으로 들어가지 않습니다. 비교 전에 이 도구를 다시 돌리세요.
"""
from __future__ import annotations

import argparse
import sys
import time

from server import db, embed_compare
from server.config import EMBED_CANDIDATES, embed_candidate_info


def main() -> int:
    parser = argparse.ArgumentParser(description="글 좌표 후보 모델로 장면 카드를 다시 색인합니다")
    parser.add_argument("--model", action="append", default=[],
                        help=f"후보 이름 (여러 번 가능, all = 준비된 후보 전부): {', '.join(EMBED_CANDIDATES)}")
    parser.add_argument("--yes", action="store_true", help="실제로 실행 (없으면 계획만)")
    parser.add_argument("--video", default=None, help="이 영상 하나만")
    parser.add_argument("--user", default=None, help="이 사용자의 영상만")
    parser.add_argument("--recreate", action="store_true", help="후보 창고를 비우고 새로 만듭니다")
    args = parser.parse_args()
    db.init_db()

    print("후보 (server/config.py EMBED_CANDIDATES)")
    infos = {name: embed_candidate_info(name) for name in EMBED_CANDIDATES}
    for name, info in infos.items():
        state = "준비됨" if info["ready"] else f"없음: {', '.join(info['missing'])}"
        prefix = f"  질문 {info['query_prefix']!r} / 장면 {info['doc_prefix']!r}" if info["text_embed"] == "st" else ""
        print(f"  {name:<8} {info['model']:<45} {state}{prefix}")

    if "all" in args.model:
        models = [name for name, info in infos.items() if info["ready"] and info["text_embed"] != "gemini"]
        print("\nall: gemini 후보는 API 를 부르므로 빼고, 준비된 로컬 후보만 색인합니다(넣으려면 --model gemini).")
    else:
        models = args.model
    unknown = [m for m in models if m not in EMBED_CANDIDATES]
    if unknown:
        print(f"\n모르는 후보: {', '.join(unknown)}", file=sys.stderr)
        return 2

    rows = embed_compare.ready_videos(args.user, args.video)
    segments = sum(len(db.list_segments(uid, vid)) for uid, vid, _ in rows)
    print(f"\n대상: 영상 {len(rows)}개 / 장면 {segments}개")
    if not models:
        print("색인할 후보를 --model 로 고르세요.")
        return 0
    if not args.yes:
        print(f"색인할 후보: {', '.join(models)}")
        print("계획만 보여줬습니다. 실제로 하려면 --yes 를 붙이세요.")
        return 0

    failed = 0
    for model in models:
        print(f"\n[{model}] {infos[model]['label']}")
        started = time.time()
        try:
            report = embed_compare.build_index(model, args.user, args.video, recreate=args.recreate)
        except Exception as exc:  # noqa: BLE001 - 한 후보가 실패해도(모델 내려받기 등) 나머지는 계속합니다
            if "already accessed" in str(exc):
                print("서버가 Qdrant 를 잡고 있습니다. 서버를 끄고 다시 실행하세요.", file=sys.stderr)
                return 2
            print(f"  건너뜀: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            failed += 1
            continue
        print(f"  영상 {report['videos']}개 / 장면 {report['segments']}개 · {time.time() - started:.0f}초")
        if report["without_image"]:
            print(f"  ⚠ 그림 좌표가 없는 영상 {report['without_image']}개 — 이 영상들은 글 좌표만으로 찾습니다")
        if report["failed"]:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
