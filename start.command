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

# ── 가상환경 확인 ─────────────────────────────────────────
VENV_DIR="$DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"

if [ ! -x "$PYTHON" ]; then
    echo "  ❌  가상환경이 없습니다."
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
    echo "      install.command를 다시 실행해주세요."
    read -p "  Enter를 눌러 닫기..."; exit 1
fi

# ── LLM 백엔드 확인 ───────────────────────────────────────
echo "  🤖  LLM 백엔드 확인 중..."
if command -v claude &>/dev/null; then
    echo "  ✅  Claude Code CLI → AI 기능 활성"
    LLM_LABEL="Claude Code"
elif curl -s --max-time 3 http://localhost:11434/api/tags &>/dev/null 2>&1; then
    OLLAMA_MODEL_NAME="${OLLAMA_MODEL:-exaone3.5}"
    echo "  ✅  Ollama (모델: $OLLAMA_MODEL_NAME) → AI 기능 활성"
    LLM_LABEL="Ollama ($OLLAMA_MODEL_NAME)"
    export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-1h}"
else
    echo "  ⚠️  Claude CLI / Ollama 미감지 — 회의 기록만 동작"
    LLM_LABEL="없음 (AI 기능 비활성)"
fi

echo "  ✅  준비 완료"
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  🌐  http://localhost:5555             │"
echo "  │  🤖  LLM: $LLM_LABEL"
echo "  │                                         │"
echo "  │  브라우저가 자동으로 열립니다.           │"
echo "  │  종료하려면 이 창을 닫거나 Ctrl+C       │"
echo "  └─────────────────────────────────────────┘"
echo ""

"$PYTHON" "$DIR/server.py"

echo ""
echo "  서버가 종료됐습니다."
read -p "  Enter를 눌러 닫기..."
