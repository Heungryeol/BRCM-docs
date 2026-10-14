"""
코퍼스 저장소 (SQLite + FTS5).

  doc      문서 1건 (id 예: kb:312060, td:<경로>, gh:vmware/repo:path, url:<URL>, local:<sha>)
  section  문서의 섹션(제목 경로 + 본문). 검색·RAG 단위
  fts      섹션 전문검색 (title/heading/text, porter 형태소)
  queue    수집 대기열 (URL 단위, 재시작·증분 수집용)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS doc(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT, title TEXT, products TEXT, updated TEXT,
  lastmod TEXT, fetched_at TEXT, status TEXT NOT NULL, sha TEXT, chars INTEGER DEFAULT 0, meta TEXT);
CREATE INDEX IF NOT EXISTS doc_src ON doc(source, status);
CREATE TABLE IF NOT EXISTS section(
  id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL, heading TEXT, text TEXT NOT NULL,
  UNIQUE(doc_id, seq));
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, heading, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS queue(
  url TEXT PRIMARY KEY, source TEXT NOT NULL, doc_id TEXT, lastmod TEXT, priority INTEGER DEFAULT 5,
  state TEXT NOT NULL DEFAULT 'pending', tries INTEGER DEFAULT 0, note TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS q_pick ON queue(source, state, priority);
"""
STATES = ("pending", "done", "skipped", "gone", "error", "blocked")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()

    # ------------------------------------------------------------------ meta
    def set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))
        self.db.commit()

    def get_meta(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    # ------------------------------------------------------------------ queue
    def enqueue(self, rows: list[tuple]) -> int:
        """rows: (url, source, doc_id, lastmod, priority[, state, note]). 이미 있으면 lastmod 가 새로울 때만 다시 대기."""
        n0 = self.db.total_changes
        for r in rows:
            url, source, doc_id, lastmod, prio = r[:5]
            state = r[5] if len(r) > 5 else "pending"
            note = r[6] if len(r) > 6 else None
            self.db.execute(
                """INSERT INTO queue(url, source, doc_id, lastmod, priority, state, note, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                     priority=excluded.priority,
                     state=CASE WHEN queue.state IN ('done','skipped','gone') AND excluded.state='pending'
                                 AND COALESCE(excluded.lastmod,'') > COALESCE(queue.lastmod,'') THEN 'pending'
                                WHEN queue.state='skipped' AND queue.note='slug' AND excluded.state='pending' THEN 'pending'
                                ELSE queue.state END,
                     lastmod=COALESCE(excluded.lastmod, queue.lastmod)""",
                (url, source, doc_id, lastmod, prio, state, note, now()))
        self.db.commit()
        return self.db.total_changes - n0

    def pick(self, source: str, limit: int = 200) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM queue WHERE source=? AND state='pending' ORDER BY priority, url LIMIT ?",
            (source, limit)).fetchall()

    def mark(self, url: str, state: str, note: str | None = None) -> None:
        self.db.execute(
            "UPDATE queue SET state=?, note=?, tries=tries+(CASE WHEN ?='error' THEN 1 ELSE 0 END), updated_at=? WHERE url=?",
            (state, note, state, now(), url))
        self.db.commit()

    def retry_errors(self, source: str | None = None, max_tries: int = 5) -> int:
        q = "UPDATE queue SET state='pending' WHERE state IN ('error','blocked') AND tries < ?"
        args: list = [max_tries]
        if source:
            q += " AND source=?"
            args.append(source)
        c = self.db.execute(q, args).rowcount
        self.db.commit()
        return c

    def queue_stats(self) -> dict:
        out: dict = {}
        for r in self.db.execute("SELECT source, state, COUNT(*) n FROM queue GROUP BY source, state"):
            out.setdefault(r["source"], {})[r["state"]] = r["n"]
        return out

    # ------------------------------------------------------------------ doc
    def doc_sha(self, doc_id: str) -> str | None:
        r = self.db.execute("SELECT sha FROM doc WHERE id=?", (doc_id,)).fetchone()
        return r[0] if r else None

    def put_doc(self, doc_id: str, source: str, url: str, title: str, sections: list[tuple[str, str]],
                products: str = "", updated: str = "", lastmod: str | None = None, status: str = "ok",
                meta: dict | None = None) -> bool:
        """문서 저장(교체). 내용이 같으면 False."""
        sha = hashlib.sha256(json.dumps([title, products, sections], ensure_ascii=False).encode()).hexdigest()[:24]
        if self.doc_sha(doc_id) == sha:
            self.db.execute("UPDATE doc SET fetched_at=?, lastmod=COALESCE(?, lastmod) WHERE id=?", (now(), lastmod, doc_id))
            self.db.commit()
            return False
        self._drop_sections(doc_id)
        chars = 0
        for seq, (head, text) in enumerate(sections):
            cur = self.db.execute("INSERT INTO section(doc_id, seq, heading, text) VALUES (?,?,?,?)", (doc_id, seq, head, text))
            self.db.execute("INSERT INTO fts(rowid, title, heading, text) VALUES (?,?,?,?)", (cur.lastrowid, title, head, text))
            chars += len(text)
        self.db.execute(
            """INSERT OR REPLACE INTO doc(id, source, url, title, products, updated, lastmod, fetched_at, status, sha, chars, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, source, url, title, products, updated, lastmod, now(), status, sha, chars,
             json.dumps(meta or {}, ensure_ascii=False)))
        self.db.commit()
        return True

    def put_stub(self, doc_id: str, source: str, url: str, title: str, status: str, products: str = "",
                 lastmod: str | None = None, note: str = "") -> None:
        """범위 밖(skipped)·삭제(gone) 문서는 본문 없이 기록만 — 다음 실행 때 다시 받지 않도록"""
        self._drop_sections(doc_id)
        self.db.execute(
            """INSERT OR REPLACE INTO doc(id, source, url, title, products, lastmod, fetched_at, status, meta)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, source, url, title, products, lastmod, now(), status, json.dumps({"note": note}, ensure_ascii=False)))
        self.db.commit()

    def _drop_sections(self, doc_id: str) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM section WHERE doc_id=?", (doc_id,))]
        if ids:
            self.db.executemany("DELETE FROM fts WHERE rowid=?", [(i,) for i in ids])
            self.db.execute("DELETE FROM section WHERE doc_id=?", (doc_id,))

    def doc_stats(self) -> dict:
        out: dict = {}
        for r in self.db.execute("SELECT source, status, COUNT(*) n, SUM(chars) c FROM doc GROUP BY source, status"):
            d = out.setdefault(r["source"], {})
            d[r["status"]] = r["n"]
            if r["status"] == "ok":
                d["chars"] = r["c"] or 0
        out["_sections"] = self.db.execute("SELECT COUNT(*) FROM section").fetchone()[0]
        return out

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 10, source: str | None = None) -> list[dict]:
        q = """SELECT d.id, d.source, d.url, d.title, d.products, s.heading,
                      snippet(fts, 2, '[', ']', ' … ', 24) AS snip, bm25(fts, 4.0, 2.0, 1.0) AS score
               FROM fts JOIN section s ON s.id = fts.rowid JOIN doc d ON d.id = s.doc_id
               WHERE fts MATCH ?""" + (" AND d.source=?" if source else "") + " ORDER BY score LIMIT ?"
        args = [query] + ([source] if source else []) + [limit]
        try:
            rows = self.db.execute(q, args).fetchall()
        except sqlite3.OperationalError:
            terms = " ".join('"' + t.replace('"', "") + '"' for t in query.split())
            rows = self.db.execute(q, [terms] + args[1:]).fetchall()
        return [dict(r) for r in rows]
