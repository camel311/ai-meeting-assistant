#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  🎙️  AI 회의 어시스턴트 — 설치 프로그램
#  이 파일을 더블클릭하면 모든 설치가 자동으로 진행됩니다.
# ═══════════════════════════════════════════════════════════

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ── 터미널 크기 설정 ──────────────────────────────────────
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

# ── 함수 정의 ─────────────────────────────────────────────
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

# ── 2. Homebrew + portaudio ───────────────────────────────
step "Homebrew / portaudio 확인 중..."
if command -v brew &>/dev/null; then
    ok "Homebrew 이미 설치됨"
    if brew list portaudio &>/dev/null 2>&1; then
        ok "portaudio 이미 설치됨"
    else
        info "portaudio 설치 중..."
        brew install portaudio -q && ok "portaudio 설치 완료" || warn "portaudio 설치 실패 (sounddevice가 작동 안 할 수 있음)"
    fi
else
    warn "Homebrew 없음 — portaudio 없이 진행 (마이크 오류 시 https://brew.sh 설치 후 재시도)"
fi

# ── 3. pip 패키지 설치 ────────────────────────────────────
step "필수 패키지 설치 중... (수 분 소요될 수 있습니다)"

PACKAGES="flask sounddevice numpy send2trash faster-whisper resemblyzer noisereduce cryptography"

# webrtcvad 선택 설치 (C 확장, 실패해도 계속 진행)
printf "  📦  %-20s" "webrtcvad (선택)..."
if $PYTHON -m pip install webrtcvad -q 2>/dev/null; then
    echo "✅"
else
    echo "ℹ️  (없어도 동작함, 정밀 발화 감지 비활성)"
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

# ── 4. Whisper large-v3 모델 다운로드 ────────────────────
step "Whisper 음성인식 모델 다운로드 중..."
echo ""
echo "  📥  large-v3-turbo 모델 (~1.5GB) 다운로드 중..."
echo "  ⏳  인터넷 속도에 따라 3~10분 소요될 수 있습니다."
echo ""

$PYTHON -c "
from faster_whisper import WhisperModel
import sys
print('  모델 다운로드 시작...')
try:
    WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')
    print('  ✅  모델 다운로드 완료')
except Exception as e:
    print(f'  ⚠️  모델 다운로드 실패: {e}')
    print('  ℹ️  첫 회의 시작 시 자동으로 다시 다운로드됩니다.')
"

# ── 5. 실행 파일 권한 설정 ────────────────────────────────
step "실행 파일 권한 설정 중..."
chmod +x "$DIR/install.command" 2>/dev/null && ok "install.command"
chmod +x "$DIR/start.command" 2>/dev/null && ok "start.command"
chmod +x "$DIR/setup.sh" 2>/dev/null && ok "setup.sh"

# ── 6. 폴더 생성 ──────────────────────────────────────────
mkdir -p "$DIR/meetings" "$DIR/voices" "$DIR/static"
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
