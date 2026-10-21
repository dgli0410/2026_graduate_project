# -*- coding: utf-8 -*-
"""사용자 평가 **인쇄물** 두 개를 PDF 로 만듭니다 (자문 7번).

    python -m tools.make_user_study_pdf

  docs/user-study-guide.pdf   진행 안내서 — 진행자가 보면서 진행 (2쪽)
  docs/user-study-forms.pdf   평가지 — 참가자 1인당 1장, 10장

설계 전문은 `docs/user-study.md` 입니다. 이 도구는 그것을 **손에 들고 쓸 수 있는
형태**로 뽑을 뿐이고, 과제·배정표는 `tools/make_user_study_sheet.py` 와 **같은
정의를 가져다 씁니다.** 한쪽만 고치면 어긋나기 때문입니다.

**평가지에는 정답을 적지 않습니다.** 진행자가 참가자 앞에서 들고 있는 종이라,
정답 시각이 보이면 참가자가 흘끗 볼 수 있습니다. 도달 시점만 적고, 성공 판정은
`eval/user_study_sheet.xlsx` 가 수식으로 합니다.

한글 PDF 는 **헤드리스 크롬**으로 만듭니다. reportlab 은 CJK 글꼴을 따로 등록해야
하고 Windows 글꼴 경로가 환경마다 달라, 시스템 글꼴을 그대로 쓰는 크롬이 안전합니다.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from server.config import DB_PATH, ROOT
from tools.make_user_study_sheet import LAG, LEAD, LIMIT_SEC, PLAN, TASKS

CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic", "맑은 고딕", sans-serif; font-size: 10pt;
       color: #111; line-height: 1.45; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 2mm; }
h2 { font-size: 12pt; margin: 6mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1.5px solid #222; }
.lead { color: #444; margin: 0 0 4mm; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 3mm; }
th, td { border: 0.7px solid #999; padding: 1.6mm 2mm; text-align: left;
         vertical-align: top; }
th { background: #eceff4; font-weight: 700; }
td.c, th.c { text-align: center; }
.two { display: flex; gap: 4mm; }
.box { flex: 1; border: 1px solid #999; border-radius: 2mm; padding: 3mm; }
.box h3 { margin: 0 0 1.5mm; font-size: 11pt; }
.tag { display: inline-block; background: #222; color: #fff; border-radius: 1mm;
       padding: 0.3mm 2mm; font-size: 9pt; margin-right: 1.5mm; }
.say { background: #f5f7fa; border-left: 3px solid #555; padding: 2mm 3mm;
       margin: 1.5mm 0; }
.no { background: #fdf0ee; border-left: 3px solid #c0392b; padding: 2.5mm 3mm; }
.no b { color: #c0392b; }
small { color: #555; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
/* 평가지 */
.fhead { display: flex; justify-content: space-between; align-items: flex-end;
         border-bottom: 2px solid #222; padding-bottom: 2mm; margin-bottom: 3mm; }
.pid { font-size: 22pt; font-weight: 800; letter-spacing: 1px; }
.fill { display: inline-block; border-bottom: 0.8px solid #666; min-width: 26mm; }
.assign { display: flex; gap: 3mm; margin-bottom: 3mm; }
.assign div { flex: 1; border: 1px solid #999; padding: 2mm 3mm; border-radius: 1.5mm; }
.ftable td { height: 14mm; }
.goal { font-size: 9.5pt; }
.vid { font-size: 9pt; }
.foot { margin-top: 3mm; border: 1px solid #999; padding: 3mm; border-radius: 1.5mm; }
"""


def find_chrome() -> Path | None:
    for p in CHROME_CANDIDATES:
        if p.is_file():
            return p
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def video_titles() -> dict[str, str]:
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        out = dict(conn.execute("SELECT video_id, title FROM videos"))
        conn.close()
        return out
    except Exception:  # noqa: BLE001 - DB 가 없어도 양식은 만들 수 있어야 합니다
        return {}


def short(title: str, n: int = 26) -> str:
    title = (title or "").split("#")[0].strip()
    return title if len(title) <= n else title[: n - 1] + "…"


def by_set() -> dict[int, list]:
    out: dict[int, list] = {1: [], 2: []}
    for t in TASKS:
        out[t[0]].append(t)
    return out


def cond_label(c: str) -> str:
    return "A 직접 찾기" if c == "A" else "B 우리 시스템"


# --- 1) 진행 안내서 ---------------------------------------------------------

def guide_html() -> str:
    titles = video_titles()
    rows = "".join(
        f"<tr><td class='c'><b>{tid}</b></td><td class='c'>{st}</td>"
        f"<td class='c'>{kind}</td>"
        f"<td><b>{short(titles.get(vid, vid), 22)}</b></td><td>{goal}</td></tr>"
        for st, tid, vid, kind, goal, _s, _e in TASKS
    )
    prep = "".join(
        f"<tr><td class='c'><b>{tid}</b></td><td class='c'>세트{st}</td>"
        f"<td>{short(titles.get(vid, vid), 30)}</td>"
        f"<td><code>youtube.com/shorts/{vid}</code></td></tr>"
        for st, tid, vid, _k, _g, _s, _e in TASKS
    )
    plan = "".join(
        f"<tr><td class='c'><b>{pid}</b></td>"
        f"<td class='c'>세트{s1} — {cond_label(c1)}</td>"
        f"<td class='c'>세트{s2} — {cond_label(c2)}</td></tr>"
        for pid, s1, c1, s2, c2 in PLAN
    )
    return f"""<!doctype html><meta charset="utf-8"><style>{CSS}</style><body>

<div class="page">
<h1>사용자 평가 진행 안내</h1>
<p class="lead">멀티모달 RAG 비디오 검색 · <b>과제 성공률</b>과 <b>소요 시간</b>을 잽니다.
만족도 설문은 하지 않습니다. 참가자 10명 · 1인당 20~25분.</p>

<h2>무엇과 비교하는가</h2>
<div class="two">
  <div class="box"><h3><span class="tag">A</span>직접 찾기 <small>(지금 방식)</small></h3>
    유튜브에서 영상을 열고 <b>직접 스크롤해</b> 장면을 찾습니다.</div>
  <div class="box"><h3><span class="tag">B</span>우리 시스템</h3>
    검색창에 <b>자기 말로 물어보고</b> 결과를 눌러 이동합니다.</div>
</div>
<p><small>한 사람이 두 조건을 다 합니다. 순서는 참가자마다 교차합니다(아래 배정표).
같은 과제를 두 조건으로 시키지 않습니다 — 두 번째는 답을 이미 알기 때문입니다.</small></p>

<h2>과제 6개</h2>
<table>
<tr><th class="c" style="width:11mm">과제</th><th class="c" style="width:11mm">세트</th>
<th class="c" style="width:21mm">유형</th><th style="width:40mm">틀어줄 영상</th>
<th>참가자에게 읽어줄 목표</th></tr>
{rows}
</table>
<p><small>영상 6개가 전부 다릅니다. 유형 3종이 세트마다 하나씩 들어 있습니다.</small></p>

<h2>배정표 — 그대로 따르세요</h2>
<table>
<tr><th class="c" style="width:18mm">참가자</th><th class="c">전반 3과제</th>
<th class="c">후반 3과제</th></tr>
{plan}
</table>
</div>

<div class="page">
<h2>측정 항목</h2>
<table>
<tr><th style="width:30mm">항목</th><th>어떻게</th></tr>
<tr><td><b>성공 여부</b> ★</td>
    <td>도달 시점만 적으면 됩니다. 판정은 엑셀이 합니다
        (정답 시작 기준 <b>이전 {LEAD:.0f}초 ~ 이후 {LAG:.0f}초</b>)</td></tr>
<tr><td><b>소요 시간</b> ★</td><td>"시작하겠습니다" → 참가자가 "찾았어요" 할 때까지 초</td></tr>
<tr><td>시도 횟수</td><td>검색어를 몇 번 바꿔 쳤나 (A 조건은 탐색을 몇 번 다시 했나)</td></tr>
<tr><td>포기</td><td><b>{LIMIT_SEC}초({LIMIT_SEC // 60}분)</b>가 되면 중단하고 "포기 = 예"</td></tr>
<tr><td>첫 질의문</td><td>B 조건에서 참가자가 <b>처음 친 말</b>을 그대로 적습니다. A 조건은 공란</td></tr>
<tr><td>메모</td><td>막힌 지점 한 줄</td></tr>
</table>

<h2>진행자가 읽을 말 — 매번 똑같이</h2>
<div class="say"><b>[시작]</b> "요리 쇼츠에서 특정 장면을 찾는 과제를 6개 드립니다.
앞 3개와 뒤 3개는 찾는 방법이 다릅니다. 시간을 재지만 <b>빨리 하는 게 목표가 아닙니다.</b>
평소 하시던 대로 하세요. 한 과제에 {LIMIT_SEC // 60}분이 넘으면 제가 중단시킵니다.
그건 참가자분 잘못이 아니라 <b>저희 시스템을 평가하는 것</b>입니다.
중간에 그만두고 싶으시면 언제든 말씀하세요."</div>
<div class="say"><b>[A 조건]</b> "이 영상입니다. <b>유튜브에서 직접</b> 원하는 장면을 찾아
그 시점으로 이동해 주세요. 영상을 끌거나 돌려보셔도 됩니다."</div>
<div class="say"><b>[B 조건]</b> "이번에는 이 검색창을 쓰시면 됩니다.
<b>찾고 싶은 것을 평소 말하듯 입력</b>하시면 됩니다. 결과를 누르면 그 시점으로 넘어갑니다.
어떻게 입력해야 하는지는 <b>알려드리지 않겠습니다.</b> 생각나는 대로 쓰세요."</div>
<div class="say"><b>[끝나고]</b> "쓰시면서 불편했던 점 있으면 한 가지만 말씀해 주세요."</div>

<h2>절대 하지 말 것</h2>
<div class="no">
<b>· 검색어 힌트 주기</b> ("'간장'이라고 쳐보세요") &nbsp;
<b>· 성공/실패 알려주기</b> &nbsp; <b>· "거의 다 왔어요" 같은 반응</b> &nbsp;
<b>· 헤맬 때 거들기</b><br>
<small>진행자가 더 잘 설명해주면 그 참가자만 빨라져 비교가 깨집니다.</small>
</div>

<h2>영상 준비 — 진행 전에 이것부터</h2>
<p><small><b>영상은 과제가 정합니다. 조건(A/B)과 무관합니다.</b>
모든 참가자가 영상 6개를 전부 봅니다 — 어떤 방법으로 찾느냐만 다릅니다.</small></p>
<table>
<tr><th class="c" style="width:11mm">과제</th><th class="c" style="width:14mm">세트</th>
<th style="width:52mm">영상</th><th>주소</th></tr>
{prep}
</table>
<p><small><b>A 조건</b>: 그 영상의 유튜브 쇼츠 페이지를 열어 줍니다.
<b>B 조건</b>: 보관함에 저장돼 있어야 합니다(이미 저장됨). 사이드패널 검색창을 씁니다.</small></p>

<h2>준비물 점검</h2>
<table>
<tr><td class="c" style="width:8mm">□</td><td>서버 켜짐 (<code>uvicorn server.main:app --port 8000</code>)</td></tr>
<tr><td class="c">□</td><td>과제 영상 6개가 보관함에 저장돼 있음</td></tr>
<tr><td class="c">□</td><td>검색 한 번 해봐서 잘 도는지 확인</td></tr>
<tr><td class="c">□</td><td>스톱워치 / 평가지 / 펜</td></tr>
<tr><td class="c">□</td><td><b>이전 참가자의 검색 기록이 화면에 안 남아 있음</b></td></tr>
</table>

<h2>끝난 뒤</h2>
<p>평가지의 숫자를 <b>eval/user_study_sheet.xlsx</b> [기록] 탭에 옮기면
[집계] 탭이 조건별 성공률·평균 시간을 자동으로 냅니다.</p>
<p><small><b>해석 주의</b> — n=10 입니다. "73% 대 90% 였다" 까지만 쓰고
"유의하게 높다" 는 쓰지 마세요. 시간 평균은 <b>성공한 과제만</b> 내고 실패 건수는
따로 밝힙니다.</small></p>
</div>
</body>"""


# --- 2) 평가지 --------------------------------------------------------------

def form_page(pid: str, s1: int, c1: str, s2: int, c2: str,
              sets: dict[int, list], titles: dict[str, str]) -> str:
    rows = []
    order = 1
    for st, cond in ((s1, c1), (s2, c2)):
        for (_s, tid, _vid, kind, goal, _a, _b) in sets[st]:
            rows.append(
                f"<tr>"
                f"<td class='c'><b>{order}</b><br><small>{tid}</small></td>"
                f"<td class='c'><b>{'A' if cond == 'A' else 'B'}</b><br>"
                f"<small>{'직접' if cond == 'A' else '시스템'}</small></td>"
                f"<td class='vid'><b>{short(titles.get(_vid, _vid))}</b></td>"
                f"<td class='goal'>{goal}<br><small>({kind})</small></td>"
                f"<td></td><td></td><td></td><td></td><td></td>"
                f"</tr>"
            )
            order += 1
    return f"""<div class="page">
<div class="fhead">
  <div><div class="pid">{pid}</div><small>사용자 평가 기록지</small></div>
  <div style="text-align:right">
    날짜 <span class="fill"></span> &nbsp; 진행자 <span class="fill"></span>
  </div>
</div>

<div class="assign">
  <div><b>전반 1~3</b> &nbsp; 세트{s1} · <b>{cond_label(c1)}</b></div>
  <div><b>후반 4~6</b> &nbsp; 세트{s2} · <b>{cond_label(c2)}</b></div>
</div>

<table class="ftable">
<tr>
  <th class="c" style="width:11mm">순서</th>
  <th class="c" style="width:12mm">조건</th>
  <th style="width:34mm">틀어줄 영상</th>
  <th style="width:44mm">읽어줄 목표</th>
  <th style="width:28mm">첫 질의문<br><small>(B조건만)</small></th>
  <th class="c" style="width:17mm">도달<br>시점(초)</th>
  <th class="c" style="width:17mm">소요<br>시간(초)</th>
  <th class="c" style="width:12mm">시도<br>횟수</th>
  <th class="c" style="width:17mm">포기<br><small>예 / 아니오</small></th>
</tr>
{''.join(rows)}
</table>

<div class="foot">
  <b>막힌 지점 / 관찰 메모</b><br><br><br><br>
  <b>끝나고 한 줄</b> — "쓰시면서 불편했던 점 한 가지만"<br><br><br>
</div>
<p><small>· {LIMIT_SEC}초가 넘으면 중단하고 포기=예, 소요시간은 {LIMIT_SEC} 로 적습니다.
· <b>성공/실패는 참가자에게 알려주지 않습니다.</b> 판정은 나중에 엑셀이 합니다.</small></p>
</div>"""


def forms_html() -> str:
    sets = by_set()
    titles = video_titles()
    pages = "".join(form_page(pid, s1, c1, s2, c2, sets, titles)
                    for pid, s1, c1, s2, c2 in PLAN)
    return f"""<!doctype html><meta charset="utf-8"><style>{CSS}</style><body>{pages}</body>"""


def to_pdf(chrome: Path, html: str, out: Path, tmp: Path) -> None:
    tmp.write_text(html, encoding="utf-8")
    url = "file:///" + str(tmp.resolve()).replace("\\", "/")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--no-pdf-header-footer", f"--print-to-pdf={out}", url],
        check=True, capture_output=True, timeout=180,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="사용자 평가 인쇄물 PDF 생성")
    p.add_argument("--outdir", default=str(ROOT / "docs"))
    args = p.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("크롬(또는 엣지)을 찾지 못했습니다. 한글 PDF 생성에 필요합니다.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    tmp = outdir / "_tmp_user_study.html"
    try:
        to_pdf(chrome, guide_html(), outdir / "user-study-guide.pdf", tmp)
        print(f"만들었습니다: {outdir / 'user-study-guide.pdf'}  (진행 안내서 2쪽)")
        to_pdf(chrome, forms_html(), outdir / "user-study-forms.pdf", tmp)
        print(f"만들었습니다: {outdir / 'user-study-forms.pdf'}  (평가지 {len(PLAN)}장)")
    finally:
        tmp.unlink(missing_ok=True)

    print("\n인쇄해서 쓰세요. 숫자는 나중에 eval/user_study_sheet.xlsx 에 옮기면")
    print("[집계] 탭이 조건별 성공률·평균 시간을 자동으로 냅니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
