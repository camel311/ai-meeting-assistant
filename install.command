#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  🎙️  AI 회의 어시스턴트 — 설치 프로그램
#  이 파일을 더블클릭하면 모든 설치가 자동으로 진행됩니다.
# ═══════════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

printf '\e[8;40;80t' 2>/dev/null

clear
echo ""
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║      🎙️  AI 회의 어시스턴트  설치         ║"
echo "  ╚═══════════════════════════════════════════╝"
echo ""
echo "  설치가 자동으로 진행됩니다."
echo "  완료될 때까지 이 창을 닫지 마세요."
echo ""

ok()   { echo "  ✅  $1"; }
info() { echo "  ℹ️   $1"; }
warn() { echo "  ⚠️   $1"; }
fail() { echo ""; echo "  ❌  $1"; echo ""; read -p "  Enter를 눌러 닫기..."; exit 1; }
step() { echo ""; echo "  ──────────────────────────────────────────"; echo "  🔧  $1"; echo "  ──────────────────────────────────────────"; }

# ── 1. Python 찾기 ────────────────────────────────────────
step "Python 확인 중..."
PYTHON=""
for py in "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3" \
           "$(which python3 2>/dev/null)" "$(which python 2>/dev/null)"; do
    if [ -x "$py" ]; then
        VER=$("$py" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 9 ]; then
            PYTHON="$py"; break
        fi
    fi
done
[ -z "$PYTHON" ] && fail "Python 3.9 이상이 필요합니다.\nhttps://www.python.org/downloads/ 에서 설치 후 다시 실행해주세요."
ok "Python: $($PYTHON --version)"

# ── 1.5. 가상환경 생성 ───────────────────────────────────
step "가상환경 설정 중..."
VENV_DIR="$DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    ok "가상환경 이미 존재 (.venv/)"
else
    info "가상환경 생성 중..."
    $PYTHON -m venv "$VENV_DIR" || fail "가상환경 생성 실패"
    ok "가상환경 생성 완료 (.venv/)"
fi
# 이후 모든 pip 작업은 가상환경 내에서 실행
PYTHON="$VENV_DIR/bin/python3"
ok "pip: $($PYTHON -m pip --version 2>&1 | head -1)"

# ── 2. Homebrew + portaudio ───────────────────────────────
step "Homebrew / portaudio 확인 중..."
if command -v brew &>/dev/null; then
    ok "Homebrew 이미 설치됨"
    for pkg in portaudio ffmpeg; do
        if brew list $pkg &>/dev/null 2>&1; then
            ok "$pkg 이미 설치됨"
        else
            info "$pkg 설치 중..."
            brew install $pkg -q && ok "$pkg 설치 완료" || warn "$pkg 설치 실패"
        fi
    done
else
    warn "Homebrew 없음 — portaudio 없이 진행 (마이크 오류 시 https://brew.sh 설치 후 재시도)"
fi

# ── 3. pip 패키지 설치 ────────────────────────────────────
step "필수 패키지 설치 중... (수 분 소요될 수 있습니다)"

PACKAGES="flask flask-socketio sounddevice numpy send2trash faster-whisper resemblyzer noisereduce cryptography torch pyannote.audio asteroid omegaconf"

# mlx-whisper 선택 설치 (Apple Silicon GPU 가속, 2~5배 빠름)
if [[ "$(uname -m)" == "arm64" ]]; then
    printf "  📦  %-20s" "mlx-whisper (Apple Silicon)..."
    if $PYTHON -m pip install mlx-whisper -q 2>/dev/null; then
        echo "✅ (GPU 가속 활성화)"
    else
        echo "ℹ️  (설치 실패, CPU 모드로 동작)"
    fi
fi

# webrtcvad 선택 설치
printf "  📦  %-20s" "webrtcvad (선택)..."
if $PYTHON -m pip install webrtcvad -q 2>/dev/null; then
    echo "✅"
else
    echo "ℹ️  (Silero VAD로 대체됨, 무시 가능)"
fi

info "설치 목록: $PACKAGES"
echo ""

$PYTHON -m pip install --upgrade pip -q 2>&1 | tail -1

for pkg in $PACKAGES; do
    printf "  📦  %-20s" "$pkg..."
    if $PYTHON -m pip install "$pkg" -q 2>/dev/null; then
        echo "✅"
    else
        echo "⚠️  (경고: 설치 실패, 일부 기능 제한될 수 있음)"
    fi
done

# ── 4. Whisper 모델 다운로드 ──────────────────────────────
step "Whisper 음성인식 모델 다운로드 중..."
echo ""
echo "  📥  large-v3-turbo 모델 (~1.5GB) 다운로드 중..."
echo "  ⏳  인터넷 속도에 따라 3~10분 소요될 수 있습니다."
echo ""

$PYTHON -c "
from faster_whisper import WhisperModel
print('  모델 다운로드 시작...')
try:
    WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')
    print('  ✅  모델 다운로드 완료')
except Exception as e:
    print(f'  ⚠️  모델 다운로드 실패: {e}')
    print('  ℹ️  첫 회의 시작 시 자동으로 다시 다운로드됩니다.')
"

# ── 5. LLM 백엔드 설정 ───────────────────────────────────
step "LLM 백엔드 확인 중..."
if command -v claude &>/dev/null; then
    ok "Claude Code CLI 감지됨 → AI 기능 Claude로 동작"
elif command -v ollama &>/dev/null || curl -s --max-time 3 http://localhost:11434/api/tags &>/dev/null; then
    ok "Ollama 감지됨 → AI 기능 Ollama로 동작"
    OLLAMA_TARGET="${OLLAMA_MODEL:-exaone3.5:7.8b-instruct-q4_K_M}"
    info "한국어 최적화 모델 다운로드 중: $OLLAMA_TARGET (~5GB, 수 분 소요)"
    if ollama pull "$OLLAMA_TARGET" 2>/dev/null; then
        ok "$OLLAMA_TARGET 모델 준비 완료"
    else
        warn "$OLLAMA_TARGET pull 실패 — 수동으로 실행: ollama pull $OLLAMA_TARGET"
    fi
else
    warn "Claude CLI, Ollama 모두 없음"
    info "Ollama 설치를 권장합니다: https://ollama.com"
    echo ""
    read -p "  Ollama를 지금 설치하시겠습니까? (y/N): " yn
    if [[ "$yn" == "y" || "$yn" == "Y" ]]; then
        if command -v brew &>/dev/null; then
            info "Homebrew로 Ollama 설치 중..."
            if brew install ollama 2>/dev/null; then
                ok "Ollama 설치 완료"
                # Ollama 서비스 시작
                ollama serve &>/dev/null &
                sleep 3
                OLLAMA_TARGET="${OLLAMA_MODEL:-exaone3.5:7.8b-instruct-q4_K_M}"
                info "모델 다운로드 중: $OLLAMA_TARGET (~5GB)"
                if ollama pull "$OLLAMA_TARGET" 2>/dev/null; then
                    ok "$OLLAMA_TARGET 모델 준비 완료"
                else
                    warn "모델 다운로드 실패 — 나중에 실행: ollama pull $OLLAMA_TARGET"
                fi
            else
                warn "Ollama 설치 실패"
                info "수동 설치: https://ollama.com"
            fi
        else
            info "Homebrew가 없어 자동 설치 불가. https://ollama.com 에서 직접 설치해주세요."
        fi
    else
        warn "AI 기능 없이 진행 (회의 기록은 정상 동작)"
    fi
fi

# ── 6. 실행 파일 권한 설정 ────────────────────────────────
step "실행 파일 권한 설정 중..."
chmod +x "$DIR/install.command" 2>/dev/null && ok "install.command"
chmod +x "$DIR/start.command" 2>/dev/null && ok "start.command"

# ── 7. 폴더 생성 ─────────────────────────────────────────
mkdir -p "$DIR/meetings" "$DIR/voices" "$DIR/static"
for f in glossary.json vocab_ko.json vocab_en.json vocab_ja.json; do
    [ ! -f "$DIR/$f" ] && echo '{}' > "$DIR/$f"
done
ok "meetings/, voices/, static/ 폴더 준비됨"

# ── 완료 ──────────────────────────────────────────────────
echo ""
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║            🎉  설치 완료!                 ║"
echo "  ╠═══════════════════════════════════════════╣"
echo "  ║                                           ║"
echo "  ║  이제 start.command 를 더블클릭하면       ║"
echo "  ║  회의 어시스턴트가 바로 시작됩니다.       ║"
echo "  ║                                           ║"
echo "  ╚═══════════════════════════════════════════╝"
echo ""
read -p "  Enter를 눌러 닫기..."
