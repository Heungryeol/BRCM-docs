"""
brcm-docs 명령

  doctor                 사이트 접근·robots.txt·의존성 점검
  plan   [-s kb,...]     sitemap/저장소를 읽어 수집 대기열 채우기 (본문은 받지 않음)
  crawl  [-s kb,...]     대기열 수집 (중단 후 다시 실행하면 이어서, 바뀐 문서만 다시 받음)
  import PATH...         브라우저로 받은 PDF/HTML/MD 넣기 (techdocs 가 자동 수집을 막을 때)
  status                 수집 현황
  search "질의"          전문검색 시험
  export [-f sqlite|md|jsonl]   결과 묶음 만들기 (dist/)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import yaml

from . import __version__
from .http import Blocked, Fetcher
from .log import fmt_dur, log
from .sources import FETCHERS, import_files, plan_kb, plan_techdocs, plan_urls, sync_github
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
ALL_SOURCES = ["github", "urls", "kb", "techdocs"]


# --------------------------------------------------------------------------- 설정
def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | None) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    for p in ([Path(path)] if path else [ROOT / "config" / "local.yaml"]):
        if p.is_file():
            cfg = _merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return cfg


def data_dir(cfg: dict) -> Path:
    d = Path(cfg.get("data_dir") or "data")
    return d if d.is_absolute() else ROOT / d


def open_store(cfg: dict) -> Store:
    return Store(data_dir(cfg) / "corpus.sqlite")


def sources_arg(s: str | None, cfg: dict) -> list[str]:
    want = [x.strip() for x in (s or ",".join(ALL_SOURCES)).split(",") if x.strip()]
    bad = [x for x in want if x not in ALL_SOURCES]
    if bad:
        raise SystemExit(f"알 수 없는 출처: {bad} (가능: {', '.join(ALL_SOURCES)})")
    return [x for x in want if (cfg.get(x) or {}).get("enabled", True)]


def make_fetcher(cfg: dict, args) -> Fetcher:
    return Fetcher(cfg["user_agent"], float(getattr(args, "delay", None) or cfg.get("delay", 2.0)))


# --------------------------------------------------------------------------- doctor
def cmd_doctor(cfg, args) -> int:
    f = make_fetcher(cfg, args)
    ok = True
    print(f"brcm-docs {__version__} · User-Agent: {cfg['user_agent']}")
    for mod, hint in (("yaml", "apt install python3-yaml"), ("pypdf|PyPDF2", "apt install python3-pypdf2")):
        try:
            __import__(mod.split("|")[0])
            print(f"  [ok]   {mod}")
        except ImportError:
            alt = mod.split("|")[1:] or []
            if any(_try(a) for a in alt):
                print(f"  [ok]   {mod}")
            else:
                print(f"  [주의] {mod} 없음 — {hint}")
    print(f"  [{'ok' if shutil.which('git') else '없음'}]   git")
    checks = []
    if cfg["kb"].get("enabled"):
        checks.append(("kb", cfg["kb"]["sitemap"]))
    if cfg["techdocs"].get("enabled"):
        checks.append(("techdocs", cfg["techdocs"]["sitemap"]))
    if cfg["urls"].get("enabled") and cfg["urls"].get("list"):
        checks.append(("urls", cfg["urls"]["list"][0]))
    for name, url in checks:
        rp, note = f.robots(url)
        if rp is None:
            print(f"  [{'차단' if '막고' in note else '접속 실패'}] {name:<9} {note}")
            ok = False
            continue
        if not rp.can_fetch(f.ua, url):
            print(f"  [제외] {name:<9} robots.txt 가 이 주소를 허용하지 않음: {url}")
            ok = False
            continue
        r = f.get(url)
        print(f"  [{'ok' if r.ok else '실패'}]   {name:<9} {r.status or r.error} {r.content_type} "
              f"{len(r.data):,}B · {note} · 요청 간격 {f.wait_for(url):.0f}s")
        ok &= r.ok
    if not ok:
        print("\n자동 수집이 막힌 사이트는 우회하지 않습니다. 브라우저로 받은 PDF/HTML 을 "
              "`./brcm-docs import <폴더>` 로 넣을 수 있습니다.")
    return 0 if ok else 1


def _try(m: str) -> bool:
    try:
        __import__(m)
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- plan / crawl
def do_plan(cfg, store: Store, f: Fetcher, srcs: list[str], args) -> None:
    for s in srcs:
        if f.stop.is_set():
            return
        try:
            if s == "kb":
                plan_kb(cfg, f, store, slug_skip=not getattr(args, "no_slug_skip", False))
            elif s == "techdocs":
                plan_techdocs(cfg, f, store)
            elif s == "urls":
                n = plan_urls(cfg, store)
                log(f"URL 목록 {n}건")
            elif s == "github":
                st = sync_github(cfg, store, data_dir(cfg))
                log(f"GitHub 동기화: {st}")
        except Blocked as e:
            log(f"{s}: 수집하지 않음 — {e}")
        except Exception as e:  # noqa: BLE001
            log(f"{s}: 대기열 갱신 실패 — {type(e).__name__}: {e}")
    store.set_meta("planned_at", time.strftime("%Y-%m-%dT%H:%M:%S"))


def cmd_plan(cfg, args) -> int:
    store, f = open_store(cfg), make_fetcher(cfg, args)
    _signals(f)
    do_plan(cfg, store, f, sources_arg(args.source, cfg), args)
    print_status(store, f)
    return 0


def cmd_crawl(cfg, args) -> int:
    store, f = open_store(cfg), make_fetcher(cfg, args)
    _signals(f)
    srcs = sources_arg(args.source, cfg)
    if args.retry_errors:
        log(f"오류·차단 항목 {store.retry_errors()}건을 다시 대기열에")
    if not args.no_plan:
        do_plan(cfg, store, f, srcs, args)
    deadline = time.time() + args.hours * 3600 if args.hours else None
    total_done = 0
    for s in srcs:
        if s not in FETCHERS:
            continue
        handler = FETCHERS[s]
        pending = store.db.execute("SELECT COUNT(*) FROM queue WHERE source=? AND state='pending'", (s,)).fetchone()[0]
        if not pending:
            continue
        log(f"[{s}] 수집 시작 — 대기 {pending:,}건")
        t0, n, cnt = time.time(), 0, {"done": 0, "skipped": 0, "gone": 0, "error": 0}
        blocked = False
        while not f.stop.is_set() and not blocked:
            rows = store.pick(s, 100)
            if not rows:
                break
            for row in rows:
                if f.stop.is_set() or (deadline and time.time() > deadline) or (args.max and total_done >= args.max):
                    f.stop.set()
                    break
                try:
                    st = handler(cfg, f, store, row)
                    store.mark(row["url"], st)
                except Blocked as e:
                    store.mark(row["url"], "blocked", str(e))
                    log(f"[{s}] 사이트가 수집을 허용하지 않아 중단: {e}")
                    blocked = True
                    break
                except Exception as e:  # noqa: BLE001
                    st = "error"
                    store.mark(row["url"], "error", f"{type(e).__name__}: {e}"[:300])
                    log(f"  오류 {row['url']}: {e}")
                cnt[st] = cnt.get(st, 0) + 1
                n += 1
                total_done += 1
                if n % 50 == 0:
                    rate = (time.time() - t0) / n
                    left = pending - n
                    log(f"[{s}] {n:,}/{pending:,} (저장 {cnt['done']:,} · 범위 밖 {cnt['skipped']:,} · "
                        f"삭제됨 {cnt['gone']:,} · 오류 {cnt['error']:,}) 남은 시간 약 {fmt_dur(left * rate)}")
        log(f"[{s}] {n:,}건 처리 — {cnt}")
    if f.stop.is_set():
        log("중지됨 — 같은 명령을 다시 실행하면 이어서 수집합니다.")
    store.set_meta("crawled_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    print_status(store, f)
    return 0


def _signals(f: Fetcher) -> None:
    def h(sig, _frm):
        if f.stop.is_set():
            raise SystemExit(130)
        log("중지 요청 — 현재 문서까지 저장하고 멈춥니다 (한 번 더 누르면 즉시 종료)")
        f.stop.set()
    signal.signal(signal.SIGINT, h)
    signal.signal(signal.SIGTERM, h)


# --------------------------------------------------------------------------- status / search
def print_status(store: Store, f: Fetcher | None = None) -> None:
    q, d = store.queue_stats(), store.doc_stats()
    print("\n대기열 (URL)")
    for s, st in sorted(q.items()):
        line = " · ".join(f"{k} {v:,}" for k, v in sorted(st.items()))
        pend = st.get("pending", 0)
        eta = ""
        if pend and f:
            per = max(f.delay, 10.0) if s == "techdocs" else f.delay   # techdocs robots.txt crawl-delay 10s
            eta = f"  (남은 예상 {fmt_dur(pend * (per + 0.6))})"
        print(f"  {s:<9} {line}{eta}")
    print("저장된 문서")
    for s, st in sorted((k, v) for k, v in d.items() if not k.startswith("_")):
        mb = (st.get("chars", 0) or 0) / 1e6
        print(f"  {s:<9} " + " · ".join(f"{k} {v:,}" for k, v in sorted(st.items()) if k != "chars") + f" · 본문 {mb:,.1f}M자")
    print(f"섹션(검색 단위) {d.get('_sections', 0):,}개 · DB {store.path} ({store.path.stat().st_size / 1e6:,.0f}MB)")


def cmd_status(cfg, args) -> int:
    store = open_store(cfg)
    print_status(store, make_fetcher(cfg, args))
    errs = store.db.execute("SELECT source, note, COUNT(*) n FROM queue WHERE state IN ('error','blocked') "
                            "GROUP BY source, substr(note,1,60) ORDER BY n DESC LIMIT 10").fetchall()
    if errs:
        print("오류 상위")
        for r in errs:
            print(f"  {r['source']:<9} {r['n']:>6,}  {r['note']}")
    return 0


def cmd_search(cfg, args) -> int:
    store = open_store(cfg)
    for r in store.search(" ".join(args.query), args.n, args.source):
        print(f"[{r['source']}] {r['title']}  ({r['id']})\n   {r['url']}\n   § {r['heading'] or '-'}\n   {r['snip']}\n")
    return 0


def cmd_import(cfg, args) -> int:
    store = open_store(cfg)
    st = import_files(store, [Path(p) for p in args.paths], args.as_source)
    print(st)
    return 0


# --------------------------------------------------------------------------- export
def _where(args) -> tuple[str, list]:
    w, a = ["d.status='ok'"], []
    if args.source:
        srcs = [s.strip() for s in args.source.split(",")]
        w.append("d.source IN (%s)" % ",".join("?" * len(srcs)))
        a += srcs
    return " AND ".join(w), a


def cmd_export(cfg, args) -> int:
    store = open_store(cfg)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    where, wa = _where(args)
    prx = re.compile(args.product, re.I) if args.product else None
    docs = [dict(r) for r in store.db.execute(f"SELECT * FROM doc d WHERE {where} ORDER BY d.source, d.id", wa)]
    if prx:
        docs = [d for d in docs if d["source"] != "kb" or prx.search((d["products"] or "") + " " + (d["title"] or ""))]
    manifest = {"name": "brcm-docs", "version": __version__, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "format": args.format, "docs": len(docs),
                "by_source": {s: sum(1 for d in docs if d["source"] == s) for s in sorted({d["source"] for d in docs})},
                "filters": {"source": args.source, "product": args.product}}
    tmp = Path(tempfile.mkdtemp(prefix="brcm-export-", dir=out))
    base = tmp / f"brcm-docs-{stamp}"
    base.mkdir()
    try:
        if args.format == "sqlite":
            store.db.commit()
            dbp = base / "corpus.sqlite"
            store.db.execute("VACUUM INTO ?", (str(dbp),))
            c = sqlite3.connect(dbp)
            ids = {d["id"] for d in docs}
            c.execute("DROP TABLE IF EXISTS queue")
            c.execute("CREATE TEMP TABLE keep(id TEXT PRIMARY KEY)")
            c.executemany("INSERT INTO keep VALUES (?)", [(i,) for i in ids])
            c.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM section WHERE doc_id NOT IN (SELECT id FROM keep))")
            c.execute("DELETE FROM section WHERE doc_id NOT IN (SELECT id FROM keep)")
            c.execute("DELETE FROM doc WHERE id NOT IN (SELECT id FROM keep)")
            c.execute("INSERT INTO fts(fts) VALUES ('optimize')")
            c.commit()
            c.execute("VACUUM")
            c.close()
        else:
            secs: dict[str, list] = {}
            for r in store.db.execute("SELECT doc_id, heading, text FROM section ORDER BY doc_id, seq"):
                secs.setdefault(r[0], []).append((r[1], r[2]))
            if args.format == "jsonl":
                with open(base / "sections.jsonl", "w", encoding="utf-8") as fp:
                    for d in docs:
                        for h, t in secs.get(d["id"], []):
                            fp.write(json.dumps({"doc_id": d["id"], "source": d["source"], "url": d["url"], "title": d["title"],
                                                 "products": d["products"], "updated": d["updated"], "heading": h, "text": t},
                                                ensure_ascii=False) + "\n")
            else:   # md: 문서 1건 = 파일 1개 (PAIS 등 RAG 데이터 소스에 그대로 올리기 좋게)
                for d in docs:
                    name = re.sub(r"[^\w.-]+", "_", d["id"])[:150] + ".md"
                    p = base / d["source"] / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    meta = [f"# {d['title']}", ""]
                    for k, label in (("url", "Source"), ("products", "Products"), ("updated", "Updated")):
                        if d.get(k):
                            meta.append(f"- {label}: {d[k]}")
                    body = []
                    for h, t in secs.get(d["id"], []):
                        body += [f"\n## {h}" if h else "", t]
                    p.write_text("\n".join(meta + body).strip() + "\n", encoding="utf-8")
        (base / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tgz = out / f"brcm-docs-{args.format}-{stamp}.tar.gz"
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(base, arcname=base.name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{tgz}  ({tgz.stat().st_size / 1e6:,.1f}MB) — 문서 {len(docs):,}건 {manifest['by_source']}")
    return 0


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="brcm-docs", description="Broadcom VMware 문서 수집기 (KB · techdocs · 베스트 프랙티스)")
    ap.add_argument("--config", help="추가 설정 파일 (기본 config/local.yaml)")
    ap.add_argument("--delay", type=float, help="요청 간 최소 간격(초)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="접근·의존성 점검")
    p = sub.add_parser("plan", help="대기열 채우기")
    p.add_argument("-s", "--source")
    p.add_argument("--no-slug-skip", action="store_true", help="KB slug 로 비 VMware 판정하지 않고 전부 받기")
    p = sub.add_parser("crawl", help="수집 (이어하기)")
    p.add_argument("-s", "--source", help="kb,techdocs,github,urls (기본 전부)")
    p.add_argument("--hours", type=float, help="이 시간만 돌고 멈춤")
    p.add_argument("--max", type=int, help="최대 문서 수 (시험용)")
    p.add_argument("--no-plan", action="store_true", help="sitemap 갱신 없이 남은 대기열만")
    p.add_argument("--retry-errors", action="store_true", help="오류·차단 항목 다시 시도")
    p.add_argument("--no-slug-skip", action="store_true")
    p = sub.add_parser("import", help="로컬 PDF/HTML/MD 넣기")
    p.add_argument("paths", nargs="+")
    p.add_argument("--as-source", default="local", help="출처 이름 (기본 local, 예: techdocs-local)")
    sub.add_parser("status", help="현황")
    p = sub.add_parser("search", help="전문검색")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", type=int, default=8)
    p.add_argument("-s", "--source")
    p = sub.add_parser("export", help="결과 묶음")
    p.add_argument("-f", "--format", choices=["sqlite", "md", "jsonl"], default="sqlite")
    p.add_argument("-o", "--out", default=str(ROOT / "dist"))
    p.add_argument("-s", "--source", help="출처 제한 (예: kb,github)")
    p.add_argument("--product", help="KB 제품 정규식 (예: 'vSphere|ESXi|vSAN')")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    return {"doctor": cmd_doctor, "plan": cmd_plan, "crawl": cmd_crawl, "import": cmd_import, "status": cmd_status,
            "search": cmd_search, "export": cmd_export}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
