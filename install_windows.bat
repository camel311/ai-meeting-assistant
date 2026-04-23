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

:: ── 2. 가상환경 생성 ──────────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  가상환경 설정 중...
echo  ──────────────────────────────────────────
if exist ".venv\Scripts\python.exe" (
    echo  ✅  가상환경 이미 존재 (.venv\^)
) else (
    echo  📦  가상환경 생성 중...
    python -m venv .venv
    echo  ✅  가상환경 생성 완료
)
set PYTHON=.venv\Scripts\python.exe

%PYTHON% -m pip install --upgrade pip -q

:: ── 3. 필수 패키지 설치 ─────────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  필수 패키지 설치 중... (수 분 소요)
echo  ──────────────────────────────────────────
echo.

set PACKAGES=flask sounddevice numpy send2trash faster-whisper resemblyzer noisereduce webrtcvad cryptography torch pyannote.audio asteroid omegaconf

for %%p in (%PACKAGES%) do (
    echo  📦  %%p 설치 중...
    %PYTHON% -m pip install %%p -q
    if errorlevel 1 (
        echo  ⚠️  %%p 설치 실패 (일부 기능 제한될 수 있음^)
    ) else (
        echo  ✅  %%p
    )
)

:: ── 3.5. ffmpeg 확인 (회의 녹음 MP3 변환용) ──────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  ffmpeg 확인 중...
echo  ──────────────────────────────────────────
where ffmpeg > nul 2>&1
if not errorlevel 1 (
    echo  ✅  ffmpeg 이미 설치됨
) else (
    echo  📦  ffmpeg 설치 중 (winget)...
    winget install ffmpeg -e --silent > nul 2>&1
    if not errorlevel 1 (
        echo  ✅  ffmpeg 설치 완료
    ) else (
        echo  ⚠️  ffmpeg 자동 설치 실패
        echo  ℹ️  수동 설치: https://ffmpeg.org/download.html
        echo  ℹ️  없어도 WAV로 녹음 저장됩니다 (용량이 더 큼^)
    )
)

:: ── 4. Whisper 모델 다운로드 ────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  Whisper 음성인식 모델 다운로드 중...
echo  ──────────────────────────────────────────
echo.
echo  📥  large-v3-turbo 모델 (~1.5GB) 다운로드 중...
echo  ⏳  인터넷 속도에 따라 3~10분 소요될 수 있습니다.
echo.

%PYTHON% -c "from faster_whisper import WhisperModel; print('  모델 다운로드 시작...'); WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('  ✅  모델 다운로드 완료')"

:: ── 5. LLM 백엔드 확인 ─────────────────────────────────
echo.
echo  ──────────────────────────────────────────
echo  🔧  LLM 백엔드 확인 중...
echo  ──────────────────────────────────────────

where claude > nul 2>&1
if not errorlevel 1 (
    echo  ✅  Claude Code CLI 감지됨 → AI 기능 Claude로 동작
    goto :llm_done
)

where ollama > nul 2>&1
if not errorlevel 1 (
    echo  ✅  Ollama 감지됨 → AI 기능 Ollama로 동작
    set OLLAMA_TARGET=exaone3.5:7.8b-instruct-q4_K_M
    echo  📥  한국어 최적화 모델 다운로드 중: %OLLAMA_TARGET% (~5GB^)
    ollama pull %OLLAMA_TARGET%
    goto :llm_done
)

echo  ⚠️  Claude CLI, Ollama 모두 없음
echo  ℹ️  Ollama 설치를 권장합니다: https://ollama.com
echo  ℹ️  설치 후 install_windows.bat를 다시 실행해주세요.

:llm_done

:: ── 6. 폴더 생성 ────────────────────────────────────────
if not exist "meetings" mkdir meetings
if not exist "voices" mkdir voices
if not exist "static" mkdir static
if not exist "glossary.json" echo {} > glossary.json
if not exist "vocab_ko.json" echo {} > vocab_ko.json
if not exist "vocab_en.json" echo {} > vocab_en.json
if not exist "vocab_ja.json" echo {} > vocab_ja.json
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
