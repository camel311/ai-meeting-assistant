#!/usr/bin/env python3
"""
🔍 회의록 키워드 검색 CLI

사용법:
  python3 search.py [키워드]
  python3 search.py [키워드] --date 2026-04
  python3 search.py [키워드] --speaker Jerry
  python3 search.py --list          # 전체 회의 목록
  python3 search.py --summary       # 최근 회의 요약 목록
"""

import sys, re, argparse
from pathlib import Path
from datetime import datetime

_BASE_DIR  = Path(__file__).parent
OUTPUT_DIR = _BASE_DIR / "meetings"
CONTEXT_LINES = 2


# ── ANSI 컬러 ──────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    YELLOW = "\033[33m"
    GREEN  = "\033[32m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"
    RED    = "\033[31m"

def hi(text: str, color: str) -> str:
    return f"{color}{text}{C.RESET}"


# ── 회의 파일 목록 ─────────────────────────────────────────
def list_meetings():
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if not files:
        print("  회의 파일이 없습니다.")
        return

    print(f"\n{hi('📁 전체 회의 목록', C.BOLD)}\n")
    for f in files:
        content = f.read_text(encoding="utf-8")
        # 제목 추출
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem
        # 참여자 추출
        p_match = re.search(r'\*\*참여자:\*\* (.+)', content)
        participants = p_match.group(1) if p_match else "-"
        # 파일 크기로 대략적인 발화량 파악
        lines = [l for l in content.splitlines() if l.startswith("**") and "|" in l]
        size = f"{len(lines)}개 발화"

        print(f"  {hi(title, C.CYAN)}")
        print(f"  {hi('참여자:', C.GRAY)} {participants}  {hi(size, C.GRAY)}")
        print(f"  {hi('파일:', C.GRAY)} {f.name}\n")


# ── 요약 목록 ──────────────────────────────────────────────
def list_summaries():
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if not files:
        print("  회의 파일이 없습니다.")
        return

    print(f"\n{hi('📋 최근 회의 요약', C.BOLD)}\n")
    for f in files[:5]:
        content = f.read_text(encoding="utf-8")
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem

        print(f"  {hi('━' * 45, C.GRAY)}")
        print(f"  {hi(title, C.CYAN)}")

        if "# 🤖 AI 회의 분석" in content:
            analysis = content.split("# 🤖 AI 회의 분석")[-1]
            # 회의 요약 섹션만 출력
            if "## 📋 회의 요약" in analysis:
                summary = analysis.split("## 📋 회의 요약")[-1]
                summary = summary.split("##")[0].strip()
                for line in summary.splitlines()[:5]:
                    if line.strip():
                        print(f"    {line.strip()}")
        else:
            print(f"  {hi('  (AI 분석 없음 — 회의 종료 시 자동 생성됨)', C.GRAY)}")
        print()


# ── 키워드 검색 ────────────────────────────────────────────
def search(keyword: str, date_filter: str = "", speaker_filter: str = ""):
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if not files:
        print("  회의 파일이 없습니다.")
        return

    # 날짜 필터
    if date_filter:
        files = [f for f in files if date_filter in f.stem]

    total_hits = 0
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)

    print(f"\n{hi(f'🔍 \"{keyword}\" 검색 결과', C.BOLD)}\n")

    for f in files:
        content = f.read_text(encoding="utf-8")
        lines   = content.splitlines()

        # 제목
        title_match = re.search(r'^# (.+)$', content, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem

        hits = []
        for i, line in enumerate(lines):
            # 발화 라인만 검색 (** 타임스탬프 포함)
            if not (line.startswith("**") and "|" in line):
                continue
            # 발화자 필터
            if speaker_filter and f"**{speaker_filter}**" not in line:
                continue
            if pattern.search(line):
                # 앞뒤 컨텍스트 수집
                start = max(0, i - CONTEXT_LINES)
                end   = min(len(lines), i + CONTEXT_LINES + 1)
                ctx   = lines[start:end]
                hits.append((i, line, ctx))

        if not hits:
            continue

        total_hits += len(hits)
        print(f"  {hi('📄 ' + title, C.CYAN)}  {hi(f'({len(hits)}건)', C.YELLOW)}")
        print(f"  {hi(f.name, C.GRAY)}\n")

        for idx, (line_no, matched_line, ctx) in enumerate(hits):
            for ctx_line in ctx:
                if ctx_line == matched_line:
                    # 키워드 하이라이트
                    highlighted = pattern.sub(
                        lambda m: hi(m.group(), C.YELLOW + C.BOLD), ctx_line
                    )
                    print(f"  {hi('▶', C.GREEN)} {highlighted}")
                else:
                    print(f"    {hi(ctx_line, C.GRAY)}")
            if idx < len(hits) - 1:
                print()
        print()

    if total_hits == 0:
        print(f"  {hi('검색 결과 없음', C.RED)}")
    else:
        print(f"  {hi(f'총 {total_hits}건 발견', C.GREEN)}")


# ── 메인 ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="회의록 키워드 검색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python3 search.py 백엔드
  python3 search.py 마감일 --date 2026-04
  python3 search.py API --speaker Jerry
  python3 search.py --list
  python3 search.py --summary
        """
    )
    parser.add_argument("keyword",         nargs="?", help="검색 키워드")
    parser.add_argument("--date",          default="",  help="날짜 필터 (예: 2026-04)")
    parser.add_argument("--speaker",       default="",  help="발화자 필터 (예: Jerry)")
    parser.add_argument("--list",          action="store_true", help="전체 회의 목록")
    parser.add_argument("--summary",       action="store_true", help="최근 회의 요약 목록")

    args = parser.parse_args()

    if not OUTPUT_DIR.exists():
        print("  meetings/ 폴더가 없습니다. meeting.py를 먼저 실행하세요.")
        return

    if args.list:
        list_meetings()
    elif args.summary:
        list_summaries()
    elif args.keyword:
        search(args.keyword, args.date, args.speaker)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
