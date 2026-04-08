@echo off
chcp 65001 > nul
title AI 회의 어시스턴트 - 설치
cd /d "%~dp0"

echo.
echo  ╔═══════════════════════════════════════════╗
echo  ║      🎙️  AI 회의 어시스턴트  설치         ║
echo  ╚═══════════════════════════════════════════╝
echo.
echo  설치가 자동으로 진행됩니다.
echo  완료될 때까지 이 창을 닫지 마세요.
echo.

:: ── 1. Python 확인 ─────────────────────────────────────
echo  ──────────────────────────────────────────
echo  🔧  Python 확인 중...
echo  ──────────────────────────────────────────
python --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo  ❌  Python을 찾을 수 없습니다.
    echo.
    echo  https://www.python.org/downloads/ 에서
    echo  Python 3.9 이상을 설치 후 다시 실행해주세요.
    echo.
    echo  ※ 설치 시 "Add Python to PATH" 체크 필수!
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  ✅  %%v

:: ── 2. pip 업그레이드 ──────────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  pip 업그레이드 중...
echo  ──────────────────────────────────────────
python -m pip install --upgrade pip -q

:: ── 3. 필수 패키지 설치 ─────────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  필수 패키지 설치 중... (수 분 소요)
echo  ──────────────────────────────────────────
echo.

set PACKAGES=flask sounddevice numpy send2trash faster-whisper resemblyzer noisereduce webrtcvad cryptography

for %%p in (%PACKAGES%) do (
    echo  📦  %%p 설치 중...
    python -m pip install %%p -q
    if errorlevel 1 (
        echo  ⚠️  %%p 설치 실패 (일부 기능 제한될 수 있음)
    ) else (
        echo  ✅  %%p
    )
)

:: ── 4. sounddevice 의존성 안내 ──────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  ℹ️   마이크 오류 시 PortAudio 설치 필요:
echo      https://github.com/intxcc/pyaudio_portaudio
echo      또는: pip install pipwin ^&^& pipwin install pyaudio
echo  ──────────────────────────────────────────
echo.

:: ── 5. Whisper 모델 다운로드 ────────────────────────────
echo  🔧  Whisper 음성인식 모델 다운로드 중...
echo  ──────────────────────────────────────────
echo.
echo  📥  large-v3-turbo 모델 (~1.5GB) 다운로드 중...
echo  ⏳  인터넷 속도에 따라 3~10분 소요될 수 있습니다.
echo.

python -c "from faster_whisper import WhisperModel; print('  모델 다운로드 시작...'); WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('  ✅  모델 다운로드 완료')"

:: ── 6. 폴더 생성 ────────────────────────────────────────
if not exist "meetings" mkdir meetings
if not exist "voices" mkdir voices
if not exist "static" mkdir static
echo  ✅  meetings\, voices\, static\ 폴더 준비됨

:: ── 완료 ────────────────────────────────────────────────
echo.
echo  ╔═══════════════════════════════════════════╗
echo  ║            🎉  설치 완료!                 ║
echo  ╠═══════════════════════════════════════════╣
echo  ║                                           ║
echo  ║  이제 start_windows.bat 를 실행하면       ║
echo  ║  회의 어시스턴트가 바로 시작됩니다.       ║
echo  ║                                           ║
echo  ╚═══════════════════════════════════════════╝
echo.
pause
