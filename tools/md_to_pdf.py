# -*- coding: utf-8 -*-
"""마크다운 문서를 PDF 로 뽑습니다.

    python -m tools.md_to_pdf docs/PROGRESS.md
    python -m tools.md_to_pdf docs/PROGRESS.md docs/eval-guide.md
    python -m tools.md_to_pdf docs/PROGRESS.md --out docs/progress-full.pdf

`docs/summary.pdf` 와 `docs/plan-and-roles.pdf` 는 **내용을 추려 따로 짠 것**이라
전용 도구가 따로 있습니다(`make_summary_pdf` · `make_plan_pdf`). 이 도구는
**마크다운을 있는 그대로** 종이에 옮기는 용도입니다.

한글 PDF 는 헤드리스 크롬으로 만듭니다 — reportlab 은 CJK 글꼴을 따로 등록해야 하고
Windows 글꼴 경로가 환경마다 달라, 시스템 글꼴을 그대로 쓰는 크롬이 안전합니다.
표가 많은 문서라 `markdown-it-py` 의 GFM 표 확장을 켭니다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from markdown_it import MarkdownIt

from server.config import ROOT
from tools.make_user_study_pdf import find_chrome

CSS = """
@page { size: A4; margin: 15mm 13mm 14mm; }
* { box-sizing: border-box; }
body { font-family: "Malgun Gothic", "맑은 고딕", sans-serif; font-size: 9.2pt;
       color: #111; line-height: 1.5; margin: 0; }
h1 { font-size: 16pt; margin: 0 0 3mm; padding-bottom: 2mm;
     border-bottom: 2px solid #222; }
h2 { font-size: 12.5pt; margin: 7mm 0 2mm; padding-bottom: 1mm;
     border-bottom: 1.2px solid #444; page-break-after: avoid; }
h3 { font-size: 10.6pt; margin: 4.5mm 0 1.5mm; page-break-after: avoid; }
h4 { font-size: 9.8pt; margin: 3mm 0 1mm; }
p { margin: 1.5mm 0; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 3mm;
        page-break-inside: avoid; font-size: 8.8pt; }
th, td { border: 0.6px solid #999; padding: 1.3mm 1.8mm; text-align: left;
         vertical-align: top; }
th { background: #eceff4; font-weight: 700; }
blockquote { margin: 2mm 0; padding: 2mm 3mm; background: #f5f7fa;
             border-left: 3px solid #666; page-break-inside: avoid; }
blockquote p { margin: 1mm 0; }
code { font-family: Consolas, "D2Coding", monospace; font-size: 8.4pt;
       background: #f2f2f2; padding: 0.3mm 1mm; border-radius: 1mm; }
pre { background: #f6f6f6; border: 0.6px solid #ddd; border-radius: 1.5mm;
      padding: 2.5mm 3mm; overflow-x: hidden; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8pt; line-height: 1.35; }
ul, ol { margin: 1.5mm 0; padding-left: 6mm; }
li { margin: 0.6mm 0; }
hr { border: 0; border-top: 0.8px solid #ccc; margin: 5mm 0; }
a { color: #1a4d8f; text-decoration: none; }
img { max-width: 100%; }
"""


def render(path: Path) -> str:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")
    text = path.read_text(encoding="utf-8")
    body = md.render(text)
    # 문서 안 상대 링크는 종이에서 쓸모가 없으니 글자만 남깁니다.
    body = re.sub(r'<a href="(?!https?:)[^"]*">(.*?)</a>', r"\1", body, flags=re.S)
    return body


def to_pdf(chrome: Path, html: str, out: Path) -> None:
    # ⚠ 크롬의 --print-to-pdf 는 **절대 경로**를 줘야 합니다. 상대 경로를 주면
    #   오류 없이 자기 작업 디렉터리에 쓰고, 우리는 파일이 안 생긴 줄 모릅니다.
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.html")
    tmp.write_text(
        f"<!doctype html><meta charset='utf-8'><style>{CSS}</style><body>{html}</body>",
        encoding="utf-8")
    try:
        url = "file:///" + str(tmp.resolve()).replace("\\", "/")
        subprocess.run(
            [str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--print-to-pdf={out}", url],
            check=True, capture_output=True, timeout=300,
        )
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    p = argparse.ArgumentParser(description="마크다운 → PDF")
    p.add_argument("files", nargs="+", help="변환할 .md 파일")
    p.add_argument("--out", default=None, help="출력 경로 (파일 하나일 때만)")
    args = p.parse_args()

    chrome = find_chrome()
    if chrome is None:
        print("크롬(또는 엣지)을 찾지 못했습니다. 한글 PDF 생성에 필요합니다.", file=sys.stderr)
        return 2
    if args.out and len(args.files) > 1:
        print("--out 은 파일 하나일 때만 쓸 수 있습니다.", file=sys.stderr)
        return 2

    for name in args.files:
        src = Path(name)
        if not src.is_file():
            print(f"없는 파일: {src}", file=sys.stderr)
            return 1
        out = Path(args.out) if args.out else src.with_suffix(".pdf")
        to_pdf(chrome, render(src), out)
        out = out.resolve()
        if not out.is_file():
            print(f"PDF 가 만들어지지 않았습니다: {out}", file=sys.stderr)
            return 1
        lines = len(src.read_text(encoding="utf-8").splitlines())
        print(f"만들었습니다: {out}   ({src.name} · {lines}줄 · {out.stat().st_size // 1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
