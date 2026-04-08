# 🎙️ AI 회의 어시스턴트

실시간 음성 인식 → 대화형 회의록 자동 저장 → Claude AI 자동 분석

> 📄 **전체 기능 소개 및 설치 가이드**: 프로젝트 폴더의 **`guide.html`** 파일을 브라우저로 열어 확인하세요.

---

## 📦 파일 구성

| 파일 | 역할 |
|------|------|
| `install.command` | **macOS 설치** — 더블클릭 1회 실행 |
| `start.command` | **macOS 실행** — 더블클릭으로 바로 시작 |
| `install_windows.bat` | **Windows 설치** — 더블클릭 1회 실행 |
| `start_windows.bat` | **Windows 실행** — 더블클릭으로 바로 시작 |
| `meeting.py` | 엔진 (음성인식, 화자구분, 회의록 저장) |
| `server.py` | 웹 서버 (meeting.py 엔진 사용) |
| `search.py` | 회의록 키워드 검색 CLI |
| `static/index.html` | 웹 UI |
| `CLAUDE.md` | Claude Code 연동 컨텍스트 |
| `meetings/` | 회의록 저장 폴더 (자동 생성) |
| `voices/` | 목소리 프로파일 저장 (자동 생성) |
| `cert.pem` / `key.pem` | HTTPS 인증서 (최초 실행 시 자동 생성) |
| `vocab_ko.json` | 누적 도메인 어휘 (한국어, 자동 생성) |
| `vocab_ja.json` | 누적 도메인 어휘 (일본어, 자동 생성) |
| `vocab_en.json` | 누적 도메인 어휘 (영어, 자동 생성) |
| `glossary.json` | 회사 용어집 (자동 추출 + 수동 관리) |
| `settings.json` | 설정 저장 (Obsidian vault, Whisper 모델 등) |

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

> **Windows 주의**: 설치 중 "Add Python to PATH" 옵션 필수 체크

### 🔒 HTTPS로 접속하려면

기본 실행은 HTTP입니다. 모바일 접속이나 HTTPS가 필요한 경우 아래 방법을 사용하세요.

**방법 1 — 터미널에서 직접 실행**
```bash
HTTPS=1 python3 server.py
```

**방법 2 — 항상 HTTPS로 실행하도록 start.command 수정**  
`start.command` 파일을 텍스트 편집기로 열고 마지막 줄을 아래와 같이 변경합니다.
```bash
# 변경 전
python3 server.py

# 변경 후
HTTPS=1 python3 server.py
```

최초 실행 시 `cert.pem` / `key.pem` 인증서가 자동 생성됩니다.  
브라우저 경고 화면에서 **"고급 → 계속 진행"** 을 한 번 클릭하면 이후 정상 접속됩니다.

---

## 🎛️ 회의 설정

### 마이크 선택 + 레벨 미터

설정 패널에서 사용할 마이크를 선택할 수 있습니다. 선택하면 실시간 입력 레벨 바가 표시되어 마이크 작동 여부를 바로 확인할 수 있습니다.

### 언어 선택

| 언어 | 설명 |
|------|------|
| 🇰🇷 한국어 | 한국어 STT 교정, 자동개입, 요약 |
| 🇯🇵 日本語 | 일본어 STT 교정, 자동개입, 요약 |
| 🇺🇸 English | 영어 STT 교정, 자동개입, 요약 |
| 🌐 자동 감지 | Whisper가 발화별 언어 자동 감지, Claude도 동일 언어로 응답 |

선택한 언어는 자동으로 기억됩니다 (localStorage).

### 회의 템플릿

| 템플릿 | 최적화된 요약 형식 |
|--------|------------------|
| 💼 일반 회의 | 요약 / 결정사항 / 미결사항 / 액션아이템 |
| 🔄 데일리 스크럼 | 팀원별 현황 / 블로커 / 즉시 액션아이템 |
| 🤝 1on1 | 주요 논의 / 성장 피드백 / 목표 / 액션아이템 |
| 🏗️ 기획 회의 | 배경 / 결정사항 / 요구사항 / 일정 / 리스크 |
| 📅 주간 회의 | 주간 성과 / 완료 / 진행 중 / 이슈 / 다음 주 계획 |

선택한 템플릿은 자동으로 기억됩니다 (localStorage).

### 인식 정확도 설정

| 모드 | 청크 크기 | 특징 |
|------|---------|------|
| ⚡ 빠름 | 5초 | 짧은 발화 빠른 표시, 소음에 민감 |
| ⚖️ 균형 | 8초 | 기본값, 일반 회의에 적합 |
| 🎯 정확 | 15초 | 긴 문장 정확도 높음, 15초 딜레이 |

> webrtcvad 설치 시 발화가 끝나는 순간 즉시 전사합니다 (청크 크기와 무관).

---

## 🎛️ 회의 모드

### 모드 1 — 목소리 사전 등록

그 자리에서 예시 문장을 읽으면 목소리가 등록되고, 회의 중 자동으로 누가 말했는지 식별합니다.

```
참여자 이름 입력 → 예시 문장 읽기 (5초) → 등록 완료
모든 참여자 등록 후 → 회의 시작
```

### 모드 2 — 저장된 프로파일 자동 식별

이전에 등록된 목소리 파일(`voices/`)을 자동으로 불러와 식별합니다. 처음 만나는 목소리는 `미등록1`, `미등록2`로 표시되고, 회의 종료 후 이름을 등록할 수 있습니다.

---

## 🌐 웹 UI 기능

| 기능 | 설명 |
|------|------|
| 실시간 대화 표시 | 말하는 순간 화자 구분하여 표시 |
| 실시간 부분 전사 | 3초마다 현재 발화 미리 표시 (흐릿하게) |
| STT 교정 표시 | 자동 교정된 발화에 ✏️ 표시 (원문 교체) |
| 발화 수동 편집 | 발화 더블클릭 → 직접 텍스트 수정 → MD 파일 반영 |
| 📝 텍스트 메모 | 회의 중 타이핑으로 메모 추가 (음성 없이) |
| 🌙 다크/라이트 모드 | 헤더 버튼으로 즉시 전환, 설정 자동 저장 |
| Claude 스피너 | Claude 응답 처리 중 헤더에 스피너 표시 |
| ⏸ 일시정지 / ▶️ 재개 | 회의 중 녹음 일시 중단 |
| Claude 요청 | 텍스트 입력으로 Claude에게 질문 |
| 회의 종료 + 요약 | AI가 자동으로 요약/결정사항/액션아이템 생성 |
| 자동 제목 생성 | 종료 시 Claude가 회의 내용 기반 제목 자동 생성 |
| 이전 회의 브리핑 | 회의 시작 시 이전 미결 사항 자동 표시 |
| 📋 액션 아이템 대시보드 | 전체 회의록의 액션아이템 담당자별 집계 |
| 📅 Google Calendar 등록 | 다음 회의를 캘린더에 자동 등록 |
| 회의록 뷰어 | 마크다운 렌더링으로 이전 회의 확인 |
| 회의록 검색 | 키워드 / 발화자 / 날짜 필터 |
| 요약 모달 | 이전 회의 AI 분석 팝업으로 확인 |
| 목소리 프로파일 관리 | 등록/삭제/샘플 수 확인 |
| 회의록 삭제 | 휴지통으로 안전하게 이동 |
| 드래그 분할선 | 대화창과 회의록 뷰어 너비 조절 |
| 📖 용어집 | 회사 고유 용어 자동 추출 및 관리 |
| 🔧 시스템 상태 | 의존성 패키지 설치 여부 한눈에 확인 |
| 비정상 종료 복구 | 크래시 후 미완료 회의 AI 요약 사후 생성 |
| 모바일 지원 | 반응형 UI, HTTPS로 모바일 마이크 사용 가능 |

---

## 🔄 자동 기능

| 기능 | 주기 | 설명 |
|------|------|------|
| STT 교정 | 매 발화 | Claude가 음성인식 오류 자동 수정 |
| 오디오 정규화 | 매 청크 | RMS 기반 볼륨 편차 자동 보정 |
| 소음 제거 | 매 청크 | noisereduce로 배경 소음 필터링 (에어컨/환풍기 등) |
| 발화 경계 감지 | 매 프레임 | webrtcvad로 발화 끝 즉시 감지 후 전사 |
| 이전 회의 브리핑 | 회의 시작 시 | 미결 사항 & 팔로업 필요 액션아이템 표시 |
| 주제 자동 분류 | 90초 | 주제 변경 시 MD에 구간 헤더 삽입 |
| 자동 개입 | 패턴 감지 | 담당자/마감/요약 등 자동 답변 (30초 쿨다운) |
| 품질 피드백 | 10분 | 시간 배분/주의사항/제안 |
| AI 회의 분석 | 종료 시 | 요약/결정/미결/액션아이템 자동 생성 |
| 자동 제목 생성 | 종료 시 | Claude가 15자 이내 회의 제목 자동 생성 |
| 도메인 어휘 누적 | 종료 시 | 회의 중 등장한 전문 용어를 파일에 축적하여 인식률 향상 |
| 용어집 자동 추출 | 종료 시 | Claude가 팀/프로젝트 고유 용어를 감지해 glossary.json에 추가 |
| YAML frontmatter | 회의 시작 시 | Obsidian Dataview 호환 메타데이터 자동 생성 |

---

## 🟣 Obsidian 연동

회의록이 Obsidian 친화적 형식으로 저장됩니다.

### MD 파일 구조

```yaml
---
tags:
  - meeting
date: 2026-04-06
time: "14:30"
participants: ["Jerry", "민수"]
template: general
language: ko
status: in-progress   ← 회의 종료 시 complete로 자동 변경
title: "API 성능 개선 논의"   ← 자동 생성
---
```

- **Dataview 쿼리** 및 **Properties 패널** 에서 바로 사용 가능
- Claude 응답: `> [!NOTE]` callout 형식
- 품질 피드백: `> [!TIP]+` callout 형식

### Vault 자동 복사

설정(⚙️)에서 Obsidian Vault 경로를 지정하면 회의 종료 시 vault에 자동 복사됩니다.

---

## 📋 액션 아이템 대시보드

헤더의 **📋 액션 아이템** 버튼을 클릭하면 최근 30개 회의록에서 AI가 생성한 모든 액션 아이템을 담당자별로 집계합니다. 각 항목의 상태(미완료 ○ / 진행 중 ◑ / 완료 ✓)를 클릭하여 변경할 수 있습니다.

---

## 📖 회사 용어집

헤더의 **📖 용어집** 버튼으로 관리합니다.

- **자동 추출**: 회의 종료 시 Claude가 팀/프로젝트 고유 용어를 감지해 자동 추가
- **수동 추가/삭제**: UI에서 용어와 설명을 직접 입력
- **Whisper 연동**: 등록된 용어가 음성 인식 힌트로 자동 포함되어 인식 정확도 향상
- **저장 위치**: `glossary.json`

---

## 🔧 시스템 상태 패널

헤더의 **🔧 시스템** 버튼으로 의존성 패키지 설치 상태를 확인합니다.

| 항목 | 역할 |
|------|------|
| Claude CLI | AI 교정/요약/개입 핵심 기능 |
| faster-whisper | 음성 인식 엔진 |
| resemblyzer | 화자 식별 |
| noisereduce | 배경 소음 제거 |
| webrtcvad | 정밀 발화 경계 감지 (선택) |
| sounddevice / PortAudio | 마이크 입력 |

---

## 🤖 Whisper 모델 선택

설정(⚙️)에서 Whisper 모델을 변경할 수 있습니다. 변경 후 서버 재시작이 필요합니다.

| 모델 | 정확도 | 속도 |
|------|--------|------|
| `large-v3` | 최고 (기본값) | 보통 |
| `large-v3-turbo` | large-v3와 유사 | 2배 빠름 |
| `medium` | 보통 | 빠름 |
| `small` | 낮음 | 매우 빠름 |

실행 환경에 따라 자동으로 최적 디바이스를 선택합니다:

| 환경 | 가속 |
|------|------|
| Apple Silicon (M1/M2/M3) | CPU ARM 최적화 |
| NVIDIA GPU (Windows/Linux) | CUDA float16 |
| Intel / AMD CPU | int8 CPU |

---

## 🎤 목소리 프로파일

목소리를 등록하면 `voices/{이름}/` 폴더에 저장됩니다. 회의를 거듭할수록 샘플이 누적되어 인식 정확도가 높아집니다.

```
voices/
├── Jerry/
│   ├── 20260401_143012_123456.npy
│   └── 20260408_091523_654321.npy   ← 회의마다 자동 누적
└── 민수/
    └── 20260401_143015_333444.npy
```

최대 100개 샘플 보관 (초과 시 오래된 것 자동 삭제)

---

## 🔍 회의록 검색 (터미널 고급)

```bash
python3 search.py 백엔드              # 키워드 검색
python3 search.py API --speaker Jerry  # 특정 발화자 필터
python3 search.py --date 2026-04       # 날짜 필터
python3 search.py --list               # 전체 회의 목록
python3 search.py --summary            # 최근 5개 요약 보기
```

---

## ⚙️ 주요 설정값 (`meeting.py` 상단)

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `WHISPER_MODEL` | `large-v3-turbo` | 음성인식 모델 (UI 설정에서도 변경 가능) |
| `CHUNK_SECONDS` | `8` | 기본 청크 크기 (UI에서 변경 가능) |
| `ENROLL_SECONDS` | `5` | 목소리 등록 녹음 시간 |
| `MAX_VOICE_SAMPLES` | `100` | 인당 최대 목소리 샘플 수 |
| `IDENTIFY_THRESH` | `0.78` | 목소리 매칭 임계값 (높을수록 엄격) |
| `VOCAB_MIN_CNT` | `5` | 이 횟수 이상 등장한 단어를 Whisper 힌트에 포함 |

---

## 🛠️ 문제 해결

**브라우저 HTTPS 경고**
```
"연결이 안전하지 않습니다" 화면에서
→ 고급 → localhost(안전하지 않음)로 계속 이동 클릭
(자체 서명 인증서이므로 정상)
```

**HTTPS 인증서 재생성**
```bash
rm cert.pem key.pem
# 서버 재시작 시 자동 재생성
```

**cryptography 미설치 시 HTTP로 실행됨**
```bash
pip3 install cryptography   # macOS/Linux
pip install cryptography    # Windows
```

**마이크 권한 오류 (macOS)**
```
시스템 설정 → 개인정보 보호 → 마이크 → 터미널 체크
```

**마이크 권한 오류 (Windows)**
```
설정 → 개인 정보 → 마이크 → 앱 마이크 액세스 허용
```

**모바일에서 마이크가 안 될 때**
```
HTTPS가 필요합니다.
1. cryptography 설치 후 서버 재시작
2. 브라우저에서 https://[PC IP]:5555 접속
3. 인증서 경고 → 계속 진행
```

**브라우저가 안 열릴 때**
```
직접 접속: https://localhost:5555
```

**모델 다운로드 실패**
```bash
python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')"
```

**발화자 구분 안 됨 (resemblyzer 없음)**
```bash
pip3 install resemblyzer  # macOS/Linux
pip install resemblyzer   # Windows
```

**발화 감지 정확도 낮음 (webrtcvad 없음)**
```bash
pip3 install webrtcvad   # macOS/Linux
pip install webrtcvad    # Windows
```

**소음 제거 안 됨 (noisereduce 없음)**
```bash
pip3 install noisereduce  # macOS/Linux
pip install noisereduce   # Windows
```

**포트 충돌**
```bash
# macOS/Linux
lsof -i :5555
kill -9 [PID]

# Windows
netstat -ano | findstr :5555
taskkill /PID [PID] /F
```

**Windows PortAudio 오류**
```
pip install pipwin
pipwin install pyaudio
```

**Claude CLI 미설치**
```bash
# macOS (Homebrew)
brew install claude
# 또는
npm install -g @anthropic-ai/claude-code
```
