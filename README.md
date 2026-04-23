# 🎙️ AI 회의 어시스턴트

실시간 음성 인식 → 화자 구분 → 대화형 회의록 자동 저장 → AI 자동 분석

> Docker 이미지 하나로 Mac, Windows, Linux 어디서나 동일하게 실행

---

## 🚀 시작하기 (Docker)

### macOS

```
1. install.command 더블클릭 (최초 1회)
2. start.command 더블클릭
→ 브라우저 자동으로 http://localhost:5555 열림
```

### Windows

```
1. install_windows.bat 더블클릭 (최초 1회)
2. start_windows.bat 더블클릭
→ 브라우저 자동으로 http://localhost:5555 열림
```

> **Windows 주의**: Python 설치 시 "Add Python to PATH" 옵션 필수 체크

### AI 백엔드

| 우선순위 | 조건 | 동작 |
|---------|------|------|
| 1순위 | Claude Code CLI 설치됨 | Claude 사용 (최고 품질) |
| 2순위 | Ollama 설치됨 | Ollama 사용 (오프라인, 무료) |
| 3순위 | 둘 다 없음 | 회의 기록만 동작 (AI 기능 비활성) |

install 시 Claude/Ollama가 없으면 Ollama 설치를 안내합니다.

---

## 📦 파일 구조

```
meeting_assistant/
├── install.command        ← Mac 설치 (더블클릭, 최초 1회)
├── install_windows.bat    ← Windows 설치 (더블클릭, 최초 1회)
├── start.command          ← Mac 실행 (더블클릭)
├── start_windows.bat      ← Windows 실행 (더블클릭)
├── meeting.py             ← 엔진 (음성인식, 화자구분, 회의록)
├── server.py              ← 웹 서버
├── search.py              ← 회의록 검색 CLI
├── static/index.html      ← 웹 UI
├── guide.html             ← 전체 기능 소개 가이드
├── CLAUDE.md              ← Claude Code 연동 컨텍스트
├── meetings/              ← 회의록 저장 (자동 생성)
├── voices/                ← 목소리 프로파일 (자동 생성)
├── glossary.json          ← 회사 용어집 (자동 추출)
└── vocab_*.json           ← 누적 도메인 어휘 (자동 생성)
```

---

## 🔒 HTTPS로 접속하려면

모바일 접속이나 HTTPS가 필요한 경우:
```bash
HTTPS=1 python3 server.py
```
최초 실행 시 인증서가 자동 생성됩니다. 브라우저 경고 → "고급 → 계속 진행" 클릭.

---

## 🎛️ 회의 모드

### 모드 1 — 목소리 사전 등록

참여자가 예시 문장을 읽으면 목소리 등록 → 회의 중 자동 식별

### 모드 2 — 자동 식별

`voices/` 폴더의 기존 프로파일로 자동 식별. 미등록 화자는 회의 후 등록 가능.

---

## 🎛️ 회의 설정

### 언어

| 언어 | 설명 |
|------|------|
| 🇰🇷 한국어 | 한국어 STT 교정, 자동개입, 요약 |
| 🇯🇵 日本語 | 일본어 대응 |
| 🇺🇸 English | 영어 대응 |
| 🌐 자동 감지 | 발화별 언어 자동 감지 |

### 템플릿

| 템플릿 | 요약 형식 |
|--------|----------|
| 💼 일반 회의 | 요약 / 결정사항 / 미결사항 / 액션아이템 |
| 🔄 데일리 스크럼 | 팀원별 현황 / 블로커 / 액션아이템 |
| 🤝 1on1 | 논의 / 피드백 / 목표 / 액션아이템 |
| 🏗️ 기획 회의 | 배경 / 결정사항 / 요구사항 / 일정 / 리스크 |
| 📅 주간 회의 | 성과 / 진행 / 이슈 / 다음 주 계획 |

### 인식 정확도

| 모드 | 청크 크기 | 특징 |
|------|---------|------|
| ⚡ 빠름 | 5초 | 짧은 발화 빠른 표시 |
| ⚖️ 균형 | 8초 | 기본값, 일반 회의 적합 |
| 🎯 정확 | 15초 | 긴 문장 정확도 높음 |

---

## 🌐 웹 UI 기능

| 기능 | 설명 |
|------|------|
| 실시간 대화 표시 | 화자 구분하여 즉시 표시 |
| STT 교정 | AI가 음성인식 오류 자동 수정 (✏️ 표시) |
| 발화 수동 편집 | 더블클릭으로 직접 수정, MD 반영 |
| 📝 텍스트 메모 | 음성 없이 메모 추가 |
| Claude/Ollama 요청 | 텍스트 입력으로 AI에게 질문 |
| 회의 종료 + 요약 | AI 자동 요약/결정사항/액션아이템 생성 |
| 📋 액션 아이템 대시보드 | 담당자별 집계 + 상태 관리 |
| 📅 Google Calendar 등록 | 다음 회의 자동 등록 |
| 회의록 뷰어 + 검색 | 키워드/발화자/날짜 필터 |
| 📖 용어집 | 회사 고유 용어 자동 추출 및 관리 |
| 🔧 시스템 상태 | 의존성 설치 상태 확인 |
| 비정상 종료 복구 | 크래시 후 미완료 회의 AI 요약 사후 생성 |

---

## 🔄 자동 기능

| 기능 | 주기 |
|------|------|
| STT 교정 | 매 발화 |
| 오디오 정규화 + 소음 제거 | 매 청크 |
| 발화 경계 감지 (Silero VAD) | 매 프레임 |
| 이전 회의 브리핑 | 회의 시작 시 |
| 주제 자동 분류 | 90초 |
| 자동 개입 (담당자/마감/요약) | 패턴 감지 |
| 품질 피드백 | 10분 |
| AI 회의 분석 + 제목 생성 | 종료 시 |
| 도메인 어휘 누적 | 종료 시 |
| 용어집 자동 추출 | 종료 시 |

---

## 🟣 Obsidian 연동

회의록이 Obsidian Dataview 호환 YAML frontmatter로 저장됩니다.
설정(⚙️)에서 Vault 경로 지정 시 자동 복사.

---

## 📁 데이터 영구 보존

Docker 볼륨으로 마운트된 파일은 컨테이너를 삭제해도 보존됩니다:

| 경로 | 내용 |
|------|------|
| `meetings/` | 회의록 MD 파일 |
| `voices/` | 목소리 프로파일 (.npy) |
| `glossary.json` | 회사 용어집 |
| `vocab_*.json` | 누적 도메인 어휘 (언어별) |

---

## ⚙️ 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `OLLAMA_MODEL` | `exaone3.5:7.8b-instruct-q4_K_M` | Ollama 모델 |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 서버 주소 |
| `HF_TOKEN` | — | HuggingFace 토큰 (화자 임베딩 정확도 향상) |
| `HTTPS` | `0` | `1` 설정 시 HTTPS 활성화 |

---

## 🛠️ 문제 해결

**마이크 권한 오류 (macOS)**
```
시스템 설정 → 개인정보 보호 → 마이크 → 터미널 체크
```

**Ollama 모델 변경**
```bash
OLLAMA_MODEL=qwen2.5:7b python3 server.py
```

**포트 충돌**
```bash
lsof -i :5555    # Mac
netstat -ano | findstr :5555   # Windows
```

**Claude Code CLI 설치**
```bash
npm install -g @anthropic-ai/claude-code
```

**Ollama 설치**
```
https://ollama.com 에서 다운로드 후 설치
ollama pull exaone3.5:7.8b-instruct-q4_K_M
```

