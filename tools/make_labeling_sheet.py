# -*- coding: utf-8 -*-
"""평가셋 정답 라벨링 시트를 만듭니다 (단계 12 평가 · 멘토 자문 반영).

    python -m tools.make_labeling_sheet

영상을 더 저장한 뒤 다시 돌리면 [영상목록] 탭이 최신 DB 내용으로 갱신됩니다.
이미 채운 라벨을 덮어쓰지 않도록, 기존 파일이 있으면 --force 가 필요합니다.

이 도구에만 openpyxl 이 필요합니다:  pip install openpyxl
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from server.config import DB_PATH, ROOT

parser = argparse.ArgumentParser(description="평가셋 라벨링 시트 생성")
parser.add_argument("--out", default=str(ROOT / "eval" / "labeling_sheet.xlsx"))
parser.add_argument("--force", action="store_true", help="기존 파일을 덮어씁니다")
args = parser.parse_args()

OUT = Path(args.out)
if OUT.exists() and not args.force:
    sys.exit(
        f"이미 있습니다: {OUT}\n"
        "덮어쓰면 채워둔 라벨이 사라집니다. 정말 새로 만들려면 --force 를 붙이세요."
    )
OUT.parent.mkdir(parents=True, exist_ok=True)

FONT = "맑은 고딕"
NAVY = "1F3864"
HDR_FILL = PatternFill("solid", fgColor="1F3864")
SEC_FILL = PatternFill("solid", fgColor="D9E2F3")
EX_FILL = PatternFill("solid", fgColor="FFF2CC")
WARN_FILL = PatternFill("solid", fgColor="FCE4E4")

thin = Side(style="thin", color="BFBFBF")
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)

LAST = 2000


def base(cell, *, bold=False, size=10, color="000000", wrap=False,
         halign="left", valign="center", fill=None, border=False):
    cell.font = Font(name=FONT, bold=bold, size=size, color=color)
    cell.alignment = Alignment(horizontal=halign, vertical=valign, wrap_text=wrap)
    if fill:
        cell.fill = fill
    if border:
        cell.border = BOX


def header_row(ws, row, labels, start_col=1, widths=None):
    for i, label in enumerate(labels):
        c = ws.cell(row=row, column=start_col + i, value=label)
        base(c, bold=True, color="FFFFFF", fill=HDR_FILL, halign="center", border=True)
    if widths:
        for i, w in enumerate(widths):
            ws.column_dimensions[get_column_letter(start_col + i)].width = w


def title_block(ws, title, subtitle, span):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)
    base(ws.cell(row=1, column=1, value=title), bold=True, size=15, color=NAVY)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
    base(ws.cell(row=2, column=1, value=subtitle), size=9, color="595959")
    ws.row_dimensions[1].height = 24


# =====================================================================
# 시트 1 · 작성법   (A=여백 / B=라벨 / C+D=본문)
# =====================================================================
wb = Workbook()
ws = wb.active
ws.title = "작성법"
ws.sheet_view.showGridLines = False
for col, w in (("A", 3), ("B", 20), ("C", 42), ("D", 50)):
    ws.column_dimensions[col].width = w

title_block(ws, "정답 라벨링 작성법",
            "이 문서만 읽고 바로 시작할 수 있게 썼습니다. 읽는 데 5분, 영상 1개 라벨링에 5분 정도 걸립니다.", 4)

rows = [
    ("SECTION", "이 작업이 무엇인가"),
    ("TEXT", "", '"이 질문의 답은 이 영상 몇 초에서 몇 초 사이에 나온다" 를 사람이 직접 보고 적는 작업입니다.'),
    ("TEXT", "", "우리 검색 시스템이 무엇을 찾았는지는 절대 보지 않습니다. 시스템과 무관하게 정답을 먼저 확정해두는 것이 목적입니다. 그래야 나중에 시스템을 고쳐도 같은 잣대로 성능을 비교할 수 있습니다."),
    ("BLANK",),
    ("SECTION", "시작하기 전에"),
    ("STEP", "1", "[영상목록] 탭에서 본인이 맡은 영상을 확인합니다."),
    ("STEP", "2", "그 영상의 '시청 URL' 을 클릭해 엽니다. shorts 주소가 아니라 watch 주소입니다 — 일반 플레이어로 열려야 타임라인에 마우스를 올렸을 때 초가 보입니다."),
    ("STEP", "3", "단축키: 쉼표( , ) 와 마침표( . ) = 한 프레임씩 이동, j / l = 10초씩 이동, k = 일시정지. 정확한 초를 잡을 때 씁니다."),
    ("STEP", "4", "[라벨링] 탭 2행의 노란색 예시를 확인하고, 이해했으면 그 줄을 삭제한 뒤 3행부터 채웁니다."),
    ("BLANK",),
    ("SECTION", "라벨링 절차 (질문 1개 기준)"),
    ("STEP", "1", '질문을 읽습니다.  예: "간장 몇 큰술 넣어?"'),
    ("STEP", "2", "영상을 보면서 '답이 실제로 나오는 순간' 을 찾습니다. 간장을 계량해서 붓는 장면이 22초에 시작해 26초에 끝났다면 → start 22.0 / end 26.0 / grade 2"),
    ("STEP", "3", "답은 아니지만 도움이 되는 구간이 있으면 줄을 하나 더 추가해 grade 1 로 적습니다. 예: 직전에 간장병을 꺼내는 18~22초 구간."),
    ("STEP", "4", "나머지 구간은 적지 않습니다. 적지 않은 구간은 자동으로 0점(무관) 처리됩니다."),
    ("STEP", "5", "답이 영상에 아예 없으면 start/end 를 비우고 grade 에 '없음' 을 적습니다. (아래 '일부러 답 없는 질문' 항목 참고)"),
    ("BLANK",),
    ("SECTION", "등급 기준 (grade)"),
    ("GRADE", "2 · 정확", '이 구간만 봐도 질문의 답을 알 수 있다. "이거 하나만 보여줘도 사용자가 만족한다" 수준일 때만 2점을 줍니다.'),
    ("GRADE", "1 · 관련", "답 자체는 아니지만 맥락상 도움이 된다. 준비 동작, 재료 소개, 직후 결과 등."),
    ("GRADE", "0 · 무관", "나머지 전부. 시트에 적지 않으면 자동으로 0점입니다. 따로 입력하지 마세요."),
    ("GRADE", "없음", "그 질문의 답이 영상에 존재하지 않는다."),
    ("BLANK",),
    ("SECTION", "판단이 갈릴 때 쓰는 규칙 4가지"),
    ("RULE", "1", "구간 경계 — 답이 보이거나 들리기 시작하는 순간부터, 끝나는 순간까지. 최소 2초는 잡습니다."),
    ("RULE", "2", "애매하면 2 대신 1 을 줍니다. 2점은 확실할 때만."),
    ("RULE", "3", "정답 구간이 여러 개면 전부 적습니다. 영상에서 간장을 두 번 넣으면 줄이 두 개가 됩니다."),
    ("RULE", "4", "시간은 소수점 첫째 자리까지 적습니다. (예: 22.0, 26.5)"),
    ("BLANK",),
    ("SECTION", "일부러 '답 없는 질문' 을 섞으세요   ★ 중요"),
    ("TEXT", "", "전체 질문의 10~15% (45개 기준 5~8개) 는 답이 영상에 없는 질문으로 만듭니다. 예: 설탕이 전혀 안 나오는 영상에 \"설탕 얼마나 넣어?\" 라는 질문을 붙입니다."),
    ("TEXT", "", "이유 — 멘토 자문 3번이 \"근거가 불충분하면 답변을 생성하지 말고 '관련 장면 없음' 을 안내하라\" 인데, 모든 질문에 답이 있으면 그 기능을 시험할 방법이 아예 없습니다. 이 질문들이 임계값 도입의 근거가 됩니다."),
    ("BLANK",),
    ("SECTION", "2명 독립 라벨링 방법"),
    ("STEP", "1", "이 파일을 2부 복사해 각자 하나씩 가져갑니다. (파일명 뒤에 본인 이름을 붙이세요)"),
    ("STEP", "2", "같은 질문 목록을 서로 상의하지 않고 각자 채웁니다. 이것이 '독립' 의 전부입니다."),
    ("STEP", "3", "다 하면 두 파일을 그대로 제출합니다. labeler 컬럼으로 구분되므로 합치는 것은 신경 쓰지 않아도 됩니다."),
    ("STEP", "4", "일치도 계산과 불일치 항목 추출은 자동으로 처리됩니다. 불일치 목록만 받아서 합의하면 됩니다."),
    ("BLANK",),
    ("SECTION", "자주 하는 실수 3가지"),
    ("BAD", "X", "우리 시스템의 검색 결과를 보고 라벨링한다 → 평가가 무의미해집니다. 영상만 보세요."),
    ("BAD", "X", "세그먼트 번호로 적는다 → 반드시 시간(초)으로 적습니다. 나중에 세그먼트 경계가 바뀌어도 평가셋을 다시 안 만들어도 되기 때문입니다."),
    ("BAD", "X", "두 사람이 같이 앉아서 한다 → 독립성이 깨져 일치도 숫자가 의미를 잃습니다."),
    ("BLANK",),
    ("SECTION", "컬럼 설명 — [라벨링] 탭"),
]

r = 4
for item in rows:
    kind = item[0]
    if kind == "BLANK":
        r += 1
        continue
    if kind == "SECTION":
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
        base(ws.cell(row=r, column=2, value=item[1]), bold=True, size=11,
             color=NAVY, fill=SEC_FILL)
        ws.row_dimensions[r].height = 20
        r += 1
        continue
    label, text = item[1], item[2]
    base(ws.cell(row=r, column=2, value=label),
         bold=kind in ("GRADE", "STEP", "RULE", "BAD"),
         color="C00000" if kind == "BAD" else "000000",
         halign="center" if kind in ("STEP", "RULE", "BAD") else "left",
         valign="top")
    ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
    base(ws.cell(row=r, column=3, value=text), wrap=True, valign="top")
    ws.row_dimensions[r].height = 16 if len(text) < 55 else (32 if len(text) < 115 else 46)
    r += 1

# --- 컬럼 설명 표 (B / C / D) ---
col_hdr = r
header_row(ws, col_hdr, ["컬럼", "무엇을 적나", "예시 · 규칙"], start_col=2)
col_defs = [
    ("qid", "질문 번호. q01, q02 … 형식.",
     "같은 질문이 여러 줄이면 qid 를 똑같이 반복합니다. 이것이 줄을 하나의 질문으로 묶는 열쇠입니다."),
    ("video_id", "어느 영상에 대한 질문인지.",
     "드롭다운으로 고르거나 [영상목록] 탭에서 복사합니다.  예: PbCq-C9GRzs"),
    ("question", "질문 문장 그대로.",
     '같은 qid 의 모든 줄에 똑같이 반복해 적습니다.  예: 간장 몇 큰술 넣어?'),
    ("q_type", "질문 유형 3가지 중 하나. 드롭다운.",
     "재료분량 / 순서타이밍 / 시각적상태 — 세 유형을 비슷한 개수로 맞춥니다. [진행현황] 탭에서 확인하세요."),
    ("start", "정답 구간 시작 시각(초).",
     "소수점 첫째 자리.  예: 22.0 · grade 가 '없음' 이면 비웁니다."),
    ("end", "정답 구간 끝 시각(초).",
     "소수점 첫째 자리.  예: 26.0 · start 보다 커야 하고 최소 2초 차이를 둡니다."),
    ("grade", "이 구간의 등급. 드롭다운.",
     "2 = 정확 · 1 = 관련 · 없음 = 영상에 답이 없음.  0은 적지 않습니다(빈칸이 곧 0점)."),
    ("labeler", "라벨링한 사람 이름.",
     "본인 이름을 한 번 적고 아래로 복사해 채웁니다. 두 사람 파일을 합칠 때 쓰입니다."),
    ("note", "(선택) 메모.",
     "판단이 애매했던 이유 등. 비워도 됩니다. 합의 회의 때 참고용."),
]
r = col_hdr + 1
for name, what, how in col_defs:
    base(ws.cell(row=r, column=2, value=name), bold=True, halign="center", border=True)
    base(ws.cell(row=r, column=3, value=what), wrap=True, valign="top", border=True)
    base(ws.cell(row=r, column=4, value=how), wrap=True, valign="top", border=True)
    ws.row_dimensions[r].height = 32
    r += 1

r += 1
ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
base(ws.cell(row=r, column=2, value="행(row) 규칙 — 한 질문이 여러 줄이 되는 이유"),
     bold=True, size=11, color=NAVY, fill=SEC_FILL)
ws.row_dimensions[r].height = 20
r += 1
for text in [
    "이 시트는 '질문 1개 = 1줄' 이 아니라 '정답 구간 1개 = 1줄' 입니다.",
    "한 질문에 2점 구간 1개와 1점 구간 1개가 있으면 그 질문은 2줄을 차지합니다. qid / video_id / question / q_type / labeler 는 두 줄에 똑같이 반복해서 적습니다.",
    "답이 없는 질문은 1줄만 쓰고 start · end 를 비운 뒤 grade 에 '없음' 을 적습니다.",
    "줄 순서는 상관없습니다. 정렬돼 있지 않아도 집계는 정상 동작합니다.",
]:
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4)
    base(ws.cell(row=r, column=2, value="·  " + text), wrap=True, valign="top")
    ws.row_dimensions[r].height = 16 if len(text) < 60 else 32
    r += 1

# =====================================================================
# 시트 2 · 라벨링
# =====================================================================
ws2 = wb.create_sheet("라벨링")
ws2.sheet_view.showGridLines = False
cols = ["qid", "video_id", "question", "q_type", "start", "end", "grade", "labeler", "note"]
header_row(ws2, 1, cols, widths=[8, 16, 38, 14, 9, 9, 9, 12, 32])
ws2.freeze_panes = "A3"

example = ["q01", "PbCq-C9GRzs", "가지는 어떻게 썰어?", "순서타이밍", 3.5, 7.5, 2,
           "예시-지우세요", "← 이 줄은 예시입니다. 확인 후 행 전체를 삭제하고 3행부터 쓰세요"]
for i, v in enumerate(example, start=1):
    base(ws2.cell(row=2, column=i, value=v), fill=EX_FILL, border=True,
         halign="center" if i in (1, 4, 5, 6, 7) else "left", wrap=(i == 9))
ws2.row_dimensions[2].height = 30

for row in range(3, 400):
    for i in range(1, len(cols) + 1):
        base(ws2.cell(row=row, column=i), border=True,
             halign="center" if i in (1, 4, 5, 6, 7) else "left")

dv_type = DataValidation(type="list", formula1='"재료분량,순서타이밍,시각적상태"', allow_blank=True)
dv_type.errorTitle, dv_type.error = "유형 선택", "재료분량 / 순서타이밍 / 시각적상태 중에서 고르세요."
ws2.add_data_validation(dv_type)
dv_type.add(f"D2:D{LAST}")

dv_grade = DataValidation(type="list", formula1='"2,1,없음"', allow_blank=True)
dv_grade.errorTitle, dv_grade.error = "등급 선택", "2(정확) / 1(관련) / 없음 중에서 고르세요. 0은 적지 않습니다."
ws2.add_data_validation(dv_grade)
dv_grade.add(f"G2:G{LAST}")

dv_vid = DataValidation(type="list", formula1="'영상목록'!$B$4:$B$60", allow_blank=True)
ws2.add_data_validation(dv_vid)
dv_vid.add(f"B2:B{LAST}")

# =====================================================================
# 시트 3 · 영상목록
# =====================================================================
ws3 = wb.create_sheet("영상목록")
ws3.sheet_view.showGridLines = False
title_block(ws3, "저장된 영상 목록",
            "서버 DB(data/vault.db) 에서 뽑은 현재 상태입니다. 영상을 더 저장하면 아래 빈 줄에 이어서 추가하세요.", 8)
header_row(ws3, 3, ["No", "video_id", "제목", "채널", "길이(초)", "시청 URL (watch 주소)", "상태", "질문 대상"],
           widths=[5, 16, 44, 12, 10, 44, 10, 11])

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
videos = list(conn.execute(
    "SELECT video_id, title, channel, duration, status FROM videos ORDER BY created_at"))

r = 4
for n, v in enumerate(videos, start=1):
    url = f"https://www.youtube.com/watch?v={v['video_id']}"
    vals = [n, v["video_id"], v["title"], v["channel"] or "-",
            round(float(v["duration"] or 0), 1), url, v["status"],
            "O" if v["status"] == "ready" else ""]
    for i, val in enumerate(vals, start=1):
        c = ws3.cell(row=r, column=i, value=val)
        base(c, border=True, halign="center" if i in (1, 5, 7, 8) else "left")
        if i == 6:
            c.hyperlink = url
            c.font = Font(name=FONT, size=10, color="0563C1", underline="single")
        if i == 7 and v["status"] != "ready":
            c.fill = WARN_FILL
    r += 1

blank_end = r + 30
for row in range(r, blank_end):
    for i in range(1, 9):
        base(ws3.cell(row=row, column=i), border=True,
             halign="center" if i in (1, 5, 7, 8) else "left")

note_row = blank_end + 1
for text in [
    "· '질문 대상' 칸에 O 를 적은 영상에만 질문을 만듭니다. 저장 영상 25개 중 12~15개만 O 로 두고, 나머지는 검색 난이도를 유지하는 오답 후보로 남겨둡니다.",
    "· 상태가 ready 가 아닌 영상은 분석이 끝나지 않은 것입니다. 질문 대상에서 제외하세요.",
]:
    ws3.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=8)
    base(ws3.cell(row=note_row, column=1, value=text), size=9, color="595959", wrap=True)
    ws3.row_dimensions[note_row].height = 26
    note_row += 1

# =====================================================================
# 시트 4 · 진행현황
# =====================================================================
ws4 = wb.create_sheet("진행현황")
ws4.sheet_view.showGridLines = False
title_block(ws4, "진행 현황 · 자동 집계",
            "[라벨링] 탭을 채우면 자동으로 갱신됩니다. 이 탭에는 직접 입력하지 마세요.", 4)
header_row(ws4, 3, ["항목", "현재", "목표", "판정"], widths=[34, 12, 16, 14])

Q = f"라벨링!$A$3:$A${LAST}"
T = f"라벨링!$D$3:$D${LAST}"
G = f"라벨링!$G$3:$G${LAST}"
L = f"라벨링!$H$3:$H${LAST}"
DQ = f'IFERROR(SUMPRODUCT(({Q}<>"")/COUNTIF({Q},{Q}&"")),0)'
DL = f'IFERROR(SUMPRODUCT(({L}<>"")/COUNTIF({L},{L}&"")),0)'

metrics = [
    ("입력된 라벨 줄 수", f"=COUNTA({Q})", "", ""),
    ("서로 다른 질문 수 (qid 기준)", f"={DQ}", "45 ~ 55", f'=IF({DQ}>=45,"충분","더 필요")'),
    ("라벨러 수", f"={DL}", "2", f'=IF({DL}>=2,"충분","더 필요")'),
    ("SECTION", "유형별 2점(정확) 라벨 수 — 비슷하게 맞추세요", "", ""),
    ("재료분량", f'=COUNTIFS({T},"재료분량",{G},2)', "", ""),
    ("순서타이밍", f'=COUNTIFS({T},"순서타이밍",{G},2)', "", ""),
    ("시각적상태", f'=COUNTIFS({T},"시각적상태",{G},2)', "", ""),
    ("유형 간 최대 격차", "=MAX(B8:B10)-MIN(B8:B10)", "5 이하",
     '=IF(MAX(B8:B10)-MIN(B8:B10)<=5,"균형","치우침")'),
    ("SECTION", "검증 항목", "", ""),
    ("'없음' 으로 표시한 질문 수", f'=COUNTIF({G},"없음")', "5 ~ 8",
     f'=IF(COUNTIF({G},"없음")>=5,"충분","더 필요")'),
    ("2점(정확) 라벨 총 수", f"=COUNTIF({G},2)", "", ""),
    ("1점(관련) 라벨 총 수", f"=COUNTIF({G},1)", "", ""),
]

r = 4
for name, cur, goal, verdict in metrics:
    if name == "SECTION":
        ws4.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        base(ws4.cell(row=r, column=1, value=cur), bold=True, size=10, color=NAVY, fill=SEC_FILL)
        ws4.row_dimensions[r].height = 20
        r += 1
        continue
    base(ws4.cell(row=r, column=1, value=name), border=True)
    base(ws4.cell(row=r, column=2, value=cur), border=True, halign="center", bold=True)
    base(ws4.cell(row=r, column=3, value=goal), border=True, halign="center", color="595959")
    base(ws4.cell(row=r, column=4, value=verdict), border=True, halign="center")
    r += 1

r += 1
for text in [
    "※ '유형 간 최대 격차' 는 세 유형의 2점 라벨 수 중 최대-최소 차이입니다. 한 유형만 많으면 유형별 성능 비교가 흔들립니다.",
    "※ 목표 숫자는 영상 12~15개 × 질문 3~4개 기준입니다. 규모를 바꾸면 목표도 함께 조정하세요.",
]:
    ws4.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    base(ws4.cell(row=r, column=1, value=text), size=9, color="595959", wrap=True)
    ws4.row_dimensions[r].height = 26
    r += 1

wb.save(OUT)
print(f"만들었습니다: {OUT}")
print(f"[영상목록] 탭에 채운 영상: {len(videos)}개")
