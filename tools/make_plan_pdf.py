# -*- coding: utf-8 -*-
"""추진 일정표 · 역할 분담표를 PDF 로 만듭니다 (최종 보고 제출용).

    python -m tools.make_plan_pdf

  docs/plan-and-roles.pdf

내용은 `docs/plan-and-roles.md` 와 같습니다. 지난 일정은 **git 커밋 이력에서 뽑은
사실**이고 지어낸 날짜가 없습니다. 11월 이후는 계획이라 표에 "예정" 으로 구분해
두었습니다.

**역할 분담표의 담당자 칸은 일부러 비워 둡니다.** 팀이 직접 채울 자리입니다.

한글 PDF 는 헤드리스 크롬으로 만듭니다(`make_user_study_pdf` 와 같은 이유).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from server.config import ROOT
from tools.make_user_study_pdf import find_chrome

CSS = """
@page { size: A4; margin: 13mm 11mm; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic", "맑은 고딕", sans-serif; font-size: 9.2pt;
       color: #111; line-height: 1.4; margin: 0; }
h1 { font-size: 16pt; margin: 0 0 1mm; }
h2 { font-size: 12pt; margin: 5mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1.5px solid #222; }
h3 { font-size: 10.5pt; margin: 4mm 0 1.5mm; }
.lead { color: #444; margin: 0 0 3mm; font-size: 9pt; }
table { width: 100%; border-collapse: collapse; margin: 1.5mm 0 3mm; }
th, td { border: 0.6px solid #999; padding: 1.3mm 1.8mm; text-align: left;
         vertical-align: top; }
th { background: #eceff4; font-weight: 700; }
td.c, th.c { text-align: center; }
tr.milestone td { background: #fff4e6; font-weight: 700; }
tr.plan td { background: #f7f7f7; color: #444; }
.blank { background: #fffdf5; }
small { color: #555; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
.note { background: #f5f7fa; border-left: 3px solid #555; padding: 2mm 3mm;
        margin: 2mm 0; font-size: 8.8pt; }
"""

# (단계, 기간, 내용, 산출물, 상태)  상태: 완료 / 예정 / 자문
SCHEDULE = [
    ("1", "8월 4주", "<b>시스템 설계 및 전 구간 프로토타입</b><br>저장 → 추출 → 분석 → 검색 → 시점 이동",
     "크롬 확장 + FastAPI 서버, 비교 탭", "완료"),
    ("2", "8월 4주", "<b>부가 기능</b><br>제목·채널 추출 안정화, 재료 구매 링크, 사진 레시피 뷰",
     "확장 UI 3종", "완료"),
    ("3", "9월 1주", "<b>로컬 모델 전환</b><br>faster-whisper · EasyOCR · BGE-M3 · SigLIP2",
     "외부 호출 영상당 4회 → 1회", "완료"),
    ("4", "9월 2주", "<b>SigLIP2 파인튜닝 실험</b><br>LoRA / 선형 정렬 2종 비교",
     "실험 기록, <b>미채택 결론</b>", "완료"),
    ("—", "<b>9/18</b>", "<b>◆ 1차 멘토 자문 (서면)</b>", "자문 의견 5건 + 평가 설계 지시 7건", "자문"),
    ("5", "9월 4주", "<b>평가 체계 설계</b><br>등급형 라벨, 비대칭 허용 오차, 질문 유형 분류",
     "라벨링 시트, 평가 지침", "완료"),
    ("6", "10월 1주", "<b>조용한 실패 버그 수정</b><br>음성 전량 공백, 환각, 자막 전량 실패 등",
     "버그 16건 수정, 단계별 상태 기록", "완료"),
    ("7", "10월 2주", "<b>평가셋 구축 및 측정 도구</b><br>외부 지인 질문 수집, 정답 구간 라벨링",
     "질문 64개 · 라벨 113줄 · 평가 스크립트", "완료"),
    ("—", "<b>10/12</b>", "<b>◆ 2차 멘토 자문 / 중간평가 (87점)</b>", "자문 의견 3건, 보완 요구 4건", "자문"),
    ("8", "10월 2~3주", "<b>자문 대응 ①</b> 신호별 기여도 — 제거법 15조건 전수, 무작위 기준선",
     "기여도 측정 원자료", "완료"),
    ("9", "10월 3주", "<b>자문 대응 ②</b> 의미 단위 병합 구현·측정 (천장 확인 후 <b>미채택</b>)",
     "병합 실험 원자료", "완료"),
    ("10", "10월 3주", "<b>자문 대응 ③</b> \"관련 장면 없음\" 판정, 운영점 0.52 데이터로 선택",
     "임계값 곡선, 경고 UI", "완료"),
    ("11", "10월 3주", "<b>자문 대응 ④⑤</b> 처리 시간·비용 실측, 저작권 범위 명시",
     "비용 리포트, 이용 범위 문단", "완료"),
    ("12", "10월 3주", "<b>사용성 개선</b><br>검색 결과 영상 목록화, 저장을 별도 창으로 분리",
     "저장 중 다른 영상 시청 가능", "완료"),
    ("13", "10월 4주", "<b>공식 기준선 확정</b><br>장면 설명 백필 후 재측정",
     "nDCG@5 0.745 (기준선 0.719)", "완료"),
    ("—", "<b>10/26</b>", "<b>◆ 사용자 평가 (10명 · 60과제)</b>",
     "<b>장면 찾기 27.3초 → 9.3초 (66% 단축)</b>", "자문"),
    ("14", "11월 1~2주", "<b>평가셋 보강 및 재측정</b><br>시각적 상태 질문, 답 없는 질문 추가",
     "확정 성능표", "예정"),
    ("15", "11월 2~3주", "<b>장면 설명(Gemini) 사용 여부 최종 결정</b><br>비용 대비 효과",
     "아키텍처 최종안", "예정"),
    ("16", "11월 3~4주", "<b>사용성 보완</b><br>시점 이동 기능 발견 가능성 개선",
     "UI 개선", "예정"),
    ("17", "12월 1주", "<b>최종 보고서 · 발표 자료 작성</b>", "최종 산출물", "예정"),
]

ROLES = [
    ("A", "<b>시스템 설계 · 서버 백엔드</b><br>파이프라인 전 구간 설계, FastAPI 서버, 작업 큐, "
          "SQLite·Qdrant 연동, 사용자 격리", "서버 전 구간, 비교 실험 러너"),
    ("B", "<b>분석 파이프라인 안정화</b><br>단계별 상태 기록, 실패 등급 구분, 할당량 오류 처리, "
          "로컬 모델 동시 실행 제어, 자동 테스트", "실패를 드러내는 구조, 테스트 66건"),
    ("C", "<b>멀티모달 모델 · 검색 품질</b><br>SigLIP2 파인튜닝 실험, 질의 전처리, "
          "하이브리드 검색 가중치, 조용한 실패 버그 추적", "실험 기록, 검색 로직 개선"),
    ("D", "<b>평가 체계 · 자문 대응</b><br>평가셋 설계, 측정 도구, 신호별 기여도·병합·거절 임계값 "
          "실험, 처리 시간·비용 실측, 사용자 평가 설계", "평가 스크립트 6종, 측정 원자료, 통합 문서"),
    ("E", "<b>크롬 확장 UI/UX</b><br>저장 흐름, 검색 화면, 사진 레시피 뷰, 재료 구매 링크, "
          "제목·채널 추출", "확장 전 구간"),
    ("공통", "<b>평가셋 라벨링</b><br>담당 영상의 정답 구간을 사람이 직접 판정 (영상 4개씩 분담)",
     "라벨 113줄"),
    ("공통", "<b>사용자 평가 진행</b><br>참가자 섭외·진행·기록 (진행자 4명이 10명 담당)",
     "60과제 원자료"),
    ("공통", "<b>질문 수집</b><br>팀 외부 지인에게 의뢰 — 개발팀이 만들면 \"잘 푸는 질문\" 으로 기울어짐",
     "질문 64개"),
]

RESULTS = [
    ("검색 성능", "nDCG@5 <b>0.745</b> (음성 단일 신호 기준선 0.719, <b>+0.027</b>)"),
    ("<b>사용자 과제 소요 시간</b>", "<b>27.3초 → 9.3초 (66% 단축)</b>, 참가자 10명 전원 개선 (p=0.002)"),
    ("과제 성공률", "직접 탐색 93% / 제안 시스템 83% — 양쪽 모두 천장에 근접해 변별력 없음"),
    ("처리 비용", "영상 1건당 캡처 47초 + 서버 112초, 외부 호출 <b>1회</b> (4회에서 감축)"),
    ("평가 규모", "영상 18개 · 장면 카드 203개 · 질문 64개 · 라벨 113줄 · 사용자 10명"),
    ("수정한 결함", "16건 (대부분 오류 없이 조용히 틀린 값을 내던 것)"),
]

CLS = {"완료": "", "예정": "plan", "자문": "milestone"}


def html() -> str:
    sched = "".join(
        f'<tr class="{CLS[st]}"><td class="c">{n}</td><td class="c">{period}</td>'
        f"<td>{what}</td><td>{out}</td><td class='c'>{st if st != '자문' else '—'}</td></tr>"
        for n, period, what, out, st in SCHEDULE
    )
    roles = "".join(
        f'<tr><td class="c"><b>{g}</b></td><td>{job}</td><td>{out}</td>'
        f'<td class="blank"></td></tr>'
        for g, job, out in ROLES
    )
    res = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in RESULTS)
    return f"""<!doctype html><meta charset="utf-8"><style>{CSS}</style><body>

<div class="page">
<h1>추진 일정표</h1>
<p class="lead"><b>다 찾아드림</b> · 멀티모달 RAG 기반 지능형 비디오 검색 시스템</p>
<div class="note">
지난 일정(8~10월)은 <b>git 커밋 이력에서 그대로 뽑은 사실</b>입니다.
11월 이후는 계획이며 <b>최종 평가 일정에 맞춰 조정</b>하세요.
주황색 줄은 자문·평가 일정입니다.
</div>
<table>
<tr><th class="c" style="width:10mm">단계</th><th class="c" style="width:20mm">기간</th>
<th style="width:72mm">추진 내용</th><th>주요 산출물</th>
<th class="c" style="width:14mm">상태</th></tr>
{sched}
</table>

<h3>월별 요약</h3>
<table>
<tr><th class="c" style="width:16mm">월</th><th style="width:60mm">중점</th><th>결과</th></tr>
<tr><td class="c"><b>8월</b></td><td>설계 · 전 구간 구현</td><td>저장부터 시점 이동까지 동작</td></tr>
<tr><td class="c"><b>9월</b></td><td>로컬 모델 전환 · 평가 설계</td><td>외부 호출 1/4 로 감축, 1차 자문 반영 착수</td></tr>
<tr><td class="c"><b>10월</b></td><td>측정 · 자문 대응 · 사용자 평가</td><td><b>자문 요구 전 항목 처리</b>, 효용 수치 확보</td></tr>
<tr><td class="c"><b>11월</b></td><td>보강 측정 · 최종 결정</td><td>확정 성능표, 아키텍처 최종안</td></tr>
<tr><td class="c"><b>12월</b></td><td>마무리</td><td>최종 보고</td></tr>
</table>
</div>

<div class="page">
<h1>역할 분담표</h1>
<p class="lead">담당자 칸은 비워 두었습니다. 실제 수행 사실은 git 커밋 이력으로 확인됩니다.</p>
<table>
<tr><th class="c" style="width:12mm">구분</th><th style="width:78mm">담당 업무</th>
<th style="width:48mm">주요 산출물</th><th class="c" style="width:22mm">담당자</th></tr>
{roles}
</table>

<h3>분담 원칙</h3>
<table>
<tr><td style="width:46mm"><b>경계를 코드에 적었다</b></td>
<td>각 파일 첫 줄에 <code>"단계 N · 무엇. 담당 X"</code> 형식으로 담당을 명시</td></tr>
<tr><td><b>같은 인터페이스를 유지했다</b></td>
<td>모델 단계마다 Gemini 대체품과 로컬 백엔드를 같은 모양으로 두어,
담당자가 서로의 코드를 몰라도 교체 가능</td></tr>
<tr><td><b>평가는 한 사람이 모았다</b></td>
<td>측정 기준이 사람마다 다르면 비교가 깨지므로, 평가 설계·도구·보고는 한 명이 전담</td></tr>
<tr><td><b>라벨링과 질문은 나눴다</b></td>
<td>편향 방지. 질문은 팀 외부에 의뢰, 라벨링은 담당 영상별로 분담</td></tr>
</table>

<h2>정량 성과 요약</h2>
<table>
<tr><th style="width:48mm">항목</th><th>결과</th></tr>
{res}
</table>
<p><small>상세 근거와 측정 원자료는 <code>docs/PROGRESS.md</code> 에 있습니다.</small></p>
</div>
</body>"""


def main() -> int:
    p = argparse.ArgumentParser(description="추진 일정표·역할 분담표 PDF 생성")
    p.add_argument("--outdir", default=str(ROOT / "docs"))
    args = p.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("크롬(또는 엣지)을 찾지 못했습니다. 한글 PDF 생성에 필요합니다.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tmp = outdir / "_tmp_plan.html"
    out = outdir / "plan-and-roles.pdf"
    try:
        tmp.write_text(html(), encoding="utf-8")
        url = "file:///" + str(tmp.resolve()).replace("\\", "/")
        subprocess.run(
            [str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--no-pdf-header-footer", f"--print-to-pdf={out}", url],
            check=True, capture_output=True, timeout=180,
        )
    finally:
        tmp.unlink(missing_ok=True)

    print(f"만들었습니다: {out}  (2쪽)")
    print("  1쪽 추진 일정표 · 2쪽 역할 분담표 + 정량 성과")
    print("  역할 분담표의 담당자 칸은 비어 있습니다 — 팀이 채우세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
