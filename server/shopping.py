"""재료 → 구매 링크의 "고급" 부분: 네이버쇼핑 검색 API 프록시.

키가 없어도 됩니다 — 그때는 확장프로그램이 검색 URL 링크만 보여줍니다.
키가 있으면 최저가·판매처를 함께 보여줍니다 (무료, 하루 25,000회).

브라우저(content/side panel)에서 직접 부르지 않고 서버가 대신 부르는 이유:
클라이언트 시크릿을 확장프로그램에 넣으면 누구나 꺼내 볼 수 있기 때문입니다.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from server.config import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

_API = "https://openapi.naver.com/v1/search/shop.json"
_CACHE_TTL = 60 * 60  # 같은 재료를 1시간 안에 다시 물으면 API 를 안 부릅니다.
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_TAG = re.compile(r"</?b>")


def is_enabled() -> bool:
    return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)


def search(query: str, display: int = 3) -> list[dict[str, Any]]:
    """재료 이름으로 상품을 찾아 [{title, price, mall, link}] 를 돌려줍니다."""
    query = query.strip()
    if not query or not is_enabled():
        return []

    cached = _cache.get(query)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    url = f"{_API}?{urllib.parse.urlencode({'query': query, 'display': display, 'sort': 'sim'})}"
    request = urllib.request.Request(
        url,
        headers={
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))

    items = [
        {
            "title": _TAG.sub("", item.get("title", "")),
            "price": int(item.get("lprice") or 0),
            "mall": item.get("mallName", ""),
            "link": item.get("link", ""),
        }
        for item in data.get("items", [])
    ]
    _cache[query] = (time.time(), items)
    return items
