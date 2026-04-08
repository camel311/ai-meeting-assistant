#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  🎙️  AI 회의 어시스턴트 — 실행
#  이 파일을 더블클릭하면 회의 어시스턴트가 시작됩니다.
# ═══════════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

printf '\e[8;30;70t' 2>/dev/null
clear

echo ""
echo "  🎙️  AI 회의 어시스턴트 시작 중..."
echo ""

# ── Python 찾기 ───────────────────────────────────────────
PYTHON=""
for py in "/opt/homebrew/bin/python3" "/usr/local/bin/python3" \
           "/usr/bin/python3" "$(which python3 2>/dev/null)"; do
    if [ -x "$py" ]; then PYTHON="$py"; break; fi
done

if [ -z "$PYTHON" ]; then
    echo "  ❌  Python을 찾을 수 없습니다."
    echo "      install.command를 먼저 실행해주세요."
    read -p "  Enter를 눌러 닫기..."; exit 1
fi

# ── 필수 패키지 확인 ──────────────────────────────────────
MISSING=""
for pkg in flask faster_whisper sounddevice numpy; do
    $PYTHON -c "import $pkg" 2>/dev/null || MISSING="$MISSING $pkg"
done

if [ -n "$MISSING" ]; then
    echo "  ❌  설치되지 않은 패키지:$MISSING"
    echo ""
    echo "  install.command를 먼저 실행해주세요."
    echo ""
    read -p "  Enter를 눌러 닫기..."; exit 1
fi

# ── server.py 확인 ────────────────────────────────────────
if [ ! -f "$DIR/server.py" ]; then
    echo "  ❌  server.py를 찾을 수 없습니다."
    read -p "  Enter를 눌러 닫기..."; exit 1
fi

echo "  ✅  준비 완료"
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  🌐  http://localhost:5555             │"
echo "  │                                         │"
echo "  │  브라우저가 자동으로 열립니다.           │"
echo "  │  종료하려면 이 창을 닫거나 Ctrl+C       │"
echo "  │  모바일: HTTPS=1 python3 server.py     │"
echo "  └─────────────────────────────────────────┘"
echo ""

"$PYTHON" "$DIR/server.py"

echo ""
echo "  서버가 종료됐습니다."
read -p "  Enter를 눌러 닫기..."
