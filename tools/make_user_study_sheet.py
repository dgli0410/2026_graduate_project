# -*- coding: utf-8 -*-
"""사용자 평가 기록 양식을 만듭니다 (자문 7번 · 2차 자문 "사용자 평가").

    python -m tools.make_user_study_sheet

설계는 `docs/user-study.md` 에 있습니다. 이 도구는 그 설계대로 **빈 기록지**를
만들 뿐입니다. 참가자 10명 x 과제 6개 = 60줄이 조건까지 배정된 채로 들어갑니다.

성공 판정은 **사람이 눈으로 하지 않고 수식이 합니다.** 도달 시점만 적으면
`정답시작 - 5초 ~ 정답시작 + 2초` 안인지 엑셀이 계산합니다. 평가 스크립트의
비대칭 허용 오차(`tools/eval_search.py` 의 LEAD/LAG)와 **같은 자**를 쓰려는 것입니다.
진행자가 암산하면 사람마다 기준이 흔들립니다.

이미 채운 기록을 덮어쓰지 않도록, 파일이 있으면 --force 가 필요합니다.

이 도구에만 openpyxl 이 필요합니다:  pip install openpyxl
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from server.config import DB_PATH, ROOT

LEAD, LAG = 5.0, 2.0  # tools/eval_search.py 와 같은 값이어야 합니다
LIMIT_SEC = 180  # 한 과제 제한 시간 (docs/user-study.md 5장)

# 과제 6개 — 평가셋에서 **정답 구간이 이미 라벨링된** 질문만 골랐습니다.
# 그래야 성공 판정 기준이 자동 평가와 같아집니다.
# 영상이 전부 다릅니다. 같은 영상이 두 번 나오면 참가자가 그 영상을 학습해
# 두 번째 과제가 쉬워집니다.
# (세트, 과제번호, video_id, 유형, 참가자에게 읽어줄 목표, 정답시작, 정답끝)
# ⚠ 정답이 영상 **앞부분**인 질문은 쓸 수 없습니다. 허용 오차가 이전 5초라
#   정답시작이 5초면 인정 범위가 0~7초가 되어 **영상을 건드리지 않아도 성공**이 됩니다.
#   그래서 정답시작이 MIN_ANSWER_SEC 이상인 것만 골랐습니다(아래 check_tasks 가 검사).
MIN_ANSWER_SEC = 12.0

TASKS = [
    (1, "T1", "4due-wU9NFk", "재료·분량",
     "이 영상에서 물을 얼마나 넣는지 알려주는 장면으로 이동하세요.", 35.0, 41.0),
    (1, "T2", "OgR00HBMKFs", "순서·타이밍",
     "두부를 조리하는 장면으로 이동하세요.", 22.0, 33.0),
    (1, "T3", "Zlo2JQzihwc", "시각적 상태",
     "익힌 면을 건져 넣는 장면으로 이동하세요.", 39.1, 43.0),
    (2, "T4", "Z7z0wW62obE", "재료·분량",
     "고기를 얼마나 쓰는지 알려주는 장면으로 이동하세요.", 15.0, 18.0),
    (2, "T5", "iDMyw61V1XM", "순서·타이밍",
     "떡을 넣는 순간으로 이동하세요.", 31.0, 34.0),
    (2, "T6", "rfkc9X4BItc", "시각적 상태",
     "무를 어느 정도 크기로 써는지 보이는 장면으로 이동하세요.", 22.0, 24.0),
]

# 배정표 (docs/user-study.md 4장). 조건 순서와 세트 순서가 각각 5명씩 균형입니다.
# (참가자, 전반 세트, 전반 조건, 후반 세트, 후반 조건)
PLAN = [
    ("P01", 1, "A", 2, "B"), ("P02", 1, "B", 2, "A"),
    ("P03", 2, "A", 1, "B"), ("P04", 2, "B", 1, "A"),
    ("P05", 1, "A", 2, "B"), ("P06", 1, "B", 2, "A"),
    ("P07", 2, "A", 1, "B"), ("P08", 2, "B", 1, "A"),
    ("P09", 1, "A", 2, "B"), ("P10", 1, "B", 2, "A"),
]

HEAD = PatternFill("solid", fgColor="D9E2F3")
WARN = PatternFill("solid", fgColor="FCE4D6")
AUTO = PatternFill("solid", fgColor="EDEDED")
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")


def widths(ws, spec: dict[str, int]) -> None:
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def titles() -> dict[str, str]:
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        out = dict(conn.execute("SELECT video_id, title FROM videos"))
        conn.close()
        return out
    except Exception:  # noqa: BLE001 - DB 가 없어도 양식은 만들 수 있어야 합니다
        return {}


def tab_how(wb: Workbook) -> None:
    ws = wb.create_sheet("진행방법")
    widths(ws, {"A": 4, "B": 100})
    lines = [
        ("", "사용자 평가 진행 방법 — 전체 설계는 docs/user-study.md"),
        ("", ""),
        ("1", "준비: 서버 켜기 / 과제 영상 6개가 보관함에 있는지 확인 / 스톱워치 / 이전 참가자 검색기록 지우기"),
        ("", ""),
        ("2", "[안내] 그대로 읽으세요. 진행자가 더 잘 설명해주면 그 참가자만 빨라집니다."),
        ("", "  \"요리 쇼츠에서 특정 장면을 찾는 과제를 6개 드립니다. 앞 3개와 뒤 3개는 찾는 방법이 다릅니다.\""),
        ("", "  \"시간을 재지만 빨리 하는 게 목표가 아닙니다. 평소 하시던 대로 하세요.\""),
        ("", "  \"한 과제에 3분이 넘으면 제가 중단시킵니다. 참가자분 잘못이 아니라 저희 시스템을 평가하는 것입니다.\""),
        ("", "  \"중간에 그만두고 싶으시면 언제든 말씀하세요.\""),
        ("", ""),
        ("3", "[A 조건 = 직접 찾기] \"이 영상입니다. 유튜브에서 직접 원하는 장면을 찾아 그 시점으로 이동해 주세요.\""),
        ("", "[B 조건 = 우리 시스템] \"이 검색창에 찾고 싶은 것을 평소 말하듯 입력하세요. 어떻게 입력할지는 알려드리지 않겠습니다.\""),
        ("", ""),
        ("4", "과제마다: 목표 읽기 → \"시작하겠습니다\" 와 동시에 스톱워치 → 참가자가 \"찾았어요\" 하면 멈추고 도달 시점 기록"),
        ("", f"  3분({LIMIT_SEC}초)이 되면 \"여기까지 하겠습니다\" 하고 포기=예 로 기록"),
        ("", ""),
        ("5", "절대 하지 말 것"),
        ("", "  · 검색어 힌트 주기 (\"'간장'이라고 쳐보세요\")"),
        ("", "  · 성공/실패 알려주기  · \"거의 다 왔어요\" 같은 반응  · 헤맬 때 거들기"),
        ("", ""),
        ("6", "끝나고(선택): \"쓰시면서 불편했던 점 한 가지만 말씀해 주세요\" — 한 줄만 받아 적고 점수화하지 않습니다"),
        ("", ""),
        ("", "※ 성공 판정은 [기록] 탭이 수식으로 합니다. 도달 시점만 적으면 됩니다."),
        ("", f"※ 판정 기준: 정답시작 - {LEAD:.0f}초 ~ 정답시작 + {LAG:.0f}초 (평가 스크립트와 같은 자)"),
    ]
    for i, (a, b) in enumerate(lines, start=1):
        ws.cell(i, 1, a).font = BOLD
        ws.cell(i, 2, b).alignment = WRAP
    ws.cell(1, 2).font = Font(bold=True, size=13)


def tab_tasks(wb: Workbook, title_map: dict[str, str]) -> None:
    ws = wb.create_sheet("과제")
    cols = ["세트", "과제", "video_id", "영상 제목", "유형",
            "참가자에게 읽어줄 목표", "정답시작", "정답끝", "성공 인정 범위"]
    for j, c in enumerate(cols, start=1):
        cell = ws.cell(1, j, c)
        cell.font = BOLD
        cell.fill = HEAD
    for i, (st, tid, vid, kind, goal, s, e) in enumerate(TASKS, start=2):
        ws.cell(i, 1, st)
        ws.cell(i, 2, tid)
        ws.cell(i, 3, vid)
        ws.cell(i, 4, title_map.get(vid, "(DB 에 없음 — 저장했는지 확인)"))
        ws.cell(i, 5, kind)
        ws.cell(i, 6, goal).alignment = WRAP
        ws.cell(i, 7, s)
        ws.cell(i, 8, e)
        ws.cell(i, 9, f"{max(0.0, s - LEAD):.1f} ~ {s + LAG:.1f} 초")
    widths(ws, {"A": 6, "B": 7, "C": 14, "D": 30, "E": 12, "F": 46, "G": 9, "H": 9, "I": 16})
    ws.cell(len(TASKS) + 3, 1, "⚠ 이 탭은 진행자만 봅니다. 참가자에게 보여주지 마세요.").font = BOLD
    ws.cell(len(TASKS) + 3, 1).fill = WARN
    ws.cell(len(TASKS) + 4, 1,
            "정답 구간은 eval/labels_merged.csv 에서 가져왔습니다 — 자동 평가와 같은 기준입니다.")
    ws.freeze_panes = "A2"


def tab_log(wb: Workbook) -> None:
    ws = wb.create_sheet("기록")
    cols = ["참가자", "순서", "조건", "세트", "과제", "유형", "영상 제목",
            "첫 질의문", "도달시점(초)", "소요시간(초)", "시도횟수", "포기",
            "성공", "메모"]
    for j, c in enumerate(cols, start=1):
        cell = ws.cell(1, j, c)
        cell.font = BOLD
        cell.fill = HEAD

    by_set: dict[int, list] = {1: [], 2: []}
    for t in TASKS:
        by_set[t[0]].append(t)

    row = 2
    for pid, s1, c1, s2, c2 in PLAN:
        order = 1
        for st, cond in ((s1, c1), (s2, c2)):
            for (_, tid, vid, kind, _goal, _s, _e) in by_set[st]:
                ws.cell(row, 1, pid)
                ws.cell(row, 2, order)
                ws.cell(row, 3, "A 직접찾기" if cond == "A" else "B 시스템")
                ws.cell(row, 4, st)
                ws.cell(row, 5, tid)
                ws.cell(row, 6, kind)
                ws.cell(row, 7, f'=VLOOKUP(E{row},과제!$B:$D,3,FALSE)')
                # 성공 = 도달시점이 (정답시작-LEAD) ~ (정답시작+LAG) 안인가.
                # 포기면 바로 X. 비워두면 판정도 비워 둡니다.
                ws.cell(row, 13, (
                    f'=IF(L{row}="예","X",'
                    f'IF(I{row}="","",'
                    f'IF(AND(I{row}>=VLOOKUP(E{row},과제!$B:$G,6,FALSE)-{LEAD},'
                    f'I{row}<=VLOOKUP(E{row},과제!$B:$G,6,FALSE)+{LAG}),"O","X")))'
                ))
                ws.cell(row, 13).fill = AUTO
                ws.cell(row, 7).fill = AUTO
                row += 1
                order += 1
    last = row - 1

    yn = DataValidation(type="list", formula1='"예,아니오"', allow_blank=True)
    ws.add_data_validation(yn)
    yn.add(f"L2:L{last}")

    widths(ws, {"A": 9, "B": 6, "C": 12, "D": 6, "E": 7, "F": 12, "G": 26,
                "H": 28, "I": 13, "J": 13, "K": 9, "L": 7, "M": 7, "N": 30})
    ws.freeze_panes = "C2"

    note = last + 2
    ws.cell(note, 1, "회색 칸(영상 제목·성공)은 수식입니다. 건드리지 마세요.").font = BOLD
    ws.cell(note + 1, 1, "적을 것: 첫 질의문(B조건만) · 도달시점 · 소요시간 · 시도횟수 · 포기 · 메모")
    ws.cell(note + 2, 1, f"3분({LIMIT_SEC}초)을 넘기면 포기=예 로 적고 소요시간은 {LIMIT_SEC} 로 둡니다.")
    ws.cell(note + 3, 1, "A 조건에서는 첫 질의문을 비워 둡니다(검색을 안 쓰므로).")


def tab_summary(wb: Workbook) -> None:
    ws = wb.create_sheet("집계")
    n = 1 + len(PLAN) * len(TASKS)
    log = f"기록!$C$2:$C${n}"
    ok = f"기록!$M$2:$M${n}"
    sec = f"기록!$J$2:$J${n}"
    kind = f"기록!$F$2:$F${n}"

    ws.cell(1, 1, "조건별 — 주지표").font = Font(bold=True, size=12)
    head = ["조건", "과제 수", "성공", "성공률", "성공 과제 평균 시간(초)", "평균 시도횟수"]
    for j, c in enumerate(head, start=1):
        ws.cell(2, j, c).font = BOLD
        ws.cell(2, j).fill = HEAD
    for i, cond in enumerate(("A 직접찾기", "B 시스템"), start=3):
        ws.cell(i, 1, cond)
        ws.cell(i, 2, f'=COUNTIF({log},A{i})')
        ws.cell(i, 3, f'=COUNTIFS({log},A{i},{ok},"O")')
        ws.cell(i, 4, f'=IF(B{i}=0,"",C{i}/B{i})').number_format = "0%"
        # 시간은 **성공한 과제만**. 실패를 180초로 넣으면 결과가 왜곡됩니다.
        ws.cell(i, 5, f'=IFERROR(AVERAGEIFS({sec},{log},A{i},{ok},"O"),"")').number_format = "0.0"
        ws.cell(i, 6, f'=IFERROR(AVERAGEIFS(기록!$K$2:$K${n},{log},A{i}),"")').number_format = "0.0"

    ws.cell(6, 1, "유형별").font = Font(bold=True, size=12)
    head2 = ["유형", "A 성공률", "B 성공률", "A 평균시간", "B 평균시간"]
    for j, c in enumerate(head2, start=1):
        ws.cell(7, j, c).font = BOLD
        ws.cell(7, j).fill = HEAD
    for i, t in enumerate(("재료·분량", "순서·타이밍", "시각적 상태"), start=8):
        ws.cell(i, 1, t)
        for col, cond in ((2, "A 직접찾기"), (3, "B 시스템")):
            ws.cell(i, col, (
                f'=IF(COUNTIFS({log},"{cond}",{kind},A{i})=0,"",'
                f'COUNTIFS({log},"{cond}",{kind},A{i},{ok},"O")'
                f'/COUNTIFS({log},"{cond}",{kind},A{i}))'
            )).number_format = "0%"
        for col, cond in ((4, "A 직접찾기"), (5, "B 시스템")):
            ws.cell(i, col, (
                f'=IFERROR(AVERAGEIFS({sec},{log},"{cond}",{kind},A{i},{ok},"O"),"")'
            )).number_format = "0.0"

    ws.cell(12, 1, "해석할 때 지킬 것").font = Font(bold=True, size=12)
    for i, line in enumerate([
        "· n=10 입니다. 조건당 30과제뿐이라 \"유의하게 높다\" 는 쓰지 마세요. \"73% 대 90% 였다\" 까지만 씁니다.",
        "· 시간은 성공한 과제만 평균 냅니다. 실패 건수는 따로 밝히세요.",
        "· 참가자가 친 첫 질의문은 실제 사용자 말투라, 다음 평가셋 질문으로 쓸 수 있습니다.",
        "· 불편 의견은 인용만 하고 세지 마세요(만족도 설문을 하지 말라는 것이 자문의 취지입니다).",
    ], start=13):
        ws.cell(i, 1, line)
    widths(ws, {"A": 22, "B": 14, "C": 14, "D": 22, "E": 16, "F": 16})


def check_tasks() -> list[str]:
    """과제가 평가로 쓸 수 있는 모양인지 검사합니다.

    ⚠ 처음 만들 때 정답이 5초인 과제를 넣었다가 인정 범위가 **0~7초**가 되는 것을
      뒤늦게 봤습니다. 그러면 참가자가 아무것도 안 해도 성공으로 찍힙니다.
      같은 실수를 반복하지 않게 코드가 막습니다.
    """
    problems = []
    seen: dict[str, str] = {}
    for st, tid, vid, _kind, _goal, start, _end in TASKS:
        if start - LEAD < MIN_ANSWER_SEC - LEAD or start < MIN_ANSWER_SEC:
            problems.append(
                f"{tid}: 정답시작 {start:.1f}초가 너무 이릅니다 "
                f"(인정 범위 {max(0.0, start - LEAD):.1f}~{start + LAG:.1f}초 — "
                f"영상을 건드리지 않아도 성공이 될 수 있습니다)"
            )
        if vid in seen:
            problems.append(f"{tid}: 영상 {vid} 가 {seen[vid]} 와 겹칩니다 "
                            f"(참가자가 그 영상을 학습해 두 번째 과제가 쉬워집니다)")
        seen[vid] = tid
    return problems


def main() -> int:
    p = argparse.ArgumentParser(description="사용자 평가 기록 양식 생성")
    p.add_argument("--out", default=str(ROOT / "eval" / "user_study_sheet.xlsx"))
    p.add_argument("--force", action="store_true", help="기존 파일을 덮어씁니다")
    args = p.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"이미 있습니다: {out}", file=sys.stderr)
        print("덮어쓰면 적어둔 기록이 사라집니다. 정말 새로 만들려면 --force 를 붙이세요.",
              file=sys.stderr)
        return 1

    problems = check_tasks()
    if problems:
        print("과제 구성에 문제가 있습니다:", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        print("TASKS 를 고치고 다시 실행하세요.", file=sys.stderr)
        return 2

    wb = Workbook()
    wb.remove(wb.active)
    tab_how(wb)
    tab_tasks(wb, titles())
    tab_log(wb)
    tab_summary(wb)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    print(f"만들었습니다: {out}")
    print(f"  참가자 {len(PLAN)}명 x 과제 {len(TASKS)}개 = {len(PLAN) * len(TASKS)}줄")
    print(f"  조건 배정 완료 (A 먼저 5명 / B 먼저 5명)")
    print(f"  성공 판정은 수식 — 도달 시점만 적으면 됩니다 (기준 -{LEAD:.0f}초 ~ +{LAG:.0f}초)")
    print("\n설계 전문: docs/user-study.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
