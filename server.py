#!/usr/bin/env python3
"""
🌐 AI 회의 어시스턴트 — 웹 서버
meeting.py 엔진 import 사용.

실행: python3 server.py
접속: http://localhost:5555
"""

import os, threading, time, json, queue, re
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict
import numpy as np
import sounddevice as sd
from flask import Flask, Response, request, jsonify, send_from_directory


def _is_readonly() -> bool:
    """URL ?readonly=1 또는 X-Readonly 헤더 시 읽기 전용 모드"""
    return (request.args.get("readonly") == "1" or
            request.headers.get("X-Readonly") == "1")


from meeting import (
    MeetingRecorder, VoiceProfileManager,
    SAMPLE_RATE, OUTPUT_DIR, VOICES_DIR, HAS_RESEMBLYZER, ENROLL_SECONDS,
    _load_vocab, _VOCAB_MIN_CNT, HAS_NOISEREDUCE,
    load_glossary, save_glossary, _load_silero_vad, _load_pyannote_embedder,
    _load_separator,
)

_BASE_DIR = OUTPUT_DIR.parent
SETTINGS_FILE = _BASE_DIR / "settings.json"

def _load_json(path):
    if not path.exists(): return {}
    try:
        import json; return json.loads(path.read_text(encoding="utf-8"))
    except: return {}

def _save_json(path, data):
    import json
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_settings():
    s = _load_json(SETTINGS_FILE)
    if "obsidian_vault" not in s:
        s["obsidian_vault"] = ""
    if "obsidian_subfolder" not in s:
        s["obsidian_subfolder"] = "회의록"
    if "folders" not in s:
        s["folders"] = [{"name": "기본", "path": ""}] + [{"name": "", "path": ""} for _ in range(4)]
    if "whisper_model" not in s:
        s["whisper_model"] = "large-v3-turbo"
    return s

if HAS_RESEMBLYZER:
    from resemblyzer import VoiceEncoder

PORT = 5555

# ──────────────── SSE 브로드캐스터 ───────────────────────
class SSEBroadcaster:
    """
    연결된 모든 SSE 클라이언트에 이벤트 브로드캐스트.
    클라이언트별 개별 Queue 유지. deque로 O(1) 히스토리 관리.
    """
    def __init__(self):
        self._clients: List[queue.Queue] = []
        self._lock    = threading.Lock()
        self._history: deque = deque(maxlen=300)  # 자동 크기 제한

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._clients.append(q)
            # 최근 50개 이벤트 즉시 전송 (재연결 시 복원)
            for e in list(self._history)[-50:]:
                q.put(e)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, type_: str, data: dict):
        event = {"type": type_, "data": data,
                 "ts": datetime.now().strftime("%H:%M:%S")}
        self._history.append(event)
        # 회의 종료 시 히스토리 클리어 (새로고침 시 이전 이벤트 재생 방지)
        if type_ == "finished":
            self._history.clear()
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)


# ──────────────── 글로벌 상태 ─────────────────────────────
app      = Flask(__name__)
sse      = SSEBroadcaster()
recorder: Optional[MeetingRecorder] = None
_lock    = threading.Lock()

# 모델 + VPM 초기화 (서버 시작 시 1회)
import meeting as _meeting_mod
_startup_settings = _load_settings()
if _startup_settings.get("whisper_model"):
    _meeting_mod.WHISPER_MODEL = _startup_settings["whisper_model"]
print("🤖 Whisper 모델 로딩 중...")
MeetingRecorder.load_model()
print("🎙️ Silero VAD 로딩 중...")
_load_silero_vad()
print("🔊 pyannote 화자 임베딩 로딩 중...")
_load_pyannote_embedder()
print("🔈 겹침 발화 분리 모델 로딩 중...")
_load_separator()
_encoder = VoiceEncoder() if HAS_RESEMBLYZER else None
_vpm     = VoiceProfileManager(_encoder)
_pending: Dict[str, np.ndarray] = {}   # 등록 대기 임베딩
print("✅ 준비 완료\n")


def on_event(type_: str, data: dict):
    """meeting.py → server.py 이벤트 콜백"""
    sse.push(type_, data)


# ──────────────── API ─────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

@app.route("/")
def index():
    resp = send_from_directory(str(STATIC_DIR), "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

# ── 상태 ─────────────────────────────────────────────────
def _find_incomplete_meetings() -> List[str]:
    """비정상 종료된 회의 파일 탐지 (🏁 회의 종료 섹션 없는 것)."""
    incomplete = []
    for f in sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)[:20]:
        try:
            content = f.read_text(encoding="utf-8")
            if "## 🏁 회의 종료" not in content and "# 🤖 AI 회의 분석" not in content:
                incomplete.append(f.name)
        except Exception:
            pass
    return incomplete


@app.route("/api/status")
def api_status():
    with _lock:
        running = bool(recorder and recorder.running)
        md_file = recorder.md_path.name if running and recorder.md_path else ""
        speaking_stats = recorder._build_speaking_stats() if running and recorder else []
    incomplete = [] if running else _find_incomplete_meetings()
    return jsonify({"status": "running" if running else "idle",
                    "md_file": md_file,
                    "has_resemblyzer": HAS_RESEMBLYZER,
                    "has_noisereduce": HAS_NOISEREDUCE,
                    "incomplete_meetings": incomplete,
                    "speaking_stats": [
                        {"name": n, "seconds": s, "ratio": r}
                        for n, s, r in speaking_stats
                    ]})

# ── 마이크 장치 목록 ─────────────────────────────────────
@app.route("/api/devices")
def api_devices():
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        input_devices = [
            {"index": i, "name": d["name"], "channels": int(d["max_input_channels"])}
            for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        return jsonify({"ok": True, "devices": input_devices, "default": default_in})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "devices": [], "default": None})


# ── 의존성 상태 ──────────────────────────��────────────────
@app.route("/api/deps")
def api_deps():
    import subprocess as _sp
    from meeting import _LLM_BACKEND, OLLAMA_MODEL, OLLAMA_BASE_URL
    deps = []

    # LLM 백엔드 (Claude CLI 또는 Ollama)
    if _LLM_BACKEND == "claude":
        deps.append({"name": "LLM 백엔드", "ok": True, "detail": "Claude Code CLI"})
    elif _LLM_BACKEND == "ollama":
        deps.append({"name": "LLM 백엔드", "ok": True, "detail": f"Ollama ({OLLAMA_MODEL})"})
    else:
        deps.append({"name": "LLM 백엔드", "ok": False, "detail": "Claude CLI / Ollama 모두 미감지 — AI 기능 비활성"})

    # faster-whisper
    try:
        from faster_whisper import WhisperModel as _W
        deps.append({"name": "faster-whisper", "ok": True, "detail": "설치됨"})
    except ImportError:
        deps.append({"name": "faster-whisper", "ok": False, "detail": "pip install faster-whisper"})

    # resemblyzer
    deps.append({"name": "resemblyzer (화자 식별)", "ok": HAS_RESEMBLYZER,
                 "detail": "설치됨" if HAS_RESEMBLYZER else "pip3 install resemblyzer"})

    # noisereduce
    deps.append({"name": "noisereduce (소음 제거)", "ok": HAS_NOISEREDUCE,
                 "detail": "설치됨" if HAS_NOISEREDUCE else "pip3 install noisereduce"})

    # sounddevice / portaudio
    try:
        sd.query_devices()
        deps.append({"name": "sounddevice / PortAudio", "ok": True, "detail": "마이크 장치 정상"})
    except Exception as e:
        deps.append({"name": "sounddevice / PortAudio", "ok": False, "detail": str(e)})

    all_ok = all(d["ok"] for d in deps)
    return jsonify({"ok": all_ok, "deps": deps})


# ── 프로파일 목록 ────────────────────────���────────────────
@app.route("/api/profiles")
def api_profiles():
    return jsonify(_vpm.list_profiles())

@app.route("/api/profiles/<name>", methods=["DELETE"])
def api_delete_profile(name):
    _vpm.delete_profile(name)
    return jsonify({"ok": True})

# ── 목소리 등록 (비동기: Flask 스레드 블로킹 방지) ────────
@app.route("/api/enroll", methods=["POST"])
def api_enroll():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "이름 없음"})
    # 녹음은 별도 스레드에서 (3초 블로킹을 Flask 스레드 밖으로)
    threading.Thread(target=_enroll_worker, args=(name,), daemon=True).start()
    return jsonify({"ok": True, "msg": f"🔴 {name} 녹음 시작..."})

def _enroll_worker(name: str):
    global _pending
    sse.push("status", {"msg": f"🔴 {name} 녹음 중... ({ENROLL_SECONDS}초)"})
    try:
        audio = sd.rec(int(ENROLL_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                       channels=1, dtype="float32")
        sd.wait()
        embed = _vpm.save_from_audio(name, audio.flatten())
        if embed is not None:
            with _lock:
                _pending[name] = embed
        sse.push("enrolled", {"name": name, "profiles": _vpm.list_profiles()})
    except Exception as e:
        sse.push("error", {"msg": f"등록 오류: {e}"})

# ── 회의 시작 ─────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def api_start():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    global recorder, _pending
    with _lock:
        if recorder and recorder.running:
            return jsonify({"ok": False, "msg": "이미 회의 중"})

        data          = request.json or {}
        mode          = int(data.get("mode", 1))
        participants  = [p.strip() for p in data.get("participants", []) if p.strip()]
        language      = data.get("language", "ko")
        template      = data.get("template", "general")
        chunk_seconds = int(data.get("chunk_seconds", 8))
        device_id_raw = data.get("device_id")
        device_id = int(device_id_raw) if device_id_raw is not None else None
        extra_device_ids_raw = data.get("extra_device_ids", [])
        extra_device_ids = [int(x) for x in extra_device_ids_raw if x is not None]

        if mode == 1 and not participants and not _pending:
            return jsonify({"ok": False, "msg": "참여자를 먼저 등록해주세요"})

        enrolled = _pending.copy() if mode == 1 else {}
        _pending  = {}

        recorder = MeetingRecorder(
            mode=mode,
            participants=participants,
            on_event=on_event,
            enrolled_embeddings=enrolled,
            vpm=_vpm,
            language=language,
            template=template,
            chunk_seconds=chunk_seconds,
            device_id=device_id,
            extra_device_ids=extra_device_ids,
        )
        threading.Thread(target=recorder.start, daemon=True).start()

    return jsonify({"ok": True})

# ── 회의 종료 ─────────────────────────────────────────────
@app.route("/api/stop", methods=["POST"])
def api_stop():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    global recorder
    with _lock:
        r = recorder
    if not r or not r.running:
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    r.stop()
    threading.Thread(target=_finalize_worker, args=(r,), daemon=True).start()
    return jsonify({"ok": True})

def _copy_to_obsidian(md_path: Path):
    settings = _load_settings()
    vault = settings.get("obsidian_vault", "").strip()
    if not vault:
        return
    subfolder = settings.get("obsidian_subfolder", "회의록").strip()
    target_dir = Path(vault) / subfolder if subfolder else Path(vault)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # 소스 파일에 이미 YAML frontmatter 포함 — 그대로 복사
        content = md_path.read_text(encoding="utf-8")
        target_file = target_dir / md_path.name
        target_file.write_text(content, encoding="utf-8")
    except Exception as e:
        print(f"⚠️ Obsidian 복사 실패: {e}")


def _finalize_worker(r: MeetingRecorder):
    time.sleep(0.8)   # 마지막 청크 처리 대기
    r.finalize()      # 내부에서 항상 finished 이벤트 emit
    # Obsidian 복사 (백그라운드)
    if r.md_path and r.md_path.exists():
        threading.Thread(target=_copy_to_obsidian, args=(r.md_path,), daemon=True).start()

@app.route("/api/memo", methods=["POST"])
def api_memo():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    global recorder
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "내용 없음"})
    r = recorder
    if not r or not r.md_path or not r.md_path.exists():
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    now = datetime.now().strftime("%H:%M:%S")
    line = f"**{now}** | **📝 메모**: {text}\n\n"
    with open(r.md_path, "a", encoding="utf-8") as f:
        f.write(line)
    sse.push("memo", {"text": text, "time": now})
    return jsonify({"ok": True})

# ── 미등록 화자 등록 ──────────────────────────────────────
@app.route("/api/register_unknown", methods=["POST"])
def api_register_unknown():
    data  = request.json or {}
    label = data.get("label", "")
    name  = data.get("name", "").strip()
    if not label or not name:
        return jsonify({"ok": False, "msg": "label/name 없음"})
    with _lock:
        r = recorder
    if not r:
        return jsonify({"ok": False, "msg": "recorder 없음"})
    r.register_unknown(label, name)
    count = len(r.unknown_clusters.get(label, []))
    sse.push("profile_saved", {"name": name, "profiles": _vpm.list_profiles()})
    return jsonify({"ok": True, "samples": count})

@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """회의 취소 — finalize 없이 즉시 중단 + 파일 휴지통 이동"""
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    global recorder
    with _lock:
        r = recorder
    if not r or not r.running:
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    r.stop()
    md_path = r.md_path
    r.md_path = None          # 스레드가 추가로 파일을 쓰려 할 때 None → 예외 발생해 무시됨

    # UI는 즉시 리셋
    sse.push("finished", {"md_file": ""})

    # 백그라운드: 진행 중인 청크가 끝날 때까지 대기 후 휴지통 이동
    def _trash_later(path):
        time.sleep(2.0)
        try:
            if path and path.exists():
                try:
                    from send2trash import send2trash
                    send2trash(str(path.resolve()))
                except ImportError:
                    path.unlink()
        except Exception:
            pass
        sse.push("meetings_refresh", {})   # 삭제 완료 → 목록 갱신

    if md_path:
        threading.Thread(target=_trash_later, args=(md_path,), daemon=True).start()
    else:
        sse.push("meetings_refresh", {})

    return jsonify({"ok": True})

@app.route("/api/pause", methods=["POST"])
def api_pause():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    with _lock:
        r = recorder
    if not r or not r.running:
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    r.pause()
    return jsonify({"ok": True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    with _lock:
        r = recorder
    if not r or not r.running:
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    r.resume()
    return jsonify({"ok": True})

@app.route("/api/finish_unknown", methods=["POST"])
def api_finish_unknown():
    with _lock:
        r = recorder
    md = r.md_path.name if r and r.md_path else ""
    sse.push("finished", {"md_file": md})
    return jsonify({"ok": True})

# ── Claude 요청 ───────────────────────────────────────────
@app.route("/api/claude", methods=["POST"])
def api_claude():
    if _is_readonly(): return jsonify({"ok": False, "msg": "읽기 전용 모드"}), 403
    command = (request.json or {}).get("command", "").strip()
    if not command:
        return jsonify({"ok": False, "msg": "내용 없음"})
    with _lock:
        r = recorder
    if not r or not r.running:
        return jsonify({"ok": False, "msg": "회의 중 아님"})
    threading.Thread(target=r.claude_request, args=(command,), daemon=True).start()
    return jsonify({"ok": True})

# ── 검색 API ──────────────────────────────────────────────
@app.route("/api/search")
def api_search():
    import re as _re
    keyword  = request.args.get("q", "").strip()
    speaker  = request.args.get("speaker", "").strip()
    date     = request.args.get("date", "").strip()
    if not keyword:
        return jsonify({"ok": False, "msg": "검색어 없음"})

    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if date:
        files = [f for f in files if date in f.stem]

    pattern = _re.compile(_re.escape(keyword), _re.IGNORECASE)
    results = []

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            lines   = content.splitlines()
            title_m = _re.search(r'^# (.+)$', content, _re.MULTILINE)
            title   = title_m.group(1) if title_m else f.stem
            hits    = []

            for i, line in enumerate(lines):
                if not (line.startswith("**") and "|" in line):
                    continue
                if speaker and f"**{speaker}**" not in line:
                    continue
                if not pattern.search(line):
                    continue
                # 발화자, 시간 파싱
                sp_m   = _re.search(r'\*\*([^*]+)\*\*:\s*(.+)$', line)
                time_m = _re.search(r'\*\*(\d{2}:\d{2}:\d{2})\*\*', line)
                hits.append({
                    "time":    time_m.group(1) if time_m else "",
                    "speaker": sp_m.group(1) if sp_m else "",
                    "text":    sp_m.group(2).strip() if sp_m else line,
                    "context": lines[max(0,i-1):i+2],
                })

            if hits:
                results.append({
                    "file":  f.name,
                    "title": title,
                    "hits":  hits,
                })
        except Exception:
            pass

    total = sum(len(r["hits"]) for r in results)
    return jsonify({"ok": True, "total": total, "results": results, "keyword": keyword})

# ── 시맨틱 검색 API ───────────────────────────────────────
@app.route("/api/search/semantic")
def api_search_semantic():
    """Claude를 활용한 의미 기반 회의록 검색"""
    query = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 3)), 5)
    if not query:
        return jsonify({"ok": False, "msg": "검색어 없음"})

    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)[:10]
    if not files:
        return jsonify({"ok": True, "total": 0, "results": [], "query": query})

    # 각 파일에서 발화 추출 (최대 40줄)
    candidates = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            lines = [l for l in content.splitlines()
                     if l.startswith("**") and "|" in l][:40]
            if lines:
                candidates.append({"file": f.name, "lines": lines})
        except Exception:
            pass

    if not candidates:
        return jsonify({"ok": True, "total": 0, "results": [], "query": query})

    def _semantic_search():
        from meeting import claude_run
        import re as _re
        all_entries = []
        for c in candidates:
            for ln in c["lines"]:
                sp_m = _re.search(r'\*\*([^*]+)\*\*:\s*(.+)$', ln)
                tm_m = _re.search(r'\*\*(\d{2}:\d{2}:\d{2})\*\*', ln)
                if sp_m:
                    all_entries.append({
                        "file": c["file"],
                        "speaker": sp_m.group(1),
                        "text": sp_m.group(2).strip(),
                        "time": tm_m.group(1) if tm_m else "",
                    })

        if not all_entries:
            sse.push("semantic_results", {"results": [], "query": query})
            return

        # 발화 목록을 번호와 함께 Claude에게 전달
        numbered = "\n".join(
            f"{i+1}. [{e['file']}][{e['speaker']}] {e['text']}"
            for i, e in enumerate(all_entries[:80])
        )
        prompt = (
            f"다음 회의 발화 목록에서 '{query}'와 의미적으로 관련된 항목의 번호를 "
            f"최대 {limit*3}개만 쉼표로 나열해줘. 번호만 출력:\n\n{numbered}"
        )
        result = claude_run(prompt, timeout=20)
        if not result:
            sse.push("semantic_results", {"results": [], "query": query})
            return

        nums = [int(x.strip()) - 1 for x in result.split(",")
                if x.strip().isdigit() and 0 < int(x.strip()) <= len(all_entries)]

        seen_files: dict = {}
        for idx in nums:
            entry = all_entries[idx]
            fname = entry["file"]
            if fname not in seen_files:
                seen_files[fname] = {"file": fname, "title": fname.replace(".md",""), "hits": []}
            seen_files[fname]["hits"].append({
                "time": entry["time"],
                "speaker": entry["speaker"],
                "text": entry["text"],
                "context": [],
            })

        sse.push("semantic_results", {
            "results": list(seen_files.values()),
            "query": query,
            "total": sum(len(v["hits"]) for v in seen_files.values()),
        })

    threading.Thread(target=_semantic_search, daemon=True).start()
    return jsonify({"ok": True})

# ── Slack 회의 요약 전송 ──────────────────────────────────
@app.route("/api/slack/send", methods=["POST"])
def api_slack_send():
    settings = _load_settings()
    webhook_url = settings.get("slack_webhook", "").strip()
    if not webhook_url:
        return jsonify({"ok": False, "msg": "설정에서 Slack Webhook URL을 먼저 입력해주세요."})
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "전송할 내용이 없습니다."})
    try:
        import urllib.request, json as _json
        blocks = _md_to_slack_blocks(text)
        payload = _json.dumps({"blocks": blocks, "text": "📝 회의 요약"}).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return jsonify({"ok": True})
            return jsonify({"ok": False, "msg": f"Slack 응답: {resp.status}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


def _md_to_slack_blocks(md: str) -> list:
    """Markdown 회의 요약을 Slack Block Kit 형식으로 변환."""
    blocks = []
    current_section = []

    def flush_section():
        if current_section:
            text = "\n".join(current_section).strip()
            if text:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
            current_section.clear()

    for line in md.split("\n"):
        line = line.rstrip()
        # ## 헤더 → Slack header block
        if line.startswith("## "):
            flush_section()
            blocks.append({"type": "divider"})
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": line[3:].strip()}})
            continue
        # # 헤더
        if line.startswith("# "):
            flush_section()
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": line[2:].strip()}})
            continue
        # 테이블 헤더/구분선 건너뜀
        if line.startswith("|--") or line.startswith("| 담당자"):
            flush_section()
            continue
        # 테이블 행 → 리스트 형태로 변환
        if line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0]:
                current_section.append(f"• *{cells[0]}*: {cells[1]}" +
                                       (f" (마감: {cells[2]})" if len(cells) > 2 and cells[2] else ""))
            continue
        # **bold** → *bold* (Slack mrkdwn)
        converted = line.replace("**", "*")
        # - 리스트
        if converted.startswith("- "):
            converted = "• " + converted[2:]
        current_section.append(converted)

    flush_section()
    return blocks

# ── AI 대화형 회의록 검색 ─────────────────────────────────
@app.route("/api/search/ai", methods=["POST"])
def api_search_ai():
    """자연어 질문으로 회의록을 검색하고 AI가 답변을 생성"""
    data = request.json or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"ok": False, "msg": "질문이 없습니다"})

    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)[:15]
    if not files:
        return jsonify({"ok": True, "answer": "검색할 회의록이 없습니다.", "sources": []})

    def _ai_search():
        from meeting import claude_run

        # 각 파일에서 발화 내용 추출
        all_content = []
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                lines = [l for l in content.splitlines()
                         if l.startswith("**") and "|" in l][:50]
                if lines:
                    all_content.append(f"=== {f.name} ===\n" + "\n".join(lines))
            except Exception:
                pass

        if not all_content:
            sse.push("ai_search_result", {"answer": "회의록에서 발화 내용을 찾을 수 없습니다.", "sources": []})
            return

        context = "\n\n".join(all_content)[:6000]
        prompt = (
            f"당신은 회의록 검색 AI입니다. 아래 회의록 데이터를 기반으로 사용자의 질문에 답변하세요.\n"
            f"답변은 간결하고 구체적으로, 발화자와 날짜를 포함해서 답변하세요.\n"
            f"관련 내용이 없으면 '관련 내용을 찾지 못했습니다'라고 답하세요.\n\n"
            f"--- 회의록 데이터 ---\n{context}\n\n"
            f"--- 질문 ---\n{question}\n\n답변:"
        )
        result = claude_run(prompt, timeout=30, retries=1)
        if not result:
            sse.push("ai_search_result", {"answer": "AI 응답을 받지 못했습니다. 다시 시도해주세요.", "sources": []})
            return

        # 답변에서 언급된 파일명 추출
        sources = []
        for f in files:
            if f.name in result or f.stem in result:
                sources.append(f.name)

        sse.push("ai_search_result", {"answer": result, "sources": sources, "question": question})

    threading.Thread(target=_ai_search, daemon=True).start()
    return jsonify({"ok": True})

# ── 설정 ─────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(_load_settings())
    data = request.json or {}
    _save_json(SETTINGS_FILE, data)
    return jsonify({"ok": True})

# ── 회의록 목록/내용 ──────────────────────────────────────
@app.route("/api/meetings")
def api_meetings():
    tab = request.args.get("tab", "0", type=str)
    try:
        tab_idx = int(tab)
    except (ValueError, TypeError):
        tab_idx = 0

    # 탭 인덱스에 따라 폴더 결정
    if tab_idx == 0:
        search_dir = OUTPUT_DIR
    else:
        settings = _load_settings()
        folders = settings.get("folders", [])
        if tab_idx < len(folders):
            folder_path = folders[tab_idx].get("path", "").strip()
            if not folder_path:
                return jsonify([])
            search_dir = Path(folder_path)
        else:
            return jsonify([])

    all_tags = _load_json(_BASE_DIR / "meeting_tags.json")
    result   = []
    try:
        files = sorted(search_dir.glob("meeting_*.md"), reverse=True)
    except Exception:
        return jsonify([])
    for f in files[:20]:
        try:
            content = f.read_text(encoding="utf-8")
            title_m = re.search(r'^# (.+)$', content, re.MULTILINE)
            part_m  = re.search(r'\*\*참여자:\*\* (.+)', content)
            lines   = [l for l in content.splitlines()
                       if l.startswith("**") and "|" in l]
            result.append({
                "file":         f.name,
                "title":        title_m.group(1) if title_m else f.stem,
                "participants": part_m.group(1) if part_m else "-",
                "lines":        len(lines),
                "has_summary":  "# 🤖 AI 회의 분석" in content,
                "tags":         all_tags.get(f.name, []),
            })
        except Exception:
            pass
    return jsonify(result)

@app.route("/api/meetings/<filename>", methods=["GET", "DELETE", "PUT"])
def api_meeting_content(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"ok": False}), 404

    if request.method == "DELETE":
        try:
            try:
                from send2trash import send2trash
                send2trash(str(path.resolve()))
            except ImportError:
                path.unlink()  # send2trash 없을 시 직접 삭제
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

    if request.method == "PUT":
        content = (request.json or {}).get("content", "")
        try:
            path.write_text(content, encoding="utf-8")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

    return jsonify({"ok": True, "content": path.read_text(encoding="utf-8")})

@app.route("/api/meetings/<filename>/audio")
def api_meeting_audio(filename):
    """회의 녹음 파일 서빙 (MP3 우선, WAV 폴백)."""
    base = filename.replace(".md", "")
    for ext in (".mp3", ".wav"):
        audio_path = OUTPUT_DIR / (base + ext)
        if audio_path.exists():
            mime = "audio/mpeg" if ext == ".mp3" else "audio/wav"
            return send_from_directory(str(OUTPUT_DIR), base + ext, mimetype=mime)
    return jsonify({"ok": False, "msg": "녹음 파일 없음"}), 404

@app.route("/api/meetings/<filename>/line", methods=["PATCH"])
def api_meeting_line(filename):
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"ok": False}), 404
    data = request.json or {}
    old_text = data.get("old_text", "")
    new_text = data.get("new_text", "")
    if not old_text:
        return jsonify({"ok": False, "msg": "old_text 없음"})
    try:
        content = path.read_text(encoding="utf-8")
        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        # 사용자 교정 학습 — 발화 텍스트 부분만 추출해서 저장
        import re as _re
        old_m = _re.search(r'\*\*: (.+)$', old_text)
        new_m = _re.search(r'\*\*: (.+)$', new_text)
        if old_m and new_m:
            from meeting import save_correction
            save_correction(old_m.group(1).strip(), new_m.group(1).strip())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/meetings/<filename>/recover", methods=["POST"])
def api_meeting_recover(filename):
    """비정상 종료된 회의 파일에 AI 요약을 사후 생성."""
    from meeting import claude_run
    path = OUTPUT_DIR / filename
    if not path.exists():
        return jsonify({"ok": False}), 404
    content = path.read_text(encoding="utf-8")
    if "## 🏁 회의 종료" in content:
        return jsonify({"ok": False, "msg": "이미 정상 종료된 회의입니다."})

    prompt = (
        f"다음은 예기치 않게 종료된 회의 기록입니다. "
        f"정상 종료되지 않았으므로 종료 섹션을 생성해야 합니다.\n\n"
        f"회의록:\n{content[-6000:]}\n\n"
        f"위 내용을 바탕으로 아래 형식으로 회의 종료 섹션을 한국어로 작성해주세요:\n\n"
        f"## 🏁 회의 종료\n**종료 시각:** (추정)\n\n"
        f"# 🤖 AI 회의 분석\n## 📝 회의 요약\n## ✅ 결정사항\n## ❓ 미결사항\n## 📋 액션아이템\n"
        f"(각 섹션을 채워주세요. 내용이 없으면 '없음'으로 표기)"
    )
    result = claude_run(prompt, timeout=90, retries=1)
    if not result:
        return jsonify({"ok": False, "msg": "AI 분석 실패"})

    recovered = content.rstrip() + "\n\n" + result + "\n"
    path.write_text(recovered, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/action_status", methods=["GET", "POST"])
def api_action_status():
    status_path = _BASE_DIR / "action_status.json"
    if request.method == "GET":
        return jsonify({"ok": True, "statuses": _load_json(status_path)})
    data = request.json or {}
    key = data.get("key", "")
    status = data.get("status", "pending")
    if not key:
        return jsonify({"ok": False, "msg": "key 없음"})
    statuses = _load_json(status_path)
    statuses[key] = status
    _save_json(status_path, statuses)
    return jsonify({"ok": True})

@app.route("/api/tags/<filename>", methods=["GET", "POST"])
def api_tags(filename):
    tags_path = _BASE_DIR / "meeting_tags.json"
    all_tags = _load_json(tags_path)
    if request.method == "GET":
        return jsonify({"ok": True, "tags": all_tags.get(filename, [])})
    data = request.json or {}
    tags = data.get("tags", [])
    all_tags[filename] = tags
    _save_json(tags_path, all_tags)
    return jsonify({"ok": True})

# ── 액션 아이템 대시보드 ──────────────────────────────────
@app.route("/api/action_items")
def api_action_items():
    import re
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    all_meetings = []

    for f in files[:30]:
        try:
            content = f.read_text(encoding="utf-8")
            if "## 📌 액션 아이템" not in content:
                continue
            title_m = re.search(r'^# (.+)$', content, re.MULTILINE)
            title   = title_m.group(1) if title_m else f.stem
            date_m  = re.search(r'meeting_(\d{4})(\d{2})(\d{2})', f.name)
            date    = f"{date_m.group(1)}-{date_m.group(2)}-{date_m.group(3)}" if date_m else ""

            section = content.split("## 📌 액션 아이템")[-1].split("\n## ")[0]
            rows = []
            for line in section.splitlines():
                if not line.startswith("|") or "---" in line:
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 2 and cells[0] and cells[0] not in ("담당자", "팀원"):
                    rows.append({
                        "assignee": cells[0],
                        "content":  cells[1] if len(cells) > 1 else "",
                        "deadline": cells[2] if len(cells) > 2 else "미정",
                        "priority": cells[3] if len(cells) > 3 else "",
                    })
            if rows:
                all_meetings.append({"file": f.name, "title": title,
                                     "date": date, "items": rows})
        except Exception:
            pass

    return jsonify({"ok": True, "meetings": all_meetings})

# ── 어휘 사전 조회 ────────────────────────────────────────
@app.route("/api/vocab")
def api_vocab():
    lang = request.args.get("lang", "ko")
    vocab = _load_vocab(lang)
    top = sorted(vocab.items(), key=lambda x: -x[1])
    qualified = [(w, c) for w, c in top if c >= _VOCAB_MIN_CNT]
    return jsonify({
        "ok": True,
        "lang": lang,
        "total_words": len(vocab),
        "qualified_words": len(qualified),
        "min_count": _VOCAB_MIN_CNT,
        "words": [{"word": w, "count": c} for w, c in top],
    })

# ── 용어집 ───────────────────────────────────────────────
@app.route("/api/glossary", methods=["GET", "POST", "DELETE"])
def api_glossary():
    if request.method == "GET":
        return jsonify({"ok": True, "glossary": load_glossary()})
    data = request.json or {}
    glossary = load_glossary()
    if request.method == "DELETE":
        term = data.get("term", "")
        if term and term in glossary:
            del glossary[term]
            save_glossary(glossary)
        return jsonify({"ok": True})
    # POST: 추가/수정
    term = data.get("term", "").strip()
    desc = data.get("desc", "").strip()
    if not term:
        return jsonify({"ok": False, "msg": "term 없음"})
    glossary[term] = desc
    save_glossary(glossary)
    return jsonify({"ok": True, "count": len(glossary)})


# ── 공유 URL (같은 네트워크) ─────────────────────────────
@app.route("/api/share_url")
def api_share_url():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    from pathlib import Path as _Path
    _cert = _Path(__file__).parent / "cert.pem"
    _proto = "https" if _cert.exists() else "http"
    return jsonify({"url": f"{_proto}://{ip}:{PORT}", "ip": ip, "port": PORT})

# ── 회의 통계 ─────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    import re
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    total = len(files)
    by_month: Dict[str, int] = {}
    participant_count: Dict[str, int] = {}
    total_lines = 0
    has_summary_count = 0

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            date_m = re.search(r'meeting_(\d{4})(\d{2})', f.name)
            if date_m:
                ym = f"{date_m.group(1)}-{date_m.group(2)}"
                by_month[ym] = by_month.get(ym, 0) + 1
            part_m = re.search(r'\*\*참여자:\*\* (.+)', content)
            if part_m:
                for p in part_m.group(1).split(','):
                    p = p.strip()
                    if p and p != '-':
                        participant_count[p] = participant_count.get(p, 0) + 1
            lines = [l for l in content.splitlines() if l.startswith("**") and "|" in l]
            total_lines += len(lines)
            if "# 🤖 AI 회의 분석" in content or "# 🤖 AI 회의 분석" in content:
                has_summary_count += 1
        except Exception:
            pass

    top_participants = sorted(participant_count.items(), key=lambda x: -x[1])[:10]
    monthly = sorted(by_month.items())

    return jsonify({
        "ok": True,
        "total_meetings": total,
        "total_lines": total_lines,
        "avg_lines": round(total_lines / total, 1) if total else 0,
        "has_summary_count": has_summary_count,
        "by_month": [{"month": m, "count": c} for m, c in monthly],
        "top_participants": [{"name": n, "count": c} for n, c in top_participants],
    })

# ── 회의 전 브리핑 ────────────────────────────────────────
@app.route("/api/briefing")
def api_briefing():
    """시작 버튼 전 최근 회의록 미결 사항 비동기 요약"""
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)[:5]
    if not files:
        return jsonify({"ok": False, "msg": "이전 회의 없음"})

    context_parts = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            if "# 🤖 AI 회의 분석" in content:
                section = content.split("# 🤖 AI 회의 분석")[-1][:1500]
                context_parts.append(f"[{f.stem}]\n{section}")
        except Exception:
            pass

    if not context_parts:
        return jsonify({"ok": False, "msg": "분석된 회의록 없음"})

    def _run():
        from meeting import claude_run
        prompt = (
            "최근 회의들의 미결 사항과 액션 아이템 중 아직 처리되지 않았을 항목을 "
            "오늘 회의에서 다뤄야 할 내용으로 5개 이내로 요약해줘. "
            "불릿 포인트로 간결하게:\n\n" + "\n\n".join(context_parts)
        )
        result = claude_run(prompt, timeout=30)
        if result:
            sse.push("briefing_ready", {"content": result})
        else:
            sse.push("briefing_ready", {"content": "분석 실패 — Claude CLI를 확인하세요"})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})

# ── 다음 회의 어젠다 초안 ─────────────────────────────────
@app.route("/api/agenda_draft", methods=["POST"])
def api_agenda_draft():
    with _lock:
        r = recorder
    summary = (request.json or {}).get("summary", "")
    if not summary and r and r.md_path and r.md_path.exists():
        content = r.md_path.read_text(encoding="utf-8")
        for marker in ("# 🤖 AI 회의 분석", "# 🤖 AI 회의 분석", "# 🤖 AI 회의"):
            if marker in content:
                summary = content.split(marker)[-1][:2000]
                break
    if not summary:
        return jsonify({"ok": False, "msg": "요약 없음 — 먼저 회의를 종료하고 AI 요약을 생성해주세요"})

    def _draft():
        from meeting import claude_run
        prompt = (
            "다음 회의 분석 결과를 바탕으로 다음 회의 어젠다 초안을 한국어로 작성해줘.\n"
            "미결 사항과 액션 아이템 팔로업 중심으로, 5개 이내로 구체적으로:\n\n"
            f"{summary}\n\n"
            "형식 (마크다운):\n"
            "## 📋 다음 회의 어젠다 초안\n\n"
            "| # | 안건 | 담당 | 예상시간 |\n"
            "|---|------|------|----------|\n"
            "| 1 | ... | ... | ...분 |\n\n"
            "**목표:** 이번 회의에서 반드시 결정해야 할 것 1~2줄"
        )
        result = claude_run(prompt, timeout=30)
        if result:
            sse.push("agenda_draft", {"content": result})
        else:
            sse.push("error", {"msg": "어젠다 생성 실패"})

    threading.Thread(target=_draft, daemon=True).start()
    return jsonify({"ok": True})

# ── 다음 회의 캘린더 등록 ─────────────────────────────────
@app.route("/api/schedule_next", methods=["POST"])
def api_schedule_next():
    with _lock:
        r = recorder
    summary = (request.json or {}).get("summary", "")
    if not summary and r and r.md_path and r.md_path.exists():
        content = r.md_path.read_text(encoding="utf-8")
        if "# 🤖 AI 회의" in content:
            summary = content.split("# 🤖 AI 회의")[-1]
    if not summary:
        return jsonify({"ok": False, "msg": "요약 없음"})
    if r:
        threading.Thread(target=r.schedule_next_meeting,
                         args=(summary,), daemon=True).start()
    return jsonify({"ok": True})

# ── SSE 스트림 ────────────────────────────────────────────
@app.route("/api/stream")
def api_stream():
    q = sse.subscribe()

    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield "data: {\"type\":\"ping\"}\n\n"
        except GeneratorExit:
            pass
        finally:
            sse.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":      "keep-alive",
        }
    )

# ──────────────── 서버 실행 ───────────────────────────────
if __name__ == "__main__":
    import webbrowser, os as _os

    # HTTPS 모드: 환경변수 HTTPS=1 또는 cert.pem/key.pem이 이미 존재할 때만 활성화
    # localhost는 HTTP에서도 브라우저가 보안 컨텍스트로 허용(마이크 동작)
    # 모바일 접속이 필요할 때만 HTTPS 사용
    _force_https = _os.environ.get("HTTPS", "0") == "1"
    ssl_ctx = None
    cert_file = _BASE_DIR / "cert.pem"
    key_file  = _BASE_DIR / "key.pem"

    if _force_https:
        if not cert_file.exists() or not key_file.exists():
            try:
                from cryptography import x509
                from cryptography.x509.oid import NameOID
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import rsa
                import ipaddress as _ip
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"localhost")])
                cert = (
                    x509.CertificateBuilder()
                    .subject_name(name).issuer_name(name)
                    .public_key(key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.now(timezone.utc))
                    .not_valid_after(datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 10))
                    .add_extension(x509.SubjectAlternativeName([
                        x509.DNSName(u"localhost"),
                        x509.IPAddress(_ip.IPv4Address("127.0.0.1")),
                    ]), critical=False)
                    .sign(key, hashes.SHA256())
                )
                cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_file.write_bytes(key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL,
                    serialization.NoEncryption()
                ))
                print("✅ HTTPS 인증서 생성 완료 (cert.pem / key.pem)")
            except ImportError:
                print("ℹ️  cryptography 미설치 — HTTP로 실행합니다.")
        if cert_file.exists() and key_file.exists():
            import ssl as _ssl
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(str(cert_file), str(key_file))

    proto = "https" if ssl_ctx else "http"
    if proto == "https":
        print(f"🔒 HTTPS 모드 → https://localhost:{PORT}")
        print("   브라우저 경고 시 '고급 → 계속 진행' 클릭")
    else:
        print(f"🌐 http://localhost:{PORT}")
    print("   브라우저가 자동으로 열립니다. 종료: Ctrl+C\n")

    _open_url = f"{proto}://localhost:{PORT}"
    threading.Timer(2.0, lambda: webbrowser.open(_open_url)).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True,
            ssl_context=ssl_ctx)

