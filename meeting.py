#!/usr/bin/env python3
"""
🎙️ AI 회의 어시스턴트 — 엔진 (meeting.py)

터미널 직접 실행: python3 meeting.py
웹에서 사용:      server.py 가 import 하여 사용

모드 1: 그 자리에서 예시 문장 읽기 → voices/ 저장 → 회의 시작
모드 2: voices/ 프로파일 자동 식별 + 미매칭은 클러스터링
        (참석자 이름 사전 등록 여부는 선택)
"""

import time, queue, threading, subprocess, re, sys, json
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
    # 2. Apple Silicon — mlx-whisper 미사용, CPU ARM NEON int8 (CTranslate2 자동 최적화)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
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
ENROLL_SECONDS    = 5
TOPIC_INTERVAL    = 90
QUALITY_INTERVAL  = 600
CLAUDE_TIMEOUT    = 12
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
        "correct_prompt": "한국어 STT 오인식만 수정. 교정 문장 한 줄만:\n{text}",
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


VOCAB_FILE    = _BASE_DIR / "vocab.json"
GLOSSARY_FILE = _BASE_DIR / "glossary.json"
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

    all_terms = frequent + [t for t in glossary_terms if t not in frequent]
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

try:
    from resemblyzer import VoiceEncoder, preprocess_wav
    HAS_RESEMBLYZER = True
except ImportError:
    HAS_RESEMBLYZER = False
    VoiceEncoder = None

try:
    import noisereduce as nr
    HAS_NOISEREDUCE = True
except ImportError:
    HAS_NOISEREDUCE = False

# ── webrtcvad (정밀 발화 감지) ──────────────────────────────────────
try:
    import webrtcvad as _wrtcvad_mod
    _vad_inst = _wrtcvad_mod.Vad(2)   # aggressiveness 0~3
    HAS_WEBRTCVAD = True
except ImportError:
    HAS_WEBRTCVAD = False

# ── 트리거 워드 패턴 ──────────────────────────────────────
# Whisper 오인식 변형 포함:
#   "헤이/에이/hey" + "클로드/claude/cloud"
#   혼합 표기: "hey 클로드", "에이 클로드" 등
_TRIGGER_PATTERN = re.compile(
    r'((?:헤이|에이|hey)\s*(?:클로드|claude|cloud)|hey\s*클로드|에이\s*클로드|클로드\s*야|클로드야)'
    r'[,。,!\s]*(.{2,})',
    re.IGNORECASE | re.DOTALL
)

def _vad_has_speech(audio_f32: np.ndarray, threshold: float = 0.1) -> bool:
    """webrtcvad로 발화 포함 여부 판단. 미설치 시 항상 True."""
    if not HAS_WEBRTCVAD:
        return True
    try:
        pcm = (np.clip(audio_f32, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        frame_b = int(SAMPLE_RATE * 0.03) * 2  # 30ms 프레임 (bytes)
        total, speech = 0, 0
        for i in range(0, len(pcm) - frame_b + 1, frame_b):
            frame = pcm[i:i + frame_b]
            if len(frame) == frame_b:
                total += 1
                if _vad_inst.is_speech(frame, SAMPLE_RATE):
                    speech += 1
        return total == 0 or (speech / total) > threshold
    except Exception:
        return True


# ──────────────── Claude 호출 ─────────────────────────────
_claude_fail_count = 0          # 연속 실패 횟수
_claude_fail_lock  = threading.Lock()
_CLAUDE_CLI_MISSING = False     # CLI 자체가 없는 경우 재시도 불필요


def claude_run(prompt: str, timeout: int = CLAUDE_TIMEOUT, retries: int = 2,
               model: str = "") -> str:
    """
    Claude CLI를 호출하고 실패 시 최대 retries회 재시도.
    model: "" → 기본 모델(Sonnet), CLAUDE_FAST_MODEL → Haiku (빠른 작업용).
    CLI 자체가 없으면 즉시 포기(재시도 없음).
    연속 3회 이상 실패 시 stderr에 경고 출력.
    """
    global _claude_fail_count, _CLAUDE_CLI_MISSING
    if _CLAUDE_CLI_MISSING:
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
            print("[ERROR] Claude CLI를 찾을 수 없습니다. 'claude' 명령어를 설치해주세요.", file=sys.stderr)
            return ""
        except Exception as e:
            last_err = str(e)[:120]

        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))   # 1s, 2s backoff

    with _claude_fail_lock:
        _claude_fail_count += 1
        cnt = _claude_fail_count
    if cnt >= 3:
        print(f"[WARN] Claude CLI 연속 {cnt}회 실패: {last_err}", file=sys.stderr)
    return ""


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
        """오디오 → 임베딩"""
        if not self.encoder: return None
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
        best_name, best_sim = None, 0.0
        for name, ref in profiles.items():
            sim = float(np.dot(embed, ref) /
                        (np.linalg.norm(embed) * np.linalg.norm(ref) + 1e-9))
            if sim > best_sim: best_sim, best_name = sim, name
        if best_sim >= IDENTIFY_THRESH:
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

    # 클래스 변수: 최초 로딩 후 공유 (메모리 효율)
    _model: Optional[WhisperModel] = None

    @classmethod
    def load_model(cls):
        global WHISPER_PROMPT
        if cls._model is None:
            device, compute_type, label = _detect_whisper_backend()
            print(f"🤖 Whisper 모델 로딩 중... [{label}]")
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

            # utterance 경계 감지 (webrtcvad 있을 때)
            if HAS_WEBRTCVAD and buf and elapsed >= MIN_CHUNK_SEC:
                try:
                    last = buf[-1].flatten()
                    pcm = (np.clip(last, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    frame_b = int(SAMPLE_RATE * 0.03) * 2
                    has_speech = any(
                        _vad_inst.is_speech(pcm[i:i+frame_b], SAMPLE_RATE)
                        for i in range(0, len(pcm) - frame_b + 1, frame_b)
                        if len(pcm[i:i+frame_b]) == frame_b
                    )
                    if has_speech:
                        silence_frames = 0
                    else:
                        silence_frames += 1
                except Exception:
                    silence_frames = 0

            # 청크 처리 조건: (1) 최대 청크 도달 OR (2) 발화 끝 감지
            max_reached = elapsed >= self.chunk_seconds
            utt_end = HAS_WEBRTCVAD and silence_frames >= SILENCE_END_FRAMES and elapsed >= MIN_CHUNK_SEC

            if not (max_reached or utt_end):
                continue

            t_start = time.time()
            silence_frames = 0
            if not buf:
                continue
            audio = np.concatenate(buf, axis=0).flatten()
            buf.clear()
            if audio.std() < SILENCE_THRESH:
                continue
            if not _vad_has_speech(audio, self._noise_floor):
                continue
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
            try:
                with self._model_lock:
                    segs, _ = self.model.transcribe(
                        audio, language=self.lang_cfg["whisper_lang"],
                        initial_prompt=self.whisper_prompt,
                        beam_size=5,
                        best_of=5,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        no_speech_threshold=0.5,
                        compression_ratio_threshold=2.2,
                        log_prob_threshold=-0.8,
                        vad_filter=True,
                        vad_parameters={
                            "threshold": 0.5,
                            "min_speech_duration_ms": 250,
                            "min_silence_duration_ms": 600,
                            "speech_pad_ms": 300,
                        },
                        word_timestamps=False,
                    )
                text = " ".join(s.text for s in segs).strip()
            except Exception:
                continue
            if not text:
                continue
            text = re.sub(r'\.{2,}', '', text)
            text = re.sub(r'^[어음그저아]+[,\s]+', '', text.strip())
            text = re.sub(r'\b(\w+)( \1){2,}\b', r'\1', text)
            text = text.strip()
            if not text or set(text) <= {'.', ' ', '·'}:
                continue

            # 트리거 워드 감지 ("헤이 클로드 ...")
            _tm = _TRIGGER_PATTERN.search(text)
            if _tm and _tm.group(2).strip():
                _q = _tm.group(2).strip()
                threading.Thread(target=self.claude_request,
                                 args=(f"[음성 요청] {_q}",), daemon=True).start()

            speaker, embed = self._identify_speaker(audio)
            now            = datetime.now().strftime("%H:%M:%S")
            raw_line       = f"**{now}** | **{speaker}**: {text}\n\n"

            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(raw_line)
            self.emit("line", {"speaker": speaker, "text": text, "time": now})

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
            return self.participants[0] if self.participants else "참여자", None

        embed = self.vpm.embed_audio(audio)
        if embed is None:
            return self.participants[0] if self.participants else "?", None

        # ── 모드 1 ───────────────────────────────────────
        if self.mode == 1:
            # 1순위: 세션 등록 임베딩
            if self.enrolled:
                best_n, best_s = "", 0.0
                for name, ref in self.enrolled.items():
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
        best_n, best_s = "", 0.0
        for label, refs in self.unknown_clusters.items():
            ref_mean = np.mean(refs, axis=0)
            s = float(np.dot(embed, ref_mean) /
                      (np.linalg.norm(embed) * np.linalg.norm(ref_mean) + 1e-9))
            if s > best_s: best_s, best_n = s, label
        if best_s >= CLUSTER_THRESH:
            self.unknown_clusters[best_n].append(embed)
            return best_n
        # 클러스터 최대 20개, 각 클러스터 샘플 최대 50개 제한
        for lbl in list(self.unknown_clusters):
            if len(self.unknown_clusters[lbl]) > 50:
                self.unknown_clusters[lbl] = self.unknown_clusters[lbl][-50:]
        if len(self.unknown_clusters) >= 20:
            return f"미등록{len(self.unknown_clusters)+1}"
        new_label = f"미등록{len(self.unknown_clusters)+1}"
        self.unknown_clusters[new_label] = [embed]
        self.emit("unknown_speaker", {"label": new_label})
        return new_label

    # ── STT 교정 (언어별) ─────────────────────────────────
    def _correct_async(self, raw, speaker, now, raw_line):
        self._claude_inc()
        try:
            prompt = self.lang_cfg["correct_prompt"].format(text=raw)
            corrected = claude_run(prompt, timeout=8, model=CLAUDE_FAST_MODEL)
        finally:
            self._claude_dec()
        if not corrected or corrected == raw: return
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
    def stop(self):
        self.running = False

    def finalize(self) -> Tuple[str, List[dict]]:
        """회의 종료 처리. 반환: (summary, unknown_speakers_list)"""
        now = datetime.now().strftime("%H:%M:%S")
        try:
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write(f"\n---\n\n## 🏁 회의 종료\n\n**종료 시간:** {now}\n\n")
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
