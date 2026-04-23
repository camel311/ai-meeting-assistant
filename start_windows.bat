@echo off
chcp 65001 > nul
title AI 회의 어시스턴트
cd /d "%~dp0"

echo.
echo  🎙️  AI 회의 어시스턴트 시작 중...
echo.

:: ── 가상환경 확인 ───────────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo  ❌  가상환경이 없습니다.
    echo      install_windows.bat 를 먼저 실행해주세요.
    echo.
    pause
    exit /b 1
)
set PYTHON=.venv\Scripts\python.exe

:: ── 필수 패키지 확인 ────────────────────────────────────
%PYTHON% -c "import flask, faster_whisper, sounddevice, numpy" > nul 2>&1
if errorlevel 1 (
    echo  ❌  설치되지 않은 패키지가 있습니다.
    echo      install_windows.bat 를 다시 실행해주세요.
    echo.
    pause
    exit /b 1
)

:: ── server.py 확인 ──────────────────────────────────────
if not exist "server.py" (
    echo  ❌  server.py 를 찾을 수 없습니다.
    pause
    exit /b 1
)

echo  ✅  준비 완료
echo.
echo  ┌─────────────────────────────────────────┐
echo  │  🌐  http://localhost:5555             │
echo  │                                         │
echo  │  브라우저가 자동으로 열립니다.           │
echo  │  종료하려면 이 창을 닫거나 Ctrl+C       │
echo  └─────────────────────────────────────────┘
echo.

%PYTHON% server.py

echo.
echo  서버가 종료됐습니다.
pause
