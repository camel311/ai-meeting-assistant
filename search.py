#!/usr/bin/env python3
"""
🔍 회의록 하이브리드 RAG 검색

키워드(SQLite FTS5 trigram) + 시맨틱(임베딩) 검색을 RRF로 병합한다.
임베딩 백엔드는 환경에 따라 자동 선택(graceful degradation):

  1. sentence-transformers (multilingual-e5-small) → 진짜 의미 기반 검색
  2. scikit-learn TF-IDF (char n-gram)            → 설치 불필요, 어휘 유사도
  3. 없음                                          → 키워드(FTS5) 단독

사용법:
  python3 search.py [질의]                  # 하이브리드 검색 (기본)
  python3 search.py [질의] --keyword        # 키워드(FTS5)만
  python3 search.py [질의] --semantic       # 시맨틱(벡터)만
  python3 search.py [질의] --date 2026-04   # 날짜 필터
  python3 search.py [질의] --speaker Jerry  # 발화자 필터
  python3 search.py [질의] --top 10         # 상위 N개
  python3 search.py --reindex               # 변경분만 색인 갱신
  python3 search.py --rebuild               # 색인 전체 재구축
  python3 search.py --list                  # 전체 회의 목록
  python3 search.py --summary               # 최근 회의 요약 목록
"""

import sys, re, argparse, sqlite3, unicodedata, threading
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import numpy as np

_BASE_DIR  = Path(__file__).parent
OUTPUT_DIR = _BASE_DIR / "meetings"
INDEX_DB   = _BASE_DIR / ".search_index.db"

INDEX_VERSION   = 1
CHUNK_MAX_CHARS = 400      # 청크 목표 크기(연속 발화 병합 상한)
RRF_K           = 60       # Reciprocal Rank Fusion 상수(표준값)
EMBED_MODEL_ID  = "intfloat/multilingual-e5-small"

# 발화 라인: **HH:MM:SS** | **발화자**: 내용
_UTTER_RE = re.compile(r'^\*\*(\d{2}:\d{2}:\d{2})\*\* \| \*\*([^*]+)\*\*:\s*(.+)$')
_STT_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)


# ── ANSI 컬러 ──────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    YELLOW = "\033[33m"
    GREEN  = "\033[32m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"
    RED    = "\033[31m"
    BLUE   = "\033[34m"

def hi(text: str, color: str) -> str:
    return f"{color}{text}{C.RESET}"

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ══════════════════════════════════════════════════════════
#  1. 회의록 파싱 & 청킹
# ══════════════════════════════════════════════════════════
def _clean_text(text: str) -> str:
    """STT 보정 주석 제거 + 공백 정리."""
    text = _STT_COMMENT_RE.sub("", text)
    return re.sub(r'\s+', ' ', text).strip()


def _meeting_title(content: str, fallback: str) -> str:
    m = re.search(r'^title:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r'^# (.+)$', content, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def _meeting_date(content: str, fallback: str) -> str:
    m = re.search(r'^date:\s*(.+)$', content, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def parse_utterances(content: str) -> List[Dict]:
    """회의록 본문에서 사람 발화만 추출(Claude 개입/품질 블록 제외)."""
    utterances = []
    for line in content.splitlines():
        m = _UTTER_RE.match(line)
        if not m:
            continue
        text = _clean_text(m.group(3))
        if not text:
            continue
        utterances.append({
            "time":    m.group(1),
            "speaker": m.group(2).strip(),
            "text":    nfc(text),
        })
    return utterances


def chunk_utterances(utterances: List[Dict]) -> List[Dict]:
    """연속 발화를 ~CHUNK_MAX_CHARS 단위로 병합(발화 경계는 보존)."""
    chunks: List[Dict] = []
    buf: List[Dict] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if not buf:
            return
        speakers = list(dict.fromkeys(u["speaker"] for u in buf))
        chunks.append({
            "start_time": buf[0]["time"],
            "end_time":   buf[-1]["time"],
            "speaker":    ", ".join(speakers),
            "text":       " ".join(u["text"] for u in buf),
        })
        buf, buf_len = [], 0

    for u in utterances:
        ulen = len(u["text"])
        # 단일 발화가 상한을 넘으면 그 자체로 한 청크
        if ulen >= CHUNK_MAX_CHARS:
            flush()
            buf, buf_len = [u], ulen
            flush()
            continue
        if buf_len + ulen > CHUNK_MAX_CHARS:
            flush()
        buf.append(u)
        buf_len += ulen
    flush()
    return chunks


def build_chunks_for_file(path: Path) -> Tuple[str, str, List[Dict]]:
    """파일 → (title, date, chunks)."""
    content = path.read_text(encoding="utf-8")
    title = _meeting_title(content, path.stem)
    date  = _meeting_date(content, path.stem)
    chunks = chunk_utterances(parse_utterances(content))
    return title, date, chunks


# ══════════════════════════════════════════════════════════
#  2. 임베딩 백엔드 (graceful degradation)
# ══════════════════════════════════════════════════════════
class STBackend:
    """sentence-transformers multilingual-e5-small (진짜 시맨틱)."""
    id = "st:" + EMBED_MODEL_ID

    def __init__(self, model):
        self.model = model

    @classmethod
    def try_load(cls) -> Optional["STBackend"]:
        try:
            from sentence_transformers import SentenceTransformer
        except Exception:
            return None
        try:
            return cls(SentenceTransformer(EMBED_MODEL_ID))
        except Exception as e:
            print(hi(f"⚠️  임베딩 모델 로드 실패({e}) — 폴백 사용", C.GRAY), file=sys.stderr)
            return None

    def encode(self, texts: List[str], is_query: bool = False) -> np.ndarray:
        # e5 계열은 query:/passage: 프리픽스가 품질에 중요
        prefix = "query: " if is_query else "passage: "
        vecs = self.model.encode([prefix + t for t in texts],
                                 normalize_embeddings=True,
                                 show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)


def embed_backend_id() -> str:
    """모델 로드 없이 사용 가능한 영속 임베딩 백엔드 id만 판별(가벼움)."""
    try:
        import sentence_transformers  # noqa: F401
        return STBackend.id
    except Exception:
        return "none"


_embedder_cache: Dict[str, Optional["STBackend"]] = {}   # embed_id → 로드된 모델(싱글톤)
_embedder_lock = threading.Lock()


def load_embed_backend() -> Optional[STBackend]:
    """임베딩 백엔드 반환. 모델은 프로세스당 1회만 로드(서버 검색마다 재로드 방지)."""
    eid = embed_backend_id()
    if eid == "none":
        return None
    with _embedder_lock:
        if eid not in _embedder_cache:
            _embedder_cache[eid] = STBackend.try_load()   # None이면 None 캐시(재시도 안 함)
        return _embedder_cache[eid]


# ── TF-IDF 폴백 (sklearn, 설치 불필요·인메모리) ──────────────
class TfidfRanker:
    label = "TF-IDF(char 2-4gram)"

    def __init__(self, texts: List[str]):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                                   min_df=1, sublinear_tf=True)
        self.matrix = self.vec.fit_transform(texts)  # (n_chunks, vocab)

    @classmethod
    def available(cls) -> bool:
        try:
            import sklearn  # noqa: F401
            return True
        except Exception:
            return False

    def rank(self, query: str, k: int) -> List[Tuple[int, float]]:
        from sklearn.metrics.pairwise import linear_kernel
        qv = self.vec.transform([query])
        scores = linear_kernel(qv, self.matrix).ravel()
        idx = np.argsort(-scores)[:k]
        return [(int(i), float(scores[i])) for i in idx if scores[i] > 0]


# ══════════════════════════════════════════════════════════
#  3. SQLite 색인 (FTS5 + 벡터)
# ══════════════════════════════════════════════════════════
_reindex_lock = threading.Lock()   # 동시 색인 쓰기 직렬화(웹 서버 멀티스레드 대응)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(INDEX_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # 읽기-쓰기 동시성
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _init_schema(con: sqlite3.Connection):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY,
        file TEXT, title TEXT, date TEXT,
        speaker TEXT, start_time TEXT, end_time TEXT, text TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        text, tokenize='trigram'
    );
    CREATE TABLE IF NOT EXISTS vectors (
        chunk_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB
    );
    CREATE TABLE IF NOT EXISTS files (
        file TEXT PRIMARY KEY, mtime REAL, n_chunks INTEGER
    );
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file);
    """)
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('version',?)",
                (str(INDEX_VERSION),))
    con.commit()


def _meta_get(con, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _delete_file(con, fname: str):
    ids = [r["id"] for r in con.execute("SELECT id FROM chunks WHERE file=?", (fname,))]
    for cid in ids:
        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
        con.execute("DELETE FROM vectors WHERE chunk_id=?", (cid,))
    con.execute("DELETE FROM chunks WHERE file=?", (fname,))
    con.execute("DELETE FROM files WHERE file=?", (fname,))


def _index_file(con, path: Path, embedder: Optional[STBackend]):
    fname = path.name
    _delete_file(con, fname)
    title, date, chunks = build_chunks_for_file(path)
    if not chunks:
        con.execute("INSERT OR REPLACE INTO files(file,mtime,n_chunks) VALUES(?,?,0)",
                    (fname, path.stat().st_mtime))
        return 0

    vecs = embedder.encode([c["text"] for c in chunks]) if embedder else None
    for i, c in enumerate(chunks):
        cur = con.execute(
            "INSERT INTO chunks(file,title,date,speaker,start_time,end_time,text)"
            " VALUES(?,?,?,?,?,?,?)",
            (fname, title, date, c["speaker"], c["start_time"], c["end_time"], c["text"]))
        cid = cur.lastrowid
        con.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cid, c["text"]))
        if vecs is not None:
            v = vecs[i].astype(np.float32)
            con.execute("INSERT INTO vectors(chunk_id,dim,vec) VALUES(?,?,?)",
                        (cid, v.shape[0], v.tobytes()))
    con.execute("INSERT OR REPLACE INTO files(file,mtime,n_chunks) VALUES(?,?,?)",
                (fname, path.stat().st_mtime, len(chunks)))
    return len(chunks)


def reindex(rebuild: bool = False, verbose: bool = True) -> Dict:
    """변경된 회의록만 색인(증분). rebuild=True면 전체 재구축. (쓰기 직렬화)"""
    with _reindex_lock:
        return _do_reindex(rebuild, verbose)


def _do_reindex(rebuild: bool = False, verbose: bool = True) -> Dict:
    if rebuild and INDEX_DB.exists():
        INDEX_DB.unlink()
    con = _connect()
    _init_schema(con)

    # 모델 로드 없이 백엔드 id만 확인 → 변경 감지 시 전체 재색인
    embed_id = embed_backend_id()
    if _meta_get(con, "embed_id") not in (None, embed_id):
        if verbose:
            print(hi("ℹ️  임베딩 백엔드 변경 감지 — 전체 재색인", C.GRAY))
        con.close(); INDEX_DB.unlink()
        con = _connect(); _init_schema(con)
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embed_id',?)", (embed_id,))

    disk = {p.name: p for p in OUTPUT_DIR.glob("meeting_*.md")}
    known = {r["file"]: r["mtime"] for r in con.execute("SELECT file,mtime FROM files")}

    added = updated = removed = 0
    for fname in list(known):                       # 삭제된 파일 정리
        if fname not in disk:
            _delete_file(con, fname); removed += 1

    todo = [(fname, path) for fname, path in disk.items()
            if not (fname in known and abs(known[fname] - path.stat().st_mtime) < 1e-6)]

    # 색인할 파일이 있을 때만 임베딩 모델 로드(평상시 검색은 모델 로드 0회)
    embedder = None
    if todo and embed_id != "none":
        embedder = load_embed_backend()
        if embedder is None:                        # 로드 실패 → 키워드/TF-IDF로 격하
            embed_id = "none"
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('embed_id','none')")

    for fname, path in todo:
        _index_file(con, path, embedder)
        if fname in known: updated += 1
        else:              added += 1

    con.commit(); con.close()
    if verbose and (added or updated or removed):
        print(hi(f"✅ 색인 갱신: +{added} 신규 / ~{updated} 변경 / -{removed} 삭제", C.GREEN))
    return {"backend": embed_id, "added": added, "updated": updated, "removed": removed}


# ══════════════════════════════════════════════════════════
#  4. 검색 (키워드 / 벡터 / 하이브리드)
# ══════════════════════════════════════════════════════════
def _row_to_hit(row: sqlite3.Row, score: float) -> Dict:
    return {
        "id":      row["id"],
        "file":    row["file"],
        "title":   row["title"],
        "date":    row["date"],
        "speaker": row["speaker"],
        "time":    row["start_time"],
        "text":    row["text"],
        "score":   round(score, 4),
    }


def _filter_clause(date: str, speaker: str) -> Tuple[str, list]:
    clause, params = [], []
    if date:
        clause.append("c.date LIKE ?"); params.append(f"%{date}%")
    if speaker:
        clause.append("c.speaker LIKE ?"); params.append(f"%{speaker}%")
    return (" AND " + " AND ".join(clause)) if clause else "", params


def keyword_search(con, query: str, k: int, date="", speaker="") -> List[Tuple[int, float]]:
    """FTS5 trigram + BM25. 3자 미만 질의는 LIKE로 폴백."""
    extra, fparams = _filter_clause(date, speaker)
    q = query.strip()
    if len(q) >= 3:
        match = '"' + q.replace('"', '""') + '"'
        sql = (f"SELECT c.id AS id, bm25(chunks_fts) AS rank FROM chunks_fts "
               f"JOIN chunks c ON c.id = chunks_fts.rowid "
               f"WHERE chunks_fts MATCH ?{extra} ORDER BY rank LIMIT ?")
        try:
            rows = con.execute(sql, [match, *fparams, k]).fetchall()
            return [(r["id"], -float(r["rank"])) for r in rows]  # bm25는 작을수록 좋음
        except sqlite3.OperationalError:
            pass
    # 폴백: 부분 문자열 매칭
    sql = (f"SELECT c.id AS id FROM chunks c WHERE c.text LIKE ?{extra} LIMIT ?")
    rows = con.execute(sql, [f"%{q}%", *fparams, k]).fetchall()
    return [(r["id"], 1.0) for r in rows]


def _load_vectors(con, date="", speaker="") -> Tuple[List[int], np.ndarray]:
    extra, params = _filter_clause(date, speaker)
    sql = (f"SELECT v.chunk_id AS id, v.vec AS vec FROM vectors v "
           f"JOIN chunks c ON c.id = v.chunk_id WHERE 1=1{extra}")
    rows = con.execute(sql, params).fetchall()
    if not rows:
        return [], np.empty((0, 0), dtype=np.float32)
    ids = [r["id"] for r in rows]
    mat = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    return ids, mat


def vector_search(con, query: str, k: int, embedder: Optional[STBackend],
                  date="", speaker="") -> List[Tuple[int, float]]:
    """임베딩 코사인(브루트포스). 임베딩 없으면 TF-IDF 폴백."""
    ids, mat = _load_vectors(con, date, speaker)
    if embedder is not None and len(ids):
        q = embedder.encode([query], is_query=True)[0]
        scores = mat @ q                            # 정규화돼 있어 dot=cosine
        order = np.argsort(-scores)[:k]
        return [(ids[i], float(scores[i])) for i in order]
    # 폴백: TF-IDF (전체 청크 대상, 인메모리)
    if TfidfRanker.available():
        extra, params = _filter_clause(date, speaker)
        rows = con.execute(
            f"SELECT c.id AS id, c.text AS text FROM chunks c WHERE 1=1{extra}",
            params).fetchall()
        if not rows:
            return []
        ranker = TfidfRanker([r["text"] for r in rows])
        return [(rows[i]["id"], s) for i, s in ranker.rank(query, k)]
    return []


def _rrf_merge(*ranked_lists: List[Tuple[int, float]], k: int = RRF_K) -> List[int]:
    """Reciprocal Rank Fusion: 여러 랭킹을 순위 기반으로 병합."""
    scores: Dict[int, float] = {}
    for lst in ranked_lists:
        for rank, (cid, _) in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return [cid for cid, _ in sorted(scores.items(), key=lambda x: -x[1])]


def search(query: str, top_k: int = 8, mode: str = "hybrid",
           date: str = "", speaker: str = "", auto_reindex: bool = True) -> Dict:
    """
    공개 API. mode: hybrid | keyword | semantic.
    반환: {ok, query, mode, backend, total, results:[{file,title,date,hits:[...]}]}
    """
    if auto_reindex:
        reindex(verbose=False)
    if not INDEX_DB.exists():
        return {"ok": False, "msg": "색인 없음", "results": [], "total": 0}

    con = _connect()
    embed_id = _meta_get(con, "embed_id") or "none"
    embedder = load_embed_backend() if (mode != "keyword" and embed_id != "none") else None
    backend = (embedder.id if embedder else
               (TfidfRanker.label if TfidfRanker.available() else "keyword-only"))

    pool = max(top_k * 3, 20)
    if mode == "keyword":
        backend = "FTS5(trigram)"
        ranked = keyword_search(con, query, pool, date, speaker)
        order  = [cid for cid, _ in ranked]
    elif mode == "semantic":
        ranked = vector_search(con, query, pool, embedder, date, speaker)
        order  = [cid for cid, _ in ranked]
    else:  # hybrid
        kw  = keyword_search(con, query, pool, date, speaker)
        vec = vector_search(con, query, pool, embedder, date, speaker)
        order = _rrf_merge(kw, vec)
        if mode == "hybrid":
            backend = f"hybrid(FTS5 + {backend})"

    order = order[:top_k]
    if not order:
        con.close()
        return {"ok": True, "query": query, "mode": mode, "backend": backend,
                "total": 0, "results": []}

    placeholders = ",".join("?" * len(order))
    rows = {r["id"]: r for r in con.execute(
        f"SELECT * FROM chunks WHERE id IN ({placeholders})", order)}
    con.close()

    grouped: Dict[str, Dict] = {}
    for rank, cid in enumerate(order):
        row = rows.get(cid)
        if not row:
            continue
        hit = _row_to_hit(row, 1.0 / (rank + 1))
        g = grouped.setdefault(row["file"], {
            "file": row["file"], "title": row["title"],
            "date": row["date"], "hits": []})
        g["hits"].append(hit)

    return {"ok": True, "query": query, "mode": mode, "backend": backend,
            "total": len(order), "results": list(grouped.values())}


# ══════════════════════════════════════════════════════════
#  5. CLI 출력
# ══════════════════════════════════════════════════════════
def _highlight(text: str, query: str) -> str:
    try:
        return re.sub(re.escape(query),
                      lambda m: hi(m.group(), C.YELLOW + C.BOLD), text,
                      flags=re.IGNORECASE)
    except re.error:
        return text


def print_search(query: str, mode: str, top_k: int, date: str, speaker: str):
    res = search(query, top_k=top_k, mode=mode, date=date, speaker=speaker)
    if not res.get("ok"):
        print(hi(f"  {res.get('msg','검색 실패')}", C.RED)); return

    label = {"hybrid": "하이브리드", "keyword": "키워드", "semantic": "시맨틱"}.get(mode, mode)
    title_line = hi(f'🔍 "{query}" {label} 검색', C.BOLD)
    backend_tag = hi(f"[{res['backend']}]", C.GRAY)
    print(f"\n{title_line} {backend_tag}\n")

    if res["total"] == 0:
        print(hi("  검색 결과 없음", C.RED)); return

    for r in res["results"]:
        head = r["title"]
        if r.get("date"):
            head += f"  {hi(r['date'], C.GRAY)}"
        count_tag = hi(f"({len(r['hits'])}건)", C.YELLOW)
        print(f"  {hi('📄 ' + head, C.CYAN)}  {count_tag}")
        print(f"  {hi(r['file'], C.GRAY)}\n")
        for h in r["hits"]:
            meta = f"[{h['time']}] {h['speaker']}"
            snippet = h["text"]
            if len(snippet) > 220:
                snippet = snippet[:220] + "…"
            print(f"  {hi('▶', C.GREEN)} {hi(meta, C.BLUE)}")
            print(f"    {_highlight(snippet, query)}\n")
    print(hi(f"  총 {res['total']}건", C.GREEN))
    if "TF-IDF" in res["backend"]:
        print(hi("  💡 진짜 의미 기반 검색을 원하면: "
                 ".venv/bin/pip install sentence-transformers", C.GRAY))


# ── 회의 목록 ──────────────────────────────────────────────
def list_meetings():
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if not files:
        print("  회의 파일이 없습니다."); return
    print(f"\n{hi('📁 전체 회의 목록', C.BOLD)}\n")
    for f in files:
        content = f.read_text(encoding="utf-8")
        title = _meeting_title(content, f.stem)
        p_match = re.search(r'^participants:\s*(.+)$', content, re.MULTILINE)
        participants = p_match.group(1).strip() if p_match else "-"
        n = len([l for l in content.splitlines() if _UTTER_RE.match(l)])
        print(f"  {hi(title, C.CYAN)}")
        print(f"  {hi('참여자:', C.GRAY)} {participants}  {hi(f'{n}개 발화', C.GRAY)}")
        print(f"  {hi('파일:', C.GRAY)} {f.name}\n")


# ── 요약 목록 ──────────────────────────────────────────────
def list_summaries():
    files = sorted(OUTPUT_DIR.glob("meeting_*.md"), reverse=True)
    if not files:
        print("  회의 파일이 없습니다."); return
    print(f"\n{hi('📋 최근 회의 요약', C.BOLD)}\n")
    for f in files[:5]:
        content = f.read_text(encoding="utf-8")
        title = _meeting_title(content, f.stem)
        print(f"  {hi('━' * 45, C.GRAY)}")
        print(f"  {hi(title, C.CYAN)}")
        if "# 🤖 AI 회의 분석" in content:
            analysis = content.split("# 🤖 AI 회의 분석")[-1]
            if "## 📋 회의 요약" in analysis:
                summary = analysis.split("## 📋 회의 요약")[-1].split("##")[0].strip()
                for line in summary.splitlines()[:5]:
                    if line.strip():
                        print(f"    {line.strip()}")
        else:
            print(f"  {hi('  (AI 분석 없음 — 회의 종료 시 자동 생성됨)', C.GRAY)}")
        print()


# ══════════════════════════════════════════════════════════
#  6. 메인
# ══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="회의록 하이브리드 RAG 검색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python3 search.py 브레이즈 데이터 통합
  python3 search.py 마감일 --date 2026-04
  python3 search.py API --speaker 신인수 --keyword
  python3 search.py 쿠폰 전략 --semantic --top 10
  python3 search.py --reindex
  python3 search.py --list
        """)
    parser.add_argument("query", nargs="*", help="검색 질의")
    parser.add_argument("--keyword",  action="store_true", help="키워드(FTS5)만")
    parser.add_argument("--semantic", action="store_true", help="시맨틱(벡터)만")
    parser.add_argument("--top",      type=int, default=8,  help="상위 N개 (기본 8)")
    parser.add_argument("--date",     default="", help="날짜 필터 (예: 2026-04)")
    parser.add_argument("--speaker",  default="", help="발화자 필터")
    parser.add_argument("--reindex",  action="store_true", help="변경분 색인 갱신")
    parser.add_argument("--rebuild",  action="store_true", help="색인 전체 재구축")
    parser.add_argument("--list",     action="store_true", help="전체 회의 목록")
    parser.add_argument("--summary",  action="store_true", help="최근 회의 요약 목록")
    args = parser.parse_args()

    if not OUTPUT_DIR.exists():
        print("  meetings/ 폴더가 없습니다. meeting.py를 먼저 실행하세요."); return

    if args.rebuild:
        reindex(rebuild=True); return
    if args.reindex:
        reindex(); return
    if args.list:
        list_meetings(); return
    if args.summary:
        list_summaries(); return

    query = " ".join(args.query).strip()
    if not query:
        parser.print_help(); return

    mode = "keyword" if args.keyword else "semantic" if args.semantic else "hybrid"
    print_search(query, mode, args.top, args.date, args.speaker)


if __name__ == "__main__":
    main()
