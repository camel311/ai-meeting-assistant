#!/usr/bin/env python3
"""
🎙️ AI 회의 어시스턴트 — 엔진 (meeting.py)

터미널 직접 실행: python3 meeting.py
웹에서 사용:      server.py 가 import 하여 사용

모드 1: 그 자리에서 예시 문장 읽기 → voices/ 저장 → 회의 시작
모드 2: voices/ 프로파일 자동 식별 + 미매칭은 클러스터링
        (참석자 이름 사전 등록 여부는 선택)
"""

import os, time, queue, threading, subprocess, re, sys, json
try:
    import termios
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False  # Windows — 터미널 모드 에코 제어 비활성
from typing import Optional, List, Dict, Tuple, Callable
from pathlib import Path
from datetime import datetime
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

# ──────────────── 설정 ────────────────────────────────────
SAMPLE_RATE       = 16000
CHUNK_SECONDS     = 8
WHISPER_MODEL     = "large-v3-turbo"  # 기본값: 속도/정확도 균형

def _detect_whisper_backend():
    """플랫폼별 최적 디바이스/compute_type 자동 감지. (device, compute_type, label)"""
    import platform
    # 1. NVIDIA CUDA
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "float16", "CUDA (NVIDIA GPU)"
    except ImportError:
        pass
    # 2. Apple Silicon — mlx-whisper 우선 (GPU 가속, 2~5배 빠름)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # noqa: F401
            return "mlx", "mlx", "MLX (Apple Silicon GPU)"
        except ImportError:
            pass
        return "cpu", "int8", "CPU (Apple Silicon ARM)"
    # 3. Intel / AMD CPU fallback
    return "cpu", "int8", "CPU"
LANGUAGE          = "ko"
SILENCE_THRESH    = 0.003

# 경로: 어디서 실행해도 meeting.py 위치 기준으로 고정
_BASE_DIR         = Path(__file__).parent
OUTPUT_DIR        = _BASE_DIR / "meetings"
VOICES_DIR        = _BASE_DIR / "voices"

MAX_VOICE_SAMPLES = 100
IDENTIFY_THRESH   = 0.78      # 저장 프로파일 매칭 (높을수록 엄격)
CLUSTER_THRESH    = 0.74      # 실시간 클러스터링
ENROLL_SECONDS    = 10
MIN_EMBED_SECONDS = 1.5       # 이보다 짧은 발화는 화자 임베딩 건너뜀
TOPIC_INTERVAL    = 90
QUALITY_INTERVAL  = 600
CLAUDE_TIMEOUT    = 30
# 빠른 작업(STT교정·자동개입·주제감지)에는 Haiku, 무거운 작업에는 Sonnet
CLAUDE_FAST_MODEL = "claude-haiku-4-5-20251001"  # STT교정·자동개입·주제감지용

# ── 기본 WHISPER_PROMPT (서버 시작 시 동적으로 업데이트됨) ─
_WHISPER_BASE = (
    "네, 알겠습니다. 회의를 시작하겠습니다. 맞습니다. 그렇게 하죠. "
    "죄송합니다. 잠깐만요. 확인해볼게요. 말씀하신 것처럼. "
    "백엔드, 프론트엔드, API, 배포, 스프린트, 마감일, 이슈, 태스크, "
    "기획, 개발, 디자인, 일정, 담당자, 아사나, 슬랙, 깃, 기능, 버그, "
    "요구사항, 명세서, 코드리뷰, 테스트, 릴리즈, 서버, 데이터베이스."
)

# 한국어 불용어 (빈도 분석에서 제외)
_KO_STOPWORDS = {
    "이", "그", "저", "것", "수", "있", "하", "되", "않", "이다", "있다",
    "하다", "그리고", "그런데", "근데", "아", "어", "음", "네", "예",
    "좀", "더", "많이", "같이", "지금", "이제", "그냥", "진짜", "정말",
    "너무", "다시", "또", "다", "이번", "저번", "우리", "제가", "저는",
    "그게", "이게", "저게", "거", "걸", "를", "을", "이", "가", "은", "는",
    "도", "만", "서", "에", "로", "으로", "와", "과", "의", "한", "할",
    "했", "하고", "해서", "하면", "되면", "됩니다", "합니다", "있어요",
    "없어요", "그래서", "때문에", "라고", "이라고", "라는", "이라는",
    "회의", "말씀", "관련", "부분", "경우", "때", "번", "거든요",
}

# ── 언어 설정 (Whisper 언어 · Claude 응답 언어 · 자동개입 패턴) ──
LANGUAGE_CONFIGS: Dict[str, dict] = {
    "ko": {
        "name": "한국어",
        "whisper_lang": "ko",
        "whisper_base": _WHISPER_BASE,
        "word_pattern": r'[가-힣]{2,8}',
        "stopwords": _KO_STOPWORDS,
        "correct_prompt": "다음 음성인식 텍스트의 오인식만 수정하세요. 수정할 게 없으면 원문 그대로 출력. 설명 없이 교정된 한 줄만 출력:\n{text}",
        "reply_suffix": "한국어로, 2~3줄로 간결하게.",
        "topic_prompt": (
            "이전 주제: '{prev}'\n대화:\n{lines}\n\n"
            "주제 바뀌었으면 10자 이내로 새 주제명만. 안 바뀌었으면 '없음'."
        ),
        "topic_none": "없음",
        "quality_format": "⏱ 시간 배분: ...\n⚠️ 주의: ...\n💡 제안: ...",
        "auto_intervene": [
            (re.compile(r'(기억|뭐였|뭐였지|잊|까먹|생각.{0,5}안 나)'), "recall"),
            (re.compile(r'(결정하|정리하면|결론|마무리|정리해)'),          "summary"),
            (re.compile(r'(담당자|담당이|맡은|누가 하)'),                  "assignee"),
            (re.compile(r'(마감|언제까지|기한|데드라인)'),                 "deadline"),
        ],
        "intervene_prompts": {
            "recall":   "이전 회의록과 현재 대화 참고해서 관련 내용 간결하게.",
            "summary":  "지금까지 논의 3줄 요약.",
            "assignee": "담당자 관련 내용 정리.",
            "deadline": "언급된 마감일 정리.",
        },
        "schedule_prompt": (
            "회의 요약에서 '다음 회의 안건'을 파악하고 Google Calendar MCP로 "
            "미팅 이벤트를 생성해줘.\n\n"
            "지시사항:\n"
            "1. 참여자({members})를 초대\n"
            "2. 회의 일정이 명시되지 않으면 현재 시각 기준 1주일 뒤로 설정\n"
            "3. 이벤트 제목에 주요 안건 포함\n"
            "4. 결과를 한국어로 간단히 알려줘.\n\n회의 요약:\n{summary}"
        ),
    },
    "ja": {
        "name": "日本語",
        "whisper_lang": "ja",
        "whisper_base": (
            "はい、分かりました。会議を始めましょう。そうですね。 "
            "ちょっと待ってください。確認します。おっしゃる通りです。 "
            "バックエンド、フロントエンド、API、デプロイ、スプリント、 "
            "締め切り、イシュー、タスク、要件、コードレビュー、テスト。"
        ),
        "word_pattern": r'[ぁ-んァ-ン一-龥]{2,10}',
        "stopwords": {
            "は", "が", "を", "に", "で", "と", "の", "も", "から", "まで",
            "です", "ます", "した", "する", "ある", "いる", "それ", "これ",
            "あの", "その", "ので", "けど", "ね", "よ", "さ", "な",
        },
        "correct_prompt": "日本語STTの誤認識のみ修正。修正後の文を1行だけ出力:\n{text}",
        "reply_suffix": "日本語で、2〜3行で簡潔に。",
        "topic_prompt": (
            "前のトピック: '{prev}'\n会話:\n{lines}\n\n"
            "トピックが変わったなら10文字以内で新トピック名のみ。変わっていないなら'なし'。"
        ),
        "topic_none": "なし",
        "quality_format": "⏱ 時間配分: ...\n⚠️ 注意点: ...\n💡 提案: ...",
        "auto_intervene": [
            (re.compile(r'(覚えて|なんだっけ|忘れ|思い出せ|記憶)'), "recall"),
            (re.compile(r'(まとめ|決定|結論|整理)'),                  "summary"),
            (re.compile(r'(担当|誰が|責任者)'),                       "assignee"),
            (re.compile(r'(締め切り|いつまで|期限|デッドライン)'),    "deadline"),
        ],
        "intervene_prompts": {
            "recall":   "過去の会議と現在の会話を参考にして関連内容を簡潔に。",
            "summary":  "これまでの議論を3行で要約。",
            "assignee": "担当者関連の内容を整理。",
            "deadline": "言及された締め切りを整理。",
        },
        "schedule_prompt": (
            "会議の要約から「次回の会議議題」を把握し、Google Calendar MCPで"
            "ミーティングイベントを作成してください。\n\n"
            "指示:\n"
            "1. 参加者({members})を招待\n"
            "2. 日程が明示されていない場合は現在から1週間後に設定\n"
            "3. イベントタイトルに主要議題を含める\n"
            "4. 結果を日本語で簡単に教えてください。\n\n会議の要約:\n{summary}"
        ),
    },
    "en": {
        "name": "English",
        "whisper_lang": "en",
        "whisper_base": (
            "Yes, I understand. Let's start the meeting. That's correct. Sounds good. "
            "Just a moment. Let me check. As you mentioned. "
            "backend, frontend, API, deployment, sprint, deadline, issue, task, "
            "requirements, code review, testing, release, server, database."
        ),
        "word_pattern": r'\b[a-zA-Z]{4,15}\b',
        "stopwords": {
            "the", "and", "for", "are", "but", "not", "you", "all", "can",
            "has", "her", "was", "one", "our", "out", "day", "get", "has",
            "him", "his", "how", "its", "may", "new", "now", "old", "see",
            "two", "way", "who", "boy", "did", "this", "that", "with",
            "have", "from", "they", "will", "been", "said", "each", "which",
            "their", "there", "would", "about", "could", "other", "into",
            "then", "than", "like", "some", "just", "very", "what", "also",
            "when", "were", "more", "time", "think", "know", "make", "need",
        },
        "correct_prompt": "Fix only English STT recognition errors. Output corrected text in one line:\n{text}",
        "reply_suffix": "In English, 2-3 lines, concise.",
        "topic_prompt": (
            "Previous topic: '{prev}'\nConversation:\n{lines}\n\n"
            "If topic changed, write new topic in 5 words or less. If not changed, write 'none'."
        ),
        "topic_none": "none",
        "quality_format": "⏱ Time allocation: ...\n⚠️ Warning: ...\n💡 Suggestion: ...",
        "auto_intervene": [
            (re.compile(r"(don'?t remember|what was|forgot|can'?t recall|what did)"), "recall"),
            (re.compile(r'(let.s wrap|in summary|to summarize|wrap up|in conclusion)'), "summary"),
            (re.compile(r'(who.s (responsible|owner|assigned)|whose (task|job))'),    "assignee"),
            (re.compile(r'(deadline|due (date|by)|by when|time.?line)'),              "deadline"),
        ],
        "intervene_prompts": {
            "recall":   "Refer to past meeting notes and current conversation. Be brief.",
            "summary":  "Summarize the discussion so far in 3 lines.",
            "assignee": "Clarify who is responsible.",
            "deadline": "List the deadlines mentioned.",
        },
        "schedule_prompt": (
            "From the meeting summary, identify the next meeting agenda and create a "
            "Google Calendar MCP event.\n\n"
            "Instructions:\n"
            "1. Invite participants ({members})\n"
            "2. If no date specified, schedule 1 week from now\n"
            "3. Include main agenda in the event title\n"
            "4. Briefly report the result in English.\n\nMeeting summary:\n{summary}"
        ),
    },
    "auto": {
        "name": "자동 감지",
        "whisper_lang": None,
        "whisper_base": _WHISPER_BASE,
        "word_pattern": r'[가-힣a-zA-Z]{2,15}',
        "stopwords": _KO_STOPWORDS,
        "correct_prompt": "Fix only STT recognition errors. Output corrected text in one line:\n{text}",
        "reply_suffix": "Respond in the same language as the conversation, 2-3 lines.",
        "topic_prompt": (
            "Previous topic: '{prev}'\nConversation:\n{lines}\n\n"
            "If topic changed, write new topic in 10 chars or less. If not, write 'none'."
        ),
        "topic_none": "none",
        "quality_format": "⏱ Time: ...\n⚠️ Note: ...\n💡 Tip: ...",
        "auto_intervene": [
            (re.compile(r'(기억|뭐였|forget|remember|recall)'), "recall"),
            (re.compile(r'(요약|summary|정리|wrap.?up)'),        "summary"),
            (re.compile(r'(담당|who.{0,10}responsible|担当)'),   "assignee"),
            (re.compile(r'(마감|deadline|due date|期限)'),       "deadline"),
        ],
        "intervene_prompts": {
            "recall":   "Refer to past meeting notes. Be brief.",
            "summary":  "Summarize discussion in 3 lines.",
            "assignee": "Clarify who is responsible.",
            "deadline": "List mentioned deadlines.",
        },
        "schedule_prompt": (
            "From the meeting summary, create a Google Calendar event for the next meeting.\n"
            "1. Invite participants ({members})\n"
            "2. Schedule 1 week from now if no date specified\n"
            "3. Include main agenda in event title\n"
            "4. Report result briefly.\n\nMeeting summary:\n{summary}"
        ),
    },
}

# ── 회의 템플릿 (요약 포맷 + 아이콘) ──────────────────────
_TMPL_TAIL = "\n\n회의록:\n{content}"

MEETING_TEMPLATES: Dict[str, dict] = {
    "general": {
        "name": "일반 회의", "icon": "💼",
        "prompt": (
            "다음 회의록을 분석해줘. 언어는 회의 내용에 맞춰서.\n\n"
            "## 📋 회의 요약\n(핵심 내용 3줄 이내)\n\n"
            "## ✅ 결정 사항\n(결론이 명확히 난 항목만. 근거도 간략히)\n\n"
            "## ❓ 미결 사항\n(결론 없이 끝난 논의. 다음 회의 필요 여부 표시)\n\n"
            "## 📌 액션 아이템\n"
            "| 담당자 | 내용 | 마감일 | 우선순위 |\n|--------|------|--------|----------|\n"
            "(담당자/기한 불명확하면 \"미정\". Asana 태스크화 가능하도록 구체적으로)\n\n"
            "## 🔗 언급된 태스크/이슈\n(Asana 태스크명, 티켓 번호 등)\n\n"
            "## 💡 다음 회의 안건"
        ) + _TMPL_TAIL,
    },
    "daily_scrum": {
        "name": "데일리 스크럼", "icon": "🔄",
        "prompt": (
            "다음 데일리 스크럼 내용을 분석해줘. 언어는 회의 내용에 맞춰서.\n\n"
            "## 👥 팀원별 현황\n"
            "| 팀원 | 어제 한 일 | 오늘 할 일 | 블로커 |\n|------|-----------|-----------|--------|\n"
            "(언급 없으면 \"-\")\n\n"
            "## ⛔ 공통 블로커 & 의존성\n(여러 팀원에게 영향 주는 이슈)\n\n"
            "## 📌 즉시 액션 아이템\n"
            "| 담당자 | 내용 | 마감 |\n|--------|------|------|\n\n"
            "## 💡 내일 스크럼 안건"
        ) + _TMPL_TAIL,
    },
    "1on1": {
        "name": "1on1", "icon": "🤝",
        "prompt": (
            "다음 1on1 회의록을 분석해줘. 언어는 회의 내용에 맞춰서.\n\n"
            "## 💬 주요 논의 주제\n(핵심 대화 주제 요약)\n\n"
            "## 🌱 성장 & 피드백\n"
            "- **잘하고 있는 점:** ...\n"
            "- **개선 영역:** ...\n"
            "- **요청/제안:** ...\n\n"
            "## 🎯 목표 & 진행 상황\n(OKR, 개인 목표 등 언급 내용)\n\n"
            "## 📌 액션 아이템\n"
            "| 담당자 | 내용 | 마감일 |\n|--------|------|--------|\n\n"
            "## 💡 다음 1on1 안건\n(이번에 다루지 못한 주제, 팔로업 필요 항목)"
        ) + _TMPL_TAIL,
    },
    "planning": {
        "name": "기획 회의", "icon": "🏗️",
        "prompt": (
            "다음 기획 회의록을 분석해줘. 언어는 회의 내용에 맞춰서.\n\n"
            "## 📋 기획 배경 & 목표\n(무엇을, 왜 만드는가)\n\n"
            "## 🏗️ 주요 결정 사항\n(아키텍처, 방향성, 기술 선택 등)\n\n"
            "## 📐 요구사항 & 범위\n"
            "- **포함 (In scope):** ...\n"
            "- **제외 (Out of scope):** ...\n\n"
            "## ⏱️ 일정 & 마일스톤\n"
            "| 마일스톤 | 목표일 | 담당 |\n|---------|--------|------|\n\n"
            "## ⚠️ 리스크 & 의존성\n\n"
            "## ❓ 미결 사항 & 보류 결정\n\n"
            "## 📌 액션 아이템\n"
            "| 담당자 | 내용 | 마감일 | 우선순위 |\n|--------|------|--------|----------|\n\n"
            "## 💡 다음 회의 안건"
        ) + _TMPL_TAIL,
    },
    "weekly": {
        "name": "주간 회의", "icon": "📅",
        "prompt": (
            "다음 주간 회의록을 분석해줘. 언어는 회의 내용에 맞춰서.\n\n"
            "## 📊 주간 성과 요약\n(이번 주 주요 달성 내용)\n\n"
            "## ✅ 완료된 항목\n\n"
            "## 🔄 진행 중인 항목\n(진척률 및 예상 완료일)\n\n"
            "## ⚠️ 이슈 & 리스크\n(발생했거나 예상되는 문제)\n\n"
            "## 📅 다음 주 계획\n"
            "| 담당자 | 작업 내용 | 목표일 |\n|--------|---------|--------|\n\n"
            "## 📌 액션 아이템\n"
            "| 담당자 | 내용 | 마감일 |\n|--------|------|--------|\n\n"
            "## 💡 다음 주간 회의 안건"
        ) + _TMPL_TAIL,
    },
}


VOCAB_FILE       = _BASE_DIR / "vocab.json"
GLOSSARY_FILE    = _BASE_DIR / "glossary.json"
CORRECTIONS_FILE = _BASE_DIR / "corrections.json"
_VOCAB_LOCK   = threading.Lock()
_GLOSSARY_LOCK = threading.Lock()
# 언어별 vocab 파일 (ko/ja/en 분리)
_VOCAB_FILES: Dict[str, Path] = {
    "ko": _BASE_DIR / "vocab_ko.json",
    "ja": _BASE_DIR / "vocab_ja.json",
    "en": _BASE_DIR / "vocab_en.json",
}
# Whisper 프롬프트 최소 누적 횟수 (이 이상 등장한 단어 전부 포함)
# Whisper가 내부적으로 224토큰 한도 내에서 자동 처리함
_VOCAB_MIN_CNT = 5
_VOCAB_TOP_N   = None  # 제한 없음

# 영문 기술 용어 패턴 — 언어 무관 공통 추출
_TECH_WORD_PAT = re.compile(
    r'\b(?:'
    r'v\d+(?:\.\d+)*'                       # v1.2.3
    r'|[A-Z]{2,}(?:\d+)?'                   # API, JWT, PR2
    r'|[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+'  # CamelCase
    r')\b'
)
_TECH_STOPWORDS = {
    "I", "OK", "IT", "IS", "TO", "IN", "AT", "ON", "OF", "BY",
    "NO", "GO", "DO", "SO", "OR", "IF", "AS", "BE", "WE", "MY",
    "AN", "AM", "PM", "RE",
}


def _load_vocab(language: str = "ko") -> Dict[str, int]:
    path = _VOCAB_FILES.get(language, _VOCAB_FILES["ko"])
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_vocab(counts: Dict[str, int], language: str = "ko") -> None:
    import json
    path = _VOCAB_FILES.get(language, _VOCAB_FILES["ko"])
    # 빈도 내림차순 정렬 후 저장
    sorted_counts = dict(sorted(counts.items(), key=lambda x: -x[1]))
    try:
        path.write_text(json.dumps(sorted_counts, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:
        pass


def update_vocab_from_meeting(md_path: Path, language: str = "ko") -> int:
    """
    회의 종료 후 MD 파일에서 도메인 어휘를 추출해 누적 vocab 파일에 반영.
    반환값: 새로 추가/갱신된 단어 수
    """
    from collections import Counter
    lang_cfg  = LANGUAGE_CONFIGS.get(language, LANGUAGE_CONFIGS["ko"])
    stopwords = lang_cfg["stopwords"]
    word_pat  = lang_cfg["word_pattern"]

    new_counts: Counter = Counter()
    try:
        content = md_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            if not (line.startswith("**") and "|" in line):
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            text = parts[2].strip()
            for w in re.findall(word_pat, text):
                if w.lower() not in stopwords:
                    new_counts[w] += 1
            # 영문 기술 용어 추가 추출 (언어 무관)
            for w in re.findall(_TECH_WORD_PAT, text):
                if w not in _TECH_STOPWORDS:
                    new_counts[w] += 1
    except Exception:
        return 0

    if not new_counts:
        return 0

    with _VOCAB_LOCK:
        existing = _load_vocab(language)
        for word, cnt in new_counts.items():
            existing[word] = existing.get(word, 0) + cnt
        _save_vocab(existing, language)

    return len(new_counts)


_CORRECTIONS_LOCK = threading.Lock()

def load_corrections() -> List[Dict]:
    """교정 이력 로드. [{original, corrected, count, last_seen}, ...]"""
    if not CORRECTIONS_FILE.exists():
        return []
    try:
        data = json.loads(CORRECTIONS_FILE.read_text(encoding="utf-8"))
        return data.get("corrections", [])
    except Exception:
        return []


def save_correction(original: str, corrected: str):
    """사용자 교정을 저장. 같은 쌍이면 count 증가, 최대 200개."""
    if not original.strip() or not corrected.strip():
        return
    if original.strip() == corrected.strip():
        return
    with _CORRECTIONS_LOCK:
        corrections = load_corrections()
        found = False
        for c in corrections:
            if c["original"] == original:
                c["corrected"] = corrected
                c["count"] = c.get("count", 1) + 1
                c["last_seen"] = datetime.now().strftime("%Y-%m-%d")
                found = True
                break
        if not found:
            corrections.append({
                "original": original,
                "corrected": corrected,
                "count": 1,
                "last_seen": datetime.now().strftime("%Y-%m-%d"),
            })
        # 최대 200개 (사용 빈도 낮은 것부터 제거)
        if len(corrections) > 200:
            corrections.sort(key=lambda x: x.get("count", 0), reverse=True)
            corrections = corrections[:200]
        CORRECTIONS_FILE.write_text(
            json.dumps({"corrections": corrections}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


def build_whisper_prompt(language: str = "ko") -> str:
    """
    누적 vocab 파일에서 자주 등장하는 도메인 용어를 읽어
    Whisper initial_prompt 생성. vocab 파일 없으면 최근 10개 회의에서 초기화.
    """
    lang_cfg  = LANGUAGE_CONFIGS.get(language, LANGUAGE_CONFIGS["ko"])
    base      = lang_cfg["whisper_base"]
    stopwords = lang_cfg["stopwords"]
    word_pat  = lang_cfg["word_pattern"]

    vocab = _load_vocab(language)

    # vocab 파일이 없으면 기존 회의록에서 초기화 (최초 1회)
    if not vocab:
        from collections import Counter
        word_counter: Counter = Counter()
        for f in sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True):
            try:
                content = f.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if not (line.startswith("**") and "|" in line):
                        continue
                    parts = line.split("|", 2)
                    if len(parts) < 3:
                        continue
                        utterance = parts[2].strip()
                    for w in re.findall(word_pat, utterance):
                        if w.lower() not in stopwords:
                            word_counter[w] += 1
                    for w in re.findall(_TECH_WORD_PAT, utterance):
                        if w not in _TECH_STOPWORDS:
                            word_counter[w] += 1
            except Exception:
                pass
        if word_counter:
            with _VOCAB_LOCK:
                _save_vocab(dict(word_counter), language)
            vocab = dict(word_counter)

    frequent = [w for w, c in sorted(vocab.items(), key=lambda x: -x[1])
                if c >= _VOCAB_MIN_CNT
                and (w.lower() not in stopwords)
                and (w not in _TECH_STOPWORDS)]

    # 용어집 용어도 Whisper 힌트에 포함
    glossary_terms = list(load_glossary().keys())

    # 사용자 교정 학습 — 교정된 형태를 Whisper 힌트에 포함 (2회 이상 교정된 것만)
    correction_terms = []
    for c in load_corrections():
        if c.get("count", 0) >= 2:
            correction_terms.append(c["corrected"])

    all_terms = frequent + [t for t in glossary_terms if t not in frequent]
    all_terms += [t for t in correction_terms if t not in all_terms]
    if not all_terms:
        return base

    return base + " " + ", ".join(all_terms) + "."


# ──────────────── 용어집 (Glossary) ─────────────────────
def load_glossary() -> Dict[str, str]:
    """glossary.json 로드. 형식: {"용어": "설명", ...}"""
    with _GLOSSARY_LOCK:
        if not GLOSSARY_FILE.exists():
            return {}
        try:
            import json
            return json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_glossary(data: Dict[str, str]) -> None:
    import json
    with _GLOSSARY_LOCK:
        GLOSSARY_FILE.write_text(
            json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


def extract_glossary_from_meeting(md_path: Path) -> int:
    """
    회의 종료 후 MD에서 신규 용어/약어를 Claude로 자동 추출해 glossary.json에 추가.
    반환값: 새로 추가된 용어 수
    """
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return 0

    # 대화 부분만 추출 (헤더/요약 제외)
    if "## 💬 대화 내용" in content:
        dialogue = content.split("## 💬 대화 내용")[-1].split("## 🏁")[0]
    else:
        dialogue = content[:4000]

    existing = load_glossary()
    existing_keys = ", ".join(list(existing.keys())[:30]) if existing else "없음"

    prompt = (
        "다음 회의 대화에서 팀/회사 고유 용어, 약어, 프로젝트명, 시스템명을 추출해줘.\n"
        "이미 등록된 용어: " + existing_keys + "\n\n"
        "조건:\n"
        "- 이미 등록된 용어는 제외\n"
        "- 일반적인 한국어 단어, 영어 단어(API, URL 등 범용 기술용어)는 제외\n"
        "- 이 조직/팀/프로젝트 고유한 용어만 포함\n"
        "- 각 용어의 한 줄 설명 포함\n\n"
        "JSON 형식으로만 응답 (설명 없이):\n"
        '{"용어1": "설명1", "용어2": "설명2"}\n\n'
        "용어가 없으면: {}\n\n"
        "회의 대화:\n" + dialogue[:3000]
    )

    result = claude_run(prompt, timeout=30, retries=1)
    if not result:
        return 0

    try:
        import json, re as _re
        m = _re.search(r'\{[^{}]*\}', result, _re.DOTALL)
        if not m:
            return 0
        new_terms: Dict[str, str] = json.loads(m.group())
        if not new_terms:
            return 0
        existing.update(new_terms)
        save_glossary(existing)
        return len(new_terms)
    except Exception:
        return 0


# 전역 WHISPER_PROMPT — 터미널 모드 호환용 (한국어 기본값)
WHISPER_PROMPT = _WHISPER_BASE  # 초기값, load_model() 후 업데이트됨

# .env 파일 로드 (HF_TOKEN 등)
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
_load_env()

try:
    from resemblyzer import VoiceEncoder, preprocess_wav
    HAS_RESEMBLYZER = True
except ImportError:
    HAS_RESEMBLYZER = False
    VoiceEncoder = None

# ── pyannote 화자 임베딩 (resemblyzer보다 정확, HF 토큰 필요) ──────
_pyannote_embedder = None
HAS_PYANNOTE = False

_pyannote_segmenter = None
HAS_PYANNOTE_SEG = False

# ── 겹침 발화 분리 (Source Separation) ──────────────────────
_separator_model = None
HAS_SEPARATOR = False

def _load_separator():
    """ConvTasNet 음원 분리 모델 로드 (겹침 발화 분리용)."""
    global _separator_model, HAS_SEPARATOR
    if HAS_SEPARATOR:
        return True
    try:
        from asteroid.models import ConvTasNet
        _separator_model = ConvTasNet.from_pretrained("JorisCos/ConvTasNet_Libri2Mix_sepclean_16k")
        _separator_model.eval()
        HAS_SEPARATOR = True
        print("✅ 겹침 발화 분리 모델 (ConvTasNet) 로드 완료.")
        return True
    except Exception as exc:
        print(f"ℹ️  겹침 발화 분리 모델 로드 실패 ({exc}) — 겹침 구간 분리 비활성.")
        return False


def _has_overlap(audio: np.ndarray) -> bool:
    """pyannote segmenter로 겹침 발화 여부 확인."""
    if not HAS_PYANNOTE_SEG or _pyannote_segmenter is None:
        return False
    try:
        import torch
        waveform = torch.from_numpy(audio).unsqueeze(0).float()
        output = _pyannote_segmenter({"waveform": waveform, "sample_rate": SAMPLE_RATE})
        if hasattr(output, 'data'):
            # 각 프레임에서 동시 활성 화자 수 확인
            activity = (output.data > 0.5).float()
            simultaneous = activity.sum(axis=-1)  # 각 프레임의 활성 화자 수
            overlap_ratio = (simultaneous > 1).float().mean().item()
            return overlap_ratio > 0.1  # 10% 이상 겹침이면 분리 시도
    except Exception:
        pass
    return False


def _separate_speakers(audio: np.ndarray) -> List[np.ndarray]:
    """ConvTasNet으로 겹침 발화 분리. 분리된 오디오 스트림 리스트 반환."""
    if not HAS_SEPARATOR or _separator_model is None:
        return [audio]
    try:
        import torch
        with torch.no_grad():
            tensor = torch.from_numpy(audio).unsqueeze(0).float()
            separated = _separator_model(tensor)  # (1, n_sources, n_samples)
            streams = [separated[0, i].numpy() for i in range(separated.shape[1])]
        # 무음 스트림 제거
        return [s for s in streams if np.sqrt(np.mean(s ** 2)) > SILENCE_THRESH * 2]
    except Exception:
        return [audio]

def _load_pyannote_embedder():
    """pyannote ECAPA-TDNN 임베딩 + 화자 변경 감지 모델 로드."""
    global _pyannote_embedder, HAS_PYANNOTE, _pyannote_segmenter, HAS_PYANNOTE_SEG
    if HAS_PYANNOTE:
        return True
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        return False
    try:
        from pyannote.audio import Model, Inference
        # ECAPA-TDNN: wespeaker-resnet34보다 화자 구분 정확도 높음
        model = Model.from_pretrained(
            "pyannote/embedding",
            use_auth_token=hf_token,
        )
        _pyannote_embedder = Inference(model, window="whole")
        HAS_PYANNOTE = True
        print("✅ pyannote ECAPA-TDNN 임베딩 모델 로드 완료.")
    except Exception as exc:
        print(f"⚠️  pyannote 임베딩 로드 실패 ({exc}) — resemblyzer로 대체.")
        return False

    # 화자 변경 감지 (segmentation) — 선택적
    try:
        from pyannote.audio import Model, Inference
        seg_model = Model.from_pretrained(
            "pyannote/segmentation-3.0",
            use_auth_token=hf_token,
        )
        _pyannote_segmenter = Inference(seg_model)
        HAS_PYANNOTE_SEG = True
        print("✅ pyannote 화자 변경 감지 모델 로드 완료.")
    except Exception as exc:
        print(f"ℹ️  화자 변경 감지 모델 로드 실패 ({exc}) — 기본 방식 유지.")

    return True

try:
    import noisereduce as nr
    HAS_NOISEREDUCE = True
except ImportError:
    HAS_NOISEREDUCE = False

# ── Silero VAD (딥러닝 발화 감지, webrtcvad 대체) ───────────────────
_silero_model = None
_silero_utils = None
HAS_SILERO_VAD = False
HAS_WEBRTCVAD = False  # 하위 호환 플래그 (Silero 있으면 같은 경로 사용)

def _load_silero_vad():
    """Silero VAD 모델 지연 로드 (최초 1회)."""
    global _silero_model, _silero_utils, HAS_SILERO_VAD, HAS_WEBRTCVAD
    if HAS_SILERO_VAD:
        return True
    try:
        import torch
        model, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        _silero_model = model
        _silero_utils = utils
        HAS_SILERO_VAD = True
        HAS_WEBRTCVAD = True  # 하위 호환: 같은 분기 사용
        print("✅ Silero VAD 로드 완료.")
        return True
    except Exception as exc:
        print(f"⚠️  Silero VAD 로드 실패 ({exc}) — 에너지 기반 VAD로 대체.")
        return False

# ── 트리거 워드 패턴 ──────────────────────────────────────
def _filter_hallucination(text: str) -> str:
    """Whisper 할루시네이션 패턴 필터링."""
    if not text:
        return text

    # 1. 한글 자모/특수문자 연속 반복: "ㅇㅇㅇㅇ", "ㅎㅎㅎ", "ㅋㅋㅋ" 등
    if re.search(r'([ㄱ-ㅎㅏ-ㅣ])\1{4,}', text):
        return ''

    # 2. 모든 문자 연속 반복: "abcabc", "QA QA QA", "őlőlől"
    if re.search(r'(.{1,5})\1{5,}', text):
        return ''

    # 3. "..." 반복 패턴: "몰리고... 그 정도... 그리고... 그리고..."
    ellipsis_count = text.count('...')
    if ellipsis_count >= 4:
        # "..." 제거 후 내용만 추출
        cleaned = re.sub(r'\.{2,}', '.', text).strip()
        if len(cleaned) < 10:
            return ''
        return cleaned

    # 4. 문장 단위 반복 감지: 같은 문장이 2회 이상 반복
    sentences = re.split(r'[.!?]\s*', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
    if len(sentences) >= 3:
        from collections import Counter
        sent_counts = Counter(sentences)
        most_common_sent, sent_count = sent_counts.most_common(1)[0]
        if sent_count >= 2 and sent_count / len(sentences) >= 0.5:
            return most_common_sent

    # 5. 쉼표·공백 구분 반복 토큰 — 토큰이 전체의 40% 이상 차지하면 제거
    tokens = [t.strip() for t in re.split(r'[,\s?!]+', text) if t.strip()]
    if len(tokens) >= 6:
        from collections import Counter
        most_common_token, count = Counter(tokens).most_common(1)[0]
        if count / len(tokens) >= 0.4:
            return most_common_token
        # 숫자만 나열된 경우: "2, 2, 3, 4, 4, 5..."
        if sum(1 for t in tokens if t.isdigit()) / len(tokens) >= 0.8:
            return ''

    # 6. 공백 구분 반복 (2회 이상): "QA QA" → "QA"
    text = re.sub(r'\b(\w+)( \1){1,}\b', r'\1', text)

    # 7. 쉼표/물음표 구분 반복 (3회 이상): "이렇게, 이렇게, 이렇게" → "이렇게"
    text = re.sub(r'(.{1,}?)[,?]\s*(?:\1[,?]\s*){2,}', r'\1', text)

    # 8. 너무 짧은 반복 (5자 이하가 3회 이상): "네. 네. 네." → "네"
    text = re.sub(r'(.{1,5})[.\s]+(?:\1[.\s]+){2,}', r'\1', text)

    # 9. 의미 없는 짧은 텍스트 (3자 이하)
    stripped = re.sub(r'[.\s,?!…]+', '', text)
    if len(stripped) <= 2:
        return ''

    # 10. "발화..." 같은 Whisper 메타 텍스트
    _meta_hallucinations = {"발화", "자막", "구독", "좋아요", "알림", "시청", "감사합니다",
                            "구독과 좋아요", "영상", "채널", "다음 영상", "소리"}
    if stripped in _meta_hallucinations:
        return ''

    # 11. 한국어가 아닌 외국어만으로 된 짧은 텍스트 (무의미한 할루시네이션)
    korean_chars = len(re.findall(r'[가-힣]', text))
    total_alpha = len(re.findall(r'[a-zA-Zㄱ-ㅎㅏ-ㅣ가-힣]', text))
    if total_alpha > 0 and korean_chars == 0 and len(text) < 30:
        # 영문 기술 용어는 유지 (대문자 포함, 알파벳 비율 높음)
        upper_ratio = len(re.findall(r'[A-Z]', text)) / max(1, total_alpha)
        if upper_ratio < 0.3:  # 소문자 위주 외국어 → 할루시네이션
            return ''

    return text


# Whisper 오인식 변형 포함:
#   "헤이/에이/hey" + "클로드/claude/cloud"
#   혼합 표기: "hey 클로드", "에이 클로드" 등
_TRIGGER_PATTERN = re.compile(
    r'((?:헤이|에이|hey)\s*(?:클로드|claude|cloud)|hey\s*클로드|에이\s*클로드|클로드\s*야|클로드야)'
    r'[,。,!\s]*(.{2,})',
    re.IGNORECASE | re.DOTALL
)

def _vad_has_speech(audio_f32: np.ndarray, threshold: float = 0.3) -> bool:
    """Silero VAD로 발화 포함 여부 판단. 미설치 시 에너지 기반으로 대체.

    threshold: Silero confidence score 기준 (0.0~1.0). 낮을수록 민감.
    """
    if HAS_SILERO_VAD and _silero_model is not None:
        try:
            import torch
            # Silero VAD는 16kHz float32 tensor 입력
            tensor = torch.from_numpy(audio_f32)
            confidence = _silero_model(tensor, SAMPLE_RATE).item()
            return confidence >= threshold
        except Exception:
            pass
    # 폴백: 에너지 기반 (std 체크와 동일 선상)
    return bool(audio_f32.std() >= SILENCE_THRESH)


# ──────────────── LLM 백엔드 감지 ────────────────────────
_claude_fail_count = 0
_claude_fail_lock  = threading.Lock()
_CLAUDE_CLI_MISSING = False     # Claude CLI 없음
_OLLAMA_MISSING     = False     # Ollama도 없음

# 환경변수로 Ollama 모델 지정 가능
# 기본값: exaone3.5:7.8b-instruct-q4_K_M (LG AI Research, 한국어 최고 성능, Q4_K_M 4.8GB)
# 대안: exaone3.5:7.8b-instruct-q8_0 (고품질, 8.3GB), qwen2.5 (29개 언어), gemma3 (경량)
OLLAMA_MODEL      = __import__("os").environ.get("OLLAMA_MODEL", "exaone3.5:7.8b-instruct-q4_K_M")
OLLAMA_BASE_URL   = __import__("os").environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

_OLLAMA_SYSTEM_PROMPT = (
    "당신은 한국어 회의 어시스턴트입니다. "
    "반드시 한국어로 답변하세요. "
    "간결하고 명확하게 답변하세요."
)

# 응답 캐시: {hash → (result, timestamp)}
_ollama_cache: dict = {}
_ollama_cache_lock = threading.Lock()
_OLLAMA_CACHE_TTL  = 300  # 5분


def _detect_llm_backend() -> str:
    """시작 시 LLM 백엔드 자동 감지. Claude CLI → Ollama 순서."""
    # 1순위: Claude CLI
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            print("[INFO] LLM 백엔드: Claude Code CLI", file=sys.stderr)
            return "claude"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2순위: Ollama
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        if req.status == 200:
            print(f"[INFO] LLM 백엔드: Ollama ({OLLAMA_BASE_URL}, 모델: {OLLAMA_MODEL})", file=sys.stderr)
            return "ollama"
    except Exception:
        pass

    print("[WARN] Claude CLI, Ollama 모두 감지되지 않음. AI 기능 비활성화.", file=sys.stderr)
    return "none"


def _pull_ollama_model_async():
    """Ollama 모델이 없으면 백그라운드에서 자동 다운로드."""
    import urllib.request, json as _json

    def _pull():
        # 모델 목록 확인
        try:
            resp = urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
            tags = _json.loads(resp.read())
            names = [m.get("name", "") for m in tags.get("models", [])]
            if any(OLLAMA_MODEL in n for n in names):
                return  # 이미 있음
        except Exception:
            return

        print(f"[INFO] Ollama 모델 다운로드 시작: {OLLAMA_MODEL} (수 분 소요)", file=sys.stderr)
        try:
            payload = _json.dumps({"name": OLLAMA_MODEL, "stream": False}).encode()
            req = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/pull",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=600)
            print(f"[INFO] Ollama 모델 다운로드 완료: {OLLAMA_MODEL}", file=sys.stderr)
        except Exception as e:
            print(f"[ERROR] Ollama 모델 다운로드 실패: {e}", file=sys.stderr)

    threading.Thread(target=_pull, daemon=True).start()


_LLM_BACKEND: str = _detect_llm_backend()
if _LLM_BACKEND == "ollama":
    _pull_ollama_model_async()


def _ollama_run(prompt: str, timeout: int = CLAUDE_TIMEOUT) -> str:
    """Ollama Chat API 호출 (스트리밍 + 캐싱 + 한국어 최적화)."""
    import urllib.request, json as _json, hashlib
    global _OLLAMA_MISSING
    if _OLLAMA_MISSING:
        return ""

    # 캐시 확인
    cache_key = hashlib.md5(f"{OLLAMA_MODEL}:{prompt}".encode()).hexdigest()
    with _ollama_cache_lock:
        if cache_key in _ollama_cache:
            result, ts = _ollama_cache[cache_key]
            if time.time() - ts < _OLLAMA_CACHE_TTL:
                return result
            del _ollama_cache[cache_key]

    payload = _json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _OLLAMA_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "stream": True,            # 스트리밍으로 타임아웃 안정성 향상
        "keep_alive": "1h",        # 모델 메모리 상주
        "options": {
            "temperature": 0.6,    # 한국어 일관성 향상
            "top_p": 0.95,         # 동아시아 언어 최적화
            "num_ctx": 2048,       # 처리 속도 향상
        },
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        chunks = []
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = _json.loads(line)
                content = chunk.get("message", {}).get("content", "")
                if content:
                    chunks.append(content)
                if chunk.get("done", False):
                    break
        result = "".join(chunks).strip()

        # 캐시 저장
        if result:
            with _ollama_cache_lock:
                _ollama_cache[cache_key] = (result, time.time())

        return result
    except Exception as e:
        _OLLAMA_MISSING = True
        print(f"[ERROR] Ollama 호출 실패: {e}", file=sys.stderr)
        return ""


_claude_semaphore = threading.Semaphore(3)  # Claude CLI 동시 호출 최대 3개

def claude_run(prompt: str, timeout: int = CLAUDE_TIMEOUT, retries: int = 2,
               model: str = "") -> str:
    """
    LLM 호출 (Claude CLI 우선, 없으면 Ollama 자동 폴백).
    model: "" → 기본 모델(Sonnet), CLAUDE_FAST_MODEL → Haiku.
    동시 호출 3개 제한 — 초과 시 대기.
    """
    acquired = _claude_semaphore.acquire(timeout=timeout)
    if not acquired:
        return ""
    try:
        global _claude_fail_count, _CLAUDE_CLI_MISSING

        if _LLM_BACKEND == "ollama":
            return _ollama_run(prompt, timeout)

        if _LLM_BACKEND == "none" or _CLAUDE_CLI_MISSING:
            return ""

        cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]

        last_err = ""
        for attempt in range(retries + 1):
            try:
                r = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=timeout
                )
                if r.returncode == 0 and r.stdout.strip():
                    with _claude_fail_lock:
                        _claude_fail_count = 0
                    return r.stdout.strip()
                last_err = (r.stderr.strip() or f"returncode={r.returncode}")[:120]
            except subprocess.TimeoutExpired:
                last_err = f"timeout({timeout}s)"
            except FileNotFoundError:
                _CLAUDE_CLI_MISSING = True
                print("[ERROR] Claude CLI를 찾을 수 없습니다.", file=sys.stderr)
                return ""
            except Exception as e:
                last_err = str(e)[:120]

            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))

        with _claude_fail_lock:
            _claude_fail_count += 1
            cnt = _claude_fail_count
        print(f"[WARN] Claude CLI 실패 ({cnt}회): {last_err}", file=sys.stderr)
        return ""
    finally:
        _claude_semaphore.release()


# ──────────────── 목소리 프로파일 매니저 ─────────────────
class VoiceProfileManager:
    """
    voices/{이름}/ 폴더에 샘플별 npy 파일로 저장.

    voices/
    ├── Jerry/
    │   ├── 20260401_143012.npy
    │   └── 20260408_091523.npy
    └── 민수/
        └── 20260401_143015.npy
    """

    def __init__(self, encoder):
        self.encoder = encoder
        VOICES_DIR.mkdir(exist_ok=True)

    def _person_dir(self, name: str) -> Path:
        """이름별 폴더 경로 반환 (없으면 생성)"""
        d = VOICES_DIR / name
        d.mkdir(exist_ok=True)
        return d

    def embed_audio(self, audio: np.ndarray) -> Optional[np.ndarray]:
        """오디오 → 임베딩. pyannote 우선, 없으면 resemblyzer 사용."""
        # pyannote: resemblyzer보다 정확한 화자 임베딩
        if HAS_PYANNOTE and _pyannote_embedder is not None:
            try:
                import torch
                tensor = torch.from_numpy(audio).unsqueeze(0)  # (1, samples)
                waveform = {"waveform": tensor, "sample_rate": SAMPLE_RATE}
                embedding = _pyannote_embedder(waveform)
                return np.array(embedding, dtype=np.float32)
            except Exception:
                pass
        # resemblyzer fallback
        if not self.encoder:
            return None
        try:
            wav = preprocess_wav(audio, source_sr=SAMPLE_RATE)
            return self.encoder.embed_utterance(wav)
        except Exception:
            return None

    def save_embedding(self, name: str, embedding: np.ndarray):
        """
        임베딩을 타임스탬프 파일로 저장.
        폴더 내 파일이 MAX_VOICE_SAMPLES 초과 시 가장 오래된 것 삭제.
        """
        person_dir = self._person_dir(name)
        ts         = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        np.save(str(person_dir / f"{ts}.npy"), embedding)

        # 오래된 샘플 정리
        files = sorted(person_dir.glob("*.npy"))
        for old in files[:-MAX_VOICE_SAMPLES]:
            try: old.unlink()
            except Exception: pass

    def save_from_audio(self, name: str, audio: np.ndarray) -> Optional[np.ndarray]:
        """오디오에서 임베딩 추출 후 저장"""
        embed = self.embed_audio(audio)
        if embed is not None:
            self.save_embedding(name, embed)
        return embed

    def _load_person_embeddings(self, name: str) -> Optional[np.ndarray]:
        """이름 폴더의 모든 샘플을 로드해 평균 임베딩 반환"""
        person_dir = VOICES_DIR / name
        if not person_dir.is_dir(): return None
        files = sorted(person_dir.glob("*.npy"))
        if not files: return None
        embeds = []
        for f in files:
            try:
                e = np.load(str(f))
                embeds.append(e.flatten())
            except Exception:
                pass
        if not embeds: return None
        return np.mean(embeds, axis=0)

    def identify_from_embed(self, embed: np.ndarray,
                             profiles: Dict[str, np.ndarray]) -> Tuple[Optional[str], float]:
        """임베딩으로 프로파일 검색 → (name or None, similarity)"""
        if not profiles or embed is None: return None, 0.0
        embed_dim = embed.shape[0]
        best_name, best_sim = None, 0.0
        for name, ref in profiles.items():
            if ref.shape[0] != embed_dim:
                continue  # 차원 불일치 (모델 변경 시) — 건너뜀
            sim = float(np.dot(embed, ref) /
                        (np.linalg.norm(embed) * np.linalg.norm(ref) + 1e-9))
            if sim > best_sim: best_sim, best_name = sim, name
        if best_sim >= IDENTIFY_THRESH and best_name:
            # 매칭 성공 시 샘플 자동 누적 (회의마다 프로파일 강화)
            self.save_embedding(best_name, embed)
            # 메모리 내 프로파일도 rolling average로 즉시 업데이트
            profiles[best_name] = profiles[best_name] * 0.85 + embed * 0.15
            return best_name, best_sim
        return None, best_sim

    def load_profiles(self) -> Dict[str, np.ndarray]:
        """모든 이름 폴더 스캔 → {name: 평균 임베딩}"""
        profiles = {}
        for person_dir in sorted(VOICES_DIR.iterdir()):
            if not person_dir.is_dir(): continue
            mean_embed = self._load_person_embeddings(person_dir.name)
            if mean_embed is not None:
                profiles[person_dir.name] = mean_embed
        return profiles

    def list_profiles(self) -> List[dict]:
        """프로파일 목록 + 샘플 수 반환"""
        result = []
        for person_dir in sorted(VOICES_DIR.iterdir()):
            if not person_dir.is_dir(): continue
            files = list(person_dir.glob("*.npy"))
            if files:
                result.append({
                    "name":    person_dir.name,
                    "samples": len(files),
                })
        return result

    def delete_profile(self, name: str):
        """이름 폴더 전체 삭제"""
        import shutil
        person_dir = VOICES_DIR / name
        if person_dir.is_dir():
            shutil.rmtree(str(person_dir))


# ──────────────── 엔진: MeetingRecorder ──────────────────
class MeetingRecorder:
    """
    회의 녹음/전사 엔진.
    on_event(type, data) 콜백으로 모든 이벤트 전달.
    터미널 모드: print 기반 콜백
    웹 모드: SSE push 콜백 (server.py에서 주입)
    """

    # MLX Whisper 모델명 매핑 (Apple Silicon GPU 전용)
    _MLX_MODEL_MAP = {
        "tiny":              "mlx-community/whisper-tiny-mlx",
        "base":              "mlx-community/whisper-base-mlx",
        "small":             "mlx-community/whisper-small-mlx",
        "medium":            "mlx-community/whisper-medium-mlx",
        "large-v3":          "mlx-community/whisper-large-v3-mlx",
        "large-v3-turbo":    "mlx-community/whisper-large-v3-turbo",
        "distil-large-v3":   "mlx-community/distil-whisper-large-v3",
    }

    # 클래스 변수: 최초 로딩 후 공유 (메모리 효율)
    _model = None          # faster-whisper WhisperModel 또는 None (MLX 시)
    _use_mlx: bool = False # MLX 백엔드 사용 여부

    @classmethod
    def load_model(cls):
        global WHISPER_PROMPT
        if cls._model is None and not cls._use_mlx:
            device, compute_type, label = _detect_whisper_backend()
            print(f"🤖 Whisper 모델 로딩 중... [{label}]")
            if device == "mlx":
                import mlx_whisper
                mlx_model = cls._MLX_MODEL_MAP.get(WHISPER_MODEL, WHISPER_MODEL)
                # 워밍업: 빈 오디오로 모델 로드 트리거
                silent = np.zeros(16000, dtype=np.float32)
                mlx_whisper.transcribe(silent, path_or_hf_repo=mlx_model, language="ko")
                cls._use_mlx = True
                cls._model = None  # MLX는 stateless, 매 호출 시 모델명 전달
                print(f"✅ MLX Whisper 준비 완료 ({WHISPER_MODEL}, Apple Silicon GPU)")
            else:
                cls._model = WhisperModel(WHISPER_MODEL, device=device,
                                          compute_type=compute_type,
                                          num_workers=2, cpu_threads=4)
                print(f"✅ 모델 준비 완료 ({WHISPER_MODEL}, {label})")
        # 한국어 기본 도메인 용어 추출 (터미널 모드 호환)
        WHISPER_PROMPT = build_whisper_prompt("ko")
        extracted = [w for w in WHISPER_PROMPT.replace(_WHISPER_BASE, "").split(", ") if w.strip()]
        if extracted:
            print(f"📝 도메인 용어 {len(extracted)}개 자동 등록: {', '.join(extracted[:5])}{'...' if len(extracted)>5 else ''}\n")
        return cls._model

    def __init__(
        self,
        mode: int,
        participants: List[str],
        on_event: Callable[[str, dict], None],
        enrolled_embeddings: Optional[Dict[str, np.ndarray]] = None,
        vpm: Optional[VoiceProfileManager] = None,
        language: str = "ko",
        template: str = "general",
        chunk_seconds: int = CHUNK_SECONDS,
        device_id: Optional[int] = None,
        extra_device_ids: Optional[List[int]] = None,
        output_dir: Optional[Path] = None,
    ):
        self.mode           = mode
        self.participants   = participants
        self.on_event       = on_event
        self.language       = language
        self.template       = template
        self.chunk_seconds  = chunk_seconds
        self.device_id      = device_id
        self.extra_device_ids: List[int] = extra_device_ids or []
        self.output_dir     = output_dir or OUTPUT_DIR  # Obsidian vault 설정 시 그 경로 사용
        self.lang_cfg       = LANGUAGE_CONFIGS.get(language, LANGUAGE_CONFIGS["ko"])
        self.tmpl_cfg       = MEETING_TEMPLATES.get(template, MEETING_TEMPLATES["general"])
        self.model          = self.load_model()
        self.whisper_prompt = build_whisper_prompt(language)
        self._context_prompt: str = ""   # 이전 청크 마지막 발화 (맥락 연속성)

        # 목소리
        encoder           = VoiceEncoder() if HAS_RESEMBLYZER else None
        self.vpm          = vpm or VoiceProfileManager(encoder)
        self.profiles     = self.vpm.load_profiles()

        # 모드 1: 세션 중 직접 등록한 임베딩
        self.enrolled: Dict[str, np.ndarray] = enrolled_embeddings or {}

        # 런타임 상태
        self.audio_q: queue.Queue = queue.Queue(maxsize=300)  # ~30초 분량
        self.running              = False
        self.paused               = False
        self._claude_pending      = 0
        self._claude_pending_lock = threading.Lock()
        self.md_path: Optional[Path] = None
        self.recent_lines: List[str] = []
        self.last_topic           = ""
        self.prev_context         = ""
        self._intervene_lock      = threading.Lock()
        self._last_intervene      = 0.0

        # 미등록 화자 추적
        self.unknown_clusters: Dict[str, List[np.ndarray]] = {}
        self.unknown_utterances: Dict[str, List[str]]      = {}

        # 전체 오디오 저장 (회의 후 화자 재매칭용)
        self._full_audio_chunks: List[np.ndarray] = []

        # 화자별 발화 시간 추적 (초)
        self.speaker_seconds: Dict[str, float] = {}
        # 연속 발화 병합용 (같은 화자 5초 이내 재발화 → 한 줄로)
        self._last_speaker: str = ""
        self._last_line_time: float = 0.0
        self._last_line_pos: int = 0      # MD 파일에서 마지막 발화 줄의 byte offset

        # 부분 전사
        self._partial_q: queue.Queue = queue.Queue(maxsize=2)
        self._model_lock = threading.Lock()
        self._noise_floor: float = 0.1  # 적응형 VAD 임계값 (노이즈 측정 후 자동 조정)

        self._load_prev_context()

    # ── 유틸 ──────────────────────────────────────────────
    def emit(self, type_: str, data: dict):
        self.on_event(type_, data)

    def _claude_inc(self):
        """Claude 호출 시작 — UI 스피너 활성화"""
        with self._claude_pending_lock:
            self._claude_pending += 1
            if self._claude_pending == 1:
                self.emit("claude_busy", {})

    def _claude_dec(self):
        """Claude 호출 완료 — UI 스피너 비활성화"""
        with self._claude_pending_lock:
            self._claude_pending = max(0, self._claude_pending - 1)
            if self._claude_pending == 0:
                self.emit("claude_idle", {})

    def _load_prev_context(self):
        files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
        if not files: return
        try:
            c = files[0].read_text(encoding="utf-8")
            if "# 🤖 AI 회의 분석" in c:
                s = c.split("# 🤖 AI 회의 분석")[-1]
                if "## 📋 회의 요약" in s:
                    self.prev_context = s.split("## 📋 회의 요약")[-1].split("##")[0].strip()
        except Exception:
            pass

    # ── 시작 ──────────────────────────────────────────────
    def start(self, _start_audio: bool = True):
        OUTPUT_DIR.mkdir(exist_ok=True)
        title        = datetime.now().strftime('%Y-%m-%d %H:%M') + " 회의"
        ts           = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.md_path = OUTPUT_DIR / f"meeting_{ts}.md"

        mode_label = {1: "사전 등록", 2: "자동 식별"}
        now_dt     = datetime.now()
        parts_yaml = json.dumps(self.participants, ensure_ascii=False) if self.participants else "[]"
        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(f"tags:\n  - meeting\n")
            f.write(f"date: {now_dt.strftime('%Y-%m-%d')}\n")
            f.write(f"time: \"{now_dt.strftime('%H:%M')}\"\n")
            f.write(f"participants: {parts_yaml}\n")
            f.write(f"template: {self.template}\n")
            f.write(f"language: {self.language}\n")
            f.write(f"mode: {mode_label.get(self.mode, '')}\n")
            f.write(f"status: in-progress\n")
            f.write("---\n\n")
            f.write(f"# {title}\n\n")
            if self.prev_context:
                f.write(f"**이전 회의 요약:**\n{self.prev_context}\n\n")
            f.write("---\n\n## 💬 대화 내용\n\n")

        self.running = True
        self.emit("started", {"title": title, "participants": self.participants,
                               "mode": self.mode, "md_file": self.md_path.name})

        # 이전 회의 미결 사항 브리핑 (비동기, UI 블로킹 없음)
        if self.prev_context:
            threading.Thread(target=self._make_briefing, daemon=True).start()

        # 터미널 모드(_start_audio=False)는 __main__에서 직접 오디오 관리
        if _start_audio:
            threading.Thread(target=self._measure_noise_floor, daemon=True).start()
            time.sleep(2.2)  # 소음 측정 완료 대기
            threading.Thread(target=self._audio_stream, daemon=True).start()
            for _dev in self.extra_device_ids:
                threading.Thread(target=self._audio_stream_extra, args=(_dev,), daemon=True).start()
        threading.Thread(target=self._transcribe_loop, daemon=True).start()
        threading.Thread(target=self._topic_loop,      daemon=True).start()
        threading.Thread(target=self._quality_loop,    daemon=True).start()
        threading.Thread(target=self._partial_loop,     daemon=True).start()

    # ── 오디오 ────────────────────────────────────────────
    def _audio_stream(self):
        def cb(indata, frames, time_info, status):
            if not self.paused:
                if self.audio_q.full():
                    try:
                        self.audio_q.get_nowait()
                    except queue.Empty:
                        pass
                self.audio_q.put_nowait(indata.copy())
        kwargs = dict(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                      callback=cb, blocksize=int(SAMPLE_RATE * 0.1))
        if self.device_id is not None:
            kwargs["device"] = self.device_id
        with sd.InputStream(**kwargs):
            while self.running: time.sleep(0.3)

    def _measure_noise_floor(self):
        """2초간 ambient noise RMS 측정 후 VAD 임계값 자동 조정"""
        try:
            self.emit("status", {"msg": "🎙 배경 소음 측정 중 (2초)..."})
            kwargs = dict(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
            if self.device_id is not None:
                kwargs["device"] = self.device_id
            audio = sd.rec(int(2 * SAMPLE_RATE), **kwargs)
            sd.wait()
            rms = float(np.sqrt(np.mean(audio ** 2)))
            # 조용한 환경(rms<0.005): 0.08, 시끄러운 환경(rms>0.05): 0.22
            self._noise_floor = max(0.08, min(0.22, rms * 4.0))
            self.emit("status", {"msg": f"✅ 소음 측정 완료 (VAD 임계값={self._noise_floor:.2f})"})
        except Exception:
            pass

    def _audio_stream_extra(self, dev_id: int):
        """추가 마이크 디바이스 → 동일 audio_q로 합류"""
        def cb(indata, frames, time_info, status):
            if not self.paused:
                if self.audio_q.full():
                    try:
                        self.audio_q.get_nowait()
                    except queue.Empty:
                        pass
                self.audio_q.put_nowait(indata.copy())
        kwargs = dict(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                      callback=cb, blocksize=int(SAMPLE_RATE * 0.1),
                      device=dev_id)
        try:
            with sd.InputStream(**kwargs):
                while self.running:
                    time.sleep(0.3)
        except Exception as e:
            print(f"[WARN] 추가 마이크 디바이스 {dev_id} 오류: {e}", file=sys.stderr)

    def push_audio_chunk(self, audio: np.ndarray):
        """브라우저 WebSocket에서 받은 오디오를 큐에 추가 (Docker/브라우저 마이크 모드)."""
        if not self.paused and self.running:
            chunk = audio.reshape(-1, 1).astype(np.float32)
            if self.audio_q.full():
                try:
                    self.audio_q.get_nowait()
                except queue.Empty:
                    pass
            self.audio_q.put_nowait(chunk)

    # ── 부분 전사 루프 (3초마다 미리보기) ─────────────────
    def _partial_loop(self):
        while self.running:
            try:
                audio = self._partial_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self.running:
                break
            # 정규화
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms > 1e-9:
                audio = audio * (0.05 / rms)
                audio = np.clip(audio, -1.0, 1.0)
            try:
                with self._model_lock:
                    segs, _ = self.model.transcribe(
                        audio,
                        language=self.lang_cfg["whisper_lang"],
                        initial_prompt=self.whisper_prompt,
                        beam_size=1,
                        best_of=1,
                        temperature=0.0,
                        vad_filter=True,
                    )
                text = " ".join(s.text for s in segs).strip()
                if text and audio.std() >= SILENCE_THRESH:
                    self.emit("partial_line", {"text": text})
            except Exception:
                pass

    # ── 전사 루프 ──────────────────────────────────────────
    def _transcribe_loop(self):
        buf: List[np.ndarray] = []
        t_start = time.time()
        t_partial = time.time()
        PARTIAL_SEC = 1.5
        # utterance 경계 감지용 (webrtcvad 있을 때)
        silence_frames = 0
        SILENCE_END_FRAMES = 6   # 30ms × 6 = 180ms 무음 → 청크 완성 판정
        MIN_CHUNK_SEC = 1.0      # 최소 청크 길이 (너무 짧은 발화 무시)

        while self.running:
            if self.paused:
                buf.clear()
                t_start = time.time()
                t_partial = time.time()
                silence_frames = 0
                time.sleep(0.3)
                continue
            try:
                chunk = self.audio_q.get(timeout=0.1)
                buf.append(chunk)
            except queue.Empty:
                pass

            # 부분 전사 트리거 (3초마다)
            if buf and time.time() - t_partial >= PARTIAL_SEC:
                t_partial = time.time()
                if self._partial_q.empty():
                    try:
                        preview = np.concatenate(buf, axis=0).flatten()
                        self._partial_q.put_nowait(preview)
                    except queue.Full:
                        pass

            elapsed = time.time() - t_start

            # utterance 경계 감지 (Silero VAD)
            if HAS_SILERO_VAD and buf and elapsed >= MIN_CHUNK_SEC:
                try:
                    import torch
                    last = torch.from_numpy(buf[-1].flatten())
                    confidence = _silero_model(last, SAMPLE_RATE).item()
                    if confidence >= 0.3:   # 발화 중
                        silence_frames = 0
                    else:                   # 침묵
                        silence_frames += 1
                except Exception:
                    silence_frames = 0

            # 청크 처리 조건: (1) 최대 청크 도달 OR (2) 발화 끝 감지
            max_reached = elapsed >= self.chunk_seconds
            utt_end = HAS_SILERO_VAD and silence_frames >= SILENCE_END_FRAMES and elapsed >= MIN_CHUNK_SEC

            if not (max_reached or utt_end):
                continue

            t_start = time.time()
            silence_frames = 0
            if not buf:
                continue
            audio = np.concatenate(buf, axis=0).flatten()
            buf.clear()
            # 전체 오디오 저장 (무음 포함 — 재생 시 타임스탬프 정확도 보장)
            self._full_audio_chunks.append(audio.copy())
            if audio.std() < SILENCE_THRESH:
                continue
            if not _vad_has_speech(audio, self._noise_floor):
                continue
            # 겹침 발화 분리 — 겹침 감지 시 분리 후 각각 처리
            if False and HAS_SEPARATOR and _has_overlap(audio):  # 비활성화: 중복 발화 생성 이슈
                separated = _separate_speakers(audio)
                if len(separated) > 1:
                    for sep_audio in separated:
                        self.audio_q.put_nowait(sep_audio.reshape(-1, 1).astype(np.float32))
                    continue  # 분리된 스트림이 큐에 들어가서 개별 처리됨
            # 노이즈 제거
            if HAS_NOISEREDUCE:
                try:
                    audio = nr.reduce_noise(y=audio, sr=SAMPLE_RATE,
                                            stationary=True, prop_decrease=0.75)
                except Exception:
                    pass
            # 오디오 정규화
            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms > 1e-9:
                audio = audio * (0.05 / rms)
                audio = np.clip(audio, -1.0, 1.0)
            # 단어 타임스탬프 (화자 교체 감지용, try 블록 내에서 설정됨)
            _word_times = []
            # 이전 청크 컨텍스트 + 어휘 프롬프트 합성 (224토큰 한도 고려)
            combined_prompt = self.whisper_prompt
            if self._context_prompt:
                combined_prompt = (self._context_prompt + " " + self.whisper_prompt)[-500:]

            try:
                if self.__class__._use_mlx:
                    import mlx_whisper
                    mlx_model = self.__class__._MLX_MODEL_MAP.get(WHISPER_MODEL, WHISPER_MODEL)
                    result = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=mlx_model,
                        language=self.lang_cfg["whisper_lang"],
                        initial_prompt=combined_prompt,
                        compression_ratio_threshold=1.8,
                        no_speech_threshold=0.6,
                        hallucination_silence_threshold=0.5,
                        condition_on_previous_text=True,
                        word_timestamps=True,
                    )
                    segs_raw = result.get("segments", [])
                    # MLX 결과를 신뢰도 필터 적용
                    text = " ".join(
                        s.get("text", "").strip()
                        for s in segs_raw
                        if s.get("no_speech_prob", 0) < 0.6
                        and s.get("compression_ratio", 0) < 1.8
                    ).strip()
                    # 단어 타임스탬프 수집 (화자 교체 감지용)
                    _word_times = []
                    for s in segs_raw:
                        for w in s.get("words", []):
                            _word_times.append({"word": w.get("word", ""), "start": w.get("start", 0), "end": w.get("end", 0)})
                else:
                    with self._model_lock:
                        segs, _ = self.model.transcribe(
                            audio, language=self.lang_cfg["whisper_lang"],
                            initial_prompt=combined_prompt,
                            beam_size=5,
                            best_of=5,
                            temperature=[0.0, 0.2, 0.4],   # fallback: 신뢰도 낮을 때 자동 재시도
                            condition_on_previous_text=False,
                            no_speech_threshold=0.6,
                            compression_ratio_threshold=1.8,
                            log_prob_threshold=-0.8,
                            vad_filter=True,
                            vad_parameters={
                                "threshold": 0.5,
                                "min_speech_duration_ms": 250,
                                "min_silence_duration_ms": 600,
                                "speech_pad_ms": 300,
                            },
                            word_timestamps=True,
                        )
                    # 신뢰도 낮은 세그먼트 제거 (할루시네이션 근본 차단)
                    reliable_segs = [
                        s for s in segs
                        if s.no_speech_prob < 0.6
                        and s.avg_logprob > -1.0
                        and s.compression_ratio < 1.8
                    ]
                    text = " ".join(s.text for s in reliable_segs).strip()
                    # 단어 타임스탬프 수집 (화자 교체 감지용)
                    _word_times = []
                    for s in reliable_segs:
                        for w in (s.words or []):
                            _word_times.append({"word": w.word, "start": w.start, "end": w.end})
            except Exception:
                continue
            if not text:
                continue
            text = re.sub(r'\.{2,}', '', text)
            text = re.sub(r'^[어음그저아]+[,\s]+', '', text.strip())
            text = _filter_hallucination(text)
            text = text.strip()
            if not text or set(text) <= {'.', ' ', '·'}:
                continue

            # 이전 발화와 동일한 텍스트 중복 방지
            if hasattr(self, '_last_text') and text == self._last_text:
                continue
            self._last_text = text

            # 다음 청크를 위한 컨텍스트 프롬프트 업데이트 (마지막 50자)
            self._context_prompt = text[-50:]

            # 트리거 워드 감지 ("헤이 클로드 ...")
            _tm = _TRIGGER_PATTERN.search(text)
            if _tm and _tm.group(2).strip():
                _q = _tm.group(2).strip()
                threading.Thread(target=self.claude_request,
                                 args=(f"[음성 요청] {_q}",), daemon=True).start()

            # 단어 타임스탬프 기반 청크 내 화자 교체 감지 (비활성화: 중복 발화 생성 이슈)
            multi_speakers = None  # self._split_by_word_gaps(audio, text, _word_times)
            if multi_speakers and len(multi_speakers) > 1:
                # 여러 화자가 감지된 경우 각각 별도 라인으로 출력
                for sub_text, sub_speaker, sub_embed in multi_speakers:
                    now = datetime.now().strftime("%H:%M:%S")
                    sub_sec = len(audio) / SAMPLE_RATE / len(multi_speakers)
                    self.speaker_seconds[sub_speaker] = self.speaker_seconds.get(sub_speaker, 0.0) + sub_sec
                    raw_line = f"**{now}** | **{sub_speaker}**: {sub_text}\n\n"
                    with open(self.md_path, "ab") as f_bin:
                        self._last_line_pos = f_bin.tell()
                    with open(self.md_path, "a", encoding="utf-8") as f:
                        f.write(raw_line)
                    self._last_speaker = sub_speaker
                    self._last_line_time = time.time()
                    self.emit("line", {"speaker": sub_speaker, "text": sub_text, "time": now, "seconds": round(sub_sec, 1)})
                    self.recent_lines.append(f"[{sub_speaker}]: {sub_text}")
                    if sub_speaker.startswith("미등록") and sub_embed is not None:
                        self.unknown_utterances.setdefault(sub_speaker, []).append(sub_text)
                        self.unknown_clusters.setdefault(sub_speaker, []).append(sub_embed)
                    threading.Thread(target=self._correct_async, args=(sub_text, sub_speaker, now, raw_line), daemon=True).start()
                continue

            speaker, embed = self._identify_speaker(audio)
            now            = datetime.now().strftime("%H:%M:%S")
            audio_sec      = len(audio) / SAMPLE_RATE
            self.speaker_seconds[speaker] = self.speaker_seconds.get(speaker, 0.0) + audio_sec

            # 연속 발화 병합: 같은 화자가 MERGE_GAP_SEC 이내 재발화 시 이전 줄에 이어붙임
            MERGE_GAP_SEC = 10.0
            now_ts = time.time()
            merged = (
                speaker == self._last_speaker
                and (now_ts - self._last_line_time) < MERGE_GAP_SEC
                and self._last_line_pos > 0
            )
            if merged:
                try:
                    with open(self.md_path, "r+", encoding="utf-8") as f:
                        f.seek(self._last_line_pos)
                        prev = f.read()
                        # 마지막 줄에서 \n\n 제거 후 텍스트 이어붙임
                        new_tail = prev.rstrip("\n") + " " + text + "\n\n"
                        f.seek(self._last_line_pos)
                        f.write(new_tail)
                        f.truncate()
                    self._last_line_time = now_ts
                    raw_line = None  # 병합 처리 완료
                except Exception:
                    merged = False

            if not merged:
                raw_line = f"**{now}** | **{speaker}**: {text}\n\n"
                with open(self.md_path, "ab") as f_bin:
                    self._last_line_pos = f_bin.tell()
                with open(self.md_path, "a", encoding="utf-8") as f:
                    f.write(raw_line)
                self._last_speaker   = speaker
                self._last_line_time = now_ts
            else:
                raw_line = f"**{now}** | **{speaker}**: {text}\n\n"

            self.emit("line", {"speaker": speaker, "text": text, "time": now, "seconds": round(audio_sec, 1)})

            self.recent_lines.append(f"[{speaker}]: {text}")
            if len(self.recent_lines) > 30:
                self.recent_lines.pop(0)

            if speaker.startswith("미등록"):
                self.unknown_utterances.setdefault(speaker, []).append(text)
                if embed is not None:
                    self.unknown_clusters.setdefault(speaker, []).append(embed)

            threading.Thread(target=self._correct_async,
                             args=(text, speaker, now, raw_line), daemon=True).start()
            threading.Thread(target=self._detect_new_terms,
                             args=(text,), daemon=True).start()
            threading.Thread(target=self._auto_intervene,
                             args=(text, speaker), daemon=True).start()

    # ── 발화자 식별 ────────────────────────────────────────
    def _identify_speaker(self, audio: np.ndarray) -> Tuple[str, Optional[np.ndarray]]:
        if not HAS_RESEMBLYZER:
            return self.participants[0] if self.participants else "미등록", None

        audio_sec = len(audio) / SAMPLE_RATE

        # 짧은 발화는 임베딩 추출 건너뜀 (부정확한 결과 방지)
        if audio_sec < MIN_EMBED_SECONDS:
            return self._last_speaker or (self.participants[0] if self.participants else "미등록"), None

        # 화자 변경 감지 (segmentation) — 변경 없으면 이전 화자 유지
        if HAS_PYANNOTE_SEG and _pyannote_segmenter is not None and self._last_speaker:
            try:
                import torch
                waveform = torch.from_numpy(audio).unsqueeze(0).float()
                output = _pyannote_segmenter({"waveform": waveform, "sample_rate": SAMPLE_RATE})
                # 세그먼트 수 확인 — 1개면 화자 변경 없음
                n_speakers = output.data.shape[-1] if hasattr(output, 'data') else 1
                speaker_activity = output.data.sum(axis=0) if hasattr(output, 'data') else None
                if speaker_activity is not None and n_speakers > 0:
                    active_count = (speaker_activity > 0.5).sum().item()
                    if active_count <= 1:
                        # 화자 변경 없음 → 이전 화자 유지 (임베딩 비교 건너뜀)
                        return self._last_speaker, None
            except Exception:
                pass  # 실패 시 기존 임베딩 방식으로 진행

        embed = self.vpm.embed_audio(audio)
        if embed is None:
            return self.participants[0] if self.participants else "미등록", None

        # ── 모드 1 ───────────────────────────────────────
        if self.mode == 1:
            # 1순위: 세션 등록 임베딩
            if self.enrolled:
                embed_dim = embed.shape[0]
                best_n, best_s = "", 0.0
                for name, ref in self.enrolled.items():
                    if ref.shape[0] != embed_dim:
                        continue
                    s = float(np.dot(embed, ref) /
                              (np.linalg.norm(embed) * np.linalg.norm(ref) + 1e-9))
                    if s > best_s: best_s, best_n = s, name
                if best_s >= CLUSTER_THRESH:
                    self.vpm.save_embedding(best_n, embed)  # 자동 누적
                    return best_n, embed

            # 2순위: voices/ 파일 fallback
            name, sim = self.vpm.identify_from_embed(embed, self.profiles)
            if name: return name, embed

            # 3순위: 클러스터링 → 미등록N
            return self._cluster(embed), embed

        # ── 모드 2 ───────────────────────────────────────
        else:
            # 1순위: voices/ 프로파일
            name, sim = self.vpm.identify_from_embed(embed, self.profiles)
            if name: return name, embed

            # 2순위: 클러스터링 → 미등록N
            return self._cluster(embed), embed

    def _cluster(self, embed: np.ndarray) -> str:
        """미매칭 목소리 실시간 클러스터링"""
        embed_dim = embed.shape[0]
        best_n, best_s = "", 0.0
        for label, refs in self.unknown_clusters.items():
            ref_mean = np.mean(refs, axis=0)
            if ref_mean.shape[0] != embed_dim:
                continue
            s = float(np.dot(embed, ref_mean) /
                      (np.linalg.norm(embed) * np.linalg.norm(ref_mean) + 1e-9))
            if s > best_s: best_s, best_n = s, label
        if best_s >= CLUSTER_THRESH:
            self.unknown_clusters[best_n].append(embed)
            return best_n
        # 클러스터 최대 10개, 각 클러스터 샘플 최대 50개 제한
        for lbl in list(self.unknown_clusters):
            if len(self.unknown_clusters[lbl]) > 50:
                self.unknown_clusters[lbl] = self.unknown_clusters[lbl][-50:]
        if len(self.unknown_clusters) >= 10:
            # 최대치 도달 시 가장 가까운 기존 클러스터에 할당
            if best_n:
                self.unknown_clusters[best_n].append(embed)
                return best_n
            return f"미등록{len(self.unknown_clusters)}"
        new_label = f"미등록{len(self.unknown_clusters)+1}"
        self.unknown_clusters[new_label] = [embed]
        self.emit("unknown_speaker", {"label": new_label})
        return new_label

    # ── STT 교정 (언어별) ─────────────────────────────────
    def _correct_async(self, raw, speaker, now, raw_line):
        self._claude_inc()
        try:
            prompt = self.lang_cfg["correct_prompt"].format(text=raw)
            corrected = claude_run(prompt, timeout=CLAUDE_TIMEOUT, model=CLAUDE_FAST_MODEL)
        finally:
            self._claude_dec()
        if not corrected or corrected == raw: return
        # Claude 메타 응답 감지 (교정 대신 질문/설명을 반환한 경우) → 원문 유지
        _meta_patterns = ["불완전한", "확인해주", "요청하시", "도와드리", "말씀해주", "알려주시",
                          "문맥에서", "교정을 제시", "설명해", "가능한 정정", "수정할 부분",
                          "무엇을 요청", "이해하기 어렵", "명확하지"]
        if any(p in corrected for p in _meta_patterns) and len(corrected) > len(raw) * 2:
            return  # 메타 응답 — 원문 유지
        safe_raw = raw.replace("-->", "->")
        corrected_line = (
            f"**{now}** | **{speaker}**: {corrected} "
            f"<!-- STT: {safe_raw} -->\n\n"
        )
        try:
            content = self.md_path.read_text(encoding="utf-8")
            if raw_line in content:
                self.md_path.write_text(content.replace(raw_line, corrected_line, 1), encoding="utf-8")
            self.emit("correction", {"speaker": speaker, "text": corrected,
                                      "original": raw, "time": now})
        except Exception:
            pass

    def _detect_new_terms(self, text: str):
        """발화에서 신규 기술 용어 실시간 감지 후 glossary에 누적"""
        found = list(set(re.findall(_TECH_WORD_PAT, text)))
        if not found:
            return
        existing = load_glossary()
        new_terms = [w for w in found if w not in existing and w not in _TECH_STOPWORDS]
        if new_terms:
            for t in new_terms:
                existing[t] = ""
            save_glossary(existing)
            self.emit("glossary_updated", {"terms": new_terms})

    # ── 자동 개입 (템플릿·언어별 패턴) ───────────────────
    def _auto_intervene(self, text, speaker):
        now_ts = time.time()
        with self._intervene_lock:
            if now_ts - self._last_intervene < 30: return
            matched = None
            for pattern, itype in self.lang_cfg["auto_intervene"]:
                if pattern.search(text): matched = itype; break
            if not matched: return
            self._last_intervene = now_ts

        base_prompt = self.lang_cfg["intervene_prompts"].get(matched, "관련 내용 간결히.")
        self._claude_inc()
        try:
            response = claude_run(
                f"회의 중 자동 개입.\n최근 대화:\n{chr(10).join(self.recent_lines[-10:])}\n\n"
                f"이전 요약: {self.prev_context[:200] or '없음'}\n\n"
                f"{base_prompt} {self.lang_cfg['reply_suffix']}",
                timeout=15, model=CLAUDE_FAST_MODEL
            )
        finally:
            self._claude_dec()
        if not response: return
        now = datetime.now().strftime("%H:%M:%S")
        with open(self.md_path, "a", encoding="utf-8") as f:
            f.write(f"\n> [!NOTE] 🤖 Claude [{now}]\n")
            for line in response.splitlines():
                f.write(f"> {line}\n")
            f.write("\n")
        self.emit("claude_auto", {"text": response, "time": now})

    # ── 주제 감지 ──────────────────────────────────────────
    def _topic_loop(self):
        time.sleep(TOPIC_INTERVAL)
        while self.running:
            t0 = time.time()
            if len(self.recent_lines) >= 5 and not self.paused:
                prompt = self.lang_cfg["topic_prompt"].format(
                    prev=self.last_topic,
                    lines=chr(10).join(self.recent_lines[-15:])
                )
                self._claude_inc()
                try:
                    topic = claude_run(prompt, timeout=10, model=CLAUDE_FAST_MODEL)
                finally:
                    self._claude_dec()
                none_word = self.lang_cfg["topic_none"]
                if topic and topic != none_word and topic != self.last_topic:
                    self.last_topic = topic
                    now = datetime.now().strftime("%H:%M")
                    with open(self.md_path, "a", encoding="utf-8") as f:
                        f.write(f"\n## 📍 [{now}] {topic}\n\n")
                    self.emit("topic", {"topic": topic, "time": now})
                    self.recent_lines.clear()
            time.sleep(max(0, TOPIC_INTERVAL - (time.time() - t0)))

    # ── 품질 피드백 ────────────────────────────────────────
    def _quality_loop(self):
        time.sleep(QUALITY_INTERVAL)
        while self.running:
            t0 = time.time()
            if len(self.recent_lines) >= 5 and not self.paused:
                quality_fmt = self.lang_cfg["quality_format"]
                self._claude_inc()
                try:
                    feedback = claude_run(
                        f"회의 품질 분석. {self.lang_cfg['reply_suffix']}\n"
                        f"대화:\n{chr(10).join(self.recent_lines)}\n\n"
                        f"형식:\n{quality_fmt}",
                        timeout=20
                    )
                finally:
                    self._claude_dec()
                if feedback:
                    now = datetime.now().strftime("%H:%M")
                    with open(self.md_path, "a", encoding="utf-8") as f:
                        f.write(f"\n> [!TIP]+ 📊 품질 피드백 [{now}]\n")
                        for line in feedback.splitlines():
                            f.write(f"> {line}\n")
                        f.write("\n")
                    self.emit("quality", {"content": feedback, "time": now})
            time.sleep(max(0, QUALITY_INTERVAL - (time.time() - t0)))

    # ── Claude 요청 ────────────────────────────────────────
    def claude_request(self, command: str) -> str:
        now = datetime.now().strftime("%H:%M:%S")
        try: content = self.md_path.read_text(encoding="utf-8")
        except Exception: content = ""
        with open(self.md_path, "a", encoding="utf-8") as f:
            f.write(f"**{now}** | 🎤 **[Claude 요청]**: {command}\n\n")
        self.emit("line", {"speaker": "🎤 요청", "text": command, "time": now})
        self._claude_inc()
        try:
            response = claude_run(
                f"현재 진행 중 회의록:\n{content[-2000:]}\n\n[{now}] 요청: {command}", timeout=60
            )
        finally:
            self._claude_dec()
        if response:
            resp_now = datetime.now().strftime("%H:%M:%S")
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(f"\n> [!NOTE] 🤖 Claude [{resp_now}]\n")
                for line in response.splitlines():
                    f.write(f"> {line}\n")
                f.write("\n")
            self.emit("claude_response", {"text": response, "time": now})
        return response

    # ── 이전 회의 브리핑 ───────────────────────────────────
    def _make_briefing(self):
        """이전 회의 미결 사항 & 액션 아이템을 비동기로 요약해 표시"""
        lang = self.language
        if lang == "ja":
            prompt = (
                "前回の会議の要約から、未解決事項とフォローアップが必要な"
                "アクションアイテムを3行以内で要約:\n" + self.prev_context
            )
        elif lang == "en":
            prompt = (
                "From the previous meeting summary, list unresolved items "
                "and action items needing follow-up (3 lines max):\n"
                + self.prev_context
            )
        else:
            prompt = (
                "이전 회의 요약에서 미결 사항과 팔로업이 필요한 액션 아이템만 "
                "3줄 이내로 요약:\n" + self.prev_context
            )
        result = claude_run(prompt, timeout=15, model=CLAUDE_FAST_MODEL)
        if result:
            self.emit("briefing", {"content": result})

    # ── 종료 + 요약 ────────────────────────────────────────
    def _rematch_speakers(self):
        """회의 종료 후 전체 오디오로 화자 재매칭 — 실시간 오인식 보정."""
        import torch

        full_audio = np.concatenate(self._full_audio_chunks)
        duration = len(full_audio) / SAMPLE_RATE
        print(f"[INFO] 화자 재매칭 시작: {duration:.0f}초 오디오", file=sys.stderr)

        # 전체 오디오에서 청크별 임베딩 재추출 (5초 단위)
        chunk_size = 5 * SAMPLE_RATE
        embeddings = []
        for i in range(0, len(full_audio), chunk_size):
            chunk = full_audio[i:i + chunk_size]
            if len(chunk) < SAMPLE_RATE:  # 1초 미만 건너뜀
                continue
            embed = self.vpm.embed_audio(chunk)
            if embed is not None:
                embeddings.append((i / SAMPLE_RATE, embed))

        if not embeddings:
            return

        # 등록된 프로파일 + 세션 프로파일로 재매칭
        all_profiles = {**self.profiles, **self.enrolled}
        if not all_profiles:
            return

        # 각 청크의 화자 판정 (더 엄격한 임계값)
        rematch_map: Dict[float, str] = {}
        for t, embed in embeddings:
            best_name, best_sim = None, 0.0
            for name, ref in all_profiles.items():
                sim = float(np.dot(embed, ref) /
                            (np.linalg.norm(embed) * np.linalg.norm(ref) + 1e-9))
                if sim > best_sim:
                    best_sim, best_name = sim, name
            if best_sim >= CLUSTER_THRESH and best_name:
                rematch_map[t] = best_name

        if not rematch_map:
            return

        # MD 파일에서 타임스탬프 기반 화자 교체
        try:
            content = self.md_path.read_text(encoding="utf-8")
            import re as _re
            lines = content.split("\n")
            corrected = 0
            for i, line in enumerate(lines):
                m = _re.match(r'\*\*(\d{2}:\d{2}:\d{2})\*\* \| \*\*(.+?)\*\*: (.+)', line)
                if not m:
                    continue
                time_str, old_speaker, text = m.group(1), m.group(2), m.group(3)
                # 시간 → 초 변환
                h, mn, s = time_str.split(":")
                # 회의 시작 시간 기준 오프셋 (대략적)
                # rematch_map의 키는 오디오 시작부터의 초
                # 가장 가까운 rematch 결과 찾기
                line_sec = int(h) * 3600 + int(mn) * 60 + int(s)
                start_m = _re.search(r'\*\*(\d{2}:\d{2}:\d{2})\*\* \|', content)
                if start_m:
                    sh, smn, ss = start_m.group(1).split(":")
                    start_sec = int(sh) * 3600 + int(smn) * 60 + int(ss)
                    offset = line_sec - start_sec
                else:
                    continue

                # 해당 시간대의 재매칭 결과 확인 (±3초 범위)
                best_match = None
                best_dist = float('inf')
                for t, name in rematch_map.items():
                    dist = abs(t - offset)
                    if dist < best_dist and dist <= 3.0:
                        best_dist, best_match = dist, name

                if best_match and best_match != old_speaker and not old_speaker.startswith("미등록"):
                    # 등록된 화자 간 교정만 수행 (미등록→등록 교정은 하지 않음)
                    if old_speaker in all_profiles:
                        lines[i] = line.replace(f"**{old_speaker}**", f"**{best_match}**", 1)
                        corrected += 1

            if corrected > 0:
                self.md_path.write_text("\n".join(lines), encoding="utf-8")
                print(f"[INFO] 화자 재매칭 완료: {corrected}건 교정", file=sys.stderr)
                self.emit("status", {"msg": f"✅ 화자 재매칭 {corrected}건 교정"})
            else:
                print("[INFO] 화자 재매칭: 교정 필요 없음", file=sys.stderr)
        except Exception as exc:
            print(f"[WARN] 화자 재매칭 MD 업데이트 실패: {exc}", file=sys.stderr)

    def _split_by_word_gaps(self, audio: np.ndarray, text: str,
                            word_times: List[dict]) -> Optional[List[Tuple[str, str, Optional[np.ndarray]]]]:
        """단어 타임스탬프의 침묵 구간(0.8초+)에서 화자 교체 감지.
        교체가 있으면 [(text, speaker, embed), ...] 반환, 없으면 None."""
        if not word_times or len(word_times) < 4:
            return None

        audio_sec = len(audio) / SAMPLE_RATE
        if audio_sec < 3.0:
            return None

        # 침묵 구간 찾기 (0.8초 이상)
        GAP_THRESH = 0.8
        split_points = []
        for i in range(1, len(word_times)):
            gap = word_times[i]["start"] - word_times[i-1]["end"]
            if gap >= GAP_THRESH:
                split_points.append(i)

        if not split_points:
            return None

        # 분할 지점에서 오디오 잘라 화자 확인
        segments = []
        prev_idx = 0
        for sp in split_points:
            segments.append((prev_idx, sp))
            prev_idx = sp
        segments.append((prev_idx, len(word_times)))

        # 각 세그먼트별 화자 식별
        results = []
        speakers_found = set()
        for start_wi, end_wi in segments:
            seg_words = word_times[start_wi:end_wi]
            if not seg_words:
                continue
            seg_text = "".join(w["word"] for w in seg_words).strip()
            if not seg_text:
                continue

            # 오디오 슬라이스
            t_start = max(0, seg_words[0]["start"])
            t_end = min(audio_sec, seg_words[-1]["end"])
            s_start = int(t_start * SAMPLE_RATE)
            s_end = int(t_end * SAMPLE_RATE)
            sub_audio = audio[s_start:s_end]

            if len(sub_audio) < MIN_EMBED_SECONDS * SAMPLE_RATE:
                results.append((seg_text, self._last_speaker or "?", None))
                continue

            speaker, embed = self._identify_speaker(sub_audio)
            speakers_found.add(speaker)
            results.append((seg_text, speaker, embed))

        # 화자가 실제로 다른 경우에만 분할 반환
        if len(speakers_found) <= 1:
            return None

        return results

    def _save_audio_mp3(self) -> Optional[Path]:
        """전체 오디오를 MP3로 저장. 반환: MP3 파일 경로."""
        import wave, io, subprocess as _sp
        full_audio = np.concatenate(self._full_audio_chunks)
        duration = len(full_audio) / SAMPLE_RATE

        # meetings/ 폴더에 같은 이름으로 저장
        mp3_path = self.md_path.with_suffix(".mp3")
        wav_path = self.md_path.with_suffix(".wav")

        # WAV 임시 저장
        audio_int16 = (full_audio * 32767).astype(np.int16)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())

        # ffmpeg로 MP3 변환 (있으면)
        try:
            _sp.run(
                ["ffmpeg", "-y", "-i", str(wav_path), "-b:a", "128k", "-ac", "1", str(mp3_path)],
                capture_output=True, timeout=120,
            )
            if mp3_path.exists() and mp3_path.stat().st_size > 0:
                wav_path.unlink(missing_ok=True)  # WAV 삭제
                size_mb = mp3_path.stat().st_size / 1024 / 1024
                print(f"[INFO] 회의 녹음 저장: {mp3_path.name} ({size_mb:.1f}MB, {duration:.0f}초)", file=sys.stderr)
                self.emit("audio_saved", {"file": mp3_path.name, "duration": round(duration)})
                return mp3_path
        except (FileNotFoundError, _sp.TimeoutExpired):
            pass

        # ffmpeg 없으면 WAV 유지
        if wav_path.exists():
            size_mb = wav_path.stat().st_size / 1024 / 1024
            print(f"[INFO] 회의 녹음 저장 (WAV): {wav_path.name} ({size_mb:.1f}MB, {duration:.0f}초)", file=sys.stderr)
            self.emit("audio_saved", {"file": wav_path.name, "duration": round(duration)})
            return wav_path
        return None

    def _ai_full_correction(self):
        """회의 종료 후 전체 대화 내용을 AI가 맥락 기반으로 교정."""
        import re as _re
        content = self.md_path.read_text(encoding="utf-8")

        # 대화 부분만 추출
        lines = content.split("\n")
        pattern = _re.compile(r'\*\*(\d{2}:\d{2}:\d{2})\*\* \| \*\*(.+?)\*\*: (.+)')
        utterances = []
        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m:
                utterances.append({
                    "idx": i,
                    "time": m.group(1),
                    "speaker": m.group(2),
                    "text": m.group(3),
                })

        if not utterances:
            return

        # 20개씩 배치로 나눠서 교정 (프롬프트 길이 제한)
        BATCH_SIZE = 20
        corrected_count = 0

        for batch_start in range(0, len(utterances), BATCH_SIZE):
            batch = utterances[batch_start:batch_start + BATCH_SIZE]
            batch_text = "\n".join(
                f"{u['time']} [{u['speaker']}] {u['text']}" for u in batch
            )

            prompt = (
                "다음은 음성인식(STT)으로 생성된 회의 대화입니다. "
                "전체 맥락을 보고 오인식된 단어만 교정해주세요.\n\n"
                "## 교정 규칙\n"
                "- 고유명사, 전문용어, 동음이의어 오류만 수정\n"
                "- 문맥상 부자연스러운 단어를 자연스럽게 교정\n"
                "- 원문의 의미와 구조를 절대 변경하지 마세요\n"
                "- 교정이 불필요한 줄은 그대로 유지\n"
                "- 각 줄의 형식 'HH:MM:SS [화자] 텍스트'를 그대로 유지\n"
                "- 교정된 텍스트만 출력하세요\n\n"
                f"## 원본\n\n{batch_text}"
            )

            result = claude_run(prompt, timeout=60, retries=1,
                                model=CLAUDE_FAST_MODEL)
            if not result:
                continue

            # 교정 결과 파싱
            corrected_lines = result.strip().split("\n")
            corrected_map = {}
            for cl in corrected_lines:
                cm = _re.match(r'(\d{2}:\d{2}:\d{2})\s*\[(.+?)\]\s*(.+)', cl.strip())
                if cm:
                    corrected_map[cm.group(1)] = cm.group(3).strip()

            # 원본과 비교 후 변경된 것만 적용
            for u in batch:
                if u["time"] in corrected_map:
                    new_text = corrected_map[u["time"]]
                    if new_text != u["text"] and len(new_text) > 0:
                        old_line = f"**{u['time']}** | **{u['speaker']}**: {u['text']}"
                        new_line = f"**{u['time']}** | **{u['speaker']}**: {new_text}"
                        lines[u["idx"]] = new_line
                        corrected_count += 1
                        # 교정 학습에도 저장
                        save_correction(u["text"], new_text)

        if corrected_count > 0:
            self.md_path.write_text("\n".join(lines), encoding="utf-8")
            print(f"[INFO] 전체 대화 AI 교정 완료: {corrected_count}건", file=sys.stderr)
            self.emit("status", {"msg": f"✅ 전체 대화 교정 {corrected_count}건"})
        else:
            print("[INFO] 전체 대화 AI 교정: 교정 필요 없음", file=sys.stderr)

    def _merge_consecutive_speakers(self):
        """회의 종료 후 연속 같은 화자 발화를 하나로 병합 (가독성 향상)."""
        import re as _re
        content = self.md_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        pattern = _re.compile(r'\*\*(\d{2}:\d{2}:\d{2})\*\* \| \*\*(.+?)\*\*: (.+)')

        merged_lines = []
        prev_speaker = None
        prev_idx = -1

        for i, line in enumerate(lines):
            m = pattern.match(line)
            if m:
                time_str, speaker, text = m.group(1), m.group(2), m.group(3)
                if speaker == prev_speaker and prev_idx >= 0:
                    # 이전 줄에 텍스트 이어붙이기
                    merged_lines[prev_idx] = merged_lines[prev_idx].rstrip() + " " + text
                    # 빈 줄(이전 발화 뒤의 빈 줄)은 건너뜀
                    continue
                else:
                    prev_speaker = speaker
                    prev_idx = len(merged_lines)
                    merged_lines.append(line)
            else:
                # 빈 줄이고 다음에 같은 화자가 오면 건너뜀 (병합 시 중간 빈 줄 제거)
                if line.strip() == "" and prev_speaker and prev_idx >= 0:
                    # 빈 줄은 일단 추가하되, 다음 줄에서 병합되면 제거됨
                    merged_lines.append(line)
                else:
                    prev_speaker = None
                    prev_idx = -1
                    merged_lines.append(line)

        new_content = "\n".join(merged_lines)
        if new_content != content:
            # 연속 빈 줄 3개 이상 → 2개로 정리
            new_content = _re.sub(r'\n{4,}', '\n\n\n', new_content)
            self.md_path.write_text(new_content, encoding="utf-8")
            orig_count = len(pattern.findall(content))
            new_count = len(pattern.findall(new_content))
            merged = orig_count - new_count
            if merged > 0:
                print(f"[INFO] 발화 병합 완료: {merged}건 병합 ({orig_count} → {new_count}줄)", file=sys.stderr)
                self.emit("status", {"msg": f"✅ 발화 병합 {merged}건"})

    def stop(self):
        self.running = False

    def finalize(self) -> Tuple[str, List[dict]]:
        """회의 종료 처리. 반환: (summary, unknown_speakers_list)"""
        now = datetime.now().strftime("%H:%M:%S")
        try:
            speaking_stats = self._build_speaking_stats()
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(f"\n---\n\n## 🏁 회의 종료\n\n**종료 시간:** {now}\n\n")
                if speaking_stats:
                    f.write("### 🎙️ 발화 통계\n\n")
                    f.write("| 참여자 | 발화 시간 | 비율 |\n")
                    f.write("|--------|----------|------|\n")
                    for name, secs, ratio in speaking_stats:
                        bar = "█" * round(ratio * 20) + "░" * (20 - round(ratio * 20))
                        f.write(f"| {name} | {int(secs//60)}분 {int(secs%60)}초 | {bar} {ratio:.0%} |\n")
                    f.write("\n")
        except Exception:
            pass

        # 미등록 화자 정보 — 요약과 독립적으로 항상 수집
        unknowns = [
            {"label": label,
             "count": len(embeds),
             "utterances": self.unknown_utterances.get(label, [])[:3],
             "embeddings": embeds}
            for label, embeds in self.unknown_clusters.items()
            if embeds  # 임베딩이 있는 것만
        ]

        # 전체 오디오 MP3 저장
        audio_file = None
        if self._full_audio_chunks and self.md_path:
            self.emit("status", {"msg": "🎵 회의 녹음 저장 중..."})
            try:
                audio_file = self._save_audio_mp3()
            except Exception as exc:
                print(f"[WARN] 오디오 저장 실패: {exc}", file=sys.stderr)

        # 회의 후 전체 오디오로 화자 재매칭 (정확도 향상)
        if self._full_audio_chunks and HAS_PYANNOTE and self.md_path:
            self.emit("status", {"msg": "🔄 화자 재매칭 중 (전체 오디오 분석)..."})
            try:
                self._rematch_speakers()
            except Exception as exc:
                print(f"[WARN] 화자 재매칭 실패: {exc}", file=sys.stderr)
            self._full_audio_chunks.clear()

        # 회의 후 연속 같은 화자 발화 병합 (가독성 향상)
        if self.md_path and self.md_path.exists():
            try:
                self._merge_consecutive_speakers()
            except Exception as exc:
                print(f"[WARN] 발화 병합 실패: {exc}", file=sys.stderr)

        # 회의 후 전체 대화 AI 교정 (전체 맥락 기반 STT 오인식 보정)
        if self.md_path and self.md_path.exists():
            self.emit("status", {"msg": "✏️ AI 전체 대화 교정 중..."})
            try:
                self._ai_full_correction()
            except Exception as exc:
                print(f"[WARN] 전체 대화 교정 실패: {exc}", file=sys.stderr)

        # AI 요약 (실패해도 unknowns는 반환)
        summary = ""
        self.emit("status", {"msg": "📝 AI 회의 분석 중..."})
        try:
            content = self.md_path.read_text(encoding="utf-8")
            summary = claude_run(
                self.tmpl_cfg["prompt"].format(content=content),
                timeout=120,
                retries=3
            )
            if not summary:
                self.emit("error", {"msg": "⚠️ AI 요약 생성 실패 — Claude CLI 응답 없음. 회의록은 저장됐습니다."})
            if summary:
                with open(self.md_path, "a", encoding="utf-8") as f:
                    f.write("---\n\n# 🤖 AI 회의 분석\n\n" + summary + "\n")
                self.emit("summary", {
                    "content": summary,
                    "template": self.template,
                    "template_name": self.tmpl_cfg["name"],
                })
        except Exception:
            pass

        # frontmatter status 업데이트 + 자동 제목 생성
        if self.md_path and self.md_path.exists():
            try:
                raw = self.md_path.read_text(encoding="utf-8")
                updated = raw.replace("status: in-progress", "status: complete", 1)
                if updated != raw:
                    self.md_path.write_text(updated, encoding="utf-8")
            except Exception:
                pass
            # 자동 제목 생성 (요약 완료 후 비동기)
            threading.Thread(target=self._generate_title, daemon=True).start()

        # vocab 누적 업데이트 + 용어집 자동 추출 (백그라운드)
        if self.md_path and self.md_path.exists():
            _md = self.md_path
            _lang = self.language
            threading.Thread(
                target=update_vocab_from_meeting,
                args=(_md, _lang),
                daemon=True
            ).start()
            threading.Thread(
                target=extract_glossary_from_meeting,
                args=(_md,),
                daemon=True
            ).start()

        if unknowns:
            self.emit("unknown_speakers_found", {"speakers": [
                {k: v for k, v in u.items() if k != "embeddings"} for u in unknowns
            ]})
        # unknowns 여부와 관계없이 항상 finished 발행
        # (unknown_speakers_found 이후 패널을 닫으면 api_finish_unknown이 한 번 더 보내지만 무해)
        self.emit("finished", {"md_file": self.md_path.name if self.md_path else ""})

        return summary, unknowns

    def _build_speaking_stats(self) -> List[tuple]:
        """화자별 발화 시간 통계 계산. (name, seconds, ratio) 리스트 반환 (내림차순)."""
        if not self.speaker_seconds:
            return []
        total = sum(self.speaker_seconds.values())
        if total == 0:
            return []
        stats = [
            (name, secs, round(secs / total, 3))
            for name, secs in self.speaker_seconds.items()
        ]
        stats.sort(key=lambda x: x[1], reverse=True)
        return stats

    def _generate_title(self):
        """회의 내용 기반 자동 제목 생성 — frontmatter title 업데이트"""
        if not self.md_path or not self.md_path.exists():
            return
        try:
            content = self.md_path.read_text(encoding="utf-8")
            # 대화 내용 일부만 사용 (frontmatter 제외)
            body = content.split("## 💬 대화 내용")[-1][:3000] if "## 💬 대화 내용" in content else content[:3000]

            lang = self.language
            if lang == "ja":
                prompt = f"以下の会議の内容を15文字以内の日本語タイトルにしてください。タイトルのみ出力:\n{body}"
            elif lang == "en":
                prompt = f"Summarize this meeting in a title under 50 characters. Output title only:\n{body}"
            else:
                prompt = f"다음 회의 내용을 15자 이내 한국어 제목으로 요약해줘. 제목만 출력:\n{body}"

            title = claude_run(prompt, timeout=15)
            if not title or len(title) > 60:
                return
            title = title.strip().strip('"\'')

            # frontmatter의 title 필드 업데이트 (없으면 추가)
            raw = self.md_path.read_text(encoding="utf-8")
            if "title:" in raw:
                import re as _re
                updated = _re.sub(r'^title:.*$', f'title: "{title}"', raw, count=1, flags=_re.MULTILINE)
            else:
                # status 줄 뒤에 삽입
                updated = raw.replace("status: complete\n", f"status: complete\ntitle: \"{title}\"\n", 1)
            self.md_path.write_text(updated, encoding="utf-8")
            self.emit("title_generated", {"title": title, "md_file": self.md_path.name})
        except Exception:
            pass

    def register_unknown(self, label: str, name: str):
        """미등록 화자 프로파일 등록"""
        if label in self.unknown_clusters:
            for embed in self.unknown_clusters[label]:
                self.vpm.save_embedding(name, embed)

    # ── 일시정지 / 재개 ────────────────────────────────────
    def pause(self):
        self.paused = True
        # 일시정지 중 쌓인 오디오 폐기
        while not self.audio_q.empty():
            try: self.audio_q.get_nowait()
            except: break
        now = datetime.now().strftime("%H:%M:%S")
        if self.md_path:
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(f"\n> ⏸️ **[{now}] 일시정지**\n\n")
        self.emit("paused", {"time": now})

    def resume(self):
        self.paused = False
        now = datetime.now().strftime("%H:%M:%S")
        if self.md_path:
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(f"\n> ▶️ **[{now}] 재개**\n\n")
        self.emit("resumed", {"time": now})

    # ── 다음 회의 캘린더 등록 ──────────────────────────────
    def schedule_next_meeting(self, summary: str) -> str:
        """AI 분석에서 다음 회의 안건 추출 후 Google Calendar에 이벤트 생성"""
        if not summary:
            return ""
        self.emit("status", {"msg": "📅 다음 회의 캘린더 등록 중..."})
        members = ", ".join(self.participants) if self.participants else "미지정"
        prompt = self.lang_cfg["schedule_prompt"].format(
            members=members,
            summary=summary[-1500:]
        )
        result = claude_run(prompt, timeout=60)
        if result:
            self.emit("calendar_done", {"content": result})
            if self.md_path and self.md_path.exists():
                with open(self.md_path, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n\n## 📅 캘린더 등록 결과\n\n{result}\n")
        return result

    def create_asana_tasks(self, summary: str) -> str:
        """
        AI 분석 요약에서 액션 아이템을 추출해 Asana에 자동 등록.
        claude_run()이 MCP 도구를 사용하므로 Asana API 직접 접근 가능.
        """
        if not summary:
            return ""

        # 액션 아이템 섹션만 추출
        action_section = ""
        if "## 📌 액션 아이템" in summary:
            action_section = summary.split("## 📌 액션 아이템")[-1].split("##")[0].strip()
        elif "액션 아이템" in summary:
            action_section = summary.split("액션 아이템")[-1].split("##")[0].strip()

        if not action_section or len(action_section) < 10:
            self.emit("asana_skip", {"msg": "등록할 액션 아이템 없음"})
            return ""

        self.emit("status", {"msg": "📋 Asana 태스크 등록 중..."})

        title   = self.md_path.stem.replace("meeting_", "").replace("_", " ") if self.md_path else ""
        members = ", ".join(self.participants) if self.participants else "미지정"

        prompt = (
            f"다음 회의 액션 아이템을 Asana에 태스크로 등록해줘.\n\n"
            f"회의 정보:\n"
            f"- 날짜/제목: {title}\n"
            f"- 참여자: {members}\n\n"
            f"액션 아이템:\n{action_section}\n\n"
            f"지시사항:\n"
            f"1. 각 항목을 개별 Asana 태스크로 생성해줘\n"
            f"2. 담당자가 명시된 경우 assignee 설정 (Asana 멤버 이름으로 검색)\n"
            f"3. 마감일이 있으면 due_date 설정\n"
            f"4. 적절한 프로젝트가 있으면 거기에 등록, 없으면 내 태스크로 등록\n"
            f"5. 완료 후 생성된 태스크 목록을 한국어로 간결하게 알려줘\n"
        )

        result = claude_run(prompt, timeout=60)

        if result:
            self.emit("asana_done", {"content": result})
            # MD 파일에도 기록
            if self.md_path and self.md_path.exists():
                with open(self.md_path, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n\n## 📋 Asana 등록 결과\n\n{result}\n")
        return result


# ──────────────── 터미널 모드 ─────────────────────────────
def _disable_echo():
    if not HAS_TERMIOS:
        return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        return old
    except Exception:
        return None

def _restore_echo(old):
    if old and HAS_TERMIOS:
        try: termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        except Exception: pass


def terminal_event_handler(type_: str, data: dict):
    """터미널용 이벤트 핸들러"""
    if type_ == "line":
        print(f"  {data['time']} [{data['speaker']}] {data['text']}")
    elif type_ == "correction":
        print(f"  ✏️  [{data['speaker']}] {data['text']}")
    elif type_ == "claude_auto":
        print(f"\n  {'━'*42}")
        print(f"  🤖 Claude 자동 개입 [{data['time']}]")
        for line in data['text'].splitlines():
            print(f"  │ {line}")
        print(f"  {'━'*42}\n")
    elif type_ == "claude_response":
        print(f"\n  ┌─ 🤖 Claude ──────────────────────────")
        for line in data['text'].splitlines():
            print(f"  │ {line}")
        print(f"  └──────────────────────────────────────\n")
    elif type_ == "topic":
        print(f"\n  📍 [{data['time']}] {data['topic']}\n")
    elif type_ == "quality":
        print(f"\n  {'━'*42}")
        print(f"  📊 품질 피드백 [{data['time']}]")
        for line in data['content'].splitlines():
            print(f"  {line}")
        print(f"  {'━'*42}\n")
    elif type_ == "briefing":
        print(f"\n{'━'*50}")
        print("  📋 회의 전 브리핑")
        print('━'*50)
        print(data['content'])
        print('━'*50 + "\n")
    elif type_ == "summary":
        print("\n  ✅ AI 분석 완료")
    elif type_ == "status":
        print(f"  {data['msg']}")
    elif type_ == "started":
        print(f"\n📄 회의 파일: {data['md_file']}")
        print("━"*50)
        print("🎙️  회의 시작!")
        print("   [c] 누르는 동안  →  Claude 요청 녹음")
        print("   [c] 떼면         →  전송")
        print("   [q]              →  회의 종료")
        print("━"*50 + "\n")
    elif type_ == "unknown_speaker":
        print(f"  👤 새 미등록 화자 감지: {data['label']}")


def terminal_post_meeting(recorder: MeetingRecorder, unknowns: List[dict]):
    """터미널: 회의 후 미등록 화자 등록"""
    print(f"\n💡 파일: {recorder.md_path.resolve()}")

    if not unknowns:
        return

    try:
        print(f"\n{'━'*50}")
        ans = input("👤 미등록 화자를 프로파일에 등록하시겠습니까? [y/N]: ").strip().lower()
    except (EOFError, OSError):
        return

    if ans != 'y':
        return

    for u in unknowns:
        label      = u['label']
        utterances = u.get('utterances', [])
        count      = u.get('count', 0)
        print(f"\n  {label} ({count}개 발화)")
        for ut in utterances:
            print(f"    → \"{ut}\"")
        try:
            name = input(f"  이름 입력 (빈 Enter = 건너뜀): ").strip()
        except (EOFError, OSError):
            print("  ⚠️  입력 오류 — 건너뜀")
            continue

        if name:
            try:
                recorder.register_unknown(label, name)
                print(f"  ✅ {name} 등록 완료 ({count}개 샘플 저장)")
            except Exception as e:
                print(f"  ⚠️  등록 실패: {e}")
        else:
            print(f"  ⏭  건너뜀")


if __name__ == "__main__":
    import threading

    vpm      = VoiceProfileManager(VoiceEncoder() if HAS_RESEMBLYZER else None)
    profiles = vpm.load_profiles()

    # ── 모드 선택 ──────────────────────────────────────────
    print("🎛️  회의 모드")
    print("   1. 목소리 사전 등록 (그 자리에서 예시 문장)")
    print("   2. 저장된 프로파일 자동 식별 + 미매칭 클러스터링")
    while True:
        m = input("  모드 [1/2]: ").strip()
        if m in ("1", "2"): mode = int(m); break

    enrolled_embeddings: Dict[str, np.ndarray] = {}
    participants: List[str] = []

    if mode == 1:
        print("\n👥 참여자 등록 (빈 Enter = 완료)\n")
        while True:
            name = input("  이름: ").strip()
            if not name:
                if not participants: print("  ⚠️  최소 1명 필요"); continue
                break
            print(f'\n  [{name}] "안녕하세요, 지금부터 회의를 시작하겠습니다."')
            input("  준비되면 Enter →")
            print("  🔴 녹음 중...")
            audio = sd.rec(int(ENROLL_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                           channels=1, dtype="float32")
            sd.wait()
            embed = vpm.save_from_audio(name, audio.flatten())
            if embed is not None:
                enrolled_embeddings[name] = embed
            print(f"  ✅ {name} 등록 완료\n")
            participants.append(name)
    else:
        if profiles:
            print(f"\n  📂 로드된 프로파일: {', '.join(profiles.keys())}")
        ans = input("  참석자 이름 미리 등록? [y/N]: ").strip().lower()
        if ans == 'y':
            raw  = input("  참석자 (쉼표 구분): ").strip()
            participants = [p.strip() for p in raw.split(",") if p.strip()]

    # ── 레코더 생성 + 시작 (_start_audio=False: 오디오는 아래 InputStream이 담당) ──
    MeetingRecorder.load_model()
    _load_silero_vad()
    _load_pyannote_embedder()  # pyannote 화자 임베딩 (HF_TOKEN 있을 때)
    recorder = MeetingRecorder(
        mode=mode,
        participants=participants,
        on_event=terminal_event_handler,
        enrolled_embeddings=enrolled_embeddings,
        vpm=vpm,
    )
    recorder.start(_start_audio=False)  # ← 내부 오디오 스트림 비활성

    # ── 키보드 + 오디오 제어 ──────────────────────────────
    old_term = _disable_echo()

    # mutable state (클로저에서 = 재할당 가능하도록 dict 사용)
    _st = {'recording': False, 'buf': []}

    def audio_cb(indata, frames, time_info, status):
        """단일 InputStream: 전사 큐 + c키 버퍼 동시 처리"""
        data = indata.copy()
        recorder.audio_q.put(data)
        if _st['recording']:
            _st['buf'].append(data)

    def process_c_request(audio_buf: List[np.ndarray]):
        """c 뗐을 때 버퍼 전사 → Claude 요청"""
        if not audio_buf: return
        audio = np.concatenate(audio_buf, axis=0).flatten()
        if audio.std() < SILENCE_THRESH:
            print("  ⚠️  소리 감지 안 됨\n"); return
        try:
            segs, _ = recorder.model.transcribe(
                audio, language=LANGUAGE,          # 터미널 모드: 한국어 기본값
                initial_prompt=WHISPER_PROMPT, beam_size=3)
            cmd = " ".join(s.text for s in segs).strip()
        except Exception:
            return
        if cmd:
            print(f"  🎤 [{cmd}]\n  ⏳ 처리 중...\n")
            threading.Thread(target=recorder.claude_request,
                             args=(cmd,), daemon=True).start()

    print("  ⌨️  q + Enter → 회의 종료\n")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        dtype="float32", callback=audio_cb,
                        blocksize=int(SAMPLE_RATE * 0.1)):
        while recorder.running:
            try:
                if input().strip().lower() == 'q':
                    recorder.stop()
            except EOFError:
                break

    _restore_echo(old_term)
    time.sleep(0.8)

    # ── 종료 처리 ─────────────────────────────────────────
    _, unknowns = recorder.finalize()
    terminal_post_meeting(recorder, unknowns)
