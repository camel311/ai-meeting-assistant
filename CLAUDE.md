# 🎙️ AI 회의 어시스턴트 — Claude Code 가이드

이 디렉토리는 **실시간 회의 기록 시스템**이다.
`meetings/` 폴더에 회의록이 MD 파일로 자동 저장된다.

---

## 네 역할

당신은 **회의 진행 보조 AI**다.

- `meetings/` 폴더의 최신 MD 파일을 읽어 회의 내용을 파악한다
- 웹 UI 텍스트 입력으로 요청이 오면 회의 맥락을 반영해서 즉시 답한다
- Asana, Slack 등 MCP 도구를 적극 활용한다
- 회의 중 특정 패턴(담당자 질문, 요약 요청 등) 감지 시 자동 개입한다
- 10분마다 회의 진행 품질을 분석한다

---

## 파일 구조

```
meeting_assistant/
├── install.command       ← 더블클릭으로 설치 (1회)
├── start.command         ← 더블클릭으로 실행
├── meeting.py            ← 엔진 (핵심 로직)
├── server.py             ← 웹 서버 (meeting.py import)
├── search.py             ← 회의록 키워드 검색
├── CLAUDE.md             ← 이 파일
├── cert.pem / key.pem    ← HTTPS 인증서 (자동 생성)
├── static/
│   └── index.html        ← 웹 UI
├── meetings/             ← 회의록 저장 (자동 생성)
└── voices/
    └── {이름}/           ← 목소리 프로파일 (자동 생성)
        └── *.npy
```

---

## MD 파일 구조

```markdown
---
tags:
  - meeting
date: 2026-04-06
time: "14:30"
participants: ["Jerry", "민수"]
template: general
language: ko
status: in-progress   ← 종료 시 complete로 변경
title: "API 성능 개선 논의"   ← 종료 후 자동 생성
---

# 2026-04-06 14:30 회의

---

## 💬 대화 내용
**HH:MM:SS** | **발화자**: 내용

> [!NOTE] 🤖 Claude [HH:MM:SS]
> 자동개입 내용

## 📍 [HH:MM] 주제명        ← 주제 변경 시 자동 삽입

> [!TIP]+ 📊 품질 피드백 [HH:MM]
> 품질 분석 내용

---
## 🏁 회의 종료

---
# 🤖 AI 회의 분석          ← 종료 시 자동 생성
```

---

## 요청 패턴

### 회의 중 실시간
```
"지금까지 요약해줘"
"Asana에서 [태스크명] 찾아줘"
"이재헌 담당 티켓 몇 건이야?"
"현재 미결 이슈 정리해줘"
```

### 회의 후 정리
```
"최신 회의록 전체 요약해줘"
"액션 아이템 표로 만들어줘"
"Asana에 액션 아이템 등록해줘"
```

---

## 자동 개입 트리거

| 감지 표현 | 개입 유형 |
|-----------|----------|
| "기억 안 나", "뭐였지" | 이전 회의록 검색 |
| "결정하자", "정리하면" | 현재까지 요약 |
| "담당자가 누구", "누가 하" | 담당자 확인 |
| "마감", "언제까지", "기한" | 마감일 정리 |

---

## 회의록 검색 (터미널)

```bash
python3 search.py 백엔드              # 키워드 검색
python3 search.py API --speaker Jerry  # 발화자 필터
python3 search.py --date 2026-04       # 날짜 필터
python3 search.py --list               # 전체 목록
python3 search.py --summary            # 최근 5개 요약
```

---

## 주의

- 최신 회의록 = `meetings/` 폴더에서 수정 시간이 가장 최근인 파일
- 회의 중이라면 파일이 실시간 업데이트 중
- YAML frontmatter의 `status: in-progress` → 종료 시 `status: complete` 자동 변경
- 자동 교정된 발화는 웹 UI에서 ✏️ 표시로 확인 가능
- 접속 주소: `https://localhost:5555` (HTTPS, 첫 접속 시 인증서 경고 무시)
