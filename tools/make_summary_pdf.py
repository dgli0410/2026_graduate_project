# -*- coding: utf-8 -*-
"""진행 상황 요약 PDF — 멘토·심사 제출용.

    python -m tools.make_summary_pdf   →  docs/summary.pdf

`docs/PROGRESS.md` 는 963줄입니다. 통합본이라 긴 것이 의도지만 그대로 내밀기엔
부담스럽습니다. **결론과 근거 수치만** 6쪽으로 추립니다.

여기 적힌 숫자는 전부 PROGRESS.md 와 같은 값입니다. 요약하면서 바꾸지 않았습니다.
**숫자를 고칠 일이 생기면 PROGRESS.md 와 이 파일을 같이 고쳐야 합니다.**
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from server.config import ROOT
from tools.make_user_study_pdf import find_chrome

CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic", "맑은 고딕", sans-serif; font-size: 9.4pt;
       color: #111; line-height: 1.45; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 1mm; }
h2 { font-size: 12.5pt; margin: 5mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1.5px solid #222; }
h3 { font-size: 10.5pt; margin: 4mm 0 1.5mm; }
.lead { color: #444; margin: 0 0 3mm; }
table { width: 100%; border-collapse: collapse; margin: 1.5mm 0 3mm; }
th, td { border: 0.6px solid #999; padding: 1.4mm 2mm; text-align: left;
         vertical-align: top; }
th { background: #eceff4; font-weight: 700; }
td.c, th.c { text-align: center; }
td.r, th.r { text-align: right; }
tr.hl td { background: #fff4e6; }
.key { background: #eef5ff; border-left: 3px solid #2c5aa0; padding: 2.5mm 3mm;
       margin: 2mm 0; }
.warn { background: #fdf0ee; border-left: 3px solid #c0392b; padding: 2.5mm 3mm;
        margin: 2mm 0; }
.note { background: #f5f7fa; border-left: 3px solid #666; padding: 2mm 3mm;
        margin: 2mm 0; font-size: 8.8pt; }
small { color: #555; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
.two { display: flex; gap: 4mm; }
.two > div { flex: 1; }
"""

BODY = """
<div class="page">
<h1>멀티모달 RAG 기반 지능형 비디오 검색 시스템</h1>
<p class="lead"><b>다 찾아드림</b> · 진행 상황 요약 · 2026-10-26<br>
<small>상세 근거와 측정 원자료는 <code>docs/PROGRESS.md</code>(963줄)에 있습니다.</small></p>

<h2>1. 한 줄 요약</h2>
<div class="key">
유튜브 쇼츠를 저장해 <b>4초 장면 카드</b>로 분석해 두고, 자연어로 물으면 <b>그 초로
이동</b>시키는 시스템입니다. 사용자 평가에서 장면 찾기에 걸리는 시간이
<b>27.3초 → 9.3초(66% 단축)</b>로 줄었고, 참가자 <b>10명 전원</b>에서 같은 방향의
개선이 관찰되었습니다(부호검정 p=0.002).
</div>

<h2>2. 정량 성과</h2>
<table>
<tr><th style="width:44mm">항목</th><th>결과</th></tr>
<tr class="hl"><td><b>사용자 과제 소요 시간</b></td>
<td><b>27.3초 → 9.3초 (66% 단축)</b> · 참가자 10/10명 개선 · p=0.002</td></tr>
<tr><td>검색 성능 (nDCG@5)</td>
<td><b>0.745</b> (음성 단일 신호 기준선 0.719, <b>+0.027</b>)</td></tr>
<tr><td>과제 성공률</td>
<td>직접 탐색 93% / 제안 시스템 83% — <b>양쪽 모두 천장에 근접해 변별력 없음</b></td></tr>
<tr><td>처리 비용</td>
<td>영상 1건당 캡처 47초 + 서버 112초 · 외부 API 호출 <b>1회</b>(4회에서 감축)</td></tr>
<tr><td>평가 규모</td>
<td>영상 18개 · 장면 카드 203개 · 질문 64개 · 정답 라벨 113줄 · 사용자 10명</td></tr>
<tr><td>수정한 결함</td>
<td>16건 — <b>대부분 오류 없이 조용히 틀린 값을 내던 것</b></td></tr>
</table>

<h2>3. 시스템 구성</h2>
<p><b>서버는 유튜브에 한 번도 접속하지 않습니다.</b> 사용자가 자기 브라우저에서 보고
있는 화면을 확장 프로그램이 캡처해 올립니다.</p>
<table>
<tr><th class="c" style="width:34mm">단계</th><th style="width:48mm">쓰는 모델</th><th>비고</th></tr>
<tr><td>음성 받아적기</td><td>faster-whisper <small>(로컬)</small></td><td>WhisperX 는 Python 3.14 미지원</td></tr>
<tr><td>화면 자막</td><td>EasyOCR <small>(로컬)</small></td><td>장면의 94% 를 채움</td></tr>
<tr class="hl"><td>장면 설명</td><td><b>Gemini</b> <small>(외부)</small></td><td><b>유일한 외부 호출.</b> 영상당 1회</td></tr>
<tr><td>글 좌표</td><td>BGE-M3 1024차원 <small>(로컬)</small></td><td></td></tr>
<tr><td>그림 좌표</td><td>SigLIP2-naflex <small>(로컬)</small></td><td>질의를 영어로 번역해 사용</td></tr>
</table>
<p><small>검색은 <b>글 좌표 · 정확한 단어(IDF) · 그림 좌표</b> 세 신호를 질의 유형별
가중치로 합산합니다.</small></p>

<h2>4. 평가 체계</h2>
<table>
<tr><td style="width:44mm"><b>질문</b></td><td>64개 — 팀원 4명이 각자 <b>팀 외부 지인</b>에게 받았습니다(편향 방지)</td></tr>
<tr><td><b>정답 라벨</b></td><td>113줄 — 전부 팀원이 직접 작성. 정확 2 / 관련 1 / 무관 0</td></tr>
<tr><td><b>정답 단위</b></td><td><b>시간 구간(초)</b> — 세그먼트 번호가 아닙니다. 분할 방식을 바꿔도 평가셋을 다시 만들 필요가 없습니다</td></tr>
<tr><td><b>허용 오차</b></td><td><b>비대칭</b> — 정답 시작의 이전 5초 ~ 이후 2초.
사용자는 목표보다 앞서 시작하는 것은 감수해도 지나쳐 시작하는 것은 감수하지 못합니다</td></tr>
<tr><td><b>주지표</b></td><td>nDCG@5. 질문 유형(재료·분량 / 순서·타이밍 / 시각적 상태)별로 분리 보고</td></tr>
</table>
</div>

<div class="page">
<h2>5. 멘토 자문 대응</h2>
<p><b>1차(9/18) 보완 5건과 2차(10/12) 요구가 전부 처리됐습니다.</b>
전부 "좋아졌다" 는 아닙니다 — <b>해보고 채택하지 않은 것이 둘</b> 있고, 그 과정이 결과입니다.</p>

<h3>자문 1 · 인접 세그먼트 의미 단위 병합 → <b>채택하지 않음</b></h3>
<table>
<tr><th class="c" style="width:30mm">조건</th><th class="r" style="width:22mm">nDCG@5</th><th>해석</th></tr>
<tr><td class="c"><b>고정 분할(현행)</b></td><td class="r"><b>0.745</b></td><td>기준</td></tr>
<tr><td class="c">병합 0.50</td><td class="r">0.685</td><td rowspan="2">임계값 5개에서 <b>단조롭게 나빠짐</b></td></tr>
<tr><td class="c">병합 0.70</td><td class="r">0.741</td></tr>
</table>
<div class="note">
<b>멘토의 진단은 맞았습니다.</b> 병합하면 정답을 품은 카드를 <b>9%p 더 많이</b>
찾아옵니다(69% → 78%). 그러나 카드가 길어져 <b>이동 지점이 뭉툭해지고</b>
(시점 오차 2.8초 → 3.5초), 카드 수가 줄어 top-5 가 짚는 서로 다른 정답이 줄어듭니다.<br>
구제책(병합 카드로 검색 + 원래 4초 조각으로 이동)도 구현해 측정했고,
<b>정답 위치를 보고 조각을 고르는 치트(오라클)로 천장을 재도 기준선을 못 넘었습니다.</b>
→ 고정 분할을 유지합니다. 코드는 남겨 두고 기본값만 껐습니다.
</div>

<h3>자문 2 · 신호별 역할 규명 <small>(핵심 기여)</small></h3>
<p>신호 4개(음성·자막·설명·그림)의 조합 <b>2⁴−1 = 15가지를 전수</b>로 측정했습니다.</p>
<table>
<tr><th class="c" style="width:38mm">조건</th><th class="r" style="width:20mm">nDCG@5</th><th class="r" style="width:20mm">기준대비</th><th>답하는 질문</th></tr>
<tr><td class="c">전부</td><td class="r">0.745</td><td class="r">—</td><td>기준</td></tr>
<tr class="hl"><td class="c"><b>설명 제외</b></td><td class="r"><b>0.772</b></td><td class="r"><b>+0.026</b></td><td><b>Gemini 없이 로컬만으로 되나</b></td></tr>
<tr><td class="c">자막 제외</td><td class="r">0.765</td><td class="r">+0.020</td><td>EasyOCR 이 필요한가</td></tr>
<tr><td class="c">음성 제외</td><td class="r">0.697</td><td class="r">−0.049</td><td><b>음성이 가장 중요</b></td></tr>
<tr><td class="c">그림만</td><td class="r">0.470</td><td class="r">−0.275</td><td>무작위(0.480)와 구별 안 됨</td></tr>
</table>
<div class="key">
<b>가장 중요한 발견 — 신호의 역할이 질문 유형에 따라 정반대입니다.</b><br>
<b>재료·분량</b> 질문은 자막 위주가 유의하게 낫고(+0.084, 95% [+0.037, +0.139]),
<b>시각적 상태</b>는 정반대입니다. <b>전체 평균에서 둘이 상쇄돼 0 에 가까워집니다.</b>
평균만 봤다면 "신호를 섞어도 별 차이 없다" 는 잘못된 결론에 갔을 것입니다.
</div>
<div class="note">
<b>SigLIP 단독은 무작위와 구별되지 않습니다</b>(0.470 vs 무작위 0.480).
같은 영상의 카드끼리는 그림이 서로 비슷해 변별이 안 됩니다. 다만 다른 신호와 함께일
때는 짝지어 뺄 때마다 4/4 모두 떨어져, 보조 신호로는 일관되게 기여합니다.
</div>
</div>

<div class="page">
<h3>자문 3 · 근거 부족 시 "관련 장면 없음" → <b>해결</b></h3>
<p><b>막고 있던 것</b> — 순위 점수는 min-max 정규화로 <b>후보 중 최고를 항상 1.0</b>
으로 만듭니다. 요리 보관함에 "자동차 엔진 오일 교환" 을 물어도 1위가 만점으로
나옵니다(실측). 그래서 정규화 <b>전</b> 코사인 유사도를 따로 꺼내 판정에 씁니다.</p>
<table>
<tr><th style="width:44mm">질문 종류</th><th class="c" style="width:16mm">n</th>
<th class="r" style="width:30mm">1위 원유사도 평균</th><th class="r">판별력 AUC</th></tr>
<tr><td>정답 있음</td><td class="c">48</td><td class="r">0.585</td><td class="r">—</td></tr>
<tr><td>(나) 맞는 영상인데 장면만 없음</td><td class="c">12</td><td class="r">0.511</td><td class="r">0.849</td></tr>
<tr><td>(가) 분야가 아예 다름</td><td class="c">10</td><td class="r">0.450</td><td class="r"><b>0.967</b></td></tr>
</table>
<table>
<tr><th style="width:44mm">운영점 0.52 결과</th><th class="c">(가)</th><th class="c">(나)</th><th class="c">잘못된 거절</th></tr>
<tr><td>손대기 전</td><td class="c">0/10</td><td class="c">0/12</td><td class="c">0/48</td></tr>
<tr class="hl"><td><b>지금</b></td><td class="c"><b>10/10</b></td><td class="c"><b>7/12</b></td><td class="c"><b>5/48</b></td></tr>
</table>
<div class="note">
<b>완전 거절이 아니라 경고입니다.</b> 결과는 그대로 보여주고 "확실히 맞는 것을 찾지
못했습니다" 를 띄웁니다. 감추면 잘못 걸린 5건을 영영 못 보지만, 경고면 피해가 한 줄로
끝나므로 임계값을 조금 공격적으로 잡을 수 있었습니다.
<b>잘못된 거절 5/48 을 성적과 반드시 같이 적습니다</b> — 빼놓으면 대가가 보이지 않습니다.
</div>

<h3>자문 4 · 처리 시간·비용 실측</h3>
<table>
<tr><th style="width:40mm">항목</th><th class="r" style="width:24mm">평균</th><th>비중</th></tr>
<tr><td>영상 길이</td><td class="r">47.2초</td><td></td></tr>
<tr><td>음성 받아적기</td><td class="r">13.7초</td><td>12%</td></tr>
<tr class="hl"><td><b>화면 자막 + 장면 설명</b></td><td class="r"><b>90.8초</b></td><td><b>81%</b></td></tr>
<tr><td>좌표 만들기</td><td class="r">5.6초</td><td>5%</td></tr>
<tr><td><b>사용자 대기</b> (캡처 + 서버)</td><td class="r"><b>158.9초</b></td><td>캡처가 30%</td></tr>
</table>
<div class="warn">
<b>비용은 돈이 아니라 처리량입니다.</b> 실제 지출 0원(무료 티어)이지만
<b>모델당 하루 20회</b> 제한이 있어 <b>하루 20개 영상</b>이 한계입니다.
개발 중 실제로 이 벽에 부딪혀 영상당 호출을 4회에서 1회로 줄였습니다.
멘토가 지적한 "저장 영상 전체의 프레임에 캡션을 생성하는 구조" 가 정확히 위 <b>81%</b> 입니다.
</div>

<h3>자문 5 · 저작권 범위</h3>
<p>개인 학습·연구 목적의 졸업작품입니다. <b>서버는 유튜브에 접속하지 않고</b> 영상
파일을 내려받지도 않습니다. 저장되는 것은 <b>0.5초 간격 정지 이미지와 거기서 추출한
텍스트</b>이며, 검색 결과는 영상을 재생해 주는 것이 아니라 <b>유튜브의 해당 시점으로
이동</b>시킵니다. 재생은 항상 유튜브에서 일어납니다. 다만 외부 플랫폼 콘텐츠를
가공·보관한다는 점은 같으므로, <b>서비스화한다면 이용약관 검토가 선행되어야 합니다.</b></p>
</div>

<div class="page">
<h2>6. 사용자 평가 <small>(2026-10-26 · 참가자 10명 · 60과제)</small></h2>
<table>
<tr><th style="width:34mm">조건</th><th>하는 일</th></tr>
<tr><td class="c"><b>A</b> 직접 찾기</td><td>유튜브에서 <b>직접 스크롤</b>해 찾기 — 지금 사용자가 하는 행동</td></tr>
<tr><td class="c"><b>B</b> 제안 시스템</td><td>검색창에 <b>자기 말로</b> 물어보기</td></tr>
</table>
<p><small>한 사람이 두 조건을 모두 수행하고, 조건 순서와 과제 묶음 순서를 각각 5명씩
교차해 학습 효과를 상쇄했습니다. 검색어는 <b>참가자가 직접</b> 지었습니다.</small></p>

<table>
<tr><th style="width:44mm"></th><th class="r">A 직접 찾기</th><th class="r">B 제안 시스템</th></tr>
<tr class="hl"><td><b>평균 소요 시간</b></td><td class="r">27.3초</td><td class="r"><b>9.3초</b></td></tr>
<tr><td>중앙값</td><td class="r">27.3초</td><td class="r">8.5초</td></tr>
<tr><td>평균 시도 횟수</td><td class="r">1.56회</td><td class="r">1.10회</td></tr>
<tr><td>과제 성공률</td><td class="r">93%</td><td class="r">83%</td></tr>
</table>
<div class="key">
<b>참가자 10명 전원</b>에서 제안 시스템이 더 빨랐습니다(단축폭 54~80%).
<b>부호검정 p = 0.0020.</b> 유형별 단축은 순서·타이밍 76%, 재료·분량 71%,
시각적 상태 47% 입니다.
</div>
<div class="warn">
<b>성공률로는 두 방식을 가를 수 없습니다.</b> 양쪽 다 천장(93~100%)에 붙었습니다 —
저장된 영상 안에서 찾는 과제라 직접 스크롤해도 1분 안에 찾아집니다.
<b>변별력은 시간에 있습니다.</b><br><br>
<b>과제 선정에 실수가 있었습니다.</b> 실패 7건 중 6건이 "정답 구간 안에 도달했는데
성공 기준에서 벗어난" 경우였습니다. 기준이 <i>정답 시작 −5초 ~ +2초</i> 인데
두 과제의 정답 구간이 6초·11초로 길어 뒤쪽에 도달하면 범위를 벗어납니다.
구간 전체를 인정하면 A 100% / B 97% 입니다. <b>두 수치를 모두 보고합니다.</b>
</div>

<h2>7. 서비스 가능성</h2>
<div class="key">
영상 1건을 저장하는 데 <b>캡처 47초 + 서버 처리 112초</b>가 들고, 무료 티어에서는
<b>하루 20건</b>이 한계다(<b>비용</b>). 그 대가로 사용자는 저장된 영상에서 원하는
장면을 찾는 데 걸리는 시간이 <b>27.3초에서 9.3초로 66% 줄었고</b>(<b>효용</b>),
<b>참가자 10명 전원</b>에서 같은 방향의 개선이 관찰되었다(p=0.002).
</div>

<h2>8. 한계 — 그대로 밝힙니다</h2>
<table>
<tr><td style="width:46mm"><b>표본이 작습니다</b></td>
<td>질문 64개, 사용자 10명. 큰 차이는 잡히지만 <b>작은 차이는 확정할 수 없습니다.</b>
유형별 질문이 13~19개라 유형 간 비교는 경향까지만 말합니다</td></tr>
<tr><td><b>설명 채움률 81%</b></td>
<td><b>100% 는 설계상 불가능</b>합니다. 영상당 설명 지점이 12개로 고정이라 장면이
12개를 넘는 영상 9개는 구조적으로 못 채웁니다(천장 82%)</td></tr>
<tr><td><b>라벨러 일치도 없음</b></td>
<td>영상별로 담당을 나눠 같은 질문을 두 사람이 라벨링한 경우가 0건입니다.
질문을 팀 외부에 의뢰해 편향 방지 조건은 충족하지만 κ 는 낼 수 없습니다</td></tr>
<tr><td><b>사용자 평가 과제 선정</b></td>
<td>위 6장에 적은 정답 구간 길이 문제</td></tr>
</table>

<h2>9. 다음</h2>
<table>
<tr><td class="c" style="width:10mm">1</td><td><b>시각적 상태 질문 보강</b> — <b>장면 설명(Gemini)을 끌지 말지가 여기 달렸습니다.</b>
끄면 파이프라인이 100% 로컬이 되고 하루 20건 한도가 사라집니다. 지금 질문이 13개뿐이라
"설명을 빼면 시각적 상태가 나빠진다" 가 확정되지 않았습니다</td></tr>
<tr><td class="c">2</td><td><b>답 없는 질문 보강</b> — 거절 임계값을 12개로 골랐습니다. 더 받으면 운영점이 정확해집니다</td></tr>
<tr><td class="c">3</td><td><b>사용자 질의문 30개를 평가셋에 반영</b> — 실제 사용자 말투라 지금 평가셋보다 짧고 구어체입니다</td></tr>
<tr><td class="c">4</td><td><b>발견 가능성 개선</b> — 참가자 한 명이 "시간을 클릭하면 바로 이동했으면" 이라고 했는데
<b>이미 있는 기능</b>입니다. 드롭다운 안에 있어 못 찾았습니다</td></tr>
</table>
</div>
"""


def main() -> int:
    p = argparse.ArgumentParser(description="진행 상황 요약 PDF 생성")
    p.add_argument("--outdir", default=str(ROOT / "docs"))
    args = p.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("크롬(또는 엣지)을 찾지 못했습니다. 한글 PDF 생성에 필요합니다.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / "_tmp_summary.html"
    out = outdir / "summary.pdf"
    try:
        tmp.write_text(
            f"<!doctype html><meta charset='utf-8'><style>{CSS}</style><body>{BODY}</body>",
            encoding="utf-8")
        url = "file:///" + str(tmp.resolve()).replace("\\", "/")
        subprocess.run(
            [str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-pdf-header-footer", f"--print-to-pdf={out}", url],
            check=True, capture_output=True, timeout=180,
        )
    finally:
        tmp.unlink(missing_ok=True)

    print(f"만들었습니다: {out}  (4쪽)")
    print("  1쪽 요약·성과·구성·평가체계 / 2~3쪽 자문 대응 5건 / 4쪽 사용자 평가·한계·다음")
    print("\n⚠ 숫자를 고칠 일이 생기면 docs/PROGRESS.md 와 이 파일을 같이 고쳐야 합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
