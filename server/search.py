"""단계 12 · 검색 (하이브리드) + RAG 답변. 담당 D.

세 가지 신호를 섞습니다. 마스터 문서 단계 12 의 비중 표를 그대로 구현했습니다.

    최종점수 = w_dense·글좌표 + w_lexical·정확한단어 + w_image·그림좌표

⚠ 이 비중 조정이 팀이 직접 설계하고 실험할 부분이며, 심사에서 기술적 기여로
  제시할 핵심입니다(마스터 문서 단계 12, 9장). WEIGHTS 를 실험 대상으로 두세요.
"""
from __future__ import annotations

import re
from typing import Any

from server import db, embedder, image_embedder, vectors
from server.config import ANSWER_MODEL, DEDUP_OVERLAP_SECONDS, SEEK_LEAD_SECONDS

# (글 좌표, 정확한 단어, 그림 좌표)
WEIGHTS: dict[str, tuple[float, float, float]] = {
    "speech": (0.5, 0.3, 0.2),   # "감자 언제 넣어?"
    "visual": (0.3, 0.1, 0.6),   # "빨간 냄비 나오는 장면"
    "lexical": (0.3, 0.5, 0.2),  # "300g", "2큰술"
}

VISUAL_TERMS = ("색", "보이는", "모양", "빨간", "노란", "파란", "초록", "하얀", "검은",
                "그릇", "냄비", "프라이팬", "화면", "장면 사진", "노릇", "그을")
QUANTITY_RE = re.compile(r"\d+\s*(g|kg|ml|l|개|스푼|큰술|작은술|컵|분|초|인분|%)", re.IGNORECASE)
TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}|\d+[a-zA-Z가-힣]*")

VIDEO_INTENT_PATTERNS = (
    r"(영상|쇼츠|클립|비디오)\s*(찾아|검색|보여|알려|어디|있|없)",
    r"(영상|쇼츠|클립|비디오)$",
)
SCENE_INTENT_TERMS = ("장면", "부분", "언제", "어디서", "몇 초", "타이밍", "순간")
SUMMARY_INTENT_TERMS = ("요약", "정리", "설명해", "재료", "순서", "레시피 알려", "알려줘")


def classify_query(query: str) -> str:
    """검색어 성격 -> speech | visual | lexical"""
    if QUANTITY_RE.search(query):
        return "lexical"
    if any(term in query for term in VISUAL_TERMS):
        return "visual"
    return "speech"


def detect_intent(query: str) -> str:
    """video = 어느 영상인지 / scene = 그 안 어느 지점인지 / summary = 정리 요청"""
    if any(term in query for term in SCENE_INTENT_TERMS):
        return "scene"
    if any(term in query for term in SUMMARY_INTENT_TERMS):
        return "summary"
    if any(re.search(p, query) for p in VIDEO_INTENT_PATTERNS):
        return "video"
    return "scene"


def weights_for(query: str) -> tuple[float, float, float]:
    dense, lexical, image = WEIGHTS[classify_query(query)]
    if not image_embedder.is_enabled():
        # 그림 좌표가 없으면 그 몫을 나머지 둘에 비례 배분합니다.
        total = dense + lexical
        return dense / total, lexical / total, 0.0
    return dense, lexical, image


# --- 신호 계산 -------------------------------------------------------------

def minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def payload_text(hit: dict[str, Any]) -> str:
    return " ".join(
        str(hit.get(key, "")) for key in ("title", "caption", "asr_text", "ocr_text")
    )


def lexical_score(query: str, hit: dict[str, Any]) -> float:
    """정확한 단어가 실제로 들어 있는지. 숫자·고유명사에 특히 중요합니다."""
    tokens = TOKEN_RE.findall(query)
    if not tokens:
        return 0.0
    text = payload_text(hit)
    matched = sum(1 for token in tokens if token in text)
    return matched / len(tokens)


def _merge(pools: list[list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for pool in pools:
        for hit in pool:
            key = str(hit.get("segment_id") or f"{hit.get('video_id')}:{hit.get('start_time')}")
            merged.setdefault(key, dict(hit))
    return merged


def hybrid_search(
    user_id: str,
    query: str,
    top_n: int = 40,
    video_id: str | None = None,
    video_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    w_dense, w_lexical, w_image = weights_for(query)

    text_hits = vectors.search(
        user_id,
        embedder.encode([query], is_query=True)[0],
        using=vectors.TEXT_VECTOR,
        top_n=top_n,
        video_id=video_id,
        video_ids=video_ids,
    )
    image_hits: list[dict[str, Any]] = []
    if w_image > 0 and vectors.has_image_vector():
        image_hits = vectors.search(
            user_id,
            image_embedder.encode_texts([query])[0],
            using=vectors.IMAGE_VECTOR,
            top_n=top_n,
            video_id=video_id,
            video_ids=video_ids,
        )

    text_scores = dict(
        zip(
            [h.get("segment_id") for h in text_hits],
            minmax([h["score"] for h in text_hits]),
        )
    )
    image_scores = dict(
        zip(
            [h.get("segment_id") for h in image_hits],
            minmax([h["score"] for h in image_hits]),
        )
    )

    merged = _merge([text_hits, image_hits])
    ranked: list[dict[str, Any]] = []
    for hit in merged.values():
        seg = hit.get("segment_id")
        dense = text_scores.get(seg, 0.0)
        image = image_scores.get(seg, 0.0)
        lexical = lexical_score(query, hit)
        hit["score_dense"] = round(dense, 4)
        hit["score_lexical"] = round(lexical, 4)
        hit["score_image"] = round(image, 4)
        hit["score"] = round(w_dense * dense + w_lexical * lexical + w_image * image, 4)
        ranked.append(hit)

    ranked.sort(key=lambda h: h["score"], reverse=True)
    return ranked, {"dense": w_dense, "lexical": w_lexical, "image": w_image}


# --- 결과 정리 -------------------------------------------------------------

def dedup_adjacent(
    hits: list[dict[str, Any]], top_k: int, overlap: float = DEDUP_OVERLAP_SECONDS
) -> list[dict[str, Any]]:
    """같은 영상에서 시간이 겹치는 장면은 하나만 남깁니다."""
    kept: list[dict[str, Any]] = []
    for hit in hits:
        vid = hit.get("video_id")
        start, end = float(hit.get("start_time", 0)), float(hit.get("end_time", 0))
        duplicate = any(
            k.get("video_id") == vid
            and start <= float(k.get("end_time", 0)) + overlap
            and end >= float(k.get("start_time", 0)) - overlap
            for k in kept
        )
        if not duplicate:
            kept.append(hit)
        if len(kept) >= top_k:
            break
    return kept


def group_by_video(hits: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for hit in hits:
        vid = hit.get("video_id", "")
        if not vid:
            continue
        current = grouped.get(vid)
        if current is None:
            grouped[vid] = {
                "video_id": vid,
                "title": hit.get("title", ""),
                "score": hit["score"],
                "scene_count": 1,
                "best_time": float(hit.get("start_time", 0)),
                "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
            }
        else:
            current["scene_count"] += 1
            if hit["score"] > current["score"]:
                current["score"] = hit["score"]
                current["best_time"] = float(hit.get("start_time", 0))
    return sorted(grouped.values(), key=lambda v: v["score"], reverse=True)[:limit]


# --- RAG -------------------------------------------------------------------

ANSWER_PROMPT = """
너는 쇼츠 보관함 검색 도우미야.
아래 "찾은 장면"에 적힌 내용만 근거로 사용자 질문에 한국어로 답해줘.

규칙:
- 장면에 없는 내용은 절대 지어내지 마. 모르면 "저장된 장면에서는 확인되지 않습니다" 라고 해.
- 시점을 묻는 질문이면 몇 초인지 함께 말해줘.
- 재료만 물으면 재료만, 순서를 물으면 순서만 답해. 묻지 않은 건 넣지 마.
- 세 문장 안으로 끝내. 단, 순서/정리 요청이면 목록으로 답해도 돼.

사용자 질문:
{query}

찾은 장면:
{context}
""".strip()


def _context(hits: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{idx}. [{hit.get('title', '')}] {float(hit.get('start_time', 0)):.1f}초~"
        f"{float(hit.get('end_time', 0)):.1f}초 | 말: {hit.get('asr_text') or '-'}"
        f" | 자막: {hit.get('ocr_text') or '-'} | 장면: {hit.get('caption') or '-'}"
        for idx, hit in enumerate(hits, start=1)
    )


def generate_answer(query: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "저장된 영상 중에서 관련된 장면을 찾지 못했습니다."
    try:
        from server.gemini import get_client

        response = get_client().models.generate_content(
            model=ANSWER_MODEL, contents=ANSWER_PROMPT.format(query=query, context=_context(hits))
        )
        text = (getattr(response, "text", "") or "").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001 - 답변 생성이 실패해도 검색 결과는 보여줍니다
        pass
    top = hits[0]
    return (
        f"가장 가까운 장면은 [{top.get('title', '')}] {float(top.get('start_time', 0)):.0f}초 "
        "지점입니다. (AI 답변 생성에 실패해 검색 결과만 표시합니다)"
    )


# --- 진입점 ---------------------------------------------------------------

def search(
    user_id: str,
    query: str,
    video_id: str | None = None,
    playlist_id: str | None = None,
    top_k: int = 5,
    with_answer: bool = True,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"query": query, "scenes": [], "videos": [], "answer": ""}

    video_ids = None
    if playlist_id and not video_id:
        video_ids = db.playlist_video_ids(user_id, playlist_id)

    intent = detect_intent(query)
    ranked, weights = hybrid_search(
        user_id, query, top_n=max(top_k * 8, 40), video_id=video_id, video_ids=video_ids
    )
    # 요약 요청이면 근거 장면을 넉넉히 넘겨줍니다.
    scene_limit = max(top_k, 10) if intent == "summary" else top_k
    scenes = dedup_adjacent(ranked, top_k=scene_limit)

    for scene in scenes:
        start = float(scene.get("start_time", 0))
        # 마스터 문서 단계 13 · 1.5초 일찍 보냅니다.
        scene["seek_time"] = round(max(0.0, start - SEEK_LEAD_SECONDS), 2)
        scene["youtube_fallback_url"] = (
            f"https://www.youtube.com/watch?v={scene.get('video_id')}&t={int(scene['seek_time'])}s"
        )
        if scene.get("frame_path"):
            scene["frame_url"] = (
                f"/api/videos/{scene.get('video_id')}/frames/"
                f"{str(scene['frame_path']).rsplit('/', 1)[-1]}"
            )

    return {
        "query": query,
        "intent": intent,
        "query_type": classify_query(query),
        "weights": weights,
        "answer": generate_answer(query, scenes) if with_answer else "",
        "scenes": scenes[:top_k] if intent != "summary" else scenes,
        "videos": group_by_video(ranked),
        "top_score": scenes[0]["score"] if scenes else 0.0,
    }
