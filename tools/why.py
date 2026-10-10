"""검색 결과가 왜 그렇게 나왔는지 본다.

세 신호(글 좌표 / 정확한 단어 / 그림 좌표)가 각각 몇 점을 줬는지 분해해서 보여준다.
확장프로그램은 최종 결과만 보여주므로 원인 추적이 불가능하다.

서버가 떠 있어야 한다:
    python -m uvicorn server.main:app --port 8000

사용:
    python -m tools.why 고기 위에 밀가루가 올라가있는 장면      전체 영상에서
    python -m tools.why --in 김치찜 다진마늘 몇 큰술 넣어?      그 영상 안에서만
    python -m tools.why --list                                 저장된 영상 목록
    python -m tools.why                                        (질의를 물어본다)

--in 은 제목 일부나 video_id 를 받습니다. 실제 사용에서는 "어느 영상인지" 를 먼저
정하고 그 안에서 장면을 찾는 것이 자연스럽습니다(2단계 검색). 전체 검색은
"어느 영상이었지" 를 물을 때만 말이 됩니다.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://localhost:8000/api/search"


def first_user_id() -> str:
    db = ROOT / "data" / "vault.db"
    if not db.exists():
        raise SystemExit("data/vault.db 가 없습니다. 영상을 먼저 저장하세요.")
    con = sqlite3.connect(db)
    row = con.execute("SELECT user_id FROM videos LIMIT 1").fetchone()
    if not row:
        raise SystemExit("저장된 영상이 없습니다.")
    return row[0]


def videos(user_id: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(ROOT / "data" / "vault.db")
    rows = con.execute(
        "SELECT video_id, title FROM videos WHERE user_id=? AND status='ready'", (user_id,)
    ).fetchall()
    return [(r[0], r[1] or "") for r in rows]


def resolve(needle: str, user_id: str) -> str:
    """제목 일부 또는 video_id -> video_id."""
    found = [
        vid for vid, title in videos(user_id)
        if needle == vid or needle.lower() in title.lower()
    ]
    if not found:
        raise SystemExit(f"'{needle}' 에 맞는 영상이 없습니다. --list 로 확인하세요.")
    if len(found) > 1:
        raise SystemExit(f"'{needle}' 가 영상 {len(found)}개에 걸립니다. 더 정확히 적으세요.")
    return found[0]


def ask(query: str, user_id: str, video_id: str | None = None) -> dict:
    payload = {"query": query, "top_k": 5, "with_answer": False}
    if video_id:
        payload["video_id"] = video_id
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        URL, body, {"Content-Type": "application/json", "X-User-Id": user_id}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        raise SystemExit(f"서버에 연결하지 못했습니다: {e}\n"
                         f"python -m uvicorn server.main:app --port 8000 으로 띄우세요.")


def bar(value: float, width: int = 20) -> str:
    n = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * n + "·" * (width - n)


def main() -> None:
    args = sys.argv[1:]
    uid = first_user_id()

    if "--list" in args:
        for vid, title in videos(uid):
            print(f"  {vid:14s} {title}")
        return

    scope = None
    if "--in" in args:
        i = args.index("--in")
        scope = resolve(args[i + 1], uid)
        args = args[:i] + args[i + 2:]

    query = " ".join(args).strip() or input("질의: ").strip()
    if not query:
        return

    res = ask(query, uid, scope)
    w = res.get("weights", {})

    print("=" * 74)
    print(f"질의   {query}")
    print(f"범위   {scope or '전체 영상'}")
    print(f"분류   query_type={res.get('query_type')}   intent={res.get('intent')}")
    print(f"가중치 글 {w.get('dense', 0):.2f}  |  단어 {w.get('lexical', 0):.2f}"
          f"  |  그림 {w.get('image', 0):.2f}")
    if w.get("image", 0) == 0:
        print("       ⚠ 그림 좌표 가중치가 0 — SigLIP 이 꺼져 있거나 벡터가 없습니다")
    elif w.get("image", 0) < 0.3:
        print("       ⚠ 시각 질의로 분류되지 않아 그림 좌표 비중이 낮습니다")
    print("=" * 74)

    scenes = res.get("scenes", [])
    if not scenes:
        print("결과 없음")
        return

    image_helped = 0
    for i, s in enumerate(scenes, 1):
        d, lx, im = s.get("score_dense", 0), s.get("score_lexical", 0), s.get("score_image", 0)
        contrib = {"글": w.get("dense", 0) * d,
                   "단어": w.get("lexical", 0) * lx,
                   "그림": w.get("image", 0) * im}
        winner = max(contrib, key=contrib.get)
        if winner == "그림":
            image_helped += 1

        print(f"\n{i}. [{s.get('title', '')[:28]}] "
              f"{float(s.get('start_time', 0)):.1f}~{float(s.get('end_time', 0)):.1f}초"
              f"   최종 {s.get('score', 0):.4f}")
        for name, raw, c in (("글  ", d, contrib["글"]),
                             ("단어", lx, contrib["단어"]),
                             ("그림", im, contrib["그림"])):
            mark = " ←" if name.strip() == winner else ""
            print(f"     {name} {bar(raw)} {raw:.3f}  → 기여 {c:.4f}{mark}")
        cap = (s.get("caption") or "").replace("\n", " ")
        ocr = (s.get("ocr_text") or "").replace("\n", " ")
        asr = (s.get("asr_text") or "").replace("\n", " ")
        print(f"     장면: {cap[:90] or '- (캡션 없음)'}")
        print(f"     자막: {ocr[:60] or '-'}   말: {asr[:60] or '-'}")
        fp = s.get("frame_path")
        if fp:
            name_ = str(fp).rsplit("/", 1)[-1]
            vid = s.get("video_id")
            print(f"     프레임: data/media/{uid}/{vid}/frames/{name_}")
        if not cap and im > 0.8:
            print("     ★ 캡션이 없는데 그림 좌표가 높습니다 — SigLIP 단독으로 찾은 장면입니다")

    print("\n" + "=" * 74)
    print(f"그림 좌표(SigLIP)가 1등 기여였던 장면: {image_helped}/{len(scenes)}")
    # ★ 진짜 질문은 "어느 신호가 많이 기여했나" 가 아니라 "빼면 답이 바뀌나" 입니다.
    #   기여도가 낮아도 순위를 뒤집을 수 있고, 높아도 순위를 안 바꿀 수 있습니다.
    #   (그림 좌표를 끄면 그 몫이 나머지 둘에 비례 배분됩니다 - search.weights_for)
    d, lx = w.get("dense", 0), w.get("lexical", 0)
    if w.get("image", 0) > 0 and d + lx > 0:
        nd, nl = d / (d + lx), lx / (d + lx)
        without = sorted(
            scenes,
            key=lambda s: nd * s.get("score_dense", 0) + nl * s.get("score_lexical", 0),
            reverse=True,
        )

        def label(s):
            return f"{float(s.get('start_time', 0)):.1f}~{float(s.get('end_time', 0)):.1f}초"

        if without[0].get("segment_id") != scenes[0].get("segment_id"):
            print()
            print("  ★ 그림 좌표가 1위를 바꿨습니다")
            print(f"     끄면 -> {label(without[0])}")
            print(f"     켜면 -> {label(scenes[0])}")
        else:
            print()
            print(f"  -> 그림 좌표를 빼도 1위는 같습니다({label(scenes[0])}).")
            print("     이 질의에서는 SigLIP 이 답을 바꾸지 않았습니다.")
        print("     ※ 후보를 모으는 단계는 그대로 둔 어림값입니다. 정확한 비교는")
        print("       색인 전체에서 그림 좌표를 끄고 다시 돌려야 합니다.")
    print("=" * 74)


if __name__ == "__main__":
    main()
