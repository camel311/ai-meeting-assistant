# 🎙️ AI 회의 어시스턴트

실시간 음성 인식 → 화자 구분 → 대화형 회의록 자동 저장 → AI 자동 분석

---

## 🚀 시작하기

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
├── meetings/              ← 회의록 MD + MP3 녹음 (자동 생성)
├── voices/                ← 목소리 프로파일 (자동 생성)
├── glossary.json          ← 회사 용어집 (자동 추출)
├── corrections.json       ← 사용자 교정 학습 (자동 생성)
└── vocab_*.json           ← 누적 도메인 어휘 (자동 생성)
```

---

## 🔊 음성 엔진

### STT (Speech-to-Text)

| 항목 | 사양 |
|------|------|
| 엔진 | Whisper large-v3-turbo |
| GPU 가속 | MLX (Apple Silicon), CUDA (NVIDIA) |
| Word-level 타임스탬프 | 청크 내 화자 교체 감지 |
| VAD | Silero VAD (딥러닝 발화 감지) |
| 소음 제거 | noisereduce (배경 소음 필터링) |
| 도메인 어휘 | 회의마다 자동 누적 → Whisper 힌트에 반영 |

### 화자 분리

| 항목 | 사양 |
|------|------|
| 임베딩 모델 | ECAPA-TDNN (pyannote/embedding) |
| 화자 변경 감지 | pyannote/segmentation-3.0 |
| 겹침 발화 분리 | ConvTasNet (asteroid) |
| 실시간 클러스터링 | 코사인 유사도 기반 |
| 회의 후 재매칭 | 전체 오디오로 화자 재판정 + MD 자동 교정 |

### 회의 종료 후 자동 처리

```
회의 종료
  ├─ 1. 🎵 회의 녹음 MP3 저장
  ├─ 2. 🔄 화자 재매칭 (전체 오디오 분석)
  ├─ 3. 📝 연속 같은 화자 발화 병합
  ├─ 4. ✏️ 전체 대화 AI 교정 (맥락 기반)
  ├─ 5. 📝 AI 회의 분석 (요약/액션아이템)
  └─ 6. 📊 vocab 누적 + 용어집 추출
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

참여자가 예시 문장(10초)을 읽으면 목소리 등록 → 회의 중 자동 식별

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
| 사용자 교정 학습 | 수동 교정 시 corrections.json에 저장 → 다음 회의 STT 반영 |
| 발화 수동 편집 | 더블클릭으로 직접 수정, MD 반영 |
| 📝 텍스트 메모 | 음성 없이 메모 추가 |
| Claude/Ollama 요청 | 텍스트 입력으로 AI에게 질문 |
| 회의 종료 + 요약 | AI 자동 요약/결정사항/액션아이템 생성 |
| 📋 액션 아이템 대시보드 | 담당자별 집계 + 상태 관리 |
| 📅 Google Calendar 등록 | 다음 회의 자동 등록 |
| 💬 Slack 요약 전송 | 회의 요약을 Slack 채널로 전송 (Block Kit 포맷) |
| 🎵 회의 녹음 재생 | MP3 재생 + 대화 하이라이트 + 0.5x~2x 속도 조절 |
| 📱 QR 코드 참석자 접속 | 회의 시작 시 QR 표시 → 스캔하여 읽기 전용 접속 |
| 🤖 AI 회의록 검색 | 자연어 질문으로 이전 회의 검색 |
| 회의록 뷰어 + 검색 | 키워드/발화자/날짜 필터 |
| 📖 용어집 | 회사 고유 용어 자동 추출 및 관리 |
| 🔧 시스템 상태 | 의존성 설치 상태 + LLM 뱃지 (Claude/Ollama) |
| 비정상 종료 복구 | 크래시 후 미완료 회의 AI 요약 사후 생성 |

---

## 🔄 자동 기능

| 기능 | 주기 |
|------|------|
| STT 교정 (Haiku) | 매 발화 |
| 오디오 정규화 + 소음 제거 | 매 청크 |
| 발화 경계 감지 (Silero VAD) | 매 프레임 |
| 겹침 발화 분리 (ConvTasNet) | 겹침 감지 시 |
| Word-level 화자 교체 감지 | 매 청크 |
| 이전 회의 브리핑 | 회의 시작 시 |
| 주제 자동 분류 | 90초 |
| 자동 개입 (담당자/마감/요약) | 패턴 감지 |
| 품질 피드백 | 10분 |
| 회의 녹음 MP3 저장 | 종료 시 |
| 화자 재매칭 (전체 오디오) | 종료 시 |
| 연속 발화 병합 | 종료 시 |
| 전체 대화 AI 교정 | 종료 시 |
| AI 회의 분석 + 제목 생성 (Sonnet) | 종료 시 |
| 도메인 어휘 누적 | 종료 시 |
| 용어집 자동 추출 | 종료 시 |

---

## 💬 Slack 연동

1. ⚙️ 설정 → Slack 연동 → Webhook URL 입력 → 저장
2. 회의 요약 모달에서 **💬 Slack** 버튼 클릭

Webhook URL 생성: https://api.slack.com/apps → Incoming Webhooks → 채널 선택

---

## 🟣 Obsidian 연동

회의록이 Obsidian Dataview 호환 YAML frontmatter로 저장됩니다.
설정(⚙️)에서 Vault 경로 지정 시 자동 복사.

---

## ⚙️ 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `OLLAMA_MODEL` | `exaone3.5:7.8b-instruct-q4_K_M` | Ollama 모델 (한국어 최적화) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 서버 주소 |
| `HF_TOKEN` | — | HuggingFace 토큰 (ECAPA-TDNN + segmentation 사용) |
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

**HuggingFace 모델 접근 승인**
ECAPA-TDNN 및 segmentation 모델 사용 시:
1. https://huggingface.co/pyannote/embedding → 접근 요청
2. https://huggingface.co/pyannote/segmentation-3.0 → 접근 요청
3. `.env` 파일에 `HF_TOKEN=hf_xxxxx` 설정
